// Check the dashboard's pages with axe-core (WCAG 2.2 A and AA rules) in both themes
// and both layouts, through Chrome's DevTools protocol, and that no page scrolls
// sideways at 1024 px.
//
//   node scripts/dashboard-a11y.mjs --log <file the dashboard printed its address to>
//        --axe <path to axe.min.js> --chrome <path to Chrome or Chromium>
//
// The address carries the page's token, so it is read from the file and never printed.
// At the top of the first page (in view, where axe can read what is behind it) a
// low-contrast paragraph is planted, and the run fails unless axe
// reports it: an axe that never ran, or whose results were lost, would otherwise
// report every page clean.
import { spawn } from "node:child_process";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const arg = name => { const i = process.argv.indexOf(name); return i > 0 ? process.argv[i + 1] : undefined; };
const [logPath, axePath, chromePath] = [arg("--log"), arg("--axe"), arg("--chrome")];
if (!logPath || !axePath || !chromePath) { console.error("usage: --log <file> --axe <axe.min.js> --chrome <binary>"); process.exit(64); }
const sleep = ms => new Promise(r => setTimeout(r, ms));

let base = "";
for (let i = 0; i < 120 && !base; i++) {
  try { base = (readFileSync(logPath, "utf8").match(/http:\/\/127\.0\.0\.1:\d+\/#t=[A-Za-z0-9_-]+/) || [""])[0]; } catch {}
  if (!base) await sleep(500);
}
if (!base) { console.error("the dashboard printed no address"); process.exit(2); }

const AXE = readFileSync(axePath, "utf8");
const port = 9300 + Math.floor(Math.random() * 400);
const chrome = spawn(chromePath, ["--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
  `--remote-debugging-port=${port}`, `--user-data-dir=${mkdtempSync(join(tmpdir(), "a11y-"))}`, "--window-size=1024,768", "about:blank"], { stdio: "ignore" });
const done = code => { chrome.kill(); process.exit(code); };

let target;
for (let i = 0; i < 100 && !target; i++) {
  await sleep(200);
  try { target = (await (await fetch(`http://127.0.0.1:${port}/json`)).json()).find(t => t.type === "page"); } catch {}
}
if (!target) { console.error("Chrome did not start"); done(2); }
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener("open", r));
let seq = 0;
const waiting = new Map();
const pageErrors = [];
ws.addEventListener("message", e => {
  const m = JSON.parse(e.data);
  if (m.id && waiting.has(m.id)) { waiting.get(m.id)(m); waiting.delete(m.id); }
  if (m.method === "Runtime.exceptionThrown") pageErrors.push(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text);
});
const send = (method, params = {}) => new Promise(r => { const id = ++seq; waiting.set(id, r); ws.send(JSON.stringify({ id, method, params })); });
// Evaluate an expression in the page; a script error is a failure, never an empty result.
async function evaluate(expression) {
  const r = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  if (r.result?.exceptionDetails) throw new Error(r.result.exceptionDetails.exception?.description || r.result.exceptionDetails.text);
  return r.result.result.value;
}
async function waitFor(expression, ms = 10000) {
  for (let t = 0; t < ms; t += 100) { if (await evaluate(expression)) return; await sleep(100); }
  throw new Error(`timed out waiting for ${expression}`);
}
await send("Page.enable");
await send("Runtime.enable");
await send("Emulation.setDeviceMetricsOverride", { width: 1024, height: 768, deviceScaleFactor: 1, mobile: false });

async function open(hash) {
  await send("Page.navigate", { url: "about:blank" });
  await sleep(100);
  await send("Page.navigate", { url: base + hash });
  await waitFor(`document.readyState === "complete" && !!document.querySelector("#page h1, #page .doc, #graphWrap canvas")`);
}

const views = [
  ["classic", "view=overview", "#ovRecent .row ~ .row, #ovRecalled .row"],
  ["classic", "view=memories", "#list .row"],
  ["classic", "view=memories&id=1", "#drawer.open .doc"],
  ["classic", "view=graph", "#graphWrap canvas"],
  ["classic", "view=episodes&id=1", "#drawer.open .doc"],
  ["classic", "view=entities&id=1", "#drawer.open .reflist"],
  ["classic", "view=profiles", "#page .profile"],
  ["classic", 'view=memories&path=["claude-code"]', "#agentProfile .profile"],
  ["tabs", "view=episode&id=1", "#page .doc h2"],
  ["tabs", "view=entity&id=1", "#page .reflist"],
  ["tabs", "view=memory&id=1", "#page .doc h2"],
  ["tabs", "view=memories&side=search&sq=garden", "#sideResults .hit-file"],
];
const TAGS = JSON.stringify(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]);
const failures = [];
let controlCaught = false, checked = 0;
await open("&view=overview");
for (const scheme of ["light", "dark"]) {
  await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: scheme }] });
  for (const [layout, view, ready] of views) {
    const where = `${scheme} ${layout} ${view}`;
    try {
      await evaluate(`localStorage.setItem("cp.layoutMode", ${JSON.stringify(layout)}); sessionStorage.removeItem("cp.tabs"); true`);
      const hash = "&" + view.replace(/path=(\[.*\])/, (_, p) => "path=" + encodeURIComponent(p)) + "&lang=ja";
      await open(hash);
      await waitFor(`!!document.querySelector(${JSON.stringify(ready)})`);
      await sleep(view.includes("graph") ? 1200 : 300); // the drawer's slide and the first frame
      const control = checked === 0;
      if (control) await evaluate(`(() => { const p = document.createElement("p"); p.id = "a11y-control"; p.textContent = "planted low-contrast text"; p.style.color = "#cccccc"; p.style.background = "#ffffff"; document.getElementById("page").prepend(p); return true; })()`);
      await evaluate(AXE + ";true");
      const found = JSON.parse(await evaluate(`axe.run(document, { runOnly: { type: "tag", values: ${TAGS} } }).then(r => JSON.stringify(r.violations.map(v => ({ id: v.id, impact: v.impact, targets: v.nodes.map(n => n.target.join(" ")) }))))`));
      for (const v of found) {
        const targets = v.targets.filter(t => t !== "#a11y-control");
        if (control && v.id === "color-contrast" && targets.length < v.targets.length) controlCaught = true;
        if (targets.length) failures.push(`${where}: ${v.id} (${v.impact}) at ${targets.slice(0, 4).join(", ")}`);
      }
      if (control) await evaluate(`document.getElementById("a11y-control").remove(); true`);
      const sideways = await evaluate(`document.scrollingElement.scrollWidth - document.scrollingElement.clientWidth`);
      if (sideways > 0) failures.push(`${where}: the page scrolls sideways by ${sideways}px at 1024px`);
      // A record's text comes before the table of its attributes.
      const order = await evaluate(`(() => { const d = document.querySelector("#drawer.open .doc, #page .doc"); const c = d?.querySelector(".content"), f = d?.querySelector(".facts-list");
        return !c || !f ? "n/a" : (c.compareDocumentPosition(f) & Node.DOCUMENT_POSITION_FOLLOWING) && c.getBoundingClientRect().top < f.getBoundingClientRect().top ? "ok" : "after"; })()`);
      if (order === "after") failures.push(`${where}: the record's text comes after its attributes`);
      // With the drawer open the list behind it is covered by the scrim, and a click on
      // the scrim closes the drawer (run last: it changes the page).
      if (ready.startsWith("#drawer.open")) {
        const covered = await evaluate(`(() => { const r = document.querySelector("#list .row").getBoundingClientRect(); return document.elementFromPoint(r.left + 8, r.top + 8)?.id === "scrim"; })()`);
        if (!covered) failures.push(`${where}: the list beside the open drawer is not covered by the scrim`);
        await evaluate(`document.getElementById("scrim").click(); true`);
        const closed = await evaluate(`!document.getElementById("drawer").classList.contains("open") && !document.body.classList.contains("drawer-open")`);
        if (!closed) failures.push(`${where}: a click on the scrim did not close the drawer`);
      }
      checked++;
    } catch (e) {
      failures.push(`${where}: ${e.message}`);
    }
  }
}
const axeVersion = await evaluate("axe.version");
// A row of the overview's short lists opens its memory, as a row of the list does.
try {
  await evaluate(`localStorage.setItem("cp.layoutMode", "classic"); true`);
  await open("&view=overview&lang=ja");
  await waitFor(`!!document.querySelector("#ovRecalled .row")`);
  const recalled = await evaluate(`document.querySelectorAll("#ovRecalled .row").length`);
  if (recalled !== 1) failures.push(`overview: ${recalled} rows under the most recalled, where the fixture has one recalled memory`);
  await evaluate(`document.querySelector("#ovRecalled .row").click(); true`);
  await waitFor(`!!document.querySelector("#drawer.open .content")`);
  const opened = await evaluate(`document.querySelector("#drawer .content").textContent`);
  if (!opened.startsWith("The garden plan")) failures.push(`overview: the recalled row opened ${JSON.stringify(opened.slice(0, 40))}`);
} catch (e) {
  failures.push(`overview: ${e.message}`);
}
// The palette's key is named as the keyboard has it: ⌘K on a Mac, Ctrl+K elsewhere,
// in the hint under the classic layout's views and in the settings' list of keys.
const { userAgent } = (await send("Browser.getVersion")).result;
for (const [platform, key, other] of [["MacIntel", "⌘K", "Ctrl+K"], ["Win32", "Ctrl+K", "⌘K"]]) {
  try {
    await send("Emulation.setUserAgentOverride", { userAgent, platform });
    for (const layout of ["classic", "tabs"]) {
      await evaluate(`localStorage.setItem("cp.layoutMode", "${layout}"); true`);
      await open("&view=overview&lang=ja");
      const r = await evaluate(`(() => { const h = document.getElementById("cnavHint"); return { hint: h.textContent, shown: h.getClientRects().length > 0, keys: document.getElementById("kbd").textContent }; })()`);
      const at = `${platform} ${layout}`;
      if (!r.keys.includes(key) || r.keys.includes(other)) failures.push(`${at}: the settings name the palette's key as ${JSON.stringify(r.keys)}`);
      if (layout === "classic" && (!r.shown || !r.hint.includes(key) || r.hint.includes(other))) failures.push(`${at}: the hint under the views reads ${JSON.stringify(r.hint)} (shown: ${r.shown})`);
      if (layout === "tabs" && r.shown) failures.push(`${at}: the hint under the views shows in the tabbed layout, which has the palette's button`);
    }
  } catch (e) {
    failures.push(`${platform}: ${e.message}`);
  }
}
await send("Emulation.setUserAgentOverride", { userAgent });
// Times are shown in the viewer's zone: a stored time is read with its own offset,
// and one without an offset as UTC. The fixture's first memory is 09:12 UTC.
for (const [zone, memory, tokyo, sqlite, note] of [
  ["Asia/Tokyo", "2026-10-28 18:12", "2026-06-12 20:07", "2026-10-31 11:40", "Asia/Tokyo (UTC+9)"],
  ["UTC", "2026-10-28 09:12", "2026-06-12 11:07", "2026-10-31 02:40", "UTC"],
]) {
  try {
    await send("Emulation.setTimezoneOverride", { timezoneId: zone });
    await evaluate(`localStorage.setItem("cp.layoutMode", "classic"); true`);
    await open("&view=memories&id=1&lang=ja");
    await waitFor(`!!document.querySelector("#drawer.open .doc-meta")`);
    const r = await evaluate(`({ meta: document.querySelector("#drawer .doc-meta span").textContent, tokyo: when("2026-06-12T20:07:30+09:00"),
      sqlite: when("2026-10-31 02:40:00"), note: document.getElementById("tzLine").textContent })`);
    if (r.meta !== memory) failures.push(`${zone}: the memory stored at 09:12 UTC shows as ${JSON.stringify(r.meta)}, not ${memory}`);
    if (r.tokyo !== tokyo) failures.push(`${zone}: a time stored with +09:00 shows as ${JSON.stringify(r.tokyo)}, not ${tokyo}`);
    if (r.sqlite !== sqlite) failures.push(`${zone}: a time stored without an offset shows as ${JSON.stringify(r.sqlite)}, not ${sqlite}`);
    if (!r.note.includes(note)) failures.push(`${zone}: the settings name the zone as ${JSON.stringify(r.note)}`);
  } catch (e) {
    failures.push(`${zone}: ${e.message}`);
  }
}
if (pageErrors.length) failures.push(...pageErrors.map(e => `script error: ${e}`));
console.log(`checked ${checked} pages with axe-core ${axeVersion}`);
if (!controlCaught) { console.error("the planted low-contrast paragraph was not reported: axe's results cannot be trusted"); done(3); }
if (failures.length) { console.error(failures.join("\n")); done(1); }
console.log("no violations");
done(0);
