"""The contiguous index serves the episode scan as it serves the memory scan.

Measured before this existed: at 100,000 memories and 20,000 episodes the
unindexed episode scan cost 96 ms per query — more than the indexed memory
scan of a table five times its size (benchmarks/measurements/
results-recall-path-profile.md). The index format, the selection and the
merge were already table-agnostic; what this pins is the read path taking the
index for `episodes`, and answering exactly what the live scan answers.

The shape of every claim is the one `test_vector_index_read_path.py` makes for
memories, with the episode-specific rules kept in view:

- episodes carry no `source`, so a `source_id` filter without a channel skips
  them entirely, and with a channel it is ignored (bug-080) — the index must not
  be asked about a column the table lacks;
- the answer rows carry the `[Episode]` prefix, `_rid=("ep", id)` and a
  timestamp derived from `start_time` with `created_at` as the fallback
  (bug-213), and none of that may change because the vectors came from a file.
"""

import logging
import os
import sqlite3

import numpy as np
import pytest
import pytest_asyncio

from cpersona import vector, vector_index
from cpersona.database import get_db
from cpersona.isolation import isolation_where
from tests.conftest import fake_embed_one

AGENT = "epindex.agent"
OTHER = "epindex.other"
TOPIC = "delta epsilon zeta"
_SHARED = fake_embed_one(f"{TOPIC} shared")


def _paths():
    out = []
    for table in ("memories", "episodes"):
        p = vector_index.index_path(table)
        out += [p, p + ".tmp"]
    return out


async def _memory(db, content, *, created_at, project_id="", channel="", agent_id=AGENT,
                  embedding=None):
    vec = fake_embed_one(content) if embedding is None else embedding
    await db.execute(
        "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp,"
        " created_at, embedding) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            agent_id, project_id, channel, content, '{"type": "User", "id": "user-1"}',
            "2026-03-01T00:00:00+00:00", created_at,
            np.array(vec, dtype=np.float32).tobytes(),
        ),
    )


async def _episode(db, summary, *, created_at, project_id="", channel="", agent_id=AGENT,
                   embedding=None, start_time=None, resolved=0):
    vec = fake_embed_one(summary) if embedding is None else embedding
    await db.execute(
        "INSERT INTO episodes (agent_id, project_id, channel, summary, keywords, embedding,"
        " start_time, resolved, created_at) VALUES (?, ?, ?, ?, '', ?, ?, ?, ?)",
        (
            agent_id, project_id, channel, summary,
            np.array(vec, dtype=np.float32).tobytes(), start_time, resolved, created_at,
        ),
    )


@pytest_asyncio.fixture
async def corpus(fake_embedding_client):
    """Memories and episodes with deliberate score ties, axes varied on both.

    Ties are what make order a claim: a corpus of well-separated vectors ranks
    the same under any tie-break and cannot tell a right merge from a wrong one.
    Three values per axis, so a filter on one actually excludes rows.
    """
    db = await get_db()
    for p in _paths():
        if os.path.exists(p):
            os.unlink(p)
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM episodes")

    n = 0
    for stamp in ("2026-03-01 00:00:00", "2026-03-01 00:00:01", "2026-03-01 00:00:02"):
        for project in ("", "proj-x", "proj-y"):
            for channel in ("", "c1", "c2"):
                for agent in (AGENT, OTHER):
                    n += 1
                    tied = _SHARED if n % 3 else fake_embed_one(f"unrelated {n}")
                    await _memory(
                        db, f"memory {n}", created_at=stamp, project_id=project,
                        channel=channel, agent_id=agent, embedding=tied,
                    )
                    await _episode(
                        db, f"episode {n}", created_at=stamp, project_id=project,
                        channel=channel, agent_id=agent, embedding=tied,
                        # Half the episodes date themselves; the other half fall
                        # back to created_at (bug-213). Both shapes must survive.
                        start_time=None if n % 2 else "2026-02-28T12:00:00+00:00",
                        resolved=n % 2,
                    )
    await db.commit()
    yield db
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM episodes")
    await db.commit()
    for p in _paths():
        if os.path.exists(p):
            os.unlink(p)


