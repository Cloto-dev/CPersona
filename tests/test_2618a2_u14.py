"""Documentation truth: the SQLite minimum, an absolute example database path,
and the docs-facts gate seeing the upgrade guide's schema claim."""

from __future__ import annotations

import importlib.util
import re
import sqlite3
from pathlib import Path

import aiosqlite
import pytest

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "plugin" / "skills" / "cpersona-memory" / "SKILL.md"


# --- bug-837: SQLite 3.34 (FTS5 trigram tokenizer) is required -----------------


@pytest.mark.parametrize(
    "page",
    ["docs/getting-started.md", "docs/getting-started.ja.md", "docs/faq.md", "docs/faq.ja.md"],
)
def test_bug_837_docs_state_the_sqlite_minimum_and_the_way_out(page):
    text = (ROOT / page).read_text(encoding="utf-8")
    assert re.search(r"3\.34 (?:or later|以上)", text), f"{page} does not state the SQLite 3.34 minimum"
    assert "trigram" in text, f"{page} does not say why 3.34 (the FTS5 trigram tokenizer)"
    assert "CPERSONA_FTS_ENABLED=false" in text, f"{page} does not name the switch for an older SQLite"


def test_bug_837_getting_started_lists_sqlite_under_prerequisites():
    for page, heading, minimum in (
        ("docs/getting-started.md", "## Prerequisites", "SQLite 3.34 or later"),
        ("docs/getting-started.ja.md", "## 前提条件", "SQLite 3.34 以上"),
    ):
        text = (ROOT / page).read_text(encoding="utf-8")
        section = text.split(heading, 1)[1].split("\n## ", 1)[0]
        assert minimum in section, f"{page}: the SQLite minimum is not in the prerequisites"


@pytest.mark.asyncio
async def test_bug_837_old_sqlite_boot_error_names_minimum_and_switch(tmp_path, monkeypatch):
    from cpersona import database

    assert database.FTS_ENABLED
    real = aiosqlite.Connection.executescript

    async def old_sqlite_executescript(self, sql):
        # An SQLite before 3.34 has FTS5 but no trigram tokenizer.
        if "tokenize='trigram'" in sql:
            raise sqlite3.OperationalError("no such tokenizer: trigram")
        return await real(self, sql)

    monkeypatch.setattr(aiosqlite.Connection, "executescript", old_sqlite_executescript)
    db = await aiosqlite.connect(str(tmp_path / "old.db"))
    try:
        with pytest.raises(sqlite3.OperationalError) as ei:
            await database._init_schema(db)
    finally:
        await db.close()
    msg = str(ei.value)
    assert "SQLite 3.34" in msg, f"boot error does not name the minimum: {msg}"
    assert "CPERSONA_FTS_ENABLED=false" in msg, f"boot error does not name the switch: {msg}"
    assert sqlite3.sqlite_version in msg, f"boot error does not name the SQLite it found: {msg}"


@pytest.mark.asyncio
async def test_bug_837_other_fts_errors_are_not_relabelled(tmp_path, monkeypatch):
    from cpersona import database

    real = aiosqlite.Connection.executescript

    async def broken_executescript(self, sql):
        if "tokenize='trigram'" in sql:
            raise sqlite3.OperationalError("disk I/O error")
        return await real(self, sql)

    monkeypatch.setattr(aiosqlite.Connection, "executescript", broken_executescript)
    db = await aiosqlite.connect(str(tmp_path / "io.db"))
    try:
        with pytest.raises(sqlite3.OperationalError) as ei:
            await database._init_schema(db)
    finally:
        await db.close()
    assert str(ei.value) == "disk I/O error"


# --- bug-839: the skill's example CPERSONA_DB_PATH must be absolute -------------

# A CPERSONA_DB_PATH example whose value starts with '~' or $HOME: the client
# starts the server without a shell, so neither is expanded.
_TILDE_DB_PATH = re.compile(r"CPERSONA_DB_PATH[`\"']?\s*(?:\(e\.g\.\s*`?|[=:]\s*[\"']?)(~|\$HOME)")


