"""bug-282: the docs-facts env-name gate reaches names written outside tables.

`check_env_tables` reads lines that start with `|`. The reference page a reader
with a broken install is sent to writes its variables as bullets, so every one
of them sat outside the gate: renaming `CPERSONA_EMBEDDING_URL` to
`CPERSONA_EMBEDDING_URl` in that page left `check-docs-facts` at exit 0 although
no such name exists anywhere in the source.

The claim pinned here is the one the gate is trusted for — a documented variable
is a real one — over the surface the docs actually use, so the mutation that
exposed the gap is the test. The companion below keeps it from being vacuous:
the same page unmutated must be clean, otherwise a check that failed everything
would pass this file.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
REPAIR_PAGE = ROOT / "skills" / "cpersona-memory" / "references" / "embedding-backend-repair.md"


def _docs_facts():
    path = ROOT / "scripts" / "check-docs-facts.py"
    spec = importlib.util.spec_from_file_location("check_docs_facts_bug282", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_typo_in_an_inline_name_fails_the_gate():
    cdf = _docs_facts()
    text = REPAIR_PAGE.read_text(encoding="utf-8")
    assert "CPERSONA_EMBEDDING_URL" in text, (
        "this test mutates a name the page is expected to write inline; the page "
        "changed, so re-aim the mutation rather than deleting the test"
    )

    mutated = text.replace("CPERSONA_EMBEDDING_URL", "CPERSONA_EMBEDDING_URl")
    hits = cdf.env_name_violations(mutated, cdf.known_env_names())

    assert [token for _, token in hits] == ["CPERSONA_EMBEDDING_URl"], (
        "a name no source file reads must be reported wherever the page writes "
        f"it, not only in a table row; got {hits}"
    )


def test_the_unmutated_page_is_clean():
    """Without this the check above passes even if the gate failed everything."""
    cdf = _docs_facts()
    hits = cdf.env_name_violations(REPAIR_PAGE.read_text(encoding="utf-8"), cdf.known_env_names())
    assert hits == [], f"the shipped page names variables the source does not read: {hits}"


def test_the_gate_reads_more_than_table_rows():
    """The mutation above only proves the widening if the name is not in a table.

    A page that happened to repeat the variable in a table row would make the
    test pass against the old, narrow reading too — which is exactly the false
    green being ruled out.
    """
    table_rows = [
        line for line in REPAIR_PAGE.read_text(encoding="utf-8").splitlines()
        if line.startswith("|") and "CPERSONA_EMBEDDING_URL" in line
    ]
    assert table_rows == [], (
        "the mutated name now also appears in a table row, so this file no "
        "longer demonstrates that the gate reads inline text"
    )


@pytest.mark.parametrize(
    "text, expected",
    [
        # A family name is satisfied by any real member, so prose may write it.
        ("the `CPERSONA_CALIBRATE_*` pair", []),
        # ...but the prefix itself is still checked, which an allowlist entry
        # for the token would have given up.
        ("the `CPERSONA_CALIBRAT_*` pair", ["CPERSONA_CALIBRAT_*"]),
        # The bare prefix names the prefix, not a variable.
        ("`CPERSONA_`-prefixed names win", []),
    ],
)
def test_family_and_prefix_forms(text, expected):
    cdf = _docs_facts()
    assert [token for _, token in cdf.env_name_violations(text, cdf.known_env_names())] == expected
