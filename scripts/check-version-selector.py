#!/usr/bin/env python3
"""Every published page states which version line it is, and offers the others.

The selector is rendered at build time into the site header by
`overrides/partials/alternate.html`. Overriding that partial is what puts it in
the header at all -- `partials/header.html` carries no block to extend -- and it
costs a dependency worth watching: the theme includes that partial only when the
build has language alternates, so a change to the i18n configuration can take
the version selector off every page without touching anything that looks like
version code, and without failing a build.

That is the failure this checks for, and it is the same shape as the one that
was already measured on this selector: the marker saying which version a reader
is on went missing while the selector still rendered and its links still worked,
because mkdocs parsed the environment value as YAML and the string never matched
a float. Nothing was red. So the three questions here are asked separately --

  * is the control on the page at all,
  * does it name the line the page actually belongs to,
  * do its links lead to trees that exist, without claiming to be languages

-- because each can fail while the other two pass, and only the first is visible
to someone glancing at a screenshot.

Runs against the assembled tree (the one `build-all-versions.py` writes), not
against a single `mkdocs build`: a plain build declares no versions and renders
no selector, which is correct there and would make this check either wrong or
vacuous. Usage: check-version-selector.py <assembled-tree>
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "docs-versions.json"

HEADER = re.compile(r'<header class="md-header[^"]*"[^>]*>(?P<body>.*?)</header>', re.S)
CONTROL = re.compile(r'<div class="md-select cp-version">(?P<body>.*?)</ul>', re.S)
CURRENT_LABEL = re.compile(r"<button (?P<attrs>[^>]*)>(?P<label>.*?)</button>", re.S)

# The theme sizes an icon button by giving its svg an explicit height. The
# version button holds a word instead, so its line box has to be set to that
# same height by hand -- see below for why the two must agree.
# Whitespace-tolerant: the theme ships minified, our own sheet ships as written.
ICON_HEIGHT = re.compile(r"\.md-icon svg\s*\{[^}]*?[^-]height:\s*(?P<value>[\d.]+rem)")
LABEL_HEIGHT = re.compile(r"\.cp-version__current\s*\{[^}]*?line-height:\s*(?P<value>[\d.]+rem)")
LINK = re.compile(
    r'<a href="(?P<href>[^"]+)" class="md-select__link"(?P<attrs>[^>]*)>(?P<title>.*?)</a>',
    re.S,
)


def text(raw: str) -> str:
    """Collapse the whitespace a template's indentation leaves inside an element."""
    return " ".join(raw.split())


def _stated(paths, tree, ids, current, pattern: re.Pattern) -> dict[str, str]:
    """What `pattern` states, per version line, across `paths`.

    Bucketed by line rather than reduced to one value, because each line is
    built from its own branch with its own pinned theme: one tree's answer is
    not evidence about another's, and taking the first file found would silently
    make it so.
    """
    stated: dict[str, str] = {}
    for path in sorted(paths):
        found = pattern.search(path.read_text(encoding="utf-8", errors="replace"))
        if found:
            stated.setdefault(line_of(path, tree, ids, current), found.group("value"))
    return stated


