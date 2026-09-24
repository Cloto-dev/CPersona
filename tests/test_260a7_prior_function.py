"""2.6.0a7: one prior function, and confidence that neither orders nor gates.

docs/PRIOR_FUNCTION_DESIGN.md. Four behaviours, each pinned where it is decided:

- With confidence enabled and the default ``CPERSONA_CONFIDENCE_ORDERING=fusion``, recall
  returns exactly what it returns with confidence off, plus the ``confidence`` value.
  ``legacy`` restores the re-sort, and a control arm shows the corpus is one where the
  two orders differ, so the equality cannot hold vacuously.
- The gate calibration measures the signal the runtime gate compares.
- The far weight prices a far vote inside both fusions.
- The age weight reorders what the gate admitted and never changes which rows remain.
"""
from datetime import datetime, timedelta, timezone

import pytest

from cpersona import admin_handlers, config, memory_handlers
from cpersona.database import get_db

AGENT = "agent.a7-prior"
QUERY = "harbor lighthouse keeper logbook"

# Content overlapping the query to different degrees, dated across two years, so the
# fusion order (similarity) and the confidence order (similarity x time decay) differ.
CORPUS = [
    ("harbor lighthouse keeper logbook entry about the storm", "2024-09-01T00:00:00+00:00"),
    ("harbor lighthouse keeper notes", "2026-09-20T00:00:00+00:00"),
    ("lighthouse logbook", "2025-03-01T00:00:00+00:00"),
    ("keeper of the harbor", "2026-09-21T00:00:00+00:00"),
    ("logbook of the lighthouse keeper at the harbor", "2024-10-01T00:00:00+00:00"),
    ("harbor", "2026-09-22T00:00:00+00:00"),
]


async def _seed(with_episode=False):
    db = await get_db()
    for table in ("memories", "episodes", "profiles"):
        await db.execute(f"DELETE FROM {table} WHERE agent_id = ?", (AGENT,))
    await db.commit()
    for content, ts in CORPUS:
        out = await memory_handlers.do_store(
            AGENT, {"content": content, "source": {"System": "test"}, "timestamp": ts}
        )
        assert out["result"] == "stored", out
    if with_episode:
        # A boundary newer than every memory, so the opt-in penalty has rows to weigh.
        await db.execute(
            "INSERT INTO episodes (agent_id, project_id, channel, summary, created_at) "
            "VALUES (?, '', '', 'session summary', '2026-09-23 00:00:00')",
            (AGENT,),
        )
        await db.commit()


def _shape(messages, *, keep_confidence=False):
    rows = []
    for m in messages:
        m = dict(m)
        if not keep_confidence:
            m.pop("confidence", None)
        rows.append(m)
    return rows


# --- confidence ordering ---------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("penalty", [False, True])
async def test_enabled_confidence_changes_nothing_but_the_confidence_field(
    fake_embedding_client, monkeypatch, penalty
):
    """With the opt-in episode penalty on as well, the penalty's own re-sort must run for
    confidence-on exactly as for confidence-off: confidence no longer owns the order.
    The penalty arm recalls with ``deep`` (the gate halved, for every arm alike) because
    at full strength the penalty pushes most of this corpus under the gate."""
    await _seed(with_episode=penalty)
    monkeypatch.setattr(memory_handlers, "RECALL_MODE", "rrf")
    monkeypatch.setattr(memory_handlers, "EPISODE_PENALTY_ENABLED", penalty)

    async def recall():
        return (await memory_handlers.do_recall(AGENT, QUERY, limit=10, deep=penalty))["messages"]

    monkeypatch.setattr(memory_handlers, "CONFIDENCE_ENABLED", False)
    off = await recall()

    monkeypatch.setattr(memory_handlers, "CONFIDENCE_ENABLED", True)
    monkeypatch.setattr(memory_handlers, "CONFIDENCE_ORDERING", "fusion")
    on = await recall()

    assert len(off) >= 3, off
    assert _shape(on) == _shape(off), "confidence on moved, added or removed a row"
    assert all("confidence" in m for m in on), "the confidence value is no longer returned"
    assert all(m["match_reason"]["signal"] != "confidence" for m in on), on

    if penalty:
        # Non-vacuity: the penalty re-sort changed the order, so the equality above
        # shows that it ran for confidence-on as well.
        monkeypatch.setattr(memory_handlers, "CONFIDENCE_ENABLED", False)
        monkeypatch.setattr(memory_handlers, "EPISODE_PENALTY_ENABLED", False)
        unpenalised = await recall()
        assert [m["ref"] for m in unpenalised] != [m["ref"] for m in off], (
            "the penalty did not change the order here, so the equality proves nothing"
        )
    else:
        # Non-vacuity: on this corpus the old confidence order is a different answer.
        monkeypatch.setattr(memory_handlers, "CONFIDENCE_ORDERING", "legacy")
        legacy = await recall()
        assert [m.get("ref") for m in legacy] != [m.get("ref") for m in off], (
            "legacy and fusion agree here, so the equality above proves nothing"
        )
        assert all(m["match_reason"]["signal"] == "confidence" for m in legacy), legacy


