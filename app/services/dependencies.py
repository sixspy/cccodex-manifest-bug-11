"""跨区依赖门禁、处置（回滚 / 隔离放行）与解析编排回看。

一条依赖 = 下游区（如银区）必须等上游区（如铜区）齐套才能闭合。
处置提交边界只能落一边：

- ``rollback``：整笔回滚下游闭合，恢复收集中，上游维持收集；
- ``quarantine``：隔离上游缺口、写本机隔离凭证，下游保持已闭合放行。

两种模式互斥；同一笔依赖只允许一次成功改态。
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone

from app.db import get_db, get_tx
from app.services.objects import ServiceError

MODE_ROLLBACK = "rollback"
MODE_QUARANTINE = "quarantine"
MODES = (MODE_ROLLBACK, MODE_QUARANTINE)

MODE_LABELS = {
    MODE_ROLLBACK: "整笔回滚",
    MODE_QUARANTINE: "隔离缺口放行",
}
CHIP_LABELS = {
    MODE_ROLLBACK: "回滚",
    MODE_QUARANTINE: "隔离",
}
RUN_STATUS_BY_MODE = {
    MODE_ROLLBACK: "已回滚",
    MODE_QUARANTINE: "已隔离放行",
}
PENDING_STATUS = "待处置"
STAGE = "依赖门禁"


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _latest_batch(conn: sqlite3.Connection, zone_code: str):
    return conn.execute(
        "SELECT * FROM landing_batches WHERE zone_code=? "
        "ORDER BY biz_date DESC, id DESC LIMIT 1",
        (zone_code,),
    ).fetchone()


def _batch_by_id(conn: sqlite3.Connection, batch_id: int):
    return conn.execute(
        "SELECT * FROM landing_batches WHERE id=?", (batch_id,)
    ).fetchone()


def _object_counts(conn: sqlite3.Connection, batch_id: int) -> dict:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS c FROM landing_objects WHERE batch_id=? GROUP BY status",
        (batch_id,),
    ).fetchall()
    counts = {r["status"]: r["c"] for r in rows}
    registered = counts.get("已登记", 0)
    closed = counts.get("已闭合", 0)
    voided = counts.get("已作废", 0)
    return {
        "registered_count": registered,
        "closed_count": closed,
        "voided_count": voided,
        "object_total": registered + closed + voided,
    }


def _batch_view(conn: sqlite3.Connection, batch) -> dict:
    if not batch:
        return {}
    counts = _object_counts(conn, batch["id"])
    closed_count = batch["closed_object_count"]
    # 齐套：已闭合，且固化的对象数 = 实际闭合对象数（>0）
    complete = bool(
        batch["status"] == "已闭合"
        and closed_count is not None
        and closed_count > 0
        and closed_count == counts["closed_count"]
    )
    open_count = counts["registered_count"] if batch["status"] == "收集中" else 0
    return {
        "id": batch["id"],
        "zone_code": batch["zone_code"],
        "zone_name": batch["zone_name"],
        "biz_date": batch["biz_date"],
        "status": batch["status"],
        "version": batch["version"] if "version" in batch.keys() else 1,
        "closed_object_count": closed_count,
        "complete": complete,
        "open_count": open_count,
        **counts,
    }


def _run_row(conn: sqlite3.Connection, dependent_batch_id: int):
    return conn.execute(
        "SELECT * FROM orchestration_runs WHERE dependent_batch_id=?",
        (dependent_batch_id,),
    ).fetchone()


def _disposition_view(row) -> dict | None:
    if not row:
        return None
    mode = row["mode"]
    return {
        "id": row["id"],
        "mode": mode,
        "mode_label": MODE_LABELS.get(mode, mode),
        "chip": CHIP_LABELS.get(mode, mode),
        "voucher": row["voucher"],
        "note": row["note"],
        "actor": row["actor"],
        "created_at": row["created_at"],
        "dependent_version_before": row["dependent_version_before"],
        "silver_status_after": row["silver_status_after"],
        "upstream_status_after": row["upstream_status_after"],
    }


def _dependency_state(conn: sqlite3.Connection, dep, run=None) -> dict:
    dependent = None
    upstream = None
    if run is not None:
        dependent = _batch_by_id(conn, run["dependent_batch_id"])
        upstream = _batch_by_id(conn, run["upstream_batch_id"])
    if dependent is None:
        dependent = _latest_batch(conn, dep["dependent_zone"])
    if upstream is None:
        upstream = _latest_batch(conn, dep["upstream_zone"])

    dep_view = _batch_view(conn, dependent)
    up_view = _batch_view(conn, upstream)

    gap = bool(dep_view) and dep_view["status"] == "已闭合" and not up_view.get("complete", False)

    disp = None
    if run is not None and run["disposition_id"]:
        disp = conn.execute(
            "SELECT * FROM dispositions WHERE id=?", (run["disposition_id"],)
        ).fetchone()
    if disp is None and dep_view:
        disp = conn.execute(
            "SELECT * FROM dispositions WHERE dependent_batch_id=? "
            "ORDER BY id DESC LIMIT 1",
            (dep_view["id"],),
        ).fetchone()

    disposition = _disposition_view(disp)
    run_status = run["status"] if run is not None else PENDING_STATUS
    # 不变量：下游已闭合 + 上游未齐套 + 没有隔离放行凭证 => 拧了
    invariant_ok = not (
        gap and (disposition is None or disposition["mode"] != MODE_QUARANTINE)
    )
    # 芯片只看事实：有处置跟处置；无缺口即已对齐；否则待处置。
    if disposition is not None:
        chip = disposition["chip"]
        chip_kind = disposition["mode"]
        status_label = run_status
    elif not gap:
        chip = "已对齐"
        chip_kind = "aligned"
        status_label = "已对齐"
    else:
        chip = PENDING_STATUS
        chip_kind = "pending"
        status_label = PENDING_STATUS
    return {
        "dependency_id": dep["id"],
        "label": dep["label"],
        "dependent_zone": dep["dependent_zone"],
        "dependent_zone_name": dep_view.get("zone_name", dep["dependent_zone"]),
        "upstream_zone": dep["upstream_zone"],
        "upstream_zone_name": up_view.get("zone_name", dep["upstream_zone"]),
        "dependent_batch": dep_view,
        "upstream_batch": up_view,
        "gap": gap,
        "upstream_complete": bool(up_view.get("complete", False)),
        "invariant_ok": invariant_ok,
        "status": run_status,
        "status_label": status_label,
        "disposition": disposition,
        "chip": chip,
        "chip_kind": chip_kind,
        "voucher": disposition["voucher"] if disposition else None,
        "orchestration": {
            "id": run["id"] if run is not None else None,
            "stage": run["stage"] if run is not None else STAGE,
            "status": status_label,
            "detail": run["detail"] if run is not None else "",
            "updated_at": run["updated_at"] if run is not None else None,
        },
    }


def _load_dependency(conn: sqlite3.Connection, dependency_id: int):
    dep = conn.execute(
        "SELECT * FROM zone_dependencies WHERE id=?", (dependency_id,)
    ).fetchone()
    if not dep:
        raise ServiceError("依赖不存在", 404)
    return dep


def list_dependencies(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        deps = conn.execute("SELECT * FROM zone_dependencies ORDER BY id").fetchall()
        cards = []
        for dep in deps:
            dependent = _latest_batch(conn, dep["dependent_zone"])
            run = _run_row(conn, dependent["id"]) if dependent else None
            cards.append(_dependency_state(conn, dep, run))
        return cards


def dependencies_for_batch(batch_id: int, db_path: str | None = None) -> dict:
    """银区详情 / 铜区进度共用的门禁视角。"""
    with get_db(db_path) as conn:
        batch = _batch_by_id(conn, batch_id)
        if not batch:
            return {"upstream": [], "downstream": []}
        deps = conn.execute("SELECT * FROM zone_dependencies ORDER BY id").fetchall()
        upstream = []
        downstream = []
        for dep in deps:
            dependent = _latest_batch(conn, dep["dependent_zone"])
            run = _run_row(conn, dependent["id"]) if dependent else None
            card = _dependency_state(conn, dep, run)
            if dep["upstream_zone"] == batch["zone_code"]:
                downstream.append(card)
            if dep["dependent_zone"] == batch["zone_code"]:
                upstream.append(card)
        return {"upstream": upstream, "downstream": downstream}


def list_dispositions(db_path: str | None = None) -> list[dict]:
    """本机库存证：所有已落库的处置凭证。"""
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM dispositions ORDER BY id DESC"
        ).fetchall()
        return [_disposition_view(r) for r in rows]


def list_orchestration(db_path: str | None = None) -> list[dict]:
    """解析编排台：任务 + 处置芯片 + 回看历史。"""
    with get_db(db_path) as conn:
        runs = conn.execute(
            "SELECT * FROM orchestration_runs ORDER BY id"
        ).fetchall()
        out = []
        for run in runs:
            bucket = _batch_by_id(conn, run["dependent_batch_id"])
            if not bucket:
                continue
            dep = conn.execute(
                "SELECT * FROM zone_dependencies WHERE dependent_zone=?",
                (bucket["zone_code"],),
            ).fetchone()
            if not dep:
                continue
            card = _dependency_state(conn, dep, run)
            history = conn.execute(
                "SELECT * FROM dispositions WHERE dependent_batch_id=? ORDER BY id DESC",
                (run["dependent_batch_id"],),
            ).fetchall()
            card["history"] = [_disposition_view(h) for h in history]
            out.append(card)
        return out


def _make_voucher(conn: sqlite3.Connection, dependency_id: int, mode: str, version: int) -> str:
    for _ in range(20):
        stamp = datetime.now().strftime("%Y%m%d")
        token = uuid.uuid4().hex[:8].upper()
        voucher = f"VCH-{stamp}-{dependency_id}-{version}-{token}"
        exists = conn.execute(
            "SELECT 1 FROM dispositions WHERE voucher=?", (voucher,)
        ).fetchone()
        if not exists:
            return voucher
    raise ServiceError("凭证生成失败", 500)


def dispose(
    dependency_id: int,
    mode: str,
    expected_version: int | None = None,
    note: str = "",
    actor: str = "admin",
    db_path: str | None = None,
) -> dict:
    """一次事务完成处置改态；失败短句返回，绝不半写。"""
    if mode not in MODES:
        raise ServiceError("处置模式无效", 400)

    try:
        with get_tx(db_path) as conn:
            dep = _load_dependency(conn, dependency_id)

            # 以编排任务锁住这一笔依赖（BEGIN IMMEDIATE 已拿写锁，后来者必然看到新状态）
            run = conn.execute(
                "SELECT r.* FROM orchestration_runs r "
                "JOIN landing_batches b ON b.id=r.dependent_batch_id "
                "JOIN landing_batches u ON u.id=r.upstream_batch_id "
                "WHERE b.zone_code=? AND u.zone_code=? ORDER BY r.id LIMIT 1",
                (dep["dependent_zone"], dep["upstream_zone"]),
            ).fetchone()
            if run is None:
                raise ServiceError("编排任务缺失", 409)

            dependent = _batch_by_id(conn, run["dependent_batch_id"])
            upstream = _batch_by_id(conn, run["upstream_batch_id"])
            if not dependent or not upstream:
                raise ServiceError("批次缺失", 409)

            if run["status"] != PENDING_STATUS:
                raise ServiceError("依赖已处置", 409)

            current_version = dependent["version"] if "version" in dependent.keys() else 1
            if expected_version is not None and int(expected_version) != int(current_version):
                raise ServiceError("依赖已变化", 409)

            dep_view = _batch_view(conn, dependent)
            up_view = _batch_view(conn, upstream)
            if dep_view["status"] != "已闭合":
                raise ServiceError("下游未闭合，无需处置", 409)
            if up_view.get("complete", False):
                raise ServiceError("上游已齐套，无需处置", 409)

            voucher = _make_voucher(conn, dep["id"], mode, int(current_version))
            next_version = int(current_version) + 1
            upstream_open = up_view["open_count"]

            if mode == MODE_ROLLBACK:
                # 整笔回滚：银区恢复收集中，对象退回已登记，闭合记录作废
                conn.execute(
                    "UPDATE landing_batches SET status='收集中', closed_object_count=NULL, "
                    "version=? WHERE id=?",
                    (next_version, dependent["id"]),
                )
                conn.execute(
                    "UPDATE landing_objects SET status='已登记' "
                    "WHERE batch_id=? AND status='已闭合'",
                    (dependent["id"],),
                )
                conn.execute(
                    "UPDATE close_runs SET ok=0, detail=? WHERE batch_id=?",
                    ("整笔回滚：银区恢复收集", dependent["id"]),
                )
                silver_after = "收集中"
                upstream_after = upstream["status"]
                run_status = RUN_STATUS_BY_MODE[MODE_ROLLBACK]
                detail = (
                    f"整笔回滚银区闭合，恢复收集中；铜区缺口 {upstream_open} 个对象仍待收集"
                )
            else:
                # 隔离缺口放行：银区保持已闭合，铜区缺口登记隔离凭证
                conn.execute(
                    "UPDATE landing_batches SET version=? WHERE id=?",
                    (next_version, dependent["id"]),
                )
                silver_after = "已闭合"
                upstream_after = upstream["status"]
                run_status = RUN_STATUS_BY_MODE[MODE_QUARANTINE]
                detail = (
                    f"隔离铜区缺口 {upstream_open} 个对象，写凭证 {voucher} 后放行银区"
                )

            cur = conn.execute(
                "INSERT INTO dispositions("
                "dependency_id, dependent_batch_id, upstream_batch_id, mode, voucher, "
                "note, actor, dependent_version_before, silver_status_after, "
                "upstream_status_after, upstream_open_count, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    dep["id"],
                    dependent["id"],
                    upstream["id"],
                    mode,
                    voucher,
                    (note or "").strip(),
                    actor,
                    int(current_version),
                    silver_after,
                    upstream_after,
                    upstream_open,
                    _now(),
                ),
            )
            disposition_id = cur.lastrowid

            conn.execute(
                "UPDATE orchestration_runs SET status=?, disposition_id=?, detail=?, "
                "updated_at=datetime('now') WHERE id=?",
                (run_status, disposition_id, detail, run["id"]),
            )

            disp = conn.execute(
                "SELECT * FROM dispositions WHERE id=?", (disposition_id,)
            ).fetchone()
            card = _dependency_state(conn, dep, _run_row(conn, dependent["id"]))
            return {
                "ok": True,
                "mode": mode,
                "mode_label": MODE_LABELS[mode],
                "chip": CHIP_LABELS[mode],
                "voucher": voucher,
                "disposition": _disposition_view(disp),
                "dependency": card,
                "silver_status": silver_after,
                "upstream_status": upstream_after,
                "outcome": detail,
            }
    except sqlite3.OperationalError as exc:
        # 写锁排队超时等，给出短句而不是 500 堆栈
        raise ServiceError("处置写入繁忙", 409) from exc


def assert_close_allowed(conn: sqlite3.Connection, batch) -> None:
    """银区闭合与铜区齐套对齐；拧了就拦。"""
    dep = conn.execute(
        "SELECT * FROM zone_dependencies WHERE dependent_zone=?",
        (batch["zone_code"],),
    ).fetchone()
    if not dep:
        return
    upstream = _latest_batch(conn, dep["upstream_zone"])
    if not upstream:
        raise ServiceError("上游批次缺失", 409)
    up_view = _batch_view(conn, upstream)
    if up_view.get("complete", False):
        return
    # 已有隔离放行凭证则允许（银区已闭合路径不会走到这里，留作幂等）
    released = conn.execute(
        "SELECT 1 FROM dispositions d JOIN orchestration_runs r "
        "ON r.disposition_id=d.id "
        "WHERE d.dependent_batch_id=? AND d.mode=? AND r.status=? LIMIT 1",
        (batch["id"], MODE_QUARANTINE, RUN_STATUS_BY_MODE[MODE_QUARANTINE]),
    ).fetchone()
    if released:
        return
    raise ServiceError("上游铜区未齐套，禁止闭合", 409)


def dependency_gap_count(conn: sqlite3.Connection) -> int:
    """已闭合但上游未齐套、且没有隔离放行凭证的批次数。"""
    deps = conn.execute("SELECT * FROM zone_dependencies").fetchall()
    n = 0
    for dep in deps:
        dependent = _latest_batch(conn, dep["dependent_zone"])
        if not dependent or dependent["status"] != "已闭合":
            continue
        upstream = _latest_batch(conn, dep["upstream_zone"])
        if not upstream or _batch_view(conn, upstream).get("complete", False):
            continue
        released = conn.execute(
            "SELECT 1 FROM dispositions WHERE dependent_batch_id=? AND mode=? LIMIT 1",
            (dependent["id"], MODE_QUARANTINE),
        ).fetchone()
        if not released:
            n += 1
    return n
