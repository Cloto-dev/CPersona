// Blocking check: does the built site keep a reader on the language of the address,
// and tell them when the page exists in their own?
//
// The language of a page is the language of its address (overrides/main.html).
// The head script never redirects; a reader whose browser prefers the other
// language gets a small note linking to the other address, until they close it
// or pick a language. It used to redirect to the browser's language, which moved
// English-first browsers off every Japanese link, including the ones the
// project's home page gives Japanese readers.
//
// None of that is visible to `mkdocs build --strict`. The template can render a
// script that is syntactically perfect and points at the wrong pages, which is
// exactly what happened while the first version was being written: the
// destinations were read from `config.extra.alternate` without the `url` filter
// Material applies, so every page except the site root offered a Japanese URL
// relative to the wrong place.
//
// So this runs against the built site, the way check-doc-anchors.py does, and
// checks two different things:
//
//   * Structure, on every page — the script is there, and the destinations it
//     carries are the same ones Material put in that page's own hreflang links.
//   * Behaviour, once — the script is executed against a stubbed DOM for each
//     case: that nothing ever redirects, which language the note offers and
//     where it goes, and that a choice or a closed note silences it.
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
    fail(rel, "carries hreflang alternates but no language script — a reader whose browser prefers the other language is never told this page exists in it");
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

function element(tag) {
  const el = {
    tag, children: [], attrs: {}, listeners: {}, removed: false, hovered: false,
    appendChild(child) { this.children.push(child); },
    setAttribute(k, v) { this.attrs[k] = v; },
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
    remove() { this.removed = true; },
    matches() { return this.hovered; },
  };
  return el;
}

function execute(script, { pageLang, languages, stored, closed, storageThrows, search, hash }) {
  const redirects = [];
  const storage = {};
  if (stored) storage["cpersona-doc-lang"] = stored;
  if (closed) storage["cpersona-doc-lang-note-closed"] = closed;
  const listeners = [];
  const timers = [];
  const body = element("body");
  const context = {
    document: {
      readyState: "loading",
      documentElement: { getAttribute: (a) => (a === "lang" ? pageLang : null) },
      addEventListener: (type, fn, capture) => listeners.push({ type, fn, capture }),
      createElement: element,
      body,
    },
    navigator: { languages, language: languages[0] },
    location: {
      search: search || "",
      hash: hash || "",
      replace: (u) => redirects.push(u),
      assign: (u) => redirects.push(u),
      href: "",
    },
    localStorage: {
      getItem(k) { if (storageThrows) throw new Error("denied"); return k in storage ? storage[k] : null; },
      setItem(k, v) { if (storageThrows) throw new Error("denied"); storage[k] = v; },
    },
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: () => {},
  };
  vm.createContext(context);
  vm.runInContext(script, context);
  for (const l of listeners.filter((l) => l.type === "DOMContentLoaded")) l.fn();
  const note = body.children.find((c) => c.className === "cp-lang-note") || null;
  const go = note && note.children.find((c) => c.className === "cp-lang-note-go");
  const close = note && note.children.find((c) => c.className === "cp-lang-note-x");
  return { redirects, storage, listeners, timers, note, go, close, context };
}

