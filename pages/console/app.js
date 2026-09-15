import { initShaderGradient } from "./shader.js";

const $ = (id) => document.getElementById(id);

let shaderControls = null;
let editingFactId = 0;
let editingFactWhere = "memory";
let editingReviewId = 0;
let currentProfileId = "";
let profilesCache = [];
let memoryCache = [];
let memoryMode = "owner";
let reviewsStatus = "pending";
let reviewsKind = "";
let eventsStatus = "live";
let editingEventId = 0;
let settingsGroup = (() => {
  try {
    return localStorage.getItem("stype.settings.group") || "";
  } catch (err) {
    return "";
  }
})();

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
  { name: "极光", a: "#7c5cff", b: "#22d3ee", c: "#f472b6" },
  { name: "碧金", a: "#14b8a6", b: "#fbbf24", c: "#38bdf8" },
  { name: "暮霞", a: "#7c3aed", b: "#ec4899", c: "#fb923c" },
  { name: "午夜", a: "#2563eb", b: "#8b5cf6", c: "#f472b6" },
  { name: "森林", a: "#10b981", b: "#22d3ee", c: "#818cf8" },
];

const PROVIDER_KEYS = {
  summary_provider_id: "chat",
  normalize_provider_id: "chat",
  verify_provider_id: "chat",
  event_provider_id: "chat",
  quality_provider_id: "chat",
  fast_provider_id: "chat",
  fallback_provider_id: "chat",
  image_caption_provider_id: "chat",
  embedding_provider_id: "embedding",
  rerank_provider_id: "rerank",
};

