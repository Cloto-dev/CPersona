// Blocking check: does the built site still route a reader to their language?
//
// GitHub Pages cannot negotiate on Accept-Language, so `overrides/main.html`
// puts the decision in the page head: an explicit choice from the language
// selector wins and is remembered, otherwise the first Japanese-or-English entry
// in navigator.languages decides, and the move is a location.replace so the back
// button does not become a trap.
//
// None of that is visible to `mkdocs build --strict`. The template can render a
// script that is syntactically perfect and points at the wrong pages, which is
// exactly what happened while it was being written: the destinations were read
// from `config.extra.alternate` without the `url` filter Material applies, so
// every page except the site root offered a Japanese URL relative to the wrong
// place. The build was green, the home page worked, and every other page would
// have redirected into a 404.
//
// So this runs against the built site, the way check-doc-anchors.py does, and
// checks two different things:
//
//   * Structure, on every page — the script is there, and the destinations it
//     carries are the same ones Material put in that page's own hreflang links.
//     Agreement with the theme is the property; a private second theory of what
//     the Japanese URL of a page is would be the defect.
//   * Behaviour, once — the script is executed against a stubbed DOM for each
//     case the routing exists to get right, including the ones that are only
//     wrong later: the redirect loop, the overridden choice, private mode.
//
// Usage: node scripts/check-lang-routing.js [site_dir]   (default: ./site)
// Exit 1 on any failure.

"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SITE = path.resolve(process.argv[2] || "site");
const SCRIPT = /<script>\s*\(function \(\) \{[\s\S]*?\}\)\(\);\s*<\/script>/;
const ALTERNATES = /var ALTERNATES = (\{.*?\});/;
const LINK = /<link rel="alternate" href="([^"]*)" hreflang="([^"]*)">/g;
const XDEFAULT = /<link rel="alternate" hreflang="x-default" href="([^"]*)">/;

const failures = [];
const fail = (where, message) => failures.push(`${where}: ${message}`);

function htmlFiles(dir) {
  const out = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...htmlFiles(full));
    else if (entry.name.endsWith(".html")) out.push(full);
  }
  return out;
}

function routingScript(html) {
  const m = html.match(SCRIPT);
  return m ? m[0].replace(/^<script>/, "").replace(/<\/script>$/, "") : null;
}

// --- structure -------------------------------------------------------------

function checkPage(file) {
  const rel = path.relative(SITE, file);
  const html = fs.readFileSync(file, "utf8");

  const themeLinks = {};
  for (const m of html.matchAll(LINK)) themeLinks[m[2]] = m[1];
  if (!Object.keys(themeLinks).length) {
    // Not a translated page (Material renders a few standalone documents);
    // there is nothing to route between, so there is nothing to check.
    return;
  }

  const script = routingScript(html);
  if (!script) {
    fail(rel, "carries hreflang alternates but no language-routing script — a reader who follows a link here stays in whatever language the link named");
    return;
  }

  const found = script.match(ALTERNATES);
  if (!found) {
    fail(rel, "has a routing script with no ALTERNATES map");
    return;
  }

  let destinations;
  try {
    destinations = JSON.parse(found[1]);
  } catch (e) {
    fail(rel, `has an ALTERNATES map that is not JSON: ${found[1]}`);
    return;
  }

  for (const lang of Object.keys(themeLinks)) {
    if (destinations[lang] !== themeLinks[lang]) {
      fail(
        rel,
        `routes ${lang} to ${JSON.stringify(destinations[lang])} but this page's own hreflang link says ${JSON.stringify(themeLinks[lang])} — the routing script and the theme disagree about where ${lang} lives`
      );
    }
  }

  const xdefault = html.match(XDEFAULT);
  if (!xdefault) fail(rel, "has no x-default alternate");
  else if (themeLinks.en && xdefault[1] !== themeLinks.en) {
    fail(rel, `points x-default at ${xdefault[1]}, but English is at ${themeLinks.en}`);
  }
}

// --- behaviour -------------------------------------------------------------

function execute(script, { pageLang, languages, stored, storageThrows, search, hash }) {
  let replaced = null;
  const storage = stored ? { "cpersona-doc-lang": stored } : {};
  const listeners = [];
  const context = {
    document: {
      documentElement: { getAttribute: (a) => (a === "lang" ? pageLang : null) },
      addEventListener: (type, fn, capture) => listeners.push({ type, fn, capture }),
    },
    navigator: { languages, language: languages[0] },
    location: {
      search: search || "",
      hash: hash || "",
      replace: (u) => { replaced = u; },
      assign: () => { throw new Error("used location.assign; a redirect in the history stack traps the back button"); },
      href: "",
    },
    localStorage: {
      getItem(k) { if (storageThrows) throw new Error("denied"); return k in storage ? storage[k] : null; },
      setItem(k, v) { if (storageThrows) throw new Error("denied"); storage[k] = v; },
    },
  };
  vm.createContext(context);
  vm.runInContext(script, context);
  return { replaced, storage, listeners };
}