# --- calibration -----------------------------------------------------------------


@pytest.mark.parametrize(
    "enabled, ordering, mode, expected",
    [
        (True, "fusion", "rrf", "rrf"),
        (True, "fusion", "rsf", "rsf"),
        (True, "legacy", "rrf", "confidence"),
        (True, "legacy", "cascade", "confidence"),
        (False, "legacy", "rsf", "rsf"),
        (False, "fusion", "cascade", None),
        (True, "fusion", "cascade", None),
    ],
)
def test_calibration_measures_the_signal_the_runtime_gate_compares(
    monkeypatch, enabled, ordering, mode, expected
):
    monkeypatch.setattr(config, "CONFIDENCE_ENABLED", enabled)
    monkeypatch.setattr(config, "CONFIDENCE_ORDERING", ordering)
    monkeypatch.setattr(config, "RECALL_MODE", mode)
    assert admin_handlers._calibration_signal() == expected


# --- far weight ------------------------------------------------------------------


def _row(i, cosine):
    return {"id": i, "content": f"row {i}", "source": "{}", "timestamp": "2026-09-01T00:00:00+00:00",
            "_cosine": cosine, "_rid": ("mem", i)}


def _fake_search(near, far):
    async def search(db, agent_id, query, depth, **kwargs):
        kwargs["far_out"].extend(far)
        return near
    return search


@pytest.mark.asyncio
@pytest.mark.parametrize("weight", [1.0, 0.5, 0.0])
async def test_rrf_prices_only_the_far_vote(fake_embedding_client, monkeypatch, weight):
    monkeypatch.setattr(memory_handlers, "FTS_ENABLED", False)
    monkeypatch.setattr(memory_handlers, "PRIOR_FAR_WEIGHT", weight)
    monkeypatch.setattr(memory_handlers, "_search_vector", _fake_search([_row(1, 0.9), _row(2, 0.8)], [_row(3, 0.7)]))
    out = await memory_handlers._recall_rrf(await get_db(), AGENT, "q", 10, False)
    score = {r["id"]: r["_rrf_score"] for r in out if r["id"] > 0}
    k = memory_handlers.RRF_K
    assert score[1] == 1.0 / (k + 1) and score[2] == 1.0 / (k + 2), "a near vote moved"
    assert score.get(3, 0.0) == pytest.approx(weight / (k + 1))


@pytest.mark.asyncio
@pytest.mark.parametrize("weight", [1.0, 0.5])
async def test_rsf_weights_only_the_far_channel(fake_embedding_client, monkeypatch, weight):
    monkeypatch.setattr(memory_handlers, "FTS_ENABLED", False)
    monkeypatch.setattr(memory_handlers, "PRIOR_FAR_WEIGHT", weight)
    near = [_row(1, 0.9), _row(2, 0.5)]
    far = [_row(3, 0.7), _row(4, 0.6)]
    monkeypatch.setattr(memory_handlers, "_search_vector", _fake_search(near, far))
    out = await memory_handlers._recall_rsf(await get_db(), AGENT, "q", 10, False)
    score = {r["id"]: r["_rsf_score"] for r in out if r["id"] > 0}
    # Two active channels: near normalises to 1.0 / 0.0, far to 1.0 / 0.0, divisor 2.
    assert score[1] == pytest.approx(0.5) and score[2] == pytest.approx(0.0)
    assert score[3] == pytest.approx(weight / 2) and score[4] == pytest.approx(0.0)


