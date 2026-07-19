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
from pathlib import Path

import pytest

from behaviour_252 import SCENARIOS, fake_embed_one, observe, to_json

GOLDEN = Path(__file__).parent / "golden" / "behaviour_252.json"


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

    if observed != expected:
        diff = "".join(
            difflib.unified_diff(
                to_json(expected).splitlines(keepends=True),
                to_json(observed).splitlines(keepends=True),
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