def line_of(page: pathlib.Path, tree: pathlib.Path, ids: set[str], current: str) -> str:
    """Which version line a page belongs to, read from where it sits in the tree.

    Deliberately independent of what the page says about itself: the page's own
    claim is the thing under test, so taking it from the page would make the
    comparison a tautology.
    """
    first = page.relative_to(tree).parts[0]
    return first if first in ids else current


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <assembled-tree>", file=sys.stderr)
        return 2
    tree = pathlib.Path(argv[1])
    if not tree.is_dir():
        print(f"not a directory: {tree}", file=sys.stderr)
        return 2

    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    site_url = config["site_url"]
    titles = {version["id"]: version.get("title", version["id"]) for version in config["versions"]}
    current = config["current"]

    problems: list[str] = []
    checked = 0

    for page in sorted(tree.rglob("*.html")):
        html = page.read_text(encoding="utf-8", errors="replace")
        header = HEADER.search(html)
        if not header:
            # Not every generated page is a themed page; one without a header
            # has nowhere to put the control and is not evidence of anything.
            continue
        checked += 1
        where = page.relative_to(tree)

        control = CONTROL.search(header.group("body"))
        if not control:
            problems.append(f"{where}: no version selector in the page header")
            continue

        expected_id = line_of(page, tree, set(titles), current)
        expected_title = titles[expected_id]

        marked = [
            match for match in LINK.finditer(control.group("body"))
            if 'aria-current="true"' in match.group("attrs")
        ]
        if len(marked) != 1:
            problems.append(
                f"{where}: {len(marked)} rows marked as the current version, expected exactly 1"
            )
        elif text(marked[0].group("title")) != expected_title:
            problems.append(
                f"{where}: marked as {text(marked[0].group('title'))!r}, "
                f"but sits in the {expected_title!r} tree"
            )

        label = CURRENT_LABEL.search(control.group("body"))
        if not label:
            problems.append(f"{where}: the selector shows no current version")
        elif "md-header__button" not in label.group("attrs"):
            # Half of the box equality below. The other controls in this corner
            # are md-header__button; a version button that is not one is a
            # different size, and md-select opens its panel at a fixed offset
            # from the control's own height.
            problems.append(f"{where}: the version button is not an md-header__button")
        elif text(label.group("label")) != expected_title:
            problems.append(
                f"{where}: the selector reads {text(label.group('label'))!r}, "
                f"but the page sits in the {expected_title!r} tree"
            )

        for match in LINK.finditer(control.group("body")):
            href = match.group("href")
            if "hreflang" in match.group("attrs"):
                # The language routing files any clicked md-select__link with an
                # hreflang as the reader's language choice, and then honours it
                # on every later page. A version row carrying one would send a
                # reader to a language named after a version number, with the
                # selector still looking correct.
                problems.append(f"{where}: a version row carries hreflang: {href}")
            if not href.startswith(site_url):
                problems.append(f"{where}: selector link leaves the site: {href}")
                continue
            target = tree / href[len(site_url):] / "index.html"
            if not target.is_file():
                problems.append(f"{where}: selector link has no page in the tree: {href}")

    # The two controls in the header corner are the same component, so md-select
    # places both panels at `top: calc(100% - .2rem)` of their own control. If
    # the controls are not the same height, the shorter one opens its panel
    # inside the header rather than below it -- measured, twice, before this
    # check existed. The theme gives an icon button its height through
    # `.md-icon svg`; the version button holds a word, so the same height is
    # written as a line-height. Read from the stylesheets each tree ships, so a
    # theme bump that moves the icon height is a red gate rather than a panel
    # drifting a few pixels into the header.
    ids = set(titles)
    icons = _stated(tree.rglob("assets/stylesheets/main.*.min.css"), tree, ids, current, ICON_HEIGHT)
    labels = _stated(tree.rglob("stylesheets/extra.css"), tree, ids, current, LABEL_HEIGHT)
    for line in sorted(set(icons) | set(labels)):
        icon, label_box = icons.get(line), labels.get(line)
        if icon is None:
            problems.append(f"{line}: the theme stylesheet states no icon height")
        elif label_box is None:
            problems.append(f"{line}: the version button states no line box to be sized to")
        elif icon != label_box:
            problems.append(
                f"{line}: the version button's line box is {label_box} where the theme's "
                f"icons are {icon}, so the two header controls are different heights and "
                f"one of them opens its panel inside the header"
            )

    if not checked:
        # An empty pass is the one outcome that means nothing. A tree with no
        # themed pages is a broken assembly, not a clean run.
        print(f"no themed pages under {tree}", file=sys.stderr)
        return 1

    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        print(f"\n{len(problems)} problem(s) across {checked} page(s)", file=sys.stderr)
        return 1

    print(f"version selector: {checked} page(s) name their own line and link to trees that exist")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
