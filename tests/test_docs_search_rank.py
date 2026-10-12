"""Design and research pages are ranked below the guides in site search.

scripts/docs_search_rank.py lowers every page in the nav section that opens
with design/index.md, read from the nav at build time. These tests hold the
selection: the guides are never lowered, every page of that section is, and a
Japanese copy is lowered like its English page.
"""

from __future__ import annotations

import importlib.util
import pathlib
from types import SimpleNamespace

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _hook():
    spec = importlib.util.spec_from_file_location("docs_search_rank_under_test", REPO_ROOT / "scripts" / "docs_search_rank.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def page(src):
    return SimpleNamespace(is_page=True, file=SimpleNamespace(src_uri=src), children=None, meta={})


def section(*children):
    return SimpleNamespace(is_page=False, children=list(children))


def _nav(suffix=".md"):
    return SimpleNamespace(
        items=[
            page("index" + suffix),
            section(page("getting-started" + suffix), page("faq" + suffix)),
            section(
                page("design/index" + suffix),
                page("roadmap" + suffix),
                section(section(page("ACL_DESIGN" + suffix))),
                section(page("research/index" + suffix), page("research/note" + suffix)),
            ),
            page("sponsorship" + suffix),
        ]
    )


def test_only_the_design_section_is_lowered():
    hook = _hook()
    hook.on_nav(_nav())
    assert hook._lowered == {
        "design/index.md",
        "roadmap.md",
        "ACL_DESIGN.md",
        "research/index.md",
        "research/note.md",
    }


def test_the_boost_is_set_on_lowered_pages_and_not_on_guides():
    hook = _hook()
    nav = _nav(".ja.md")
    hook.on_nav(nav)
    guide, note = nav.items[1].children[0], nav.items[2].children[2].children[0].children[0]
    hook.on_page_markdown("", page=guide)
    hook.on_page_markdown("", page=note)
    assert "search" not in guide.meta
    assert note.meta["search"]["boost"] == hook.BOOST < 1


def test_a_page_that_sets_its_own_boost_keeps_it():
    hook = _hook()
    hook.on_nav(_nav())
    note = page("ACL_DESIGN.md")
    note.meta = {"search": {"boost": 2}}
    hook.on_page_markdown("", page=note)
    assert note.meta["search"]["boost"] == 2


def test_the_selection_follows_the_nav_in_mkdocs_yml():
    """The section is found by its overview, so the real nav must still open it with design/index.md."""
    text = (REPO_ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    nav = text[text.index("\nnav:\n") :]
    assert "- Overview: design/index.md" in nav
