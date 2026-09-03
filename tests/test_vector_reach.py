"""`CPERSONA_VECTOR_REACH`: a second vector list, and nothing at all when it is off.

The scan window has been doing two jobs — how far back the vector arm looks, and
a recency prior nobody named, since keeping only the newest N rows hands every
recent memory a candidate field of N instead of the whole corpus. Widening the
window removes the prior in the act of extending the reach, which is what the
measurement behind `docs/SCAN_WINDOW_REACH_DESIGN.md` recorded as +4.93 NDCG@10
on far answers against −20.19 on near ones. The reach setting separates them: the
near list stays exactly what it is, and the rows at scan positions
`[MAX_MEMORIES, VECTOR_REACH)` become a SECOND ranked list handed to the fusion.

Two claims are pinned here, and they are pinned differently:

- **Off is nothing.** Not "returns an empty list" — no scan, no statement, no
  call. A guard that ran a scan which found nothing would cost a statement, a
  matrix and a merge on every recall the server answers, so the first test
  detonates the phase-1 suppliers and asserts the default never reaches them.
- **On changes only what it adds.** The near list, its scores and its order are
  the ones the same corpus produces with the reach off, and under `rrf` every
  near row keeps the exact fused score it had. The far rows can only be added.

Every row's embedding here is one-hot, so a similarity is a single product plus
sixty-three additions of zero: exact in any summation order, at any chunk size,
on any platform. That is what lets the far list be compared to an independently
computed answer with `==` on ids and order rather than "close enough" — the ties
this corpus deliberately contains are then ties, not coincidences of rounding
(the same instrument, and the same reason, as `tests/test_chunked_exact_scan.py`).
"""

import os

import numpy as np
import pytest
import pytest_asyncio

from cpersona import memory_handlers as M
from cpersona import vector, vector_index
from cpersona.database import get_db
from cpersona.isolation import isolation_where
from tests.conftest import FakeEmbeddingClient

AGENT = "reach.agent"
DIM = 64

# The window this file works at. 100 and 250 rather than the shipped 10,000 and
# off: the claims are about the geometry of the two regions, and a corpus that
# had to exceed the real default would make every test in this file a benchmark.
NEAR = 100
REACH = 250
TOTAL = 300

# A query that reads one component, so `_vec(s) @ ONE_HOT` is exactly `s`.
ONE_HOT = np.zeros(DIM, dtype=np.float32)
ONE_HOT[0] = 1.0


class OneHotEmbeddingClient(FakeEmbeddingClient):
    """Embeds every query as `ONE_HOT`, whatever the text says.

    The suite's bag-of-words double is deterministic but its scores are
    arbitrary numbers; here the corpus is built so that a row's similarity is a
    value this file chose, which is what makes an independent oracle possible.
    """

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [ONE_HOT.tolist() for _ in texts]


@pytest.fixture
def one_hot_client(monkeypatch):
    client = OneHotEmbeddingClient()
    monkeypatch.setattr(vector, "_embedding_client", client)
    return client


def _vec(score: float) -> bytes:
    v = np.zeros(DIM, dtype=np.float32)
    v[0] = np.float32(score)
    return v.tobytes()


def _score(n: int) -> float:
    """The similarity of the row at scan position `n`. Exact in float32.

    Three regions, each with a job:

    - `[0, NEAR)` — the near window. Low and closely spaced, so a far row that
      is admitted cannot be mistaken for one of these.
    - `[NEAR, REACH)` — the far region. Spread over a wider band, with ties (the
      multiplier wraps the 128 values it can take before the region ends), so
      the top-`limit` cut has to break them the way the scan does.
    - `[REACH, TOTAL)` — past the reach, and the BEST rows in the corpus. If a
      change ever read further than it was told to, these rows would win
      everything, which is the failure this region exists to make loud.
    """
    if n < NEAR:
        return 0.25 + (n % 8) / 1024
    if n < REACH:
        return 0.375 + ((n * 37) % 128) / 1024
    return 0.90


def _drop_indexes():
    for table in ("memories", "episodes"):
        for path in (vector_index.index_path(table), vector_index.index_path(table) + ".tmp"):
            if os.path.exists(path):
                os.unlink(path)


def _stamp(n: int, total: int) -> str:
    """A canonical `created_at` that descends as `n` rises, so `n` IS the scan position."""
    remaining = total - n
    return f"2026-03-01 {remaining // 3600:02d}:{(remaining // 60) % 60:02d}:{remaining % 60:02d}"


