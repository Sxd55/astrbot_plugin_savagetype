/**
 * Panel UI test: structure, layout rules and real button handlers.
 *
 * Runs the actual pages/console/app.js against jsdom with a stubbed
 * AstrBotPluginPage bridge, then clicks the review buttons and asserts what
 * got re-rendered / posted. Catches the class of bug where controls live
 * inside a scrolling list, cards stretch and leave blank space, etc.
 *
 * Usage (needs jsdom, e.g. npm i jsdom in a scratch dir):
 *
 *   NODE_PATH=<dir>/node_modules node tests/test_panel.mjs
 *   # or
 *   JSDOM_DIR=<dir> node tests/test_panel.mjs
 *
 * Exit code 0 = all checks passed.
 */

import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);

function loadJSDOM() {
  try {
    return require("jsdom").JSDOM;
  } catch (err) {
    const dir = process.env.JSDOM_DIR;
    if (!dir) {
      console.error("jsdom not found. Install it and set NODE_PATH or JSDOM_DIR.");
      throw err;
    }
    return require(join(dir, "node_modules", "jsdom")).JSDOM;
  }
}

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));
const PANEL = join(ROOT, "pages", "console");
const JSDOM = loadJSDOM();

const state = {
  reviewItems: {
    pending: [
      {
        id: 182, kind: "persona", status: "pending", title: "人格草稿 182",
        payload: { draft: "认真记录下了" }, speaker_id: "u1", quality: 75,
        created_at: 1, updated_at: 2,
      },
      {
        id: 184, kind: "fewshot", status: "pending", title: "样本 184",
        payload: { user: "你好", bot: "在的" }, speaker_id: "u1",
        created_at: 3, updated_at: 4,
      },
    ],
    approved: [
      {
        id: 9, kind: "jargon", status: "approved", title: "黑话 9",
        payload: { meaning: "意思" }, speaker_id: "u1", created_at: 5, updated_at: 6,
      },
    ],
    rejected: [],
  },
  gets: [],
  posts: [],
  schema: {},
  values: {},
};

async function apiGet(route, query = {}) {
  const q = query || {};
  state.gets.push({ route, query: q });
  if (route === "reviews") {
    const items = (state.reviewItems[q.status] || []).filter(
      (item) => !q.kind || item.kind === q.kind
    );
    return { items };
  }
  if (route === "overview") {
    return {
      counts: {}, coexistence: { reasons: [] }, owner: {}, config: {},
      alias_suggestions: [], speakers: [],
      tokens: {
        used: 1234, hard_limit: 100000, soft_limit: 50000,
        by_task: [
          { task: "normalize", tokens: 900, skipped: 0, source: "tier:quality" },
          { task: "learn", tokens: 0, skipped: 2, source: "tier:fast" },
        ],
      },
    };
  }
  if (route === "diagnostics") return { items: [], overview: {} };
  if (route === "config") return { schema: state.schema, values: state.values };
  if (route === "providers") return { chat: [], embedding: [], rerank: [] };
  return { items: [] };
}

async function apiPost(route, body = {}) {
  state.posts.push({ route, body });
  if (route === "reviews/set") {
    const ids = body.ids || (body.id ? [body.id] : []);
    return { ok: true, count: ids.length, results: ids };
  }
  return { ok: true };
}

const html = readFileSync(join(PANEL, "index.html"), "utf8")
  .replace(
    /<link rel="stylesheet" href="\.\/style\.css"[^>]*>/,
    `<style>${readFileSync(join(PANEL, "style.css"), "utf8")}</style>`
  )
  .replace(/<script type="module" src="\.\/app\.js"><\/script>/, "");

const dom = new JSDOM(html, {
  runScripts: "dangerously",
  url: "http://localhost/",
  pretendToBeVisual: true,
});
const { window } = dom;
const { document } = window;

window.AstrBotPluginPage = { apiGet, apiPost, ready: async () => {} };
if (!window.matchMedia) {
  window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
}

const appSrc = readFileSync(join(PANEL, "app.js"), "utf8")
  .replace(
    /import \{ initShaderGradient \} from "\.\/shader\.js";\s*/,
    "const initShaderGradient = () => null;\n"
  );
window.eval(appSrc);

