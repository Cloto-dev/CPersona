"""llms.txt may address a page through a declared version line's subtree.

The development line can carry pages the line served at the site root does not.
A root link to one of those is dead, and the line's subtree is the only address
that reaches it. `check-docs-index.py` maps each llms.txt link back to a page in
this tree, so it has to read that subtree address as the same page.

It has to do that only for a declared line. Stripping any leading directory
would turn a link into a page that does not exist into a link that looks fine,
which is the failure this gate exists to report.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BASE = "https://example.test/Site"


@pytest.fixture
def gate(tmp_path, monkeypatch):
    """The gate, pointed at a scratch tree declaring one line, `2.6`."""
    spec = importlib.util.spec_from_file_location(
        "check_docs_index_under_test", REPO_ROOT / "scripts" / "check-docs-index.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    docs = tmp_path / "docs"
    (docs / "research").mkdir(parents=True)
    (docs / "PAGE.md").write_text("# Page\n", encoding="utf-8")
    (docs / "research" / "index.md").write_text("# Notes\n", encoding="utf-8")
    versions = tmp_path / "docs-versions.json"
    versions.write_text(
        json.dumps({"current": "2.5", "versions": [{"id": "2.5"}, {"id": "2.6"}]}),
        encoding="utf-8",
    )

    monkeypatch.setattr(module, "DOCS", docs)
    monkeypatch.setattr(module, "LLMS", docs / "llms.txt")
    monkeypatch.setattr(module, "VERSIONS", versions)
    monkeypatch.setattr(module, "failures", [])
    return module


def _llms(gate, *urls: str) -> None:
    gate.LLMS.write_text(
        "".join(f"- [link]({url}): text\n" for url in urls), encoding="utf-8"
    )


def test_a_link_under_a_declared_line_names_the_page(gate):
    _llms(gate, f"{BASE}/2.6/PAGE/")

    assert gate.llms_links(BASE) == {"PAGE.md": f"{BASE}/2.6/PAGE/"}
    assert gate.failures == []


def test_a_directory_page_under_a_declared_line_names_its_index(gate):
    _llms(gate, f"{BASE}/2.6/research/")

    assert gate.llms_links(BASE) == {"research/index.md": f"{BASE}/2.6/research/"}


def test_a_prefix_that_is_not_a_declared_line_is_kept(gate):
    """`9.9` is not a line, so the link names `9.9/PAGE.md`, which main reports dead."""
    _llms(gate, f"{BASE}/9.9/PAGE/")

    assert set(gate.llms_links(BASE)) == {"9.9/PAGE.md"}


def test_the_same_page_at_the_root_and_under_a_line_is_one_page_linked_twice(gate):
    _llms(gate, f"{BASE}/PAGE/", f"{BASE}/2.6/PAGE/")

    gate.llms_links(BASE)

    assert any("PAGE.md twice" in failure for failure in gate.failures)


def test_an_unreadable_version_map_strips_nothing(gate):
    """The loud direction: without the map, a subtree link reads as a page not in nav."""
    gate.VERSIONS.write_text("{ not json", encoding="utf-8")
    _llms(gate, f"{BASE}/2.6/PAGE/")

    assert set(gate.llms_links(BASE)) == {"2.6/PAGE.md"}
