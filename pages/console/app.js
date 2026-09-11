const bridge = window.AstrBotPluginPage;
const $ = (id) => document.getElementById(id);

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function factItem(f, extra = "") {
  return `<div class="item">
    <span class="chip">${esc(f.status)}</span>
    <span class="chip">${esc(f.speaker_name || f.speaker_id)}</span>
    <b>#${esc(f.id)}</b> ${esc(f.attribute)} · ${esc(f.content)}
    ${extra}
  </div>`;
}

async function loadOverview() {
  const ov = await bridge.apiGet("overview");
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
          <button data-alias="${esc(s.alias)}" data-canonical="${esc(s.canonical_id)}">映射</button></div>`).join("")
      : `<p class="lede">没有同名不同 id 的建议。</p>`;
    box.querySelectorAll("[data-alias]").forEach((btn) => {
      btn.onclick = async () => {
        await bridge.apiPost("aliases/set", { alias: btn.dataset.alias, canonical_id: btn.dataset.canonical });
        await reload();
      };
    });
  }
  $("diag").textContent = JSON.stringify({overview: ov, usage}, null, 2);
}

async function loadFacts() {
  const live = await bridge.apiGet("facts", { status: "live" });
  const archived = await bridge.apiGet("facts", { status: "superseded" });
  $("archived").innerHTML = (archived.items || []).slice(0, 12).map((f) =>
    factItem(f, f.superseded_by ? `<div class="lede">被 #${esc(f.superseded_by)} 覆盖</div>` : "")
  ).join("") || `<p class="lede">没有 superseded 记录。</p>`;
  if (!$("hits").dataset.locked) {
    $("hits").innerHTML = (live.items || []).slice(0, 16).map((f) => factItem(f)).join("")
      || `<p class="lede">还没有 live 事实。</p>`;
  }
}

async function loadPending() {
  const data = await bridge.apiGet("pending");
  const items = data.items || [];
  if (!items.length) {
    $("pending").innerHTML = `<p class="lede">没有待确认覆盖。</p>`;
    return;
  }
  $("pending").innerHTML = items.map((p) => `<div class="item">
    <div>#${esc(p.id)} ← old ${esc(p.old_fact_id)} · ${esc(p.reason)}</div>
    <div class="lede">${esc(JSON.stringify(p.new_payload))}</div>
    <div class="row">
      <button data-confirm="${esc(p.id)}">确认覆盖</button>
      <button class="ghost" data-reject="${esc(p.id)}">驳回</button>
    </div>
  </div>`).join("");
  $("pending").querySelectorAll("[data-confirm]").forEach((btn) => {
    btn.onclick = async () => {
      await bridge.apiPost("pending/confirm", { id: Number(btn.dataset.confirm) });
      await reload();
    };
  });
  $("pending").querySelectorAll("[data-reject]").forEach((btn) => {
    btn.onclick = async () => {
      await bridge.apiPost("pending/reject", { id: Number(btn.dataset.reject) });
      await reload();
    };
  });
}

