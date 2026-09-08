#!/usr/bin/env python3
"""Every absolute link this repository publishes into its own site resolves.

The documentation site is addressed from outside itself: README and PyPI
metadata, the bundled skill and its reference pages, SUPPORT.md, llms.txt, and a
checker that hard-codes the published base. Those links are absolute by
necessity -- a README is read on GitHub and on PyPI, where a relative path means
something else or nothing -- and absolute links into the site are the one class
no existing gate can see:

  * `check-doc-anchors.py` reads in-site links only. A link written as the full
    published URL is not one, even when it points at the very next page.
  * `skills/` is outside `docs_dir`, so no gate that walks the built site ever
    opens it -- and two of the files publishing these URLs live there.
  * mkdocs `--strict` fails a broken page link and passes a broken `#fragment`,
    which is how a slug guessed from a heading ("#26-in-line-releases" for
    "2.6 Feature releases within a line") shipped once with every gate green.

So this walks the repository for links to the site's own base URL and resolves
each one against the assembled tree, fragment included. Scanning the tree rather
than reading a list of files is deliberate: a hand-kept list is a second thing
to remember, and the failure it produces -- a new file publishing links nothing
checks -- looks exactly like success.

Runs against the tree `build-all-versions.py` writes, not a single `mkdocs
build`, because a link may address a version subtree and only the assembled tree
has one.

What this does not check: that a link points at the *right* page. A URL
resolving to a page that exists but says something else is a review's job.

Usage: check-site-urls.py <assembled-tree>
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "docs-versions.json"

# Directories that hold build output, dependencies or history rather than
# authored text. A published tree is full of links to itself, and counting those
# would drown the ones a person wrote.
SKIP = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "public",
    "site",
}

# Everything a URL cannot contain in the surfaces this scans: whitespace, the
# brackets of a markdown link or an autolink, quotes, backticks, and a comma.
# The autolink is the one that has to be spelled out -- an extraction that let
# `>` through reported three links as 404 when the URLs were fine and the
# measuring instrument was not.
URL_BODY = r"[^\s<>()\[\]{}\"'`,\\]*"
# Trailing punctuation belongs to the sentence, not the address.
TRAILING = "*.;:!?"

ID_ATTR = re.compile(r'\b(?:id|name)="(?P<value>[^"]+)"')


def files() -> list[pathlib.Path]:
    found = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP for part in path.relative_to(ROOT).parts):
            continue
        if path == CONFIG_PATH:
            # The file that declares the base is not a publisher of links, and
            # scanning it makes the empty-scan guard below unfalsifiable: point
            # the base somewhere nobody links to and the scan finds exactly one
            # address -- the one just written here -- which resolves to the site
            # root and reports a pass over a corpus of nothing. Measured while
            # mutating this gate, and the reason it is excluded by identity
            # rather than by name.
            continue
        found.append(path)
    return found


def links(pattern: re.Pattern) -> list[tuple[pathlib.Path, int, str]]:
    out: list[tuple[pathlib.Path, int, str]] = []
    for path in files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # a binary file publishes no links
        for number, line in enumerate(text.splitlines(), start=1):
            for match in pattern.finditer(line):
                out.append((path, number, match.group(0).rstrip(TRAILING)))
    return out


def target_of(tree: pathlib.Path, path: str) -> pathlib.Path | None:
    """The file a site path is served from, or None when nothing serves it.

    Both shapes are real: `use_directory_urls` turns a page into
    `<name>/index.html`, while `llms.txt` is copied through as itself.
    """
    trimmed = path.strip("/")
    if not trimmed:
        return tree / "index.html"
    direct = tree / trimmed
    if direct.is_file():
        return direct
    page = tree / trimmed / "index.html"
    return page if page.is_file() else None


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <assembled-tree>", file=sys.stderr)
        return 2
    tree = pathlib.Path(argv[1])
    if not tree.is_dir():
        print(f"not a directory: {tree}", file=sys.stderr)
        return 2

    site_url = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))["site_url"]
    pattern = re.compile(re.escape(site_url) + URL_BODY)

    found = links(pattern)
    if not found:
        # The one result that means nothing: a scan finding no links is a broken
        # scan, not a repository that publishes none. Both would print a pass.
        print(
            f"no links to {site_url} found anywhere in the repository -- "
            f"the scan is broken, not the links",
            file=sys.stderr,
        )
        return 1

    ids: dict[pathlib.Path, set[str]] = {}
    problems: list[str] = []

    for path, number, url in found:
        where = f"{path.relative_to(ROOT)}:{number}"
        rest = url[len(site_url):]
        page_path, _, fragment = rest.partition("#")

        target = target_of(tree, page_path)
        if target is None:
            problems.append(f"{where}: no page in the published tree serves {url}")
            continue
        if not fragment:
            continue
        if target.suffix != ".html":
            problems.append(f"{where}: {url} carries a fragment, but {page_path} is not a page")
            continue
        if target not in ids:
            ids[target] = {
                match.group("value")
                for match in ID_ATTR.finditer(target.read_text(encoding="utf-8", errors="replace"))
            }
        if fragment not in ids[target]:
            problems.append(f"{where}: {url} points at no anchor on that page")

    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        print(f"\n{len(problems)} unresolved link(s) of {len(found)} checked", file=sys.stderr)
        return 1

    distinct = len({url for _, _, url in found})
    fragments = len({url for _, _, url in found if "#" in url})
    print(
        f"site links: {len(found)} occurrence(s) of {distinct} address(es) "
        f"({fragments} with a fragment) all resolve in {tree}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
