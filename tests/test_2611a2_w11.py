"""Recall arms that disagreed with each other about the same row.

Three places where one retriever's answer about a record differed from another's:

- With FTS disabled (``CPERSONA_FTS_ENABLED=false``), the rrf and rsf fusions and
  the cue arm skipped the memory keyword channel altogether, so its LIKE fallback
  -- the path ``_search_memories_keyword`` keeps for exactly "FTS is not usable
  here", and the one cascade still calls -- never ran. With embeddings off as well,
  a recall for a word a record holds verbatim returned nothing under rrf and rsf.
- ``exclude_contents`` dropped an episode the vector and cue arms found, but not
  one the episode FTS arm (rrf, rsf, cascade) or the block reservation found, so
  an excluded episode came back or not depending on which arm reached it.
- The cue arm merged its exact part (the period's first ``MAX_MEMORIES`` rows)
  and the remainder past it without checking ids. The two are separate reads, so
  a store landing between them shifts every scan position by one and the row at
  the cap edge comes back from both: one record, two votes.

Each test shows the answer before and after: what the recall returned with the
defect is written next to each assertion.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
import pytest_asyncio

from cpersona import blocks, coarse_index, config, cue, database, far_seats, vector, vector_index
from cpersona import memory_handlers as M
from cpersona.database import get_db
from tests.conftest import FakeEmbeddingClient

FILLERS = 60  # keeps the scope above the small-pool gates, as a real store is

KUBE = "we deployed kubernetes on friday"

SUMMARY = "We decided to deploy the ledger migration to staging on Friday after review"
EPISODE = f"[Episode] {SUMMARY}"
MEMORY = "The ledger migration staging deploy needs the orchestrator lock released first"
LEDGER_QUERY = "ledger migration staging"


def _contents(result: dict) -> list[str]:
    return [m["content"] for m in result["messages"]]


@pytest_asyncio.fixture
async def clean():
    db = await get_db()
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM episodes")
    await db.commit()
    yield db
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM episodes")
    await db.commit()


async def _pool(agent: str, *contents: str) -> None:
    for i in range(FILLERS):
        assert (await M.do_store(agent, {"content": f"filler row number {i}"}))["result"] == "stored"
    for content in contents:
        assert (await M.do_store(agent, {"content": content}))["result"] == "stored"


# ── FTS disabled: the keyword channel falls back to LIKE in every fusion ──────


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["rrf", "rsf"])
async def test_fts_off_the_fusions_still_find_an_exact_substring(clean, monkeypatch, mode):
    """Disabling FTS changes how the keyword channel matches, not whether it runs.

    With the defect: [KUBE] with FTS on, [] with FTS off -- the keyword channel
    never ran, and without embeddings nothing else could vote.
    """
    agent = f"w11.fts_off.{mode}"
    await _pool(agent, KUBE)
    monkeypatch.setattr(M, "RECALL_MODE", mode)
    assert _contents(await M.do_recall(agent, "kubernetes", 5)) == [KUBE]  # FTS on: the reference

    monkeypatch.setattr(M, "FTS_ENABLED", False)
    assert _contents(await M.do_recall(agent, "kubernetes", 5)) == [KUBE]


@pytest.mark.asyncio
async def test_fts_off_the_lexical_arms_hand_the_like_rows_to_the_fusion(clean, monkeypatch):
    """The arm itself: the memory list is the LIKE fallback's, episodes stay empty
    (episodes have no LIKE fallback). With the defect both lists were empty."""
    agent = "w11.fts_off.arms"
    await _pool(agent, KUBE)
    monkeypatch.setattr(M, "FTS_ENABLED", False)
    db = await get_db()
    episodes, memories = await M._lexical_arms(db, agent, "kubernetes", 10, "", None, "", None, True)
    assert episodes == []
    assert [r["content"] for r in memories] == [KUBE]
    assert memories[0]["_bm25"] is None  # the LIKE path's rows, not FTS's


@pytest.mark.asyncio
async def test_fts_off_the_cue_arms_keyword_half_still_runs(clean, monkeypatch):
    """With no query vector the cue arm is keyword only, so with the defect it
    returned [] for a period that holds the word verbatim; with FTS on it returns
    the record, and with FTS off the LIKE fallback must return it too."""
    agent = "w11.fts_off.cue"
    await _pool(agent, KUBE)
    now = datetime.now(timezone.utc)
    window = (now - timedelta(days=1), now + timedelta(days=1))
    db = await get_db()

    async def arm():
        rows = await M._search_cue_arm(
            db, agent, "kubernetes", cue.DEPTH, window, channel="", project_id=None, source_id="",
            exclude_set=set(), query_vec=[],
        )
        return [r["content"] for r in rows]

    assert await arm() == [KUBE]  # FTS on: the reference
    monkeypatch.setattr(M, "FTS_ENABLED", False)
    assert await arm() == [KUBE]


# ── exclude_contents: an excluded episode stays out whichever arm found it ────


@pytest.mark.asyncio
@pytest.mark.parametrize("embedded", [False, True], ids=["fts_only", "vector_and_fts"])
@pytest.mark.parametrize("mode", ["rrf", "rsf"])
async def test_an_excluded_episode_stays_out_of_the_fusions(clean, monkeypatch, mode, embedded):
    """The episode FTS arm reaches the episode; with embeddings the vector arm does too.

    With the defect: excluding the memory removed it, excluding the episode by the
    very content recall had shown for it left it in the answer. With embeddings the
    vector arm skipped it and the FTS vote alone still brought it back.
    """
    agent = f"w11.exclude.{mode}.{embedded}"
    monkeypatch.setattr(M, "RECALL_MODE", mode)
    if embedded:
        monkeypatch.setattr(vector, "_embedding_client", FakeEmbeddingClient())
    # With the bag-of-words test vectors the episode clears the gate on this wording.
    query = "We decided to deploy the ledger migration" if embedded else LEDGER_QUERY
    await _pool(agent, MEMORY)
    archived = await M.do_archive_episode(agent, [], summary=SUMMARY, keywords="ledger migration")
    assert archived.get("episode_id")

    shown = _contents(await M.do_recall(agent, query, 10))
    assert EPISODE in shown and MEMORY in shown  # the probe means nothing without both

    without_memory = _contents(await M.do_recall(agent, query, 10, exclude_contents=[MEMORY]))
    assert MEMORY not in without_memory and EPISODE in without_memory

    without_episode = _contents(await M.do_recall(agent, query, 10, exclude_contents=[EPISODE]))
    assert EPISODE not in without_episode  # with the defect: still present
    assert MEMORY in without_episode  # and nothing but the excluded row went


@pytest.mark.asyncio
async def test_cascade_gives_an_excluded_episodes_place_to_the_next_stage(clean):
    """Cascade fills `limit` places stage by stage. With the defect the excluded
    episode took the one place, so the keyword stage never ran: [EPISODE]. Left
    out, the place goes to the keyword stage's row: [MEMORY]. (The quality gate
    happens to drop an FTS-only row in cascade, so this is read off the fusion.)"""
    agent = "w11.exclude.cascade"
    await _pool(agent, MEMORY)
    await M.do_archive_episode(agent, [], summary=SUMMARY, keywords="ledger migration")
    db = await get_db()

    async def fused(excluded: str | None):
        rows = await M._recall_cascade(
            db, agent, LEDGER_QUERY, 1, False, exclude_set={excluded.strip().lower()} if excluded else set()
        )
        return [r["content"] for r in rows]

    assert await fused(None) == [EPISODE]
    assert await fused(EPISODE) == [MEMORY]  # with the defect: [EPISODE]


@pytest.mark.asyncio
async def test_recall_with_context_does_not_return_an_episode_the_conversation_holds(clean):
    """recall_with_context excludes what the conversation already holds. With the
    defect the episode text appeared twice: the caller's entry and the recalled row."""
    agent = "w11.exclude.rwc"
    await _pool(agent, MEMORY)
    await M.do_archive_episode(agent, [], summary=SUMMARY, keywords="ledger migration")
    out = await M.do_recall_with_context(
        agent, LEDGER_QUERY, external_context=[{"role": "assistant", "content": EPISODE}], limit=10
    )
    assert [m.get("content") for m in out["messages"]].count(EPISODE) == 1


