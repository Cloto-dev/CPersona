// CPersona dashboard. Reads only. Sizes are set through element.style after the
// markup is inserted, because the CSP refuses inline style and script attributes.

const T = {
  en: {
    overview: "Overview", memories: "Memories", episodes: "Episodes", entities: "Entities", browse: "Browse",
    techAlways: "Always show details",
    kbd: ["⌘K search", "/ find in list", "j/k move", "Enter open", "Esc close"],
    overviewLead: "How many memories this store holds, and when they were written.",
    nMemories: "memories", nEpisodes: "episodes", nEntities: "entities", nAgents: "agents",
    spanOf: (a, b) => `from ${a} to ${b}`,
    byMonth: "Memories by month", byMonthHint: "Select a bar to list the memories written that month.",
    byAgent: "By agent", byAgentHint: "Memories are kept per agent. Select a name to list that agent's memories.",
    allMemories: "All memories", searchPh: "Search memory text", shown: (a, b) => `${a} of ${b}`, loadMore: "Show more",
    noMatch: "No memory contains that text.", noMatchHint: "Shorten the word, or clear the folder filter.",
    noMemories: "No memories yet",
    noMemoriesP: "Memories appear here as your agent stores them.",
    askMemories: "“Remember that I prefer short answers.”",
    recalled: n => `recalled ${n}×`, locked: "Locked",
    lvl: { agent: "Agent", project: "Project", channel: "Channel", year: "Year", month: "Month" },
    global: "No project", noChannel: "No channel",
    when: "Recorded", who: "Agent", where: "Project / channel", from: "Source", used: "Times recalled",
    usedN: (n, t) => n ? `${n} times, last ${t}` : "Not yet", lockedQ: "Locked", yes: "Yes, it cannot be edited or deleted", no: "No",
    length: "Length", chars: n => `${n.toLocaleString()} characters`,
    copyRef: "Copy reference", copied: r => `Copied ${r}`, close: "Close",
    tech: "Details", split: "Split",
    nodes: "Nodes", nodesN: n => `${n} node${n === 1 ? "" : "s"}`,
    nodesHint: "Parts short enough for the embedding model's input limit, so each can be found by search and quoted.",
    blocks: "Blocks", blocksN: n => `${n} block${n === 1 ? "" : "s"}`,
    blocksHint: "Shorter spans, so search can find text deep inside a long memory.",
    splitHint: "Click a part to highlight it in the text and scroll to it. Click it again to clear.",
    vector: "Search vector", vectorMissing: "None stored", ids: "Identifiers", raw: "Original memory",
    emptyEpisodes: "No episodes yet",
    emptyEpisodesP: "An episode is a summary of a whole conversation. Your agent writes one when a session ends.",
    emptyEntities: "No entities yet",
    emptyEntitiesP: "Entities are names your agent declares — people, projects, places — so it can connect memories about the same thing.",
    soonEpisodes: "Browsing episodes comes in a later version.",
    soonEntities: "Browsing entities comes in a later version.",
    tryAsk: "Try asking your agent:", askEpisodes: "“Summarise this session and archive it to memory.”",
    askEntities: "“Remember that Caroline and Melanie are friends.”",
    palettePh: "Search memories, or type a command", cmdGo: v => `Go to ${v}`, cmdLang: "Switch to 日本語", cmd: "Command",
    today: "Today", yesterday: "Yesterday", readOnly: "read-only",
    noToken: "Open the dashboard from the address it printed when it started.",
  },
  ja: {
    overview: "概要", memories: "記憶", episodes: "エピソード", entities: "実体", browse: "たどる",
    techAlways: "詳細情報を常に開く",
    kbd: ["⌘K 検索", "/ 一覧を検索", "j/k 移動", "Enter 開く", "Esc 閉じる"],
    overviewLead: "記録先にある記憶の数と、書かれた時期をまとめて表示します。",
    nMemories: "件の記憶", nEpisodes: "件のエピソード", nEntities: "件の実体", nAgents: "エージェント",
    spanOf: (a, b) => `${a} から ${b} まで`,
    byMonth: "月間記憶グラフ", byMonthHint: "棒を選ぶと、その月に書かれた記憶の一覧を開きます。",
    byAgent: "エージェントごと", byAgentHint: "記憶はエージェントごとに分かれています。名前を選ぶと、そのエージェントの記憶の一覧を開きます。",
    allMemories: "すべての記憶", searchPh: "記憶の本文を検索", shown: (a, b) => `${b} 件中 ${a} 件`, loadMore: "さらに表示",
    noMatch: "その語を含む記憶はありません。", noMatchHint: "語を短くするか、フォルダでの絞り込みを外してください。",
    noMemories: "記憶はまだありません",
    noMemoriesP: "エージェントが記憶を保存すると、ここに表示されます。",
    askMemories: "「短い答えが好みだと覚えておいて」",
    recalled: n => `${n} 回想起`, locked: "ロック中",
    lvl: { agent: "エージェント", project: "プロジェクト", channel: "チャンネル", year: "年", month: "月" },
    global: "プロジェクトなし", noChannel: "チャンネルなし",
    when: "記録日時", who: "エージェント", where: "プロジェクト / チャンネル", from: "保存元", used: "想起された回数",
    usedN: (n, t) => n ? `${n} 回 (最後は ${t})` : "まだありません", lockedQ: "ロック", yes: "はい (編集・削除できません)", no: "いいえ",
    length: "長さ", chars: n => `${n.toLocaleString()} 文字`,
    copyRef: "参照をコピー", copied: r => `${r} をコピーしました`, close: "閉じる",
    tech: "詳細情報", split: "分割",
    nodes: "ノード", nodesN: n => `${n} 個のノード`,
    nodesHint: "埋め込みモデルの入力上限に収まるように分けた区間です。区間ごとに検索で見つけ、引用できます。",
    blocks: "ブロック", blocksN: n => `${n} 個のブロック`,
    blocksHint: "長い記憶の途中まで検索が届くように、さらに細かく分けた区間です。",
    splitHint: "区間をクリックすると、本文の該当箇所へ移動して強調表示します。もう一度クリックすると解除します。",
    vector: "検索用のベクトル", vectorMissing: "保存されていません", ids: "識別子", raw: "原記憶",
    emptyEpisodes: "エピソードはまだありません",
    emptyEpisodesP: "エピソードは会話 1 回分のまとめで、セッションが終わる時にエージェントが書きます。",
    emptyEntities: "実体はまだありません",
    emptyEntitiesP: "実体は、エージェントが宣言する人・プロジェクト・場所などの名前です。同じものについての記憶をつなぐのに使います。",
    soonEpisodes: "エピソードを見る画面は、今後のバージョンで入ります。",
    soonEntities: "実体を見る画面は、今後のバージョンで入ります。",
    tryAsk: "エージェントにこう頼んでみてください:", askEpisodes: "「このセッションをまとめて記憶に残して」",
    askEntities: "「Caroline と Melanie は友達だと覚えておいて」",
    palettePh: "記憶を検索、またはコマンドを入力", cmdGo: v => `${v} へ移動`, cmdLang: "Switch to English", cmd: "コマンド",
    today: "今日", yesterday: "昨日", readOnly: "読み取り専用",
    noToken: "起動時に表示されたアドレスから、ダッシュボードを開き直してください。",
  },
};