def _shipped_pages():
    pages = [ROOT / "README.md", SKILL, *sorted((SKILL.parent / "references").glob("*.md"))]
    pages += sorted((ROOT / "docs").glob("*.md"))
    return pages


def test_bug_839_no_db_path_example_relies_on_shell_expansion():
    hits = []
    for page in _shipped_pages():
        for n, line in enumerate(page.read_text(encoding="utf-8").splitlines(), 1):
            if _TILDE_DB_PATH.search(line):
                hits.append(f"{page.relative_to(ROOT)}:{n}: {line.strip()}")
    assert not hits, "CPERSONA_DB_PATH example is not an absolute path:\n" + "\n".join(hits)


def test_bug_839_skill_example_is_absolute_and_says_why():
    text = SKILL.read_text(encoding="utf-8")
    m = re.search(r"Pick an absolute `CPERSONA_DB_PATH`[^.]*?\(e\.g\.\s*`([^`]+)`", text)
    assert m, "the skill no longer gives an example CPERSONA_DB_PATH"
    assert m.group(1).startswith("/"), f"example path is not absolute: {m.group(1)}"
    tail = text[m.end() : m.end() + 600]
    assert "`~`" in tail and "without a shell" in tail, "the skill does not say that '~' is not expanded"


# --- bug-838: the docs-facts gate sees the upgrade guide's schema claim -------


def _gate():
    path = ROOT / "scripts" / "check-docs-facts.py"
    spec = importlib.util.spec_from_file_location("cdf_u14", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


UPGRADE_PAGES = ("upgrading-to-2.6.md", "upgrading-to-2.6.ja.md")


def test_bug_838_upgrade_pages_are_scanned():
    names = {p.name for p in _gate().DOC_FILES}
    for page in UPGRADE_PAGES:
        assert page in names, f"{page} is not scanned by docs-facts"


def test_bug_838_upgrade_pages_agree_with_schema_version():
    cdf = _gate()
    cdf.DOC_FILES = [ROOT / "docs" / p for p in UPGRADE_PAGES]
    cdf.MANIFEST_FILES = []
    cdf.failures.clear()
    cdf.check_tool_and_schema_claims(0, cdf.measured_schema_version())
    assert not cdf.failures, cdf.failures


def test_bug_838_gate_reads_the_schema_claim_in_both_languages():
    # One version past the real one: every current-state schema claim on the
    # pages must now be reported, in English and in Japanese.
    cdf = _gate()
    cdf.DOC_FILES = [ROOT / "docs" / p for p in UPGRADE_PAGES]
    cdf.MANIFEST_FILES = []
    cdf.failures.clear()
    cdf.check_tool_and_schema_claims(0, cdf.measured_schema_version() + 1)
    for page in UPGRADE_PAGES:
        assert any(f.startswith(f"docs/{page}:") for f in cdf.failures), (
            f"a wrong schema version on {page} is not reported: {cdf.failures}"
        )


def test_bug_838_schema_claim_pattern_exemptions(tmp_path):
    cdf = _gate()
    cdf.ROOT = tmp_path
    page = tmp_path / "page.md"
    cdf.DOC_FILES = [page]
    cdf.MANIFEST_FILES = []

    def failures_for(text):
        page.write_text(text, encoding="utf-8")
        cdf.failures.clear()
        cdf.check_tool_and_schema_claims(0, 21)
        return list(cdf.failures)

    # Stale current-state claims, each spelling.
    assert failures_for("It migrates to schema version 20 (19 for older).\n")
    assert failures_for("Schema version 20 is current.\n")
    assert failures_for("データベースをスキーマのバージョン 20 まで移行します。\n")
    assert failures_for("We are on schema v20.\n")
    # Correct claims pass.
    assert not failures_for("It migrates to schema version 21 (20 for older).\n")
    assert not failures_for("データベースをスキーマのバージョン 21 まで移行します。\n")
    # A migration's starting point and table rows are history, not current state.
    assert not failures_for("It migrates from schema version 13 to schema version 21.\n")
    assert not failures_for("2.5.x のスキーマのバージョン 13 から、スキーマのバージョン 21 まで。\n")
    assert not failures_for("| Schema version 14 | 2.6.0a3 | nodes |\n")
