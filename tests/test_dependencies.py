"""跨区依赖处置：回滚 / 隔离放行、互斥、并发、对齐与回看一致。"""

import threading


def _dep(authed):
    return authed.get("/api/dependencies").json()["items"][0]


def _dispose(authed, dep, mode, version=None):
    return authed.post(
        f"/api/dependencies/{dep['dependency_id']}/dispose",
        json={
            "mode": mode,
            "expected_version": (
                version if version is not None else dep["dependent_batch"]["version"]
            ),
        },
    )


def test_seed_gap_is_flagged(authed):
    dep = _dep(authed)
    assert dep["gap"] is True
    assert dep["invariant_ok"] is False
    assert dep["status"] == "待处置"
    assert dep["dependent_batch"]["status"] == "已闭合"
    assert dep["upstream_batch"]["complete"] is False


def _batch(authed, zone):
    return next(
        b for b in authed.get("/api/batches").json()["items"] if b["zone_code"] == zone
    )


def test_close_blocked_when_upstream_not_complete(authed):
    dep = _dep(authed)
    # 回滚后银区变收集中，铜区仍未齐套：此时闭合必须被拦
    assert _dispose(authed, dep, "rollback").status_code == 200
    silver = _batch(authed, "silver_orders")
    blocked = authed.post(f"/api/batches/{silver['id']}/close", json={})
    assert blocked.status_code == 409
    assert "未齐套" in blocked.json()["error"]


def test_upstream_complete_unblocks_close_without_disposition(authed):
    dep = _dep(authed)
    assert _dispose(authed, dep, "rollback").status_code == 200
    # 铜区补登记一个对象后闭合 -> 齐套
    bronze = _batch(authed, "bronze_orders")
    authed.post(
        f"/api/batches/{bronze['id']}/objects",
        json={"object_key": "raw/extra.json", "content_hash": "hx", "byte_size": 3},
    )
    assert authed.post(f"/api/batches/{bronze['id']}/close", json={}).status_code == 200

    silver = _batch(authed, "silver_orders")
    ok = authed.post(f"/api/batches/{silver['id']}/close", json={})
    assert ok.status_code == 200
    card = _dep(authed)
    assert card["gap"] is False
    assert card["invariant_ok"] is True
    assert card["chip"] == "回滚"  # 历史处置芯片仍可见


def test_rollback_mode(authed):
    dep = _dep(authed)
    r = _dispose(authed, dep, "rollback")
    assert r.status_code == 200
    body = r.json()
    assert body["silver_status"] == "收集中"
    assert body["chip"] == "回滚"
    assert body["voucher"].startswith("VCH-")

    after = _dep(authed)
    assert after["status"] == "已回滚"
    assert after["dependent_batch"]["status"] == "收集中"
    assert after["dependent_batch"]["closed_object_count"] is None
    assert after["invariant_ok"] is True


def test_quarantine_mode(authed):
    dep = _dep(authed)
    r = _dispose(authed, dep, "quarantine")
    assert r.status_code == 200
    body = r.json()
    assert body["silver_status"] == "已闭合"
    assert body["chip"] == "隔离"

    after = _dep(authed)
    assert after["status"] == "已隔离放行"
    assert after["dependent_batch"]["status"] == "已闭合"
    assert after["gap"] is True
    assert after["invariant_ok"] is True  # 有隔离凭证，缺口被收口
    assert after["voucher"] == body["voucher"]


def test_modes_exclusive_second_click_fails(authed):
    dep = _dep(authed)
    assert _dispose(authed, dep, "quarantine").status_code == 200
    second = _dispose(authed, dep, "rollback")
    assert second.status_code == 409
    assert second.json()["error"] == "依赖已处置"
    # 只有一张凭证
    vouchers = authed.get("/api/dispositions").json()["items"]
    assert len(vouchers) == 1


def test_stale_version_rejected(authed):
    dep = _dep(authed)
    stale = _dispose(authed, dep, "rollback", version=dep["dependent_batch"]["version"] + 99)
    assert stale.status_code == 409
    assert stale.json()["error"] == "依赖已变化"


def test_orchestration_and_vouchers_consistent(authed):
    dep = _dep(authed)
    body = _dispose(authed, dep, "quarantine").json()

    orch = authed.get("/api/orchestration").json()["items"]
    card = next(c for c in orch if c["dependency_id"] == dep["dependency_id"])
    assert card["chip"] == "隔离"
    assert card["orchestration"]["status"] == "已隔离放行"
    assert card["voucher"] == body["voucher"]
    assert card["history"][0]["voucher"] == body["voucher"]

    vouchers = authed.get("/api/dispositions").json()["items"]
    assert any(v["voucher"] == body["voucher"] and v["mode"] == "quarantine" for v in vouchers)

    detail = authed.get(f"/api/batches/{dep['dependent_batch']['id']}").json()
    upstream_cards = detail["dependencies"]["upstream"]
    assert upstream_cards[0]["voucher"] == body["voucher"]
    assert upstream_cards[0]["chip"] == "隔离"


def test_dependency_card_and_orchestration_agree(authed):
    dep = _dep(authed)
    assert _dispose(authed, dep, "rollback").status_code == 200
    card = _dep(authed)
    orch = next(
        c
        for c in authed.get("/api/orchestration").json()["items"]
        if c["dependency_id"] == dep["dependency_id"]
    )
    assert card["chip"] == orch["chip"] == "回滚"
    assert card["voucher"] == orch["voucher"]


def test_visibility_counts_gap_until_released(authed):
    rep = authed.post("/api/visibility-reports", json={"as_of_date": "2026-11-16"}).json()["report"]
    assert rep["mismatch_count"] >= 1
    assert "依赖缺口" in rep["summary"]

    dep = _dep(authed)
    _dispose(authed, dep, "quarantine")
    rep2 = authed.post("/api/visibility-reports", json={"as_of_date": "2026-11-16"}).json()["report"]
    assert rep2["mismatch_count"] == 0


def test_concurrent_dispose_only_one_wins(client):

    client.post("/api/login", json={"username": "admin", "password": "123456"})
    dep = client.get("/api/dependencies").json()["items"][0]
    dep_id = dep["dependency_id"]
    version = dep["dependent_batch"]["version"]
    results = []

    # 两个编排员同时点：同一依赖、两种模式各一
    def worker(mode):
        r = client.post(
            f"/api/dependencies/{dep_id}/dispose",
            json={"mode": mode, "expected_version": version},
        )
        results.append((mode, r.status_code))

    threads = [
        threading.Thread(target=worker, args=("rollback",)),
        threading.Thread(target=worker, args=("quarantine",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    codes = sorted(code for _, code in results)
    assert codes == [200, 409], codes
    assert len(client.get("/api/dispositions").json()["items"]) == 1