const LEVELS = ["agent", "project", "channel", "year", "month"];
const $ = id => document.getElementById(id);

// The token arrives after "#t=" and is never sent to the server in a URL. Keep it
// for this tab, then take it out of the address bar so it is not shared by accident.
const H0 = new URLSearchParams(location.hash.slice(1));
let token = H0.get("t") || "";
try {
  if (token) sessionStorage.setItem("cp.token", token);
  else token = sessionStorage.getItem("cp.token") || "";
} catch {}

const state = { view: "overview", path: [], q: "", id: 0, month: "", lang: "en" };
for (const k of ["view", "q", "month"]) if (H0.get(k)) state[k] = H0.get(k);
if (H0.get("id")) state.id = Number(H0.get("id")) || 0;
try { if (H0.get("path")) state.path = JSON.parse(H0.get("path")); } catch {}
try { state.lang = localStorage.getItem("cp.lang") || (navigator.language || "").startsWith("ja") && "ja" || "en"; } catch {}
if (H0.get("lang")) state.lang = H0.get("lang");
let techAlways = false;
try { techAlways = localStorage.getItem("cp.tech") === "1"; } catch {}
if (H0.get("tech") === "1") techAlways = true;

const t = k => T[state.lang][k];
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const day = ts => (ts || "").slice(0, 10);
const when = ts => (ts || "").replace("T", " ").slice(0, 16);
const mark = (text, q) => {
  const e = esc(text);
  if (!q) return e;
  const re = new RegExp(esc(q).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi");
  return e.replace(re, m => `<mark>${m}</mark>`);
};
function levelLabel(level, key) {
  if (level === "project" && !key) return t("global");
  if (level === "channel" && !key) return t("noChannel");
  return key;
}

async function api(path, params = {}) {
  const r = await fetch(path + "?" + new URLSearchParams(params), { headers: { Authorization: `Bearer ${token}` } });
  if (r.status === 401) throw new Error(t("noToken"));
  const body = await r.json();
  if (!r.ok) throw new Error(body.error || r.statusText);
  return body;
}

// Apply data-h / data-w / data-flex sizes (percent or flex weight) inside a subtree.
function sizes(root) {
  for (const el of root.querySelectorAll("[data-h]")) el.style.height = el.dataset.h + "%";
  for (const el of root.querySelectorAll("[data-w]")) el.style.width = el.dataset.w + "%";
  for (const el of root.querySelectorAll("[data-flex]")) el.style.flex = el.dataset.flex;
}

function saveHash() {
  const h = new URLSearchParams({ view: state.view });
  if (state.path.length) h.set("path", JSON.stringify(state.path));
  if (state.q) h.set("q", state.q);
  if (state.month) h.set("month", state.month);
  if (state.id) h.set("id", state.id);
  history.replaceState(null, "", "#" + h);
}

function toast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), 1600);
}

