"""Locks for the gate that notices a page nobody translated.

`check-i18n-coverage.py` exists because the drift gate structurally cannot see
this class: it walks `docs/*.ja.md`, so a page with no translation is not walked
and not reported. That gap shipped once — a new page went out untranslated, with
its English nav label between two Japanese ones, and every gate stayed green.

A gate written for a class nobody has seen fail is a gate nobody has seen work,
so each case below is the deliberate violation it was built to catch, asserted
on the exit code rather than on the text it prints. The messages are a display
format; the exit code is the contract CI branches on.
"""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load():
    """Import the hyphenated script by path — `scripts/` is not a package."""
    spec = importlib.util.spec_from_file_location(
        "check_i18n_coverage", ROOT / "scripts" / "check-i18n-coverage.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MKDOCS = """\
site_name: X
nav:
  - Home: index.md
  - Guide: guide.md
  - Design documents:
      - One: one.md
plugins:
  - i18n:
      languages:
        - locale: ja
          nav_translations:
            Home: ホーム
            Guide: ガイド
            Design documents: 設計ドキュメント
            One: いち
"""


@pytest.fixture
def site(tmp_path, monkeypatch):
    """A minimal, fully-covered docs tree the cases below then break one way each."""
    docs = tmp_path / "docs"
    docs.mkdir()
    for name in ("index", "guide", "one"):
        (docs / f"{name}.md").write_text(f"# {name}\n")
        (docs / f"{name}.ja.md").write_text(f"# {name} ja\n")
    mkdocs = tmp_path / "mkdocs.yml"
    mkdocs.write_text(MKDOCS)

    module = _load()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "DOCS", docs)
    monkeypatch.setattr(module, "ALLOWLIST", docs / ".i18n-english-only")
    monkeypatch.setattr(module, "MKDOCS", mkdocs)
    monkeypatch.setattr(module.sys, "argv", ["check-i18n-coverage.py", "--strict"])
    return module, tmp_path, docs, mkdocs


def test_fully_covered_tree_passes(site):
    module, _, _, _ = site
    assert module.main() == 0


def test_untranslated_page_fails(site):
    """The class the drift gate cannot see: a page with no .ja.md at all."""
    module, _, docs, _ = site
    (docs / "new.md").write_text("# new\n")
    assert module.main() == 1


def test_untranslated_page_passes_once_declared(site):
    """English-only stays possible — what is refused is leaving it unsaid."""
    module, _, docs, _ = site
    (docs / "new.md").write_text("# new\n")
    (docs / ".i18n-english-only").write_text(
        "# reason: generated reference, translated upstream\ndocs/new.md\n"
    )
    assert module.main() == 0


def test_stale_allowlist_entry_fails(site):
    """A waiver outliving its page turns the list into fiction; it must expire loudly."""
    module, _, docs, _ = site
    (docs / ".i18n-english-only").write_text("docs/gone.md\n")
    assert module.main() == 1


def test_translated_page_may_not_sit_in_the_allowlist(site):
    """Declaring a page English-only while it *has* a translation is a contradiction."""
    module, _, docs, _ = site
    (docs / ".i18n-english-only").write_text("docs/guide.md\n")
    assert module.main() == 1


def test_nav_label_without_translation_fails(site):
    """A missing nav_translations entry does not fail the build — it ships English."""
    module, _, _, mkdocs = site
    mkdocs.write_text(MKDOCS.replace("            Guide: ガイド\n", ""))
    assert module.main() == 1


def test_section_heading_label_is_covered_too(site):
    """Section headings render in the nav as well, so they are checked like leaves."""
    module, _, _, mkdocs = site
    mkdocs.write_text(MKDOCS.replace("            Design documents: 設計ドキュメント\n", ""))
    assert module.main() == 1


def test_non_strict_reports_without_failing(site):
    """Local mode: the same findings, exit 0, for working on a page pre-translation."""
    module, _, docs, _ = site
    (docs / "new.md").write_text("# new\n")
    module.sys.argv = ["check-i18n-coverage.py"]
    assert module.main() == 0


def test_the_real_tree_is_covered():
    """The repository itself passes — the gate is wired to a tree, not to a fixture."""
    module = _load()
    module.sys.argv = ["check-i18n-coverage.py", "--strict"]
    assert module.main() == 0
