#!/usr/bin/env python3
"""Blocking check: is every documentation page reachable from both indexes?

The site has two indexes over the same set of pages, written for two different
readers, and they are maintained by hand:

  mkdocs.yml `nav:`   the human index — the sidebar
  docs/llms.txt       the machine index — what an agent fetches to find out
                      which pages exist before it fetches any of them

Nothing connected them. Measured on this repository (2026-09-02): the site had
twenty pages and `llms.txt` listed fourteen. The six it had lost were the six
most recently added — OAuth support, session identity, memory origin, the
contiguous index, and both project standards — because adding a page means
editing two files and only one of them is visible while you do it. Every gate
was green the whole time, including the eight this repository already runs over
the docs: they check anchors, headings, translations, routing and facts, and
all of them start from the page they are given. A page nothing points at is not
an input to any of them.

The failure is quiet in the way that matters most for `llms.txt` specifically.
Its whole purpose is to be the list an agent trusts instead of guessing, so a
page missing from it is not a broken link the reader can see and route around —
it is a page the reader concludes does not exist.

What is checked, in both directions:

  * every `docs/*.md` appears in `nav:` exactly once (mkdocs logs unlisted
    pages at INFO, which `--strict` does not promote — it is not a detector)
  * every `nav:` entry points at a file that exists
  * every page in `nav:` has a link in `llms.txt`
  * every site link in `llms.txt` points at a page that is in `nav:`
  * those links use this site's `site_url` and the directory form mkdocs
    publishes (`.../page/`), so a rename of the site does not leave absolute
    links pointing into the old one

Exit 0 when the three lists agree; exit 1 with one line per violation.
Run from the repository root: `python3 scripts/check-docs-index.py`.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
MKDOCS = ROOT / "mkdocs.yml"
LLMS = DOCS / "llms.txt"

# Same parsing choice, and the same reason, as check-i18n-coverage.py:
# mkdocs.yml carries tags a plain safe_load refuses, and depending on the
# build environment's YAML loader to read a handful of strings would make this
# check unable to run ahead of the build it is meant to gate.
NAV_LEAF = re.compile(r"^\s*-\s+(?P<label>[^:]+):\s*(?P<path>\S+\.md)\s*$")
SITE_URL = re.compile(r"^site_url:\s*(?P<url>\S+)\s*$")
MD_LINK = re.compile(r"\[(?P<label>[^\]]+)\]\((?P<url>[^)\s]+)\)")

# The site root. `llms.txt` opens by describing the site itself, which is what
# index.md is — a second link to it under "Docs" would be the same page twice.
# Listed here rather than left implicit so that the next page exempted has to
# say why in the same place.
LLMS_EXEMPT = {"index.md"}

failures: list[str] = []


def fail(msg: str) -> None:
    failures.append(msg)


def site_url() -> str:
    for line in MKDOCS.read_text().splitlines():
        m = SITE_URL.match(line)
        if m:
            return m.group("url").rstrip("/")
    fail("mkdocs.yml: no `site_url:` — llms.txt links cannot be checked against it")
    return ""


def nav_pages() -> list[str]:
    """Page paths under `nav:`, in nav order, as they are written (relative to docs/)."""
    lines = MKDOCS.read_text().splitlines()
    pages: list[str] = []
    start = -1
    for i, line in enumerate(lines):
        if line.startswith("nav:"):
            start = i
            continue
        if start < 0:
            continue
        if line and not line[0].isspace():
            break  # dedented to a new top-level key: nav is over
        m = NAV_LEAF.match(line)
        if m:
            pages.append(m.group("path").strip())
    if start < 0:
        fail("mkdocs.yml: no `nav:` block — this check cannot see the nav")
    return pages


def llms_links(base: str) -> dict[str, str]:
    """Site links in llms.txt, as {page path: url}, reporting the malformed ones."""
    text = LLMS.read_text()
    found: dict[str, str] = {}
    for m in MD_LINK.finditer(text):
        url = m.group("url")
        if not url.startswith(base):
            # Links off this site (the repository, PyPI) are the "Optional"
            # section's job and are not an index over docs/.
            continue
        rest = url[len(base) :]
        if not rest.endswith("/") or not rest.startswith("/"):
            fail(
                f"docs/llms.txt: {url} is not the directory form mkdocs publishes — "
                f"write it as {base}/<page>/"
            )
            continue
        slug = rest.strip("/")
        if not slug:
            fail(f"docs/llms.txt: {url} is the site root, which llms.txt already describes")
            continue
        if "/" in slug:
            fail(f"docs/llms.txt: {url} is not a top-level page of this site")
            continue
        page = f"{slug}.md"
        if page in found:
            fail(f"docs/llms.txt: links {page} twice — an index that repeats itself has drifted")
        found[page] = url
    return found


def main() -> int:
    base = site_url()
    nav = nav_pages()

    seen: set[str] = set()
    for page in nav:
        if page in seen:
            fail(f"mkdocs.yml: {page} appears in nav more than once")
        seen.add(page)
        if not (DOCS / page).exists():
            fail(f"mkdocs.yml: nav points at docs/{page}, which does not exist")

    for path in sorted(DOCS.glob("*.md")):
        if path.name.endswith(".ja.md"):
            continue  # the translation is routed by mkdocs-static-i18n, not by nav
        if path.name not in seen:
            fail(
                f"docs/{path.name} is not in mkdocs.yml `nav:`. mkdocs logs this at INFO "
                "and --strict does not promote it, so the page publishes unreachable."
            )

    if base:
        indexed = llms_links(base)
        for page in nav:
            if page in LLMS_EXEMPT:
                continue
            if page not in indexed:
                fail(
                    f"docs/llms.txt has no link to docs/{page}. An agent reading llms.txt "
                    "concludes the page does not exist — add it under the section that "
                    "matches its nav group."
                )
        for page in sorted(set(indexed) - seen):
            fail(
                f"docs/llms.txt links {page}, which is not in mkdocs.yml `nav:` "
                "(renamed or removed) — the link is published dead."
            )
        for page in sorted(set(indexed) & LLMS_EXEMPT):
            fail(
                f"docs/llms.txt links {page}, which this check exempts as the site root "
                "llms.txt already describes — drop the link or drop the exemption."
            )

    if failures:
        for msg in failures:
            print(f"::error file=docs/llms.txt,line=1::docs index: {msg}")
            print(f"  - {msg}", file=sys.stderr)
        print(f"{len(failures)} docs index finding(s)", file=sys.stderr)
        return 1

    print(f"docs index: OK ({len(nav)} pages in nav, {len(nav) - len(LLMS_EXEMPT)} in llms.txt)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
