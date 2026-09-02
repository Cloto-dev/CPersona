/**
 * Blocking check: does every ```mermaid block in docs/ actually parse?
 *
 * mkdocs does not validate a diagram. The superfences custom fence turns the
 * block into `<pre class="mermaid">` and the build is done — the parser only
 * runs in the reader's browser, at view time, which is nowhere near CI. A
 * mistyped arrow or an unbalanced quote therefore produces a green build, a
 * green --strict, green anchors, green translations, and a page where the
 * diagram is simply not there. That is the same shape as the other failures
 * this repository gates: the artefact is wrong and nothing on our side says so.
 *
 * So the check runs mermaid's own parser, the version the theme loads, against
 * every block in the sources. `parse` is used rather than `render` on purpose:
 * it settles whether the diagram is well-formed without needing layout, so a
 * headless DOM shim is enough and no browser is downloaded.
 *
 * Verified by mutation, not by reading: a misspelled diagram type, an
 * unbalanced label quote and a doubled arrow are each reported, while a
 * correct block beside them passes — the check discriminates instead of
 * failing everything once the parser is unhappy.
 *
 * Usage: node scripts/check-mermaid.mjs [docs_dir]     (default: ./docs)
 * Requires mermaid and jsdom on the module path; CI installs them with
 * --no-save immediately before this runs.
 */

import { readFileSync, readdirSync } from "node:fs";
import { JSDOM } from "jsdom";

const dom = new JSDOM("<!doctype html><html><body></body></html>", {
  pretendToBeVisual: true,
});

// navigator is a getter-only global in current node, so every name is defined
// rather than assigned — a plain `globalThis.navigator =` throws there.
for (const key of [
  "window", "document", "Node", "Element", "SVGElement", "HTMLElement",
  "DOMParser", "MutationObserver", "getComputedStyle",
]) {
  Object.defineProperty(globalThis, key, {
    value: key === "window" ? dom.window : dom.window[key],
    configurable: true,
    writable: true,
  });
}

const mermaid = (await import("mermaid")).default;
mermaid.initialize({ startOnLoad: false });

const docs = process.argv[2] || "docs";
const FENCE = /```mermaid\n([\s\S]*?)\n```/g;

let blocks = 0;
const failures = [];

for (const name of readdirSync(docs).filter((n) => n.endsWith(".md")).sort()) {
  const text = readFileSync(`${docs}/${name}`, "utf8");
  // Line number of each block, so the message points at the source and not at
  // an ordinal the reader then has to count out by hand.
  let match;
  let nth = 0;
  while ((match = FENCE.exec(text))) {
    blocks++;
    nth++;
    const line = text.slice(0, match.index).split("\n").length;
    try {
      await mermaid.parse(match[1]);
    } catch (error) {
      const first = String(error?.message ?? error).split("\n")[0];
      failures.push({ name, nth, line, first });
    }
  }
}

if (failures.length) {
  for (const f of failures) {
    console.log(
      `::error file=${docs}/${f.name},line=${f.line}::mermaid: block #${f.nth} does not parse — ${f.first}`,
    );
    console.error(`  - ${docs}/${f.name}:${f.line} (block #${f.nth}): ${f.first}`);
  }
  console.error(`${failures.length} of ${blocks} mermaid block(s) failed to parse`);
  process.exit(1);
}

console.log(`mermaid: OK (${blocks} block(s) parse)`);
