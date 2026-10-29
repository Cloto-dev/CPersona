// CPersona dashboard. Reads only. Sizes are set through element.style after the
// markup is inserted, because the CSP refuses inline style and script attributes.

const T = {
  en: {
    overview: "Overview", memories: "Memories", episodes: "Episodes", entities: "Entities", browse: "Browse",
    techAlways: "Always show details",
    calmDots: "Avoid clusters of dots", calmDotsHint: "Draws the network with lines, without bright clusters of dots.",
    sideSearch: "Search", settings: "Settings", back: "Back", forward: "Forward", newTab: "New tab", closeTab: "Close tab",
    layout: "Layout", layoutClassic: "Standard", layoutTabs: "Tabbed", language: "Language",
    layoutDesc: "Standard shows one view at a time and opens a memory beside the list. Tabbed adds a strip of view icons and opens each memory in a tab.",
    techAlwaysDesc: "Show a memory's technical details opened, without a click.",
    signOut: "Sign out", signOutDesc: "Signs this browser out. To open the dashboard again, ask your agent for a new link.",
    signedOut: "Signed out. To open the dashboard again, ask your agent for a new link.",
    searchHits: n => `${n.toLocaleString()} result${n === 1 ? "" : "s"}`, searchMore: n => `${n} more: refine the words`,
    sideSearchPh: "Type to start searching…", matchCase: "Match case", searchSettings: "Search settings", searchOptions: "Search options",
    searchOps: [["path:", "match the agent / project / channel"], ["agent:", "match the agent's name"], ["tag:", "memories that mention an entity"],
      ["line:", "find the words on the same line"], ["month:", "match the month it was stored (2026-10)"], ["[property]", "match a property: [locked] [recalled] [vector] [project:x]"]],
    searchHelpNote: "Words are all required. \"Quote\" a phrase; -word leaves it out.",
    sortBy: "Sort", sorts: { new: "Newest first", old: "Oldest first", recalled: "Most recalled" }, showLines: "Show matching lines",
    noHits: "No memory matches.",
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
    noToken: "Open the dashboard again from the address it printed, or ask your agent for a new link.",
    linkUsed: "This link has been used or has expired. Ask your agent to open the dashboard again.",
    graph: "Network",
    graphLead: "The memories of this folder, joined by what they have in common.",
    nGraph: (shown, total) => shown < total ? `The newest ${shown.toLocaleString()} of ${total.toLocaleString()} memories` : `${total.toLocaleString()} memories`,
    noVectorN: n => `${n.toLocaleString()} without a search vector have no similar lines.`,
    aroundTitle: id => `Around mem:${id}`,
    aroundLead: (k, hops) => `From the memory you chose, its ${k} most similar memories, and theirs, ${hops} steps out.`,
    aroundNoVector: "This memory has no search vector, so similar memories cannot be found from it.",
    backToMap: "Back to the whole picture",
    layer: { declared: "Declared relations", similar: "Similar content (computed)", structure: "Grouped by agent",
      part: "Folder and subfolder", co: "Mentioned in the same memory", near: "Groups with close centres (computed)" },
    linesN: n => `${n}`,
    colourByAgent: "Colour by agent", display: "Display",
    layerNote: {
      memories: "Similar lines are computed from the stored embeddings. No agent declared them.",
      entities: "Entities are names agents declared. A “mentioned in the same memory” line joins two entities one memory mentions; the thicker it is, the more such memories.",
      clusters: "Groups are computed from the stored embeddings. No agent made them, and they can change as memories are added.",
    },
    networkLead: "Each point is a memory, gathered around its agent. Lines join memories with similar content.",
    tooMany: (n, max) => `${n.toLocaleString()} memories have vectors, more than can be grouped (${max.toLocaleString()}). Larger stores will be grouped once the time it takes has been measured.`,
    clusterTitle: "Similar group", entityTitle: name => `Memories that mention ${name}`, memoriesN: n => `${n.toLocaleString()} memories`,
    openGroup: "Open this folder's network", openCluster: "See this group's memories", openEntity: "See the memories that mention it",
    clusterQuoteHint: "Named by the memory nearest the group's centre.",
    graphHint: "Drag the background to move · Scroll to zoom · Drag a point to move it · Click a point to see it",
    similarN: n => `Similar memories: ${n}`, mentionsN: n => `Mentioned in ${n} memories`,
    openMemory: "Open", centerHere: "Center on this memory", showAround: "See it in the network",
  },
  ja: {
    overview: "概要", memories: "記憶", episodes: "エピソード", entities: "実体", browse: "たどる",
    techAlways: "詳細情報を常に開く",
    calmDots: "集合体回避モード", calmDotsHint: "点の集まりを目立たせず、線で描きます。",
    sideSearch: "検索", settings: "設定", back: "戻る", forward: "進む", newTab: "新しいタブ", closeTab: "タブを閉じる",
    layout: "表示の方式", layoutClassic: "標準", layoutTabs: "タブ表示", language: "言語",
    layoutDesc: "標準は、画面を 1 つずつ表示し、記憶の詳細を一覧の横に開きます。タブ表示は、左端にアイコンの列を置き、記憶を 1 件ずつタブで開きます。",
    techAlwaysDesc: "記憶の技術的な情報を、クリックしなくても開いた状態で表示します。",
    signOut: "ログアウト", signOutDesc: "このブラウザからログアウトします。もう一度開くには、エージェントに新しいリンクを頼んでください。",
    signedOut: "ログアウトしました。もう一度開くには、エージェントに新しいリンクを頼んでください。",
    searchHits: n => `${n.toLocaleString()} 件の結果`, searchMore: n => `ほかに ${n} 件あります。語を足して絞ってください`,
    sideSearchPh: "入力して検索を開始...", matchCase: "大文字と小文字を区別", searchSettings: "検索の設定", searchOptions: "検索オプション",
    searchOps: [["path:", "エージェント / プロジェクト / チャンネルに一致"], ["agent:", "エージェント名に一致"], ["tag:", "実体に言及している記憶を検索"],
      ["line:", "同一の行でキーワードを検索"], ["month:", "記録した月に一致 (2026-10)"], ["[property]", "属性に一致: [locked] [recalled] [vector] [project:x]"]],
    searchHelpNote: "語はすべて含むものを探します。\"語句\" で 1 つの語句、-語 で除外します。",
    sortBy: "並び順", sorts: { new: "新しい順", old: "古い順", recalled: "想起回数の多い順" }, showLines: "一致した行を表示",
    noHits: "一致する記憶はありません。",
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
    noToken: "表示されたアドレスから開き直すか、エージェントに新しいリンクを頼んでください。",
    linkUsed: "このリンクは使用済みか、期限が切れています。エージェントに、ダッシュボードを開き直すよう頼んでください。",
    graph: "記憶ネットワーク",
    graphLead: "このフォルダの記憶を、共通点の線で結んで表示します。",
    nGraph: (shown, total) => shown < total ? `${total.toLocaleString()} 件のうち、新しい ${shown.toLocaleString()} 件を表示しています` : `${total.toLocaleString()} 件の記憶`,
    noVectorN: n => `検索用のベクトルがない ${n.toLocaleString()} 件には、「内容が似ている」線がありません。`,
    aroundTitle: id => `mem:${id} の周り`,
    aroundLead: (k, hops) => `選んだ記憶から ${hops} 段先まで、それぞれ似ている記憶を ${k} 件ずつたどっています。`,
    aroundNoVector: "この記憶には検索用のベクトルがないので、似ている記憶をたどれません。",
    backToMap: "全体の図に戻る",
    layer: { declared: "宣言した関係", similar: "内容が似ている (自動)", structure: "エージェントごとのまとまり",
      part: "フォルダの親子", co: "同じ記憶で言及", near: "中心が近いまとまり (自動)" },
    linesN: n => `${n} 本`,
    colourByAgent: "エージェントで色分け", display: "表示",
    layerNote: {
      memories: "「内容が似ている」線は、保存された埋め込みから計算したつながりです。エージェントが宣言した関係ではありません。",
      entities: "実体は、エージェントが宣言した名前です。「同じ記憶で言及」線は、1 件の記憶が両方に言及していることを表します。線が太いほど、そうした記憶が多くあります。",
      clusters: "まとまりは、保存された埋め込みから計算したものです。エージェントが分けたものではありません。記憶が増えると、分け方が変わることがあります。",
    },
    networkLead: "記憶を点にして、エージェントのまわりに集めています。線は、内容が似ている記憶どうしを結んでいます。",
    tooMany: (n, max) => `ベクトルを持つ記憶が ${n.toLocaleString()} 件あり、まとめられる上限 (${max.toLocaleString()} 件) を超えています。この規模での計算時間を測ってから対応します。`,
    clusterTitle: "内容の近いまとまり", entityTitle: name => `${name} に言及している記憶`, memoriesN: n => `${n.toLocaleString()} 件の記憶`,
    openGroup: "このフォルダの図を開く", openCluster: "このまとまりの記憶を図で見る", openEntity: "言及している記憶を図で見る",
    clusterQuoteHint: "名前は、中心にいちばん近い記憶の冒頭です。",
    graphHint: "背景をドラッグで移動 · ホイールで拡大縮小 · 点をつまんで動かす · 点をクリックで内容",
    similarN: n => `内容が似ている記憶: ${n} 件`, mentionsN: n => `${n} 件の記憶で言及されています`,
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

const state = { view: "overview", path: [], q: "", id: 0, month: "", around: 0, cluster: 0, entity: 0, lang: "en" };
// The layout this browser chose in the settings; the classic one unless it chose tabs.
let layout = "classic";
try { if (localStorage.getItem("cp.layout") === "tabs") layout = "tabs"; } catch {}
document.body.dataset.layout = layout;
for (const k of ["view", "q", "month"]) if (H0.get(k)) state[k] = H0.get(k);
if (H0.get("id")) state.id = Number(H0.get("id")) || 0;
for (const k of ["around", "cluster", "entity"]) if (H0.get(k)) state[k] = Number(H0.get(k)) || 0;
try { if (H0.get("path")) state.path = JSON.parse(H0.get("path")); } catch {}
try { state.lang = localStorage.getItem("cp.lang") || (navigator.language || "").startsWith("ja") && "ja" || "en"; } catch {}
if (H0.get("lang")) state.lang = H0.get("lang");
// Avoid clusters of dots: dense fields of bright dots can be distressing to look at, so
// this draws the network with its lines as the subject instead. Kept in this browser.
let calmDots = false;
try { calmDots = localStorage.getItem("cp.calmDots") === "1"; } catch {}
if (H0.get("calm") === "1") calmDots = true;
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

// Relative, so the same page works at the loopback root and under /dashboard/ on a
// remote server; there the session cookie, not a token, carries the access.
async function api(path, params = {}) {
  const r = await fetch(path.replace(/^\//, "") + "?" + new URLSearchParams(params), token ? { headers: { Authorization: `Bearer ${token}` } } : {});
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
  for (const k of ["around", "cluster", "entity"]) if (state[k]) h.set(k, state[k]);
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
  for (const el of document.querySelectorAll("[data-title-t]")) {
    el.title = t(el.dataset.titleT);
    if (el.tagName === "BUTTON") el.setAttribute("aria-label", t(el.dataset.titleT));
  }
  $("sideQ").placeholder = t("sideSearchPh");
  $("cnavQ").placeholder = t("searchPh");
  $("cnavQ").setAttribute("aria-label", t("searchPh"));
  for (const b of document.querySelectorAll("[data-lang]")) b.setAttribute("aria-pressed", String(b.dataset.lang === state.lang));
  drawLayoutChoice();
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
    const on = (state.view === "memories" || state.view === "graph" && !state.around && !state.cluster && !state.entity) && JSON.stringify(state.path) === JSON.stringify(p);
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
    const r = await fetch("api/health", token ? { headers: { Authorization: `Bearer ${token}` } } : {});
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

// ---- A memory in full
// Each part sits where its characters are in the text, so the bar never grows past
// its width however many parts there are.
function spans(parts, total, kind) {
  return parts.map(p => `<button class="part ${kind}" data-part data-kind="${kind}" data-i="${p.i}"
    data-left="${(100 * p.start) / total}" data-w="${(100 * Math.max(p.end - p.start, 1)) / total}"
    aria-label="${esc(t(kind))} ${p.i + 1}"></button>`).join("");
}

// A memory opens in a tab of its own, as a note does in a notes app; a tab already
// showing it is brought forward instead of opening another.
async function openDetail(id) {
  // The classic layout opens a memory in a drawer over the list, so the list stays.
  if (layout === "classic") {
    state.id = id;
    saveHash();
    keepPlace();
    for (const b of document.querySelectorAll(".row")) b.classList.toggle("sel", Number(b.dataset.id) === id);
    await renderDetail(id, $("drawer"));
    $("drawer").classList.add("open");
    return;
  }
  const at = tabs.findIndex(tb => { const c = tb.hist[tb.pos]; return c.view === "memory" && c.id === id; });
  if (at >= 0) return switchTab(at);
  return openTab({ view: "memory", id, q: state.q });
}

async function renderDetail(id, d = $("page")) {
  const m = await api("/api/memory", { id });
  let src = m.source;
  try { const s = JSON.parse(m.source); src = [s.type, s.name || s.id].filter(Boolean).join(" · "); } catch {}
  const total = Math.max(1, m.content.length);
  const nodes = m.nodes.map(n => ({ i: n.node_index, start: n.start_char, end: n.end_char }));
  const blocks = m.blocks.map(b => ({ i: b.block_index, start: b.start_char, end: b.end_char }));
  memTitles[id] = `${day(m.timestamp)} · ${m.agent_id}`;
  renderTabs();
  d.innerHTML = `<article class="doc">
    <div class="top"><h2>${esc(day(m.timestamp))} · ${esc(m.agent_id)}</h2>${d.id === "drawer" ? `<button class="x" id="closeBtn" aria-label="${t("close")}">×</button>` : ""}</div>
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
    </details></article>`;
  sizes(d);
  $("closeBtn")?.addEventListener("click", closeDetail);
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
  for (const b of document.querySelectorAll(".row.sel")) b.classList.remove("sel");
  if (!$("drawer").classList.contains("open")) return;
  $("drawer").classList.remove("open");
  state.id = 0;
  saveHash();
  keepPlace();
}

// ---- Settings
let settingsFrom = null;
function openSettings() {
  settingsFrom = document.activeElement;
  $("settingsBg").hidden = false;
  $("setClose").focus();
}
function closeSettings() {
  if ($("settingsBg").hidden) return;
  $("settingsBg").hidden = true;
  settingsFrom?.focus?.();
}
function drawLayoutChoice() {
  for (const b of document.querySelectorAll("[data-layout]")) b.setAttribute("aria-pressed", String(b.dataset.layout === layout));
  $("layoutSeg").setAttribute("aria-label", t("layout"));
}
// Switching keeps what is open: a memory in a tab moves to the drawer over its list,
// and a memory in the drawer moves to a tab.
async function setLayout(l) {
  if (l === layout) return;
  const inTab = state.view === "memory" ? state.id : 0;
  const inDrawer = $("drawer").classList.contains("open") ? state.id : 0;
  closeDetail();
  layout = l;
  document.body.dataset.layout = l;
  try { localStorage.setItem("cp.layout", l); } catch {}
  drawLayoutChoice();
  if (l === "classic") {
    sideShow("tree");
    if (inTab) { await go({ view: "memories", path: [], month: "" }); await openDetail(inTab); }
  } else if (inDrawer) await openDetail(inDrawer);
}
// Signing out reloads the page, so nothing read before stays on the screen.
async function signOut() {
  await fetch("api/logout").catch(() => {});
  try { sessionStorage.removeItem("cp.tabs"); } catch {}
  location.replace(location.pathname + "#signedout=1");
  location.reload();
}

// ---- Memory network
// Laid out by a small force simulation drawn on a canvas, so nothing is loaded from
// elsewhere. The simulation keeps running while the graph is touched and cools back
// to stillness: points keep some momentum (friction 0.86), while a hub, pulled by
// everything tied to it, is damped to 0.4 so it does not overshoot.
const PALETTE = {
  light: ["#3f6fd8", "#d9733f", "#2f9e7a", "#b8509c", "#8a6bd1", "#c49a2c", "#3a9fb5", "#d0545a", "#6b8f3a", "#7d7f86"],
  dark: ["#6f9af2", "#f0915e", "#4cc39b", "#d978bd", "#a990ee", "#e2bb52", "#5cc2d8", "#ef7a7f", "#93b85d", "#a3a6ad"],
};
const FRICTION = 0.86, HUB_FRICTION = 0.4;
// The lines each kind of picture can show, in the order the legend lists them.
const LAYERS = {
  memories: ["declared", "similar", "structure"],
  tree: ["part"],
  entities: ["declared", "co"],
  clusters: ["near"],
};
let gs = null; // the graph on screen, or null

// A number in [0, 1) from a string (FNV-1a), so a point's first place comes from its
// id and the same graph lays out the same way on every load.
function hash01(s) {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 0x01000193);
  return (h >>> 0) / 4294967296;
}

// Where each point of a picture was left, per store and picture, so a reload, or a
// memory added since, does not move the points already seen. Kept in this browser
// only, for the pictures opened most recently.
const LAYOUT_KEY = "cp.layout", LAYOUT_KEEP = 20;
function layoutKey() {
  const where = state.around ? `a:${state.around}` : state.cluster ? `c:${state.cluster}`
    : state.entity ? `e:${state.entity}` : state.path.length || state.month ? `f:${JSON.stringify(state.path)}:${state.month}`
    : "map:memories"; // the key it had while other pictures existed, so saved places stay
  return `${overviewData.db}|${where}`;
}
function loadLayout(key) {
  try { return JSON.parse(localStorage.getItem(LAYOUT_KEY) || "{}")[key]?.pos || null; } catch { return null; }
}
function saveLayout(key, nodes) {
  const entry = { t: Date.now(), pos: Object.fromEntries(nodes.map(n => [n.id, [Math.round(n.x * 10) / 10, Math.round(n.y * 10) / 10]])) };
  try {
    const all = JSON.parse(localStorage.getItem(LAYOUT_KEY) || "{}");
    all[key] = entry;
    const keep = Object.keys(all).sort((a, b) => all[b].t - all[a].t).slice(0, LAYOUT_KEEP);
    localStorage.setItem(LAYOUT_KEY, JSON.stringify(Object.fromEntries(keep.map(k => [k, all[k]]))));
  } catch {
    // Past the browser's quota (a picture of every memory is large): keep this one alone.
    try { localStorage.setItem(LAYOUT_KEY, JSON.stringify({ [key]: entry })); } catch {}
  }
}

function stopGraph() {
  if (gs) { gs.alive = false; gs.observer?.disconnect(); }
  gs = null;
}

function layerRow(kind, n, on = true) {
  const swatch = kind === "similar" || kind === "near"
    ? `<defs><linearGradient id="lg-${kind}" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="34" y2="0"><stop offset="0" class="s-a"/><stop offset="1" class="s-b"/></linearGradient></defs><line x1="0" y1="5" x2="34" y2="5" class="l-similar l-${kind}"/>`
    : `<line x1="0" y1="5" x2="34" y2="5" class="l-${kind}"/>`;
  return `<label class="layer"><input type="checkbox" data-layer="${kind}"${on ? " checked" : ""}>
    <svg width="34" height="10" aria-hidden="true">${swatch}</svg>
    <span>${t("layer")[kind]}</span><span class="n">${esc(t("linesN")(n))}</span></label>`;
}

// The lines from each agent to its memories start hidden in any picture of more than a
// thousand points, where an agent of thousands turns them into a burst that hides the
// rest; where a memory sits already says whose it is.
const startsHidden = (k, G) => k === "structure" && (G.nodes || []).length > 1000;

async function renderGraph() {
  const page = $("page");
  if (!overviewData.counts.memories) return renderEmptyMemories();
  // The network is always of memories: the whole store, or the part a link narrowed it to.
  const memoryView = state.around || state.cluster || state.entity || state.path.length || state.month;
  const G = state.around ? await api("/api/graph", { around: state.around })
    : state.cluster ? await api("/api/graph", { cluster_of: state.cluster })
    : state.entity ? await api("/api/graph", { entity: state.entity })
    : await api("/api/graph", { path: JSON.stringify(state.path), month: state.month });
  const lens = "memories";
  const back = `<div class="crumbs"><button id="backBtn">${t("backToMap")}</button></div>`;
  const shown = G.shown != null ? `<span class="hint">${esc(t("nGraph")(G.shown, G.total))}${G.without_vector ? "<br>" + esc(t("noVectorN")(G.without_vector)) : ""}</span>` : "";
  let head;
  if (state.around) head = `<h1>${esc(t("aroundTitle")(G.center))}</h1>
       <p class="lead">${G.has_vector ? esc(t("aroundLead")(G.k, G.hops)) : esc(t("aroundNoVector"))}</p>${back}`;
  else if (state.cluster) head = `<h1>${t("clusterTitle")}</h1><p class="lead">“${esc(G.label)}”<br>${shown}</p>${back}`;
  else if (state.entity) head = `<h1>${esc(t("entityTitle")(G.label))}</h1><p class="lead">${shown}</p>${back}`;
  else if (memoryView) head = `<h1>${t("graph")}</h1><p class="lead">${t("graphLead")}<br>${shown}</p>${back}`;
  else head = `<h1>${t("graph")}</h1><p class="lead">${t("networkLead")}<br>${shown}</p>`;
  const count = k => G.links ? G.links.filter(l => l.kind === k).length : 0;
  const note = t("layerNote")[lens];
  // The picture takes the whole pane; what to show sits behind the gear at the top
  // right, as in a notes app's graph view, and what the lines mean stays on screen.
  const keyRows = G.nodes ? LAYERS[lens].filter(k => count(k)).map(k =>
    `<div class="k-row"><svg width="22" height="8" aria-hidden="true"><line x1="0" y1="4" x2="22" y2="4" class="k-${k}"/></svg><span>${t("layer")[k]}</span></div>`).join("") : "";
  const hint = t("graphHint").split(" · ").map(h => `<span>${h}</span>`).join("");
  page.innerHTML = `<div class="graph whole" id="graphWrap"><canvas id="gc"></canvas>
    <div class="g-top">${head}</div>
    <button class="g-gear" id="gGear" aria-expanded="false" aria-controls="gSet" aria-label="${t("display")}" title="${t("display")}"><svg aria-hidden="true" width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="10" cy="10" r="2.5"/><path d="M10 2.5v2M10 15.5v2M2.5 10h2M15.5 10h2M4.7 4.7l1.4 1.4M13.9 13.9l1.4 1.4M4.7 15.3l1.4-1.4M13.9 6.1l1.4-1.4"/></svg></button>
    <div class="g-panel g-set" id="gSet">
      ${G.nodes ? LAYERS[lens].map(k => layerRow(k, count(k), !startsHidden(k, G))).join("") : ""}
      ${G.nodes ? `<label class="layer"><input type="checkbox" id="byAgent"><svg width="34" height="10" aria-hidden="true"></svg><span>${t("colourByAgent")}</span></label>` : ""}
    </div>
    <div class="g-panel g-card" id="gcard"></div>
    ${keyRows ? `<div class="g-key">${keyRows}${note ? `<p>${note}</p>` : ""}</div>` : ""}
    <div class="g-hint">${hint}</div></div>`;
  const lead = page.querySelector(".g-top .lead");
  if (lead) $("gSet").prepend(lead);
  $("gGear").addEventListener("click", () => { const open = $("gSet").classList.toggle("open"); $("gGear").setAttribute("aria-expanded", open); });
  $("backBtn")?.addEventListener("click", () => go({ view: "graph", path: [], month: "" }));
  // A lens with nothing to draw says why, instead of an empty canvas.
  const empty = G.too_many ? t("tooMany")(G.too_many, G.max_rows)
    : lens === "entities" && !G.nodes.length ? `${t("emptyEntitiesP")} ${t("tryAsk")} ${t("askEntities")}`
    : G.nodes && !G.nodes.length ? t("noMemories") : "";
  if (empty) { $("gc").remove(); page.querySelector(".g-hint").remove(); $("graphWrap").insertAdjacentHTML("beforeend", `<div class="g-empty"><p>${esc(empty)}</p></div>`); return; }
  startGraph(G, lens);
}

function groupLabel(n) {
  if (n.level === "agent") return n.label || n.key;
  if (n.level === "project") return n.key || t("global");
  if (n.level === "channel") return n.key || t("noChannel");
  return n.key;
}

// "agent / project / channel / 2026-10", each part as the sidebar names it.
function groupPath(n) {
  const p = n.path, parts = [p[0]];
  if (p.length > 1) parts.push(levelLabel("project", p[1]));
  if (p.length > 2) parts.push(levelLabel("channel", p[2]));
  if (p.length > 4) parts.push(`${p[3]}-${p[4]}`);
  return parts.join(" / ");
}

function startGraph(G, lens) {
  const wrap = $("graphWrap"), cv = $("gc"), ctx = cv.getContext("2d");
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  const pal = PALETTE[dark ? "dark" : "light"];
  const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const s = gs = { alive: true, view: { x: 0, y: 0, k: 1 }, show: Object.fromEntries(LAYERS[lens].map(k => [k, !startsHidden(k, G)])),
    heat: 0, pin: false, panV: null, hover: null, sel: null, drag: null, panning: null, W: 0, H: 0, DPR: 1 };
  const byId = new Map(G.nodes.map(n => [n.id, n]));
  const adj = new Map(G.nodes.map(n => [n.id, new Set()]));
  G.links = G.links.filter(l => byId.has(l.s) && byId.has(l.t));
  for (const l of G.links) { l.a = byId.get(l.s); l.b = byId.get(l.t); adj.get(l.s).add(l.t); adj.get(l.t).add(l.s); }
  const agents = [...new Set(G.nodes.map(n => n.agent).filter(Boolean))].sort();
  const color = {};
  agents.forEach((a, i) => { color[a] = pal[i % pal.length]; });
  const tint = n => n.kind === "entity" ? css("--accent") : color[n.agent] || css("--faint");
  const counted = G.nodes.filter(n => n.count != null);
  const maxCount = Math.max(1, ...counted.map(n => n.count));
  // Counted in one pass over the lines: per point it would be points × lines.
  for (const n of G.nodes) n.deg = 0;
  for (const l of G.links) if (l.kind === "similar") { l.a.deg++; if (l.b !== l.a) l.b.deg++; }
  for (const n of G.nodes) {
    if (n.kind === "group") n.name = groupLabel(n);
    else if (n.kind === "cluster") n.name = n.label.length > 24 ? n.label.slice(0, 24) + "…" : n.label;
    else if (n.kind === "project") n.name = n.label || t("global");
    else n.name = n.label;
  }
  // Small, nearly even dots, a little larger the more they hold.
  const radius = n => hub(n) ? 5.5 : n.count != null ? 2.6 + 2.4 * Math.sqrt(n.count / maxCount) : 2.4 + Math.min(2, Math.sqrt(adj.get(n.id).size) * 0.45) + (n.hop === 0 ? 1 : 0);
  const hub = n => n.kind === "agent" || n.kind === "group" && n.level === "agent";
  // A point seen before starts where it was left. A new one starts beside the seen
  // points it is tied to, or else near its agent's angle (around a memory, by step),
  // so the layout settles in a few hundred steps.
  const key = layoutKey(), saved = loadLayout(key) || {};
  const rnd = (n, salt) => hash01(`${n.id}:${salt}`);
  for (const n of G.nodes) {
    n.vx = n.vy = 0;
    if (saved[n.id]) { [n.x, n.y] = saved[n.id]; n.kept = true; continue; }
    const i = Math.max(0, agents.indexOf(n.agent)), ang = (i / Math.max(1, agents.length)) * Math.PI * 2;
    const depth = n.kind === "group" ? n.path.length : 0;
    const r = state.around && n.kind === "memory" ? n.hop * 90 : hub(n) ? 220 : 220 + depth * 30 + (rnd(n, "r") - 0.5) * 160;
    const spin = state.around && n.kind === "memory" || lens === "clusters" || lens === "entities" ? rnd(n, "a") * Math.PI * 2 : ang;
    n.x = Math.cos(spin) * r + (rnd(n, "x") - 0.5) * 60; n.y = Math.sin(spin) * r + (rnd(n, "y") - 0.5) * 60;
  }
  for (const n of G.nodes) {
    if (n.kept) continue;
    const seen = [...adj.get(n.id)].map(id => byId.get(id)).filter(m => m.kept);
    if (!seen.length) continue;
    n.x = seen.reduce((a, m) => a + m.x, 0) / seen.length + (rnd(n, "x") - 0.5) * 40;
    n.y = seen.reduce((a, m) => a + m.y, 0) / seen.length + (rnd(n, "y") - 0.5) * 40;
  }
  const kept = G.nodes.filter(n => n.kept).length;

  // Many-body push by Barnes–Hut (θ = 0.9): a cell far enough away pushes as one point.
  function quadTree(N, chargeOf) {
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const n of N) { x0 = Math.min(x0, n.x); y0 = Math.min(y0, n.y); x1 = Math.max(x1, n.x); y1 = Math.max(y1, n.y); }
    const root = { x0, y0, s: Math.max(x1 - x0, y1 - y0, 1) + 1, kids: null, pts: [] };
    const insert = (q, n, depth) => {
      if (!q.kids) {
        if (!q.pts.length || depth > 32) { q.pts.push(n); return; }
        const old = q.pts; q.pts = []; q.kids = [null, null, null, null];
        for (const o of old) put(q, o, depth);
      }
      put(q, n, depth);
    };
    const put = (q, n, depth) => {
      const h = q.s / 2, i = (n.x >= q.x0 + h ? 1 : 0) + (n.y >= q.y0 + h ? 2 : 0);
      q.kids[i] ||= { x0: q.x0 + (i & 1) * h, y0: q.y0 + (i >> 1) * h, s: h, kids: null, pts: [] };
      insert(q.kids[i], n, depth + 1);
    };
    const sum = q => {
      let m = 0, cx = 0, cy = 0;
      if (q.kids) for (const k of q.kids) { if (!k) continue; sum(k); m += k.m; cx += k.cx * k.m; cy += k.cy * k.m; }
      else for (const n of q.pts) { const c = chargeOf(n); m += c; cx += n.x * c; cy += n.y * c; }
      q.m = m; q.cx = m ? cx / m : q.x0; q.cy = m ? cy / m : q.y0;
    };
    for (const n of N) insert(root, n, 0);
    sum(root);
    return root;
  }
  // scale: how strongly n itself answers; the push grows with both points' sizes.
  function repel(q, n, alpha, chargeOf, scale) {
    if (!q || !q.m) return;
    const dx = q.cx - n.x, dy = q.cy - n.y;
    let l = dx * dx + dy * dy;
    if (q.kids) {
      if (q.s * q.s / 0.81 >= l) { for (const k of q.kids) repel(k, n, alpha, chargeOf, scale); return; }
    } else {
      for (const o of q.pts) {
        if (o === n) continue;
        const ex = o.x - n.x, ey = o.y - n.y, e = Math.max(1, ex * ex + ey * ey), c = chargeOf(o) * scale;
        n.vx += ex * c * alpha / e; n.vy += ey * c * alpha / e;
      }
      return;
    }
    if (l < 1) l = 1;
    n.vx += dx * q.m * scale * alpha / l; n.vy += dy * q.m * scale * alpha / l;
  }
  s.autoFit = false; s.byAgent = false;
  const SPRING = { structure: [46, 0.03], part: [36, 0.06], near: [70, 0.03], co: [60, 0.05], declared: [34, 0.08] };

  // pin: points seen before hold still, so only the new ones find their place.
  // Past BH_FROM points the push is summed by Barnes–Hut instead of pair by pair: the
  // same push, as a product of the two points' sizes, in n log n rather than n².
  const BH_FROM = 1000;
  const push = n => Math.sqrt(240) * (0.6 + radius(n) / 8);
  const pushCharge = n => -push(n);
  function tick(alpha, friction = 0.6, pin = false) {
    const N = G.nodes;
    if (N.length > BH_FROM) { const tree = quadTree(N, pushCharge); for (const n of N) repel(tree, n, alpha, pushCharge, push(n)); }
    else for (let i = 0; i < N.length; i++) {
      const a = N[i], ra = radius(a);
      for (let j = i + 1; j < N.length; j++) {
        const b = N[j], dx = b.x - a.x, dy = b.y - a.y, d2 = dx * dx + dy * dy + 0.01;
        if (d2 > 90000) continue;
        // Larger points push harder, so a big group keeps room around it.
        const base = hub(a) && hub(b) ? 2400 : 240;
        const f = base * (0.6 + ra / 8) * (0.6 + radius(b) / 8) / d2 * alpha;
        a.vx -= dx * f; a.vy -= dy * f; b.vx += dx * f; b.vy += dy * f;
      }
    }
    for (const l of G.links) {
      const [len0, str0] = SPRING[l.kind] || [34, 0.05 + 0.1 * (l.w - 0.5)];
      const len = len0 + radius(l.a) + radius(l.b), str = str0;
      const dx = l.b.x - l.a.x, dy = l.b.y - l.a.y, d = Math.hypot(dx, dy) || 1, f = (d - len) / d * str * alpha;
      // The busier end yields: a point tied to hundreds of others would otherwise take
      // the pull of every spring at once, overshoot, and fly apart. Ends with as many
      // ties as each other pull as hard as before.
      const ca = adj.get(l.a.id).size || 1, cb = adj.get(l.b.id).size || 1;
      const fa = f * Math.min(1, 2 * cb / (ca + cb)), fb = f * Math.min(1, 2 * ca / (ca + cb));
      l.a.vx += dx * fa; l.a.vy += dy * fa; l.b.vx -= dx * fb; l.b.vy -= dy * fb;
    }
    for (const n of N) {
      n.vx -= n.x * 0.002 * alpha; n.vy -= n.y * 0.002 * alpha;
      if (n === s.drag) continue;
      if (pin && n.kept) { n.vx = n.vy = 0; continue; }
      const fr = hub(n) || n.kind === "project" ? Math.min(friction, HUB_FRICTION) : friction;
      n.vx *= fr; n.vy *= fr; n.x += n.vx; n.y += n.vy;
    }
  }

  function resize() {
    const r = wrap.getBoundingClientRect();
    s.DPR = devicePixelRatio || 1; s.W = r.width; s.H = r.height;
    cv.width = s.W * s.DPR; cv.height = s.H * s.DPR;
    cv.style.width = s.W + "px"; cv.style.height = s.H + "px";
  }

  function fitView() {
    const xs = G.nodes.map(n => n.x), ys = G.nodes.map(n => n.y);
    const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
    const k = Math.min(2.4, s.W / (maxX - minX + 160), s.H / (maxY - minY + 160));
    return { k, x: s.W / 2 - (minX + maxX) / 2 * k, y: s.H / 2 - (minY + maxY) / 2 * k };
  }
  function fit() { Object.assign(s.view, fitView()); }

  // A constellation on a flat night colour: memories are small plain dots, an agent a
  // flat dot with a figure of its own, and colour marks only what is pointed at (or
  // each agent, when asked for). Names fade in as the view comes closer.
  const NIGHT = "#0f131c";
  // The page's own stack: its Japanese faces draw "…" as centred dots.
  const FONT = 'system-ui, -apple-system, "Segoe UI", "Hiragino Sans", "Yu Gothic UI", "Noto Sans JP", sans-serif';
  const hsl = (h, sat, l, a = 1) => `hsla(${((h % 360) + 360) % 360},${sat}%,${l}%,${a})`;
  // Ids that differ in one character hash to nearby values, so a colour mixes the hash
  // once more (murmur3's finaliser) before using it.
  const mix01 = str => {
    let h = Math.floor(hash01(str) * 4294967296);
    h = Math.imul(h ^ h >>> 16, 0x85ebca6b); h = Math.imul(h ^ h >>> 13, 0xc2b2ae35); h ^= h >>> 16;
    return (h >>> 0) / 4294967296;
  };
  // Uncoloured, every agent shares the memories' blue-white (218°); coloured by agent,
  // each takes two hues of its own, derived from its id so they hold across loads.
  function lookOf(agent, uniform = false) {
    if (uniform) return { h1: 218, h2: 250, sat: 85 };
    const h1 = mix01(`${agent}:h`) * 360;
    return { h1, h2: h1 + 50 + mix01(`${agent}:g`) * 110, sat: 62 + mix01(`${agent}:s`) * 26 };
  }
  const dots = new Map();
  // One sprite per size and colour: a flat dot. Large enough on screen to be read, it
  // carries one of five figures — stripes, a crescent, a ring, dots or quarters —
  // turned to an angle, both derived from the agent's id; smaller, it stays plain,
  // since a figure a few pixels wide only muddies the dot.
  const FIGURES = ["stripes", "crescent", "ring", "dots", "quarters"];
  const PATTERN_FROM = 6;
  function agentDot(R, agent, uniform) {
    const plain = R < PATTERN_FROM, key = `${R}|${agent}|${uniform ? 1 : 0}`;
    if (dots.has(key)) return dots.get(key);
    const L = lookOf(agent, uniform), size = Math.ceil(R * 2 + 2), c = document.createElement("canvas");
    c.width = c.height = size * 2;
    const x = c.getContext("2d");
    x.scale(2, 2); x.translate(size / 2, size / 2);
    const base = uniform ? "#c7d6f2" : hsl(L.h1, L.sat, 80), ink = uniform ? "#4f6aa6" : hsl(L.h2, L.sat, 42);
    x.beginPath(); x.arc(0, 0, R, 0, Math.PI * 2); x.fillStyle = base; x.fill();
    if (!plain) {
      const figure = FIGURES[Math.floor(mix01(`${agent}:k`) * FIGURES.length)];
      x.save(); x.clip(); x.rotate(mix01(`${agent}:r`) * Math.PI);
      x.fillStyle = ink; x.strokeStyle = ink;
      if (figure === "stripes") { x.lineWidth = R * 0.32; for (const t of [-1, 0, 1]) { x.beginPath(); x.moveTo(-2 * R, t * R * 0.66); x.lineTo(2 * R, t * R * 0.66); x.stroke(); } }
      else if (figure === "crescent") { x.beginPath(); x.arc(R * 0.45, 0, R * 0.95, 0, Math.PI * 2); x.fill(); }
      else if (figure === "ring") { x.lineWidth = R * 0.28; x.beginPath(); x.arc(0, 0, R * 0.55, 0, Math.PI * 2); x.stroke(); }
      else if (figure === "dots") { for (const [u, v] of [[-0.45, -0.45], [0.45, -0.45], [-0.45, 0.45], [0.45, 0.45], [0, 0]]) { x.beginPath(); x.arc(u * R, v * R, R * 0.2, 0, Math.PI * 2); x.fill(); } }
      else { x.fillRect(0, -R, R, R); x.fillRect(-R, 0, R, R); }
      x.restore();
    }
    const out = { c, size };
    dots.set(key, out);
    return out;
  }
  function drawStar(n, r, on) {
    const k = s.view.k;
    if (hub(n)) {
      const R = Math.max(2, Math.round(r * k * 2) / 2), sp = agentDot(R, n.agent, !s.byAgent), d = sp.size / k;
      ctx.drawImage(sp.c, n.x - d / 2, n.y - d / 2, d, d);
    } else {
      // Coloured by agent, a memory takes its agent's first hue.
      const L = s.byAgent && n.agent ? lookOf(n.agent) : null;
      ctx.fillStyle = on ? "#ffffff" : L ? hsl(L.h1, L.sat, 80) : "#c7d6f2";
      ctx.beginPath(); ctx.arc(n.x, n.y, r * (on ? 0.95 : 0.7), 0, Math.PI * 2); ctx.fill();
    }
    if (n === s.sel || n.hop === 0) { ctx.strokeStyle = "#ffffff"; ctx.lineWidth = 1 / k; ctx.beginPath(); ctx.arc(n.x, n.y, r + 3.5, 0, Math.PI * 2); ctx.stroke(); }
  }

  function draw() {
    const { view } = s;
    ctx.setTransform(s.DPR, 0, 0, s.DPR, 0, 0);
    ctx.fillStyle = NIGHT; ctx.fillRect(0, 0, s.W, s.H);
    ctx.setTransform(s.DPR * view.k, 0, 0, s.DPR * view.k, s.DPR * view.x, s.DPR * view.y);
    const focus = s.hover || s.sel, near = focus ? adj.get(focus.id) : null;
    const lit = n => !focus || n === focus || near.has(n.id);
    const ink = "170,200,255", accent = "#ffd9ef", grey = "#c9dcff";
    ctx.lineWidth = 0.9 / view.k;
    // Lines go out in batches, one path per kind and strength step, so tens of
    // thousands of lines cost a few dozen strokes; the lines of the point pointed at
    // are drawn last, on their own.
    const hot = new Path2D();
    let anyHot = false;
    // In a large picture the points are bright over the web of lines, unless clusters
    // of dots are to be avoided.
    const bold = !calmDots && G.nodes.length > 1000;
    for (const kind of ["structure", "part", "near", "similar", "co", "declared"]) {
      if (!s.show[kind]) continue;
      const steps = new Map();
      for (const l of G.links) {
        if (l.kind !== kind) continue;
        if (focus && (l.a === focus || l.b === focus)) { hot.moveTo(l.a.x, l.a.y); hot.lineTo(l.b.x, l.b.y); anyHot = true; continue; }
        // How similar two memories are shows as how strong their line is.
        let alpha = kind === "structure" ? 0.08 : kind === "declared" ? 0.55
          : kind === "similar" || kind === "near" ? 0.04 + 0.3 * Math.max(0, Math.min(1, (l.w - 0.55) / 0.35)) : 0.22;
        if (focus) alpha *= 0.35;
        const step = Math.round(alpha * 100) / 100;
        let path = steps.get(step);
        if (!path) steps.set(step, path = new Path2D());
        path.moveTo(l.a.x, l.a.y); path.lineTo(l.b.x, l.b.y);
      }
      ctx.strokeStyle = kind === "declared" ? accent : `rgb(${ink})`;
      for (const [alpha, path] of steps) { ctx.globalAlpha = Math.max(0.02, alpha); ctx.stroke(path); }
    }
    if (anyHot) { ctx.globalAlpha = 0.9; ctx.strokeStyle = accent; ctx.stroke(hot); }
    // Plain memory stars, the bulk of a large picture, are filled as two paths (lit and
    // dimmed); the rest are drawn one by one.
    const plainLit = new Path2D(), plainDim = new Path2D();
    for (const n of G.nodes) {
      const r = radius(n);
      if (n.kind === "memory" && !s.byAgent && n !== focus && n !== s.sel && n.hop !== 0) {
        // Bold, a point keeps at least 1.3 px on screen however far the view is out.
        const path = lit(n) ? plainLit : plainDim, rr = bold ? Math.max(r * 1.1, 1.3 / view.k) : r * 0.7;
        path.moveTo(n.x + rr, n.y); path.arc(n.x, n.y, rr, 0, Math.PI * 2);
        continue;
      }
      ctx.globalAlpha = lit(n) ? 1 : 0.18;
      drawStar(n, r, n === focus);
    }
    ctx.fillStyle = bold ? "#e6eeff" : "#c7d6f2";
    ctx.globalAlpha = 1; ctx.fill(plainLit);
    ctx.globalAlpha = 0.18; ctx.fill(plainDim);
    ctx.textAlign = "center"; ctx.textBaseline = "top";
    ctx.font = `400 ${11.5 / view.k}px ${FONT}`;
    // A rim in the background colour keeps lines from running through the names.
    ctx.lineJoin = "round"; ctx.lineWidth = 3 / view.k; ctx.strokeStyle = NIGHT;
    const ramp = v => Math.max(0, Math.min(1, v));
    for (const n of G.nodes) {
      // No names from afar; they fade in on the way closer, and on the point pointed at.
      // A picture of a few dozen groups is read by their names, so those stay.
      let a = n.kind !== "memory" && G.nodes.length <= 60 ? 0.9 : ramp((view.k - (n.kind === "memory" ? 2.2 : 1.6)) / 0.6);
      if (focus) a = n === focus ? 1 : near.has(n.id) ? Math.max(a, view.k > 1 ? 0.9 : 0) : a * 0.25;
      if (hub(n)) a = Math.max(a, focus && !lit(n) ? 0.3 : 0.95);
      if (n.kind === "project" && n.count >= 50) a = Math.max(a, focus && !lit(n) ? 0.25 : 0.75);
      if (a < 0.02) continue;
      ctx.globalAlpha = a;
      ctx.fillStyle = n === focus || hub(n) ? "#eef3ff" : grey;
      const label = n.kind === "memory" ? day(n.timestamp) : n.name, ly = n.y + radius(n) * (hub(n) ? 1.4 : 1) + 4 / view.k;
      ctx.strokeText(label, n.x, ly); ctx.fillText(label, n.x, ly);
    }
    ctx.globalAlpha = 1;
  }

  function loop() {
    if (!s.alive) return;
    let moved = false;
    if (s.heat > 0.004) {
      tick(s.heat, FRICTION, s.pin); s.heat *= 0.985; moved = true;
      if (s.autoFit) { const v = fitView(); s.view.k += (v.k - s.view.k) * 0.12; s.view.x += (v.x - s.view.x) * 0.12; s.view.y += (v.y - s.view.y) * 0.12; }
      if (s.heat <= 0.004) { // still again: frame it where it settled, and remember that
        if (s.autoFit) Object.assign(s.view, fitView());
        s.autoFit = false; s.pin = false; saveLayout(key, G.nodes);
      }
    }
    if (s.panV && !s.panning) {
      s.view.x += s.panV.x; s.view.y += s.panV.y; s.panV.x *= 0.92; s.panV.y *= 0.92;
      if (Math.hypot(s.panV.x, s.panV.y) < 0.05) s.panV = null;
      moved = true;
    }
    if (moved) draw();
    requestAnimationFrame(loop);
  }

  function card(html, buttons) {
    const el = $("gcard");
    el.innerHTML = html + (buttons.length ? `<div class="actions">${buttons.map(([id, label]) => `<button class="btn" id="${id}">${label}</button>`).join("")}</div>` : "");
    el.classList.add("open");
  }

  function select(n) {
    s.sel = n;
    const el = $("gcard");
    if (!n || n.kind === "agent") { el.classList.remove("open"); draw(); return; }
    if (n.kind === "group") {
      card(`<div class="g-head"><span>${esc(t("lvl")[n.level === "month" ? "month" : n.level])}</span><span>${esc(t("memoriesN")(n.count))}</span></div>
        <p class="g-title">${esc(groupPath(n))}</p>`, [["gGo", t("openGroup")]]);
      $("gGo").addEventListener("click", () => go({ view: "graph", path: n.path, month: "" }));
    } else if (n.kind === "cluster") {
      const mix = Object.entries(n.agents).map(([a, c]) => `${esc(a)} ${c}`).join(", ");
      card(`<div class="g-head"><span class="g-agent"><span class="g-dot"></span>${esc(t("memoriesN")(n.count))}</span><span>mem:${n.mem}</span></div>
        <p>“${esc(n.quote)}”</p><div class="g-near">${esc(t("clusterQuoteHint"))}<br>${mix}</div>`, [["gGo", t("openCluster")], ["gOpen", t("openMemory")]]);
      el.querySelector(".g-dot").style.background = tint(n);
      $("gGo").addEventListener("click", () => go({ view: "graph", cluster: n.mem }));
      $("gOpen").addEventListener("click", () => openDetail(n.mem).catch(fail));
    } else if (n.kind === "entity") {
      const mentions = n.count != null ? n.count : [...adj.get(n.id)].filter(id => byId.get(id).kind === "memory").length;
      card(`<div class="g-head"><span>${esc(t("entities"))}</span></div><p class="g-title">${esc(n.label)}</p>
        <div class="g-near">${esc(t("mentionsN")(mentions))}</div>`, mentions && n.entity ? [["gGo", t("openEntity")]] : []);
      $("gGo")?.addEventListener("click", () => go({ view: "graph", entity: n.entity }));
    } else {
      card(`<div class="g-head"><span class="g-agent"><span class="g-dot"></span>${esc(n.agent)}</span><span>${esc(day(n.timestamp))} · mem:${n.mem}</span></div>
        <p id="gText" class="g-loading">…</p><div class="g-near">${esc(t("similarN")(n.deg))}</div>`,
        n.hop === 0 ? [["gOpen", t("openMemory")]] : [["gOpen", t("openMemory")], ["gCenter", t("centerHere")]]);
      el.querySelector(".g-dot").style.background = tint(n);
      $("gOpen").addEventListener("click", () => openDetail(n.mem).catch(fail));
      $("gCenter")?.addEventListener("click", () => go({ view: "graph", around: n.mem }));
      // The picture carries no text, so a large one stays light; the card asks for it.
      api("/api/memory", { id: n.mem }).then(m => {
        const el = $("gText");
        if (!el || s.sel !== n) return;
        const text = m.content || "";
        el.textContent = text.length > 280 ? text.slice(0, 280) + "…" : text;
        el.classList.remove("g-loading");
      }).catch(() => {});
    }
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
    else { s.autoFit = false; s.panV = null; s.panning = { x: x - s.view.x, y: y - s.view.y, sx: x, sy: y }; cv.classList.add("dragging"); }
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
    s.autoFit = false;
    const [x, y] = at(e), f = Math.exp(-e.deltaY * 0.0015), [wx, wy] = world(x, y);
    s.view.k = Math.max(0.2, Math.min(8, s.view.k * f)); s.view.x = x - wx * s.view.k; s.view.y = y - wy * s.view.k; draw();
  }, { passive: false });
  for (const box of wrap.querySelectorAll("[data-layer]")) box.addEventListener("change", () => { s.show[box.dataset.layer] = box.checked; draw(); });
  $("byAgent")?.addEventListener("change", e => { s.byAgent = e.target.checked; draw(); });
  s.observer = new ResizeObserver(() => { if (s.alive) { resize(); draw(); } });
  s.observer.observe(wrap);

  resize();
  // Nothing is settled before the first frame: settling unseen held a large picture
  // back for a second or more. A picture whose every point was seen before is drawn
  // as it was left; new points start beside the points they are tied to and find
  // their place on screen while the seen ones hold still; a first view is drawn at
  // once and settles on screen, framed as it goes, until it is still (and
  // remembered then).
  if (kept < G.nodes.length) {
    s.pin = kept > 0;
    s.heat = kept ? 0.3 : 1;
    s.autoFit = !kept;
  }
  if (!s.autoFit) saveLayout(key, G.nodes); // also marks this picture as recently opened
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
    found = res.rows.map(r => ({ label: near(r.snippet), side: day(r.timestamp), q, run: () => { state.q = q; return openDetail(r.id); } }));
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
// ---- Tabs, each with its own back and forward
// A tab holds a history of places (what the hash names); moving within a tab adds to
// its history, the arrows walk it, and a memory or a new tab opens beside the current.
const PLACE = ["view", "path", "q", "month", "id", "around", "cluster", "entity"];
const place = () => JSON.parse(JSON.stringify(Object.fromEntries(PLACE.map(k => [k, state[k]]))));
let tabs = [], active = 0;
const memTitles = {};
const cur = () => tabs[active];
function keepPlace() { if (cur()) cur().hist[cur().pos] = place(); }
function saveTabs() {
  try { sessionStorage.setItem("cp.tabs", JSON.stringify({ db: overviewData?.db, tabs, active })); } catch {}
}
function tabTitle(c) {
  if (c.view === "memory") return memTitles[c.id] || `mem:${c.id}`;
  if (c.view === "memories" && c.q) return `${t("sideSearch")}: ${c.q}`;
  if ((c.view === "memories" || c.view === "graph") && (c.path || []).length) {
    const i = c.path.length - 1;
    return `${t(c.view)}: ${levelLabel(LEVELS[i], c.path[i])}`;
  }
  return t(c.view);
}
function renderTabs() {
  const bar = $("tabbar");
  bar.innerHTML = tabs.map((tb, i) => {
    const c = tb.hist[tb.pos];
    return `<div class="tab ${i === active ? "on" : ""}" role="tab" aria-selected="${i === active}" data-i="${i}" draggable="true">
      <button class="tab-go" data-i="${i}" title="${esc(tabTitle(c))}">${esc(tabTitle(c))}</button>
      <button class="tab-x" data-x="${i}" aria-label="${t("closeTab")}">×</button></div>`;
  }).join("") + `<button class="tab-new" id="tabNew" aria-label="${t("newTab")}" title="${t("newTab")}">+</button>`;
  for (const b of bar.querySelectorAll(".tab-go")) b.addEventListener("click", () => switchTab(Number(b.dataset.i)));
  for (const b of bar.querySelectorAll(".tab-x")) b.addEventListener("click", () => closeTab(Number(b.dataset.x)));
  for (const el of bar.querySelectorAll(".tab")) {
    el.addEventListener("auxclick", e => { if (e.button === 1) closeTab(Number(el.dataset.i)); });
    el.addEventListener("dragstart", e => e.dataTransfer.setData("text/plain", el.dataset.i));
    el.addEventListener("dragover", e => e.preventDefault());
    el.addEventListener("drop", e => {
      e.preventDefault();
      const from = Number(e.dataTransfer.getData("text/plain")), to = Number(el.dataset.i);
      if (from === to || Number.isNaN(from)) return;
      const keep = tabs[active];
      tabs.splice(to, 0, tabs.splice(from, 1)[0]);
      active = tabs.indexOf(keep);
      renderTabs(); saveTabs();
    });
  }
  $("tabNew").addEventListener("click", () => openTab({ view: "overview" }));
  const c = cur()?.hist[cur().pos];
  $("navTitle").textContent = c ? tabTitle(c) : "";
  $("backBtnTab").disabled = !cur() || cur().pos === 0;
  $("fwdBtnTab").disabled = !cur() || cur().pos >= cur().hist.length - 1;
}
function show(c) {
  Object.assign(state, { path: [], q: "", month: "", id: 0, around: 0, cluster: 0, entity: 0 }, JSON.parse(JSON.stringify(c)));
  return route().catch(fail);
}
function switchTab(i) { keepPlace(); active = i; return show(cur().hist[cur().pos]); }
function openTab(c) {
  keepPlace();
  tabs.splice(active + 1, 0, { hist: [{ ...place(), ...c }], pos: 0 });
  active += 1;
  return show(cur().hist[0]);
}
function closeTab(i) {
  keepPlace();
  if (tabs.length === 1) { tabs = [{ hist: [{ ...place(), view: "overview", id: 0 }], pos: 0 }]; active = 0; return show(cur().hist[0]); }
  tabs.splice(i, 1);
  if (active > i || active >= tabs.length) active = Math.max(0, active - 1);
  return show(cur().hist[cur().pos]);
}
function step(d) {
  const tb = cur(), to = tb.pos + d;
  if (to < 0 || to >= tb.hist.length) return;
  keepPlace(); tb.pos = to;
  return show(tb.hist[to]);
}

// A move that does not name what to center on leaves the "around", group and entity views.
function go(next) {
  keepPlace();
  Object.assign(state, { around: 0, cluster: 0, entity: 0, id: 0 }, next);
  const tb = cur();
  if (tb) { tb.hist = tb.hist.slice(0, tb.pos + 1); tb.hist.push(place()); tb.pos = tb.hist.length - 1; }
  closeDetail();
  return route().catch(fail);
}
async function route() {
  for (const b of document.querySelectorAll(".nav")) {
    if (b.dataset.view === state.view) b.setAttribute("aria-current", "page");
    else b.removeAttribute("aria-current");
  }
  saveHash();
  keepPlace();
  renderTabs();
  saveTabs();
  // The side bar's search shows the words the list is searching, unless being typed in.
  if (document.activeElement !== $("cnavQ")) $("cnavQ").value = state.view === "memories" ? state.q : "";
  renderTree().catch(() => {});
  stopGraph();
  $("main").classList.toggle("is-doc", state.view === "memory");
  if (state.view === "overview") renderOverview();
  else if (state.view === "memories") await renderMemories();
  else if (state.view === "graph") await renderGraph();
  else if (state.view === "memory") await renderDetail(state.id);
  else renderOther(state.view);
}

// ---- Side panel: the directory, or a search of memory text
let sideTimer;
function sideShow(which) {
  for (const b of document.querySelectorAll(".st")) b.setAttribute("aria-selected", String(b.dataset.side === which));
  $("sideTree").hidden = which !== "tree";
  $("sideSearch").hidden = which !== "search";
  if (which === "search") $("sideQ").focus();
}
// The search panel follows a notes app's: Aa matches case, the settings set the order
// and whether matching lines are shown, and an empty box lists the operators.
const sq = { case: false, sort: "new", lines: true };
try { Object.assign(sq, JSON.parse(localStorage.getItem("cp.sq") || "{}")); } catch {}
function sqSave() { try { localStorage.setItem("cp.sq", JSON.stringify(sq)); } catch {} }
function sqHelp() {
  const box = $("sqHelp");
  box.innerHTML = `<div class="sq-head">${t("searchOptions")}</div>` + t("searchOps").map(([op, what]) =>
    `<button class="sq-op" data-op="${esc(op)}"><b>${esc(op)}</b> ${esc(what)}</button>`).join("") + `<div class="hint sq-note">${esc(t("searchHelpNote"))}</div>`;
  for (const b of box.querySelectorAll(".sq-op")) b.addEventListener("mousedown", e => {
    e.preventDefault(); // keep the focus in the box
    const input = $("sideQ"), op = b.dataset.op === "[property]" ? "[" : b.dataset.op;
    input.value = (input.value && !input.value.endsWith(" ") ? input.value + " " : input.value) + op;
    input.focus(); sqToggleHelp(); sideSearch(input.value.trim()).catch(() => {});
  });
}
function sqToggleHelp() { $("sqHelp").hidden = !(document.activeElement === $("sideQ") && !$("sideQ").value.trim()); }
function sqSettings() {
  const box = $("sqSettings");
  box.innerHTML = `<label>${t("sortBy")} <select id="sqSort">${Object.entries(t("sorts")).map(([k, v]) => `<option value="${k}" ${k === sq.sort ? "selected" : ""}>${esc(v)}</option>`).join("")}</select></label>
    <label><input type="checkbox" id="sqLines" ${sq.lines ? "checked" : ""}> ${t("showLines")}</label>`;
  $("sqSort").addEventListener("change", e => { sq.sort = e.target.value; sqSave(); sideSearch($("sideQ").value.trim()).catch(() => {}); });
  $("sqLines").addEventListener("change", e => { sq.lines = e.target.checked; sqSave(); sideSearch($("sideQ").value.trim()).catch(() => {}); });
}
async function sideSearch(q) {
  const out = $("sideResults");
  if (!q) { out.innerHTML = ""; return; }
  const res = await api("/api/search", { q, case: sq.case ? "1" : "0", sort: sq.sort, limit: 100 });
  if ($("sideQ").value.trim() !== q) return; // a newer search is on its way
  // Only what is looked for in the text is marked: words, phrases and line: words, not
  // where a memory lives (path:, agent:, month:, tag:) or its [properties].
  const words = (q.replace(/\[[^\]]*\]/g, " ").replace(/\b(?:path|agent|month|tag):(?:"[^"]*"|\([^)]*\)|\S+)/g, " ")
    .replace(/\bline:\(([^)]*)\)/g, " $1 ").replace(/\bline:/g, " ").match(/"[^"]*"|\S+/g) || [])
    .filter(w => !w.startsWith("-")).map(w => w.replace(/["()]/g, "")).filter(Boolean);
  const hl = text => words.reduce((h, w) => h.replace(new RegExp(esc(w).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), sq.case ? "g" : "gi"), m => `<mark>${m}</mark>`), esc(text));
  out.innerHTML = `<div class="sq-count">${esc(t("searchHits")(res.total))}</div>` + (res.rows.length ? res.rows.map(r =>
    `<div class="hit-group"><button class="hit-file" data-id="${r.id}"><span class="hit-name">${esc(day(r.timestamp))} · ${esc(r.agent_id)}${r.project_id ? " / " + esc(r.project_id) : ""}</span>${r.matches ? `<span class="hit-n">${r.matches}</span>` : ""}</button>
      ${sq.lines ? r.lines.map(l => `<button class="hit-line" data-id="${r.id}">${hl(l)}</button>`).join("") : ""}</div>`).join("")
    : `<div class="hint sq-note">${esc(t("noHits"))}</div>`) +
    (res.total > res.rows.length ? `<div class="hint sq-note">${esc(t("searchMore")(res.total - res.rows.length))}</div>` : "");
  for (const b of out.querySelectorAll("[data-id]")) b.addEventListener("click", () => { state.q = words.join(" "); openDetail(Number(b.dataset.id)).catch(fail); });
}

function wire() {
  for (const b of document.querySelectorAll(".nav")) b.addEventListener("click", () => go({ view: b.dataset.view, path: [], q: "", month: "" }));
  for (const b of document.querySelectorAll(".st")) b.addEventListener("click", () => sideShow(b.dataset.side));
  $("sideQ").addEventListener("input", e => { sqToggleHelp(); clearTimeout(sideTimer); sideTimer = setTimeout(() => sideSearch(e.target.value.trim()).catch(() => {}), 200); });
  $("sideQ").addEventListener("focus", () => { sqHelp(); sqToggleHelp(); });
  $("sideQ").addEventListener("blur", () => { $("sqHelp").hidden = true; });
  $("sqCase").setAttribute("aria-pressed", String(sq.case));
  $("sqCase").addEventListener("click", e => {
    e.preventDefault(); sq.case = !sq.case; sqSave();
    $("sqCase").setAttribute("aria-pressed", String(sq.case));
    sideSearch($("sideQ").value.trim()).catch(() => {});
  });
  $("sqOpts").addEventListener("click", () => {
    const open = $("sqSettings").hidden;
    if (open) sqSettings();
    $("sqSettings").hidden = !open; $("sqOpts").setAttribute("aria-expanded", String(open));
  });
  $("backBtnTab").addEventListener("click", () => step(-1));
  $("fwdBtnTab").addEventListener("click", () => step(1));
  $("paletteBtn").addEventListener("click", () => openPalette());
  // The side bar's search opens the memory list on the words typed, as the list's own
  // search does; within a folder already open in the list, it searches that folder.
  let cnavTimer;
  $("cnavQ").addEventListener("input", e => {
    clearTimeout(cnavTimer);
    cnavTimer = setTimeout(() => {
      const q = e.target.value.trim();
      if (state.view !== "memories" && !q) return;
      const inList = state.view === "memories";
      go({ view: "memories", q, path: inList ? state.path : [], month: inList ? state.month : "" }).catch(fail);
    }, 220);
  });
  $("setBtn").addEventListener("click", openSettings);
  $("setClose").addEventListener("click", closeSettings);
  $("settingsBg").addEventListener("click", e => { if (e.target.id === "settingsBg") closeSettings(); });
  // Tab stays inside the dialog while it is open.
  $("settings").addEventListener("keydown", e => {
    if (e.key !== "Tab") return;
    const all = [...$("settings").querySelectorAll("button, input")].filter(el => !el.closest("[hidden]"));
    const first = all[0], last = all[all.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });
  for (const b of document.querySelectorAll("[data-layout]")) b.addEventListener("click", () => setLayout(b.dataset.layout).catch(fail));
  // Signing out is for a remote server's page, which a session cookie opens; the page
  // on this machine opens with the token in its address instead.
  $("signOutRow").hidden = Boolean(token);
  $("signOut").addEventListener("click", () => signOut());
  for (const b of document.querySelectorAll("[data-lang]")) b.addEventListener("click", () => setLang(b.dataset.lang));
  const calm = $("calmDots");
  calm.checked = calmDots;
  calm.addEventListener("change", () => {
    calmDots = calm.checked;
    try { localStorage.setItem("cp.calmDots", calmDots ? "1" : "0"); } catch {}
    if (state.view === "graph") route().catch(fail);
  });
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
    // ⌘K, and ⌘O / ⌘P as in a notes app, all open the same search.
    if ((e.metaKey || e.ctrlKey) && ["k", "o", "p"].includes(e.key.toLowerCase())) { e.preventDefault(); openPalette(); return; }
    if ((e.altKey) && e.key === "ArrowLeft") { e.preventDefault(); step(-1); return; }
    if ((e.altKey) && e.key === "ArrowRight") { e.preventDefault(); step(1); return; }
    if (e.key === "Escape") {
      if (!$("settingsBg").hidden) return closeSettings();
      closePalette();
      if (layout === "classic") closeDetail();
      return;
    }
    if (e.target.matches("input, textarea")) return;
    if (e.key === "/") { e.preventDefault(); $("q")?.focus(); }
    if (e.key === "j" || e.key === "k") {
      const rows = [...document.querySelectorAll(".row")];
      if (!rows.length) return;
      const i = rows.findIndex(r => r.classList.contains("sel"));
      // j / k move the selection; Enter opens it in a tab.
      const next = rows[Math.max(0, Math.min(rows.length - 1, i + (e.key === "j" ? 1 : -1)))];
      for (const r of rows) r.classList.toggle("sel", r === next);
      next.focus();
    }
  });
}

(async () => {
  wire();
  applyLang();
  try {
    // A remote link carries a one-time code after the "#": it is sent once, in a
    // header (never in a URL a log or a Referer could keep), for a session cookie.
    const code = H0.get("c");
    if (code) {
      history.replaceState(null, "", location.pathname);
      const r = await fetch("api/session", { headers: { "X-Dashboard-Code": code } });
      if (!r.ok) throw new Error(t("linkUsed"));
    }
    overviewData = await api("/api/overview");
  } catch (e) {
    return fail(H0.get("signedout") ? new Error(t("signedOut")) : e);
  }
  $("dbname").textContent = overviewData.db;
  for (const k of ["memories", "episodes", "entities"]) $("n-" + k).textContent = overviewData.counts[k].toLocaleString();
  for (const el of document.querySelectorAll("[data-n]")) el.textContent = overviewData.counts[el.dataset.n].toLocaleString();
  applyLang();
  // Tabs come back from this browser session for the same database; the place the
  // link names is shown in the active one.
  try {
    const saved = JSON.parse(sessionStorage.getItem("cp.tabs") || "null");
    if (saved && saved.db === overviewData.db && saved.tabs?.length) { tabs = saved.tabs; active = Math.min(saved.active, tabs.length - 1); }
  } catch {}
  const named = place();
  let openMemory = named.id && named.view !== "memory" ? named.id : 0;
  if (openMemory) named.id = 0;
  if (!tabs.length) tabs = [{ hist: [named], pos: 0 }];
  else if (H0.get("view")) { tabs[active].hist = tabs[active].hist.slice(0, tabs[active].pos + 1); tabs[active].hist.push(named); tabs[active].pos = tabs[active].hist.length - 1; }
  // The classic layout has no memory tabs: one left from the tabbed layout opens in
  // the drawer over the list instead.
  const here = cur().hist[cur().pos];
  if (layout === "classic" && here.view === "memory") {
    openMemory = here.id;
    cur().hist[cur().pos] = { ...here, view: "memories", id: 0 };
  }
  sideShow(H0.get("side") === "search" && layout === "tabs" ? "search" : "tree");
  if (H0.get("sq")) { $("sideQ").value = H0.get("sq"); sqToggleHelp(); sideSearch(H0.get("sq")).catch(() => {}); }
  await show(cur().hist[cur().pos]);
  if (openMemory) await openDetail(openMemory).catch(fail);
  if (H0.get("palette") === "1") openPalette(H0.get("pq") || "");
})();
