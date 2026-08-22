#!/usr/bin/env python3
"""Verify that every in-site link with a #fragment lands on an id that exists.

`mkdocs build --strict` fails on a broken *page* link but says nothing about a
broken *anchor*: a link to `behavior-contracts.md#3-episode-boundary-penalty`
keeps passing after the heading it names is gone. Translations make that gap
load-bearing. A `.ja.md` page replaces the English fallback at `/ja/<page>/`,
and its anchors are derived from its own headings — so translating a heading
silently invalidates every cross-page link aimed at the English slug, on the
Japanese site only. That is why translated pages carry explicit heading ids
(`## 見出し { #english-slug }`, via the attr_list extension) and why this check
exists to prove they still do.

This runs against the *built* site rather than the Markdown sources, so it sees
what a reader's browser sees: fallback pages, theme-generated links, and
percent-encoded fragments included.

Usage: check-doc-anchors.py [site_dir]   (default: ./site)
Exit 1 on any unresolved fragment.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

HREF = re.compile(r'href="([^"]+)"')
# id="..." covers headings and theme anchors; name="..." covers legacy targets.
ID = re.compile(r'\b(?:id|name)="([^"]+)"')
EXTERNAL = ("http://", "https://", "mailto:", "tel:", "data:", "//")


def ids_of(html_path: Path, cache: dict[Path, set[str]]) -> set[str]:
    if html_path not in cache:
        try:
            cache[html_path] = set(ID.findall(html_path.read_text(errors="replace")))
        except OSError:
            cache[html_path] = set()
    return cache[html_path]


def main() -> int:
    site = Path(sys.argv[1] if len(sys.argv) > 1 else "site").resolve()
    if not site.is_dir():
        print(f"site directory not found: {site}", file=sys.stderr)
        return 2

    pages = sorted(site.rglob("*.html"))
    if not pages:
        print(f"no HTML found under {site} — did the build run?", file=sys.stderr)
        return 2

    cache: dict[Path, set[str]] = {}
    findings: list[str] = []
    checked = 0

    for page in pages:
        for href in HREF.findall(page.read_text(errors="replace")):
            if href.startswith(EXTERNAL) or "#" not in href:
                continue
            path_part, _, fragment = href.partition("#")
            fragment = unquote(urlsplit(fragment).path or fragment)
            if not fragment:
                continue  # bare "#" is a no-op link, not a claim about a target

            if not path_part:
                target = page
            else:
                base = path_part if path_part.startswith("/") else None
                resolved = (
                    (site / base.lstrip("/")) if base else (page.parent / path_part)
                )
                target = resolved.resolve()
                if target.is_dir():
                    target = target / "index.html"
                elif not target.suffix:
                    target = target.with_suffix(".html")

            if not target.is_file():
                # A missing *page* is mkdocs --strict's job; do not double-report.
                continue

            checked += 1
            if fragment not in ids_of(target, cache):
                findings.append(
                    f"{page.relative_to(site)} -> {target.relative_to(site)}#{fragment}"
                )

    if findings:
        for f in sorted(set(findings)):
            print(f"::error::broken anchor: {f}")
            print(f"  - broken anchor: {f}", file=sys.stderr)
        print(
            f"{len(set(findings))} broken anchor(s) out of {checked} checked",
            file=sys.stderr,
        )
        return 1

    print(f"doc anchors: OK ({checked} in-site fragment links resolve)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
