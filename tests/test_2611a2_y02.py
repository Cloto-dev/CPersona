"""Recall's retrieval arms at the edges their existing tests never reached.

- The block arm's episode rule: a source-scoped recall sees episodes only when a
  channel scopes them too. Only the suppression half (no channel) was tested; the
  half that keeps a channel's episodes reachable was not.
- The cue arm's "period not searched whole" disclosure, at the boundary where the
  period holds exactly ``MAX_MEMORIES`` embedded records and one more.
- The cue arm's own depth (``cue.DEPTH``): the merged capped-plus-remainder vector
  list and the final fused list are each cut to it.
- ``exclude_contents`` is matched case-insensitively at the ``do_recall`` seam.
"""

from __future__ import annotations

import os

import numpy as np
import pytest
import pytest_asyncio

from cpersona import coarse_index, config, cue, vector
from cpersona import memory_handlers as M
from cpersona.database import get_db
from tests.test_blocks_retrieval import AGENT as REACH_AGENT
from tests.test_blocks_retrieval import LONG_RECORD, TAIL_SUBJECT, _TempDB
from tests.test_blocks_retrieval import building, lexical_off, reading  # noqa: F401 (fixtures)
from tests.test_cue_coarse import ONE_HOT, OUTSIDE, PERIOD, TOTAL, OneHotClient, _seed
from tests.test_cue_coarse import AGENT as CUE_AGENT
from tests.test_cue_coarse import QUERY as CUE_QUERY
from tests.test_cue_coarse_auto import REMAINDER, TIME_CUE

# ── the block arm: a channel lets a source-scoped recall see episodes ─────────


@pytest.mark.asyncio
@pytest.mark.usefixtures("reading", "lexical_off")
async def test_a_source_scoped_recall_with_a_channel_reserves_that_channels_episode():
    """Episodes carry no per-user source tag, so a source filter alone hides them
    (pinned in test_blocks_retrieval). A channel scopes them instead: the grounding
    path recalls with both a source and a channel, and the channel's episodes are
    its own to see. Hiding them there would drop every episode the block arm
    reached for that caller, silently."""
    async with _TempDB() as tmp:
        archived = await M.do_archive_episode(REACH_AGENT, [], summary=LONG_RECORD, channel="room-7")
        await tmp.drain()

        scoped = await M.do_recall(
            REACH_AGENT, TAIL_SUBJECT, limit=3, source_id="discord:12345", channel="room-7"
        )
        episode = [m for m in scoped["messages"] if m.get("ref") == f"ep:{archived['episode_id']}"]
        assert episode, "the channel's episode was hidden from a source-scoped recall"
        assert episode[0]["match_reason"]["signal"] == "block"

        # The suppression half still holds beside it: without the channel, nothing.
        unscoped = await M.do_recall(REACH_AGENT, TAIL_SUBJECT, limit=3, source_id="discord:12345")
        assert not [m for m in unscoped["messages"] if str(m.get("ref", "")).startswith("ep:")]


# ── the cue arm's remainder disclosure at exactly MAX_MEMORIES ─────────────────


def _clean_coarse_index():
    path = coarse_index.index_path("memories")
    for p in (path, path + ".tmp"):
        if os.path.exists(p):
            os.unlink(p)


@pytest_asyncio.fixture
async def period_store(monkeypatch):
    """The store of tests/test_cue_coarse.py, with no coarse index (auto skips)."""
    monkeypatch.setattr(vector, "_embedding_client", OneHotClient())
    monkeypatch.setattr(vector, "VECTOR_REACH", 0)
    monkeypatch.setattr(M, "FTS_ENABLED", False)
    monkeypatch.setattr(config, "FAR_SEATS_ENABLED", False)
    monkeypatch.setattr(config, "CUE_COARSE_MODE", "auto")
    db = await get_db()
    _clean_coarse_index()
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM episodes")
    await db.commit()
    await _seed(db)
    yield db
    await db.execute("DELETE FROM memories")
    await db.commit()
    _clean_coarse_index()


#: Every seeded record is embedded; the OUTSIDE ones lie outside the cue's period.
IN_PERIOD = TOTAL - len(OUTSIDE)


async def _cued(monkeypatch, cap: int) -> dict:
    monkeypatch.setattr(vector, "MAX_MEMORIES", cap)
    monkeypatch.setattr(M, "MAX_MEMORIES", cap)
    return await M.do_recall(CUE_AGENT, CUE_QUERY, 5, time_cue=TIME_CUE)


@pytest.mark.asyncio
async def test_a_period_of_exactly_the_cap_was_searched_whole(period_store, monkeypatch):
    """The capped scan read every record the period holds, so nothing was left out
    and the response must not say otherwise."""
    rows = await period_store.execute_fetchall(
        "SELECT COUNT(*) FROM memories WHERE agent_id = ? AND embedding IS NOT NULL AND timestamp = ?",
        (CUE_AGENT, "2026-03-15T00:00:00+00:00"),
    )
    assert rows[0][0] == IN_PERIOD  # the boundary below is only one if this holds
    result = await _cued(monkeypatch, IN_PERIOD)
    assert "remainder" not in result["time_cue"]


@pytest.mark.asyncio
async def test_one_record_past_the_cap_is_disclosed(period_store, monkeypatch):
    """One embedded record past the cap was not read, and with no index to search
    it the response says the period was not searched whole."""
    result = await _cued(monkeypatch, IN_PERIOD - 1)
    assert result["time_cue"].get("remainder") == REMAINDER


# ── the cue arm's depth ───────────────────────────────────────────────────────

