// CPersona dashboard. Reads only. Sizes are set through element.style after the
// markup is inserted, because the CSP refuses inline style and script attributes.

const T = {
  en: {
    overview: "Overview", memories: "Memories", episodes: "Episodes", entities: "Entities", browse: "Browse",
    techAlways: "Always show details",
    kbd: ["⌘K search", "/ find in list", "j/k move", "Enter open", "Esc close"],
    overviewLead: "How many memories are stored, and when they were stored.",
    nMemories: "memories", nEpisodes: "episodes", nEntities: "entities", nAgents: "agents",
    spanOf: (a, b) => `from ${a} to ${b}`,
    byMonth: "Memories by month", byMonthHint: "Select a bar to list the memories stored that month.",
    byAgent: "By agent", byAgentHint: "Memories are kept per agent. Select a name to list that agent's memories.",
    health: "Store health", healthChecking: "Checking…", healthOk: "No problems found.",
    healthInfo: n => `${n} note${n === 1 ? "" : "s"} for information`, sev: { critical: "Critical", warn: "Warning", info: "Note" },
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
    emptyEpisodesP: "An episode is the summary of one conversation. Your agent stores one when a session ends.",
    emptyEntities: "No entities yet",
    emptyEntitiesP: "Entities are names your agent declares — people, projects, places — so it can connect memories about the same thing.",
    soonEpisodes: "Browsing episodes comes in a later version.",
    soonEntities: "Browsing entities comes in a later version.",
    tryAsk: "Try asking your agent:", askEpisodes: "“Summarise this session and archive it to memory.”",
    askEntities: "“Remember that Caroline and Melanie are friends.”",
    palettePh: "Search memories, or type a command", cmdGo: v => `Go to ${v}`, cmdLang: "Switch to 日本語", cmd: "Command",
    today: "Today", yesterday: "Yesterday", readOnly: "read-only",
    noToken: "Open the dashboard from the address it printed when it started.",
    graph: "Network",
    graphLead: "The memories of this folder, joined by what they have in common.",
    nGraph: (shown, total) => shown < total ? `The newest ${shown.toLocaleString()} of ${total.toLocaleString()} memories` : `${total.toLocaleString()} memories`,
    noVectorN: n => `${n.toLocaleString()} without a search vector have no similar lines.`,
    aroundTitle: id => `Around mem:${id}`,
    aroundLead: (k, hops) => `From the memory you chose, its ${k} most similar memories, and theirs, ${hops} steps out.`,
    aroundNoVector: "This memory has no search vector, so similar memories cannot be found from it.",
    backToFolder: "Back to the folder",
    layerDeclared: "Declared relations", layerSimilar: "Similar content (computed)", layerStructure: "Grouped by agent",
    linesN: n => `${n}`,
    similarNote: "Similar lines are computed from the stored embeddings. No agent declared them.",
    graphHint: "Drag the background to move · Scroll to zoom · Drag a point to move it · Click a point to see it",
    similarN: n => `Similar memories: ${n}`, mentionsN: n => `Mentioned in ${n} memories shown here`,
    openMemory: "Open", centerHere: "Center on this memory", showAround: "See it in the network",
  },
  ja: {
    overview: "概要", memories: "記憶", episodes: "エピソード", entities: "実体", browse: "たどる",
    techAlways: "詳細情報を常に開く",
    kbd: ["⌘K 検索", "/ 一覧を検索", "j/k 移動", "Enter 開く", "Esc 閉じる"],
    overviewLead: "保存されている記憶の件数と、保存された時期を確認できます。",
    nMemories: "件の記憶", nEpisodes: "件のエピソード", nEntities: "件の実体", nAgents: "エージェント",
    spanOf: (a, b) => `${a} から ${b} まで`,
    byMonth: "月間記憶グラフ", byMonthHint: "棒を選ぶと、その月に保存された記憶の一覧を開きます。",
    byAgent: "エージェントごと", byAgentHint: "記憶はエージェントごとに分かれています。名前を選ぶと、そのエージェントの記憶の一覧を開きます。",
    health: "データベースの状態", healthChecking: "確認しています…", healthOk: "問題は見つかりませんでした。",
    healthInfo: n => `お知らせ ${n} 件`, sev: { critical: "重大", warn: "注意", info: "お知らせ" },
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
    emptyEpisodesP: "エピソードは、1 回の会話の要約です。セッションの終わりにエージェントが保存します。",
    emptyEntities: "実体はまだありません",
    emptyEntitiesP: "実体は、エージェントが宣言する人・プロジェクト・場所などの名前です。同じものについての記憶をつなぐのに使います。",
    soonEpisodes: "エピソードを見る画面は、今後のバージョンで入ります。",
    soonEntities: "実体を見る画面は、今後のバージョンで入ります。",
    tryAsk: "エージェントにこう頼んでみてください:", askEpisodes: "「このセッションをまとめて記憶に残して」",
    askEntities: "「Caroline と Melanie は友達だと覚えておいて」",
    palettePh: "記憶を検索、またはコマンドを入力", cmdGo: v => `${v} へ移動`, cmdLang: "Switch to English", cmd: "コマンド",
    today: "今日", yesterday: "昨日", readOnly: "読み取り専用",
    noToken: "起動時に表示されたアドレスから、ダッシュボードを開き直してください。",
    graph: "記憶ネットワーク",
    graphLead: "このフォルダの記憶を、共通点の線で結んで表示します。",
    nGraph: (shown, total) => shown < total ? `${total.toLocaleString()} 件のうち、新しい ${shown.toLocaleString()} 件を表示しています` : `${total.toLocaleString()} 件の記憶`,
    noVectorN: n => `検索用のベクトルがない ${n.toLocaleString()} 件には、「内容が似ている」線がありません。`,
    aroundTitle: id => `mem:${id} の周り`,
    aroundLead: (k, hops) => `選んだ記憶から ${hops} 段先まで、それぞれ似ている記憶を ${k} 件ずつたどっています。`,
    aroundNoVector: "この記憶には検索用のベクトルがないので、似ている記憶をたどれません。",
    backToFolder: "フォルダの図に戻る",
    layerDeclared: "宣言した関係", layerSimilar: "内容が似ている (自動)", layerStructure: "エージェントごとのまとまり",
    linesN: n => `${n} 本`,
    similarNote: "「内容が似ている」線は、保存された埋め込みから計算したつながりです。エージェントが宣言した関係ではありません。",
    graphHint: "背景をドラッグで移動 · ホイールで拡大縮小 · 点をつまんで動かす · 点をクリックで内容",
    similarN: n => `内容が似ている記憶: ${n} 件`, mentionsN: n => `この図の記憶 ${n} 件で言及されています`,
    openMemory: "詳細を開く", centerHere: "この記憶を中心にする", showAround: "記憶ネットワークで見る",
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

const state = { view: "overview", path: [], q: "", id: 0, month: "", around: 0, lang: "en" };
for (const k of ["view", "q", "month"]) if (H0.get(k)) state[k] = H0.get(k);
if (H0.get("id")) state.id = Number(H0.get("id")) || 0;
if (H0.get("around")) state.around = Number(H0.get("around")) || 0;
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

// Apply data-h / data-w / data-left sizes (percent) inside a subtree. A part of a
// split bar also carries data-part: its width leaves a 1px gap to the next part.
function sizes(root) {
  for (const el of root.querySelectorAll("[data-h]")) el.style.height = el.dataset.h + "%";
  for (const el of root.querySelectorAll("[data-w]"))
    el.style.width = el.hasAttribute("data-part") ? `max(1px, calc(${el.dataset.w}% - 1px))` : el.dataset.w + "%";
  for (const el of root.querySelectorAll("[data-left]")) el.style.left = el.dataset.left + "%";
}

function saveHash() {
  const h = new URLSearchParams({ view: state.view });
  if (state.path.length) h.set("path", JSON.stringify(state.path));
  if (state.q) h.set("q", state.q);
  if (state.month) h.set("month", state.month);
  if (state.id) h.set("id", state.id);
  if (state.around) h.set("around", state.around);
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
    const on = (state.view === "memories" || state.view === "graph" && !state.around) && JSON.stringify(state.path) === JSON.stringify(p);
    const node = document.createElement("div");
    node.className = "node";
    const label = k.key ? k.label : levelLabel(k.level, k.key);
    node.innerHTML = `<button role="treeitem" class="${on ? "on" : ""}" ${leaf ? "" : `aria-expanded="${open}"`}
        title="${esc(t("lvl")[k.level])}: ${esc(levelLabel(k.level, k.key))}">
      <span class="caret">${leaf ? "" : open ? "▾" : "▸"}</span>
      <span class="label">${esc(label)}</span><span class="n">${k.count}</span></button><div class="kids"></div>`;
    node.firstElementChild.addEventListener("click", () => go({ view: state.view === "graph" ? "graph" : "memories", path: p, q: "", month: "" }));
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
    <div class="block" id="healthBlock" hidden><h2>${t("health")}</h2><div id="health" class="hint">${t("healthChecking")}</div></div>
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
  renderHealth();
}

// Store health comes from the running server's own checks, so it exists only when an
// agent opened the dashboard; started from a terminal, the block stays hidden.
// The check runs once per page load (it can take seconds); going back to the
// overview shows the same answer.
let health = null;
async function renderHealth() {
  if (!health) {
    const r = await fetch("/api/health", { headers: { Authorization: `Bearer ${token}` } });
    health = { status: r.status, ok: r.ok, body: await r.json().catch(() => ({})) };
  }
  const block = $("healthBlock"), el = $("health");
  if (health.status === 404 || !block || !el) return;
  block.hidden = false;
  if (!health.ok) { el.textContent = health.body.error || String(health.status); return; }
  const issues = health.body.issues || [];
  const serious = issues.filter(i => i.severity === "critical" || i.severity === "warn");
  const notes = issues.length - serious.length;
  el.className = "";
  el.innerHTML = (serious.length
    ? serious.map(i => `<div class="issue"><b>${esc(t("sev")[i.severity] || i.severity)}</b>
        <span><code>${esc(i.type || i.check)}</code>${i.count != null ? ` · ${esc(i.count)}` : ""}${i.hint ? `<br><span class="hint">${esc(i.hint)}</span>` : ""}</span></div>`).join("")
    : `<div class="ok">${t("healthOk")}</div>`) + (notes ? `<div class="hint">${t("healthInfo")(notes)}</div>` : "");
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
// Each part sits where its characters are in the text, so the bar never grows past
// its width however many parts there are.
function spans(parts, total, kind) {
  return parts.map(p => `<button class="part ${kind}" data-part data-kind="${kind}" data-i="${p.i}"
    data-left="${(100 * p.start) / total}" data-w="${(100 * Math.max(p.end - p.start, 1)) / total}"
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
    <div class="actions"><button class="btn" id="copyRef">${t("copyRef")} · mem:${m.id}</button>
      ${m.has_embedding ? `<button class="btn" id="aroundBtn">${t("showAround")}</button>` : ""}</div>
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
  $("aroundBtn")?.addEventListener("click", () => go({ view: "graph", around: m.id }));
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

// ---- Memory network
// Laid out by a small force simulation drawn on a canvas, so nothing is loaded from
// elsewhere. The simulation keeps running while the graph is touched and cools back
// to stillness: memories keep some momentum (friction 0.86), while an agent hub,
// pulled by every one of its memories, is damped to 0.4 so it does not overshoot.
const PALETTE = {
  light: ["#3f6fd8", "#d9733f", "#2f9e7a", "#b8509c", "#8a6bd1", "#c49a2c", "#3a9fb5", "#d0545a", "#6b8f3a", "#7d7f86"],
  dark: ["#6f9af2", "#f0915e", "#4cc39b", "#d978bd", "#a990ee", "#e2bb52", "#5cc2d8", "#ef7a7f", "#93b85d", "#a3a6ad"],
};
const FRICTION = 0.86, HUB_FRICTION = 0.4;
let gs = null; // the graph on screen, or null

function stopGraph() {
  if (gs) { gs.alive = false; gs.observer?.disconnect(); }
  gs = null;
}

async function renderGraph() {
  const page = $("page");
  if (!overviewData.counts.memories) return renderEmptyMemories();
  const params = state.around ? { around: state.around } : { path: JSON.stringify(state.path), month: state.month };
  const G = await api("/api/graph", params);
  const head = state.around
    ? `<h1>${esc(t("aroundTitle")(G.center))}</h1>
       <p class="lead">${G.has_vector ? esc(t("aroundLead")(G.k, G.hops)) : esc(t("aroundNoVector"))}</p>
       <div class="crumbs"><button id="backBtn">${t("backToFolder")}</button></div>`
    : `<h1>${t("graph")}</h1>
       <p class="lead">${t("graphLead")}<br><span class="hint">${esc(t("nGraph")(G.shown, G.total))}${G.without_vector ? "<br>" + esc(t("noVectorN")(G.without_vector)) : ""}</span></p>`;
  const count = k => G.links.filter(l => l.kind === k).length;
  page.innerHTML = `<div class="graph" id="graphWrap"><canvas id="gc"></canvas>
    <div class="g-panel g-legend">${head}
      <label class="layer"><input type="checkbox" data-layer="declared" checked>
        <svg width="34" height="10" aria-hidden="true"><line x1="0" y1="5" x2="34" y2="5" class="l-declared"/></svg>
        <span>${t("layerDeclared")}</span><span class="n">${esc(t("linesN")(count("declared")))}</span></label>
      <label class="layer"><input type="checkbox" data-layer="similar" checked>
        <svg width="34" height="10" aria-hidden="true"><defs><linearGradient id="lg" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="34" y2="0">
          <stop offset="0" class="s-a"/><stop offset="1" class="s-b"/></linearGradient></defs><line x1="0" y1="5" x2="34" y2="5" class="l-similar"/></svg>
        <span>${t("layerSimilar")}</span><span class="n">${esc(t("linesN")(count("similar")))}</span></label>
      <label class="layer"><input type="checkbox" data-layer="structure" checked>
        <svg width="34" height="10" aria-hidden="true"><line x1="0" y1="5" x2="34" y2="5" class="l-structure"/></svg>
        <span>${t("layerStructure")}</span><span class="n">${esc(t("linesN")(count("structure")))}</span></label>
      <div class="g-note">${t("similarNote")}</div>
    </div>
    <div class="g-panel g-card" id="gcard"></div>
    <div class="g-hint">${t("graphHint")}</div></div>`;
  $("backBtn")?.addEventListener("click", () => go({ view: "graph" }));
  startGraph(G);
}

function startGraph(G) {
  const wrap = $("graphWrap"), cv = $("gc"), ctx = cv.getContext("2d");
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  const pal = PALETTE[dark ? "dark" : "light"];
  const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const s = gs = { alive: true, view: { x: 0, y: 0, k: 1 }, show: { declared: true, similar: true, structure: true },
    heat: 0, panV: null, hover: null, sel: null, drag: null, panning: null, W: 0, H: 0, DPR: 1 };
  const byId = new Map(G.nodes.map(n => [n.id, n]));
  const adj = new Map(G.nodes.map(n => [n.id, new Set()]));
  G.links = G.links.filter(l => byId.has(l.s) && byId.has(l.t));
  for (const l of G.links) { l.a = byId.get(l.s); l.b = byId.get(l.t); adj.get(l.s).add(l.t); adj.get(l.t).add(l.s); }
  const agents = [...new Set(G.nodes.filter(n => n.kind === "agent").map(n => n.agent))];
  const color = {};
  agents.forEach((a, i) => { color[a] = pal[i % pal.length]; });
  const tint = n => n.kind === "entity" ? css("--accent") : color[n.agent] || css("--faint");
  for (const n of G.nodes) n.deg = G.links.filter(l => l.kind === "similar" && (l.a === n || l.b === n)).length;
  // Start each agent's memories near its own angle (or, around a memory, by step),
  // so the layout settles in a few hundred steps.
  for (const n of G.nodes) {
    const i = Math.max(0, agents.indexOf(n.agent)), ang = (i / Math.max(1, agents.length)) * Math.PI * 2;
    const r = state.around && n.kind === "memory" ? n.hop * 90 : n.kind === "agent" ? 220 : 220 + (Math.random() - 0.5) * 160;
    const spin = state.around && n.kind === "memory" ? Math.random() * Math.PI * 2 : ang;
    n.x = Math.cos(spin) * r + (Math.random() - 0.5) * 60; n.y = Math.sin(spin) * r + (Math.random() - 0.5) * 60; n.vx = n.vy = 0;
  }
  const radius = n => n.kind === "agent" ? 7 : n.kind === "entity" ? 5 : 2.4 + Math.min(n.deg, 8) * 0.45 + (n.hop === 0 ? 2 : 0);

  function tick(alpha, friction = 0.6) {
    const N = G.nodes;
    for (let i = 0; i < N.length; i++) {
      const a = N[i];
      for (let j = i + 1; j < N.length; j++) {
        const b = N[j], dx = b.x - a.x, dy = b.y - a.y, d2 = dx * dx + dy * dy + 0.01;
        if (d2 > 90000) continue;
        const f = (a.kind === "agent" && b.kind === "agent" ? 2400 : 240) / d2 * alpha;
        a.vx -= dx * f; a.vy -= dy * f; b.vx += dx * f; b.vy += dy * f;
      }
    }
    for (const l of G.links) {
      const len = l.kind === "structure" ? 46 : 34, str = l.kind === "structure" ? 0.03 : l.kind === "similar" ? 0.05 + 0.1 * (l.w - 0.5) : 0.08;
      const dx = l.b.x - l.a.x, dy = l.b.y - l.a.y, d = Math.hypot(dx, dy) || 1, f = (d - len) / d * str * alpha;
      l.a.vx += dx * f; l.a.vy += dy * f; l.b.vx -= dx * f; l.b.vy -= dy * f;
    }
    for (const n of N) {
      n.vx -= n.x * 0.002 * alpha; n.vy -= n.y * 0.002 * alpha;
      if (n === s.drag) continue;
      const fr = n.kind === "agent" ? Math.min(friction, HUB_FRICTION) : friction;
      n.vx *= fr; n.vy *= fr; n.x += n.vx; n.y += n.vy;
    }
  }

  function resize() {
    const r = wrap.getBoundingClientRect();
    s.DPR = devicePixelRatio || 1; s.W = r.width; s.H = r.height;
    cv.width = s.W * s.DPR; cv.height = s.H * s.DPR;
    cv.style.width = s.W + "px"; cv.style.height = s.H + "px";
  }

  function fit() {
    const xs = G.nodes.map(n => n.x), ys = G.nodes.map(n => n.y);
    const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
    s.view.k = Math.min(2.4, s.W / (maxX - minX + 160), s.H / (maxY - minY + 160));
    s.view.x = s.W / 2 - (minX + maxX) / 2 * s.view.k; s.view.y = s.H / 2 - (minY + maxY) / 2 * s.view.k;
  }

  function draw() {
    const { view } = s;
    ctx.setTransform(s.DPR, 0, 0, s.DPR, 0, 0);
    ctx.fillStyle = css("--base"); ctx.fillRect(0, 0, s.W, s.H);
    ctx.setTransform(s.DPR * view.k, 0, 0, s.DPR * view.k, s.DPR * view.x, s.DPR * view.y);
    const focus = s.hover || s.sel, near = focus ? adj.get(focus.id) : null;
    const lit = n => !focus || n === focus || near.has(n.id);
    const ink = dark ? "255,255,255" : "20,24,30";
    // Lines are context, not the subject: thin and faint, tinted with their ends'
    // colours. Declared relations are drawn last, thicker, in the accent colour.
    for (const kind of ["structure", "similar", "declared"]) {
      if (!s.show[kind]) continue;
      for (const l of G.links) {
        if (l.kind !== kind) continue;
        const on = !focus || l.a === focus || l.b === focus;
        let alpha = kind === "structure" ? 0.1 : kind === "similar" ? 0.16 + 0.3 * (l.w - 0.5) : 0.9;
        let width = kind === "declared" ? 1.8 : kind === "similar" ? 0.9 : 0.6;
        if (focus && on) { alpha = kind === "structure" ? 0.45 : 0.85; width *= 1.4; } else if (focus) alpha *= 0.25;
        if (kind === "declared") ctx.strokeStyle = css("--accent");
        else if (kind === "similar") {
          const g = ctx.createLinearGradient(l.a.x, l.a.y, l.b.x, l.b.y);
          g.addColorStop(0, tint(l.a)); g.addColorStop(1, tint(l.b));
          ctx.strokeStyle = g;
        } else ctx.strokeStyle = `rgb(${ink})`;
        ctx.globalAlpha = alpha;
        ctx.lineWidth = width / Math.sqrt(view.k);
        ctx.beginPath(); ctx.moveTo(l.a.x, l.a.y); ctx.lineTo(l.b.x, l.b.y); ctx.stroke();
      }
    }
    for (const n of G.nodes) {
      const r = radius(n), col = tint(n);
      ctx.globalAlpha = lit(n) ? 1 : 0.15;
      ctx.shadowColor = col; ctx.shadowBlur = (n === focus ? 18 : n.kind === "agent" ? 14 : dark ? 5 : 0) * view.k;
      ctx.beginPath(); ctx.arc(n.x, n.y, n === focus ? r + 1.5 : r, 0, Math.PI * 2);
      if (n.kind === "entity") { ctx.fillStyle = css("--raised"); ctx.fill(); ctx.lineWidth = 2 / view.k; ctx.strokeStyle = col; ctx.stroke(); }
      else { ctx.fillStyle = col; ctx.fill(); }
      ctx.shadowBlur = 0;
      if (n === s.sel || n.hop === 0) { ctx.strokeStyle = css("--text"); ctx.lineWidth = 1.2 / view.k; ctx.beginPath(); ctx.arc(n.x, n.y, r + 4, 0, Math.PI * 2); ctx.stroke(); }
    }
    ctx.textAlign = "center"; ctx.textBaseline = "top";
    for (const n of G.nodes) {
      const named = n.kind !== "memory" || n === focus || (focus && near.has(n.id) && view.k > 1.4) || view.k > 2.6;
      if (!named) continue;
      ctx.globalAlpha = lit(n) ? 1 : 0.2;
      ctx.font = `${n.kind === "memory" ? 400 : 600} ${(n.kind === "memory" ? 10.5 : 12) / view.k}px system-ui, sans-serif`;
      ctx.fillStyle = n.kind === "memory" ? css("--muted") : css("--text");
      ctx.fillText(n.kind === "memory" ? day(n.timestamp) : n.label, n.x, n.y + radius(n) + 4 / view.k);
    }
    ctx.globalAlpha = 1;
  }

  function loop() {
    if (!s.alive) return;
    let moved = false;
    if (s.heat > 0.004) { tick(s.heat, FRICTION); s.heat *= 0.985; moved = true; }
    if (s.panV && !s.panning) {
      s.view.x += s.panV.x; s.view.y += s.panV.y; s.panV.x *= 0.92; s.panV.y *= 0.92;
      if (Math.hypot(s.panV.x, s.panV.y) < 0.05) s.panV = null;
      moved = true;
    }
    if (moved) draw();
    requestAnimationFrame(loop);
  }

  function select(n) {
    s.sel = n;
    const card = $("gcard");
    if (!n || n.kind === "agent") { card.classList.remove("open"); draw(); return; }
    if (n.kind === "entity") {
      card.innerHTML = `<div class="g-head"><span>${esc(n.label)}</span></div>
        <div class="g-near">${esc(t("mentionsN")([...adj.get(n.id)].filter(id => byId.get(id).kind === "memory").length))}</div>`;
    } else {
      card.innerHTML = `<div class="g-head"><span class="g-agent"><span class="g-dot"></span>${esc(n.agent)}</span><span>${esc(day(n.timestamp))} · mem:${n.mem}</span></div>
        <p>${esc(n.snippet)}</p><div class="g-near">${esc(t("similarN")(n.deg))}</div>
        <div class="actions"><button class="btn" id="gOpen">${t("openMemory")}</button>
        ${n.hop === 0 ? "" : `<button class="btn" id="gCenter">${t("centerHere")}</button>`}</div>`;
      card.querySelector(".g-dot").style.background = tint(n);
      $("gOpen").addEventListener("click", () => openDetail(n.mem).catch(fail));
      $("gCenter")?.addEventListener("click", () => go({ view: "graph", around: n.mem }));
    }
    card.classList.add("open");
    draw();
  }

  const at = e => { const r = cv.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
  const world = (x, y) => [(x - s.view.x) / s.view.k, (y - s.view.y) / s.view.k];
  function pick(x, y) {
    const [wx, wy] = world(x, y);
    let best = null, bd = 1e9;
    for (const n of G.nodes) { const d = Math.hypot(n.x - wx, n.y - wy); if (d < radius(n) + 6 / s.view.k && d < bd) { best = n; bd = d; } }
    return best;
  }
  cv.addEventListener("pointermove", e => {
    const [x, y] = at(e);
    if (s.drag) { [s.drag.x, s.drag.y] = world(x, y); s.drag.vx = s.drag.vy = 0; s.drag.moved = true; s.heat = Math.max(s.heat, 0.3); return; }
    if (s.panning) {
      const nx = x - s.panning.x, ny = y - s.panning.y;
      s.panning.v = { x: nx - s.view.x, y: ny - s.view.y }; s.view.x = nx; s.view.y = ny; draw(); return;
    }
    const h = pick(x, y);
    if (h !== s.hover) { s.hover = h; cv.classList.toggle("over", !!h); draw(); }
  });
  cv.addEventListener("pointerdown", e => {
    const [x, y] = at(e), n = pick(x, y);
    cv.setPointerCapture(e.pointerId);
    if (n) { s.drag = n; n.moved = false; s.heat = Math.max(s.heat, 0.2); }
    else { s.panV = null; s.panning = { x: x - s.view.x, y: y - s.view.y, sx: x, sy: y }; cv.classList.add("dragging"); }
  });
  cv.addEventListener("pointerup", e => {
    const [x, y] = at(e);
    // Let go of a point and it stays where it was put: no velocity is carried over.
    if (s.drag) { const n = s.drag; s.drag = null; n.vx = n.vy = 0; if (!n.moved) select(n === s.sel ? null : n); }
    else if (s.panning) {
      if (Math.hypot(x - s.panning.sx, y - s.panning.sy) < 3) select(null);
      else if (s.panning.v) s.panV = { ...s.panning.v };
      s.panning = null; cv.classList.remove("dragging");
    }
  });
  cv.addEventListener("wheel", e => {
    e.preventDefault();
    const [x, y] = at(e), f = Math.exp(-e.deltaY * 0.0015), [wx, wy] = world(x, y);
    s.view.k = Math.max(0.2, Math.min(8, s.view.k * f)); s.view.x = x - wx * s.view.k; s.view.y = y - wy * s.view.k; draw();
  }, { passive: false });
  for (const box of wrap.querySelectorAll("[data-layer]")) box.addEventListener("change", () => { s.show[box.dataset.layer] = box.checked; draw(); });
  s.observer = new ResizeObserver(() => { if (s.alive) { resize(); draw(); } });
  s.observer.observe(wrap);

  resize();
  // Fewer warm-up steps for a large graph: each step compares every pair of points.
  const warm = Math.max(60, Math.min(400, Math.round(120000 / Math.max(1, G.nodes.length))));
  for (let i = 0; i < warm; i++) tick(1 - i / warm);
  fit();
  draw();
  requestAnimationFrame(loop);
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
    ...["overview", "memories", "graph", "episodes", "entities"].map(v => ({ label: t("cmdGo")(t(v)), side: t("cmd"), run: () => go({ view: v, path: [], q: "", month: "" }) })),
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
// A move that does not name a memory to center on leaves the "around" view.
function go(next) { Object.assign(state, { around: 0 }, next); closeDetail(); return route().catch(fail); }
async function route() {
  for (const b of document.querySelectorAll(".nav")) {
    if (b.dataset.view === state.view) b.setAttribute("aria-current", "page");
    else b.removeAttribute("aria-current");
  }
  saveHash();
  renderTree().catch(() => {});
  stopGraph();
  if (state.view === "overview") renderOverview();
  else if (state.view === "memories") await renderMemories();
  else if (state.view === "graph") await renderGraph();
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
