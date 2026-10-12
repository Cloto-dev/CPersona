"""A page may be indexed by the overview of the nav section it sits in.

The home page lists the guides. The design, standards and research pages are
described on the "Design and research" overview instead, which the home page
links. check-docs-index.py accepts that overview as a page's index only on the
page's own path through the nav: the overview of an unrelated section does not
count, and an overview nothing links leaves its pages unreachable, which is
reported.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

NAV = """site_url: https://example.test/Site/
nav:
  - Home: index.md
  - Guide:
      - Start: start.md
  - Design and research:
      - Overview: design/index.md
      - Notes:
          - First note: FIRST.md
          - Second note: SECOND.md
  - Other:
      - Overview: other/index.md
      - Loose: LOOSE.md
"""
PAGES = ["index.md", "start.md", "design/index.md", "FIRST.md", "SECOND.md", "other/index.md", "LOOSE.md"]


@pytest.fixture
def gate(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "check_docs_index_overview_under_test", REPO_ROOT / "scripts" / "check-docs-index.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    docs = tmp_path / "docs"
    for page in PAGES:
        (docs / page).parent.mkdir(parents=True, exist_ok=True)
        (docs / page).write_text("# Page\n", encoding="utf-8")
    (tmp_path / "mkdocs.yml").write_text(NAV, encoding="utf-8")
    (tmp_path / "docs-versions.json").write_text(json.dumps({"versions": []}), encoding="utf-8")
    (docs / "llms.txt").write_text(
        "".join(
            f"- [p](https://example.test/Site/{p[:-3].removesuffix('index').rstrip('/')}/): x\n"
            for p in PAGES
            if p != "index.md"
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "DOCS", docs)
    monkeypatch.setattr(module, "MKDOCS", tmp_path / "mkdocs.yml")
    monkeypatch.setattr(module, "LLMS", docs / "llms.txt")
    monkeypatch.setattr(module, "VERSIONS", tmp_path / "docs-versions.json")
    monkeypatch.setattr(module, "failures", [])
    return module


def _write(gate, page: str, *links: str) -> None:
    (gate.DOCS / page).write_text("# Page\n" + "".join(f"- [l]({u})\n" for u in links), encoding="utf-8")


def _run(gate) -> list[str]:
    gate.main()
    return list(gate.failures)


def test_a_page_linked_only_from_its_sections_overview_is_indexed(gate):
    _write(gate, "index.md", "start.md", "design/index.md", "other/index.md", "LOOSE.md")
    _write(gate, "design/index.md", "../FIRST.md", "../SECOND.md#a-heading")
    assert _run(gate) == []


def test_a_page_its_overview_does_not_link_is_reported(gate):
    _write(gate, "index.md", "start.md", "design/index.md", "other/index.md", "LOOSE.md")
    _write(gate, "design/index.md", "../FIRST.md")
    failures = _run(gate)
    assert len(failures) == 1 and "docs/SECOND.md" in failures[0] and "docs/design/index.md" in failures[0]


def test_an_unrelated_sections_overview_does_not_index_a_page(gate):
    _write(gate, "index.md", "start.md", "design/index.md", "other/index.md")
    _write(gate, "other/index.md")
    _write(gate, "design/index.md", "../FIRST.md", "../SECOND.md", "../LOOSE.md")
    failures = _run(gate)
    assert len(failures) == 1 and "docs/LOOSE.md" in failures[0]


def test_an_overview_the_home_page_does_not_link_is_reported(gate):
    _write(gate, "index.md", "start.md", "other/index.md", "LOOSE.md")
    _write(gate, "design/index.md", "../FIRST.md", "../SECOND.md")
    failures = _run(gate)
    assert len(failures) == 1 and "docs/design/index.md" in failures[0]


def test_an_overview_link_to_a_page_outside_nav_is_reported(gate):
    _write(gate, "index.md", "start.md", "design/index.md", "other/index.md", "LOOSE.md")
    _write(gate, "design/index.md", "../FIRST.md", "../SECOND.md", "../GONE.md")
    failures = _run(gate)
    assert len(failures) == 1 and "GONE.md" in failures[0] and "docs/design/index.md" in failures[0]


def test_overviews_are_found_by_nesting(gate):
    owners = gate.section_overviews()
    assert owners["FIRST.md"] == ["design/index.md"]
    assert owners["LOOSE.md"] == ["other/index.md"]
    assert owners["start.md"] == []
    assert owners["design/index.md"] == []
