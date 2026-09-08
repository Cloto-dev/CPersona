#!/usr/bin/env python3
"""The version line this tree is on, and the label its pages state for it.

Every page that opens with "Applies to: CPersona <line>" used to write that
label by hand. A hand-written label is right exactly once. The day a branch is
copied to start the next line, all eight pages keep naming the line they were
copied from, and nothing goes red: the tree builds, the translations stay in
sync, every link resolves, and the only wrong thing on the page is the sentence
saying which software it describes.

So the label is derived here, from the version the tree itself states, and the
pages carry a placeholder instead. Three callers read this module:

  * mkdocs, as a build hook (`hooks:` in mkdocs.yml). The substitution happens
    while the page is still markdown, so nothing downstream -- the parser, the
    theme, the translated copies -- has to know the token exists.
  * scripts/check-docs-facts.py, which forbids the hand-written label rather
    than checking it. A gate that checked the label would pass a page stating
    the right line for the wrong reason, and would say nothing at all about a
    page added later outside its file list.
  * scripts/check-version-map.py, which reads the same version from the same
    places when it compares a line's branch against that line's tags. Two
    implementations of "where a tree states its version" is one more than can
    be kept in agreement.

The label shape is stated once, here, and scripts/check-version-selector.py
asserts the published page carries it -- compared against the line the page's
own position in the assembled tree says it belongs to, not against anything the
page claims about itself.
"""

from __future__ import annotations

import functools
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Where a line states its version, in the order the packaging resolves it. Both
# shapes are live: this line points `[tool.hatch.version]` at the package, while
# 2.4.x carries a static `version` in pyproject.toml and no `__version__` at all
# -- measured, and the reason this is a list rather than one path. A frozen line
# is exactly the case where the older shape is the one still in use, so reading
# only the current shape would report "states no version" about a line that
# states it perfectly well.
VERSION_SOURCES = (
    ("cpersona/__init__.py", re.compile(r'^__version__\s*=\s*"(?P<version>[^"]+)"', re.M)),
    ("pyproject.toml", re.compile(r'^version\s*=\s*"(?P<version>[^"]+)"', re.M)),
)

# A version names its line by its first two components. Pre-release suffixes are
# part of the patch component and never of the line, so `2.5.12b3` is on `2.5`.
LINE_PREFIX = re.compile(r"^(?P<line>\d+\.\d+)(?:\D|$)")

# What a page writes where the line label goes. Kept obviously not-a-version so
# that a build with the hook switched off publishes something a reader reports
# rather than a version number that happens to be wrong; scripts/
# check-version-selector.py is what turns that into a red gate.
PLACEHOLDER = "{{ version_line }}"

# The banner as it appears in source, in either language. The label is captured
# rather than matched so that a hand-written one can be named in the failure.
# `[^*]` stops the capture at the closing emphasis; the sentence that follows
# the banner is page-specific prose and is not this module's business.
BANNER = re.compile(
    r"\*\*(?:Applies to|対象):\s*CPersona\s+(?P<label>[^*]*?)\s*[.。]\*\*"
)


def label_for_line(line: str) -> str:
    """The label a page uses for a line: the line, then the patch wildcard.

    One convention, one place. A page says which line it describes, never which
    patch release -- a patch label would be wrong between a cut and the next
    bump, and nothing about a page changes per patch anyway.
    """
    return f"{line}.x"


def stated_version(root: pathlib.Path = ROOT) -> str | None:
    """The version this checkout states, or None when it states none anywhere."""
    for path, pattern in VERSION_SOURCES:
        source = root / path
        if not source.is_file():
            continue  # a line that does not carry this file states it elsewhere
        found = pattern.search(source.read_text(encoding="utf-8"))
        if found:
            return found.group("version")
    return None


def line_of_version(version: str) -> str | None:
    """The line a version is on, or None when the string names no line."""
    found = LINE_PREFIX.match(version)
    return found.group("line") if found else None


@functools.lru_cache(maxsize=None)
def tree_label(root: pathlib.Path = ROOT) -> str:
    """The label the pages in this checkout state. Raises rather than guessing.

    There is no sensible fallback. A build that cannot read its own version has
    nothing true to put in the banner, and putting something untrue there is the
    entire failure this module exists to remove -- so it fails the build, where
    the person who can fix it is already looking.
    """
    version = stated_version(root)
    if version is None:
        raise RuntimeError(
            "no version found in " + " or ".join(path for path, _ in VERSION_SOURCES)
            + f" under {root}, so the documentation cannot state which line it describes"
        )
    line = line_of_version(version)
    if line is None:
        raise RuntimeError(
            f"version {version!r} names no line (expected a major.minor prefix), so the "
            "documentation cannot state which line it describes"
        )
    return label_for_line(line)


def on_page_markdown(markdown: str, **_kwargs) -> str:
    """mkdocs build hook: put this tree's line label where the pages ask for it.

    Runs before the markdown is parsed, so the token never reaches an extension
    that might read the braces as something of its own, and the translated pages
    are substituted by the same pass as their sources.
    """
    if PLACEHOLDER not in markdown:
        return markdown
    return markdown.replace(PLACEHOLDER, tree_label())