async def _seed_memories(db, scores, *, tag="m", offset=0, total=None):
    total = total if total is not None else len(scores) + offset
    for n, score in enumerate(scores, start=offset):
        await db.execute(
            "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp,"
            " created_at, embedding) VALUES (?, '', '', ?, ?, ?, ?, ?)",
            (
                AGENT,
                f"{tag} row {n}",
                '{"type": "User", "id": "user-a"}',
                "2026-03-01T00:00:00+00:00",
                _stamp(n, total),
                _vec(score),
            ),
        )
    await db.commit()


async def _seed_episodes(db, scores, *, total=None):
    total = total if total is not None else len(scores)
    for n, score in enumerate(scores):
        await db.execute(
            "INSERT INTO episodes (agent_id, project_id, channel, summary, created_at,"
            " embedding) VALUES (?, '', '', ?, ?, ?)",
            (AGENT, f"episode {n}", _stamp(n, total), _vec(score)),
        )
    await db.commit()


@pytest_asyncio.fixture
async def db():
    conn = await get_db()
    _drop_indexes()
    await conn.execute("DELETE FROM memories")
    await conn.execute("DELETE FROM episodes")
    await conn.commit()
    yield conn
    await conn.execute("DELETE FROM memories")
    await conn.execute("DELETE FROM episodes")
    await conn.commit()
    _drop_indexes()


@pytest_asyncio.fixture
async def corpus(db):
    """`TOTAL` memory rows whose scan position is their index. No episodes."""
    await _seed_memories(db, [_score(n) for n in range(TOTAL)], total=TOTAL)
    return db


def set_reach(monkeypatch, near: int, reach: int) -> None:
    """Both windows at once — they are only ever meaningful as a pair."""
    monkeypatch.setattr(vector, "MAX_MEMORIES", near)
    monkeypatch.setattr(vector, "VECTOR_REACH", reach)


async def _search(db, *, limit=10, min_sim=-1.0, want_far=True):
    """`(near, far)` out of the real entry point, in one call and one embedding."""
    far: list[dict] = []
    near = await vector._search_vector(
        db, AGENT, "any text at all", limit, min_similarity=min_sim,
        far_out=far if want_far else None,
    )
    return near, far


async def _oracle(db, *, start, stop, limit, min_sim, table="memories"):
    """The top-`limit` rows of scan positions `[start, stop)`, computed here.

    Its own statement, its own window, its own matmul: an oracle that reused the
    scan's helpers would agree with the scan by construction. Ties break by scan
    position, which is what the heap's `(score, -ordinal)` key and the index
    branch's `(score, -i)` key both spell.
    """
    rows = await db.execute_fetchall(
        f"SELECT id, embedding FROM {table} WHERE agent_id = ? AND embedding IS NOT NULL"
        " ORDER BY created_at DESC, id ASC",
        (AGENT,),
    )
    window = rows[start:stop]
    if not window:
        return []
    mat = np.frombuffer(b"".join(r[1] for r in window), dtype=np.float32).reshape(len(window), DIM)
    sims = mat @ ONE_HOT
    ranked = [
        (float(sim), pos, row[0])
        for pos, (row, sim) in enumerate(zip(window, sims))
        if sim >= min_sim
    ]
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [(row_id, score) for score, _, row_id in ranked[:limit]]


def _ids(rows):
    return [r["_rid"] for r in rows]


def _ids_and_scores(rows):
    return [(r["_rid"], r["_cosine"]) for r in rows]


# --------------------------------------------------------------------------
# (1) the default is a guard, not a scan that returns nothing
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("reach", [0, NEAR])
async def test_an_inactive_reach_never_reaches_the_database(
    corpus, one_hot_client, monkeypatch, reach
):
    """Off must cost nothing: no supplier, no statement, no matrix.

    The suppliers are replaced with detonators rather than counters — a count
    asserted to be zero and a call that never happens look the same in the
    passing case, but only one of them names the culprit when it does.

    Both spellings of off are here. `0` is the documented default; `REACH ==
    MAX_MEMORIES` names a region of zero rows, and a guard written as `>=`
    instead of `>` would run a scan of width zero for it — an empty answer that
    is indistinguishable from this one in the result and costs a statement, a
    matrix and a merge on every recall.
    """
    set_reach(monkeypatch, NEAR, reach)

    def detonate(*args, **kwargs):
        raise AssertionError(
            "the far path read the corpus with CPERSONA_VECTOR_REACH at its default; "
            "the design requires a guard, not a scan that returns nothing"
        )

    monkeypatch.setattr(vector, "_index_phase1", detonate)
    monkeypatch.setattr(vector, "_chunked_cosine_scan", detonate)

    assert not vector.far_list_enabled()
    far = await vector._search_vector_far(
        corpus,
        iso=isolation_where(agent_id=AGENT, project_id=None, channel=""),
        src_clause="",
        src_params=(),
        src_like="",
        limit=10,
        query_vec=ONE_HOT,
        query_dim=DIM,
        effective_min_sim=-1.0,
        agent_id=AGENT,
        project_id=None,
        channel="",
        source_id="",
    )
    assert far == []


