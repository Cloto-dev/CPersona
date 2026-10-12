#!/usr/bin/env python3
"""Rank the design and research pages below the guides in site search.

Measured on the published site (2026-10-12): searching "install" put the 2.6
progress page and the release lifecycle standard among the first eight
results, and the first result for "embedding server" was the contiguous index
design note -- not Getting Started, whose step 2 is titled with those words.
Thirty of the forty-two pages are design, standards and research records, and
they are long, so on term frequency alone they outrank the guide a reader is
looking for.

The pages stay in the index: a search for "far vote" should still find the
design note that defines it. They are lowered, not removed, by giving each one
a document boost below the default of 1. Which pages those are is read from
the nav -- every page in the section whose overview is design/index.md -- so a
design note added to that section is ranked with the others without anyone
remembering this file, and a page moved out of it stops being lowered.

It is a build hook, like docs_version.py, so that each version line's branch
builds with only the two pinned packages and no front matter has to be copied
into sixty source files and their translations.
"""

from __future__ import annotations

OVERVIEW = "design/index.md"
# lunr's document boost. 1 is every other page; Material passes the value through
# from page.meta["search"]["boost"] when it writes the search index.
BOOST = 0.5

_lowered: set[str] = set()


def _key(src_uri: str) -> str:
    # The Japanese copy (page.ja.md) ranks like its English page.
    return src_uri[: -len(".ja.md")] + ".md" if src_uri.endswith(".ja.md") else src_uri


def _pages(items):
    for item in items:
        if getattr(item, "is_page", False):
            yield item
        yield from _pages(getattr(item, "children", None) or [])


def lowered_pages(items) -> set[str]:
    """Source paths of every page in the section that opens with the design overview."""
    found: set[str] = set()
    for item in items:
        children = getattr(item, "children", None) or []
        if any(getattr(c, "is_page", False) and _key(c.file.src_uri) == OVERVIEW for c in children):
            found |= {_key(page.file.src_uri) for page in _pages(children)}
        else:
            found |= lowered_pages(children)
    return found


def on_nav(nav, **_kwargs):
    _lowered.clear()
    _lowered.update(lowered_pages(nav.items))
    return nav


def on_page_markdown(markdown: str, page, **_kwargs) -> str:
    if _key(page.file.src_uri) in _lowered:
        page.meta.setdefault("search", {}).setdefault("boost", BOOST)
    return markdown