function fail(e) {
  $("page").innerHTML = `<div class="empty"><h2>${esc(e.message)}</h2></div>`;
}

let overviewData = null;

function applyLang() {
  document.documentElement.lang = state.lang;
  for (const el of document.querySelectorAll("[data-t]")) el.textContent = t(el.dataset.t);
  for (const b of document.querySelectorAll("[data-lang]")) b.setAttribute("aria-pressed", String(b.dataset.lang === state.lang));
  $("paletteInput").placeholder = t("palettePh");
  $("kbd").innerHTML = t("kbd").map(k => `<span>${esc(k)}</span>`).join("");
  if (overviewData) $("schemaLine").textContent = `Schema ${overviewData.schema} · ${t("readOnly")}`;
}

// ---- Sidebar: the logical directory (agent / project / channel / year / month)
async function renderTree() {
  const root = $("tree");
  const fresh = document.createElement("div");
  await growTree(fresh, [], 0);
  root.replaceChildren(...fresh.childNodes);
}

async function growTree(container, path, depth) {
  const kids = await api("/api/tree", { path: JSON.stringify(path) });
  for (const k of kids) {
    const p = [...path, k.key];
    const leaf = p.length >= LEVELS.length;
    const open = !leaf && state.path.length > depth && JSON.stringify(state.path.slice(0, depth + 1)) === JSON.stringify(p);
    const on = state.view === "memories" && JSON.stringify(state.path) === JSON.stringify(p);
    const node = document.createElement("div");
    node.className = "node";
    const label = k.key ? k.label : levelLabel(k.level, k.key);
    node.innerHTML = `<button role="treeitem" class="${on ? "on" : ""}" ${leaf ? "" : `aria-expanded="${open}"`}
        title="${esc(t("lvl")[k.level])}: ${esc(levelLabel(k.level, k.key))}">
      <span class="caret">${leaf ? "" : open ? "▾" : "▸"}</span>
      <span class="label">${esc(label)}</span><span class="n">${k.count}</span></button><div class="kids"></div>`;
    node.firstElementChild.addEventListener("click", () => go({ view: "memories", path: p, q: "", month: "" }));
    container.appendChild(node);
    if (open) await growTree(node.lastElementChild, p, depth + 1);
  }
}

