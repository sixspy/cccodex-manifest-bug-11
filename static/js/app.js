(() => {
  const view = document.getElementById("view");
  const msgEl = document.getElementById("msg");
  let tab = "batches";
  let meta = null;
  let batches = [];
  let selected = null;
  let detail = null;
  let reports = [];
  let deps = [];
  let orch = [];
  let vouchers = [];
  let form = { object_key: "", content_hash: "", byte_size: "100" };

  async function api(path, opts = {}) {
    const r = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
      ...opts,
    });
    if (r.status === 401) {
      location.href = "/login";
      throw new Error("未登录");
    }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || j.detail || ("HTTP " + r.status));
    return j;
  }

  function setMsg(t) {
    msgEl.textContent = t || "";
  }

  function esc(v) {
    return String(v == null ? "" : v)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function chipOf(card) {
    const status = card.status || card.status_label || "待处置";
    let cls = card.chip_kind || "pending";
    if (!card.chip_kind) {
      if (status === "已回滚") cls = "rollback";
      else if (status === "已隔离放行") cls = "quarantine";
      else if (status === "已对齐") cls = "aligned";
    }
    return `<span class="chip ${esc(cls)}">${esc(card.chip || status)}</span>`;
  }

  function voucherLine(card) {
    if (!card.voucher) return "";
    return `<div class="voucher">凭证 · <code>${esc(card.voucher)}</code></div>`;
  }

  function progress(up) {
    if (!up) return "";
    const open = up.status === "收集中";
    // 收集中没有目标对象数，只展示已登记；已闭合才用固化数做占比
    const total = up.status === "已闭合" ? (up.closed_object_count || 0) : null;
    const done = up.status === "已闭合" ? (up.closed_count || 0) : (up.registered_count || 0);
    const pct = total ? Math.round((done / total) * 100) : 0;
    const label = open
      ? `收集中 · 已登记 ${done} 个 · 齐套：否`
      : `${done}/${total} 对象 · 齐套：${up.complete ? "是" : "否"}`;
    return `<div class="progress ${open ? "open" : ""}">
      <div class="bar" style="width:${pct}%"></div>
      <span>${esc(label)}</span>
    </div>`;
  }

  async function refresh() {
    meta = await api("/api/meta");
    document.getElementById("lab-brand").textContent = meta.lab_name || "湖区清单台";
    batches = (await api("/api/batches")).items || [];
    reports = (await api("/api/visibility-reports")).items || [];
    deps = (await api("/api/dependencies")).items || [];
    orch = (await api("/api/orchestration")).items || [];
    vouchers = (await api("/api/dispositions")).items || [];
  }

  async function openBatch(id) {
    selected = id;
    detail = await api("/api/batches/" + id);
    tab = "detail";
    syncNav();
    render();
  }

  async function doRegister() {
    if (!selected) return;
    setMsg("");
    try {
      await api("/api/batches/" + selected + "/objects", {
        method: "POST",
        body: JSON.stringify({
          object_key: form.object_key,
          content_hash: form.content_hash,
          byte_size: Number(form.byte_size || 0),
        }),
      });
      form = { object_key: "", content_hash: "", byte_size: "100" };
      await openBatch(selected);
      await refresh();
      setMsg("已登记对象");
      render();
    } catch (e) {
      setMsg(e.message);
    }
  }

  async function doClose() {
    if (!selected) return;
    setMsg("");
    try {
      await api("/api/batches/" + selected + "/close", { method: "POST", body: "{}" });
      await openBatch(selected);
      await refresh();
      setMsg("清单已闭合");
      render();
    } catch (e) {
      setMsg(e.message);
    }
  }

  async function doDispose(card, mode) {
    const hint =
      mode === "rollback"
        ? `整笔回滚 ${card.dependent_zone_name} 的闭合，恢复收集中？`
        : `隔离 ${card.upstream_zone_name} 缺口并写隔离凭证、放行 ${card.dependent_zone_name}？`;
    if (!window.confirm(hint)) return;
    setMsg("");
    try {
      const r = await api(`/api/dependencies/${card.dependency_id}/dispose`, {
        method: "POST",
        body: JSON.stringify({
          mode,
          expected_version: card.dependent_batch ? card.dependent_batch.version : null,
        }),
      });
      await refresh();
      if (selected) {
        try {
          detail = await api("/api/batches/" + selected);
        } catch (e) {
          /* 保留旧详情，刷新失败不阻断结局展示 */
        }
      }
      setMsg(`处置结局：${r.mode_label} · ${r.silver_status} · 凭证 ${r.voucher}`);
      render();
    } catch (e) {
      setMsg(e.message);
      await refresh().catch(() => {});
      render();
    }
  }

  async function doReport() {
    setMsg("");
    try {
      const r = await api("/api/visibility-reports", {
        method: "POST",
        body: JSON.stringify({ as_of_date: "2026-11-16" }),
      });
      await refresh();
      tab = "reports";
      syncNav();
      setMsg(r.report.summary);
      render();
    } catch (e) {
      setMsg(e.message);
    }
  }

  function syncNav() {
    document.querySelectorAll("nav [data-tab]").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.tab === tab);
    });
  }

  function renderBatches() {
    return `
      <h2>落地批次</h2>
      <p class="muted">主链：选批 → 登记对象 → 闭合清单 → 可见性对账 · 跨区依赖须先处置再闭合</p>
      <table>
        <thead><tr><th>编号</th><th>湖区</th><th>业务日</th><th>状态</th><th></th></tr></thead>
        <tbody>
          ${batches
            .map(
              (b) => `<tr>
            <td>${b.id}</td>
            <td>${esc(b.zone_name)}<div class="muted">${esc(b.zone_code)}</div></td>
            <td>${esc(b.biz_date)}</td>
            <td><span class="pill">${esc(b.status)}</span></td>
            <td><button type="button" class="secondary" data-open="${b.id}">打开</button></td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function renderDependencyCard(card) {
    const dep = card.dependent_batch || {};
    const up = card.upstream_batch || {};
    const actionable = card.status === "待处置" && card.gap;
    return `<article class="dep-card ${card.status === "待处置" ? "attention" : ""}">
      <header>
        <h3>${esc(card.dependent_zone_name)} ← ${esc(card.upstream_zone_name)}</h3>
        ${chipOf(card)}
      </header>
      <p class="muted">${esc(card.label)}</p>
      <dl class="dep-grid">
        <div><dt>银区闭环</dt><dd>${esc(dep.zone_name)} · <span class="pill">${esc(dep.status)}</span> · 固化 ${dep.closed_object_count == null ? "—" : dep.closed_object_count} 个</dd></div>
        <div><dt>铜区进度</dt><dd>${progress(up)}</dd></div>
        <div><dt>依赖对齐</dt><dd>${card.invariant_ok ? "已对齐" : "未对齐（银闭铜未齐）"}</dd></div>
      </dl>
      ${voucherLine(card)}
      ${
        actionable
          ? `<div class="row-actions">
              <button type="button" class="danger" data-dispose="${card.dependency_id}" data-mode="rollback">整笔回滚银区</button>
              <button type="button" data-dispose="${card.dependency_id}" data-mode="quarantine">隔离缺口放行</button>
            </div>`
          : `<p class="muted">处置已落库，芯片与凭证见解析编排台。</p>`
      }
    </article>`;
  }

  function renderDependencies() {
    return `
      <h2>依赖卡片</h2>
      <p class="muted">银区闭合与铜区齐套须对齐；拧了就拦。处置提交边界只落一边。</p>
      ${deps.length ? deps.map(renderDependencyCard).join("") : `<p class="muted">暂无跨区依赖。</p>`}`;
  }

  function renderDetail() {
    if (!detail) {
      return `<h2>批次详情</h2><p class="muted">请先从落地批次打开一条。</p>`;
    }
    const b = detail.batch;
    const objs = detail.objects || [];
    const links = detail.dependencies || { upstream: [], downstream: [] };
    const upstreamCards = links.upstream || [];
    const downstreamCards = links.downstream || [];
    return `
      <h2>批次详情 #${b.id}</h2>
      <p>${esc(b.zone_name)} · ${esc(b.biz_date)} · <span class="pill">${esc(b.status)}</span></p>
      <div class="row-actions">
        <button type="button" id="btn-close">闭合清单</button>
        <button type="button" class="secondary" id="btn-report-from-detail">跑可见性对账</button>
      </div>
      ${upstreamCards.length ? `<section class="zone-gate"><h3>银区详情 · 依赖门禁</h3>${upstreamCards.map(renderDependencyCard).join("")}</section>` : ""}
      ${downstreamCards.length ? `<section class="zone-gate"><h3>铜区进度 · 下游门禁</h3>${downstreamCards.map(renderDependencyCard).join("")}</section>` : ""}
      <div class="panel">
        <h3>登记对象</h3>
        <label>对象键<input id="f-key" value="${esc(form.object_key)}" /></label>
        <label>内容指纹<input id="f-hash" value="${esc(form.content_hash)}" /></label>
        <label>字节数<input id="f-size" value="${esc(form.byte_size)}" /></label>
        <button type="button" id="btn-register">写入登记</button>
      </div>
      <h3>对象列表</h3>
      <table>
        <thead><tr><th>键</th><th>指纹</th><th>字节</th><th>状态</th></tr></thead>
        <tbody>
          ${objs
            .map(
              (o) => `<tr>
            <td>${esc(o.object_key)}</td><td>${esc(o.content_hash)}</td><td>${esc(o.byte_size)}</td><td>${esc(o.status)}</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function renderOrchestration() {
    return `
      <h2>解析编排台</h2>
      <p class="muted">依赖门禁任务 · 处置芯片 · 回看与库存证须与处置一致。</p>
      <table>
        <thead><tr><th>任务</th><th>阶段</th><th>状态</th><th>结局 / 凭证</th></tr></thead>
        <tbody>
          ${orch
            .map(
              (card) => `<tr>
            <td>#${esc(card.orchestration.id)} ${esc(card.dependent_zone_name)} ← ${esc(card.upstream_zone_name)}</td>
            <td>${esc(card.orchestration.stage)}</td>
            <td>${chipOf(card)}</td>
            <td>${esc(card.orchestration.detail || "")}<div class="voucher">${card.voucher ? "凭证 · <code>" + esc(card.voucher) + "</code>" : "—"}</div></td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>
      <h3>处置回看</h3>
      ${
        orch.some((c) => (c.history || []).length)
          ? orch
              .filter((c) => (c.history || []).length)
              .map(
                (c) => `<div class="review">
                  <p>${esc(c.dependent_zone_name)} ← ${esc(c.upstream_zone_name)}</p>
                  <ul>${c.history
                    .map(
                      (h) => `<li>${chipOf({ chip: h.chip, status: h.mode === "rollback" ? "已回滚" : "已隔离放行" })}
                        <code>${esc(h.voucher)}</code> · ${esc(h.mode_label)} · 银区→${esc(h.silver_status_after)} · ${esc(h.created_at)}</li>`
                    )
                    .join("")}</ul>
                </div>`
              )
              .join("")
          : `<p class="muted">尚无处置记录。</p>`
      }
      <h3>本机库存证</h3>
      <table>
        <thead><tr><th>凭证</th><th>模式</th><th>下游</th><th>处置前版本</th><th>时间</th></tr></thead>
        <tbody>
          ${(vouchers.length ? vouchers : [])
            .map(
              (v) => `<tr>
            <td><code>${esc(v.voucher)}</code></td>
            <td>${esc(v.mode_label)}</td>
            <td>银区→${esc(v.silver_status_after)}</td>
            <td>${esc(v.dependent_version_before)}</td>
            <td>${esc(v.created_at)}</td>
          </tr>`
            )
            .join("") || `<tr><td colspan="5" class="muted">暂无凭证。</td></tr>`}
        </tbody>
      </table>`;
  }

  function renderReports() {
    return `
      <h2>可见性对账</h2>
      <button type="button" id="btn-report">生成对账</button>
      <table>
        <thead><tr><th>编号</th><th>截至</th><th>错位</th><th>摘要</th></tr></thead>
        <tbody>
          ${reports
            .map(
              (r) => `<tr>
            <td>${r.id}</td><td>${esc(r.as_of_date)}</td><td>${r.mismatch_count}</td><td>${esc(r.summary)}</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function bindDisposals(root) {
    root.querySelectorAll("[data-dispose]").forEach((btn) => {
      const id = Number(btn.dataset.dispose);
      const card = deps.find((c) => c.dependency_id === id)
        || (orch || []).find((c) => c.dependency_id === id);
      if (!card) return;
      btn.onclick = () => doDispose(card, btn.dataset.mode);
    });
  }

  function render() {
    if (tab === "batches") view.innerHTML = renderBatches();
    else if (tab === "detail") view.innerHTML = renderDetail();
    else if (tab === "dependencies") view.innerHTML = renderDependencies();
    else if (tab === "orchestration") view.innerHTML = renderOrchestration();
    else view.innerHTML = renderReports();

    view.querySelectorAll("[data-open]").forEach((btn) => {
      btn.onclick = () => openBatch(Number(btn.dataset.open));
    });
    bindDisposals(view);
    const reg = document.getElementById("btn-register");
    if (reg) {
      const key = document.getElementById("f-key");
      const hash = document.getElementById("f-hash");
      const size = document.getElementById("f-size");
      key.oninput = () => (form.object_key = key.value);
      hash.oninput = () => (form.content_hash = hash.value);
      size.oninput = () => (form.byte_size = size.value);
      reg.onclick = doRegister;
    }
    const closeBtn = document.getElementById("btn-close");
    if (closeBtn) closeBtn.onclick = doClose;
    const r1 = document.getElementById("btn-report");
    const r2 = document.getElementById("btn-report-from-detail");
    if (r1) r1.onclick = doReport;
    if (r2) r2.onclick = doReport;
  }

  document.querySelectorAll("nav [data-tab]").forEach((btn) => {
    btn.onclick = () => {
      tab = btn.dataset.tab;
      syncNav();
      render();
    };
  });
  document.getElementById("btn-logout").onclick = () => {
    fetch("/api/logout", { method: "POST" }).then(() => (location.href = "/login"));
  };

  refresh()
    .then(render)
    .catch((e) => setMsg(e.message));
})();