@pytest.mark.asyncio
async def test_the_fusion_does_not_call_the_far_scan_at_the_default(
    corpus, one_hot_client, monkeypatch
):
    """And the counter is proven live by turning the setting on in the same test.

    A test that only asserted "zero calls with the reach off" would also pass
    against a patch that never took effect, which is the false green this whole
    file would then be built on.
    """
    calls: list[int] = []
    real = vector._search_vector_far

    async def counting(*args, **kwargs):
        calls.append(1)
        return await real(*args, **kwargs)

    monkeypatch.setattr(vector, "_search_vector_far", counting)

    set_reach(monkeypatch, NEAR, 0)
    await M._recall_rrf(corpus, AGENT, "row", 10, False)
    await M._recall_rsf(corpus, AGENT, "row", 10, False)
    assert calls == [], f"the fusion asked for a far list with the reach off: {len(calls)} calls"

    set_reach(monkeypatch, NEAR, REACH)
    await M._recall_rrf(corpus, AGENT, "row", 10, False)
    await M._recall_rsf(corpus, AGENT, "row", 10, False)
    assert len(calls) == 2, (
        f"the far scan was not reached with the setting on ({len(calls)} calls), so the "
        "assertion above proves nothing about the setting"
    )


# --------------------------------------------------------------------------
# (2) a reach that does not exceed the window is the same as off
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_reach_equal_to_the_window_is_the_same_as_off(
    corpus, one_hot_client, monkeypatch
):
    """`REACH == MAX_MEMORIES` names an empty region; recall must not notice it."""
    set_reach(monkeypatch, NEAR, 0)
    off = await M._recall_rrf(corpus, AGENT, "row", 10, False)
    off_scores = [(r.get("_rid", r["id"]), r.get("_rrf_score")) for r in off]

    set_reach(monkeypatch, NEAR, NEAR)
    assert not vector.far_list_enabled()
    equal = await M._recall_rrf(corpus, AGENT, "row", 10, False)
    assert [(r.get("_rid", r["id"]), r.get("_rrf_score")) for r in equal] == off_scores

    _, far = await _search(corpus)
    assert far == []


# --------------------------------------------------------------------------
# (3) the near list is the list it always was
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_near_list_is_unchanged_when_the_reach_is_on(
    corpus, one_hot_client, monkeypatch
):
    """Same rows, same order, same scores — the claim the whole design rests on."""
    set_reach(monkeypatch, NEAR, 0)
    baseline, _ = await _search(corpus, want_far=False)

    set_reach(monkeypatch, NEAR, REACH)
    near, far = await _search(corpus)

    assert _ids_and_scores(near) == _ids_and_scores(baseline)
    assert far, "the far list is empty, so this comparison is not testing anything"


# --------------------------------------------------------------------------
# (4) what the far list contains
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("min_sim", [-1.0, 0.4])
async def test_the_far_list_is_the_top_of_its_own_region(
    corpus, one_hot_client, monkeypatch, min_sim
):
    """The top ten of positions [NEAR, REACH), threshold and tie-break included.

    `min_sim=0.4` cuts the lower half of the far band, so the parametrisation is
    the threshold: a far list that ignored it would return the same rows plus
    weaker ones and fail on the comparison rather than on a count.
    """
    set_reach(monkeypatch, NEAR, REACH)
    near, far = await _search(corpus, limit=10, min_sim=min_sim)

    expected = await _oracle(corpus, start=NEAR, stop=REACH, limit=10, min_sim=min_sim)
    assert [(r["id"], r["_cosine"]) for r in far] == [
        (row_id, pytest.approx(score)) for row_id, score in expected
    ]


