const $ = (id) => document.getElementById(id);

function pluginBase() {
  const path = window.location.pathname.replace(/\/+$/, "");
  if (path.endsWith("/console") || path.endsWith("/console/index.html")) {
    return path.replace(/\/console(?:\/index\.html)?$/, "");
  }
  return path;
}

async function apiGet(route, query = {}) {
  if (window.AstrBotPluginPage?.apiGet) return window.AstrBotPluginPage.apiGet(route, query);
  const params = new URLSearchParams(query).toString();
  const res = await fetch(`${pluginBase()}/${route}${params ? `?${params}` : ""}`);
  return res.json();
}

async function apiPost(route, body = {}) {
  if (window.AstrBotPluginPage?.apiPost) return window.AstrBotPluginPage.apiPost(route, body);
  const res = await fetch(`${pluginBase()}/${route}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return res.json();
}

function toast(msg) {
  const el = $("toast");
  if (!el) return;
  el.hidden = false;
  el.textContent = msg;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, 1800);
}

function showDiag(data) {
  const box = $("diag");
  if (box) box.textContent = typeof data === "string" ? data : JSON.stringify(data, null, 2);
}

async function run(label, fn) {
  try {
    const r = await fn();
    if (r && r.ok === false) toast(`${label}失败`);
    else toast(label);
    if (r !== undefined) showDiag(r);
    return r;
  } catch (err) {
    toast(`${label}失败`);
    showDiag(`${label} 失败: ${err}`);
    return null;
  }
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function speakerLabel(name, id) {
  const n = (name || "").trim();
  const q = (id || "").trim();
  if (n && q && n !== q) return `${n} (${q})`;
  return n || q || "未知";
}

function factItem(f, extra = "", withCheck = false) {
  const who = speakerLabel(f.speaker_name, f.speaker_id);
  const check = withCheck
    ? `<input type="checkbox" class="pick" data-id="${esc(f.id)}" />`
    : "";
  return `<div class="item">
    <div class="item-head">
      ${check}
      <span class="chip">${esc(f.status)}</span>
      <span class="chip">${esc(who)}</span>
      <b>#${esc(f.id)}</b> ${esc(f.attribute)}
    </div>
    <div>${esc(f.content)}</div>
    <div class="lede">QQ/id ${esc(f.speaker_id || "")}</div>
    ${extra}
    <div class="row">
      <button type="button" class="ghost" data-act="del" data-id="${esc(f.id)}">删除此条</button>
    </div>
  </div>`;
}

function downloadText(filename, text) {
  const blob = new Blob([text], { type: "application/json;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename || "savagetype.jsonl";
  a.click();
  URL.revokeObjectURL(a.href);
}

async function loadOverview() {
  const ov = await apiGet("overview");
  const c = ov.counts || {};
  $("kpis").innerHTML = [
    ["时间线", c.timeline],
    ["live", c.facts_live],
    ["覆盖待确认", c.pending],
    ["学习待审", c.reviews_pending],
  ].map(([k, v]) => `<div class="kpi card"><b>${esc(v ?? 0)}</b><span>${esc(k)}</span></div>`).join("");
  const reasons = (ov.coexistence && ov.coexistence.reasons) || [];
  const emb = ov.embedding || {};
  const embLine = emb.active
    ? `Embedding 实际启用（${emb.reason}）`
    : `Embedding 关闭`;
  $("coexist").textContent = (reasons.length ? `共存降级：${reasons.join("；")}。` : "未检测到重叠记忆插件。") + " " + embLine;
  const box = $("alias-suggestions");
  const items = ov.alias_suggestions || [];
  box.innerHTML = items.length
    ? items.map((s) => `<div class="item">同名 ${esc(s.name)}：<code>${esc(s.alias)}</code> → <code>${esc(s.canonical_id)}</code>
        <button type="button" data-act="alias" data-alias="${esc(s.alias)}" data-canonical="${esc(s.canonical_id)}">映射</button></div>`).join("")
    : `<p class="lede">没有同名不同 id 的建议。</p>`;
  showDiag({ overview: ov });
  const sel = $("remember-speaker");
  if (sel) {
    const speakers = ov.speakers || [{ id: "admin", name: "admin" }];
    sel.innerHTML = speakers.map((s) =>
      `<option value="${esc(s.id)}">${esc(s.name)} (${esc(s.id)})</option>`
    ).join("");
  }
  const pathBox = $("archive-path");
  if (pathBox && !pathBox.value && ov.data_dir) {
    pathBox.placeholder = ov.data_dir;
    pathBox.value = ov.data_dir || "";
  }
}

async function loadFacts() {
  const live = await apiGet("facts", { status: "live" });
  if (!$("hits").dataset.locked) {
    $("hits").innerHTML = (live.items || []).slice(0, 80).map((f) => factItem(f, "", true)).join("")
      || `<p class="lede">还没有 live 事实。</p>`;
  }
}

async function loadPending() {
  const data = await apiGet("pending");
  const items = data.items || [];
  $("pending").innerHTML = items.length
    ? items.map((p) => `<div class="item">
        <div>#${esc(p.id)} ← old ${esc(p.old_fact_id)} · ${esc(p.reason)}</div>
        <div class="lede">${esc(JSON.stringify(p.new_payload))}</div>
        <div class="row">
          <button type="button" data-act="pending-confirm" data-id="${esc(p.id)}">确认覆盖</button>
          <button type="button" class="ghost" data-act="pending-reject" data-id="${esc(p.id)}">驳回</button>
        </div>
      </div>`).join("")
    : `<p class="lede">没有待确认覆盖。</p>`;
}

async function loadReviews() {
  const data = await apiGet("reviews", { status: "pending" });
  const items = data.items || [];
  $("reviews").innerHTML = items.length
    ? items.map((r) => `<div class="item">
        <span class="chip">${esc(r.kind)}</span>
        <span class="chip">${esc(speakerLabel("", r.speaker_id))}</span>
        <b>#${esc(r.id)}</b> ${esc(r.title)}
        <div class="lede">QQ/id ${esc(r.speaker_id || "")}</div>
        <div class="row">
          <button type="button" data-act="approve" data-id="${esc(r.id)}">批准</button>
          <button type="button" class="ghost" data-act="reject-review" data-id="${esc(r.id)}">驳回</button>
        </div>
      </div>`).join("")
    : `<p class="lede">没有待审学习项。</p>`;
}

async function loadMicroscope() {
  const data = await apiGet("microscope", { n: 8 });
  const items = data.items || [];
  $("microscope").innerHTML = items.length
    ? items.map((it) => {
        const p = it.payload || {};
        return `<div class="item">
          <span class="chip">${esc(p.route)}</span>
          <span class="chip">${esc(speakerLabel("", p.speaker_id))}</span>
          <div>${esc(p.query || "")}</div>
          <div class="lede">QQ/id ${esc(p.speaker_id || "")} · chars=${esc(p.pack_chars)}</div>
        </div>`;
      }).join("")
    : `<p class="lede">还没有注入记录。</p>`;
}

function fieldControl(key, spec, value) {
  const hint = spec.hint ? `<div class="lede">${esc(spec.hint)}</div>` : "";
  const label = `<label for="cfg-${esc(key)}">${esc(spec.description || key)}</label>`;
  if (spec.type === "bool") {
    return `<div class="setting">${label}
      <label class="toggle"><input id="cfg-${esc(key)}" name="${esc(key)}" type="checkbox" ${value ? "checked" : ""} /> 开启</label>
      ${hint}</div>`;
  }
  if (Array.isArray(spec.options) && spec.options.length) {
    const opts = spec.options.map((o) =>
      `<option value="${esc(o)}" ${String(o) === String(value) ? "selected" : ""}>${esc(o)}</option>`
    ).join("");
    return `<div class="setting">${label}<select id="cfg-${esc(key)}" name="${esc(key)}">${opts}</select>${hint}</div>`;
  }
  const typ = spec.type === "int" || spec.type === "float" ? "number" : "text";
  const step = spec.type === "float" ? "0.01" : spec.type === "int" ? "1" : "";
  return `<div class="setting">${label}
    <input id="cfg-${esc(key)}" name="${esc(key)}" type="${typ}" ${step ? `step="${step}"` : ""} value="${esc(value ?? "")}" />
    ${hint}</div>`;
}

async function loadSettings() {
  const data = await apiGet("config");
  const schema = data.schema || {};
  const values = data.values || {};
  $("settings-form").innerHTML = Object.entries(schema).map(([key, spec]) =>
    fieldControl(key, spec, values[key])
  ).join("");
}

function readSettings() {
  const values = {};
  $("settings-form").querySelectorAll("[name]").forEach((el) => {
    if (el.type === "checkbox") values[el.name] = el.checked;
    else if (el.type === "number") values[el.name] = el.value === "" ? 0 : Number(el.value);
    else values[el.name] = el.value;
  });
  return values;
}

async function reload() {
  await loadOverview();
  await loadFacts();
  await loadPending();
  await loadReviews();
  await loadMicroscope();
}

function showTab(name) {
  $("page-ops").hidden = name !== "ops";
  $("page-settings").hidden = name !== "settings";
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === name));
  if (name === "settings") run("加载设置", loadSettings);
}

async function onAct(act, el) {
  if (act === "tab") return showTab(el.dataset.tab);
  if (act === "refresh") return run("已刷新", reload);
  if (act === "extract") return run("已抽取", async () => { const r = await apiPost("extract", {}); await reload(); return r; });
  if (act === "sleep") return run("已维护", async () => { const r = await apiPost("sleep", {}); await reload(); return r; });
  if (act === "learn") return run("已学习", async () => { const r = await apiPost("learn", {}); await reload(); return r; });
  if (act === "export") return run("已导出", async () => {
    const r = await apiGet("export");
    if (r && r.content) downloadText(r.filename || "savagetype.jsonl", r.content);
    return { ok: true, filename: r && r.filename, chars: r && r.chars };
  });
  if (act === "search") return run("已检索", async () => {
    $("hits").dataset.locked = "1";
    const data = await apiGet("search", { q: $("q").value, speaker_id: $("speaker").value, k: 30 });
    $("hits").innerHTML = (data.items || []).map((f) => factItem(f, "", true)).join("") || `<p class="lede">没有命中。</p>`;
    return data;
  });
  if (act === "batch-archive") return run("已删除所选", async () => {
    const ids = [...$("hits").querySelectorAll(".pick:checked")].map((n) => Number(n.dataset.id));
    if (!ids.length) return { ok: false, error: "未选择" };
    if (!confirm(`归档 ${ids.length} 条？`)) return { ok: false, error: "cancelled" };
    const r = await apiPost("facts/archive", { ids });
    $("hits").dataset.locked = "";
    await reload();
    return r;
  });
  if (act === "del") return run("已删除", async () => {
    if (!confirm(`归档 #${el.dataset.id}？`)) return { ok: false, error: "cancelled" };
    const r = await apiPost("facts/archive", { ids: [Number(el.dataset.id)] });
    $("hits").dataset.locked = "";
    await reload();
    return r;
  });
  if (act === "remember") return run("已记住", async () => {
    const content = $("remember-text").value.trim();
    if (!content) return { ok: false, error: "empty" };
    const custom = ($("remember-speaker-custom").value || "").trim();
    const speaker_id = custom || $("remember-speaker").value || "admin";
    const r = await apiPost("remember", { content, speaker_id });
    $("remember-text").value = "";
    $("hits").dataset.locked = "";
    await reload();
    return r;
  });
  if (act === "alias") return run("已映射", async () => {
    await apiPost("aliases/set", { alias: el.dataset.alias, canonical_id: el.dataset.canonical });
    await reload();
  });
  if (act === "pending-confirm") return run("已确认覆盖", async () => { await apiPost("pending/confirm", { id: Number(el.dataset.id) }); await reload(); });
  if (act === "pending-reject") return run("已驳回", async () => { await apiPost("pending/reject", { id: Number(el.dataset.id) }); await reload(); });
  if (act === "approve") return run("已批准", async () => { await apiPost("reviews/set", { id: Number(el.dataset.id), status: "approved" }); await reload(); });
  if (act === "reject-review") return run("已驳回", async () => { await apiPost("reviews/set", { id: Number(el.dataset.id), status: "rejected" }); await reload(); });
  if (act === "archive-preview") return run("已预览", () => apiPost("archive/preview", { path: $("archive-path").value.trim() }));
  if (act === "archive-import") return run("已导入档案", async () => { const r = await apiPost("archive/import", { path: $("archive-path").value.trim() }); await reload(); return r; });
  if (act === "chat-preview") return run("已预览聊天", () => apiPost("chat/preview", { text: $("chat-text").value, user_names: $("chat-users").value, bot_names: $("chat-bots").value }));
  if (act === "chat-import") return run("已导入聊天", async () => { const r = await apiPost("chat/import", { text: $("chat-text").value, user_names: $("chat-users").value, bot_names: $("chat-bots").value }); await reload(); return r; });
  if (act === "settings-save") return run("已保存设置", async () => { const r = await apiPost("config/save", { values: readSettings() }); await loadSettings(); await reload(); return r; });
}

document.addEventListener("click", (ev) => {
  const el = ev.target.closest("[data-act]");
  if (!el) return;
  ev.preventDefault();
  onAct(el.dataset.act, el);
});

async function boot() {
  if (window.AstrBotPluginPage?.ready) {
    try { await window.AstrBotPluginPage.ready(); } catch (err) { showDiag(err); }
  }
  await run("已刷新", reload);
}

boot();
