"""Far seats: records past the scan window, held beside the answer and displacing nothing.

Design: `docs/BINARY_COARSE_SEARCH_DESIGN.md` §5, with invariants 1, 2, 7 and 8 of
§8. Every embedding here is one-hot on the first axis, as in
`tests/test_vector_reach.py`, so a record's cosine to the query is a value this file
chose and is exact: which records the seats must take is known without running the
code under test. The store has `NEAR` records inside the window and the rest past
it, most of them below the vector arm's floor and a few far above everything near.
"""

import numpy as np
import pytest
import pytest_asyncio

from cpersona import coarse_search, config, far_seats, vector
from cpersona import memory_handlers as M
from cpersona.database import get_db
from tests.conftest import FakeEmbeddingClient

AGENT = "far.seats.agent"
DIM = 32
NEAR = 20
TOTAL = 60
QUERY = "a question nothing lexical answers"

ONE_HOT = np.zeros(DIM, dtype=np.float32)
ONE_HOT[0] = 1.0

#: Scan position -> cosine, past the window. Everything else past it is 0.05,
#: below the floor (0.3 x 0.5 under rrf).
HIGH = {30: 0.90, 45: 0.95, 50: 0.92}  # cosine order 45, 50, 30: not the scan order
BELOW_FLOOR = 0.05


class OneHotClient(FakeEmbeddingClient):
    async def embed(self, texts):
        return [ONE_HOT.tolist() for _ in texts]


def _cosine(n: int) -> float:
    if n < NEAR:
        return 0.30 + (n % 5) / 100
    return HIGH.get(n, BELOW_FLOOR)


def _vec(n: int) -> bytes:
    """Unit length, so the dot product with the query is `_cosine(n)`.

    The rest of the length sits on axis 1 for every record except the high far ones,
    which each get an axis of their own: they are then far from the window's records
    and from each other, so `reconstruct` does not fold them into the window's items.
    """
    score = _cosine(n)
    v = np.zeros(DIM, dtype=np.float32)
    v[0] = np.float32(score)
    v[2 + n % (DIM - 2) if n in HIGH else 1] = np.float32((1.0 - score * score) ** 0.5)
    return v.tobytes()


def _stamp(n: int) -> str:
    """created_at descending in n, so n is the scan position."""
    left = TOTAL - n
    return f"2026-03-01 00:{left // 60:02d}:{left % 60:02d}"


#: Text no other record shares, so `reconstruct` does not bundle a high far record with
#: the window's records (they would all read "m row ...").
WORDS = {30: "zebra quartz", 45: "violet anchor", 50: "copper meadow"}


def _content(n: int) -> str:
    return WORDS.get(n, f"m row {n}")


async def _seed(db, *, content=_content, project=lambda n: "", agent=lambda n: AGENT):
    ids = {}
    for n in range(TOTAL):
        cur = await db.execute(
            "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp, created_at, embedding)"
            " VALUES (?, ?, '', ?, ?, '2026-03-01T00:00:00+00:00', ?, ?)",
            # A source of its own for each high far record: `reconstruct` bundles one
            # source's records close in time into one item (cluster:adjacent).
            (agent(n), project(n), content(n), '{"type": "User", "id": "%s"}' % (f"far-{n}" if n in HIGH else "u"),
             _stamp(n), _vec(n)),
        )
        ids[n] = cur.lastrowid
    await db.commit()
    return ids


@pytest_asyncio.fixture
async def db(monkeypatch):
    monkeypatch.setattr(vector, "_embedding_client", OneHotClient())
    monkeypatch.setattr(vector, "MAX_MEMORIES", NEAR)
    monkeypatch.setattr(vector, "VECTOR_REACH", 0)
    conn = await get_db()
    await conn.execute("DELETE FROM memories")
    await conn.commit()
    yield conn
    await conn.execute("DELETE FROM memories")
    await conn.commit()


def _far(messages):
    return [m for m in messages if m.get("match_reason", {}).get("signal") == "far"]


def _others(messages):
    return [m for m in messages if m.get("match_reason", {}).get("signal") != "far"]


async def _recall(**kw):
    return await M.do_recall(AGENT, kw.pop("query", QUERY), kw.pop("limit", 5), **kw)


@pytest.mark.asyncio
async def test_off_runs_nothing(db, monkeypatch):
    """Invariant 8: at the default the far scan is a guard, not a scan that finds nothing."""
    await _seed(db)
    assert config.FAR_SEATS_ENABLED is False

    async def detonate(*a, **kw):
        raise AssertionError("the far scan ran with far seats off")

    monkeypatch.setattr(coarse_search, "coarse_candidates", detonate)
    monkeypatch.setattr(far_seats, "ranked", detonate)
    out = await _recall(trace=True)
    assert not _far(out["messages"])
    assert "far_fetch" not in out["trace"]["budget"]["limits"]


@pytest.mark.asyncio
async def test_the_seats_hold_the_best_far_records_and_displace_nothing(db, monkeypatch):
    ids = await _seed(db)
    off = await _recall()
    monkeypatch.setattr(config, "FAR_SEATS_ENABLED", True)
    on = await _recall(trace=True)

    # Invariant 2: every row the recall returned without the seats, in the same order.
    assert _others(on["messages"]) == off["messages"]
    seated = _far(on["messages"])[::-1]  # the response lists the answer in reverse (results.reverse())
    assert [m["ref"] for m in seated] == [f"mem:{ids[45]}", f"mem:{ids[50]}"]  # the best two by cosine
    for m, n in zip(seated, (45, 50)):
        assert m["match_reason"]["admission"] == "reservation"
        assert m["match_reason"]["cosine"] == pytest.approx(HIGH[n], abs=1e-6)
    # Invariant 7: at most limit + the held places of every kind, the far seats included.
    assert len(on["messages"]) <= 5 + len(seated) + 2
    assert on["trace"]["budget"]["used"]["far_fetch"] == 1
    assert [r["kind"] for r in on["trace"]["reservation"]].count("far") == 2


