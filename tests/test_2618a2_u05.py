"""Recall scoring: a cue that ends before the scope's oldest record, and a NaN vector row.

bug-817: a before-only cue whose `before` was not later than the scope's oldest
record was given a one-second period at that record, after the cue's own end. The
cue arm then searched and lifted a record the cue excludes, and a store whose
records are all recent judged the cue `recent_only` on that substituted period.

bug-836: the live chunked scan filtered with `sim < floor`, which a NaN similarity
never satisfies, so a row holding a non-finite vector passed every floor and could
push a better row out of the bounded top-k. The index supplier keeps only
`sim >= floor` and drops the same row.
"""
import math
from datetime import datetime, timedelta, timezone

import aiosqlite
import numpy as np
import pytest

from cpersona import cue, memory_handlers, vector
from cpersona.database import get_db

AGENT = "agent.u05-recall-scoring"
NOW = datetime.now(timezone.utc)
QUERY = "harbor lighthouse keeper logbook"


def _ts(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


def _refs(out):
    return [m["ref"] for m in out["messages"] if "ref" in m]


async def _seed(rows):
    db = await get_db()
    for table in ("memories", "episodes", "profiles"):
        await db.execute(f"DELETE FROM {table} WHERE agent_id = ?", (AGENT,))
    await db.commit()
    for content, days_ago in rows:
        await memory_handlers.do_store(
            AGENT, {"content": content, "source": {"System": "test"}, "timestamp": _ts(days_ago)}
        )


# --- bug-817 --------------------------------------------------------------------------


@pytest.mark.parametrize("confidence", ["sure", "likely", "vague"])
def test_bug_817_a_cue_ending_before_the_oldest_record_never_reaches_that_record(confidence):
    now = datetime(2026, 11, 24, tzinfo=timezone.utc)
    oldest = datetime(2025, 5, 1, 10, tzinfo=timezone.utc)
    span = (oldest, datetime(2025, 7, 2, 10, tzinfo=timezone.utc))
    tc = cue.parse({"before": "2024-01-01", "confidence": confidence})
    window = cue.period(tc, confidence, now, span)
    assert window is not None
    # The period starts before the cue's own end, and even widened by the
    # confidence margin it stays far from the 2025 record the cue excludes.
    assert window[0] < tc.before, window
    assert window[1] < oldest, window
    if confidence == "sure":
        assert window[1] <= tc.before, window
    # A before equal to the oldest record excludes that record too.
    at_oldest = cue.parse({"before": oldest.isoformat(), "confidence": "sure"})
    w = cue.period(at_oldest, "sure", now, span)
    assert w[0] < oldest and w[1] <= oldest, w
    # An ordinary open start is still closed by the oldest record.
    later = cue.parse({"before": "2025-06-01", "confidence": "sure"})
    assert cue.period(later, "sure", now, span)[0] == oldest


def test_bug_817_an_old_before_cue_on_a_recent_store_is_not_recent_only():
    now = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)
    span = (now - timedelta(hours=3), now - timedelta(minutes=5))
    tc = cue.parse({"before": "2024-01-01", "confidence": "sure"})
    # The cue's period ends in 2024: it does not start within the last day.
    assert not cue.recent_only(tc, now, span)
    # A cue that really points only at today still is.
    today = cue.parse({"after": (now - timedelta(hours=2)).isoformat(), "confidence": "sure"})
    assert cue.recent_only(today, now, span)


