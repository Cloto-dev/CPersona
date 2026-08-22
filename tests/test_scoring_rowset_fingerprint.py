"""The second SCORING_VERSION detector: a fingerprint over the real scoring CALL SITE.

``test_255a1_scoring_fingerprint.py`` (bug-209) pins the distribution by driving two
PURE FUNCTIONS — ``_compute_confidence`` and ``_episode_boundary_factor`` — across a
fixed input grid. That grid is blind to a whole class of distribution shift, and the
blindness is structural rather than accidental: which rows the episode-boundary penalty
is applied TO is decided by the loop in ``_apply_recall_scoring``, not by either
function. Add an exclusion to that loop and both functions keep returning exactly what
they returned before, so the grid's hash cannot move.

That is not hypothetical. bug-257 (episode rows exempt from the boundary penalty) was
exactly such a call-site membership change; it moved SCORING_VERSION only because a
human noticed, and the sibling test says so in its own golden comment ("the fingerprint
deliberately stays the same while the version moves"). bug-209 itself was filed after
PR #88 shifted the distribution and sailed through CI green. Two shifts, one detector,
and the second one went around it.

This test closes that class by hashing the OUTPUT OF THE CALL SITE. It drives the real
``_apply_recall_scoring`` over a fixed rowset against a fixed database state under a
frozen clock, across all four (CONFIDENCE_ENABLED × EPISODE_PENALTY_ENABLED) arms plus
two channel-scoped arms and two arms over a second, homogeneous rowset, and pins the
SHA-256 of the resulting score columns *in order*. A membership change moves the hash
because the excluded row's scores — and usually the row order — change. The scoped arms
extend that to the boundary's own isolation scoping (bug-147), which the unscoped arms
cannot see because they apply no channel filter; the homogeneous arms extend it to the
bug-115/126 re-sort, which a ragged rowset never reaches.

Decision table on mismatch: identical to the sibling test (fingerprint moved with
SCORING_VERSION unchanged is the RED this exists for; both moved is a re-pin RED; a bump
with no movement re-pins the version half of the pair).

Mutation record (production code only, SCORING_VERSION held fixed; measured on the
commit that added this file):

  - an extra exclusion in the penalty loop        -> this file RED, sibling green
  - the bug-126 profile-row exclusion removed
    from the re-sort homogeneity guard            -> this file RED, sibling green
  - the channel axis dropped from the boundary
    read                                          -> this file RED, sibling green

The sibling staying green in all three is the point: they are the class it cannot see.
The second of the three was NOT caught by the first draft of this file — the ragged
rowset made the re-sort unreachable — which is why ``test_penalty_resort_actually_reorders``
and ``test_scoped_arm_sees_a_different_boundary`` exist. A fingerprint over a fixture
that never reaches a branch is green for the wrong reason.

What this detector can and cannot see
-------------------------------------
A membership change is visible here only when the fixture holds rows on BOTH sides of
the new predicate. An exclusion keyed on a field no fixture row carries excludes nothing
and moves no hash. The rowset below is therefore built to carry, among the PENALISED
memory rows, a representative on each side of every field the penalty loop and its
immediate neighbours actually read: ``_is_episode_result``, ``timestamp``
(dated / undated / unparseable), the presence of each of ``_cosine`` / ``_rrf_score`` /
``_rsf_score``, ``id == -1`` (the profile sentinel that the bug-126 homogeneity guard
keys on), and ``_resolved`` (read by the confidence block). A predicate over some other
field is still invisible — that is a real limit, not a covered case, and extending the
rowset is the way to close it.

Deliberately out of scope: ``_apply_quality_gate``'s membership. Its decisions depend on
stored calibration state, which makes it a different surface needing its own golden.
"""

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

import cpersona.memory_handlers as memory_handlers
import cpersona.utils as utils
from cpersona._vendored_mcp_common import no_persist
from cpersona.database import get_db
from cpersona.utils import SCORING_VERSION

# The golden pair. Re-pin BOTH together (see the decision table in the test below).
GOLDEN_SCORING_VERSION = "255a3-episode-penalty-exempt"
GOLDEN_ROWSET_FINGERPRINT = "2ec8069b9cda70a68baf541a772e8594ebf97b93ccea0ba2c889b788c2d0d23b"

AGENT = "rowset-fingerprint-agent"

FIXED_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

# The in-scope boundary episode. Every row older than this is penalised, every row newer
# is not; both sides must stay populated for the penalty to be observable at all.
BOUNDARY_HOURS_AGO = 100.0