# --- the age weight --------------------------------------------------------------

NEWEST = datetime(2026, 9, 22, tzinfo=timezone.utc)
OLDEST = NEWEST - timedelta(hours=1000)
SPAN = (OLDEST, NEWEST)


def _scored(i, score, ts):
    return {"id": i, "_rrf_score": score, "timestamp": ts}


def _iso(hours_before_newest):
    return (NEWEST - timedelta(hours=hours_before_newest)).isoformat()


@pytest.fixture
def age_on(monkeypatch):
    monkeypatch.setattr(memory_handlers, "PRIOR_AGE_RATE", 0.01)
    monkeypatch.setattr(memory_handlers, "PRIOR_AGE_FLOOR", 0.3)
    monkeypatch.setattr(memory_handlers, "PRIOR_AGE_ANCHOR", "newest")
    monkeypatch.setattr(memory_handlers, "CONFIDENCE_ENABLED", False)


def test_a_zero_rate_returns_the_list_untouched(monkeypatch):
    monkeypatch.setattr(memory_handlers, "PRIOR_AGE_RATE", 0.0)
    rows = [_scored(1, 0.01, _iso(900)), _scored(2, 0.02, _iso(0))]
    out = memory_handlers._apply_prior(rows, SPAN, NEWEST)
    assert out is rows and [r["id"] for r in out] == [1, 2] and "_prior" not in rows[0]


def test_the_weight_orders_by_score_times_age_weight(age_on):
    # Row 1 scores higher but is 900 h older: p = max(0.3, 1/(1+9)) = 0.3.
    rows = [_scored(1, 0.031, _iso(900)), _scored(2, 0.030, _iso(0))]
    out = memory_handlers._apply_prior(rows, SPAN, NEWEST)
    assert [r["id"] for r in out] == [2, 1]
    assert out[0]["_prior"] == 1.0 and out[1]["_prior"] == pytest.approx(0.3)


def test_a_moderate_age_uses_the_curve_not_the_floor(age_on):
    rows = [_scored(1, 0.02, _iso(50))]
    memory_handlers._apply_prior(rows, SPAN, NEWEST)
    assert rows[0]["_prior"] == pytest.approx(1 / (1 + 50 * 0.01))


def test_an_undated_row_is_placed_at_the_middle_of_the_scope(age_on):
    rows = [_scored(1, 0.02, ""), _scored(2, 0.02, "not a timestamp")]
    memory_handlers._apply_prior(rows, SPAN, NEWEST)
    mid = max(0.3, 1 / (1 + 500 * 0.01))
    assert rows[0]["_prior"] == pytest.approx(mid) and rows[1]["_prior"] == pytest.approx(mid)


def test_a_row_newer_than_the_anchor_counts_as_age_zero(age_on):
    rows = [_scored(1, 0.02, (NEWEST + timedelta(hours=30)).isoformat())]
    memory_handlers._apply_prior(rows, SPAN, NEWEST)
    assert rows[0]["_prior"] == 1.0


def test_measured_from_the_newest_record_idle_time_changes_nothing(age_on):
    def weights(now):
        rows = [_scored(1, 0.03, _iso(10)), _scored(2, 0.02, _iso(400))]
        memory_handlers._apply_prior(rows, SPAN, now)
        return {r["id"]: r["_prior"] for r in rows}
    assert weights(NEWEST) == weights(NEWEST + timedelta(days=90))


def test_measured_from_now_idle_time_ages_everything(age_on, monkeypatch):
    monkeypatch.setattr(memory_handlers, "PRIOR_AGE_ANCHOR", "now")
    def weight(now):
        rows = [_scored(1, 0.03, _iso(10))]
        memory_handlers._apply_prior(rows, SPAN, now)
        return rows[0]["_prior"]
    assert weight(NEWEST + timedelta(days=90)) < weight(NEWEST)


