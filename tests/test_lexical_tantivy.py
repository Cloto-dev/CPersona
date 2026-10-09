"""CPERSONA_LEXICAL_ENGINE=tantivy: the keyword arms answered from an index derived from SQLite.

The index is not the isolation authority, but it ranks inside the authority's set: a looser set cut
at the limit would lose rows silently. So, over a random store and many random filters, the rows
it returns for a term every row holds must be exactly the rows the authority's SQL selects -- for
memories and episodes, across agent, project, channel, source and window. And it must follow the
store: a row added, rewritten, moved or deleted is searched as it now is.
"""

import itertools
import json
import random

import pytest
import pytest_asyncio

pytest.importorskip("tantivy")

from cpersona import config, lexical_tantivy  # noqa: E402
from cpersona import memory_handlers as M  # noqa: E402
from cpersona.database import get_db  # noqa: E402
from cpersona.isolation import isolation_where, source_id_where  # noqa: E402

WORD = "zephyrine"
AGENTS = ["agent-lt-a", "agent-lt-b"]
PROJECTS = ["", "proj-x", "proj-y"]
CHANNELS = ["", "c1", "c2"]
SOURCES = ["discord:Alice", "discord:alice", "web:1", None]
WINDOW = ("2026-03-01T00:00:00Z", "2026-06-01T00:00:00Z")


async def _drop(db):
    for t in ("mem_ai", "mem_ad", "mem_au", "ep_ai", "ep_ad", "ep_au"):
        await db.execute(f"DROP TRIGGER IF EXISTS lexical_log_{t}")
    await db.execute("DROP TABLE IF EXISTS lexical_changes")
    await db.execute("DROP TABLE IF EXISTS lexical_log_clock")
    await db.commit()


@pytest_asyncio.fixture
async def tantivy_db(monkeypatch):
    db = await get_db()
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM episodes")
    await _drop(db)
    monkeypatch.setattr(config, "LEXICAL_ENGINE", "tantivy")
    monkeypatch.setattr(M, "LEXICAL_ENGINE", "tantivy")
    await lexical_tantivy.install(db)
    await db.commit()
    lexical_tantivy._cache.clear()
    yield db
    lexical_tantivy._cache.clear()
    await _drop(db)
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM episodes")
    await db.commit()