# A second, FRESHER boundary episode parked on a channel the scoped arms filter out.
# It is what makes the boundary's isolation scoping (bug-147) observable: the unscoped
# arms see it and take it as their boundary, the channel-scoped arms must not. Drop the
# channel axis from the boundary read and the scoped arms silently inherit this value —
# which is precisely the regression the scoped arms exist to catch.
OFF_CHANNEL_BOUNDARY_HOURS_AGO = 3.0
SCOPED_CHANNEL = "chat"
OFF_CHANNEL = "other"


class _FrozenDatetime(datetime):
    """datetime subclass whose now() is pinned to FIXED_NOW.

    A subclass rather than a stub so ``fromisoformat`` and arithmetic inside
    ``_parse_timestamp_utc`` keep working unchanged.
    """

    @classmethod
    def now(cls, tz=None):
        return FIXED_NOW if tz else FIXED_NOW.replace(tzinfo=None)


def _iso(hours_before: float) -> str:
    return (FIXED_NOW - timedelta(hours=hours_before)).isoformat()


def _round(value):
    """Quantise floats to 10 significant digits before hashing.

    Same rationale as the sibling grid: the last bits of a float are not the contract,
    and pinning them would turn a cross-platform libm difference into a CI failure that
    says "the scoring distribution moved".
    """
    if isinstance(value, float):
        return float(f"{value:.10g}")
    if isinstance(value, dict):
        return {k: _round(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_round(v) for v in value]
    return value


def _rowset() -> list[dict]:
    """A fresh rowset. ``_apply_recall_scoring`` mutates and re-sorts in place, so every
    arm must get its own dicts.

    Row roles (P = penalised in the baseline, i.e. a memory older than the boundary):

      1  P  memory, dated old, all three score keys, ``_resolved`` FALSE
      2  P  memory, dated old, all three score keys, ``_resolved`` TRUE
      3     memory, dated fresh (inside the boundary — the un-penalised control)
      4  P  memory, dated old, ``_rrf_score`` only
      5  P  memory, dated old, ``_rsf_score`` only
      6     memory, undated ('' timestamp — factor 1.0, the bug-237 shape)
      7     memory, unparseable timestamp
      8  P  memory, dated old, NO score keys (a cascade row: nothing for the penalty to
            scale, which is what makes it the control for "penalised set membership"
            versus "penalty had an effect")
      9     episode via ``_rid``, dated old (exempt since bug-257)
      10    episode via source dict, dated old (the other episode shape)
      11    episode via both markers, undated
      12    memory 50 h old: newer than the in-scope boundary (factor 1.0) but older than
            the off-channel one (on its decay ramp) — one of the two rows that make the
            boundary's isolation scoping observable
      13    memory 130 h old: on the in-scope boundary's decay ramp, at the floor under
            the off-channel one — the other such row
      -1    profile sentinel (carries no fusion score; the bug-126 re-sort guard keys on
            this id)

    Rows 12 and 13 are load-bearing for the scoped arms. The decay clamps at
    EPISODE_DECAY_FLOOR after ln(floor) / -rate hours (69.3 h at the shipped defaults),
    so every row further back than that scores identically under ANY boundary — a rowset
    made only of ancient rows cannot distinguish two boundaries at all.
    """
    return [
        {
            "id": 1,
            "content": "memory dated old, resolved false",
            "timestamp": _iso(2404.7),
            "_cosine": 0.83,
            "_rrf_score": 0.052,
            "_rsf_score": 0.91,
            "_resolved": False,
        },
        {
            "id": 2,
            "content": "memory dated old, resolved true",
            "timestamp": _iso(1503.2),
            "_cosine": 0.71,
            "_rrf_score": 0.047,
            "_rsf_score": 0.86,
            "_resolved": True,
        },
        {
            "id": 3,
            "content": "memory dated fresh",
            "timestamp": _iso(1.37),
            "_cosine": 0.47,
            "_rrf_score": 0.049,
            "_rsf_score": 0.62,
        },
        {
            "id": 4,
            "content": "memory dated old, rrf only",
            "timestamp": _iso(503.9),
            "_rrf_score": 0.031,
        },
        {
            "id": 5,
            "content": "memory dated old, rsf only",
            "timestamp": _iso(701.4),
            "_rsf_score": 0.33,
        },
        {
            "id": 6,
            "content": "memory undated",
            "timestamp": "",
            "_cosine": 0.19,
            "_rrf_score": 0.028,
            "_rsf_score": 0.21,
        },
        {
            "id": 7,
            "content": "memory unparseable timestamp",
            "timestamp": "not-a-timestamp",
            "_cosine": 0.24,
            "_rrf_score": 0.026,
            "_rsf_score": 0.30,
        },
        {
            "id": 8,
            "content": "memory dated old, cascade row with no scores",
            "timestamp": _iso(239.61),
        },
        {
            "id": 12,
            "content": "memory inside the in-scope boundary, behind the off-channel one",
            "timestamp": _iso(50.0),
            "_cosine": 0.66,
            "_rrf_score": 0.038,
            "_rsf_score": 0.71,
        },
        {
            "id": 13,
            "content": "memory on the decay ramp of the in-scope boundary",
            "timestamp": _iso(130.0),
            "_cosine": 0.58,
            "_rrf_score": 0.035,
            "_rsf_score": 0.64,
        },
        {
            "id": 9,
            "_rid": ("ep", 9),
            "content": "episode via rid, dated old",
            "timestamp": _iso(2404.7),
            "_cosine": 0.83,
            "_rrf_score": 0.048,
            "_rsf_score": 0.91,
        },
        {
            "id": 10,
            "source": {"System": "episode"},
            "content": "episode via source dict, dated old",
            "timestamp": _iso(503.9),
            "_cosine": 0.62,
            "_rrf_score": 0.044,
            "_rsf_score": 0.58,
        },
        {
            "id": 11,
            "_rid": ("ep", 11),
            "source": {"System": "episode"},
            "content": "episode undated",
            "timestamp": "",
            "_cosine": 0.55,
            "_rrf_score": 0.041,
            "_rsf_score": 0.49,
        },
        {
            "id": -1,
            "source": {"System": "profile"},
            "content": "profile sentinel",
            "timestamp": "",
        },
    ]


def _homogeneous_rowset() -> list[dict]:
    """A rowset on which the penalty's re-sort actually fires.

    The main rowset above is deliberately ragged — rows carrying different subsets of the
    fusion score keys — because that is what makes membership changes observable. But the
    bug-115 re-sort only runs when EVERY scored row carries the same fusion key
    (``all(r.get(score_key) is not None ...)``), so on a ragged rowset that branch is dead
    code and a mutation to it changes nothing. Measured: with only the ragged rowset, a
    mutation removing the bug-126 profile-row exclusion was NOT caught.

    So this second rowset is homogeneous on ``_rrf_score`` for every scored row, and still
    carries the profile sentinel that the bug-126 guard exists to exclude. The scores are
    chosen so the penalty REORDERS the list (h1 outranks h2 before the penalty and loses
    to it after), because a re-sort that changes no order is equally invisible.
    """
    return [
        {
            "id": 101,
            "content": "homogeneous: old, high raw score, penalised into second place",
            "timestamp": _iso(2404.7),
            "_rrf_score": 0.060,
        },
        {
            "id": 102,
            "content": "homogeneous: fresh, lower raw score, wins after the penalty",
            "timestamp": _iso(1.37),
            "_rrf_score": 0.045,
        },
        {
            "id": 103,
            "_rid": ("ep", 103),
            "content": "homogeneous: episode, exempt from the penalty",
            "timestamp": _iso(2404.7),
            "_rrf_score": 0.050,
        },
        {
            "id": 104,
            "content": "homogeneous: on the in-scope boundary's decay ramp",
            "timestamp": _iso(130.0),
            "_rrf_score": 0.040,
        },
        {
            "id": -1,
            "source": {"System": "profile"},
            "content": "profile sentinel (no fusion score — the bug-126 guard's subject)",
            "timestamp": "",
        },
    ]


@pytest_asyncio.fixture
async def seeded_db():
    """A database in a fixed state: one boundary episode plus the memory rows whose
    ``recall_count`` / ``last_recalled_at`` the confidence block reads back by id.

    The stored timestamps also set the corpus span (MIN/MAX over ``memories``) that
    scales the confidence curve, so they are part of the fingerprint's input and must
    not drift.
    """
    no_persist.resume()
    db = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await db.execute(f"DELETE FROM {table}")
    await db.execute(
        "INSERT INTO episodes (agent_id, summary, keywords, channel, created_at) "
        "VALUES (?, 's', 'k', '', ?)",
        (AGENT, _iso(BOUNDARY_HOURS_AGO)),
    )
    await db.execute(
        "INSERT INTO episodes (agent_id, summary, keywords, channel, created_at) "
        "VALUES (?, 's', 'k', ?, ?)",
        (AGENT, OFF_CHANNEL, _iso(OFF_CHANNEL_BOUNDARY_HOURS_AGO)),
    )
    # (recall_count, last_recalled_at) per memory id 1..5 — both sides of the recall-boost
    # branch in _compute_confidence are represented.
    recall_state = [
        (0, ""),
        (3, _iso(97.3)),
        (1, _iso(2.1 / 60)),
        (0, ""),
        (2, _iso(50.0)),
    ]
    for i, (count, last) in enumerate(recall_state, start=1):
        # channel is set explicitly: the scoped arms filter on (channel = ? OR ''), so
        # leaving it to the column default would make the corpus-span half of the scoped
        # arms depend on a schema default rather than on this fixture.
        await db.execute(
            "INSERT INTO memories (id, agent_id, content, timestamp, channel, recall_count, "
            "last_recalled_at) VALUES (?, ?, ?, ?, '', ?, ?)",
            (i, AGENT, f"stored row {i}", _iso(10.0 * i), count, last),
        )
    await db.commit()
    return db


async def _arm(
    db, *, confidence: bool, penalty: bool, channel: str = "", rows_factory=None
) -> list[dict]:
    """Run one config arm and return its serialisable score table."""
    memory_handlers.CONFIDENCE_ENABLED = confidence
    memory_handlers.EPISODE_PENALTY_ENABLED = penalty
    rows = (rows_factory or _rowset)()
    # query="" keeps _backfill_cosines a no-op, so the fingerprint carries no dependency
    # on an embedding backend (the suite is hermetic and must stay that way).
    results, time_range, _recall_counts, newest_age = await memory_handlers._apply_recall_scoring(
        db, AGENT, rows, deep=False, channel=channel, query=""
    )
    table = [
        {
            key: _round(row.get(key))
            # _confidence_score is the key the call site actually writes. Hashing the
            # public-response name ("confidence") would fold a column of None into the
            # digest and quietly drop the confidence axis from this detector.
            for key in ("id", "_cosine", "_rrf_score", "_rsf_score", "_confidence_score")
        }
        for row in results
    ]
    # Both derived scalars are returned to do_recall and feed the response metadata, so a
    # change in either is a distribution change even when no row score moves.
    table.append({"time_range_hours": _round(time_range), "newest_age_hours": _round(newest_age)})
    return table


async def _fingerprint(db) -> str:
    table = {}
    for confidence in (False, True):
        for penalty in (False, True):
            table[f"confidence={confidence},penalty={penalty}"] = await _arm(
                db, confidence=confidence, penalty=penalty
            )
    # Channel-scoped arms. Only meaningful with the penalty on — with it off the boundary
    # is never read, so a scoped penalty-off arm would add rows to the digest without
    # adding a decision to it.
    for confidence in (False, True):
        table[f"confidence={confidence},penalty=True,channel={SCOPED_CHANNEL}"] = await _arm(
            db, confidence=confidence, penalty=True, channel=SCOPED_CHANNEL
        )
    # Homogeneous arms. The confidence=False one is the load-bearing half: it is the only
    # configuration in which the bug-115 re-sort runs at all (with confidence on, the
    # confidence sort owns the ordering and the penalty's own re-sort is skipped).
    for confidence in (False, True):
        table[f"confidence={confidence},penalty=True,rowset=homogeneous"] = await _arm(
            db, confidence=confidence, penalty=True, rows_factory=_homogeneous_rowset
        )
    blob = json.dumps(table, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch):
    """Freeze the clock in BOTH modules that read it.

    ``utils`` and ``memory_handlers`` each bind ``datetime`` at import, so patching one
    leaves the other on the real clock and the fingerprint drifts every run.
    """
    monkeypatch.setattr(utils, "datetime", _FrozenDatetime)
    monkeypatch.setattr(memory_handlers, "datetime", _FrozenDatetime)


@pytest.fixture(autouse=True)
def _restore_scoring_flags():
    """``_arm`` assigns the module flags directly (the call site reads them as globals),
    so restore them for every other test in the suite."""
    original = (memory_handlers.CONFIDENCE_ENABLED, memory_handlers.EPISODE_PENALTY_ENABLED)
    yield
    memory_handlers.CONFIDENCE_ENABLED, memory_handlers.EPISODE_PENALTY_ENABLED = original


@pytest.mark.asyncio
async def test_rowset_scoring_fingerprint(seeded_db):
    current = await _fingerprint(seeded_db)
    if current == GOLDEN_ROWSET_FINGERPRINT:
        # The pair is pinned on the passing path too (bug-244's lesson on the sibling
        # test): a bump for a change outside this rowset leaves the hash identical, the
        # golden version string goes stale, and the NEXT genuine unbumped shift then gets
        # told it is "presumably intentional, re-pin" — the exact blessing this detector
        # exists to withhold.
        assert SCORING_VERSION == GOLDEN_SCORING_VERSION, (
            "SCORING_VERSION moved but the call-site scoring output did not, so the\n"
            "golden pair in this test is now out of step:\n"
            f"  SCORING_VERSION:        {SCORING_VERSION!r}\n"
            f"  GOLDEN_SCORING_VERSION: {GOLDEN_SCORING_VERSION!r}\n"
            "Re-pin GOLDEN_SCORING_VERSION on ANY bump, not only on one that moves this\n"
            "fingerprint — a stale pair inverts the verdict on the next unbumped shift."
        )
        return
    if SCORING_VERSION == GOLDEN_SCORING_VERSION:
        pytest.fail(
            "The scoring call site's output moved but SCORING_VERSION did not.\n"
            f"  SCORING_VERSION:     {SCORING_VERSION!r} (unchanged)\n"
            f"  golden fingerprint:  {GOLDEN_ROWSET_FINGERPRINT}\n"
            f"  current fingerprint: {current}\n"
            "This detector covers the class the pure-function grid cannot see: WHICH rows\n"
            "the penalty and the re-sort apply to. Every stored calibration is trusted or\n"
            "discarded by SCORING_VERSION alone (ensure_calibrated_on_startup /\n"
            "deep_calibration_staleness), so an unbumped shift leaves every deployed gate\n"
            "silently stale.\n"
            "If the change is intentional: bump SCORING_VERSION in cpersona/utils.py and\n"
            "re-pin GOLDEN_SCORING_VERSION + GOLDEN_ROWSET_FINGERPRINT here. If it is not,\n"
            "fix the scoring change instead of re-pinning."
        )
    pytest.fail(
        "SCORING_VERSION was bumped and the call-site output moved with it — presumably\n"
        "intentional, but the golden pair here is now stale and must be re-pinned so the\n"
        "NEXT unbumped shift is caught:\n"
        f'  GOLDEN_SCORING_VERSION = "{SCORING_VERSION}"\n'
        f'  GOLDEN_ROWSET_FINGERPRINT = "{current}"'
    )


@pytest.mark.asyncio
async def test_fingerprint_is_deterministic(seeded_db):
    """Two runs against the same database state must agree.

    Without this, a re-pin could silently record a hash that never reproduces, and the
    detector would fail for reasons unrelated to scoring on the very next run — noise
    that gets a real detector disabled.
    """
    first = await _fingerprint(seeded_db)
    second = await _fingerprint(seeded_db)
    assert first == second, (
        "The rowset fingerprint is not reproducible across runs against identical state.\n"
        f"  run 1: {first}\n  run 2: {second}\n"
        "Something in the scoring path reads unfrozen time, unordered iteration, or "
        "database state this fixture does not pin."
    )


@pytest.mark.asyncio
async def test_penalised_and_exempt_rows_both_present(seeded_db):
    """The fixture must actually exercise the membership split it claims to.

    If every row landed on one side of the boundary — or the boundary episode failed to
    insert — the fingerprint would still be stable and still be pinned, but it would be
    hashing a rowset in which membership has no observable consequence. The detector
    would look green while covering nothing. Compare the penalty-on and penalty-off arms
    directly: at least one row must differ, and at least one must not.
    """
    without = await _arm(seeded_db, confidence=False, penalty=False)
    with_penalty = await _arm(seeded_db, confidence=False, penalty=True)

    by_id_without = {row["id"]: row for row in without if "id" in row}
    by_id_with = {row["id"]: row for row in with_penalty if "id" in row}

    changed = {i for i, row in by_id_with.items() if by_id_without.get(i) != row}
    unchanged = set(by_id_without) - changed

    assert changed, (
        "No row's scores changed when the episode penalty was enabled, so this rowset "
        "cannot observe a membership change at all. Check that the boundary episode is "
        "seeded and that some scored memory row is older than it."
    )
    assert unchanged, (
        "Every row changed under the penalty, so the fixture holds no exempt control "
        "rows and an exclusion added to the penalty loop could not be distinguished "
        "from a change in the penalty's own arithmetic."
    )
    # The bug-257 exemption specifically: episode rows must be on the unchanged side.
    assert {9, 10, 11} <= unchanged, (
        "Episode rows were penalised. Either _is_episode_result no longer recognises "
        "these row shapes, or the bug-257 exemption regressed — both are membership "
        "changes this file exists to catch."
    )


@pytest.mark.asyncio
async def test_scoped_arm_sees_a_different_boundary(seeded_db):
    """The channel-scoped arms must actually resolve a different boundary.

    They exist to cover the boundary's isolation scoping, and they can only do that if
    the off-channel episode is visible to the unscoped read and invisible to the scoped
    one. If both arms landed on the same boundary the digest would still be stable and
    still be pinned — and a mutation that drops the channel axis from the boundary read
    would sail through, which is the exact regression these arms are here for.
    """
    unscoped = await _arm(seeded_db, confidence=False, penalty=True)
    scoped = await _arm(seeded_db, confidence=False, penalty=True, channel=SCOPED_CHANNEL)
    assert unscoped != scoped, (
        "The channel-scoped arm produced the same scores as the unscoped arm, so the "
        "boundary's isolation scoping is not observable in this fixture. Check that the "
        f"off-channel episode (channel={OFF_CHANNEL!r}) is seeded and that it is fresher "
        "than the in-scope one."
    )


@pytest.mark.asyncio
async def test_penalty_resort_actually_reorders(seeded_db):
    """The homogeneous arm must exercise the bug-115 re-sort, and the re-sort must move
    rows.

    Two ways this coverage can be silently absent: the rowset stops being homogeneous in
    a fusion key (the ``all(...)`` guard fails and the branch is skipped), or the scores
    stop crossing under the penalty (the branch runs but the order is unchanged). Either
    way a mutation to the re-sort — including removing the bug-126 profile-row exclusion
    — becomes invisible while the fingerprint stays green. That happened during this
    file's own development, which is why the assertion is here rather than assumed.
    """
    without = await _arm(
        seeded_db, confidence=False, penalty=False, rows_factory=_homogeneous_rowset
    )
    with_penalty = await _arm(
        seeded_db, confidence=False, penalty=True, rows_factory=_homogeneous_rowset
    )
    order_without = [row["id"] for row in without if "id" in row]
    order_with = [row["id"] for row in with_penalty if "id" in row]
    assert order_without != order_with, (
        "The episode penalty did not reorder the homogeneous rowset, so the bug-115 "
        "re-sort is not observable here.\n"
        f"  without penalty: {order_without}\n"
        f"  with penalty:    {order_with}\n"
        "Check that every scored row carries the same fusion key (the homogeneity guard "
        "the re-sort keys on) and that the penalised and un-penalised scores cross."
    )
    assert order_with.index(102) < order_with.index(101), (
        "The fresh row (102) should overtake the penalised old row (101) once the penalty "
        f"scales it; got {order_with}. The re-sort either did not run or sorted on another "
        "key."
    )
    assert order_with[0] == 103, (
        "The exempt episode (103) should lead: it keeps its raw score while every memory "
        f"above it is scaled down. Got {order_with} — if 103 slipped, the bug-257 "
        "exemption and the re-sort are no longer composing as they do in production."
    )


def test_fingerprint_input_excludes_scoring_version():
    """The fingerprint must never incorporate SCORING_VERSION itself.

    Folding the version into the hash would re-pin the golden in the same motion as the
    bump, so an unbumped shift could never be caught — a generator authoring its own
    conformance record.
    """
    source = open(__file__, encoding="utf-8").read()
    hashed_region = source[source.index("async def _arm(") : source.index("@pytest.fixture(autouse=True)")]
    assert "SCORING_VERSION" not in hashed_region, (
        "SCORING_VERSION reached the fingerprint construction path. The version string "
        "must only ever be COMPARED against the golden, never hashed into it."
    )
    assert len(GOLDEN_ROWSET_FINGERPRINT) == 64, (
        "GOLDEN_ROWSET_FINGERPRINT is not a SHA-256 hex digest — pin it by running "
        "test_rowset_scoring_fingerprint and copying the printed fingerprint."
    )