// ---- Overview
function renderOverview() {
  const o = overviewData;
  const page = $("page");
  if (!o.counts.memories) return renderEmptyMemories();
  const max = Math.max(...o.months.map(m => m.count), 1);
  const maxA = Math.max(...o.agents.map(a => a.count), 1);
  page.innerHTML = `
    <h1>${t("overview")}</h1><p class="lead">${t("overviewLead")}</p>
    <div class="facts">
      <div class="fact"><div class="v">${o.counts.memories.toLocaleString()}</div><div class="k">${t("nMemories")}<br><small>${esc(t("spanOf")(day(o.span[0]), day(o.span[1])))}</small></div></div>
      <div class="fact"><div class="v">${o.counts.episodes.toLocaleString()}</div><div class="k">${t("nEpisodes")}</div></div>
      <div class="fact"><div class="v">${o.counts.entities.toLocaleString()}</div><div class="k">${t("nEntities")}</div></div>
      <div class="fact"><div class="v">${o.agents.length}</div><div class="k">${t("nAgents")}</div></div>
    </div>
    <div class="block"><h2>${t("byMonth")}</h2><div class="hint">${t("byMonthHint")}</div>
      <div class="hist">${o.months.map(m => `<button data-h="${Math.max(2, 100 * m.count / max)}" title="${m.month}: ${m.count}" aria-label="${m.month}: ${m.count}" data-month="${esc(m.month)}"></button>`).join("")}</div>
      <div class="hist-axis"><span>${esc(o.months[0]?.month ?? "")}</span><span>${esc(o.months.at(-1)?.month ?? "")}</span></div>
    </div>
    <div class="block"><h2>${t("byAgent")}</h2><div class="hint">${t("byAgentHint")}</div>
      <div class="bars">${o.agents.slice(0, 12).map(a => `<button data-agent="${esc(a.id)}" title="${esc(a.id)}">${esc(a.id)}</button>
        <div class="track"><div class="fill" data-w="${100 * a.count / maxA}"></div></div><div class="num">${a.count}</div>`).join("")}</div>
    </div>`;
  sizes(page);
  for (const b of page.querySelectorAll("[data-month]")) b.addEventListener("click", () => go({ view: "memories", path: [], q: "", month: b.dataset.month }));
  for (const b of page.querySelectorAll("[data-agent]")) b.addEventListener("click", () => go({ view: "memories", path: [b.dataset.agent], q: "", month: "" }));
}

function renderEmptyMemories() {
  $("page").innerHTML = `<h1>${t("memories")}</h1>
    <div class="empty"><h2>${t("noMemories")}</h2><p>${t("noMemoriesP")}</p>
      <p class="hint">${t("tryAsk")}</p><div class="say">${t("askMemories")}</div></div>`;
}