@pytest.mark.asyncio
async def test_bug_817_the_recall_neither_lifts_a_record_after_before_nor_reports_its_period(
    fake_embedding_client, monkeypatch
):
    # Records 1 to 111 days old; the cue asks for what came before 200 days ago.
    await _seed([(f"{QUERY} entry {i}", 10 * i + 1) for i in range(12)])
    monkeypatch.setattr(memory_handlers, "RECALL_MODE", "rrf")
    time_cue = {"before": _ts(200)[:10], "confidence": "sure"}
    before = cue.parse(time_cue).before
    plain = await memory_handlers.do_recall(AGENT, QUERY, limit=5)
    out = await memory_handlers.do_recall(AGENT, QUERY, limit=5, trace=True, time_cue=time_cue)
    tc = out["time_cue"]
    assert "ignored" not in tc, tc
    # The empty period is widened once, by its own (second-long) margin; it stays
    # on the cue's end, 89 days before the oldest record.
    start, end = (datetime.fromisoformat(t) for t in tc["period"])
    assert start < before and end <= before + timedelta(seconds=1), tc
    assert tc["moved"] == 0 and tc["seated"] == 0, tc
    assert _refs(out) == _refs(plain)


@pytest.mark.asyncio
async def test_bug_817_a_recent_store_does_not_drop_an_old_cue_as_recent_only(
    fake_embedding_client, monkeypatch
):
    await _seed([(f"{QUERY} note {i}", i / 24) for i in range(3)])
    monkeypatch.setattr(memory_handlers, "RECALL_MODE", "rrf")
    out = await memory_handlers.do_recall(
        AGENT, QUERY, limit=5, time_cue={"before": "2024-01-01", "confidence": "sure"}
    )
    assert out["time_cue"].get("ignored") != "recent_only", out["time_cue"]
    end = datetime.fromisoformat(out["time_cue"]["period"][1])
    assert end <= datetime(2024, 1, 2, 0, 0, 1, tzinfo=timezone.utc), out["time_cue"]


# --- bug-836 --------------------------------------------------------------------------


def _blob(v):
    return np.asarray(v, dtype=np.float32).tobytes()


@pytest.mark.asyncio
async def test_bug_836_a_nan_row_neither_passes_the_floor_nor_evicts_a_better_row():
    rows = [(1, _blob([1, 0, 0, 0])), (2, _blob([math.nan, 0, 0, 0])),
            (3, _blob([0, 1, 0, 0])), (4, _blob([1, 0, 0, 0]))]
    q = np.asarray([1, 0, 0, 0], dtype=np.float32)
    async with aiosqlite.connect(":memory:") as db:
        await db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, embedding BLOB)")
        await db.executemany("INSERT INTO t VALUES (?, ?)", rows)
        await db.commit()
        sql = "SELECT id, embedding FROM t ORDER BY id LIMIT ?"
        top2 = await vector._chunked_cosine_scan(db, sql, (100,), q, 4, 0.3, 2)
        unbounded = await vector._chunked_cosine_scan(db, sql, (100,), q, 4, 0.3, None)
        # A floor of 0 still keeps the orthogonal row and still drops the NaN one.
        at_zero = await vector._chunked_cosine_scan(db, sql, (100,), q, 4, 0.0, None)
    # What the index supplier keeps for the same scores.
    sims = vector._cosine_batch(q, 4, [b for _, b in rows])
    index_kept = [rows[i][0] for i, s in enumerate(sims) if s >= 0.3]
    assert index_kept == [1, 4]
    assert [rid for _, rid, _ in unbounded] == index_kept, unbounded
    assert [rid for _, rid, _ in top2] == [1, 4], top2
    assert all(s == pytest.approx(1.0) for _, _, s in top2), top2
    assert [rid for _, rid, _ in at_zero] == [1, 3, 4], at_zero


@pytest.mark.asyncio
async def test_bug_836_the_live_scan_answers_as_the_recorded_index_path_does():
    """The recall over the non-finite corpus, through the live scan, returns exactly
    the messages the contiguous-index path recorded for the same corpus."""
    import json
    from pathlib import Path

    from behaviour_252 import SCENARIOS, observe

    golden = json.loads((Path(__file__).parent / "golden" / "behaviour_252.json").read_text(encoding="utf-8"))
    scenario = {s.id: s for s in SCENARIOS}["corpus-nonfinite-recall-scan-shipping-default"]
    observed = await observe(scenario)
    recorded_index = golden["corpus-nonfinite-recall-index-shipping-default"]["result"]["recalled"]
    assert observed["result"]["messages"] == recorded_index["messages"]
