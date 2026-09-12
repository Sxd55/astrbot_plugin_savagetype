import { initShaderGradient } from "./shader.js";

const $ = (id) => document.getElementById(id);

let shaderControls = null;
let editingFactId = 0;
let editingFactWhere = "memory";
let editingReviewId = 0;
let currentProfileId = "";
let profilesCache = [];
let memoryCache = [];

function unwrap(result) {
  if (result && typeof result === "object" && "status" in result && "data" in result) {
    if (result.status === "error") {
      const err = new Error(result.message || "request failed");
      err.payload = result;
      throw err;
    }
    return result.data;
  }
  return result;
}

async function apiGet(route, query = {}) {
  const bridge = window.AstrBotPluginPage;
  if (!bridge?.apiGet) throw new Error("AstrBotPluginPage 未就绪，请在 AstrBot 拓展页打开");
  return unwrap(await bridge.apiGet(route, query));
}

async function apiPost(route, body = {}) {
  const bridge = window.AstrBotPluginPage;
  if (!bridge?.apiPost) throw new Error("AstrBotPluginPage 未就绪，请在 AstrBot 拓展页打开");
  return unwrap(await bridge.apiPost(route, body));
}

function toast(msg) {
  const el = $("toast");
  if (!el) return;
  el.hidden = false;
  el.textContent = msg;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, 2200);
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