@pytest.mark.asyncio
async def test_reaching_past_the_answer_does_not_read_it(corpus, one_hot_client, monkeypatch):
    """The rows below the reach are the best in the corpus and must stay invisible.

    Reaching further is not free — the measurement that motivated this design
    found a 50,000-row window beating a 200,000-row one, because the wider one
    only made the same answers compete against 150,000 more rows. So the bound
    has to be a bound: `[REACH, TOTAL)` holds the highest similarities here, and
    a scan that read one row too far would return them and win nothing.
    """
    set_reach(monkeypatch, NEAR, REACH)
    near, far = await _search(corpus, limit=10)

    beyond = await corpus.execute_fetchall(
        "SELECT id FROM memories WHERE agent_id = ? ORDER BY created_at DESC, id ASC", (AGENT,)
    )
    unreachable = {row[0] for row in beyond[REACH:]}
    assert unreachable, "the fixture no longer holds rows past the reach"
    assert not unreachable & {r["id"] for r in near + far}, (
        "a row at a scan position past CPERSONA_VECTOR_REACH reached the answer"
    )


# --------------------------------------------------------------------------
# (5) the two lists cannot double-count a row
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_two_lists_are_disjoint(corpus, one_hot_client, monkeypatch):
    """Disjoint by position, which is what keeps a row from voting twice.

    The fusion adds one reciprocal-rank contribution per list, and the legacy
    quality gate rescales its threshold by the maximum a single row can score
    from three retrievers. Both stay right only while no row is on both lists.
    """
    set_reach(monkeypatch, NEAR, REACH)
    near, far = await _search(corpus, limit=25)

    assert near and far
    assert not set(_ids(near)) & set(_ids(far))


# --------------------------------------------------------------------------
# (6) both suppliers produce the same far list
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_index_and_the_scan_agree_on_the_far_list(
    corpus, one_hot_client, monkeypatch
):
    """An index build must not change the answer — including the second list.

    A separation that worked only when the index is present would make building
    one a behavioural change, which `docs/CONTIGUOUS_INDEX_DESIGN.md` §2 forbids.
    """
    set_reach(monkeypatch, NEAR, REACH)
    taken = _count_index_path(monkeypatch)

    scanned_near, scanned_far = await _search(corpus, limit=10)
    assert taken["index"] == 0, "no index exists yet — the baseline must be the live scan"

    assert (await vector_index.build_index(corpus, "memories"))["built"]
    taken["index"] = taken["fallback"] = 0

    indexed_near, indexed_far = await _search(corpus, limit=10)
    assert taken["index"] >= 1, "the index path was not taken; the comparison is vacuous"
    assert _ids_and_scores(indexed_far) == _ids_and_scores(scanned_far)
    assert _ids_and_scores(indexed_near) == _ids_and_scores(scanned_near)
    assert indexed_far, "an empty far list would make this agreement meaningless"


