"""Assert the 2.5.2 split did not change behaviour.

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

# Keys that did not exist when the golden was recorded.
#
# The golden's claim is "the 2.5.2 split changed nothing". A field added
# deliberately by a LATER version is not that refactor changing behaviour, but it
# still lands in this diff. The two ways out are not equivalent:
#
#   - Re-record the golden. One command, and it makes the file agree with the
#     code by construction -- which is what the file exists NOT to do. It would
#     also discard the evidence behind every earlier equivalence claim, not just
#     this scenario's.
#   - Name the new key here. The golden keeps every value it recorded under full
#     comparison, and admitting a key stays a visible, reviewed edit.
#
# So: additions are listed, never absorbed. Anything that changes a value the
# golden already holds still fails, which is the whole point.
#
# repairable (2.5.5): every fix_capable check now declares how many
# rows its fix would write. Additive -- severity, status and counts are
# unchanged, as this diff itself showed. Guarded by
# tests/test_255_repairable_contract.py, which is where its behaviour is pinned.
#
# The 2.5.5 import/merge integrity batch (bug-218..bug-246) is NOT listed here.
# It changes recorded VALUES, not just keys -- a move stops deleting rows it never
# copied, an import stops duplicating episodes, a merged episode keeps its
# created_at -- so the eleven affected scenarios were re-recorded and the diff on
# them is the review surface, exactly as the header above prescribes for an
# intended change. The invariants themselves are pinned in tests/test_review_b.py.
#
# checks_run (bug-230): check_health echoes the registry names it executed, the
# way deep_check always has. Additive -- it reports what the run did instead of
# changing it -- and it exists because an unrecognised name selected nothing and
# the all-zero result read as a clean bill of health. Pinned in
# tests/test_review_c_fixes.py.
#
# advisory_scope (bug-251): the degraded advisory now names what its
# once-per-episode suppression is keyed on, because that state is per PROCESS and
# a shared transport makes a process several sessions. Additive -- severity,
# reason, evidence and the runbook this scenario recorded are unchanged, and the
# recorded run is stdio, where the answer is "session" and nothing downgraded
# differently. Pinned in tests/test_bug251_advisory_scope.py.
_KEYS_ADDED_SINCE_GOLDEN = {"repairable", "checks_run", "advisory_scope"}


# Values the golden DOES hold that a later version deliberately changed.
#
# Different from the additions above, and deliberately harder to use: a recorded
# value is evidence, so an entry names one scenario, one key path and the change
# that owns it, and nothing else in that scenario is relaxed.
#
# ("health-fix-repairs-warn", ("result", "issues")) — bug-225: a fix run used to
# answer with the RESIDUAL issue list (empty once the repair converged), which
# discarded every field a runner emits only under fix=True (`fixed`,
# `fix_error`, `mapped`, `remaining`) before any caller could read it.
# `issues` is now what the FIX run found; `severity_summary` and `status` are
# still the residual verdict and are still compared here, so the bug-059
# property this scenario was recorded for stays pinned. Behaviour pinned in
# tests/test_review_c_fixes.py.
_VALUES_CHANGED_SINCE_GOLDEN = {
    ("health-fix-repairs-warn", ("result", "issues")),
}


def _drop_keys_added_since_golden(obj: Any, recorded: Any = None) -> Any:
    """Recursively remove post-golden keys the golden does not hold here.

    Position-aware, not name-only: `checks_run` is new on check_health (bug-230)
    but deep_check has always emitted it and the golden records it, so a rule
    keyed on the name alone would delete the RECORDED one from the comparison —
    quietly retiring the evidence this file exists to keep.
    """
    if isinstance(obj, dict):
        rec = recorded if isinstance(recorded, dict) else {}
        return {
            k: _drop_keys_added_since_golden(v, rec.get(k))
            for k, v in obj.items()
            if k in rec or k not in _KEYS_ADDED_SINCE_GOLDEN
        }
    if isinstance(obj, list):
        rec = recorded if isinstance(recorded, list) else []
        return [
            _drop_keys_added_since_golden(v, rec[i] if i < len(rec) else None)
            for i, v in enumerate(obj)
        ]
    return obj


def _without_changed_values(obj: Any, scenario_id: str) -> Any:
    """Drop the key paths listed in _VALUES_CHANGED_SINCE_GOLDEN for this scenario."""
    paths = [p for sid, p in _VALUES_CHANGED_SINCE_GOLDEN if sid == scenario_id]
    if not paths:
        return obj
    out = json.loads(json.dumps(obj))
    for path in paths:
        node = out
        for key in path[:-1]:
            node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, dict):
            node.pop(path[-1], None)
    return out


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

    expected = golden[scenario.id]
    observed = _drop_keys_added_since_golden(await observe(scenario), expected)
    observed = _without_changed_values(observed, scenario.id)
    expected = _without_changed_values(expected, scenario.id)

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


# ---------------------------------------------------------------------------
# The harness's own integrity: an observation must be a fact about the scenario,
# not about the run order
# ---------------------------------------------------------------------------
#
# `observe` empties the tables between scenarios, but the package also keeps
# module-level state that a scenario writes and the next one reads. Two such
# leaks were live when these tests were written (see `behaviour_252._reset`), and
# neither announced itself: they do not raise, they change what the golden
# records. A golden captured under a leak is a recording of one particular
# ordering, so every equivalence claim above rests on these two tests.
#
# They are shaped to fail for one reason each. The first needs a scenario whose
# answer depends on the pool SIZE, so it only bites when a count leaks; the
# second needs a scenario that fires the degraded advisory, so it only bites when
# the advisory's suppression memory leaks. Deleting either line from `_reset`
# turns exactly one of them red.


async def _seed_probe(ctx, n_filler: int, agent: str) -> None:
    """`n_filler` embedded rows that do not match the query, plus six that match
    it lexically and carry NO embedding.

    The six come back unscored, and an unscored row survives the quality gate
    only when the pool holds at least 100 rows (`_apply_quality_gate`'s volume
    rule). So this corpus answers with six rows or with none, decided purely by
    the pool count the gate is handed -- which is what makes a leaked count
    visible as a behavioural difference rather than an internal one.
    """
    from behaviour_252 import _mem_raw, pack

    db = ctx.db
    for i in range(n_filler):
        await _mem_raw(
            db, agent=agent, content=f"filler text number {i}",
            timestamp=f"2026-01-02T00:00:{i % 60:02d}Z",
            created_at=f"2026-01-02 00:00:{i % 60:02d}",
            blob=pack(f"filler text number {i}"),
        )
    for i in range(6):
        await db.execute(
            "INSERT INTO memories (agent_id, content, timestamp, created_at) VALUES (?,?,?,?)",
            (agent, f"apples row {i}", f"2026-01-01T00:00:{i:02d}Z", f"2026-01-01 00:00:{i:02d}"),
        )
    await db.commit()


def _rescope(observation: dict, agent: str) -> dict:
    """Replace one arm's agent id with a placeholder so two arms in different
    isolation scopes compare as the same observation."""
    return json.loads(to_json(observation).replace(agent, "<agent>"))


def _pool_size_probe(sid: str, n_filler: int, agent: str = "probe"):
    from behaviour_252 import Scenario, install_local

    async def run(ctx):
        from cpersona import memory_handlers

        install_local(ctx)
        return await memory_handlers.do_recall(agent, "apples", 6)

    return Scenario(
        id=sid,
        seam="harness-integrity",
        covers="the recall pool count the quality gate is handed",
        run=run,
        seed=lambda ctx: _seed_probe(ctx, n_filler, agent),
    )


@pytest.mark.asyncio
async def test_an_observation_does_not_depend_on_the_scenario_that_ran_before_it():
    """A 106-row corpus must answer the same whichever corpus preceded it.

    `scope_stats` caches the pool counts per isolation scope, and BOTH of its
    invalidation paths are blind inside this harness: the write-generation
    counter is bumped in `database.py`'s write seam and these fixtures INSERT on
    the connection directly, and the TTL cannot elapse between two scenarios
    microseconds apart. So without an explicit clear the second scenario is gated
    on the first one's pool size.

    Each arm runs a 106-row corpus after a 6-row one; they differ only in whether
    that predecessor shared the isolation scope. The cache is keyed on
    (agent_id, project_id, channel), so the control's predecessor -- under its own
    agent -- cannot leave an entry the control's own run would read, and that run
    therefore counts for real.

    The two arms must not share a scope either, and that is load-bearing rather
    than tidy. A stale entry is sticky: the run that reads it returns before the
    recompute, so it neither corrects the entry nor refreshes its stamp. Whichever
    arm touches a key first therefore fixes the answer every later run under that
    key receives, and the arms agree no matter what the code does. Two earlier
    drafts of this test shared a key -- once poisoned toward the wrong answer and
    once toward the right one -- and both stayed green against the live bug.
    """
    control = _pool_size_probe("probe-large", 100, agent="scope-control")
    leaked = _pool_size_probe("probe-large", 100, agent="scope-leaked")

    await observe(_pool_size_probe("probe-small", 0, agent="scope-control-pred"))
    control_obs = _rescope(await observe(control), "scope-control")

    await observe(_pool_size_probe("probe-small", 0, agent="scope-leaked"))
    leaked_obs = _rescope(await observe(leaked), "scope-leaked")

    assert _structures_equal(leaked_obs, control_obs), (
        "the same corpus answered differently depending on what ran before it -- "
        "module state survived `_reset`, so the golden records a run order rather "
        "than a behaviour:\n"
        + "".join(
            difflib.unified_diff(
                to_json(_round_for_diff(control_obs)).splitlines(keepends=True),
                to_json(_round_for_diff(leaked_obs)).splitlines(keepends=True),
                fromfile="106 rows, after a 6-row scenario in ANOTHER scope",
                tofile="106 rows, after a 6-row scenario in the SAME scope",
            )
        )
    )


@pytest.mark.asyncio
async def test_repeating_a_scenario_reproduces_it():
    """The degraded advisory fires the full runbook once per episode and a short
    reminder afterwards, and that "once" is module state in `cpersona.health`.

    This harness pins `EMBEDDING_MODE=none`, so every recall scenario enters the
    `hint` state and one of them records the advisory in the golden. Without a
    reset the payload it records -- 1098 characters or 151 -- is decided by
    whether any earlier scenario in the run already fired one.
    """
    scenario = next(s for s in SCENARIOS if s.id == "recall-empty-query-pure-recency")

    first = await observe(scenario)
    second = await observe(scenario)

    assert _structures_equal(first, second), (
        "a scenario observed differently the second time in one process -- module "
        "state survived `_reset`:\n"
        + "".join(
            difflib.unified_diff(
                to_json(_round_for_diff(first)).splitlines(keepends=True),
                to_json(_round_for_diff(second)).splitlines(keepends=True),
                fromfile="first run",
                tofile="second run",
            )
        )
    )
