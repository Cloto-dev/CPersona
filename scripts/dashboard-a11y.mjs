// Check the dashboard's pages with axe-core (WCAG 2.2 A and AA rules) in both themes
// and both layouts, through Chrome's DevTools protocol, and that no page scrolls
// sideways at 1024 px.
//
//   node scripts/dashboard-a11y.mjs --log <file the dashboard printed its address to>
//        --axe <path to axe.min.js> --chrome <path to Chrome or Chromium>
//
// The address carries the page's token, so it is read from the file and never printed.
// On the first page a low-contrast paragraph is planted, and the run fails unless axe
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
  ["classic", "view=overview", "#page h1"],
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
      if (control) await evaluate(`(() => { const p = document.createElement("p"); p.id = "a11y-control"; p.textContent = "planted low-contrast text"; p.style.color = "#cccccc"; p.style.background = "#ffffff"; document.getElementById("page").appendChild(p); return true; })()`);
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
      checked++;
    } catch (e) {
      failures.push(`${where}: ${e.message}`);
    }
  }
}
if (pageErrors.length) failures.push(...pageErrors.map(e => `script error: ${e}`));
console.log(`checked ${checked} pages with axe-core ${await evaluate("axe.version")}`);
if (!controlCaught) { console.error("the planted low-contrast paragraph was not reported: axe's results cannot be trusted"); done(3); }
if (failures.length) { console.error(failures.join("\n")); done(1); }
console.log("no violations");
done(0);