function fmtTime(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function speakerLabel(name, id) {
  const n = (name || "").trim();
  const q = (id || "").trim();
  if (n && q && n !== q) return `${n} (${q})`;
  return n || q || "未知";
}

const THEME_PRESETS = [
  { name: "黑玫", a: "#1a1a1d", b: "#e6397c" },
  { name: "酒红米", a: "#990033", b: "#ddcdb7" },
  { name: "青蜜", a: "#01847f", b: "#ffaa93" },
  { name: "蓝黄", a: "#0081ff", b: "#fef99d" },
  { name: "粉紫", a: "#ffdbdd", b: "#652c97" },
  { name: "藏蓝米白", a: "#122e8a", b: "#f5efea" },
  { name: "炭黑青", a: "#2c2c34", b: "#00d4ff" },
  { name: "淡紫银", a: "#b19cd9", b: "#d3d3d3" },
];

const PROVIDER_KEYS = {
  summary_provider_id: "chat",
  normalize_provider_id: "chat",
  verify_provider_id: "chat",
  embedding_provider_id: "embedding",
  rerank_provider_id: "rerank",
};

function normalizeHex(value) {
  const raw = String(value || "").trim().toLowerCase();
  return /^#[0-9a-f]{6}$/.test(raw) ? raw : "";
}

function rgbaOf(hex, alpha) {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

function relLuminance(hex) {
  const channels = [1, 3, 5].map((i) => {
    const c = parseInt(hex.slice(i, i + 2), 16) / 255;
    return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
  });
  return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
}

function onColor(hex) {
  return relLuminance(hex) > 0.22 ? "#1c1b1f" : "#ffffff";
}

const themeState = { color: "#7c5cff", color2: "#22d3ee" };

function applyTheme(color, color2) {
  const raw = normalizeHex(color) || "#7c5cff";
  const raw2 = normalizeHex(color2) || "#22d3ee";
  themeState.color = raw;
  themeState.color2 = raw2;

  const root = document.documentElement.style;
  root.setProperty("--accent", raw);
  root.setProperty("--accent2", raw2);
  root.setProperty("--accent-soft", rgbaOf(raw, 0.16));
  root.setProperty("--accent2-soft", rgbaOf(raw2, 0.16));
  root.setProperty("--on-accent", onColor(raw));

  if (shaderControls) shaderControls.setColors(raw, raw2);
}

function renderThemeControls(color, color2) {
  const box = $("theme-presets");
  const ca = normalizeHex(color) || "#7c5cff";
  const cb = normalizeHex(color2) || "#22d3ee";
  if (box) {
    box.innerHTML = THEME_PRESETS.map((p) => `
      <button type="button" class="theme-dot ${p.a === ca && p.b === cb ? "on" : ""}"
        data-act="theme-preset" data-a="${p.a}" data-b="${p.b}">
        <span class="dot" style="background:${p.a}"></span><span class="dot" style="background:${p.b}"></span>${esc(p.name)}
      </button>`).join("");
  }
  if ($("theme-color")) $("theme-color").value = ca;
  if ($("theme-color2")) $("theme-color2").value = cb;
}

function reviewBadge(status) {
  const map = {
    ai_passed: ["ok", "AI 通过"],
    manual: ["ok", "手动"],
    unverified: ["warn", "未审核"],
  };
  const item = map[status];
  if (!item) return "";
  return `<span class="chip ${item[0]}">${esc(item[1])}</span>`;
}

function keywordsHtml(list) {
  const items = (list || []).slice(0, 4).map((k) => `<span class="chip gray">${esc(k)}</span>`).join(" ");
  return items ? ` ${items}` : "";
}

function factActions(id, where) {
  return `<div class="actions">
    <button type="button" class="ghost tiny" data-act="fact-edit" data-id="${esc(id)}" data-where="${esc(where)}">编辑</button>
    <button type="button" class="ghost tiny" data-act="fact-del" data-id="${esc(id)}" data-where="${esc(where)}">删除</button>
  </div>`;
}

function factEditor(f, where, quitAct) {
  return `<div class="item on">
    <div class="item-head"><span class="chip">#${esc(f.id)} ${esc(f.attribute)}</span><span class="chip gray">${esc(speakerLabel(f.speaker_name, f.speaker_id))}</span></div>
    <textarea data-edit-fact="${esc(f.id)}">${esc(f.plain || f.content || f.value)}</textarea>
    <div class="actions">
      <button type="button" data-act="fact-edit-save" data-id="${esc(f.id)}" data-where="${esc(where)}">保存</button>
      <button type="button" class="ghost tiny" data-act="${esc(quitAct)}">取消</button>
    </div>
  </div>`;
}

function factCard(f, where) {
  const who = speakerLabel(f.speaker_name, f.speaker_id);
  const text = f.plain || f.content || f.value;
  const detailId = `${where}-fact-${f.id}`;
  return `<div class="item" id="${esc(detailId)}">
    <div class="item-head">
      <span class="chip">${esc(f.attribute)}</span>
      <span class="chip gray">${esc(who)}</span>
      ${reviewBadge(f.review_status)}
      ${f.edited_at ? `<span class="chip gray">已编辑</span>` : ""}
    </div>
    <div class="plain">${esc(text)}</div>
    <div class="meta">QQ/id ${esc(f.speaker_id || "")} · #${esc(f.id)} · 更新 ${esc(fmtTime(f.updated_at))}${keywordsHtml(f.keywords)}</div>
    ${f.content && f.content !== text ? `<details><summary>原文</summary><div class="raw">${esc(f.content)}</div></details>` : ""}
    ${factActions(f.id, where)}
  </div>`;
}

async function loadOverview() {
  const ov = await apiGet("overview");
  const c = ov.counts || {};
  const kpiHtml = [
    ["时间线", c.timeline],
    ["主人记忆", c.owner_facts],
    ["人物档案", c.profiles],
    ["待审记忆", c.memory_pending],
  ].map(([k, v]) => `<div class="kpi"><b>${esc(v ?? 0)}</b><span>${esc(k)}</span></div>`).join("");
  ["kpis", "kpis-diag"].forEach((id) => { if ($(id)) $(id).innerHTML = kpiHtml; });
  const reasons = (ov.coexistence && ov.coexistence.reasons) || [];
  const emb = ov.embedding || {};
  const embLine = emb.active ? `Embedding 实际启用（${emb.reason}）` : "Embedding 关闭";
  const coexLine = (reasons.length ? `共存降级：${reasons.join("；")}。` : "未检测到重叠记忆插件。") + " " + embLine;
  if ($("coexist")) $("coexist").textContent = coexLine;
  if ($("coexist-diag")) $("coexist-diag").textContent = coexLine;

  const owner = ov.owner || {};
  const cfg = ov.config || {};
  if ($("owner-line")) {
    const ownerText = owner.qq
      ? `主人 QQ：${owner.qq}${owner.ids && owner.ids.length > 1 ? `（含归并 id：${owner.ids.join(", ")}）` : ""}。`
      : "主人 QQ 未配置：当前回退 AstrBot 管理员判定，建议在设置里填写，避免把别人的主人当成你的主人。";
    const platforms = (cfg.platforms || []).join(", ") || "不限";
    const skip = cfg.capture_skip;
    const skipText = skip ? `上次采集跳过：${skip.reason}（${skip.platform || "?"}）。` : "";
    $("owner-line").textContent = `${ownerText} 允许平台：${platforms}。${skipText}`;
  }
  applyTheme(cfg.theme_color, cfg.theme_color2);

  const box = $("alias-suggestions");
  if (box) {
    const items = ov.alias_suggestions || [];
    box.innerHTML = items.length
      ? items.map((s) => `<div class="item">同名 ${esc(s.name)}：<code>${esc(s.alias)}</code> → <code>${esc(s.canonical_id)}</code>
          <div class="actions"><button type="button" class="ghost tiny" data-act="alias" data-alias="${esc(s.alias)}" data-canonical="${esc(s.canonical_id)}">映射</button></div></div>`).join("")
      : `<p class="lede">没有同名不同 id 的建议。</p>`;
  }
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
  showDiag({ overview: ov });
}

async function loadMemory() {
  const q = $("memory-q") ? $("memory-q").value.trim() : "";
  const data = await apiGet("memory", q ? { q } : {});
  memoryCache = data.items || [];
  const box = $("memory-list");
  if (!box) return;
  if (!memoryCache.length) {
    box.innerHTML = `<p class="lede">还没有主人记忆。主人在 QQ 里说「记住/我喜欢/以后…」并审核通过后会出现。</p>`;
    return;
  }
  box.innerHTML = memoryCache.map((f) => (
    editingFactId === f.id && editingFactWhere === "memory"
      ? factEditor(f, "memory", "fact-edit-cancel")
      : factCard(f, "memory")
  )).join("");
}

async function loadMemoryPending() {
  const data = await apiGet("memory/pending");
  const items = data.items || [];
  const box = $("memory-pending");
  if (!box) return;
  if (!items.length) {
    box.innerHTML = `<p class="lede">没有待审记忆。</p>`;
    return;
  }
  box.innerHTML = items.map((r) => {
    if (editingReviewId === r.id) {
      return `<div class="item on">
        <div class="item-head"><span class="chip">#${esc(r.id)}</span><span class="chip gray">${esc(speakerLabel(r.speaker_name, r.speaker_id))}</span></div>
        <textarea data-edit-review="${esc(r.id)}">${esc(r.plain)}</textarea>
        <div class="actions">
          <button type="button" data-act="memory-pass-edit" data-id="${esc(r.id)}">保存并过审</button>
          <button type="button" class="ghost tiny" data-act="memory-edit-cancel">取消</button>
        </div>
      </div>`;
    }
    const trace = (r.trace || []).map((t) =>
      `第 ${esc(t.round)} 轮：${t.pass ? "通过" : "不通过"} ${esc(t.reason || "")} ${t.fix_hint ? "｜建议：" + esc(t.fix_hint) : ""}`
    ).join("\n");
    return `<div class="item">
      <div class="item-head">
        <span class="chip">#${esc(r.id)}</span>
        <span class="chip gray">${esc(r.scope === "owner" ? "主人" : "档案")}</span>
        <span class="chip gray">${esc(speakerLabel(r.speaker_name, r.speaker_id))}</span>
      </div>
      <div class="plain">${esc(r.plain)}</div>
      <div class="meta">来源 ${esc(r.speaker_id || "")} · ${esc(fmtTime(r.created_at))}${r.notified_at ? " · 已通知" : ""}</div>
      ${r.raw_text ? `<details><summary>原文</summary><div class="raw">${esc(r.raw_text)}</div></details>` : ""}
      ${trace ? `<details><summary>审核轨迹</summary><pre class="trace">${esc(trace)}</pre></details>` : ""}
      <div class="actions">
        <button type="button" data-act="memory-pass" data-id="${esc(r.id)}">通过</button>
        <button type="button" class="ghost tiny" data-act="memory-drop" data-id="${esc(r.id)}">删除</button>
        <button type="button" class="ghost tiny" data-act="memory-edit" data-id="${esc(r.id)}">编辑</button>
      </div>
    </div>`;
  }).join("");
}

async function loadReviews() {
  const data = await apiGet("reviews", { status: "pending" });
  const items = data.items || [];
  const box = $("reviews");
  if (!box) return;
  box.innerHTML = items.length
    ? items.map((r) => `<div class="item">
        <div class="item-head"><span class="chip">${esc(r.kind)}</span><b>#${esc(r.id)}</b> ${esc(r.title)}</div>
        <div class="meta">QQ/id ${esc(r.speaker_id || "")}</div>
        <div class="actions">
          <button type="button" class="tiny" data-act="approve" data-id="${esc(r.id)}">批准</button>
          <button type="button" class="ghost tiny" data-act="reject-review" data-id="${esc(r.id)}">驳回</button>
        </div>
      </div>`).join("")
    : `<p class="lede">没有待审学习项。</p>`;
}

async function loadPending() {
  const data = await apiGet("pending");
  const items = data.items || [];
  const box = $("pending");
  if (!box) return;
  box.innerHTML = items.length
    ? items.map((p) => `<div class="item">
        <div>#${esc(p.id)} ← old ${esc(p.old_fact_id)} · ${esc(p.reason)}</div>
        <div class="actions">
          <button type="button" class="tiny" data-act="pending-confirm" data-id="${esc(p.id)}">确认覆盖</button>
          <button type="button" class="ghost tiny" data-act="pending-reject" data-id="${esc(p.id)}">驳回</button>
        </div>
      </div>`).join("")
    : `<p class="lede">没有待确认覆盖。</p>`;
}

async function loadMicroscope() {
  const data = await apiGet("microscope", { n: 8 });
  const items = data.items || [];
  const box = $("microscope");
  if (!box) return;
  box.innerHTML = items.length
    ? items.map((it) => {
        const p = it.payload || {};
        return `<div class="item">
          <div class="item-head">
            <span class="chip">${esc(p.route)}</span>
            <span class="chip gray">${esc(speakerLabel("", p.speaker_id))}</span>
          </div>
          <div>${esc(p.query || "")}</div>
          <div class="meta">chars=${esc(p.pack_chars)} · core=${esc(p.core)} related=${esc(p.related)} · ${esc(fmtTime(it.ts))}</div>
        </div>`;
      }).join("")
    : `<p class="lede">还没有注入记录。</p>`;
}

async function loadProfiles() {
  const data = await apiGet("profiles");
  profilesCache = data.items || [];
  renderProfileList();
}

function renderProfileList() {
  const box = $("profile-list");
  if (!box) return;
  const q = ($("profile-q") ? $("profile-q").value : "").trim().toLowerCase();
  const items = profilesCache.filter((p) => {
    if (!q) return true;
    return (p.speaker_name || "").toLowerCase().includes(q) || (p.speaker_id || "").toLowerCase().includes(q);
  });
  if (!items.length) {
    box.innerHTML = `<p class="lede">还没有档案。QQ 里出现过的消息会自动建档。</p>`;
    return;
  }
  box.innerHTML = items.map((p) => `<div class="item selectable ${p.speaker_id === currentProfileId ? "on" : ""}" data-act="profile-open" data-id="${esc(p.speaker_id)}">
    <div class="item-head">
      <span class="chip gray">${esc(p.speaker_name || p.speaker_id)}</span>
      ${p.is_owner ? `<span class="chip ok">主人</span>` : ""}
      <span class="chip">${esc(p.fact_count)} 条</span>
    </div>
    <div class="meta">QQ/id ${esc(p.speaker_id)} · 最近 ${esc(fmtTime(p.last_seen))}</div>
  </div>`).join("");
}

async function loadProfile(speakerId) {
  currentProfileId = speakerId;
  renderProfileList();
  const data = await apiGet("profile", { speaker_id: speakerId });
  const p = data.profile || {};
  const facts = data.items || [];
  if ($("profile-title")) $("profile-title").textContent = p.speaker_name || speakerId;
  const box = $("profile-detail");
  if (!box) return;
  const factsHtml = facts.length
    ? facts.map((f) => (
        editingFactId === f.id && editingFactWhere === "profile"
          ? factEditor(f, "profile", "fact-edit-cancel")
          : factCard(f, "profile")
      )).join("")
    : `<p class="lede">这个档案还没有条目。</p>`;
  box.innerHTML = `
    <div class="row">
      <input id="profile-name" value="${esc(p.speaker_name || "")}" placeholder="昵称" />
      <button type="button" class="ghost tiny" data-act="profile-save">保存昵称/备注</button>
    </div>
    <textarea id="profile-note" placeholder="备注">${esc(p.note || "")}</textarea>
    <div class="meta">QQ/id ${esc(p.speaker_id)} · 首次 ${esc(fmtTime(p.first_seen))} · 出现 ${esc(p.seen_count)} 次</div>
    <h2 style="margin-top:14px">档案条目</h2>
    <div class="row">
      <textarea id="profile-add-text" placeholder="给这个人补一条（直接生效，标记 manual）"></textarea>
    </div>
    <div class="row">
      <button type="button" data-act="profile-add">写入条目</button>
    </div>
    <div class="scroll">${factsHtml}</div>
  `;
}

function fieldControl(key, spec, value, providers) {
  const hint = spec.hint ? `<div class="lede">${esc(spec.hint)}</div>` : "";
  const label = `<label for="cfg-${esc(key)}">${esc(spec.description || key)}</label>`;
  const providerType = PROVIDER_KEYS[key];
  if (providerType) {
    const list = (providers && providers[providerType]) || [];
    const known = list.some((p) => String(p.id) === String(value));
    const opts = [`<option value="">（跟随默认）</option>`]
      .concat(list.map((p) =>
        `<option value="${esc(p.id)}" ${String(p.id) === String(value) ? "selected" : ""}>${esc(p.name || p.id)}</option>`
      ))
      .concat(value && !known ? [`<option value="${esc(value)}" selected>（当前）${esc(value)}</option>`] : [])
      .join("");
    return `<div class="setting">${label}<select id="cfg-${esc(key)}" name="${esc(key)}">${opts}</select>${hint}</div>`;
  }
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
  const [data, providers] = await Promise.all([apiGet("config"), apiGet("providers")]);
  const schema = data.schema || {};
  const values = data.values || {};
  renderThemeControls(values.ui_theme_color, values.ui_theme_color2);
  $("settings-form").innerHTML = Object.entries(schema).map(([key, spec]) =>
    fieldControl(key, spec, values[key], providers)
  ).join("");
}

async function saveTheme(color, color2) {
  const r = await apiPost("ui/theme", { color, color2 });
  applyTheme(r.color, r.color2);
  renderThemeControls(r.color, r.color2);
  return r;
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
  await loadMemory();
  await loadMemoryPending();
  await loadProfiles();
  await loadReviews();
  await loadPending();
  await loadMicroscope();
}

function showTab(name) {
  ["memory", "profiles", "diag", "settings"].forEach((key) => {
    const page = $(`page-${key}`);
    if (!page) return;
    page.hidden = key !== name;
    if (key === name) {
      page.classList.remove("page-enter");
      void page.offsetWidth;
      page.classList.add("page-enter");
    }
  });
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === name));
  if (name === "settings") run("加载设置", loadSettings);
  if (name === "profiles") run("已刷新档案", loadProfiles);
}