function checkBehaviour(enFile, jaFile) {
  const en = routingScript(fs.readFileSync(enFile, "utf8"));
  const ja = routingScript(fs.readFileSync(jaFile, "utf8"));
  if (!en || !ja) {
    fail("behaviour", "could not find the language script on the sample pages");
    return;
  }
  const enHtml = fs.readFileSync(enFile, "utf8");
  const jaHtml = fs.readFileSync(jaFile, "utf8");
  const enLinks = {}; for (const m of enHtml.matchAll(LINK)) enLinks[m[2]] = m[1];
  const jaLinks = {}; for (const m of jaHtml.matchAll(LINK)) jaLinks[m[2]] = m[1];

  // [name, [script, options], expected note: null or [language, href]]
  const cases = [
    ["a Japanese browser on an English page stays, and is offered Japanese",
      [en, { pageLang: "en", languages: ["ja"] }], ["ja", enLinks.ja]],
    ["an English browser on a Japanese page stays (the link named Japanese), and is offered English",
      [ja, { pageLang: "ja", languages: ["en-GB"] }], ["en", jaLinks.en]],
    ["a Japanese browser on the Japanese page is offered nothing",
      [ja, { pageLang: "ja", languages: ["ja"] }], null],
    ["an English browser on an English page is offered nothing",
      [en, { pageLang: "en", languages: ["en-US"] }], null],
    ["a language picked earlier silences the note (English picked, Japanese browser)",
      [en, { pageLang: "en", languages: ["ja"], stored: "en" }], null],
    ["a language picked earlier silences the note (Japanese picked, on an English page)",
      [en, { pageLang: "en", languages: ["en"], stored: "ja" }], null],
    ["a note closed for Japanese stays closed",
      [en, { pageLang: "en", languages: ["ja"], closed: "ja" }], null],
    ["the first matching entry wins: en before ja",
      [en, { pageLang: "en", languages: ["en-US", "ja"] }], null],
    ["the first matching entry wins: ja before en",
      [en, { pageLang: "en", languages: ["ja-JP", "en-US"] }], ["ja", enLinks.ja]],
    ["a language that is neither is offered nothing",
      [en, { pageLang: "en", languages: ["fr-FR"] }], null],
    ["the query survives in the note's link",
      [en, { pageLang: "en", languages: ["ja"], search: "?q=recall" }], ["ja", `${enLinks.ja}?q=recall`]],
    ["storage denied (private browsing) neither throws nor redirects, and still offers the note",
      [en, { pageLang: "en", languages: ["ja"], storageThrows: true }], ["ja", enLinks.ja]],
  ];

  for (const [name, [script, options], expected] of cases) {
    let result;
    try {
      result = execute(script, options);
    } catch (e) {
      fail("behaviour", `${name} — threw: ${e.message}`);
      continue;
    }
    if (result.redirects.length) {
      fail("behaviour", `${name} — redirected to ${JSON.stringify(result.redirects)}; the address decides the language, nothing may move the reader`);
    }
    const got = result.note ? [result.note.lang, result.go && result.go.href] : null;
    if (JSON.stringify(got) !== JSON.stringify(expected)) {
      fail("behaviour", `${name} — offered ${JSON.stringify(got)}, expected ${JSON.stringify(expected)}`);
    }
  }

  // The note's own controls: following it is a choice, and keeps the section in view;
  // closing it is remembered for that language; it goes after a while unless hovered.
  const run = execute(en, { pageLang: "en", languages: ["ja"], hash: "#storage" });
  if (!run.note || !run.go || !run.close) {
    fail("behaviour", "the note was not built with a link and a close button");
  } else {
    if (run.note.lang !== "ja" || !/日本語/.test(run.go.textContent || "")) {
      fail("behaviour", `the note offering Japanese is not written in Japanese: ${JSON.stringify(run.go.textContent)}`);
    }
    if (!run.close.attrs["aria-label"]) fail("behaviour", "the close button has no accessible name");
    (run.go.listeners.click || []).forEach((fn) => fn());
    if (run.go.href !== `${enLinks.ja}#storage`) {
      fail("behaviour", `following the note goes to ${JSON.stringify(run.go.href)}, expected the section in view (${enLinks.ja}#storage)`);
    }
    const capture = run.listeners.find((l) => l.type === "click");
    if (capture) {
      capture.fn({ target: { closest: (sel) => (sel.includes(".cp-lang-note-go") ? { hreflang: "ja" } : null) } });
      if (run.storage["cpersona-doc-lang"] !== "ja") fail("behaviour", "following the note does not record the choice");
    }
    const timer = run.timers[0];
    if (!timer || timer.ms < 3000) fail("behaviour", "the note does not wait a few seconds before it goes");
    else {
      run.note.hovered = true; timer.fn();
      if (run.note.removed) fail("behaviour", "the note went while the pointer was on it");
      run.note.hovered = false; run.timers[run.timers.length - 1].fn();
      if (!run.note.removed) fail("behaviour", "the note never goes on its own");
    }
  }
  const closing = execute(en, { pageLang: "en", languages: ["ja"] });
  if (closing.close) {
    (closing.close.listeners.click || []).forEach((fn) => fn());
    if (!closing.note.removed) fail("behaviour", "closing the note leaves it on the page");
    if (closing.storage["cpersona-doc-lang-note-closed"] !== "ja") fail("behaviour", "closing the note is not remembered for its language");
  }

  // The selector click is what makes a choice a choice.
  const sel = execute(en, { pageLang: "en", languages: ["en"] });
  const click = sel.listeners.find((l) => l.type === "click");
  if (!click) fail("behaviour", "no click listener is registered, so using the language selector records nothing");
  else {
    if (click.capture !== true) {
      fail("behaviour", "the click listener is not in the capture phase, so the choice may not be stored before the browser navigates away");
    }
    click.fn({ target: { closest: (s) => (s.includes(".md-select__link") ? { hreflang: "ja" } : null) } });
    if (sel.storage["cpersona-doc-lang"] !== "ja") {
      fail("behaviour", `clicking the language selector stored ${JSON.stringify(sel.storage["cpersona-doc-lang"])}, expected "ja"`);
    }
    click.fn({ target: { closest: () => null } });
    if (sel.storage["cpersona-doc-lang"] !== "ja") {
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

console.log(`language routing: OK (${pages.length} pages checked, behaviour matrix green; nothing redirects)`);