// ---- Memory list
let listOffset = 0, listRows = [];
async function renderMemories(append = false) {
  const page = $("page");
  if (!overviewData.counts.memories) return renderEmptyMemories();
  if (!append) { listOffset = 0; listRows = []; }
  const res = await api("/api/memories", { q: state.q, path: JSON.stringify(state.path), month: state.month, limit: 40, offset: listOffset });
  listRows = listRows.concat(res.rows);
  listOffset += 40;
  if (!append) {
    const crumbs = [`<button data-depth="0">${t("allMemories")}</button>`].concat(state.path.map((k, i) =>
      `<span class="sep">›</span><button data-depth="${i + 1}" title="${esc(t("lvl")[LEVELS[i]])}">${esc(levelLabel(LEVELS[i], k))}</button>`)).join("");
    page.innerHTML = `
      <h1>${t("memories")}</h1>
      <div class="crumbs">${crumbs}${state.month ? `<span class="sep">›</span><span>${esc(state.month)}</span>` : ""}</div>
      <div class="toolbar"><label class="search">
        <svg aria-hidden="true" class="icon" width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="7" cy="7" r="5"/><path d="M11 11l3.5 3.5"/></svg>
        <input id="q" type="search" placeholder="${t("searchPh")}" value="${esc(state.q)}" aria-label="${t("searchPh")}"></label></div>
      <div id="list"></div><div class="more" id="more"></div>`;
    let timer;
    $("q").addEventListener("input", e => {
      clearTimeout(timer);
      timer = setTimeout(() => { state.q = e.target.value.trim(); saveHash(); renderMemories().catch(fail); }, 220);
    });
    for (const b of page.querySelectorAll(".crumbs [data-depth]"))
      b.addEventListener("click", () => go({ view: "memories", path: state.path.slice(0, Number(b.dataset.depth)), month: "" }));
  }
  const list = $("list");
  if (!listRows.length) {
    list.innerHTML = `<div class="empty"><h2>${t("noMatch")}</h2><p>${t("noMatchHint")}</p></div>`;
    $("more").innerHTML = "";
    return;
  }
  const now = new Date();
  const today = now.toISOString().slice(0, 10), yday = new Date(now - 864e5).toISOString().slice(0, 10);
  let html = "", last = "";
  for (const r of listRows) {
    const d = day(r.timestamp);
    if (d !== last) { html += `<div class="group">${d === today ? t("today") : d === yday ? t("yesterday") : esc(d)}</div>`; last = d; }
    const meta = [`<span title="${esc(r.agent_id)}">${esc(r.agent_id)}</span>`, `<span>${esc(when(r.timestamp).slice(11))}</span>`];
    if (r.recall_count) meta.push(`<span>${t("recalled")(r.recall_count)}</span>`);
    if (r.locked) meta.push(`<span>${t("locked")}</span>`);
    html += `<button class="row ${r.id === state.id ? "sel" : ""}" data-id="${r.id}"><div class="text">${mark(r.snippet, state.q)}</div><div class="meta">${meta.join("")}</div></button>`;
  }
  list.innerHTML = html;
  for (const b of list.querySelectorAll(".row")) b.addEventListener("click", () => openDetail(Number(b.dataset.id)).catch(fail));
  $("more").innerHTML = `<span class="hint">${t("shown")(listRows.length, res.total)}</span>` +
    (res.total > listRows.length ? ` <button class="btn" id="moreBtn">${t("loadMore")}</button>` : "");
  $("moreBtn")?.addEventListener("click", () => renderMemories(true).catch(fail));
}

function renderOther(kind) {
  const n = overviewData.counts[kind];
  const E = kind === "episodes" ? ["emptyEpisodes", "emptyEpisodesP", "askEpisodes", "soonEpisodes"] : ["emptyEntities", "emptyEntitiesP", "askEntities", "soonEntities"];
  $("page").innerHTML = n
    ? `<h1>${t(kind)}</h1><p class="lead">${n.toLocaleString()} · ${t(E[3])}</p>`
    : `<h1>${t(kind)}</h1><div class="empty"><h2>${t(E[0])}</h2><p>${t(E[1])}</p><p class="hint">${t("tryAsk")}</p><div class="say">${t(E[2])}</div></div>`;
}

// ---- Detail drawer
function spans(parts, total, kind) {
  return parts.map(p => `<button class="part ${kind}" data-kind="${kind}" data-i="${p.i}" data-flex="${Math.max(p.end - p.start, 1) / total}"
    aria-label="${esc(t(kind))} ${p.i + 1}"></button>`).join("");
}