function attachRipple(ev) {
  const el = ev.target.closest("button, .tabs button, .theme-dot, .item.selectable");
  if (!el || el.disabled) return;
  const rect = el.getBoundingClientRect();
  const size = Math.max(rect.width, rect.height) * 2;
  const ripple = document.createElement("span");
  ripple.className = "ripple";
  ripple.style.width = `${size}px`;
  ripple.style.height = `${size}px`;
  ripple.style.left = `${ev.clientX - rect.left - size / 2}px`;
  ripple.style.top = `${ev.clientY - rect.top - size / 2}px`;
  el.appendChild(ripple);
  setTimeout(() => ripple.remove(), 520);
}

async function saveFactEdit(el) {
  const id = Number(el.dataset.id);
  const where = el.dataset.where || "memory";
  const area = document.querySelector(`textarea[data-edit-fact="${id}"]`);
  const plain = area ? area.value.trim() : "";
  if (!plain) return { ok: false, error: "empty" };
  const r = await apiPost("facts/update", { id, plain });
  editingFactId = 0;
  if (where === "profile") await loadProfile(currentProfileId);
  else await loadMemory();
  return r;
}

async function onAct(act, el) {
  if (act === "tab") return showTab(el.dataset.tab);
  if (act === "refresh") return run("已刷新", reload);
  if (act === "extract") return run("已整理", async () => { const r = await apiPost("extract", {}); await reload(); return r; });
  if (act === "sleep") return run("已维护", async () => { const r = await apiPost("sleep", {}); await reload(); return r; });
  if (act === "learn") return run("已学习", async () => { const r = await apiPost("learn", {}); await reload(); return r; });
  if (act === "export") return run("已导出", async () => {
    const bridge = window.AstrBotPluginPage;
    if (bridge?.download) {
      await bridge.download("export", {}, "savagetype.jsonl");
      return { ok: true };
    }
    const r = await apiGet("export");
    return { ok: true, filename: r && r.filename };
  });
  if (act === "memory-search") return run("已搜索", loadMemory);
  if (act === "memory-pass") return run("已通过", async () => {
    const r = await apiPost("memory/review", { id: Number(el.dataset.id), status: "approved" });
    await loadMemoryPending(); await loadMemory(); await loadOverview();
    return r;
  });
  if (act === "memory-drop") return run("已删除", async () => {
    const r = await apiPost("memory/review", { id: Number(el.dataset.id), status: "rejected" });
    await loadMemoryPending(); await loadOverview();
    return r;
  });
  if (act === "memory-edit") { editingReviewId = Number(el.dataset.id); await loadMemoryPending(); return; }
  if (act === "memory-edit-cancel") { editingReviewId = 0; await loadMemoryPending(); return; }
  if (act === "memory-pass-edit") return run("已过审", async () => {
    const id = Number(el.dataset.id);
    const area = document.querySelector(`textarea[data-edit-review="${id}"]`);
    const plain = area ? area.value.trim() : "";
    if (!plain) return { ok: false, error: "empty" };
    const r = await apiPost("memory/review", { id, status: "approved", plain });
    editingReviewId = 0;
    await loadMemoryPending(); await loadMemory(); await loadOverview();
    return r;
  });
  if (act === "fact-edit") {
    editingFactId = Number(el.dataset.id);
    editingFactWhere = el.dataset.where || "memory";
    if (editingFactWhere === "profile") await loadProfile(currentProfileId);
    else await loadMemory();
    return;
  }
  if (act === "fact-edit-cancel") {
    editingFactId = 0;
    if (editingFactWhere === "profile") await loadProfile(currentProfileId);
    else await loadMemory();
    return;
  }
  if (act === "fact-edit-save") return run("已保存", () => saveFactEdit(el));
  if (act === "fact-del") return run("已删除", async () => {
    await apiPost("facts/archive", { ids: [Number(el.dataset.id)] });
    if (el.dataset.where === "profile") await loadProfile(currentProfileId);
    else await loadMemory();
    await loadOverview();
  });
  if (act === "remember") return run("已写入", async () => {
    const content = $("remember-text").value.trim();
    if (!content) return { ok: false, error: "empty" };
    const custom = ($("remember-speaker-custom").value || "").trim();
    const speaker_id = custom || $("remember-speaker").value || "admin";
    const r = await apiPost("remember", { content, speaker_id });
    $("remember-text").value = "";
    await loadMemory(); await loadOverview();
    return r;
  });
  if (act === "profiles-refresh") return run("已刷新档案", loadProfiles);
  if (act === "profile-open") return run("已打开", () => loadProfile(el.dataset.id));
  if (act === "profile-save") return run("已保存", async () => {
    const r = await apiPost("profile/update", {
      speaker_id: currentProfileId,
      speaker_name: $("profile-name").value,
      note: $("profile-note").value,
    });
    await loadProfiles(); await loadProfile(currentProfileId);
    return r;
  });
  if (act === "profile-add") return run("已写入", async () => {
    const content = $("profile-add-text").value.trim();
    if (!content) return { ok: false, error: "empty" };
    const r = await apiPost("remember", { content, speaker_id: currentProfileId });
    await loadProfile(currentProfileId); await loadOverview();
    return r;
  });
  if (act === "alias") return run("已映射", async () => {
    await apiPost("aliases/set", { alias: el.dataset.alias, canonical_id: el.dataset.canonical });
    await loadOverview();
  });
  if (act === "pending-confirm") return run("已确认覆盖", async () => { await apiPost("pending/confirm", { id: Number(el.dataset.id) }); await reload(); });
  if (act === "pending-reject") return run("已驳回", async () => { await apiPost("pending/reject", { id: Number(el.dataset.id) }); await reload(); });
  if (act === "approve") return run("已批准", async () => { await apiPost("reviews/set", { id: Number(el.dataset.id), status: "approved" }); await loadReviews(); });
  if (act === "reject-review") return run("已驳回", async () => { await apiPost("reviews/set", { id: Number(el.dataset.id), status: "rejected" }); await loadReviews(); });
  if (act === "archive-preview") return run("已预览", () => apiPost("archive/preview", { path: $("archive-path").value.trim() }));
  if (act === "archive-import") return run("已导入档案", async () => { const r = await apiPost("archive/import", { path: $("archive-path").value.trim() }); await reload(); return r; });
  if (act === "chat-preview") return run("已预览聊天", () => apiPost("chat/preview", { text: $("chat-text").value, user_names: $("chat-users").value, bot_names: $("chat-bots").value }));
  if (act === "chat-import") return run("已导入聊天", async () => { const r = await apiPost("chat/import", { text: $("chat-text").value, user_names: $("chat-users").value, bot_names: $("chat-bots").value }); await reload(); return r; });
  if (act === "reset") return run("已清空重建", async () => {
    const r = await apiPost("reset", { confirm: "reset" });
    await reload();
    return r;
  });
  if (act === "theme-preset") return run("主题已切换", () => saveTheme(el.dataset.a, el.dataset.b));
  if (act === "theme-apply") return run("主题已切换", () => saveTheme($("theme-color").value, $("theme-color2").value));
  if (act === "settings-save") return run("已保存设置", async () => { const r = await apiPost("config/save", { values: readSettings() }); await loadSettings(); await loadOverview(); return r; });
}

document.addEventListener("pointerdown", (ev) => {
  if (ev.button !== 0) return;
  attachRipple(ev);
});

document.addEventListener("click", (ev) => {
  const el = ev.target.closest("[data-act]");
  if (!el) return;
  ev.preventDefault();
  onAct(el.dataset.act, el);
});

async function boot() {
  shaderControls = initShaderGradient({
    color1: themeState.color,
    color2: themeState.color2,
  });
  if (window.AstrBotPluginPage?.ready) {
    try { await window.AstrBotPluginPage.ready(); } catch (err) { showDiag(err); }
  }
  await run("已刷新", reload);
}

boot();