const TASK_LABELS = {
  normalize: "缩写",
  verify: "审核",
  event: "事件",
  learn: "学习",
  image: "图片",
  embed: "向量",
  rerank: "重排",
  default: "其他",
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

const themeState = { color: "#7c5cff", color2: "#22d3ee", color3: "#f472b6" };

function applyTheme(color, color2, color3) {
  const raw = normalizeHex(color) || "#7c5cff";
  const raw2 = normalizeHex(color2) || "#22d3ee";
  const raw3 = normalizeHex(color3) || "#f472b6";
  themeState.color = raw;
  themeState.color2 = raw2;
  themeState.color3 = raw3;

  const root = document.documentElement.style;
  root.setProperty("--accent", raw);
  root.setProperty("--accent2", raw2);
  root.setProperty("--accent3", raw3);
  root.setProperty("--accent-soft", rgbaOf(raw, 0.16));
  root.setProperty("--accent2-soft", rgbaOf(raw2, 0.16));
  root.setProperty("--accent3-soft", rgbaOf(raw3, 0.16));
  root.setProperty("--on-accent", onColor(raw));

  if (shaderControls) shaderControls.setColors(raw, raw2, raw3);
}

const PREFERS_REDUCED = Boolean(
  window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches
);
const DYNAMIC_DWELL_MS = 1000;
const DYNAMIC_MORPH_MS = 5000;
let dynamicOn = false;
let dynamicFrame = 0;

function lerpHex(a, b, t) {
  const ra = [1, 3, 5].map((i) => parseInt(a.slice(i, i + 2), 16));
  const rb = [1, 3, 5].map((i) => parseInt(b.slice(i, i + 2), 16));
  const out = ra.map((v, i) => Math.round(v + (rb[i] - v) * t));
  return `#${out.map((v) => Math.max(0, Math.min(255, v)).toString(16).padStart(2, "0")).join("")}`;
}

function easeInOut(t) {
  return t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
}

function updateDynamicButton() {
  const btn = $("dynamic-toggle");
  if (!btn) return;
  btn.textContent = dynamicOn ? "动态颜色：开" : "动态颜色：关";
  btn.classList.toggle("on", dynamicOn);
  btn.classList.toggle("ghost", !dynamicOn);
}

function startDynamic() {
  if (dynamicOn || PREFERS_REDUCED) return;
  dynamicOn = true;
  updateDynamicButton();
  let idx = THEME_PRESETS.findIndex(
    (p) => p.a === themeState.color && p.b === themeState.color2 && p.c === themeState.color3
  );
  if (idx < 0) idx = 0;
  let from = [themeState.color, themeState.color2, themeState.color3];
  let target = null;
  let phase = "hold";
  let holdUntil = performance.now() + DYNAMIC_DWELL_MS;
  let morphStart = 0;
  const step = () => {
    if (!dynamicOn) return;
    const now = performance.now();
    if (phase === "hold") {
      if (now >= holdUntil) {
        idx = (idx + 1) % THEME_PRESETS.length;
        const preset = THEME_PRESETS[idx];
        target = [preset.a, preset.b, preset.c];
        from = [themeState.color, themeState.color2, themeState.color3];
        morphStart = now;
        phase = "morph";
      }
    } else {
      const t = Math.min(1, (now - morphStart) / DYNAMIC_MORPH_MS);
      const e = easeInOut(t);
      applyTheme(
        lerpHex(from[0], target[0], e),
        lerpHex(from[1], target[1], e),
        lerpHex(from[2], target[2], e)
      );
      if (t >= 1) {
        phase = "hold";
        holdUntil = now + DYNAMIC_DWELL_MS;
      }
    }
    dynamicFrame = requestAnimationFrame(step);
  };
  dynamicFrame = requestAnimationFrame(step);
}

function stopDynamic(save) {
  const was = dynamicOn;
  dynamicOn = false;
  if (dynamicFrame) {
    cancelAnimationFrame(dynamicFrame);
    dynamicFrame = 0;
  }
  updateDynamicButton();
  if (save && was) apiPost("ui/dynamic", { enabled: false }).catch(() => {});
}

function renderThemeControls(color, color2, color3) {
  const box = $("theme-presets");
  const ca = normalizeHex(color) || "#7c5cff";
  const cb = normalizeHex(color2) || "#22d3ee";
  const cc = normalizeHex(color3) || "#f472b6";
  if (box) {
    box.innerHTML = THEME_PRESETS.map((p) => `
      <button type="button" class="theme-dot ${p.a === ca && p.b === cb && p.c === cc ? "on" : ""}"
        data-act="theme-preset" data-a="${p.a}" data-b="${p.b}" data-c="${p.c}">
        <span class="dot" style="background:${p.a}"></span><span class="dot" style="background:${p.b}"></span><span class="dot" style="background:${p.c}"></span>${esc(p.name)}
      </button>`).join("");
  }
  if ($("theme-color")) $("theme-color").value = ca;
  if ($("theme-color2")) $("theme-color2").value = cb;
  if ($("theme-color3")) $("theme-color3").value = cc;
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

function factActions(f, where) {
  const pinLabel = f.pinned ? "取消置顶" : "置顶";
  return `<div class="actions">
    <button type="button" class="ghost tiny" data-act="fact-pin" data-id="${esc(f.id)}" data-pinned="${f.pinned ? 1 : 0}" data-where="${esc(where)}">${pinLabel}</button>
    <button type="button" class="ghost tiny" data-act="fact-edit" data-id="${esc(f.id)}" data-where="${esc(where)}">编辑</button>
    ${f.supersedes ? `<button type="button" class="ghost tiny" data-act="fact-rollback" data-id="${esc(f.id)}" data-where="${esc(where)}">回滚改口</button>` : ""}
    <button type="button" class="ghost tiny" data-act="fact-entities" data-id="${esc(f.id)}">相关</button>
    <button type="button" class="ghost tiny" data-act="fact-del" data-id="${esc(f.id)}" data-where="${esc(where)}">删除</button>
  </div>
  <div class="meta" id="fact-entities-${esc(f.id)}" hidden></div>`;
}

function factEditor(f, where, quitAct) {
  return `<div class="item on">
    <div class="item-head"><span class="chip">#${esc(f.id)} ${esc(f.attribute)}</span><span class="chip gray">${esc(speakerLabel(f.speaker_name, f.speaker_id))}</span></div>
    <textarea data-edit-fact="${esc(f.id)}">${esc(f.plain || f.content || f.value)}</textarea>
    <div class="row">
      <label class="lede">重要度
        <input type="number" min="0" max="1" step="0.05" style="width:110px"
          data-edit-importance="${esc(f.id)}" value="${esc(f.importance ?? 0)}" />
      </label>
      <span class="lede">当前权重 ${esc(f.weight ?? "-")}（随时间衰减，被召回会回升）</span>
    </div>
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
      ${f.kind ? `<span class="chip gray">${esc(f.kind)}</span>` : ""}
      ${f.topic ? `<span class="chip gray">领域 ${esc(f.topic)}</span>` : ""}
      <span class="chip gray">${esc(who)}</span>
      ${reviewBadge(f.review_status)}
      ${f.pinned ? `<span class="chip">置顶</span>` : ""}
      ${typeof f.weight === "number" ? `<span class="chip gray">权重 ${esc(f.weight)}</span>` : ""}
      ${f.edited_at ? `<span class="chip gray">已编辑</span>` : ""}
    </div>
    <div class="plain">${esc(text)}</div>
    <div class="meta">QQ/id ${esc(f.speaker_id || "")} · #${esc(f.id)} · 更新 ${esc(fmtTime(f.updated_at))}${f.valid_to ? ` · 有效期 ${esc(fmtTime(f.created_at))} ~ ${esc(fmtTime(f.valid_to))}` : ""}${keywordsHtml(f.keywords)}</div>
    ${f.content && f.content !== text ? `<details><summary>原文</summary><div class="raw">${esc(f.content)}</div></details>` : ""}
    ${factActions(f, where)}
  </div>`;
}

async function loadOverview() {
  const ov = await apiGet("overview");
  const c = ov.counts || {};
  const kpiHtml = [
    ["时间线", c.timeline],
    ["主人记忆", c.owner_facts],
    ["事件", c.events],
    ["人物档案", c.profiles],
    ["待审记忆", c.memory_pending],
    ["事件待审", c.events_needs_review],
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
  if ($("usage-line")) {
    const tk = ov.tokens || {};
    const hard = tk.hard_limit ? String(tk.hard_limit) : "不限";
    const soft = tk.soft_limit ? String(tk.soft_limit) : "不限";
    const rows = (tk.by_task || []).filter((r) => (r.tokens || 0) > 0 || (r.skipped || 0) > 0);
    const skipped = rows.reduce((n, r) => n + (r.skipped || 0), 0);
    const top = rows
      .slice(0, 4)
      .map((r) => `${TASK_LABELS[r.task] || r.task} ${r.tokens}${r.skipped ? `(跳过${r.skipped})` : ""}`)
      .join(" / ");
    $("usage-line").textContent =
      `今日模型用量 ${tk.used || 0} tokens（硬限 ${hard} / 软限 ${soft}${skipped ? ` / 预算跳过 ${skipped} 次` : ""}）` +
      (top ? ` · ${top}` : "");
  }
  if (!dynamicOn) applyTheme(cfg.theme_color, cfg.theme_color2, cfg.theme_color3);
  if (cfg.dynamic_colors && !dynamicOn) startDynamic();
  if (!cfg.dynamic_colors && dynamicOn) {
    stopDynamic(false);
    applyTheme(cfg.theme_color, cfg.theme_color2, cfg.theme_color3);
  }

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
    const prev = sel.value;
    sel.innerHTML = speakers.map((s) =>
      `<option value="${esc(s.id)}">${esc(s.name)} (${esc(s.id)})</option>`
    ).join("");
    if (prev && speakers.some((s) => s.id === prev)) {
      sel.value = prev;
    } else {
      const ownerIds = (ov.owner && ov.owner.ids) || [];
      const ownerId = ownerIds.find((id) => speakers.some((s) => s.id === id))
        || (ov.owner && ov.owner.qq)
        || "";
      if (ownerId && speakers.some((s) => s.id === ownerId)) sel.value = ownerId;
    }
  }
  const pathBox = $("archive-path");
  if (pathBox && !pathBox.value && ov.data_dir) {
    pathBox.placeholder = ov.data_dir;
    pathBox.value = ov.data_dir || "";
  }
  showDiag({ overview: ov });
}

function applyMemoryMode() {
  const isBot = memoryMode === "bot";
  if ($("memory-title")) $("memory-title").textContent = isBot ? "Savage 记忆" : "主人记忆";
  if ($("memory-lede")) {
    $("memory-lede").textContent = isBot
      ? "Bot 自己的记忆（主体是 bot），由 AI 整理聊天时自动写入。"
      : "主人（QQ 或 ChatUI）的指令与自述，经 AI 缩写和审核后写入；这些内容全局可注入。";
  }
  const tools = $("memory-owner-tools");
  if (tools) tools.hidden = isBot;
  const botTools = $("memory-bot-tools");
  if (botTools) botTools.hidden = !isBot;
  document.querySelectorAll('[data-act="memory-switch"]').forEach((b) => {
    const on = b.dataset.mode === memoryMode;
    b.classList.toggle("on", on);
    b.classList.toggle("ghost", !on);
  });
}

async function loadMemory() {
  const isBot = memoryMode === "bot";
  const q = !isBot && $("memory-q") ? $("memory-q").value.trim() : "";
  const data = await apiGet(isBot ? "memory/bot" : "memory", q ? { q } : {});
  memoryCache = data.items || [];
  const box = $("memory-list");
  if (!box) return;
  if (!memoryCache.length) {
    box.innerHTML = isBot
      ? `<p class="lede">Savage 还没有自己的记忆。Bot 在聊天里自述「记住/我喜欢…」并被 AI 整理后会出现。</p>`
      : `<p class="lede">还没有主人记忆。主人（QQ 或 ChatUI）说「记住/我喜欢/以后…」并审核通过后会出现。</p>`;
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

async function loadDiagnostics() {
  const data = await apiGet("diagnostics");
  const items = data.items || [];
  const lines = items.slice(0, 15).map((it) => {
    let payload = "";
    try {
      payload = JSON.stringify(it.payload);
    } catch (err) {
      payload = String(it.payload);
    }
    return `#${it.id} ${it.kind} ${payload.slice(0, 200)}`;
  }).join("\n");
  showDiag(`诊断条目（最近 ${items.length} 条）：\n${lines}\n\n总览：\n${JSON.stringify(data.overview, null, 2)}`);
}

async function loadArchived() {
  const box = $("archive-list");
  if (!box) return;
  const [archived, superseded] = await Promise.all([
    apiGet("facts", { status: "archived" }),
    apiGet("facts", { status: "superseded" }),
  ]);
  const items = [...(archived.items || []), ...(superseded.items || [])]
    .sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0));
  box.innerHTML = items.length
    ? items.slice(0, 120).map((f) => {
        const isSuperseded = f.status === "superseded";
        const reason = isSuperseded ? "被新说法覆盖" : (f.reason || "");
        return `<div class="item">
        <div class="item-head">
          <span class="chip gray">#${esc(f.id)}</span>
          <span class="chip gray">${esc(f.kind || f.attribute)}</span>
          <span class="chip gray">${esc(speakerLabel(f.speaker_name, f.speaker_id))}</span>
          ${isSuperseded ? `<span class="chip warn">已覆盖</span>` : ""}
        </div>
        <div class="plain">${esc(f.plain || f.content || f.value)}</div>
        <div class="meta">${esc(reason)} · ${esc(fmtTime(f.updated_at))}</div>
        <div class="actions">
          <button type="button" class="ghost tiny" data-act="fact-restore" data-id="${esc(f.id)}">恢复</button>
        </div>
      </div>`;
      }).join("")
    : `<p class="lede">回收站是空的。</p>`;
}

function eventStatusChip(e) {
  if (e.review_status === "needs_review") return `<span class="chip warn">待审</span>`;
  if (e.review_status === "manual") return `<span class="chip ok">手动</span>`;
  if (e.status === "archived") return `<span class="chip gray">已归档</span>`;
  return "";
}

function eventParticipants(e) {
  const names = (e.participants || []).map((p) => p.name || p.id).filter(Boolean);
  return names.join("、") || e.speaker_name || e.speaker_id || "";
}

function eventActions(e) {
  const pinLabel = e.pinned ? "取消置顶" : "置顶";
  const approve = e.review_status === "needs_review"
    ? `<button type="button" class="tiny" data-act="event-approve" data-id="${esc(e.id)}">确认写入</button>`
    : "";
  const del = e.status === "archived"
    ? `<button type="button" class="ghost tiny" data-act="event-restore" data-id="${esc(e.id)}">恢复</button>`
    : `<button type="button" class="ghost tiny" data-act="event-del" data-id="${esc(e.id)}">删除</button>`;
  return `<div class="actions">
    ${approve}
    <button type="button" class="ghost tiny" data-act="event-pin" data-id="${esc(e.id)}" data-pinned="${e.pinned ? 1 : 0}">${pinLabel}</button>
    <button type="button" class="ghost tiny" data-act="event-edit" data-id="${esc(e.id)}">编辑</button>
    <button type="button" class="ghost tiny" data-act="event-evidence" data-id="${esc(e.id)}">看原文</button>
    ${del}
  </div>
  <div class="raw" id="event-raw-${esc(e.id)}" hidden></div>`;
}

function eventEditor(e) {
  const highlights = (e.highlights || []).join("\n");
  return `<div class="item on">
    <div class="item-head"><span class="chip">#${esc(e.id)} ${esc(e.kind)}</span><span class="chip gray">${esc(fmtTime(e.start_ts))}</span></div>
    <input data-edit-event-title="${esc(e.id)}" value="${esc(e.title)}" placeholder="标题" />
    <textarea data-edit-event-summary="${esc(e.id)}" placeholder="摘要">${esc(e.summary)}</textarea>
    <textarea data-edit-event-highlights="${esc(e.id)}" placeholder="要点（每行一条）">${esc(highlights)}</textarea>
    <div class="row">
      <label class="lede">重要度
        <input type="number" min="0" max="1" step="0.05" style="width:110px"
          data-edit-event-importance="${esc(e.id)}" value="${esc(e.importance ?? 0)}" />
      </label>
      <span class="lede">当前权重 ${esc(e.weight ?? "-")}；参与人 ${esc(eventParticipants(e))}</span>
    </div>
    <div class="actions">
      <button type="button" data-act="event-save" data-id="${esc(e.id)}">保存</button>
      <button type="button" class="ghost tiny" data-act="event-edit-cancel">取消</button>
    </div>
  </div>`;
}

function eventCard(e) {
  const highlights = (e.highlights || []).map((h) => `<div class="meta">· ${esc(h)}</div>`).join("");
  return `<div class="item">
    <div class="item-head">
      <span class="chip">#${esc(e.id)}</span>
      <span class="chip gray">${esc(e.kind === "talk" ? "聊过" : "经历")}</span>
      ${e.pinned ? `<span class="chip">置顶</span>` : ""}
      ${eventStatusChip(e)}
      <span class="chip gray">${esc(e.scope === "owner" ? "主人" : "档案")}</span>
      ${typeof e.weight === "number" ? `<span class="chip gray">权重 ${esc(e.weight)}</span>` : ""}
    </div>
    <div class="plain">【${esc(e.title)}】${esc(e.summary)}</div>
    ${highlights}
    <div class="meta">${esc(fmtTime(e.start_ts))} · ${esc(eventParticipants(e))} · 来源 ${esc(e.window_tag || "")}</div>
    ${e.keywords && e.keywords.length ? `<div class="meta">${keywordsHtml(e.keywords)}</div>` : ""}
    ${eventActions(e)}
  </div>`;
}

async function loadEvents() {
  const box = $("events-list");
  if (!box) return;
  const q = $("events-q") ? $("events-q").value.trim() : "";
  const data = await apiGet("events", { status: eventsStatus, ...(q ? { q } : {}) });
  const items = data.items || [];
  document.querySelectorAll('[data-act="events-status"]').forEach((b) => {
    const on = b.dataset.status === eventsStatus;
    b.classList.toggle("on", on);
    b.classList.toggle("ghost", !on);
  });
  if (!items.length) {
    box.innerHTML = `<p class="lede">没有事件。聊一段后会自动整理成「一件事」；已经有一段对话时可以点上方「抽取/整理」立即生成。</p>`;
    return;
  }
  box.innerHTML = items.map((e) => (
    editingEventId === e.id ? eventEditor(e) : eventCard(e)
  )).join("");
}

const REVIEW_KIND_LABELS = { jargon: "黑话", fewshot: "表达样本", persona: "人格草稿" };
const REVIEW_STATUS_LABELS = { pending: "待审", approved: "已批准", rejected: "已驳回" };

function reviewItemHtml(r) {
  const payload = r.payload || {};
  let detail = "";
  if (r.kind === "jargon") {
    detail = `<div class="plain">含义：${esc(payload.meaning || "")}</div>`;
  } else if (r.kind === "fewshot") {
    detail = `<div class="plain">用户：${esc(payload.user || "")}</div><div class="plain">Bot：${esc(payload.bot || "")}</div>`;
  } else if (r.kind === "persona") {
    detail = `<div class="plain">${esc(payload.draft || "")}</div>`;
  }
  const statusChip = r.status === "approved" ? "ok" : r.status === "rejected" ? "gray" : "warn";
  const actions = reviewsStatus === "pending"
    ? `<button type="button" class="tiny" data-act="approve" data-id="${esc(r.id)}">批准</button>
       <button type="button" class="ghost tiny" data-act="reject-review" data-id="${esc(r.id)}">驳回</button>`
    : `<button type="button" class="ghost tiny" data-act="review-reopen" data-id="${esc(r.id)}">改回待审</button>`;
  return `<div class="item">
    <div class="item-head">
      ${reviewsStatus === "pending" ? `<input type="checkbox" data-role="review-check" value="${esc(r.id)}" />` : ""}
      <span class="chip ${statusChip}">${esc(REVIEW_KIND_LABELS[r.kind] || r.kind)}</span>
      <b>#${esc(r.id)}</b> <span>${esc(r.title)}</span>
      ${r.quality ? `<span class="chip gray">Q${esc(r.quality)}</span>` : ""}
    </div>
    ${detail}
    <div class="meta">QQ/id ${esc(r.speaker_id || "")} · ${esc(fmtTime(r.updated_at || r.created_at))}</div>
    <div class="actions">${actions}</div>
  </div>`;
}

async function loadReviews() {
  const query = { status: reviewsStatus };
  if (reviewsKind) query.kind = reviewsKind;
  const data = await apiGet("reviews", query);
  const items = data.items || [];
  const box = $("reviews");
  const toolbar = $("reviews-toolbar");
  if (!box) return;
  const tabs = ["pending", "approved", "rejected"].map((s) =>
    `<button type="button" class="${reviewsStatus === s ? "tiny" : "ghost tiny"}" data-act="reviews-status" data-status="${s}">${REVIEW_STATUS_LABELS[s]}</button>`
  ).join("");
  const kinds = [["", "全部"], ["fewshot", "表达样本"], ["jargon", "黑话"], ["persona", "人格草稿"]].map(([k, label]) =>
    `<button type="button" class="${reviewsKind === k ? "tiny" : "ghost tiny"}" data-act="reviews-kind" data-kind="${k}">${label}</button>`
  ).join("");
  const batch = reviewsStatus === "pending"
    ? `<button type="button" class="ghost tiny" data-act="reviews-select-all">全选/取消</button>
       <button type="button" class="tiny" data-act="reviews-batch" data-status="approved">批准选中</button>
       <button type="button" class="ghost tiny" data-act="reviews-batch" data-status="rejected">驳回选中</button>
       <button type="button" class="ghost tiny" data-act="reviews-all" data-status="approved">一键批准全部</button>
       <button type="button" class="ghost tiny" data-act="reviews-all" data-status="rejected">一键驳回全部</button>`
    : "";
  if (toolbar) {
    // 筛选与批量按钮固定在列表上方，滚列表时不会跟着滚走。
    toolbar.innerHTML = `
      <div class="row">${tabs}</div>
      <div class="row">${kinds}</div>
      ${batch ? `<div class="row">${batch}</div>` : ""}
    `;
  }
  const empty = `<p class="lede">没有${REVIEW_STATUS_LABELS[reviewsStatus] || ""}学习项。</p>`;
  box.innerHTML = items.length ? items.map(reviewItemHtml).join("") : empty;
}

const PENDING_REASON_LABELS = {
  joke_or_banter: "玩笑待确认",
  pinned_needs_confirm: "置顶冲突待确认",
  not_first_person_or_correction: "非第一人称/非纠正",
  high_evidence_needs_confirm: "高证据冲突待确认",
  domain_needs_confirm: "疑似同词不同义（通过=存成两条）",
};

const PENDING_REASON_HELP = {
  joke_or_banter: "这句像玩笑或反话，系统不敢自动覆盖。通过＝写入新说法并覆盖旧条；驳回＝丢弃新说法。",
  pinned_needs_confirm: "旧条是置顶记忆，任何冲突都不会自动删除。通过＝新说法生效并删除旧条；驳回＝保留旧条。",
  not_first_person_or_correction: "新说法不是本人第一人称自述，也不是明确纠正。通过＝覆盖旧条；驳回＝保留旧条。",
  high_evidence_needs_confirm: "旧条置信度高且被使用过，系统不敢自动覆盖。通过＝新说法生效并删除旧条；驳回＝保留旧条。",
  domain_needs_confirm: "同一个词疑似不同含义（如美式咖啡 / 美式穿搭），拿不准要不要合并。通过＝两条并存；驳回＝丢弃新说法、保留旧条。",
};

function pendingFactText(v) {
  return v.plain || v.content || v.value || "（无内容）";
}

function pendingFactMeta(f) {
  if (!f) return "旧条已不存在（可能已被其他改动处理）";
  return [
    speakerLabel(f.speaker_name, f.speaker_id),
    f.pinned ? "置顶" : "",
    f.attribute ? `槽位 ${f.attribute}${f.topic ? "/" + f.topic : ""}` : "",
    `置信度 ${f.confidence}`,
    `访问 ${f.access_count ?? 0} 次`,
    f.updated_at ? `更新于 ${fmtTime(f.updated_at)}` : "",
  ].filter(Boolean).join(" · ");
}

function pendingNewMeta(n) {
  const evidence = (n.evidence || []).map((x) => `#${x}`).join(" ");
  return [
    speakerLabel(n.speaker_name, n.speaker_id),
    n.attribute ? `槽位 ${n.attribute}${n.topic ? "/" + n.topic : ""}` : "",
    n.window_tag ? `来源 ${n.window_tag}` : "",
    evidence ? `证据消息 ${evidence}` : "",
    n.explicit_correction ? "明确纠正" : "",
    n.first_person ? "第一人称" : "",
  ].filter(Boolean).join(" · ");
}

async function loadPending() {
  const data = await apiGet("pending");
  const items = data.items || [];
  const box = $("pending");
  if (!box) return;
  box.innerHTML = items.length
    ? items.map((p) => {
        const reasonLabel = PENDING_REASON_LABELS[p.reason] || p.reason || "待确认";
        const help = PENDING_REASON_HELP[p.reason] || "通过＝新说法生效；驳回＝保留旧条。";
        const old = p.old_fact;
        const nw = p.new_fact || {};
        const confirmLabel = p.reason === "domain_needs_confirm" ? "通过（存成两条）" : "通过（新覆盖旧）";
        return `<div class="item">
        <div class="item-head">
          <span class="chip">#${esc(p.id)} 待确认覆盖</span>
          <span class="chip warn">${esc(reasonLabel)}</span>
          <span class="chip gray">${esc(fmtTime(p.created_at))}</span>
        </div>
        <div class="compare">
          <div class="compare-row old">
            <span class="compare-tag">旧</span>
            <div class="compare-body">
              <div class="plain">${old ? esc(pendingFactText(old)) : "<em>旧条已不存在</em>"}</div>
              <div class="meta">${esc(pendingFactMeta(old))}</div>
            </div>
          </div>
          <div class="compare-row new">
            <span class="compare-tag">新</span>
            <div class="compare-body">
              <div class="plain">${esc(pendingFactText(nw))}</div>
              <div class="meta">${esc(pendingNewMeta(nw))}</div>
            </div>
          </div>
        </div>
        <div class="meta">${esc(help)}</div>
        <div class="actions">
          <button type="button" class="tiny" data-act="pending-confirm" data-id="${esc(p.id)}">${confirmLabel}</button>
          <button type="button" class="ghost tiny" data-act="pending-reject" data-id="${esc(p.id)}">驳回（保留旧条）</button>
        </div>
      </div>`;
      }).join("")
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
        const core = p.core || [];
        const related = p.related || [];
        const blocked = p.blocked || [];
        const reasons = {};
        blocked.forEach((b) => {
          const key = b.reason || "?";
          reasons[key] = (reasons[key] || 0) + 1;
        });
        const reasonText = Object.entries(reasons).map(([k, n]) => `${k}×${n}`).join("、");
        return `<div class="item">
          <div class="item-head">
            <span class="chip">${esc(p.route)}</span>
            <span class="chip gray">${esc(speakerLabel("", p.speaker_id))}</span>
            <span class="chip gray">dedup ${esc(p.dedup ?? 0)}</span>
          </div>
          <div>${esc(p.query || "")}</div>
          <div class="meta">chars=${esc(p.pack_chars)}${p.pack_tokens ? `（≈${esc(p.pack_tokens)} tok）` : ""} · core ${core.length} 条 [${esc(core.join(","))}] · related ${related.length} 条 [${esc(related.join(","))}]${reasonText ? ` · 过滤 ${esc(reasonText)}` : ""} · ${esc(fmtTime(it.ts))}</div>
          ${p.window ? `<div class="meta">窗口 …${esc(String(p.window).slice(-30))}</div>` : ""}
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

const SETTING_GROUPS = [
  { title: "总开关与采集", keys: ["enabled", "capture_enabled", "inject_enabled", "owner_qq", "notify_umo", "memory_source_platforms", "memory_whitelist"] },
  { title: "抽取与整理（AI 管线）", keys: ["extract_enabled", "pipeline_enabled", "summary_provider_id", "normalize_provider_id", "verify_provider_id", "extract_min_messages", "extract_cooldown_seconds", "extract_fail_cooldown_seconds", "extract_idle_seconds", "pipeline_max_revisions", "pipeline_batch_size", "pipeline_notify_cooldown_seconds"] },
  { title: "模型档位与 Token 预算", keys: ["quality_provider_id", "fast_provider_id", "fallback_provider_id", "daily_token_limit", "soft_token_limit", "single_call_token_cap"] },
  { title: "检索与注入", keys: ["retrieval_mode", "retrieval_bm25", "provider_timeout_seconds", "inject_budget_chars", "inject_warm_triggered", "inject_novelty_filter", "top_k", "core_fact_limit", "related_fact_limit", "inject_dedup_window_seconds", "cache_ttl_seconds", "high_evidence_confidence", "debug_log_injection", "coexistence_degrade"] },
  { title: "重要性与维护", keys: ["importance_weight", "importance_half_life_days", "importance_reinforce_factor", "importance_max_half_life_multiplier", "importance_prune_threshold", "sleep_timeline_retain_days", "sleep_low_value_days", "sleep_low_value_confidence", "sleep_superseded_retain_days", "empty_profile_ttl_days"] },
  { title: "学习与人格草稿", keys: ["learning_enabled", "jargon_enabled", "jargon_scope", "jargon_min_count", "jargon_cooldown_seconds", "fewshot_enabled", "persona_draft_enabled", "persona_draft_min_fewshots", "persona_draft_cooldown_seconds", "persona_draft_ttl_seconds", "inject_jargon_limit", "inject_fewshot_limit"] },
  { title: "事件记忆与隐私", keys: ["event_enabled", "event_gap_minutes", "event_max_hours", "event_min_messages", "event_merge_minutes", "event_max_per_run", "event_provider_id", "event_budget_chars", "event_max_inject", "event_archive_days", "memory_session_isolation", "entity_linking_enabled", "entity_boost_weight", "history_enabled", "history_max_facts"] },
  { title: "图片", keys: ["image_caption_provider_id", "image_caption_timeout_seconds"] },
  { title: "Embedding 与 Rerank", keys: ["embedding_enabled", "embedding_auto_threshold", "embedding_provider_id", "rerank_provider_id"] },
];

const SETTINGS_APPEARANCE = "外观";
const SETTINGS_HANDLED_ELSEWHERE = new Set([
  "ui_dynamic_colors",
  "ui_theme_color",
  "ui_theme_color2",
  "ui_theme_color3",
]);

function groupSchema(schema) {
  const used = new Set();
  const groups = SETTING_GROUPS.map((g) => {
    const entries = g.keys.filter((k) => schema[k]).map((k) => [k, schema[k]]);
    entries.forEach(([k]) => used.add(k));
    return { title: g.title, entries };
  }).filter((g) => g.entries.length);
  const rest = Object.entries(schema).filter(
    ([k]) => !used.has(k) && !SETTINGS_HANDLED_ELSEWHERE.has(k)
  );
  if (rest.length) groups.push({ title: "其他", entries: rest });
  return groups;
}

function applySettingsGroup(key, persist = true) {
  settingsGroup = key;
  if (persist) {
    try {
      localStorage.setItem("stype.settings.group", key);
    } catch (err) {
      /* ignore storage errors */
    }
  }
  document.querySelectorAll("#page-settings .settings-panel").forEach((el) => {
    el.classList.toggle("on", el.dataset.group === key);
  });
  document.querySelectorAll('#settings-nav [data-act="settings-group"]').forEach((el) => {
    const on = el.dataset.group === key;
    el.classList.toggle("on", on);
    el.classList.toggle("ghost", !on);
  });
}

async function loadSettings() {
  const [data, providers] = await Promise.all([apiGet("config"), apiGet("providers")]);
  const schema = data.schema || {};
  const values = data.values || {};
  renderThemeControls(values.ui_theme_color, values.ui_theme_color2, values.ui_theme_color3);
  const groups = groupSchema(schema);
  const items = groups.map((g) => ({ key: g.title, title: g.title, count: g.entries.length }));
  items.push({ key: SETTINGS_APPEARANCE, title: SETTINGS_APPEARANCE, count: 0 });
  if (!items.some((item) => item.key === settingsGroup)) {
    settingsGroup = items[0].key;
  }
  $("settings-nav").innerHTML = items
    .map(
      (item) => `<button type="button" class="settings-nav-item" data-act="settings-group" data-group="${esc(item.key)}">
        <span class="settings-nav-title">${esc(item.title)}</span>
        ${item.count ? `<span class="chip gray">${item.count}</span>` : ""}
      </button>`
    )
    .join("");
  $("settings-form").innerHTML = groups
    .map(
      (g) => `<section class="card settings-panel" data-group="${esc(g.title)}">
        <h2>${esc(g.title)}</h2>
        <div class="settings">${g.entries.map(([key, spec]) => fieldControl(key, spec, values[key], providers)).join("")}</div>
      </section>`
    )
    .join("");
  applySettingsGroup(settingsGroup, false);
}

async function saveTheme(color, color2, color3) {
  const r = await apiPost("ui/theme", { color, color2, color3 });
  applyTheme(r.color, r.color2, r.color3);
  renderThemeControls(r.color, r.color2, r.color3);
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
  await loadEvents();
  await loadProfiles();
  await loadReviews();
  await loadPending();
  await loadMicroscope();
  await loadArchived();
  try {
    await loadDiagnostics();
  } catch (err) {
    showDiag(String(err));
  }
}

function showTab(name) {
  ["memory", "events", "profiles", "diag", "settings"].forEach((key) => {
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
  if (name === "events") run("已刷新事件", loadEvents);
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

async function safe(fn) {
  try {
    return await fn();
  } catch (err) {
    showDiag(String(err));
    return null;
  }
}

async function saveFactEdit(el) {
  const id = Number(el.dataset.id);
  const where = el.dataset.where || "memory";
  const area = document.querySelector(`textarea[data-edit-fact="${id}"]`);
  const plain = area ? area.value.trim() : "";
  if (!plain) return { ok: false, error: "empty" };
  const body = { id, plain };
  const imp = document.querySelector(`input[data-edit-importance="${id}"]`);
  if (imp && imp.value !== "") {
    const n = Number(imp.value);
    if (Number.isFinite(n)) body.importance = n;
  }
  const r = await apiPost("facts/update", body);
  editingFactId = 0;
  if (where === "profile") await loadProfile(currentProfileId);
  else await loadMemory();
  return r;
}

async function saveEventEdit(el) {
  const id = Number(el.dataset.id);
  const titleEl = document.querySelector(`input[data-edit-event-title="${id}"]`);
  const summaryEl = document.querySelector(`textarea[data-edit-event-summary="${id}"]`);
  const highlightsEl = document.querySelector(`textarea[data-edit-event-highlights="${id}"]`);
  const importanceEl = document.querySelector(`input[data-edit-event-importance="${id}"]`);
  const body = { id, title: titleEl ? titleEl.value.trim() : "" };
  if (!body.title) return { ok: false, error: "empty" };
  if (summaryEl) body.summary = summaryEl.value.trim();
  if (highlightsEl) body.highlights = highlightsEl.value;
  if (importanceEl && importanceEl.value !== "") body.importance = Number(importanceEl.value);
  const r = await apiPost("events/update", body);
  editingEventId = 0;
  await loadEvents();
  return r;
}

async function onAct(act, el) {
  if (act === "tab") return showTab(el.dataset.tab);
  if (act === "events-refresh" || act === "events-search") return run("已刷新事件", loadEvents);
  if (act === "events-status") return safe(async () => {
    eventsStatus = el.dataset.status || "live";
    await loadEvents();
  });
  if (act === "event-pin") return run("已更新", async () => {
    const r = await apiPost("events/pin", {
      id: Number(el.dataset.id),
      pinned: el.dataset.pinned !== "1",
    });
    await loadEvents();
    return r;
  });
  if (act === "event-approve") return run("已确认写入", async () => {
    const r = await apiPost("events/update", { id: Number(el.dataset.id), approve: true });
    await loadEvents(); await loadOverview();
    return r;
  });
  if (act === "event-edit") {
    return safe(async () => {
      editingEventId = Number(el.dataset.id);
      await loadEvents();
    });
  }
  if (act === "event-edit-cancel") {
    return safe(async () => {
      editingEventId = 0;
      await loadEvents();
    });
  }
  if (act === "event-save") return run("已保存", () => saveEventEdit(el));
  if (act === "event-del") return run("已删除", async () => {
    await apiPost("events/archive", { ids: [Number(el.dataset.id)] });
    await loadEvents(); await loadOverview();
  });
  if (act === "event-restore") return run("已恢复", async () => {
    await apiPost("events/restore", { ids: [Number(el.dataset.id)] });
    await loadEvents(); await loadOverview();
  });
  if (act === "event-evidence") return safe(async () => {
    const id = Number(el.dataset.id);
    const box = $(`event-raw-${id}`);
    const data = await apiGet("events/evidence", { id });
    const items = data.items || [];
    if (box) {
      box.hidden = false;
      box.innerHTML = items.length
        ? items.map((m) => `[${esc(fmtTime(m.ts))}] ${esc(m.speaker)}(${esc(m.role)}): ${esc(m.content)}`).join("\n")
        : "原文已被时间线清理。";
    }
  });
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
  if (act === "memory-switch") return safe(async () => {
    memoryMode = el.dataset.mode === "bot" ? "bot" : "owner";
    applyMemoryMode();
    await loadMemory();
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
  if (act === "memory-edit") {
    return safe(async () => {
      editingReviewId = Number(el.dataset.id);
      await loadMemoryPending();
    });
  }
  if (act === "memory-edit-cancel") {
    return safe(async () => {
      editingReviewId = 0;
      await loadMemoryPending();
    });
  }
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
  if (act === "fact-pin") return run("已更新", async () => {
    const r = await apiPost("facts/pin", {
      id: Number(el.dataset.id),
      pinned: el.dataset.pinned !== "1",
    });
    if (el.dataset.where === "profile") await loadProfile(currentProfileId);
    else await loadMemory();
    return r;
  });
  if (act === "fact-restore") return run("已恢复", async () => {
    const r = await apiPost("facts/restore", { ids: [Number(el.dataset.id)] });
    await reload();
    if (r && (r.blocked || []).length) toast("槽位已被占用，恢复被阻止");
    return r;
  });
  if (act === "fact-edit") {
    return safe(async () => {
      editingFactId = Number(el.dataset.id);
      editingFactWhere = el.dataset.where || "memory";
      if (editingFactWhere === "profile") await loadProfile(currentProfileId);
      else await loadMemory();
    });
  }
  if (act === "fact-edit-cancel") {
    return safe(async () => {
      editingFactId = 0;
      if (editingFactWhere === "profile") await loadProfile(currentProfileId);
      else await loadMemory();
    });
  }
  if (act === "fact-edit-save") return run("已保存", () => saveFactEdit(el));
  if (act === "fact-entities") return safe(async () => {
    const id = Number(el.dataset.id);
    const box = $(`fact-entities-${id}`);
    const data = await apiGet("entities", { ref: "fact", id });
    const items = data.items || [];
    if (box) {
      box.hidden = false;
      box.innerHTML = items.length
        ? items.map((x) => `<span class="chip gray">${esc(x.kind)} ${esc(x.name)}</span>`).join(" ")
        : "没有登记到实体（关键词为空时不会登记）。";
    }
  });
  if (act === "fact-del") return run("已删除", async () => {
    await apiPost("facts/archive", { ids: [Number(el.dataset.id)] });
    if (el.dataset.where === "profile") await loadProfile(currentProfileId);
    else await loadMemory();
    await loadOverview();
    await loadArchived();
  });
  if (act === "fact-rollback") return run("已回滚改口", async () => {
    const r = await apiPost("rollback", { id: Number(el.dataset.id) });
    if (el.dataset.where === "profile") await loadProfile(currentProfileId);
    else await loadMemory();
    await loadOverview();
    await loadArchived();
    return r;
  });
  if (act === "remember-bot") return run("已写入", async () => {
    const content = $("remember-bot-text").value.trim();
    if (!content) return { ok: false, error: "empty" };
    const r = await apiPost("remember", {
      content,
      speaker_id: "bot_self",
      speaker_name: "bot",
      subject: "bot",
    });
    $("remember-bot-text").value = "";
    await loadMemory(); await loadOverview();
    return r;
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
  if (act === "alias") return run("已归并", async () => {
    const r = await apiPost("aliases/set", { alias: el.dataset.alias, canonical_id: el.dataset.canonical });
    await reload();
    if (r && (r.moved || 0) > 0) toast(`已迁移 ${r.moved} 条事实`);
    return r;
  });
  if (act === "pending-confirm") return run("已确认覆盖", async () => { await apiPost("pending/confirm", { id: Number(el.dataset.id) }); await reload(); });
  if (act === "pending-reject") return run("已驳回", async () => { await apiPost("pending/reject", { id: Number(el.dataset.id) }); await reload(); });
  if (act === "approve") return run("已批准", async () => { await apiPost("reviews/set", { id: Number(el.dataset.id), status: "approved" }); await loadReviews(); });
  if (act === "reject-review") return run("已驳回", async () => { await apiPost("reviews/set", { id: Number(el.dataset.id), status: "rejected" }); await loadReviews(); });
  if (act === "reviews-status") return safe(async () => { reviewsStatus = el.dataset.status || "pending"; await loadReviews(); });
  if (act === "reviews-kind") return safe(async () => { reviewsKind = el.dataset.kind || ""; await loadReviews(); });
  if (act === "reviews-select-all") return safe(async () => {
    const boxes = Array.from(document.querySelectorAll('#reviews [data-role="review-check"]'));
    const allOn = boxes.length > 0 && boxes.every((b) => b.checked);
    boxes.forEach((b) => { b.checked = !allOn; });
  });
  if (act === "reviews-batch") return run("已处理", async () => {
    const ids = Array.from(document.querySelectorAll('#reviews [data-role="review-check"]:checked')).map((b) => Number(b.value));
    if (!ids.length) return { ok: false, error: "none_selected", message: "没有勾选条目" };
    const r = await apiPost("reviews/set", { ids, status: el.dataset.status });
    await loadReviews();
    return r;
  });
  if (act === "reviews-all") return run("已处理", async () => {
    const ids = Array.from(document.querySelectorAll('#reviews [data-role="review-check"]')).map((b) => Number(b.value)).filter(Boolean);
    if (!ids.length) return { ok: false, error: "none", message: "当前没有待审条目" };
    const r = await apiPost("reviews/set", { ids, status: el.dataset.status });
    await loadReviews();
    return r;
  });
  if (act === "review-reopen") return run("已恢复待审", async () => { await apiPost("reviews/set", { id: Number(el.dataset.id), status: "pending" }); await loadReviews(); });
  if (act === "archive-preview") return run("已预览", () => apiPost("archive/preview", { path: $("archive-path").value.trim() }));
  if (act === "archive-import") return run("已导入档案", async () => { const r = await apiPost("archive/import", { path: $("archive-path").value.trim() }); await reload(); return r; });
  if (act === "chat-preview") return run("已预览聊天", () => apiPost("chat/preview", { text: $("chat-text").value, user_names: $("chat-users").value, bot_names: $("chat-bots").value }));
  if (act === "chat-import") return run("已导入聊天", async () => { const r = await apiPost("chat/import", { text: $("chat-text").value, user_names: $("chat-users").value, bot_names: $("chat-bots").value }); await reload(); return r; });
  if (act === "reset") return run("已清空重建", async () => {
    const r = await apiPost("reset", { confirm: "reset" });
    await reload();
    return r;
  });
  if (act === "theme-preset") return run("主题已切换", () => {
    if (dynamicOn) stopDynamic(true);
    return saveTheme(el.dataset.a, el.dataset.b, el.dataset.c);
  });
  if (act === "theme-apply") {
    if (dynamicOn) stopDynamic(true);
    return run("主题已切换", () =>
      saveTheme($("theme-color").value, $("theme-color2").value, $("theme-color3").value)
    );
  }
  if (act === "dynamic-toggle") return run("已切换", async () => {
    if (PREFERS_REDUCED) {
      toast("系统已开启「减少动态效果」，动态颜色不可用");
      return { ok: false, error: "reduce_motion" };
    }
    const next = !dynamicOn;
    if (next) startDynamic();
    else stopDynamic(false);
    return apiPost("ui/dynamic", { enabled: next });
  });
  if (act === "settings-group") return safe(() => applySettingsGroup(el.dataset.group || ""));
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
  Promise.resolve(onAct(el.dataset.act, el)).catch((err) => showDiag(String(err)));
});

async function boot() {
  try {
    shaderControls = initShaderGradient({
      color1: themeState.color,
      color2: themeState.color2,
      color3: themeState.color3,
    });
  } catch (err) {
    shaderControls = null;
    console.error("shader background failed:", err);
  }
  if (window.AstrBotPluginPage?.ready) {
    try { await window.AstrBotPluginPage.ready(); } catch (err) { showDiag(err); }
  }
  await run("已刷新", reload);
}

boot();