@pytest.mark.asyncio
async def test_the_suppliers_agree_when_rows_were_written_after_the_build(
    corpus, one_hot_client, monkeypatch
):
    """Rows above the watermark are the NEWEST rows, so they push the region down.

    They belong to the near list, and every row they displace moves one position
    older. An offset applied to the index selection alone would skip that many
    INDEXED rows and then hand the tail back as well — the near window's own
    content appearing in the far list — and the corpus is built so that shows up
    as a different answer rather than as a different order.
    """
    set_reach(monkeypatch, NEAR, REACH)

    # The five rows that the tail will push OUT of the near window and into the
    # far region, scored so that they must lead the far list once they get
    # there. Written before the build, because an embedding changed afterwards
    # would make the index stale about the row rather than about the window.
    shifted = await corpus.execute_fetchall(
        "SELECT id FROM memories WHERE agent_id = ? ORDER BY created_at DESC, id ASC"
        " LIMIT 5 OFFSET ?",
        (AGENT, NEAR - 5),
    )
    for row_id in [r[0] for r in shifted]:
        await corpus.execute(
            "UPDATE memories SET embedding = ? WHERE id = ?", (_vec(0.99), row_id)
        )

    # And one row that STAYS near, scored above everything: the merge walk has to
    # visit the rows the offset skips (their position is what decides where the
    # far region starts) without collecting any of them. A walk that visited and
    # kept them would put this row at the head of the far list.
    stays_near = await corpus.execute_fetchall(
        "SELECT id FROM memories WHERE agent_id = ? ORDER BY created_at DESC, id ASC"
        " LIMIT 1 OFFSET 10",
        (AGENT,),
    )
    anchor_id = stays_near[0][0]
    await corpus.execute(
        "UPDATE memories SET embedding = ? WHERE id = ?", (_vec(1.0), anchor_id)
    )
    await corpus.commit()

    assert (await vector_index.build_index(corpus, "memories"))["built"]

    # Newer than everything: five rows that take scan positions 0..4 and shift
    # the far region five rows further into the old corpus. They are scored just
    # below the rows they displace, so an offset applied to the index SELECTION
    # instead of to the merged order — which skips indexed rows and then hands
    # the tail back on top of them — returns these five at the head of the far
    # list instead of the five that moved into it. Both mistakes are invisible
    # on a corpus whose displaced rows score like their neighbours.
    for n in range(5):
        await corpus.execute(
            "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp,"
            " created_at, embedding) VALUES (?, '', '', ?, ?, ?, ?, ?)",
            (
                AGENT,
                f"written after the build {n}",
                '{"type": "User", "id": "user-a"}',
                "2026-03-01T00:00:00+00:00",
                f"2026-03-01 00:06:{n:02d}",
                _vec(0.98 - n / 1024),
            ),
        )
    await corpus.commit()

    taken = _count_index_path(monkeypatch)
    indexed_near, indexed_far = await _search(corpus, limit=10)
    assert taken["index"] >= 1, "the index path was not taken; the comparison is vacuous"

    _drop_indexes()
    taken["index"] = taken["fallback"] = 0
    scanned_near, scanned_far = await _search(corpus, limit=10)
    assert taken["index"] == 0, "the index answered after its file was removed"

    assert _ids_and_scores(indexed_far) == _ids_and_scores(scanned_far)
    assert _ids_and_scores(indexed_near) == _ids_and_scores(scanned_near)
    assert _ids_and_scores(scanned_far) == [
        (("mem", row_id), pytest.approx(score))
        for row_id, score in await _oracle(
            corpus, start=NEAR, stop=REACH, limit=10, min_sim=-1.0
        )
    ], "the tail shifted the corpus but not the region the far list is taken from"
    assert [r["_cosine"] for r in indexed_far[:5]] == [pytest.approx(0.99)] * 5, (
        "the five rows the tail pushed out of the near window did not lead the far "
        f"list: {_ids_and_scores(indexed_far)}"
    )
    assert not {r["_rid"] for r in indexed_far} & {r["_rid"] for r in indexed_near}, (
        "a row written after the build reached both lists, so the offset was applied "
        "to the index selection rather than to the merged scan order"
    )
    assert ("mem", anchor_id) in {r["_rid"] for r in indexed_near}
    assert ("mem", anchor_id) not in {r["_rid"] for r in indexed_far}, (
        "the strongest row in the near window reached the far list: the merge walk "
        "collected the rows it was only supposed to count past"
    )


@pytest.mark.asyncio
async def test_a_tail_longer_than_the_near_window_still_feeds_the_far_list(
    corpus, one_hot_client, monkeypatch
):
    """The rows read exactly must cover the far region, not just the near one.

    Everything written since the build is read from the live table, and that read
    is bounded. Bounding it by the far region's WIDTH rather than by where the
    region ends leaves the far list to be filled with indexed rows that sit
    further down the scan order than the rows it should hold — which only shows
    up once the tail is longer than the near window, the state a bulk write after
    a build produces.
    """
    set_reach(monkeypatch, NEAR, REACH)
    assert (await vector_index.build_index(corpus, "memories"))["built"]

    # 160 rows written after the build: 100 of them fill the near window, and the
    # far region then begins inside the tail. The five that land at far positions
    # score above the whole indexed corpus, so a read that stopped short of them
    # answers with different rows rather than with the same rows in a different
    # order.
    for n in range(160):
        score = 0.97 if 150 <= n < 155 else 0.20 + (n % 8) / 1024
        await corpus.execute(
            "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp,"
            " created_at, embedding) VALUES (?, '', '', ?, ?, ?, ?, ?)",
            (
                AGENT,
                f"bulk write {n}",
                '{"type": "User", "id": "user-a"}',
                "2026-03-01T00:00:00+00:00",
                f"2026-03-01 01:{(159 - n) // 60:02d}:{(159 - n) % 60:02d}",
                _vec(score),
            ),
        )
    await corpus.commit()

    taken = _count_index_path(monkeypatch)
    _, indexed_far = await _search(corpus, limit=10)
    assert taken["index"] >= 1, "the index path was not taken; the comparison is vacuous"

    expected = await _oracle(corpus, start=NEAR, stop=REACH, limit=10, min_sim=-1.0)
    assert _ids_and_scores(indexed_far) == [
        (("mem", row_id), pytest.approx(score)) for row_id, score in expected
    ]
    assert [r["_cosine"] for r in indexed_far[:5]] == [pytest.approx(0.97)] * 5, (
        f"the far region's own rows are missing from the far list: "
        f"{_ids_and_scores(indexed_far)}"
    )