@pytest.fixture
def taken(monkeypatch):
    """Phase-1 answers the index gave, per table.

    Every way this can go wrong ends in a silent fallback, so 'the answers
    match' is only worth anything next to 'the index produced one of them' —
    and for THIS table, since the memory index answering says nothing about
    the episode one.
    """
    counter = {"memories": 0, "episodes": 0, "fallback": 0, "scans": 0}
    real = vector._index_phase1
    real_batch = vector._cosine_batch

    async def counting(*args, **kwargs):
        result = await real(*args, **kwargs)
        table = kwargs.get("table", "memories")
        counter[table if result is not None else "fallback"] += 1
        return result

    def counting_batch(*args, **kwargs):
        # The live scan's scorer, and only its: a phase-1 answer that a scan
        # then ignored would still count above, so the scan itself is counted
        # too. `scans` is how many tables were ranked from SQLite rows.
        counter["scans"] += 1
        return real_batch(*args, **kwargs)

    monkeypatch.setattr(vector, "_index_phase1", counting)
    monkeypatch.setattr(vector, "_cosine_batch", counting_batch)
    return counter


async def _search(db, **kw):
    # A limit wide enough that episodes place: memories are scanned first and
    # win every tie, so a narrow limit would fill with memories alone and the
    # episode assertions below would be vacuous.
    return await vector._search_vector(db, AGENT, TOPIC, 40, min_similarity=-1.0, **kw)


async def _search_without_index(db, **kw):
    """The same query with the episode index removed, as the order oracle."""
    path = vector_index.index_path("episodes")
    saved = open(path, "rb").read() if os.path.exists(path) else None
    if saved is not None:
        os.unlink(path)
    try:
        return await _search(db, **kw)
    finally:
        if saved is not None:
            with open(path, "wb") as fh:
                fh.write(saved)


def _episode_rows(rows):
    return [r for r in rows if r.get("_rid", ("", 0))[0] == "ep"]


_SCENARIOS = {
    "no-filter": {},
    "project-gamma": {"project_id": "proj-x"},
    "project-global-only": {"project_id": ""},
    "channel": {"channel": "c1"},
    "channel-and-project": {"channel": "c1", "project_id": "proj-x"},
    # bug-080: a channel makes episodes safe to return under a source filter.
    "source-and-channel": {"source_id": "user-1", "channel": "c1"},
}


@pytest.mark.parametrize("name", sorted(_SCENARIOS))
@pytest.mark.asyncio
async def test_episode_index_answers_exactly_what_the_scan_answers(corpus, taken, name):
    scenario = _SCENARIOS[name]
    expected = await _search(corpus, **scenario)
    assert _episode_rows(expected), f"[{name}] the fixture must put episodes in the answer"
    assert taken["episodes"] == 0, "no index exists yet — the baseline must be the live scan"

    result = await vector_index.build_index(corpus, "episodes")
    assert result["built"], result

    taken["scans"] = 0
    actual = await _search(corpus, **scenario)
    assert taken["episodes"] == 1, f"[{name}] the episode index path was not taken"
    assert taken["scans"] == 1, f"[{name}] only memories should still be scanned from rows"
    assert actual == expected, f"[{name}] the episode index changed the answer"


@pytest.mark.asyncio
async def test_both_indexes_together_answer_what_the_two_scans_answer(corpus, taken):
    """The production shape: one file per table, both consulted on every query."""
    expected = await _search(corpus)
    assert (await vector_index.build_index(corpus, "memories"))["built"]
    assert (await vector_index.build_index(corpus, "episodes"))["built"]

    taken["scans"] = 0
    actual = await _search(corpus)
    assert taken["memories"] == 1 and taken["episodes"] == 1, taken
    assert taken["scans"] == 0, "with both indexes built nothing should be ranked from rows"
    assert actual == expected