def test_the_profile_row_sinks(age_on):
    rows = [{"id": -1, "content": "[Profile]"}, _scored(1, 0.02, _iso(0))]
    out = memory_handlers._apply_prior(rows, SPAN, NEWEST)
    assert [r["id"] for r in out] == [1, -1]


def test_a_list_without_a_uniform_fusion_score_keeps_its_order(age_on):
    rows = [{"id": 1, "timestamp": _iso(900)}, {"id": 2, "_rrf_score": 0.03, "timestamp": _iso(0)}]
    out = memory_handlers._apply_prior(rows, SPAN, NEWEST)
    assert out is rows and [r["id"] for r in out] == [1, 2]


def test_legacy_confidence_ordering_is_left_alone(age_on, monkeypatch):
    monkeypatch.setattr(memory_handlers, "CONFIDENCE_ENABLED", True)
    monkeypatch.setattr(memory_handlers, "CONFIDENCE_ORDERING", "legacy")
    rows = [_scored(1, 0.031, _iso(900)), _scored(2, 0.030, _iso(0))]
    assert [r["id"] for r in memory_handlers._apply_prior(rows, SPAN, NEWEST)] == [1, 2]


@pytest.mark.asyncio
async def test_the_weight_reorders_the_admitted_rows_and_admits_nothing(
    fake_embedding_client, monkeypatch
):
    """At the call site: an extreme age weight changes the order recall returns but not
    which rows it returns. The order change is the control that shows the weight ran."""
    await _seed()
    monkeypatch.setattr(memory_handlers, "RECALL_MODE", "rrf")
    monkeypatch.setattr(memory_handlers, "CONFIDENCE_ENABLED", False)

    monkeypatch.setattr(memory_handlers, "PRIOR_AGE_RATE", 0.0)
    plain = (await memory_handlers.do_recall(AGENT, QUERY, limit=50))["messages"]
    monkeypatch.setattr(memory_handlers, "PRIOR_AGE_RATE", 1.0)
    monkeypatch.setattr(memory_handlers, "PRIOR_AGE_FLOOR", 0.0)
    weighted = (await memory_handlers.do_recall(AGENT, QUERY, limit=50))["messages"]

    assert sorted(m["ref"] for m in weighted) == sorted(m["ref"] for m in plain)
    assert [m["ref"] for m in weighted] != [m["ref"] for m in plain], "the weight did not run"
    assert all("prior" in m["match_reason"] for m in weighted)
    assert all("prior" not in m["match_reason"] for m in plain)
    # recall returns rows least relevant first; the last row is the best one, and
    # under a steep age weight the best row is the newest.
    newest = max(CORPUS, key=lambda c: c[1])[1]
    assert weighted[-1]["timestamp"] == newest


def test_the_weight_never_rewrites_the_score_the_gate_reads(age_on):
    rows = [_scored(1, 0.031, _iso(900)), _scored(2, 0.030, _iso(0))]
    memory_handlers._apply_prior(rows, SPAN, NEWEST)
    assert {r["id"]: r["_rrf_score"] for r in rows} == {1: 0.031, 2: 0.030}


@pytest.mark.asyncio
@pytest.mark.parametrize("ordering, runs", [("fusion", False), ("legacy", True)])
async def test_the_cosine_backfill_runs_only_where_confidence_gates(monkeypatch, ordering, runs):
    """The backfill gives the confidence gate a real cosine (bug-155). With confidence on
    but gating nothing, running it would still move the cosine branch of the gate."""
    calls = []

    async def spy(*args, **kwargs):
        calls.append(args)

    monkeypatch.setattr(memory_handlers, "_backfill_cosines", spy)
    monkeypatch.setattr(memory_handlers, "CONFIDENCE_ENABLED", True)
    monkeypatch.setattr(memory_handlers, "CONFIDENCE_ORDERING", ordering)
    rows = [{"id": 1, "content": "x", "timestamp": "2026-09-01T00:00:00+00:00", "_rrf_score": 0.03}]
    await memory_handlers._apply_recall_scoring(await get_db(), AGENT, rows, False, query="x")
    assert bool(calls) is runs
