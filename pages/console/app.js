const $ = (id) => document.getElementById(id);

function pluginBase() {
  const path = window.location.pathname.replace(/\/+$/, "");
  if (path.endsWith("/console") || path.endsWith("/console/index.html")) {
    return path.replace(/\/console(?:\/index\.html)?$/, "");
  }
  return path;
}

async function apiGet(route, query = {}) {
  if (window.AstrBotPluginPage?.apiGet) {
    return window.AstrBotPluginPage.apiGet(route, query);
  }
  const params = new URLSearchParams(query).toString();
  const url = `${pluginBase()}/${route}${params ? `?${params}` : ""}`;
  const res = await fetch(url);
  return res.json();
}

async function apiPost(route, body = {}) {
  if (window.AstrBotPluginPage?.apiPost) {
    return window.AstrBotPluginPage.apiPost(route, body);
  }
  const res = await fetch(`${pluginBase()}/${route}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return res.json();
}

function showError(err) {
  const box = $("diag");
  if (box) box.textContent = String(err && err.stack ? err.stack : err);
  console.error(err);
}

async function run(label, fn) {
  try {
    const r = await fn();
    if (r !== undefined) $("diag").textContent = JSON.stringify(r, null, 2);
    return r;
  } catch (err) {
    showError(`${label} 失败: ${err}`);
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
    <div class="lede">QQ/id ${esc(f.speaker_id || "")} · 槽 ${esc(f.slot_key || f.attribute)}</div>
    ${extra}
    <div class="row">
      <button class="ghost" type="button" data-del="${esc(f.id)}">删除此条</button>
    </div>
  </div>`;
}

function bindDeletes(root) {
  root.querySelectorAll("[data-del]").forEach((btn) => {
    btn.onclick = () => run("删除", async () => {
      if (!confirm(`归档 #${btn.dataset.del}？不硬删除，可回滚。`)) return { ok: false, error: "cancelled" };
      const r = await apiPost("facts/archive", { ids: [Number(btn.dataset.del)] });
      await reload();
      return r;
    });
  });
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
  const usage = ov.usage || {};
  const emb = ov.embedding || {};
  const embLine = emb.active
    ? `Embedding 实际启用（${emb.reason}，live ${emb.live}/${emb.threshold}）`
    : emb.reason === "need_provider"
      ? `live 已达 ${emb.live}/${emb.threshold}，但没有 Embedding Provider，仍走本地检索。`
      : `Embedding 关闭（live ${emb.live || 0}/${emb.threshold || 2500}）。`;
  $("coexist").textContent = (reasons.length ? `共存降级：${reasons.join("；")}。` : "未检测到重叠记忆插件。") + " " + embLine;
  const box = $("alias-suggestions");
  if (box) {
    const items = ov.alias_suggestions || [];
    box.innerHTML = items.length
      ? items.map((s) => `<div class="item">同名 ${esc(s.name)}：<code>${esc(s.alias)}</code> → <code>${esc(s.canonical_id)}</code>
          <button type="button" data-alias="${esc(s.alias)}" data-canonical="${esc(s.canonical_id)}">映射</button></div>`).join("")
      : `<p class="lede">没有同名不同 id 的建议。</p>`;
    box.querySelectorAll("[data-alias]").forEach((btn) => {
      btn.onclick = () => run("映射", async () => {
        await apiPost("aliases/set", { alias: btn.dataset.alias, canonical_id: btn.dataset.canonical });
        await reload();
      });
    });
  }
  $("diag").textContent = JSON.stringify({ overview: ov, usage }, null, 2);
}

async function loadFacts() {
  const live = await apiGet("facts", { status: "live" });
  const archived = await apiGet("facts", { status: "superseded" });
  $("archived").innerHTML = (archived.items || []).slice(0, 12).map((f) =>
    factItem(f, f.superseded_by ? `<div class="lede">被 #${esc(f.superseded_by)} 覆盖</div>` : "", false)
  ).join("") || `<p class="lede">没有 superseded 记录。</p>`;
  if (!$("hits").dataset.locked) {
    $("hits").innerHTML = (live.items || []).slice(0, 30).map((f) => factItem(f, "", true)).join("")
      || `<p class="lede">还没有 live 事实。</p>`;
    bindDeletes($("hits"));
  }
}

async function loadPending() {
  const data = await apiGet("pending");
  const items = data.items || [];
  if (!items.length) {
    $("pending").innerHTML = `<p class="lede">没有待确认覆盖。</p>`;
    return;
  }
  $("pending").innerHTML = items.map((p) => `<div class="item">
    <div>#${esc(p.id)} ← old ${esc(p.old_fact_id)} · ${esc(p.reason)}</div>
    <div class="lede">${esc(JSON.stringify(p.new_payload))}</div>
    <div class="row">
      <button type="button" data-confirm="${esc(p.id)}">确认覆盖</button>
      <button class="ghost" type="button" data-reject="${esc(p.id)}">驳回</button>
    </div>
  </div>`).join("");
  $("pending").querySelectorAll("[data-confirm]").forEach((btn) => {
    btn.onclick = () => run("确认覆盖", async () => {
      await apiPost("pending/confirm", { id: Number(btn.dataset.confirm) });
      await reload();
    });
  });
  $("pending").querySelectorAll("[data-reject]").forEach((btn) => {
    btn.onclick = () => run("驳回覆盖", async () => {
      await apiPost("pending/reject", { id: Number(btn.dataset.reject) });
      await reload();
    });
  });
}

async function loadReviews() {
  const data = await apiGet("reviews", { status: "pending" });
  const items = data.items || [];
  const box = $("reviews");
  if (!box) return;
  if (!items.length) {
    box.innerHTML = `<p class="lede">没有待审学习项。</p>`;
    return;
  }
  box.innerHTML = items.map((r) => `<div class="item">
    <span class="chip">${esc(r.kind)}</span>
    <span class="chip">Q${esc(r.quality ?? (r.payload && r.payload.quality) ?? 0)}</span>
    <span class="chip">${esc(speakerLabel("", r.speaker_id))}</span>
    <b>#${esc(r.id)}</b> ${esc(r.title)}
    <div class="lede">QQ/id ${esc(r.speaker_id || "")} · ${esc(r.reason)}</div>
    <div class="lede">${esc(JSON.stringify(r.payload))}</div>
    <div class="row">
      <button type="button" data-approve="${esc(r.id)}">批准</button>
      <button class="ghost" type="button" data-reject-review="${esc(r.id)}">驳回</button>
    </div>
  </div>`).join("");
  box.querySelectorAll("[data-approve]").forEach((btn) => {
    btn.onclick = () => run("批准", async () => {
      await apiPost("reviews/set", { id: Number(btn.dataset.approve), status: "approved" });
      await reload();
    });
  });
  box.querySelectorAll("[data-reject-review]").forEach((btn) => {
    btn.onclick = () => run("驳回学习", async () => {
      await apiPost("reviews/set", { id: Number(btn.dataset.rejectReview), status: "rejected" });
      await reload();
    });
  });
}

async function loadMicroscope() {
  const box = $("microscope");
  if (!box) return;
  const data = await apiGet("microscope", { n: 8 });
  const items = data.items || [];
  if (!items.length) {
    box.innerHTML = `<p class="lede">还没有注入记录。说几句让主链跑起来就会出现。</p>`;
    return;
  }
  box.innerHTML = items.map((it) => {
    const p = it.payload || {};
    const blocked = (p.blocked || []).map((b) => `${b.id}:${b.reason}`).join("；") || "无";
    const who = speakerLabel("", p.speaker_id);
    return `<div class="item">
      <span class="chip">${esc(p.route)}</span>
      <span class="chip">${esc(p.path)}</span>
      <span class="chip">${esc(who)}</span>
      <div>${esc(p.query || "")}</div>
      <div class="lede">QQ/id ${esc(p.speaker_id || "")} · core=${esc(p.core)} related=${esc(p.related)} chars=${esc(p.pack_chars)}</div>
      <div class="lede">blocked ${esc(blocked)}</div>
    </div>`;
  }).join("");
}

function fieldControl(key, spec, value) {
  const hint = spec.hint ? `<div class="lede">${esc(spec.hint)}</div>` : "";
  const label = `<label for="cfg-${esc(key)}">${esc(spec.description || key)}</label>`;
  if (spec.type === "bool") {
    const on = value ? "checked" : "";
    return `<div class="setting">${label}
      <label class="toggle"><input id="cfg-${esc(key)}" name="${esc(key)}" type="checkbox" ${on} /> 开启</label>
      ${hint}</div>`;
  }
  if (Array.isArray(spec.options) && spec.options.length) {
    const opts = spec.options.map((o) =>
      `<option value="${esc(o)}" ${String(o) === String(value) ? "selected" : ""}>${esc(o)}</option>`
    ).join("");
    return `<div class="setting">${label}
      <select id="cfg-${esc(key)}" name="${esc(key)}">${opts}</select>
      ${hint}</div>`;
  }
  const typ = spec.type === "int" || spec.type === "float" ? "number" : "text";
  const step = spec.type === "float" ? "0.01" : spec.type === "int" ? "1" : "";
  const stepAttr = step ? `step="${step}"` : "";
  return `<div class="setting">${label}
    <input id="cfg-${esc(key)}" name="${esc(key)}" type="${typ}" ${stepAttr} value="${esc(value ?? "")}" />
    ${hint}</div>`;
}

async function loadSettings() {
  const form = $("settings-form");
  if (!form) return;
  const data = await apiGet("config");
  const schema = data.schema || {};
  const values = data.values || {};
  form.innerHTML = Object.entries(schema).map(([key, spec]) =>
    fieldControl(key, spec, values[key])
  ).join("");
}

function readSettings() {
  const form = $("settings-form");
  const values = {};
  form.querySelectorAll("[name]").forEach((el) => {
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
  document.querySelectorAll(".tabs button").forEach((b) => {
    b.classList.toggle("on", b.dataset.tab === name);
  });
  if (name === "settings") run("加载设置", loadSettings);
}

function bindUi() {
  document.querySelectorAll(".tabs button").forEach((b) => {
    b.onclick = () => showTab(b.dataset.tab);
  });
  $("refresh").onclick = () => run("刷新", reload);
  $("extract").onclick = () => run("抽取", async () => {
    const r = await apiPost("extract", {});
    await reload();
    return r;
  });
  $("sleep").onclick = () => run("维护", async () => {
    const r = await apiPost("sleep", {});
    await reload();
    return r;
  });
  $("learn").onclick = () => run("学习", async () => {
    const r = await apiPost("learn", {});
    await reload();
    return r;
  });
  $("search").onclick = () => run("检索", async () => {
    $("hits").dataset.locked = "1";
    const data = await apiGet("search", {
      q: $("q").value,
      speaker_id: $("speaker").value,
      k: 30,
    });
    $("hits").innerHTML = (data.items || []).map((f) => factItem(f, "", true)).join("")
      || `<p class="lede">没有命中。</p>`;
    bindDeletes($("hits"));
    return data;
  });
  $("batch-archive").onclick = () => run("批量删除", async () => {
    const ids = [...$("hits").querySelectorAll(".pick:checked")].map((el) => Number(el.dataset.id));
    if (!ids.length) return { ok: false, error: "未选择" };
    if (!confirm(`归档 ${ids.length} 条？不硬删除。`)) return { ok: false, error: "cancelled" };
    const r = await apiPost("facts/archive", { ids });
    $("hits").dataset.locked = "";
    await reload();
    return r;
  });
  $("remember").onclick = () => run("记住", async () => {
    const content = $("remember-text").value.trim();
    if (!content) return { ok: false, error: "empty" };
    const r = await apiPost("remember", {
      content,
      speaker_id: $("remember-speaker").value || "manual",
    });
    $("remember-text").value = "";
    await reload();
    return r;
  });
  $("rollback").onclick = () => run("回滚", async () => {
    const id = Number($("rollback-id").value);
    if (!id) return { ok: false, error: "missing id" };
    const r = await apiPost("rollback", { id });
    await reload();
    return r;
  });
  $("export").onclick = () => run("导出", () => apiGet("export"));
  $("archive-preview").onclick = () => run("预览档案", async () => {
    const path = $("archive-path").value.trim();
    if (!path) return { ok: false, error: "empty path" };
    return apiPost("archive/preview", { path });
  });
  $("archive-import").onclick = () => run("导入档案", async () => {
    const path = $("archive-path").value.trim();
    if (!path) return { ok: false, error: "empty path" };
    const r = await apiPost("archive/import", { path });
    await reload();
    return r;
  });
  $("chat-preview").onclick = () => run("预览聊天", async () => {
    const text = $("chat-text").value.trim();
    if (!text) return { ok: false, error: "empty" };
    return apiPost("chat/preview", {
      text,
      user_names: $("chat-users").value,
      bot_names: $("chat-bots").value,
    });
  });
  $("chat-import").onclick = () => run("导入聊天", async () => {
    const text = $("chat-text").value.trim();
    if (!text) return { ok: false, error: "empty" };
    const r = await apiPost("chat/import", {
      text,
      user_names: $("chat-users").value,
      bot_names: $("chat-bots").value,
    });
    await reload();
    return r;
  });
  $("settings-save").onclick = () => run("保存设置", async () => {
    const r = await apiPost("config/save", { values: readSettings() });
    await loadSettings();
    await reload();
    return r;
  });
}

async function boot() {
  bindUi();
  if (window.AstrBotPluginPage?.ready) {
    try { await window.AstrBotPluginPage.ready(); } catch (err) { showError(err); }
  }
  await run("刷新", reload);
}

boot();