@pytest.mark.asyncio
async def test_a_source_filter_without_a_channel_never_asks_the_episode_index(corpus, taken):
    """Episodes carry no source column. The scan skips them; so must the index path,
    without ever being asked about a column the table lacks."""
    assert (await vector_index.build_index(corpus, "episodes"))["built"]
    rows = await _search(corpus, source_id="user-1")
    assert not _episode_rows(rows), "a source-only filter must exclude every episode"
    assert taken["episodes"] == 0, "the episode index was consulted for a query that skips episodes"


@pytest.mark.asyncio
async def test_an_episode_written_after_the_build_is_still_found(corpus, taken):
    """The watermark's whole point, for the second table."""
    assert (await vector_index.build_index(corpus, "episodes"))["built"]
    await _episode(corpus, f"{TOPIC} freshly archived", created_at="2026-03-01 00:00:09",
                   embedding=_SHARED)
    await corpus.commit()

    rows = await _search(corpus)
    assert taken["episodes"] >= 1
    eps = _episode_rows(rows)
    assert eps and "freshly archived" in eps[0]["content"], (
        "an episode archived after the build must reach the answer through the exact tail, "
        "and its recency must place it where the live scan would"
    )
    assert [r["id"] for r in rows] == [r["id"] for r in await _search_without_index(corpus)]


@pytest.mark.asyncio
async def test_an_episode_older_than_the_index_lands_in_the_right_place(corpus, taken):
    """An ordered merge, not a prepend — the import path gives a restored episode a
    fresh id and its original timestamp."""
    assert (await vector_index.build_index(corpus, "episodes"))["built"]
    await _episode(corpus, f"{TOPIC} restored", created_at="2025-01-01 00:00:00",
                   embedding=_SHARED)
    await corpus.commit()

    actual = await _search(corpus)
    assert taken["episodes"] >= 1
    assert [r["id"] for r in actual] == [r["id"] for r in await _search_without_index(corpus)]


@pytest.mark.asyncio
async def test_the_answer_rows_keep_their_episode_shape(corpus, taken):
    """Prefix, kind marker, the bug-213 timestamp rule and the resolved flag are
    the hydrate's business, and the hydrate did not change."""
    assert (await vector_index.build_index(corpus, "episodes"))["built"]
    rows = _episode_rows(await _search(corpus))
    assert taken["episodes"] == 1 and taken["scans"] == 1
    assert rows
    for r in rows:
        assert r["content"].startswith("[Episode] episode ")
        assert r["source"] == {"System": "episode"}
        assert r["_rid"] == ("ep", r["id"])
        assert isinstance(r["_resolved"], bool)
        assert r["timestamp"], "bug-213: an undated episode still gets created_at"


@pytest.mark.asyncio
async def test_absent_episode_index_falls_back(corpus, taken):
    expected = await _search(corpus)
    assert (await vector_index.build_index(corpus, "episodes"))["built"]
    os.unlink(vector_index.index_path("episodes"))
    assert await _search(corpus) == expected
    assert taken["episodes"] == 0, "a deleted index must not still be answering"


@pytest.mark.asyncio
async def test_corrupt_episode_index_falls_back(corpus, taken):
    expected = await _search(corpus)
    assert (await vector_index.build_index(corpus, "episodes"))["built"]
    path = vector_index.index_path("episodes")
    with open(path, "r+b") as fh:
        fh.truncate(os.path.getsize(path) - 8)
    assert await _search(corpus) == expected
    assert taken["episodes"] == 0, "a corrupt index must be refused, not partially read"


@pytest.mark.asyncio
async def test_a_raising_episode_probe_stays_inside_recall(corpus, taken, monkeypatch, caplog):
    """bug-285's claim, for the second table: whatever the probe raises, recall survives."""
    expected = await _search(corpus)
    assert (await vector_index.build_index(corpus, "episodes"))["built"]

    async def exploding(*args, **kwargs):
        raise sqlite3.OperationalError("too many SQL variables")

    monkeypatch.setattr(vector, "_index_rows_lost_embedding", exploding)
    with caplog.at_level(logging.WARNING, logger=vector.logger.name):
        assert await _search(corpus) == expected
    assert "probe raised" in caplog.text