@pytest.mark.asyncio
async def test_the_block_reservation_excludes_an_excluded_episode(clean):
    """The episode branch of the reservation applies the exclusion its memory branch
    applies. With the defect the excluded episode still took the reserved place."""
    agent = "w11.exclude.block"
    archived = await M.do_archive_episode(agent, [], summary=SUMMARY, keywords="ledger migration")
    ep_id = archived["episode_id"]
    mem = await M.do_store(agent, {"content": MEMORY})
    hits = [
        blocks.BlockHit(kind="ep", parent_id=ep_id, block_index=0, distance=0),
        blocks.BlockHit(kind="mem", parent_id=mem["id"], block_index=0, distance=1),
    ]
    db = await get_db()

    async def reserved(excluded: str | None):
        rows = await M._block_reserved_rows(
            db, hits, agent, project_id=None, channel="", source_id="",
            exclude_set={excluded.strip().lower()} if excluded else set(), wanted=5,
        )
        return [r["_rid"] for r in rows]

    assert await reserved(None) == [("ep", ep_id), ("mem", mem["id"])]
    assert await reserved(MEMORY) == [("ep", ep_id)]
    assert await reserved(EPISODE) == [("mem", mem["id"])]  # with the defect: both


# ── the cue arm: one record, one vote, when the two parts overlap ─────────────

