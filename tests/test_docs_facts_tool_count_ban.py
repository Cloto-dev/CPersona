"""The docs-facts gate rejects a total tool count wherever it is written.

The count used to be checked against the registry, and that check held on every
page it could read while the same number went stale on the surfaces it could not
reach: the GitHub repository description (30), the registry's frozen per-version
copy (29), a code comment (29) — all while the registry served 31 (2026-09-05).
A number that leaks past a path list cannot be kept true, so the gate now fails
on the claim itself. These tests pin that the detector fires on a known positive
in both languages, stays quiet on the per-axis phrasing the tools page still
uses, and that the per-axis patterns still reach that page — a pattern that
quietly stops matching returns the same green as one that matched and agreed.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _gate():
    path = ROOT / "scripts" / "check-docs-facts.py"
    spec = importlib.util.spec_from_file_location("check_docs_facts_tool_count", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_tool_count_check(cdf, tmp_path: Path, text: str) -> list[str]:
    page = tmp_path / "page.md"
    page.write_text(text, encoding="utf-8")
    cdf.DOC_FILES = [page]
    cdf.MANIFEST_FILES = []
    cdf.ROOT = tmp_path
    cdf.failures.clear()
    cdf.check_tool_and_schema_claims(tool_count=31, schema_version=13)
    return list(cdf.failures)


@pytest.mark.parametrize(
    "claim",
    [
        "Single SQLite file. 31 tools. Zero LLM dependency.",
        "ships 31 MCP tools in one package",
        "All 30 tools, grouped by purpose",
        "31 個のツールを目的別にまとめ",
        "31個のツールが使えます",
    ],
)
def test_a_total_tool_count_is_rejected(tmp_path, claim):
    failures = _run_tool_count_check(_gate(), tmp_path, claim)
    assert len(failures) == 1, failures
    assert "total tool count" in failures[0]


@pytest.mark.parametrize(
    "prose",
    [
        # The per-axis phrasing the tools page keeps: a count of schemas that
        # offer one argument, measured by check_axis_claims, not a total.
        "`agent_id` is accepted by most tools (22); `project_id` by six",
        "`agent_id` はほとんどのツール\n(22 個) が受け取り、`project_id` は 6 個",
        "Every tool, grouped by what you reach for it for",
        "すべてのツールを目的別にまとめ",
        "Schema v13 (auto-migrating)",
    ],
)
def test_phrasing_without_a_total_passes(tmp_path, prose):
    assert _run_tool_count_check(_gate(), tmp_path, prose) == []


def test_axis_patterns_still_reach_the_tools_page():
    """The agent_id patterns were reworded with the page; prove they still hit it."""
    cdf = _gate()
    (axis, en_pattern, ja_pattern), *_ = cdf._AXIS_COUNT_CLAIMS
    assert axis == "agent_id"
    en = (ROOT / "docs" / "tools.md").read_text(encoding="utf-8")
    ja = (ROOT / "docs" / "tools.ja.md").read_text(encoding="utf-8")
    assert re.search(en_pattern, en), "docs/tools.md no longer states the agent_id count in a form the gate reads"
    assert re.search(ja_pattern, ja), "docs/tools.ja.md no longer states the agent_id count in a form the gate reads"