async function loadReviews() {
  const data = await bridge.apiGet("reviews", { status: "pending" });
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
    <b>#${esc(r.id)}</b> ${esc(r.title)}
    <div class="lede">${esc(r.reason)} · ${esc(JSON.stringify(r.payload))}</div>
    <div class="row">
      <button data-approve="${esc(r.id)}">批准</button>
      <button class="ghost" data-reject-review="${esc(r.id)}">驳回</button>
    </div>
  </div>`).join("");
  box.querySelectorAll("[data-approve]").forEach((btn) => {
    btn.onclick = async () => {
      await bridge.apiPost("reviews/set", { id: Number(btn.dataset.approve), status: "approved" });
      await reload();
    };
  });
  box.querySelectorAll("[data-reject-review]").forEach((btn) => {
    btn.onclick = async () => {
      await bridge.apiPost("reviews/set", { id: Number(btn.dataset.rejectReview), status: "rejected" });
      await reload();
    };
  });
}

async function loadMicroscope() {
  const box = $("microscope");
  if (!box) return;
  const data = await bridge.apiGet("microscope", { n: 8 });
  const items = data.items || [];
  if (!items.length) {
    box.innerHTML = `<p class="lede">还没有注入记录。说几句让主链跑起来就会出现。</p>`;
    return;
  }
  box.innerHTML = items.map((it) => {
    const p = it.payload || {};
    const blocked = (p.blocked || []).map((b) => `${b.id}:${b.reason}`).join("；") || "无";
    return `<div class="item">
      <span class="chip">${esc(p.route)}</span>
      <span class="chip">${esc(p.path)}</span>
      <span class="chip">${esc(p.cache)}</span>
      <div>${esc(p.query || "")}</div>
      <div class="lede">core=${esc(p.core)} related=${esc(p.related)} jargon=${esc(p.jargon)} chars=${esc(p.pack_chars)}</div>
      <div class="lede">blocked ${esc(blocked)}</div>
    </div>`;
  }).join("");
}

async function reload() {
  await loadOverview();
  await loadFacts();
  await loadPending();
  await loadReviews();
  await loadMicroscope();
}

await bridge.ready();
await reload();

$("refresh").onclick = reload;
$("extract").onclick = async () => {
  const r = await bridge.apiPost("extract", {});
  $("diag").textContent = JSON.stringify(r, null, 2);
  await reload();
};
$("sleep").onclick = async () => {
  const r = await bridge.apiPost("sleep", {});
  $("diag").textContent = JSON.stringify(r, null, 2);
};
$("learn").onclick = async () => {
  const r = await bridge.apiPost("learn", {});
  $("diag").textContent = JSON.stringify(r, null, 2);
  await reload();
};
$("search").onclick = async () => {
  $("hits").dataset.locked = "1";
  const data = await bridge.apiGet("search", {
    q: $("q").value,
    speaker_id: $("speaker").value,
    k: 16,
  });
  $("hits").innerHTML = (data.items || []).map((f) => factItem(f)).join("")
    || `<p class="lede">没有命中。</p>`;
};
$("remember").onclick = async () => {
  const content = $("remember-text").value.trim();
  if (!content) return;
  await bridge.apiPost("remember", {
    content,
    speaker_id: $("remember-speaker").value || "manual",
  });
  $("remember-text").value = "";
  await reload();
};
$("rollback").onclick = async () => {
  const id = Number($("rollback-id").value);
  if (!id) return;
  const r = await bridge.apiPost("rollback", { id });
  $("diag").textContent = JSON.stringify(r, null, 2);
  await reload();
};
$("export").onclick = async () => {
  const r = await bridge.apiGet("export");
  $("diag").textContent = JSON.stringify(r, null, 2);
};
$("archive-preview").onclick = async () => {
  const path = $("archive-path").value.trim();
  if (!path) return;
  const r = await bridge.apiPost("archive/preview", { path });
  $("diag").textContent = JSON.stringify(r, null, 2);
};
$("archive-import").onclick = async () => {
  const path = $("archive-path").value.trim();
  if (!path) return;
  const r = await bridge.apiPost("archive/import", { path });
  $("diag").textContent = JSON.stringify(r, null, 2);
  await reload();
};
$("chat-preview").onclick = async () => {
  const text = $("chat-text").value.trim();
  if (!text) return;
  const r = await bridge.apiPost("chat/preview", {
    text,
    user_names: $("chat-users").value,
    bot_names: $("chat-bots").value,
  });
  $("diag").textContent = JSON.stringify(r, null, 2);
};
$("chat-import").onclick = async () => {
  const text = $("chat-text").value.trim();
  if (!text) return;
  const r = await bridge.apiPost("chat/import", {
    text,
    user_names: $("chat-users").value,
    bot_names: $("chat-bots").value,
  });
  $("diag").textContent = JSON.stringify(r, null, 2);
  await reload();
};
