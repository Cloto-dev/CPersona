"""A recall's budget ledger (cpersona/budget.py).

The limits are the bounds recall already held, so what is pinned here is that a
traced recall reports them and what it spent, that the cue loop stops where the
ledger says, that an iteration budget above one is reported as unused rather
than as spent, and that nothing a caller receives depends on the budget.
"""
from datetime import datetime, timedelta, timezone

import pytest

from cpersona import budget, config, memory_handlers
from cpersona.database import get_db

AGENT = "agent.budget"
NOW = datetime.now(timezone.utc)
QUERY = "harbor lighthouse keeper logbook"
CORPUS = [(f"harbor lighthouse keeper logbook entry {i}", 10 * i + 1) for i in range(12)]


def _ts(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


def _period(after_days: float, before_days: float, confidence: str = "sure") -> dict:
    return {"after": _ts(after_days)[:10], "before": _ts(before_days)[:10], "confidence": confidence}


async def _seed(rows=CORPUS, agent=AGENT):
    db = await get_db()
    for table in ("memories", "episodes", "profiles"):
        await db.execute(f"DELETE FROM {table} WHERE agent_id = ?", (agent,))
    await db.commit()
    for content, days_ago in rows:
        await memory_handlers.do_store(
            agent, {"content": content, "source": {"System": "test"}, "timestamp": _ts(days_ago)}
        )


def _refs(out):
    return [m.get("ref") for m in out["messages"]]


# --- the ledger ---------------------------------------------------------------------------


def test_a_recall_declares_the_bounds_it_already_held():
    ledger = budget.Ledger.for_recall()
    assert dict(ledger.limits) == {
        "ordinary_fetch": 1, "block_fetch": 1, "cue_stage": 2, "iteration": 1,
    }


def test_spending_up_to_the_limit_is_counted_and_past_it_is_refused():
    ledger = budget.Ledger.for_recall()
    assert ledger.allows(budget.CUE_STAGE)
    ledger.spend(budget.CUE_STAGE)
    assert ledger.allows(budget.CUE_STAGE)
    ledger.spend(budget.CUE_STAGE)
    assert not ledger.allows(budget.CUE_STAGE)
    with pytest.raises(budget.BudgetExceeded, match="cue_stage: the limit is 2"):
        ledger.spend(budget.CUE_STAGE)
    assert ledger.used[budget.CUE_STAGE] == 2, "a refused spend is not recorded"


def test_kinds_are_counted_apart():
    ledger = budget.Ledger.for_recall()
    ledger.spend(budget.ORDINARY_FETCH)
    assert ledger.allows(budget.BLOCK_FETCH) and ledger.allows(budget.CUE_STAGE)
    assert not ledger.allows(budget.ORDINARY_FETCH)


@pytest.mark.parametrize("requested, evaluated, stop", [
    (1, 1, "budget_exhausted"),
    (4, 1, "no_new_hypothesis"),
    (1, 0, "no_new_hypothesis"),
])
def test_the_iteration_report_says_what_was_asked_what_was_done_and_why_it_stopped(requested, evaluated, stop):
    ledger = budget.Ledger.for_recall(requested)
    for _ in range(evaluated):
        ledger.spend(budget.ITERATION)
    assert ledger.report()["iterations"] == {"requested": requested, "evaluated": evaluated, "stop": stop}


@pytest.mark.parametrize("bad", [0, -3])
def test_an_iteration_budget_below_one_is_refused(bad):
    with pytest.raises(ValueError, match="at least 1"):
        budget.Ledger.for_recall(bad)


# --- what a recall spends -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_plain_recall_spends_one_fetch_and_one_iteration(fake_embedding_client):
    await _seed()
    out = await memory_handlers.do_recall(AGENT, QUERY, limit=5, trace=True)
    assert out["trace"]["budget"] == {
        "limits": {"ordinary_fetch": 1, "block_fetch": 1, "cue_stage": 2, "iteration": 1},
        "used": {"ordinary_fetch": 1, "block_fetch": 0, "cue_stage": 0, "iteration": 1},
        "iterations": {"requested": 1, "evaluated": 1, "stop": "budget_exhausted"},
    }


@pytest.mark.asyncio
async def test_the_block_arm_spends_its_own_fetch(fake_embedding_client, monkeypatch):
    await _seed()
    monkeypatch.setattr(config, "BLOCK_RETRIEVAL_ENABLED", True)
    out = await memory_handlers.do_recall(AGENT, QUERY, limit=5, trace=True)
    assert out["trace"]["budget"]["used"]["block_fetch"] == 1
    assert out["trace"]["budget"]["used"]["ordinary_fetch"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("period, stages", [
    # Days 45 to 75 hold records: the first stage finds them and the loop stops.
    ((75, 46), 1),
    # Days 3 to 8 hold nothing: the loop widens once and the ledger stops it there.
    ((8, 3), 2),
])
async def test_the_cue_loop_spends_one_stage_per_stage_it_runs(fake_embedding_client, period, stages):
    await _seed()
    out = await memory_handlers.do_recall(AGENT, QUERY, limit=5, trace=True, time_cue=_period(*period))
    assert len(out["trace"]["stages"]) == stages
    assert out["trace"]["budget"]["used"]["cue_stage"] == stages


@pytest.mark.asyncio
async def test_a_larger_iteration_budget_is_reported_unused_and_changes_nothing(fake_embedding_client):
    await _seed()
    plain = await memory_handlers.do_recall(AGENT, QUERY, limit=5, trace=True, time_cue=_period(75, 46))
    asked = await memory_handlers.do_recall(
        AGENT, QUERY, limit=5, trace=True, time_cue=_period(75, 46), iteration_budget=256
    )
    assert _refs(asked) == _refs(plain)
    assert asked["time_cue"] == plain["time_cue"]
    assert asked["trace"]["budget"]["iterations"] == {"requested": 256, "evaluated": 1, "stop": "no_new_hypothesis"}


@pytest.mark.asyncio
async def test_an_untraced_recall_carries_no_budget(fake_embedding_client):
    await _seed()
    out = await memory_handlers.do_recall(AGENT, QUERY, limit=5, iteration_budget=4)
    assert "budget" not in out and "trace" not in out


@pytest.mark.asyncio
async def test_a_recall_asked_for_no_iteration_is_refused(fake_embedding_client):
    out = await memory_handlers.do_recall(AGENT, QUERY, limit=5, iteration_budget=0)
    assert "at least 1" in out["error"] and out["messages"] == []


@pytest.mark.asyncio
async def test_a_cue_whose_widened_period_is_still_empty_stops_at_the_ledger(fake_embedding_client):
    """A vague width is one step further, and the ledger's two stages do not reach it."""
    await _seed()
    out = await memory_handlers.do_recall(
        AGENT, QUERY, limit=5, trace=True,
        time_cue={"after": "2020-01-01", "before": "2020-01-10", "confidence": "sure"},
    )
    stages = out["trace"]["stages"]
    assert [s["confidence"] for s in stages] == ["sure", "likely"]
    assert stages[1]["found"] == 0 and stages[1]["next"] is None
    assert out["trace"]["budget"]["used"]["cue_stage"] == 2
    assert out["time_cue"]["revised"] is True
