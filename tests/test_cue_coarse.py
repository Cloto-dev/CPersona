"""The cue arm's remainder: a time cue's period past the vector half's cap, searched coarsely.

Design: `docs/BINARY_COARSE_SEARCH_DESIGN.md` §6. The vector half of the cue arm ranks
at most `CPERSONA_MAX_MEMORIES` of the period's records, the most recently stored. With
`CPERSONA_CUE_COARSE_ENABLED=true` (mode "on"), the rest of the period goes through the
coarse supplier and is merged on cosine. No index is built here, so "on" reads the live
store; the default mode (auto), which asks the index only, is pinned in
`tests/test_cue_coarse_auto.py`.

Every embedding is one-hot on the first axis plus a filler axis, as in
`tests/test_far_seats.py`, so a record's cosine to the query is a value this file chose
and is exact. `n` is a record's scan position (`created_at` descending in `n`). The
record at `OUTSIDE` lies outside the period, so the period's own positions skip it.
"""

from datetime import datetime, timezone

import numpy as np
import pytest
import pytest_asyncio

from cpersona import coarse_search, config, cue, far_seats, vector
from cpersona import memory_handlers as M
from cpersona.coarse_search import Candidates
from cpersona.database import get_db
from tests.conftest import FakeEmbeddingClient

AGENT = "cue.coarse.agent"
DIM = 32
CAP = 20
TOTAL = 60
QUERY = "a question nothing lexical answers"

ONE_HOT = np.zeros(DIM, dtype=np.float32)
ONE_HOT[0] = 1.0

PERIOD = (datetime(2026, 3, 10, tzinfo=timezone.utc), datetime(2026, 3, 20, tzinfo=timezone.utc))
INSIDE_STAMP = "2026-03-15T00:00:00+00:00"
OUTSIDE_STAMP = "2026-02-01T00:00:00+00:00"

#: Outside the period: one inside the cap's scan range (the period's positions skip it),
#: and one far above everything that must never be reached through the period.
OUTSIDE = {7: 0.30, 45: 0.99}
#: Past the cap and inside the period.
HIGH = {40: 0.95, 50: 0.60}
BELOW_FLOOR_PAST_CAP = 35  # 0.10, below the floor (0.3 x 0.5)
TIE_PAST_CAP = 55  # 0.32, equal to the capped records with n % 5 == 2


class OneHotClient(FakeEmbeddingClient):
    async def embed(self, texts):
        return [ONE_HOT.tolist() for _ in texts]


def _cosine(n: int) -> float:
    if n in OUTSIDE:
        return OUTSIDE[n]
    if n in HIGH:
        return HIGH[n]
    if n == BELOW_FLOOR_PAST_CAP:
        return 0.10
    if n == TIE_PAST_CAP:
        return 0.32
    if n <= CAP:  # with n = 7 outside, positions 0..20 hold the period's first CAP records
        return 0.30 + (n % 5) / 100
    return 0.05


def _vec(n: int) -> bytes:
    score = _cosine(n)
    v = np.zeros(DIM, dtype=np.float32)
    v[0] = np.float32(score)
    v[1] = np.float32((1.0 - score * score) ** 0.5)
    return v.tobytes()


def _stamp(n: int) -> str:
    left = TOTAL - n
    return f"2026-03-01 00:{left // 60:02d}:{left % 60:02d}"


async def _seed(db):
    ids = {}
    for n in range(TOTAL):
        cur = await db.execute(
            "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp, created_at, embedding)"
            " VALUES (?, '', '', ?, '{}', ?, ?, ?)",
            (AGENT, f"m row {n}", OUTSIDE_STAMP if n in OUTSIDE else INSIDE_STAMP, _stamp(n), _vec(n)),
        )
        ids[n] = cur.lastrowid
    await db.commit()
    return ids


@pytest_asyncio.fixture
async def db(monkeypatch):
    monkeypatch.setattr(vector, "_embedding_client", OneHotClient())
    monkeypatch.setattr(vector, "MAX_MEMORIES", CAP)
    monkeypatch.setattr(vector, "VECTOR_REACH", 0)
    monkeypatch.setattr(M, "MAX_MEMORIES", CAP)
    monkeypatch.setattr(M, "FTS_ENABLED", False)  # the vector half alone reaches the cue list
    conn = await get_db()
    await conn.execute("DELETE FROM memories")
    await conn.execute("DELETE FROM episodes")
    await conn.commit()
    yield conn
    await conn.execute("DELETE FROM memories")
    await conn.commit()


async def _cue_arm(db, monkeypatch, *, enabled: bool):
    monkeypatch.setattr(config, "CUE_COARSE_MODE", "on" if enabled else "off")
    return await M._search_cue_arm(
        db, AGENT, QUERY, cue.DEPTH, PERIOD, channel="", project_id=None, source_id="",
        exclude_set=set(), query_vec=[ONE_HOT.tolist()],
    )


def _ids(rows):
    return [r["id"] for r in rows]