DEPTH_AGENT = "y02.cue.depth"
DIM = 32
#: More capped-scan records than the cue arm's depth, so the vector half fills it.
CAPPED = cue.DEPTH + 10
LATE = "zebra crossing note"  # the one record the keyword half finds
DEPTH_QUERY = "zebra"


def _vec(score: float) -> bytes:
    v = np.zeros(DIM, dtype=np.float32)
    v[0] = np.float32(score)
    v[1] = np.float32((1.0 - score * score) ** 0.5)
    return v.tobytes()


@pytest_asyncio.fixture
async def deep_period(monkeypatch):
    """`CAPPED` recent records, each a better vector match than the next, and one
    older record (LATE) past the cap: a weaker vector match than any of them, and the
    only record the keyword half finds.

    The vector half's capped part keeps the best `cue.DEPTH` of the capped scan, the
    remainder (mode "on", read live) adds LATE, and on cosine LATE sorts after all of
    them: position DEPTH in the merged list, the first one past the cut.
    """
    monkeypatch.setattr(vector, "_embedding_client", OneHotClient())
    monkeypatch.setattr(vector, "VECTOR_REACH", 0)
    monkeypatch.setattr(vector, "MAX_MEMORIES", CAPPED)
    monkeypatch.setattr(M, "MAX_MEMORIES", CAPPED)
    monkeypatch.setattr(M, "FTS_ENABLED", False)  # the keyword half runs through LIKE
    monkeypatch.setattr(config, "FAR_SEATS_ENABLED", False)
    monkeypatch.setattr(config, "CUE_COARSE_MODE", "on")
    db = await get_db()
    _clean_coarse_index()
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM episodes")
    await db.commit()
    ids = {}
    stamp = "2026-03-15T00:00:00+00:00"
    for n in range(CAPPED):
        # created_at descending in n: n is the record's position in the capped scan.
        cur = await db.execute(
            "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp, created_at, embedding)"
            " VALUES (?, '', '', ?, '{}', ?, ?, ?)",
            (DEPTH_AGENT, f"vector row {n}", stamp, f"2026-03-01 01:{(CAPPED - n) // 60:02d}:{(CAPPED - n) % 60:02d}",
             _vec(0.90 - n * 0.002)),
        )
        ids[n] = cur.lastrowid
    cur = await db.execute(
        "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp, created_at, embedding)"
        " VALUES (?, '', '', ?, '{}', ?, '2026-03-01 00:00:00', ?)",
        (DEPTH_AGENT, LATE, stamp, _vec(0.50)),
    )
    ids["late"] = cur.lastrowid
    await db.commit()
    yield ids
    await db.execute("DELETE FROM memories")
    await db.commit()
    _clean_coarse_index()


async def _depth_arm(db):
    return await M._search_cue_arm(
        db, DEPTH_AGENT, DEPTH_QUERY, cue.DEPTH, PERIOD, channel="", project_id=None, source_id="",
        exclude_set=set(), query_vec=[ONE_HOT.tolist()],
    )


@pytest.mark.asyncio
async def test_the_cue_arm_returns_no_more_than_its_depth(deep_period):
    """DEPTH vector rows and one keyword row are DEPTH + 1 candidates; the arm
    answers with DEPTH of them, whatever the period holds."""
    rows = await _depth_arm(await get_db())
    assert len(rows) == cue.DEPTH
    assert deep_period["late"] in [r["id"] for r in rows]  # the keyword half was heard


@pytest.mark.asyncio
async def test_a_vector_rank_past_the_depth_casts_no_vote(deep_period):
    """LATE's only vote is its keyword rank 0, the same as the best vector row's, so
    it sits right behind that row (first seen wins a tie). A vector rank past the cut
    adding a second vote would lift it to the top."""
    rows = await _depth_arm(await get_db())
    assert [r["id"] for r in rows[:2]] == [deep_period[0], deep_period["late"]]
    assert [r["id"] for r in rows[2:]] == [deep_period[n] for n in range(1, cue.DEPTH - 1)]


# ── exclude_contents is matched without regard to case ────────────────────────

CASE_AGENT = "y02.exclude.case"
MIXED = "Apples and Pears in the Orchard Grove were picked on Tuesday"
CASE_QUERY = "apples pears orchard grove"


@pytest_asyncio.fixture
async def case_store(monkeypatch):
    monkeypatch.setattr(M, "RECALL_MODE", "rsf")
    db = await get_db()
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM episodes")
    await db.commit()
    for i in range(60):  # above the small-pool gates, as a real store is
        assert (await M.do_store(CASE_AGENT, {"content": f"filler row number {i}"}))["result"] == "stored"
    assert (await M.do_store(CASE_AGENT, {"content": MIXED}))["result"] == "stored"
    yield db
    await db.execute("DELETE FROM memories")
    await db.commit()


def _contents(result: dict) -> list[str]:
    return [m["content"] for m in result["messages"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", [MIXED, MIXED.upper(), MIXED.swapcase()], ids=["as_stored", "upper", "swapped"])
async def test_a_mixed_case_exclude_entry_excludes_the_row_it_names(case_store, entry):
    """The recall tool forwards exclude_contents as given, and the row side is
    lower-cased where it is compared; the entries are lower-cased once, here, or a
    caller quoting a stored turn verbatim gets that turn back."""
    assert MIXED in _contents(await M.do_recall(CASE_AGENT, CASE_QUERY, 10))  # reachable at all
    assert MIXED not in _contents(await M.do_recall(CASE_AGENT, CASE_QUERY, 10, exclude_contents=[entry]))
