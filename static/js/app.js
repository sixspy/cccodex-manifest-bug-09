(() => {
  const view = document.getElementById("view");
  const msgEl = document.getElementById("msg");
  let tab = "batches";
  let meta = null;
  let batches = [];
  let selected = null;
  let detail = null;
  let reports = [];
  let scans = [];
  let form = { object_key: "", content_hash: "", byte_size: "100" };

  // 与后端同源的拒因码 → 中文短句（页面只做展示，拦截权威在服务端）
  const REASON_TEXT = {
    BATCH_CLOSED: "批次已闭合，拒绝登记新对象",
    BATCH_VOID: "批次已作废，拒绝登记新对象",
    BATCH_NOT_FOUND: "批次不存在",
    DUPLICATE_KEY: "同批对象键已存在",
    INVALID_INPUT: "对象键与内容指纹不能为空",
  };

  class ApiError extends Error {
    constructor(payload) {
      super(payload.error || payload.detail || "请求失败");
      this.reason = payload.reason || null;
      this.readSource = payload.read_source || null;
    }
  }

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
    if (!r.ok) throw new ApiError(j);
    return j;
  }

  function setMsg(t) {
    msgEl.textContent = t || "";
  }

  async function refresh() {
    meta = await api("/api/meta");
    document.getElementById("lab-brand").textContent = meta.lab_name || "湖区清单台";
    batches = (await api("/api/batches")).items || [];
    reports = (await api("/api/visibility-reports")).items || [];
    scans = (await api("/api/dedup-scans")).items || [];
  }

  async function openBatch(id) {
    selected = id;
    detail = await api("/api/batches/" + id);
    tab = "detail";
    syncNav();
    render();
  }

  function gateOf() {
    return (detail && detail.register_gate) || null;
  }

  // 登记详情顶部拒因条：亮拒因 + 读态来源
  function denyBanner(gate) {
    if (!gate || gate.register_allowed) return "";
    const reason = gate.reason || "";
    const text = gate.message || REASON_TEXT[reason] || "当前批次禁止登记";
    return `
      <div class="deny-banner" role="alert">
        <span class="deny-code">拒登 · ${reason}</span>
        <span> ${text}</span>
        <span class="deny-src">读态来源：${gate.read_source || "—"}（提交瞬间服务端按此读态一次拦截）</span>
      </div>`;
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
      // 失败吐短句，并刷新详情让拒因条与对象列表同因展示
      await openBatch(selected).catch(() => {});
      const src = e.readSource ? `（读态来源：${e.readSource}）` : "";
      setMsg(`${e.message}${e.reason ? ` [${e.reason}]` : ""}${src}`);
      render();
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

  async function doScan() {
    setMsg("");
    try {
      const r = await api("/api/dedup-scans", {
        method: "POST",
        body: JSON.stringify({ as_of_date: "2026-11-16" }),
      });
      await refresh();
      tab = "scans";
      syncNav();
      const d = r.scan;
      setMsg(
        `扫库完成：可入库 ${d.eligible_count} 行，拒登脏行 ${d.rejected_row_count} 行，` +
          `同指纹去重组 ${d.duplicate_group_count} 个`
      );
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
      <p class="muted">主链：选批 → 登记对象 → 闭合清单 → 可见性对账</p>
      <table>
        <thead><tr><th>编号</th><th>湖区</th><th>业务日</th><th>状态</th><th>可登记门槛</th><th></th></tr></thead>
        <tbody>
          ${batches
            .map(
              (b) => {
                const closed = b.status === "已闭合";
                const voided = b.status === "已作废";
                const gateCell = b.status === "收集中"
                  ? `<span class="pill">可登记</span>`
                  : `<span class="pill-danger">拒登 · ${closed ? "BATCH_CLOSED" : "BATCH_VOID"}</span>`;
                return `<tr>
            <td>${b.id}</td>
            <td>${b.zone_name}<div class="muted">${b.zone_code}</div></td>
            <td>${b.biz_date}</td>
            <td><span class="${closed || voided ? "pill-danger" : "pill"}">${b.status}</span></td>
            <td>${gateCell}</td>
            <td><button type="button" class="secondary" data-open="${b.id}">打开</button></td>
          </tr>`;
              }
            )
            .join("")}
        </tbody>
      </table>
      <p class="muted">门槛与批次态同源；已闭合批写入由服务端在提交瞬间一次拦截。</p>`;
  }

  function renderDetail() {
    if (!detail) {
      return `<h2>批次详情</h2><p class="muted">请先从落地批次打开一条。</p>`;
    }
    const b = detail.batch;
    const objs = detail.objects || [];
    const gate = gateOf();
    const denied = gate && !gate.register_allowed;
    return `
      <h2>批次详情 #${b.id}</h2>
      <p>${b.zone_name} · ${b.biz_date} · <span class="${denied ? "pill-danger" : "pill"}">${b.status}</span></p>
      ${denyBanner(gate)}
      <div class="row-actions">
        <button type="button" id="btn-close" ${denied ? "disabled" : ""}>闭合清单</button>
        <button type="button" class="secondary" id="btn-report-from-detail">跑可见性对账</button>
      </div>
      <div class="panel">
        <h3>登记对象</h3>
        <label>对象键<input id="f-key" value="${form.object_key}" ${denied ? "disabled" : ""} /></label>
        <label>内容指纹<input id="f-hash" value="${form.content_hash}" ${denied ? "disabled" : ""} /></label>
        <label>字节数<input id="f-size" value="${form.byte_size}" ${denied ? "disabled" : ""} /></label>
        <button type="button" id="btn-register" ${denied ? "disabled" : ""}>写入登记</button>
        ${
          denied
            ? `<p class="muted">按钮仅按页面批次态禁用；真正门槛以服务端提交瞬间读态为准。</p>`
            : ""
        }
      </div>
      <h3>对象列表</h3>
      <table>
        <thead><tr><th>键</th><th>指纹</th><th>字节</th><th>状态</th><th>入库口径</th></tr></thead>
        <tbody>
          ${objs
            .map(
              (o) => {
                // 闭合/作废批里若冒出“已登记”行，即脏行：标同一拒因，清洗口径不收
                const dirty =
                  o.status === "已登记" && (b.status === "已闭合" || b.status === "已作废");
                const reason = b.status === "已闭合" ? "BATCH_CLOSED" : "BATCH_VOID";
                return `<tr>
            <td>${o.object_key}</td><td>${o.content_hash}</td><td>${o.byte_size}</td>
            <td><span class="${dirty ? "pill-danger" : "pill"}">${o.status}</span></td>
            <td>${dirty ? `<span class="pill-danger">拒登 · ${reason}</span>` : `<span class="pill">可入库</span>`}</td>
          </tr>`;
              }
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function renderScans() {
    const latest = scans[0] ? JSON.parse(scans[0].detail || "{}") : null;
    const rejected = (latest && latest.rejected) || [];
    const groups = (latest && latest.duplicate_groups) || [];
    return `
      <h2>去重扫库台</h2>
      <button type="button" id="btn-scan">跑一遍去重扫库</button>
      <p class="muted">清洗入库口径只认“收集中”批内“已登记”行；闭合/作废批的脏行按登记同因拒收，复跑不会再吃到新已登记行。</p>
      ${
        scans[0]
          ? `<div class="scan-stats">
              <span class="stat"><b>${scans[0].eligible_count}</b>可入库行</span>
              <span class="stat"><b>${scans[0].duplicate_group_count}</b>同指纹去重组</span>
              <span class="stat"><b>${scans[0].rejected_row_count}</b>拒登脏行</span>
              <span class="stat"><b>${scans[0].as_of_date}</b>截至日</span>
            </div>
            <p class="muted">读态来源：${latest.read_source || "—"} · 扫库记录 #${scans[0].id}</p>`
          : `<p class="muted">尚无扫库记录。</p>`
      }
      <h3>拒登脏行（与登记详情同一拒因）</h3>
      <table>
        <thead><tr><th>对象</th><th>批次</th><th>拒因</th><th>短句</th></tr></thead>
        <tbody>
          ${
            rejected.length
              ? rejected
                  .map(
                    (r) => `<tr>
              <td>${r.object_key}<div class="muted">${r.content_hash}</div></td>
              <td>#${r.batch_id} ${r.zone_name}</td>
              <td><span class="pill-danger">${r.reason}</span></td>
              <td>${r.message}<div class="muted">${r.read_source}</div></td>
            </tr>`
                  )
                  .join("")
              : `<tr><td colspan="4" class="muted">无脏行：闭合批未再写入新已登记对象。</td></tr>`
          }
        </tbody>
      </table>
      <h3>同指纹去重组（仅统计可入库行）</h3>
      <table>
        <thead><tr><th>指纹</th><th>命中数</th><th>成员</th></tr></thead>
        <tbody>
          ${
            groups.length
              ? groups
                  .map(
                    (g) => `<tr>
              <td>${g.content_hash}</td><td>${g.count}</td>
              <td>${g.members.map((m) => `#${m.batch_id}/${m.object_key}`).join("，")}</td>
            </tr>`
                  )
                  .join("")
              : `<tr><td colspan="3" class="muted">无重复。</td></tr>`
          }
        </tbody>
      </table>
      <h3>历史扫库</h3>
      <table>
        <thead><tr><th>编号</th><th>截至</th><th>可入库</th><th>去重组</th><th>拒登</th></tr></thead>
        <tbody>
          ${scans
            .map(
              (s) => `<tr>
            <td>${s.id}</td><td>${s.as_of_date}</td><td>${s.eligible_count}</td>
            <td>${s.duplicate_group_count}</td><td>${s.rejected_row_count}</td>
          </tr>`
            )
            .join("")}
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
            <td>${r.id}</td><td>${r.as_of_date}</td><td>${r.mismatch_count}</td><td>${r.summary}</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function render() {
    if (tab === "batches") view.innerHTML = renderBatches();
    else if (tab === "detail") view.innerHTML = renderDetail();
    else if (tab === "scans") view.innerHTML = renderScans();
    else view.innerHTML = renderReports();

    view.querySelectorAll("[data-open]").forEach((btn) => {
      btn.onclick = () => openBatch(Number(btn.dataset.open));
    });
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
    const scanBtn = document.getElementById("btn-scan");
    if (scanBtn) scanBtn.onclick = doScan;
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