@pytest.mark.asyncio
async def test_relative_score_fusion_receives_the_far_list_as_a_channel(
    corpus, one_hot_client, monkeypatch
):
    """Under rsf the far list is a fourth channel — which is not free, and is said so.

    Each channel is min-max normalised within itself and the sum divided by the
    number of ACTIVE channels, so a far list that exists lowers every fused score
    against the cosine-scale gate. That is accepted and unmeasured; what must not
    happen is the far rows being collected and then dropped, which is what a
    channel missing from the divisor list looks like from the outside: rows in
    the document map that no fused score can reach.
    """
    set_reach(monkeypatch, NEAR, REACH)
    _, far = await _search(corpus, limit=10)
    far_rids = {r["_rid"] for r in far}
    assert far_rids

    fused = await M._recall_rsf(corpus, AGENT, "row", 10, False)
    scored = {r["_rid"] for r in fused if "_rsf_score" in r and "_rid" in r}
    assert far_rids & scored, (
        "no far row carries a fused score, so the far list was read and then thrown "
        "away rather than fused as a channel"
    )


def _count_index_path(monkeypatch):
    """Count the phase-1 calls the contiguous index actually answered."""
    counter = {"index": 0, "fallback": 0}
    real = vector._index_phase1

    async def counting(*args, **kwargs):
        result = await real(*args, **kwargs)
        counter["index" if result is not None else "fallback"] += 1
        return result

    monkeypatch.setattr(vector, "_index_phase1", counting)
    return counter


# --------------------------------------------------------------------------
# (7) what the fusion does with it
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_near_row_keeps_the_fused_score_it_had(
    corpus, one_hot_client, monkeypatch
):
    """Under rrf a far list can only ADD.

    A row's reciprocal-rank contribution is a function of its rank on its own
    list, so appending a fourth list leaves every existing row's score alone —
    and that is the property the whole "default off, on is additive" claim rests
    on. Compared before the response pops the internal keys, by calling the
    fusion directly.
    """
    set_reach(monkeypatch, NEAR, 0)
    before = {r.get("_rid", r["id"]): r["_rrf_score"] for r in await M._recall_rrf(
        corpus, AGENT, "row", 10, False
    ) if "_rrf_score" in r}

    set_reach(monkeypatch, NEAR, REACH)
    after_rows = await M._recall_rrf(corpus, AGENT, "row", 10, False)
    after = {r.get("_rid", r["id"]): r["_rrf_score"] for r in after_rows if "_rrf_score" in r}

    assert before, "the reach-off run scored nothing; there is no claim to make"
    for rid, score in before.items():
        assert after.get(rid) == pytest.approx(score), (
            f"{rid} scored {after.get(rid)} with the far list present and {score} without it; "
            "the far list is displacing rows instead of adding them"
        )
    assert set(after) - set(before), "no far row reached the fusion, so nothing was added"


# --------------------------------------------------------------------------
# (8) episodes
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_far_list_reaches_episodes(db, one_hot_client, monkeypatch):
    """Episodes are scanned under the same window, so they have the same far region.

    A far list built from memories alone would be a silent halving of the reach
    on a corpus whose older material is episodic — the shape a long-running
    agent produces, since `archive_episode` is what summarises what fell out of
    the session.
    """
    set_reach(monkeypatch, 5, 15)
    # Memories score below every episode, so the merged far list is decided by
    # the episode window alone and a wrong episode window is a wrong answer.
    await _seed_memories(db, [0.30 + n / 1024 for n in range(20)], total=20)
    # Positions 0..4 are the episode scan's NEAR window and hold the strongest
    # episodes in the table; 5..14 are its far region; 15..19 are past the reach
    # and stronger still. An episode scan that ignored the offset would answer
    # with the near rows, which is a different answer rather than a shorter one.
    await _seed_episodes(
        db,
        [0.99] * 5 + [0.5 + n / 1024 for n in range(10)] + [0.95] * 5,
        total=20,
    )

    near, far = await _search(db, limit=5)

    expected = await _oracle(db, start=5, stop=15, limit=5, min_sim=-1.0, table="episodes")
    assert _ids_and_scores(far) == [
        (("ep", row_id), pytest.approx(score)) for row_id, score in expected
    ], (
        "the far list is not the top of the episode table's own far region: "
        f"{_ids_and_scores(far)}"
    )
    assert not set(_ids(far)) & set(_ids(near))