@pytest.mark.asyncio
async def test_an_episode_that_lost_its_embedding_hands_the_query_back(corpus, taken):
    """bug-279 for episodes: the file keeps bytes the row no longer has."""
    assert (await vector_index.build_index(corpus, "episodes"))["built"]
    row = await corpus.execute_fetchall(
        "SELECT id FROM episodes WHERE agent_id = ? ORDER BY id LIMIT 1", (AGENT,)
    )
    await corpus.execute("UPDATE episodes SET embedding = NULL WHERE id = ?", (row[0][0],))
    await corpus.commit()

    actual = await _search(corpus)
    assert taken["episodes"] == 0, "an index holding a cleared vector must not answer"
    assert [r["id"] for r in actual] == [r["id"] for r in await _search_without_index(corpus)]


@pytest.mark.asyncio
async def test_the_probe_sql_names_the_table_it_is_asked_about():
    """The statements are built per table; a memories-shaped probe against the
    episode index would answer the wrong question without an error."""
    assert "FROM episodes" in vector._lost_embedding_range_sql("episodes")
    assert "FROM episodes" in vector._lost_embedding_chunk_sql(3, "episodes")
    assert vector._lost_embedding_range_sql("memories") == vector._LOST_EMBEDDING_RANGE_SQL


# --------------------------------------------------------------------------------------
# One-directional containment, for the second table.
# --------------------------------------------------------------------------------------

AGENTS = ("agent.a", "agent.b", "")
PROJECTS = ("", "proj.x", "proj.y")
CHANNELS = ("", "chan.1", "chan.2")


@pytest_asyncio.fixture
async def indexed_episodes():
    import random

    db = await get_db()
    path = vector_index.index_path("episodes")
    for p in (path, path + ".tmp"):
        if os.path.exists(p):
            os.unlink(p)
    await db.execute("DELETE FROM episodes")
    rng = random.Random(20260902)
    for n in range(300):
        await db.execute(
            "INSERT INTO episodes (agent_id, project_id, channel, summary, keywords, embedding,"
            " start_time, resolved, created_at) VALUES (?, ?, ?, ?, '', ?, NULL, 0, ?)",
            (
                rng.choice(AGENTS), rng.choice(PROJECTS), rng.choice(CHANNELS), f"ep {n}",
                np.random.default_rng(n).standard_normal(6).astype(np.float32).tobytes(),
                f"2026-03-01 00:{n // 60:02d}:{n % 60:02d}",
            ),
        )
    await db.commit()
    result = await vector_index.build_index(db, "episodes", path)
    assert result["built"], result
    yield db, vector_index.load_index("episodes", path)
    await db.execute("DELETE FROM episodes")
    await db.commit()
    for p in (path, path + ".tmp"):
        if os.path.exists(p):
            os.unlink(p)


@pytest.mark.asyncio
async def test_episode_index_never_hides_a_row_the_authority_admits(indexed_episodes):
    db, index = indexed_episodes
    misses, covered, narrowed = [], 0, 0
    everything = None
    for agent in AGENTS:
        for project in (None, *PROJECTS):
            for channel in CHANNELS:
                iso = isolation_where(agent_id=agent, project_id=project, channel=channel)
                rows = await db.execute_fetchall(
                    f"SELECT id FROM episodes WHERE {iso.clause} AND embedding IS NOT NULL",
                    tuple(iso.params),
                )
                admitted = {r[0] for r in rows}
                positions = vector_index.select(
                    index, agent_id=agent, project_id=project, channel=channel,
                    source_id="", limit=0,
                )
                offered = {int(index.ids[p]) for p in positions}
                covered += len(admitted)
                if everything is None:
                    everything = admitted
                elif admitted < everything:
                    narrowed += 1
                if not admitted <= offered:
                    misses.append(f"agent={agent!r} project={project!r} channel={channel!r}")
    assert not misses, "the episode index hid rows the authority admits:\n  " + "\n  ".join(misses)
    assert covered > 500, f"the axis matrix admitted only {covered} rows in total"
    assert narrowed > 0, "no combination excluded anything; the property is vacuous"