async function openDetail(id) {
  state.id = id;
  saveHash();
  for (const b of document.querySelectorAll(".row")) b.classList.toggle("sel", Number(b.dataset.id) === id);
  const m = await api("/api/memory", { id });
  let src = m.source;
  try { const s = JSON.parse(m.source); src = [s.type, s.name || s.id].filter(Boolean).join(" · "); } catch {}
  const total = Math.max(1, m.content.length);
  const nodes = m.nodes.map(n => ({ i: n.node_index, start: n.start_char, end: n.end_char }));
  const blocks = m.blocks.map(b => ({ i: b.block_index, start: b.start_char, end: b.end_char }));
  const d = $("drawer");
  d.innerHTML = `
    <div class="top"><h2>${esc(day(m.timestamp))} · ${esc(m.agent_id)}</h2>
      <button class="x" id="closeBtn" aria-label="${t("close")}">×</button></div>
    <dl class="facts-list">
      <dt>${t("when")}</dt><dd>${esc(when(m.timestamp))} UTC</dd>
      <dt>${t("who")}</dt><dd>${esc(m.agent_id)}</dd>
      <dt>${t("where")}</dt><dd>${esc(levelLabel("project", m.project_id))} / ${esc(levelLabel("channel", m.channel))}</dd>
      <dt>${t("from")}</dt><dd>${esc(src)}</dd>
      <dt>${t("used")}</dt><dd>${esc(t("usedN")(m.recall_count, when(m.last_recalled_at)))}</dd>
      <dt>${t("lockedQ")}</dt><dd>${m.locked ? t("yes") : t("no")}</dd>
    </dl>
    <div class="actions"><button class="btn" id="copyRef">${t("copyRef")} · mem:${m.id}</button></div>
    <div class="content" id="content">${mark(m.content, state.q)}</div>
    <details class="tech" ${techAlways ? "open" : ""}><summary>${t("tech")}</summary>
      <dl class="facts-list">
        <dt>${t("split")}</dt><dd>
          ${nodes.length || blocks.length ? `
          <div class="split-row"><span class="split-name is-node">${t("nodesN")(nodes.length)}</span><div class="parts">${spans(nodes, total, "nodes")}</div></div>
          <div class="split-row"><span class="split-name is-block">${t("blocksN")(blocks.length)}</span><div class="parts">${spans(blocks, total, "blocks")}</div></div>
          <div class="hint">${t("splitHint")}</div>
          <div class="hint"><b>${t("nodes")}</b>: ${t("nodesHint")} <b>${t("blocks")}</b>: ${t("blocksHint")}</div>` : "—"}
        </dd>
        <dt>${t("vector")}</dt><dd>${m.has_embedding ? esc(m.embedding_model || "✓") : `<span class="muted">${t("vectorMissing")}</span>`}</dd>
        <dt>${t("length")}</dt><dd>${t("chars")(m.content.length)}</dd>
        <dt>${t("ids")}</dt><dd>mem:${m.id}${m.msg_id ? " · " + esc(m.msg_id) : ""}</dd>
      </dl>
      <details><summary class="hint">${t("raw")}</summary><pre class="raw">${esc(JSON.stringify({ ...m, nodes: undefined, blocks: undefined }, null, 2))}</pre></details>
    </details>`;
  sizes(d);
  d.classList.add("open");
  $("closeBtn").addEventListener("click", closeDetail);
  $("copyRef").addEventListener("click", () => {
    navigator.clipboard?.writeText(`mem:${m.id}`);
    toast(t("copied")(`mem:${m.id}`));
  });
  // Clicking a part lights it in the text and scrolls to it; clicking it again clears.
  // Hovering does nothing, so moving the pointer across the bars never moves the text.
  const content = $("content");
  const plain = mark(m.content, state.q);
  let lit = null;
  for (const b of d.querySelectorAll(".part")) {
    const list = b.dataset.kind === "nodes" ? nodes : blocks;
    const p = list.find(x => x.i === Number(b.dataset.i));
    b.addEventListener("click", () => {
      for (const o of d.querySelectorAll(".part.on")) o.classList.remove("on");
      if (lit === b) { lit = null; content.innerHTML = plain; return; }
      lit = b;
      const cls = b.dataset.kind === "nodes" ? "is-node" : "is-block";
      content.innerHTML = esc(m.content.slice(0, p.start)) + `<span class="lit ${cls}">${esc(m.content.slice(p.start, p.end))}</span>` + esc(m.content.slice(p.end));
      content.querySelector(".lit")?.scrollIntoView({ block: "center" });
      b.classList.add("on");
    });
  }
}

function closeDetail() {
  state.id = 0;
  saveHash();
  $("drawer").classList.remove("open");
  for (const b of document.querySelectorAll(".row.sel")) b.classList.remove("sel");
}