# --------------------------------------------------------------------------
# (9) the chunked read
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_far_region_spanning_several_chunks_is_still_exact(
    corpus, one_hot_client, monkeypatch
):
    """The far region is read in chunks like any other window.

    A cut applied per chunk instead of across the region would return the best
    rows of the LAST chunk, which on this corpus is a different answer and not
    merely a differently ordered one. The chunk size is set so the region spans
    three matrices; no matrix is small, and the one-hot vectors make the scores
    exact regardless.
    """
    monkeypatch.setattr(vector, "VECTOR_SCAN_CHUNK_ROWS", 64)
    set_reach(monkeypatch, NEAR, REACH)

    scored: list[int] = []
    real = vector._cosine_batch

    def recording(query_vec, query_dim, blobs):
        scored.append(len(blobs))
        return real(query_vec, query_dim, blobs)

    monkeypatch.setattr(vector, "_cosine_batch", recording)

    near, far = await _search(corpus, limit=10)

    # The near window is scored first and, at 100 rows in chunks of 64, arrives as
    # one matrix (the loop carries a short tail into the chunk before it rather
    # than scoring it alone). Everything after that belongs to the far region.
    assert scored and scored[0] == NEAR, f"the near window was not scored first: {scored}"
    far_matrices = scored[1:]
    assert len(far_matrices) >= 2 and sum(far_matrices) == REACH - NEAR, (
        f"the far region was not read as several chunks ({scored}); the case this test "
        "is about was never exercised"
    )
    expected = await _oracle(corpus, start=NEAR, stop=REACH, limit=10, min_sim=-1.0)
    assert [(r["id"], r["_cosine"]) for r in far] == [
        (row_id, pytest.approx(score)) for row_id, score in expected
    ]


# --------------------------------------------------------------------------
# (10) `CPERSONA_VECTOR_FAR_LIMIT`: how many far rows reach the fusion
# --------------------------------------------------------------------------
#
# The reach measurement rejected the far list at equal weight: 75% of the rows
# that displaced a recent answer carried a far-list vote and no other, and the
# near cost was nearly the same at a reach of 50,000 as at 300,000 — so it is the
# far list's ten full-strength votes, not the depth they came from, that
# displaces (`benchmarks/measurements/results-scan-window-reach-ab.md`). This
# setting bounds that count. It is a candidate-count knob: it decides which far
# rows reach the fusion, and changes nothing about how any row is scored, so the
# rows it keeps are the ones the unbounded list led with, in the same order.


def set_far_limit(monkeypatch, far_limit: int) -> None:
    """The far list's length, patched where the far scan reads it."""
    monkeypatch.setattr(vector, "VECTOR_FAR_LIMIT", far_limit)


@pytest.mark.asyncio
@pytest.mark.parametrize("far_limit", [0, 50])
async def test_a_far_limit_at_or_above_the_limit_is_the_far_list_it_always_was(
    corpus, one_hot_client, monkeypatch, far_limit
):
    """`0` is the default and means the response `limit`; `50` is `min` doing its job.

    Both are compared against the same oracle the unbounded far list is pinned
    to, so this is the "the default reproduces today's list" claim and the "a
    limit above the response size cannot lengthen it" claim in one statement. A
    setting that took `VECTOR_FAR_LIMIT` on its own instead of the smaller of the
    two would answer the `50` case with fifty rows.
    """
    set_reach(monkeypatch, NEAR, REACH)
    set_far_limit(monkeypatch, far_limit)

    near, far = await _search(corpus, limit=10)

    expected = await _oracle(corpus, start=NEAR, stop=REACH, limit=10, min_sim=-1.0)
    assert len(expected) == 10, "the fixture no longer fills a ten-row far list"
    assert [(r["id"], r["_cosine"]) for r in far] == [
        (row_id, pytest.approx(score)) for row_id, score in expected
    ]


