"""The block candidate golden says what recall's candidate generation returns.

``tests/golden/block_candidates.json`` is the conformance data another
implementation of the block arm's candidate generation is held to
(``docs/BLOCK_CANDIDATES_CONTRACT.md``). Two things keep it honest: the expected
rows are what the code recall runs returns today, and the cases still exercise
every rule the contract names. If the first fails, either the code changed by
accident or an intended change needs ``scripts/capture-block-candidates.py`` and
every other implementation has to follow; if the second fails, a case stopped
covering a rule and an implementation could break that rule unnoticed.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from block_candidates_cases import build_cases, capture, observe, to_json

GOLDEN = Path(__file__).parent / "golden" / "block_candidates.json"


def _golden() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_the_golden_is_what_recall_returns():
    """Every case's rows, query, caps and expected rows, as the code observes them now."""
    assert GOLDEN.read_text(encoding="utf-8") == to_json(await capture())


@pytest.mark.asyncio
async def test_a_case_is_observed_from_its_rows_and_not_their_insertion_order():
    """The expected rows depend on the table's key alone."""
    case = next(c for c in build_cases() if c.name == "examined_cap_binds")
    first = await observe(case)
    case.name = "examined_cap_binds, inserted in another order"
    assert await observe(case) == first


def _distance(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def test_the_cases_exercise_every_rule_the_contract_names():
    cases = _golden()["cases"]
    by_name = {c["name"]: c for c in cases}
    assert len(by_name) == len(cases), "case names are unique"

    def any_case(predicate):
        return any(predicate(c) for c in cases)

    # The result: most cases return rows, and the empty answer is held too.
    assert sum(1 for c in cases if c["expected"]) >= len(cases) - 2
    assert any_case(lambda c: not c["expected"])

    # The depth cuts some cases and not others.
    assert any_case(lambda c: len(c["expected"]) == c["caps"]["depth"])
    assert any_case(lambda c: 0 < len(c["expected"]) < c["caps"]["depth"])

    # Both caps bind somewhere: more rows than the examined cap, and a record
    # holding more rows than its share.
    assert any_case(lambda c: len(c["rows"]) > c["caps"]["examined"])
    assert any_case(
        lambda c: max(Counter((r[0], r[1]) for r in c["rows"]).values()) > c["caps"]["per_parent"]
    )

    # The cut falls inside a run of equal distances, so the tie order decides.
    def tie_at_the_cut(c):
        if len(c["expected"]) < c["caps"]["depth"]:
            return False
        last = c["expected"][-1][3]
        kept = {tuple(h[:3]) for h in c["expected"]}
        width = len(c["query"]["bits"])
        return any(
            r[7] is not None and len(r[7]) == width and tuple(r[:3]) not in kept
            and _distance(r[7], c["query"]["bits"]) == last
            for r in c["rows"]
        )

    assert any_case(tie_at_the_cut)

    # The rows the filter refuses or the measure skips.
    assert any_case(lambda c: any(r[7] is None for r in c["rows"]))
    assert any_case(lambda c: any(r[6] not in c["query"]["models"] for r in c["rows"]))
    assert any_case(lambda c: any(r[6] == "" for r in c["rows"]) and "" in c["query"]["models"])
    assert any_case(lambda c: any(r[7] is not None and len(r[7]) < len(c["query"]["bits"]) for r in c["rows"]))
    assert any_case(lambda c: any(r[7] is not None and len(r[7]) > len(c["query"]["bits"]) for r in c["rows"]))

    # Both kinds, interleaved in key order.
    assert any_case(lambda c: {r[0] for r in c["rows"]} == {"ep", "mem"})

    # Every reading of the axes.
    queries = [c["query"] for c in cases]
    assert any(q["agent_id"] == "" for q in queries)
    assert {q["project_id"] for q in queries} >= {None, "", "proj-x"}
    assert any(q["channel"] == "" for q in queries) and any(q["channel"] for q in queries)
    assert any_case(lambda c: len({r[3] for r in c["rows"]}) > 1)


def test_the_rows_of_a_case_are_in_key_order():
    """The contract hands an implementation its rows in key order; the file does too."""
    for case in _golden()["cases"]:
        keys = [(r[0], r[1], r[2]) for r in case["rows"]]
        assert keys == sorted(keys) and len(set(keys)) == len(keys), case["name"]