@pytest.mark.asyncio
async def test_off_the_remainder_is_not_searched(db, monkeypatch):
    """Off, the remainder is a guard, not a search that finds nothing."""
    ids = await _seed(db)
    monkeypatch.setattr(config, "CUE_COARSE_MODE", "off")

    async def detonate(*a, **kw):
        raise AssertionError("the remainder was searched with the setting off")

    monkeypatch.setattr(coarse_search, "coarse_candidates", detonate)
    monkeypatch.setattr(far_seats, "ranked", detonate)
    rows = await M._search_cue_arm(
        db, AGENT, QUERY, cue.DEPTH, PERIOD, channel="", project_id=None, source_id="",
        exclude_set=set(), query_vec=[ONE_HOT.tolist()],
    )
    assert ids[40] not in _ids(rows) and ids[50] not in _ids(rows)


@pytest.mark.asyncio
async def test_a_period_within_the_cap_gives_the_same_answer_on_and_off(db, monkeypatch):
    """§6: a period holding no more than the cap gives exactly today's answer."""
    await _seed(db)
    monkeypatch.setattr(M, "MAX_MEMORIES", TOTAL)  # the whole period fits under the cap
    off = await _cue_arm(db, monkeypatch, enabled=False)
    on = await _cue_arm(db, monkeypatch, enabled=True)
    assert [(r["id"], r["_cosine"]) for r in on] == [(r["id"], r["_cosine"]) for r in off]


@pytest.mark.asyncio
async def test_a_close_record_past_the_cap_is_reached_and_ranked_by_cosine(db, monkeypatch):
    ids = await _seed(db)
    off = await _cue_arm(db, monkeypatch, enabled=False)
    on = await _cue_arm(db, monkeypatch, enabled=True)
    assert ids[40] not in _ids(off) and ids[50] not in _ids(off)
    # 0.95 and 0.60 lead, above every capped record (0.30-0.34).
    assert _ids(on)[:2] == [ids[40], ids[50]]
    assert [r["_cosine"] for r in on] == sorted((r["_cosine"] for r in on), reverse=True)
    # The capped list is still there, whole and in its order.
    capped = [i for i in _ids(on) if i in set(_ids(off))]
    assert capped == _ids(off)
    assert len(set(_ids(on))) == len(on)


@pytest.mark.asyncio
async def test_the_remainder_stays_inside_the_period(db, monkeypatch):
    ids = await _seed(db)
    on = await _cue_arm(db, monkeypatch, enabled=True)
    assert ids[45] not in _ids(on)  # 0.99, past the cap, outside the period
    assert ids[7] not in _ids(on)


@pytest.mark.asyncio
async def test_the_remainder_is_held_to_the_floor(db, monkeypatch):
    ids = await _seed(db)
    on = await _cue_arm(db, monkeypatch, enabled=True)
    assert ids[BELOW_FLOOR_PAST_CAP] not in _ids(on)
    assert all(r["_cosine"] >= vector._get_vector_threshold(AGENT) * M.RRF_THRESHOLD_FACTOR for r in on)


@pytest.mark.asyncio
async def test_the_remainder_starts_at_the_cap_of_the_same_period(db, monkeypatch):
    """Positions are counted among the period's records, so the cap's rows are not searched twice."""
    await _seed(db)
    asked = []
    real = coarse_search.coarse_candidates

    async def recording(*a, **kw):
        asked.append((kw["start"], kw["period"]))
        return await real(*a, **kw)

    monkeypatch.setattr(coarse_search, "coarse_candidates", recording)
    await _cue_arm(db, monkeypatch, enabled=True)
    assert asked == [(CAP, (cue.sql_instant(PERIOD[0]), cue.sql_instant(PERIOD[1])))]


@pytest.mark.asyncio
async def test_a_tie_goes_to_the_capped_list(db, monkeypatch):
    ids = await _seed(db)
    on = await _cue_arm(db, monkeypatch, enabled=True)
    order = _ids(on)
    tied = [ids[n] for n in (2, 12, 17)]  # the capped records at 0.32
    assert all(order.index(t) < order.index(ids[TIE_PAST_CAP]) for t in tied)


@pytest.mark.asyncio
async def test_the_period_is_reapplied_when_the_candidates_are_read(db, monkeypatch):
    """A candidate the supplier offers from outside the period (a stale index) is dropped."""
    ids = await _seed(db)
    real = coarse_search.coarse_candidates

    async def stale(*a, **kw):
        found = await real(*a, **kw)
        return Candidates(
            ids=(ids[45], *found.ids), distances=(0, *found.distances),
            positions=(TOTAL, *found.positions), source=found.source,
        )

    monkeypatch.setattr(coarse_search, "coarse_candidates", stale)
    on = await _cue_arm(db, monkeypatch, enabled=True)
    assert ids[45] not in _ids(on)
    assert ids[40] in _ids(on)


@pytest.mark.asyncio
async def test_a_cued_recall_seats_the_record_the_remainder_reached(db, monkeypatch):
    """Through the whole recall: nothing about the cue's seats changes, only what its vector half saw."""
    ids = await _seed(db)
    time_cue = {"after": "2026-03-10", "before": "2026-03-19", "confidence": "sure"}
    monkeypatch.setattr(config, "CUE_COARSE_MODE", "off")
    off = await M.do_recall(AGENT, QUERY, 5, time_cue=time_cue)
    monkeypatch.setattr(config, "CUE_COARSE_MODE", "on")
    on = await M.do_recall(AGENT, QUERY, 5, time_cue=time_cue)
    off_refs = [m["ref"] for m in off["messages"]]
    on_refs = [m["ref"] for m in on["messages"]]
    assert f"mem:{ids[40]}" not in off_refs
    assert f"mem:{ids[40]}" in on_refs