CAP = 3
#: Scan order is id descending (created_at rises with id): 6, 5, 4 | 3, 2, 1. Id 4 is
#: the cap edge and the weakest match, so a second vote is the only thing that can
#: move it up.
COS = {1: 0.80, 2: 0.85, 3: 0.90, 4: 0.61, 5: 0.95, 6: 0.97}
CUE_AGENT = "w11.cue"
CUE_WINDOW = (datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 2, 1, tzinfo=timezone.utc))


def _vec(c: float) -> bytes:
    return np.array([c, (1 - c * c) ** 0.5, 0, 0], dtype="<f4").tobytes()


async def _seed_cue(w, ids=range(1, 7)) -> None:
    for i in ids:
        await w.execute(
            "INSERT INTO memories (id, agent_id, content, embedding, created_at, timestamp)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (i, CUE_AGENT, f"row {i}", _vec(COS.get(i, 0.99)), f"2026-01-0{i} 00:00:00",
             f"2026-01-0{i}T00:00:00+00:00"),
        )


async def _cue_ids(r) -> list[int]:
    rows = await M._search_cue_arm(
        r, CUE_AGENT, "zzzz", 10, CUE_WINDOW, channel="", project_id=None, source_id="",
        exclude_set=set(), query_vec=[[1.0, 0.0, 0.0, 0.0]],
    )
    return [row["id"] for row in rows]


def _drop_coarse_files() -> None:
    path = coarse_index.index_path("memories")
    for p in (path, path + ".tmp"):
        if os.path.exists(p):
            os.unlink(p)


@pytest_asyncio.fixture
async def cue_store(clean, monkeypatch):
    monkeypatch.setattr(M, "MAX_MEMORIES", CAP)
    monkeypatch.setattr(vector, "_embedding_client", None)
    _drop_coarse_files()
    await vector_index.stop_logging(clean)
    await clean.commit()
    yield clean
    await vector_index.stop_logging(clean)
    await clean.commit()
    _drop_coarse_files()


@pytest.mark.asyncio
async def test_a_store_between_the_cue_arms_two_reads_does_not_count_a_row_twice(cue_store, monkeypatch):
    """The exact part and the remainder are two statements, as the near and far
    vector windows are (bug-334). A record stored between them shifts every scan
    position by one, so the remainder starts at the row the exact part ended on.

    With the defect: [4, 6, 5, 3, 2, 1] -- the weakest match took two votes and
    went first. Each record is counted once: [6, 5, 3, 2, 1, 4], the quiet answer.
    """
    monkeypatch.setattr(config, "CUE_COARSE_MODE", "on")  # the live store answers the remainder
    async with database.transaction() as w:
        await _seed_cue(w)
    async with database.connection() as r:
        quiet = await _cue_ids(r)
    assert quiet == [6, 5, 3, 2, 1, 4]

    real = far_seats.ranked_with_source

    async def a_store_lands_first(*args, **kwargs):
        async with database.transaction() as w:
            await _seed_cue(w, ids=[7])  # the newest record of the period
        return await real(*args, **kwargs)

    seen = {}
    real_drop = vector._drop_rejected_labels

    async def spy(db, rows):
        seen.setdefault("vector", [row["id"] for row in rows])
        return await real_drop(db, rows)

    monkeypatch.setattr(far_seats, "ranked_with_source", a_store_lands_first)
    monkeypatch.setattr(vector, "_drop_rejected_labels", spy)
    async with database.connection() as r:
        raced = await _cue_ids(r)
    vector_list = seen["vector"]
    assert len(vector_list) == len(set(vector_list)), vector_list  # with the defect: 4 twice
    assert raced == quiet


@pytest.mark.asyncio
async def test_a_delete_since_the_coarse_build_does_not_count_a_row_twice(cue_store, monkeypatch):
    """The reported trigger: a period row ahead of the cap deleted after the coarse
    index was built. The change log (bug-514) names the row, so the index's positions
    follow the live table; the cue arm answers as it does after a rebuild."""
    monkeypatch.setattr(config, "CUE_COARSE_MODE", "auto")  # the index only
    async with database.transaction() as w:
        await _seed_cue(w)
    await vector_index.start_logging()
    async with database.connection() as r:
        assert (await coarse_index.build_coarse_index(r))["built"]
        assert await _cue_ids(r) == [6, 5, 3, 2, 1, 4]
    async with database.transaction() as w:
        await w.execute("DELETE FROM memories WHERE id = 6")  # inside the cap
    async with database.connection() as r:
        stale = await _cue_ids(r)
        assert (await coarse_index.build_coarse_index(r))["built"]
        rebuilt = await _cue_ids(r)
    assert stale == rebuilt == [5, 3, 2, 1, 4]