async def _seed(db, rng: random.Random, n: int = 120):
    for i in range(n):
        agent, project, channel = rng.choice(AGENTS), rng.choice(PROJECTS), rng.choice(CHANNELS)
        source = rng.choice(SOURCES)
        month = rng.randint(1, 9)
        ts = f"2026-0{month}-1{rng.randint(0, 9)}T0{rng.randint(0, 9)}:00:00Z"
        await db.execute(
            "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
            (agent, project, channel, f"{WORD} record {i} の記録 filler{rng.randint(0, 5)}",
             json.dumps({"id": source} if source else {}), ts),
        )
    # Episodes draw their own axes and are fewer, so an episode's id never names a memory
    # with the same filters: a memory and an episode taken for each other would show.
    for i in range(n * 3 // 4):
        agent, project, channel = rng.choice(AGENTS), rng.choice(PROJECTS), rng.choice(CHANNELS)
        ts = f"2026-0{rng.randint(1, 9)}-1{rng.randint(0, 9)}T00:00:00Z"
        await db.execute(
            "INSERT INTO episodes (agent_id, project_id, channel, summary, keywords, start_time) VALUES (?, ?, ?, ?, ?, ?)",
            (agent, project, channel, f"{WORD} episode {i}", "", ts if rng.random() < 0.7 else ""),
        )
    await db.commit()


async def _authority_memories(db, agent, project, channel, source, window):
    iso = isolation_where(agent_id=agent, project_id=project, channel=channel)
    src = source_id_where(source)
    win = " AND datetime(timestamp) >= datetime(?) AND datetime(timestamp) < datetime(?)" if window else ""
    rows = await db.execute_fetchall(
        f"SELECT id FROM memories WHERE {iso.clause}{src.and_clause}{win} AND content LIKE ?",
        (*iso.params, *src.params, *(window or ()), f"%{WORD}%"),
    )
    return {r[0] for r in rows}


async def _authority_episodes(db, agent, project, channel, window):
    iso = isolation_where(agent_id=agent, project_id=project, channel=channel, alias="e")
    rows = await db.execute_fetchall(
        f"SELECT e.id FROM episodes e WHERE {iso.clause}{M._EPISODE_IN_WINDOW if window else ''} AND e.summary LIKE ?",
        (*iso.params, *(window or ()), f"%{WORD}%"),
    )
    return {r[0] for r in rows}


@pytest.mark.asyncio
async def test_the_index_returns_the_authoritys_set_on_every_filter(tantivy_db):
    db = tantivy_db
    await _seed(db, random.Random(20261009))
    checked = 0
    for agent, project, channel, source, window in itertools.product(
        AGENTS, [None, *PROJECTS], CHANNELS, ["", "discord:Alice", "discord:a", "web"], [None, WINDOW]
    ):
        # The limit is the authority's count: a looser set in the index would spend places on
        # rows the read-back then drops, and the answer would come back short.
        want = await _authority_memories(db, agent, project, channel, source, window)
        got = await lexical_tantivy.search_memories(db, agent, WORD, max(len(want), 1), channel, project, source, None, window)
        assert {r["id"] for r in got} == want, (agent, project, channel, source, window)
        checked += bool(want)
        if not source:
            want_ep = await _authority_episodes(db, agent, project, channel, window)
            got_ep = await lexical_tantivy.search_episodes(
                db, agent, WORD, max(len(want_ep), 1), channel, project, None, window, M._EPISODE_IN_WINDOW
            )
            assert {r[0] for r in got_ep} == want_ep, ("ep", agent, project, channel, window)
    assert checked > 50, "fixture is vacuous: most filters selected nothing"


@pytest.mark.asyncio
async def test_the_limit_cuts_inside_the_set(tantivy_db):
    db = tantivy_db
    await _seed(db, random.Random(7))
    agent, project, channel = AGENTS[0], "proj-x", "c1"
    want = await _authority_memories(db, agent, project, channel, "", None)
    assert len(want) > 3, "fixture is vacuous"
    got = await lexical_tantivy.search_memories(db, agent, WORD, 3, channel, project, "", None, None)
    assert len(got) == 3 and {r["id"] for r in got} <= want


@pytest.mark.asyncio
async def test_the_index_follows_the_store(tantivy_db):
    db = tantivy_db
    agent = AGENTS[0]

    async def ids(term, project=None):
        rows = await lexical_tantivy.search_memories(db, agent, term, 100, "", project, "", None, None)
        return {r["id"] for r in rows}

    cur = await db.execute(
        "INSERT INTO memories (agent_id, content, timestamp) VALUES (?, 'quokka habitat notes', '2026-01-01T00:00:00Z')",
        (agent,),
    )
    await db.commit()
    rid = cur.lastrowid
    assert await ids("quokka") == {rid}  # built after the insert
    await db.execute("UPDATE memories SET content = 'wombat burrow notes' WHERE id = ?", (rid,))
    await db.commit()
    assert await ids("quokka") == set() and await ids("wombat") == {rid}
    await db.execute("UPDATE memories SET project_id = 'proj-y' WHERE id = ?", (rid,))
    await db.commit()
    assert await ids("wombat", "") == set() and await ids("wombat", "proj-y") == {rid}
    cur = await db.execute(
        "INSERT INTO memories (agent_id, content, timestamp) VALUES (?, 'wombat second', '2026-01-02T00:00:00Z')", (agent,)
    )
    await db.commit()
    assert await ids("wombat") == {rid, cur.lastrowid}
    await db.execute("DELETE FROM memories WHERE id = ?", (rid,))
    await db.commit()
    assert await ids("wombat") == {cur.lastrowid}


@pytest.mark.asyncio
async def test_no_log_means_no_index(tantivy_db):
    db = tantivy_db
    await _drop(db)
    lexical_tantivy._cache.clear()
    assert await lexical_tantivy.search_memories(db, AGENTS[0], WORD, 10, "", None, "", None, None) is None


def test_terms_keep_an_identifier_whole_and_cut_japanese(monkeypatch):
    monkeypatch.setattr(config, "TANTIVY_ASCII", "w")
    monkeypatch.setattr(config, "TANTIVY_CJK", "k3")
    monkeypatch.setattr(config, "QUERY_SEGMENTER", "trigram")
    assert lexical_tantivy.terms("CVE-2024-3094 の影響") == ["cve", "2024", "3094", "cve-2024-3094", "の影響"]
    assert lexical_tantivy.terms("plain words.") == ["plain", "words"]
    assert lexical_tantivy.terms("bug-191 fix", query=True) == ["bug", "191", "bug-191", "fix"]
    monkeypatch.setattr(config, "TANTIVY_ASCII", "wi")
    assert lexical_tantivy.terms("bug-191 fix", query=True) == ["bug-191", "fix"]
    assert lexical_tantivy.terms("bug-191 fix") == ["bug", "191", "bug-191", "fix"]
    monkeypatch.setattr(config, "TANTIVY_ASCII", "w")
    monkeypatch.setattr(config, "TANTIVY_CJK", "k2")
    assert lexical_tantivy.terms("の影響") == ["の影", "影響"]
    monkeypatch.setattr(config, "TANTIVY_ASCII", "t")
    assert lexical_tantivy.terms("Quokka") == ["quo", "uok", "okk", "kka"]