const $ = (id) => document.getElementById(id);
const tick = (ms = 30) => new Promise((resolve) => setTimeout(resolve, ms));
/** jsdom objects live in another realm: JSON round-trip before deep compare. */
const plain = (value) => JSON.parse(JSON.stringify(value));
const click = (el) => {
  assert.ok(el, "click target must exist");
  el.dispatchEvent(new window.MouseEvent("click", { bubbles: true, cancelable: true }));
};

async function waitFor(predicate, label, timeout = 3000) {
  const started = Date.now();
  while (Date.now() - started < timeout) {
    if (predicate()) return;
    await tick(20);
  }
  throw new Error(`timeout waiting for: ${label}`);
}

const results = [];
async function check(name, fn) {
  try {
    await fn();
    results.push([true, name]);
    console.log(`ok   ${name}`);
  } catch (err) {
    results.push([false, name]);
    console.log(`FAIL ${name}\n     ${err.message}`);
  }
}

function reviewButtons(act) {
  return [...$("reviews-toolbar").querySelectorAll(`[data-act="${act}"]`)];
}

await waitFor(() => reviewButtons("reviews-status").length > 0, "initial review load");

await check("筛选/批量按钮在列表容器之外（滚动不会滚走）", () => {
  const toolbar = $("reviews-toolbar");
  const list = $("reviews");
  assert.ok(toolbar.contains(reviewButtons("reviews-status")[0]), "status tab inside toolbar");
  assert.ok(toolbar.contains(reviewButtons("reviews-all")[0]), "batch button inside toolbar");
  assert.equal(list.querySelector('[data-act="reviews-status"]'), null, "tab must not be in list");
  assert.equal(list.querySelector('[data-act="reviews-all"]'), null, "batch must not be in list");
  assert.ok(!list.contains(toolbar), "toolbar must not sit inside scroll list");
  assert.ok(toolbar.closest(".scroll") === null, "toolbar must not be scrollable");
});

await check("列表容器可滚动、撑满卡片剩余高度、工具条不滚动", () => {
  const listStyle = window.getComputedStyle($("reviews"));
  assert.equal(listStyle.overflow, "auto");
  assert.equal(listStyle.maxHeight, "none", "等高卡片里列表要能撑满，不能再封顶");
  assert.equal(listStyle.flexGrow, "1");
  assert.equal(listStyle.minHeight, "140px");
  const toolbar = $("reviews-toolbar");
  assert.ok(toolbar.closest(".scroll") === null, "toolbar must not be scrollable");
});

await check("同一排卡片等高对齐（学习审查与主人记忆底部齐平）", () => {
  const card = $("reviews-toolbar").closest("section.card");
  const grid = card.parentElement;
  const align = window.getComputedStyle(grid).alignItems;
  assert.ok(align !== "start", `grid must stretch, got ${align}`);
  assert.equal(window.getComputedStyle(card).maxHeight, "720px", "顶排卡片统一封顶");
  const memoryCard = grid.querySelector("section.card");
  assert.equal(
    window.getComputedStyle(memoryCard).maxHeight,
    window.getComputedStyle(card).maxHeight,
    "两张卡的上限必须一致，底部才会齐平"
  );
});

await check("底排三卡有最小高度、列表同样自适应", () => {
  const trio = document.querySelector(".grid.three");
  assert.ok(trio, "bottom row exists");
  const card = trio.querySelector("section.card");
  const cardStyle = window.getComputedStyle(card);
  assert.equal(cardStyle.minHeight, "280px");
  assert.equal(cardStyle.maxHeight, "720px");
  const list = card.querySelector(".scroll");
  assert.equal(window.getComputedStyle(list).flexGrow, "1");
});

await check("非等高排的独立列表保留自己的高度上限（不被撑成巨长）", () => {
  const events = $("events-list");
  assert.equal(window.getComputedStyle(events).maxHeight, "460px");
  const archive = $("archive-list");
  assert.equal(window.getComputedStyle(archive).maxHeight, "240px");
  const diag = $("diag");
  assert.equal(window.getComputedStyle(diag).maxHeight, "460px");
  const profiles = $("profile-list");
  assert.equal(window.getComputedStyle(profiles).maxHeight, "620px");
});