// ---- Command palette
let palItems = [], palOn = 0, palTimer;
async function openPalette(text = "") {
  $("paletteBg").classList.add("open");
  $("paletteInput").value = text;
  $("paletteInput").focus();
  await fillPalette(text);
}
async function fillPalette(text) {
  const q = text.trim();
  const cmds = [
    ...["overview", "memories", "episodes", "entities"].map(v => ({ label: t("cmdGo")(t(v)), side: t("cmd"), run: () => go({ view: v, path: [], q: "", month: "" }) })),
    { label: t("cmdLang"), side: t("cmd"), run: () => setLang(state.lang === "en" ? "ja" : "en") },
  ].filter(c => !q || c.label.toLowerCase().includes(q.toLowerCase()));
  let found = [];
  if (q.length >= 2) {
    const res = await api("/api/memories", { q, limit: 8 });
    // Start the line just before the match, so the matched word is not cut off at the end.
    const near = s => { const i = s.toLowerCase().indexOf(q.toLowerCase()); return i > 30 ? "… " + s.slice(s.indexOf(" ", i - 30) + 1) : s; };
    found = res.rows.map(r => ({ label: near(r.snippet), side: day(r.timestamp), q, run: () => go({ view: "memories", path: [], q, month: "" }).then(() => openDetail(r.id)) }));
  }
  palItems = [...found, ...cmds];
  palOn = 0;
  drawPalette();
}
function drawPalette() {
  $("paletteList").innerHTML = palItems.map((it, i) =>
    `<li class="${i === palOn ? "on" : ""}" data-i="${i}" role="option"><span class="t">${mark(it.label, it.q)}</span><span class="s">${esc(it.side)}</span></li>`).join("");
  for (const li of $("paletteList").querySelectorAll("li")) li.addEventListener("click", () => runPalette(Number(li.dataset.i)));
}
function runPalette(i) { const it = palItems[i]; closePalette(); it?.run(); }
function closePalette() { $("paletteBg").classList.remove("open"); }

// ---- Routing
function setLang(l) {
  state.lang = l;
  try { localStorage.setItem("cp.lang", l); } catch {}
  applyLang();
  route().catch(fail);
}
function go(next) { Object.assign(state, next); closeDetail(); return route().catch(fail); }
async function route() {
  for (const b of document.querySelectorAll(".nav")) {
    if (b.dataset.view === state.view) b.setAttribute("aria-current", "page");
    else b.removeAttribute("aria-current");
  }
  saveHash();
  renderTree().catch(() => {});
  if (state.view === "overview") renderOverview();
  else if (state.view === "memories") await renderMemories();
  else renderOther(state.view);
}

function wire() {
  for (const b of document.querySelectorAll(".nav")) b.addEventListener("click", () => go({ view: b.dataset.view, path: [], q: "", month: "" }));
  for (const b of document.querySelectorAll("[data-lang]")) b.addEventListener("click", () => setLang(b.dataset.lang));
  const tech = $("techAlways");
  tech.checked = techAlways;
  tech.addEventListener("change", () => {
    techAlways = tech.checked;
    try { localStorage.setItem("cp.tech", techAlways ? "1" : "0"); } catch {}
  });
  $("paletteInput").addEventListener("input", e => { clearTimeout(palTimer); palTimer = setTimeout(() => fillPalette(e.target.value).catch(() => {}), 150); });
  $("paletteInput").addEventListener("keydown", e => {
    if (e.key === "ArrowDown") { palOn = Math.min(palOn + 1, palItems.length - 1); drawPalette(); e.preventDefault(); }
    if (e.key === "ArrowUp") { palOn = Math.max(palOn - 1, 0); drawPalette(); e.preventDefault(); }
    if (e.key === "Enter") runPalette(palOn);
  });
  $("paletteBg").addEventListener("click", e => { if (e.target.id === "paletteBg") closePalette(); });
  document.addEventListener("keydown", e => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openPalette(); return; }
    if (e.key === "Escape") { closePalette(); closeDetail(); return; }
    if (e.target.matches("input, textarea")) return;
    if (e.key === "/") { e.preventDefault(); $("q")?.focus(); }
    if (e.key === "j" || e.key === "k") {
      const rows = [...document.querySelectorAll(".row")];
      if (!rows.length) return;
      const i = rows.findIndex(r => r.classList.contains("sel"));
      const next = rows[Math.max(0, Math.min(rows.length - 1, i + (e.key === "j" ? 1 : -1)))];
      next.focus();
      next.click();
    }
  });
}

(async () => {
  wire();
  applyLang();
  try {
    if (!token) throw new Error(t("noToken"));
    overviewData = await api("/api/overview");
  } catch (e) {
    return fail(e);
  }
  $("dbname").textContent = overviewData.db;
  for (const k of ["memories", "episodes", "entities"]) $("n-" + k).textContent = overviewData.counts[k].toLocaleString();
  applyLang();
  await route().catch(fail);
  if (state.id) await openDetail(state.id).catch(fail);
  if (H0.get("palette") === "1") openPalette(H0.get("pq") || "");
})();
