"""Assert the 2.5.2 split did not change behaviour (CSC Task #293).

`tests/behaviour_252.py` defines the scenario matrix and what an observation
contains; `scripts/capture-behaviour.py` recorded the pre-refactor answers into
`tests/golden/behaviour_252.json`. This replays the matrix and diffs.

A failure here means the refactor changed something. It does not say whether the
change is a bug -- only that the claim "this is a pure code move" is false as
stated, which is the claim the whole alpha stage rests on.
"""

from __future__ import annotations

import difflib
import json
import math
from pathlib import Path
from typing import Any

import pytest

from behaviour_252 import SCENARIOS, fake_embed_one, observe, to_json

GOLDEN = Path(__file__).parent / "golden" / "behaviour_252.json"

# Cross-platform float tolerance. `behaviour_252.canonical` already rounds to
# FLOAT_PLACES (10) at observe time -- tight enough to catch any behavioural
# change but ALSO tight enough to catch bit-level drift between macOS arm64
# (where the golden was recorded) and Linux x86_64 (CI). Observed ULP delta on
# `_cosine`: ~6e-8, which perturbs the tenth decimal.
#
# We compare with a pairwise absolute tolerance instead of the raw dict `==`.
# Pairwise (rather than "round both sides to N places") is deliberate:
# rounding has a boundary problem -- a value near 0.xxxxxx5 will round up on
# one platform and down on the other, and a genuine ULP drift can straddle the
# boundary. `math.isclose` sidesteps that entirely: |a - b| <= abs_tol is
# transitively stable regardless of where the value sits.
#
# The tolerance (1e-5) leaves ~two decades of margin over the observed drift
# (6e-8) and is many orders of magnitude tighter than any behavioural change
# -- a different candidate set, threshold or ranking moves these scores in the
# first two decimals, not the fifth.
#
# The golden file on disk is not rewritten. Diffs are rendered at
# _DIFF_FLOAT_PLACES so a real failure is readable; the equality decision is
# always the pairwise walk above.
_COMPARE_ABS_TOL = 1e-5
_DIFF_FLOAT_PLACES = 6


def _structures_equal(a: Any, b: Any, *, abs_tol: float = _COMPARE_ABS_TOL) -> bool:
    """Deep-compare two JSON-shaped structures with float tolerance.

    Everything else (strings, ints, bools, None, dict keys, list order) is
    compared exactly -- only float leaves get the tolerance. A missing key,
    extra row, or type change surfaces immediately.
    """
    if isinstance(a, float) or isinstance(b, float):
        # int/float mix (json.loads may hand back ints for whole numbers): treat
        # both as floats for the comparison.
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            return False
        return math.isclose(float(a), float(b), abs_tol=abs_tol, rel_tol=0.0)
    if isinstance(a, dict):
        if not isinstance(b, dict) or a.keys() != b.keys():
            return False
        return all(_structures_equal(a[k], b[k], abs_tol=abs_tol) for k in a)
    if isinstance(a, list):
        if not isinstance(b, list) or len(a) != len(b):
            return False
        return all(_structures_equal(x, y, abs_tol=abs_tol) for x, y in zip(a, b))
    return a == b


def _round_for_diff(obj: Any) -> Any:
    """Round floats to a stable precision so unified_diff of a real failure
    is readable (long tail decimals bury the actual difference)."""
    if isinstance(obj, float):
        r = round(obj, _DIFF_FLOAT_PLACES)
        return 0.0 if r == 0.0 else r
    if isinstance(obj, dict):
        return {k: _round_for_diff(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round_for_diff(v) for v in obj]
    return obj


@pytest.fixture(scope="module")
def golden() -> dict:
    if not GOLDEN.exists():
        pytest.fail(
            f"missing {GOLDEN}. It is the pre-refactor behaviour and cannot be "
            "reconstructed from the current code -- restore it from git rather "
            "than regenerating, or the comparison is vacuous."
        )
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
@pytest.mark.asyncio
async def test_behaviour_matches_the_pre_refactor_golden(scenario, golden):
    if scenario.id not in golden:
        pytest.fail(
            f"scenario {scenario.id!r} has no recorded behaviour. If it is new, run "
            "`uv run python scripts/capture-behaviour.py` BEFORE the refactor it "
            "guards -- a golden captured afterwards agrees with the code by "
            "construction and proves nothing."
        )

    observed = await observe(scenario)
    expected = golden[scenario.id]

    if not _structures_equal(observed, expected):
        diff = "".join(
            difflib.unified_diff(
                to_json(_round_for_diff(expected)).splitlines(keepends=True),
                to_json(_round_for_diff(observed)).splitlines(keepends=True),
                fromfile="recorded before the refactor",
                tofile="observed now",
            )
        )
        pytest.fail(f"behaviour changed for {scenario.id} ({scenario.covers}):\n{diff}")


def test_the_golden_covers_every_scenario(golden):
    """A scenario deleted from the matrix silently reduces coverage. The golden
    is the record of what was once guarded, so a key with no scenario is either a
    deletion to justify or a rename that lost its history."""
    orphans = sorted(set(golden) - {s.id for s in SCENARIOS})
    assert not orphans, (
        f"the golden records scenarios the matrix no longer runs: {orphans}. "
        "Removing an input shape is a coverage decision -- make it deliberately."
    )


def test_the_local_embedding_stays_in_step_with_conftest():
    """behaviour_252 duplicates conftest's fake embedding (the capture script
    runs outside pytest, where conftest is not importable). If they drift, the
    golden was recorded against vectors the suite no longer produces and every
    similarity in it becomes fiction."""
    from conftest import fake_embed_one as conftest_embed

    for text in ("apples", "raspberry pi cluster wiring", "", "  ", "同じ日本語"):
        assert fake_embed_one(text) == conftest_embed(text), (
            f"the two fake embeddings disagree on {text!r} -- reconcile them and "
            "re-capture the golden"
        )