await check("空态提示在列表区居中显示", async () => {
  state.reviewItems.pending = [];
  click(reviewButtons("reviews-status").find((b) => b.dataset.status === "pending"));
  await waitFor(
    () => /没有待审学习项/.test($("reviews").textContent),
    "empty state rendered"
  );
  const hint = $("reviews").querySelector(".lede");
  assert.ok(hint, "empty hint exists");
  const style = window.getComputedStyle(hint);
  assert.equal(style.display, "flex");
  assert.equal(style.alignItems, "center");
  state.reviewItems.pending = [
    {
      id: 182, kind: "persona", status: "pending", title: "人格草稿 182",
      payload: { draft: "认真记录下了" }, speaker_id: "u1", quality: 75,
      created_at: 1, updated_at: 2,
    },
    {
      id: 184, kind: "fewshot", status: "pending", title: "样本 184",
      payload: { user: "你好", bot: "在的" }, speaker_id: "u1",
      created_at: 3, updated_at: 4,
    },
  ];
  click(reviewButtons("reviews-status").find((b) => b.dataset.status === "pending"));
  await waitFor(() => $("reviews").querySelectorAll(".item").length === 2, "items back");
});

await check("待审列表渲染条目与复选框，且只有列表在滚动区", () => {
  const list = $("reviews");
  assert.equal(list.querySelectorAll(".item").length, 2);
  assert.equal(list.querySelectorAll('input[data-role="review-check"]').length, 2);
});

await check("切到已批准：重新查询、列表只剩已批准、批量按钮隐藏", async () => {
  click(reviewButtons("reviews-status").find((b) => b.dataset.status === "approved"));
  await tick();
  const last = state.gets.filter((g) => g.route === "reviews").pop();
  assert.equal(last.query.status, "approved");
  assert.equal($("reviews").querySelectorAll(".item").length, 1);
  assert.match($("reviews").textContent, /黑话 9/);
  assert.equal(reviewButtons("reviews-all").length, 0, "批量按钮只对待审显示");
});

await check("类型筛选带 kind 参数", async () => {
  click(reviewButtons("reviews-kind").find((b) => b.dataset.kind === "jargon"));
  await tick();
  const last = state.gets.filter((g) => g.route === "reviews").pop();
  assert.equal(last.query.kind, "jargon");
  assert.equal(last.query.status, "approved");
});

await check("回到待审：批量按钮回归", async () => {
  click(reviewButtons("reviews-status").find((b) => b.dataset.status === "pending"));
  await waitFor(() => reviewButtons("reviews-all").length === 2, "batch buttons back");
  assert.equal(reviewButtons("reviews-batch").length, 2);
  click(reviewButtons("reviews-kind")[0]); // 回到「全部」，后面的用例才能看到条目
  await waitFor(
    () => $("reviews").querySelectorAll(".item").length === 2,
    "items visible again"
  );
});

await check("全选/取消：勾选状态切换", async () => {
  const boxes = () => [...$("reviews").querySelectorAll('input[data-role="review-check"]')];
  click(reviewButtons("reviews-select-all")[0]);
  await waitFor(() => boxes().every((b) => b.checked), "all selected");
  click(reviewButtons("reviews-select-all")[0]);
  await waitFor(() => boxes().every((b) => !b.checked), "all cleared");
});

await check("批准选中：只提交勾选的 id 并刷新列表", async () => {
  const boxes = [...$("reviews").querySelectorAll('input[data-role="review-check"]')];
  boxes[0].checked = true;
  const before = state.gets.filter((g) => g.route === "reviews").length;
  click(reviewButtons("reviews-batch").find((b) => b.dataset.status === "approved"));
  await waitFor(
    () => state.posts.some((p) => p.route === "reviews/set"),
    "batch post sent"
  );
  const post = state.posts.filter((p) => p.route === "reviews/set").pop();
  assert.deepEqual(plain(post.body), { ids: [182], status: "approved" });
  await waitFor(
    () => state.gets.filter((g) => g.route === "reviews").length > before,
    "list reloaded after batch"
  );
});