function checkBehaviour(enFile, jaFile) {
  const en = routingScript(fs.readFileSync(enFile, "utf8"));
  const ja = routingScript(fs.readFileSync(jaFile, "utf8"));
  if (!en || !ja) {
    fail("behaviour", "could not find the routing script on the sample pages");
    return;
  }
  const enHtml = fs.readFileSync(enFile, "utf8");
  const jaHtml = fs.readFileSync(jaFile, "utf8");
  const enLinks = {}; for (const m of enHtml.matchAll(LINK)) enLinks[m[2]] = m[1];
  const jaLinks = {}; for (const m of jaHtml.matchAll(LINK)) jaLinks[m[2]] = m[1];

  const cases = [
    ["a Japanese browser on an English page is routed to Japanese",
      [en, { pageLang: "en", languages: ["ja"] }], enLinks.ja],
    ["a Japanese browser already on the Japanese page stays (no redirect loop)",
      [ja, { pageLang: "ja", languages: ["ja"] }], null],
    ["an English browser on an English page stays",
      [en, { pageLang: "en", languages: ["en-US"] }], null],
    ["an English browser following a shared Japanese link is routed to English",
      [ja, { pageLang: "ja", languages: ["en-GB"] }], jaLinks.en],
    ["an explicit English choice survives a Japanese browser",
      [en, { pageLang: "en", languages: ["ja"], stored: "en" }], null],
    ["an explicit Japanese choice is honoured on an English page",
      [en, { pageLang: "en", languages: ["en"], stored: "ja" }], enLinks.ja],
    ["a stored choice does not loop on its own page",
      [ja, { pageLang: "ja", languages: ["en"], stored: "ja" }], null],
    ["the first matching entry wins: en before ja",
      [en, { pageLang: "en", languages: ["en-US", "ja"] }], null],
    ["the first matching entry wins: ja before en",
      [en, { pageLang: "en", languages: ["ja-JP", "en-US"] }], enLinks.ja],
    ["a language that is neither moves nobody",
      [en, { pageLang: "en", languages: ["fr-FR"] }], null],
    ["query and fragment survive the redirect",
      [en, { pageLang: "en", languages: ["ja"], search: "?q=recall", hash: "#storage" }],
      `${enLinks.ja}?q=recall#storage`],
    ["storage denied (private browsing) neither throws nor redirects",
      [en, { pageLang: "en", languages: ["en"], storageThrows: true }], null],
  ];

  for (const [name, [script, options], expected] of cases) {
    let result;
    try {
      result = execute(script, options);
    } catch (e) {
      fail("behaviour", `${name} — threw: ${e.message}`);
      continue;
    }
    if (result.replaced !== expected) {
      fail("behaviour", `${name} — went to ${JSON.stringify(result.replaced)}, expected ${JSON.stringify(expected)}`);
    }
  }

  // The selector click is what makes an explicit choice explicit; without it the
  // "choice wins" cases above are unreachable in a real browser.
  const run = execute(en, { pageLang: "en", languages: ["en"] });
  const click = run.listeners.find((l) => l.type === "click");
  if (!click) fail("behaviour", "no click listener is registered, so using the language selector records nothing");
  else {
    if (click.capture !== true) {
      fail("behaviour", "the click listener is not in the capture phase, so the choice may not be stored before the browser navigates away");
    }
    click.fn({ target: { closest: (sel) => (sel === ".md-select__link" ? { hreflang: "ja" } : null) } });
    if (run.storage["cpersona-doc-lang"] !== "ja") {
      fail("behaviour", `clicking the language selector stored ${JSON.stringify(run.storage["cpersona-doc-lang"])}, expected "ja"`);
    }
    click.fn({ target: { closest: () => null } });
    if (run.storage["cpersona-doc-lang"] !== "ja") {
      fail("behaviour", "a click outside the selector changed the stored choice");
    }
  }
}

// --- main ------------------------------------------------------------------

if (!fs.existsSync(SITE)) {
  console.error(`language routing: ${SITE} does not exist — build the site first (mkdocs build)`);
  process.exit(1);
}

const pages = htmlFiles(SITE);
if (!pages.length) {
  console.error(`language routing: no HTML under ${SITE}; a check with nothing to check would report green`);
  process.exit(1);
}
pages.forEach(checkPage);

// Behaviour is a property of the script, not of a page, so it is exercised once
// against a translated page and its counterpart rather than on all of them.
const enSample = path.join(SITE, "architecture", "index.html");
const jaSample = path.join(SITE, "ja", "architecture", "index.html");
if (fs.existsSync(enSample) && fs.existsSync(jaSample)) {
  checkBehaviour(enSample, jaSample);
} else {
  fail("behaviour", `sample pages are missing (${path.relative(SITE, enSample)} / ${path.relative(SITE, jaSample)}) — the behaviour matrix did not run, so it proved nothing`);
}

if (failures.length) {
  for (const f of failures) {
    console.log(`::error::language routing: ${f}`);
    console.error(`  - ${f}`);
  }
  console.error(`${failures.length} language-routing finding(s)`);
  process.exit(1);
}

console.log(`language routing: OK (${pages.length} pages checked, behaviour matrix green)`);