@pytest.mark.asyncio
async def test_a_far_record_below_the_vector_floor_is_not_seated(db, monkeypatch):
    """Decision F: a far record is held to the floor the near records of the recall met."""
    ids = await _seed(db)
    monkeypatch.setattr(config, "FAR_SEATS_ENABLED", True)
    # Raise the floor above two of the three high far records.
    monkeypatch.setattr(vector, "_get_vector_threshold", lambda agent_id: 0.93 / M.RRF_THRESHOLD_FACTOR)
    out = await _recall()
    assert [m["ref"] for m in _far(out["messages"])] == [f"mem:{ids[45]}"]


@pytest.mark.asyncio
async def test_a_record_an_ordinary_arm_reached_and_the_gate_refused_is_not_seated(db, monkeypatch):
    """As with the cue's seats: a refused row does not come back this way."""
    ids = await _seed(db, content=lambda n: "beacon lantern" if n == 45 else _content(n))
    monkeypatch.setattr(config, "FAR_SEATS_ENABLED", True)
    monkeypatch.setattr(M, "AUTOCUT_ENABLED", True)
    real = M._autocut

    def refuse_row_45(rows):
        return [r for r in real(rows) if r.get("id") != ids[45]]

    monkeypatch.setattr(M, "_autocut", refuse_row_45)
    traced = await _recall(query="beacon lantern", trace=True)
    reached = {row["ref"] for name, arm in traced["trace"]["arms"].items() if name != "far" for row in arm}
    assert f"mem:{ids[45]}" in reached  # the lexical arm did reach it
    seated = [m["ref"] for m in _far(traced["messages"])][::-1]
    assert f"mem:{ids[45]}" not in seated
    assert seated == [f"mem:{ids[50]}", f"mem:{ids[30]}"]


@pytest.mark.asyncio
async def test_the_hydrate_drops_a_candidate_the_authority_does_not_admit(db, monkeypatch):
    """Invariant 1: the index is not an authority. A looser supplier cannot widen what is seated."""
    ids = await _seed(db, agent=lambda n: "far.seats.someone.else" if n == 45 else AGENT)
    monkeypatch.setattr(config, "FAR_SEATS_ENABLED", True)
    real = coarse_search.coarse_candidates

    async def looser(*a, **kw):
        found = await real(*a, **kw)
        # Offer the other agent's record first, as an index whose axis codes went stale could.
        return coarse_search.Candidates(
            (ids[45], *found.ids), (0, *found.distances), (kw["start"], *found.positions), found.source,
        )

    monkeypatch.setattr(coarse_search, "coarse_candidates", looser)
    out = await _recall()
    seated = [m["ref"] for m in _far(out["messages"])][::-1]
    assert f"mem:{ids[45]}" not in seated
    assert seated == [f"mem:{ids[50]}", f"mem:{ids[30]}"]


@pytest.mark.asyncio
async def test_the_far_scan_begins_past_every_vector_list(db, monkeypatch):
    """It looks from max(MAX_MEMORIES, VECTOR_REACH): the far vote list's rows are not seated twice."""
    await _seed(db)
    monkeypatch.setattr(config, "FAR_SEATS_ENABLED", True)
    monkeypatch.setattr(vector, "VECTOR_REACH", 40)
    starts = []
    real = coarse_search.coarse_candidates

    async def recording(*a, **kw):
        starts.append(kw["start"])
        return await real(*a, **kw)

    monkeypatch.setattr(coarse_search, "coarse_candidates", recording)
    await _recall()
    assert starts == [40]


@pytest.mark.asyncio
async def test_a_seated_record_is_not_credited(db, monkeypatch):
    """bug-453: no gate admitted it, so its recall count does not move."""
    ids = await _seed(db)
    monkeypatch.setattr(config, "FAR_SEATS_ENABLED", True)
    out = await _recall()
    assert _far(out["messages"])
    rows = await db.execute_fetchall("SELECT recall_count FROM memories WHERE id IN (?, ?)", (ids[45], ids[50]))
    assert [r[0] for r in rows] == [0, 0]


@pytest.mark.asyncio
async def test_reconstruct_holds_a_far_seat_as_an_item_after_its_window(db, monkeypatch):
    ids = await _seed(db)
    monkeypatch.setattr(config, "FAR_SEATS_ENABLED", True)
    from cpersona import reconstruct

    out = await reconstruct.do_reconstruct(AGENT, QUERY, count=3)
    held = [item for item in out["items"] if item.get("admission") == "reservation"]
    assert {item["head_ref"] for item in held} == {f"mem:{ids[45]}", f"mem:{ids[50]}"}
    assert out["items"][-len(held):] == held  # after the window


@pytest.mark.asyncio
async def test_no_query_vector_no_far_scan(db, monkeypatch):
    """A recall with no local query vector (here, no embedding client) has nothing to quantise."""
    await _seed(db)
    monkeypatch.setattr(config, "FAR_SEATS_ENABLED", True)
    monkeypatch.setattr(vector, "_embedding_client", None)

    async def detonate(*a, **kw):
        raise AssertionError("the far scan ran without a query vector")

    monkeypatch.setattr(far_seats, "ranked", detonate)
    out = await _recall(query="m row")
    assert not _far(out["messages"])

