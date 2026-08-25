#!/usr/bin/env python3
"""Verify that a translated page keeps the heading ids its English source generates.

A `.ja.md` page does not sit beside the English one — it *replaces* it at
`/ja/<page>/`. Anchors are derived from heading text, so translating a heading
moves its id, and every cross-page link aimed at the English slug lands nowhere
on the Japanese site. The convention that prevents it is an explicit id on each
translated heading (`## 見出し { #english-slug }`, via attr_list).

`check-doc-anchors.py` is the other half of this and does not cover it: it asks
whether the links that exist resolve, so a page whose ids have all moved stays
green until someone links to one of them. Measured on this repo before this
check existed: index.ja.md and faq.ja.md had no explicit ids at all — 16
headings whose anchors were `_1`, `3`, `ai`, `recall_1` — and every gate was
green, because nothing linked to them yet. The breakage was real and merely
not yet reachable, which is the state this check exists to make visible.

Compared per page, in document order, so a heading added on one side and not
the other shows up as a length mismatch rather than a silent re-pairing.

h1 is excluded deliberately: the convention stamps h2 and below, and page
titles are linked as pages rather than as anchors. Extending it to h1 would
mean re-stamping every translated page, which is a decision, not a check.

Runs against the built site, like the anchor check, so it sees what a reader's
browser sees rather than what the Markdown promises.

Usage: check-heading-parity.py [site_dir]   (default: ./site)
Exit 1 on any page whose Japanese heading ids differ from its English ones.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HEADING_ID = re.compile(r'<h([2-6])\s[^>]*\bid="([^"]+)"')


def heading_ids(page: Path) -> list[str]:
    return [m.group(2) for m in HEADING_ID.finditer(page.read_text(errors="replace"))]


def main() -> int:
    site = Path(sys.argv[1] if len(sys.argv) > 1 else "site").resolve()
    if not site.is_dir():
        print(f"site directory not found: {site}", file=sys.stderr)
        return 2

    translated = site / "ja"
    if not translated.is_dir():
        print(f"no translated site under {translated}", file=sys.stderr)
        return 2

    checked = 0
    findings = 0
    for english in sorted(site.rglob("*.html")):
        rel = english.relative_to(site)
        if rel.parts[0] == "ja":
            continue
        japanese = translated / rel
        if not japanese.exists():
            continue

        en_ids, ja_ids = heading_ids(english), heading_ids(japanese)
        checked += 1
        if en_ids == ja_ids:
            continue

        findings += 1
        source = f"docs/{rel.parent.name or 'index'}.ja.md"
        missing = [i for i in en_ids if i not in ja_ids]
        print(
            f"::error file={source},line=1::heading ids differ from the English page "
            f"({rel}): the Japanese page cannot be linked at "
            f"{', '.join(missing[:4]) or 'the English slugs'}"
            f"{' and more' if len(missing) > 4 else ''}. Add "
            "{ #english-slug } to the translated headings."
        )
        print(f"  - {rel}\n      english: {en_ids}\n      japanese: {ja_ids}", file=sys.stderr)

    if findings:
        print(f"{findings} page(s) whose translated headings moved their anchors", file=sys.stderr)
        return 1

    print(f"heading parity: OK ({checked} translated page(s) keep their English anchors)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