@pytest.mark.asyncio
async def test_a_far_limit_below_the_limit_keeps_the_head_of_the_same_list(
    corpus, one_hot_client, monkeypatch
):
    """Three rows, and they are the first three of the ten — not three others.

    That is what makes this a count rather than a re-ranking: the cut is applied
    to a list already ordered the way the unbounded one is, so a shorter far list
    holds exactly the rows the longer one led with. The near list is the list it
    is with the reach off, and the two remain disjoint.
    """
    set_reach(monkeypatch, NEAR, 0)
    baseline_near, _ = await _search(corpus, want_far=False)

    set_reach(monkeypatch, NEAR, REACH)
    set_far_limit(monkeypatch, 3)
    near, far = await _search(corpus, limit=10)

    expected = await _oracle(corpus, start=NEAR, stop=REACH, limit=10, min_sim=-1.0)
    assert [(r["id"], r["_cosine"]) for r in far] == [
        (row_id, pytest.approx(score)) for row_id, score in expected[:3]
    ]
    assert _ids_and_scores(near) == _ids_and_scores(baseline_near), (
        "the far list's length reached the near list; the near cut is `limit` and "
        "is not this setting's business"
    )
    assert not set(_ids(near)) & set(_ids(far))


@pytest.mark.asyncio
async def test_the_cut_is_taken_across_the_merge_and_not_per_table(
    db, one_hot_client, monkeypatch
):
    """Two tables, a far limit of two, and two rows out — not two per table.

    Memories and episodes are scanned under the same window and merged, so a cut
    applied only to each table's own scan would hand the fusion one short list
    per table instead of one short list. On this corpus every far episode outscores
    every far memory, so the mistake is visible as a longer answer AND as rows
    that the merged top two does not contain.
    """
    set_reach(monkeypatch, 5, 15)
    set_far_limit(monkeypatch, 2)
    await _seed_memories(db, [0.30 + n / 1024 for n in range(20)], total=20)
    await _seed_episodes(
        db,
        [0.99] * 5 + [0.5 + n / 1024 for n in range(10)] + [0.95] * 5,
        total=20,
    )

    near, far = await _search(db, limit=5)

    expected = await _oracle(db, start=5, stop=15, limit=2, min_sim=-1.0, table="episodes")
    assert _ids_and_scores(far) == [
        (("ep", row_id), pytest.approx(score)) for row_id, score in expected
    ], f"the far list is not the merged region's top two: {_ids_and_scores(far)}"
    assert not set(_ids(far)) & set(_ids(near))


@pytest.mark.asyncio
async def test_the_fusion_receives_at_most_that_many_far_only_votes(
    corpus, one_hot_client, monkeypatch
):
    """The point of the setting, measured where it acts: inside the fusion.

    A row that scores with the reach on and did not score with it off reached the
    fusion through the far list and through no other list — the near list is
    identical between the two runs and the lexical lists do not know the setting
    exists — so counting the rows the far list added counts far-only votes. With
    the list cut to three there can be at most three, and every near row keeps the
    exact fused score it had, which is the "on only adds" claim at the new length.

    The unbounded run is here as the calibration: without it, "at most three" would
    also pass against a fusion that received nothing at all.
    """
    set_reach(monkeypatch, NEAR, 0)
    before = {r["_rid"]: r["_rrf_score"] for r in await M._recall_rrf(
        corpus, AGENT, "row", 10, False
    ) if "_rrf_score" in r and "_rid" in r}
    assert before, "the reach-off run scored nothing; there is no claim to make"

    set_reach(monkeypatch, NEAR, REACH)
    set_far_limit(monkeypatch, 0)
    unbounded = {r["_rid"] for r in await M._recall_rrf(corpus, AGENT, "row", 10, False)
                 if "_rrf_score" in r and "_rid" in r}
    assert len(unbounded - set(before)) > 3, (
        "the full-length far list added no more than three rows on this corpus, so a "
        f"bound of three would prove nothing: {len(unbounded - set(before))} added"
    )

    set_far_limit(monkeypatch, 3)
    after_rows = await M._recall_rrf(corpus, AGENT, "row", 10, False)
    after = {r["_rid"]: r["_rrf_score"] for r in after_rows
             if "_rrf_score" in r and "_rid" in r}

    far_only = set(after) - set(before)
    assert far_only, "no far row reached the fusion, so the far list was cut to nothing"
    assert len(far_only) <= 3, (
        f"{len(far_only)} rows carried a far-only vote with the far list cut to three"
    )
    for rid, score in before.items():
        assert after.get(rid) == pytest.approx(score), (
            f"{rid} scored {after.get(rid)} with a three-row far list and {score} "
            "without one; a shorter far list must still only add"
        )