await check("一键驳回全部：提交当前列表全部 id", async () => {
  const before = state.posts.length;
  click(reviewButtons("reviews-all").find((b) => b.dataset.status === "rejected"));
  await waitFor(() => state.posts.length > before, "reject-all post sent");
  const post = state.posts.filter((p) => p.route === "reviews/set").pop();
  assert.deepEqual(plain(post.body), { ids: [182, 184], status: "rejected" });
});

await check("单项批准/驳回按钮仍然可用", async () => {
  state.reviewItems.pending = [
    {
      id: 200, kind: "jargon", status: "pending", title: "词 200",
      payload: { meaning: "含义" }, speaker_id: "u1", created_at: 7, updated_at: 8,
    },
  ];
  click(reviewButtons("reviews-status").find((b) => b.dataset.status === "pending"));
  await waitFor(() => /词 200/.test($("reviews").textContent), "new item rendered");
  click($("reviews").querySelector('[data-act="approve"]'));
  await waitFor(() => state.posts.length > 0, "approve post sent");
  const post = state.posts.filter((p) => p.route === "reviews/set").pop();
  assert.deepEqual(plain(post.body), { id: 200, status: "approved" });
  click($("reviews").querySelector('[data-act="reject-review"]'));
  await tick();
  const last = state.posts.filter((p) => p.route === "reviews/set").pop();
  assert.deepEqual(plain(last.body), { id: 200, status: "rejected" });
});

await check("其他页面元素未被破坏（tab 切换可用）", async () => {
  const eventsTab = [...document.querySelectorAll('.tabs button')].find(
    (b) => b.dataset.tab === "events"
  );
  click(eventsTab);
  await tick();
  assert.equal($("page-events").hidden, false);
  assert.equal($("page-memory").hidden, true);
  click([...document.querySelectorAll('.tabs button')].find((b) => b.dataset.tab === "memory"));
  await tick();
  assert.equal($("page-memory").hidden, false);
});

await check("模型用量行：显示消耗、限额与按任务拆解", () => {
  const line = $("usage-line");
  assert.ok(line, "usage line exists");
  assert.match(line.textContent, /今日模型用量 1234 tokens/);
  assert.match(line.textContent, /硬限 100000 \/ 软限 50000/);
  assert.match(line.textContent, /预算跳过 2 次/);
  assert.match(line.textContent, /缩写 900/, "任务名应翻译成中文标签");
});

await check("设置页出现「模型档位与 Token 预算」分组与三个 Provider 下拉", async () => {
  state.schema = {
    quality_provider_id: { description: "精准档模型", type: "string", default: "" },
    fast_provider_id: { description: "快速档模型", type: "string", default: "" },
    fallback_provider_id: { description: "备用模型", type: "string", default: "" },
    daily_token_limit: { description: "每日 Token 硬限额", type: "int", default: 0 },
    soft_token_limit: { description: "每日 Token 软限额", type: "int", default: 0 },
    single_call_token_cap: { description: "单次上限", type: "int", default: 0 },
  };
  state.values = {
    quality_provider_id: "", fast_provider_id: "", fallback_provider_id: "",
    daily_token_limit: 0, soft_token_limit: 0, single_call_token_cap: 0,
  };
  click([...document.querySelectorAll(".tabs button")].find((b) => b.dataset.tab === "settings"));
  await waitFor(
    () => document.querySelector('#settings-nav [data-group="模型档位与 Token 预算"]'),
    "settings group rendered"
  );
  const group = document.querySelector('#settings-nav [data-group="模型档位与 Token 预算"]');
  assert.match(group.textContent, /6/);
  const panel = document.querySelector('.settings-panel[data-group="模型档位与 Token 预算"]');
  assert.ok(panel, "settings panel exists");
  for (const key of ["quality_provider_id", "fast_provider_id", "fallback_provider_id"]) {
    const field = panel.querySelector(`#cfg-${key}`);
    assert.ok(field, `${key} field`);
    assert.equal(field.tagName, "SELECT", `${key} should render as provider dropdown`);
  }
  for (const key of ["daily_token_limit", "soft_token_limit", "single_call_token_cap"]) {
    assert.ok(panel.querySelector(`#cfg-${key}`), `${key} field`);
  }
});

const failed = results.filter(([ok]) => !ok).length;
console.log(`\n${results.length - failed}/${results.length} checks passed`);
if (failed) process.exit(1);
