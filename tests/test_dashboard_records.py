"""The dashboard's episode, entity and profile views (2.6.16): lists, detail pages and each viewer's reach.

Each view is pinned on rows that a wrong query would order, match or describe
differently: episodes stored out of order, an entity whose alias (not its name)
matches, a relation whose far end was deleted, and two agents whose records name
each other, so a view that leaked one row of the hidden agent fails.
"""

import json
import os

import numpy as np
import pytest
import pytest_asyncio

from cpersona import acl, config, database
from cpersona.dashboard import store, web
from cpersona.dashboard.scope import Scope
from cpersona.database import get_db

TABLES = ("memories", "episodes", "entities", "entity_aliases", "entity_mentions", "relations", "profiles", "record_nodes", "record_blocks")


@pytest_asyncio.fixture
async def db():
    conn = await get_db()
    for table in TABLES:
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()
    return conn


async def _episode(conn, agent, summary, *, start=None, end=None, created="2026-10-01 00:00:00", keywords="", resolved=0, embedding=None):
    cur = await conn.execute(
        "INSERT INTO episodes (agent_id, summary, keywords, start_time, end_time, resolved, created_at, embedding) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (agent, summary, keywords, start, end, resolved, created, embedding),
    )
    await conn.commit()
    return cur.lastrowid


async def _memory(conn, agent, content):
    cur = await conn.execute(
        "INSERT INTO memories (agent_id, content, timestamp) VALUES (?, ?, '2026-10-01T00:00:00+00:00')", (agent, content)
    )
    await conn.commit()
    return cur.lastrowid


async def _entity(conn, agent, name, aliases=()):
    cur = await conn.execute(
        "INSERT INTO entities (agent_id, name, normalized, declared_by, created_at) VALUES (?, ?, ?, ?, '2026-10-02T00:00:00+00:00')",
        (agent, name, name.lower(), agent),
    )
    ent = cur.lastrowid
    for alias in aliases:
        await conn.execute(
            "INSERT INTO entity_aliases (entity_id, alias, normalized) VALUES (?, ?, ?)", (ent, alias, alias.lower())
        )
    await conn.commit()
    return ent


async def _mention(conn, ent, ref):
    await conn.execute(
        "INSERT INTO entity_mentions (entity_id, ref, declared_by, created_at) VALUES (?, ?, 'a', '2026-10-03T00:00:00+00:00')",
        (ent, ref),
    )
    await conn.commit()


async def _relation(conn, agent, subject, predicate, obj, anchor=""):
    cur = await conn.execute(
        "INSERT INTO relations (agent_id, subject_kind, subject_id, predicate, object_kind, object_id, anchor_ref, declared_by, declared_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, '2026-10-04T00:00:00+00:00')",
        (agent, *subject, predicate, *obj, anchor, agent),
    )
    await conn.commit()
    return cur.lastrowid


def _answer(route, scope=None, **q):
    return web.answer(database.DB_PATH, route, {k: str(v) for k, v in q.items()}, "t.db", scope)


def _read():
    return store.open_store(database.DB_PATH)


# ---- Episodes


@pytest.mark.asyncio
async def test_episodes_list_the_latest_conversation_first(db):
    # Stored in an order unlike when the conversations took place; one has no times.
    old = await _episode(db, "a", "the oldest talk", start="2026-09-01T10:00:00", end="2026-09-01T11:00:00", created="2026-10-09 00:00:00")
    new = await _episode(db, "a", "the newest talk", start="2026-10-05T10:00:00", end="2026-10-05T12:00:00", created="2026-10-06 00:00:00")
    untimed = await _episode(db, "a", "stored without times", created="2026-10-01 00:00:00")
    out = store.episodes(_read())
    assert out["total"] == 3
    assert [r["id"] for r in out["rows"]] == [new, untimed, old]


@pytest.mark.asyncio
async def test_episodes_are_searched_in_summaries_and_keywords(db):
    by_summary = await _episode(db, "a", "We planned the garden layout", keywords="plants")
    by_keyword = await _episode(db, "a", "Talked it through", keywords="garden budget")
    await _episode(db, "a", "Unrelated chat about trains", keywords="rail")
    found = {r["id"] for r in store.episodes(_read(), "garden")["rows"]}
    assert found == {by_summary, by_keyword}
    # Shorter than the trigram index can match: searched as a substring.
    short = await _episode(db, "a", "猫の話をした")
    assert [r["id"] for r in store.episodes(_read(), "猫")["rows"]] == [short]
    # The preview starts near the match.
    row = next(r for r in store.episodes(_read(), "garden")["rows"] if r["id"] == by_summary)
    assert "garden" in row["snippet"]


@pytest.mark.asyncio
async def test_episodes_come_a_page_at_a_time(db):
    ids = [await _episode(db, "a", f"talk {i}", end=f"2026-10-{i + 1:02d}T00:00:00") for i in range(5)]
    first = store.episodes(_read(), limit=2)
    second = store.episodes(_read(), limit=2, offset=2)
    assert first["total"] == second["total"] == 5
    assert [r["id"] for r in first["rows"] + second["rows"]] == ids[::-1][:4]


@pytest.mark.asyncio
async def test_an_episode_in_full_names_its_entities_and_never_its_vector(db):
    vec = np.asarray([1, 0, 0, 0], dtype=np.float32).tobytes()
    ep = await _episode(db, "a", "Met Caroline about the move", keywords="move", resolved=1, embedding=vec)
    other = await _episode(db, "a", "Another day")
    caroline = await _entity(db, "a", "Caroline")
    unrelated = await _entity(db, "a", "Melanie")
    await _mention(db, caroline, f"ep:{ep}")
    await _mention(db, unrelated, f"ep:{other}")
    code, out = _answer("/api/episode", id=ep)
    assert code == 200
    assert out["summary"] == "Met Caroline about the move"
    assert out["resolved"] is True and out["has_embedding"] is True
    assert out["entities"] == [{"id": caroline, "name": "Caroline"}]
    assert "embedding" not in out
    assert _answer("/api/episode", id=ep + 999)[0] == 404


# ---- Entities


@pytest.mark.asyncio
async def test_entities_list_the_most_mentioned_first_and_match_aliases(db):
    m1, m2 = await _memory(db, "a", "one"), await _memory(db, "a", "two")
    rare = await _entity(db, "a", "Bob")
    common = await _entity(db, "a", "Robert Smith", aliases=("Robert Smith", "Bobby"))
    await _mention(db, common, f"mem:{m1}")
    await _mention(db, common, f"mem:{m2}")
    await _mention(db, rare, f"mem:{m1}")
    await _relation(db, "a", ("entity", common), "knows", ("entity", rare))
    rows = store.entities(_read())["rows"]
    assert [r["id"] for r in rows] == [common, rare]
    assert rows[0]["mentions"] == 2 and rows[0]["relations"] == 1 and rows[1]["relations"] == 1
    # An alias equal to the name only repeats it.
    assert rows[0]["aliases"] == ["Bobby"]
    # "bobby" is in no name, only an alias.
    assert [r["id"] for r in store.entities(_read(), "bobby")["rows"]] == [common]
    assert {r["id"] for r in store.entities(_read(), "bob")["rows"]} == {common, rare}


@pytest.mark.asyncio
async def test_an_entity_page_describes_each_end_of_its_mentions_and_relations(db):
    mem = await _memory(db, "a", "Caroline moved to Sweden last spring")
    ep = await _episode(db, "a", "Planned the visit to Caroline")
    # Deleting a memory deletes its relations too (a trigger), so a relation whose
    # end is gone comes only from a store older than that trigger: written directly.
    gone = await _memory(db, "a", "the newest id") + 1000
    caroline = await _entity(db, "a", "Caroline", aliases=("Caro",))
    melanie = await _entity(db, "a", "Melanie")
    await _mention(db, caroline, f"mem:{mem}")
    await _mention(db, caroline, f"ep:{ep}")
    await _relation(db, "a", ("entity", melanie), "friend_of", ("entity", caroline), anchor=f"mem:{mem}")
    await _relation(db, "a", ("entity", caroline), "wrote", ("mem", gone))

    code, out = _answer("/api/entity", id=caroline)
    assert code == 200
    assert out["name"] == "Caroline" and out["aliases"] == ["Caro"]
    by_ref = {(m["kind"], m["id"]): m for m in out["mentions"]}
    assert set(by_ref) == {("mem", mem), ("ep", ep)}
    assert by_ref[("mem", mem)]["text"].startswith("Caroline moved to Sweden")
    assert by_ref[("ep", ep)]["text"] == "Planned the visit to Caroline"
    # A long record is cut, and says so.
    long_mem = await _memory(db, "a", "Caroline " + "word " * 100)
    await _mention(db, caroline, f"mem:{long_mem}")
    cut = next(m for m in _answer("/api/entity", id=caroline)[1]["mentions"] if m["id"] == long_mem)["text"]
    assert cut.endswith("…") and len(cut) <= store.REF_CHARS + 1

    rels = {r["predicate"]: r for r in out["relations"]}
    assert set(rels) == {"friend_of", "wrote"}
    friend = rels["friend_of"]
    assert friend["subject"] == {"kind": "entity", "id": melanie, "text": "Melanie", "missing": False}
    assert friend["object"]["id"] == caroline
    assert friend["anchor"]["kind"] == "mem" and friend["anchor"]["id"] == mem
    # The memory that is not there is named as missing, not dropped and not described.
    wrote = rels["wrote"]
    assert wrote["object"] == {"kind": "mem", "id": gone, "text": None, "missing": True}
    assert wrote["anchor"] is None
    assert _answer("/api/entity", id=caroline + 999)[0] == 404


# ---- Reach: a remote viewer sees only the agents they read


@pytest.mark.asyncio
async def test_a_viewer_sees_only_their_agents_episodes_and_entities(db):
    ep_a = await _episode(db, "a", "alpha conversation")
    ep_b = await _episode(db, "b", "bravo secret conversation")
    mem_a, mem_b = await _memory(db, "a", "alpha note"), await _memory(db, "b", "bravo secret note")
    thing = await _entity(db, "a", "Thing")
    hidden = await _entity(db, "b", "Hidden")
    for ref in (f"mem:{mem_a}", f"mem:{mem_b}", f"ep:{ep_b}"):
        await _mention(db, thing, ref)
    await _relation(db, "a", ("entity", thing), "cites", ("mem", mem_b))
    await _relation(db, "a", ("entity", thing), "near", ("entity", hidden))

    only_a = Scope({"a": acl.PERM_READ})
    assert [r["id"] for r in _answer("/api/episodes", only_a)[1]["rows"]] == [ep_a]
    assert _answer("/api/episodes", only_a, q="secret")[1]["total"] == 0
    assert _answer("/api/episode", only_a, id=ep_b)[0] == 404
    assert [r["id"] for r in _answer("/api/entities", only_a)[1]["rows"]] == [thing]
    assert _answer("/api/entity", only_a, id=hidden)[0] == 404
    code, page = _answer("/api/entity", only_a, id=thing)
    assert code == 200
    text = json.dumps(page)
    assert "bravo" not in text and "Hidden" not in text
    assert [(m["kind"], m["id"]) for m in page["mentions"]] == [("mem", mem_a)]
    assert page["relations"] == []
    listed = _answer("/api/entities", only_a)[1]["rows"][0]
    assert listed["mentions"] == 1 and listed["relations"] == 0

    # With both readable the same views do carry b: the checks above can fail.
    both = Scope({"*": acl.PERM_READ})
    text = json.dumps(_answer("/api/entity", both, id=thing)[1])
    assert "bravo" in text and "Hidden" in text
    assert _answer("/api/episodes", both)[1]["total"] == 2


# ---- Profiles


async def _profile(conn, agent, content, updated, user=""):
    await conn.execute(
        "INSERT INTO profiles (agent_id, user_id, content, updated_at) VALUES (?, ?, ?, ?)", (agent, user, content, updated)
    )
    await conn.commit()


@pytest.mark.asyncio
async def test_profiles_come_whole_the_latest_written_first_and_say_which_a_recall_sends(db):
    long = "Prefers short answers. " * 50
    await _profile(db, "a", long, "2026-10-30 10:00:00")
    await _profile(db, "b", "Works in the cpersona repository.", "2026-10-31 09:00:00")
    await _profile(db, "a", "A guest who asked once.", "2026-10-29 08:00:00", user="guest")
    out = store.profiles(_read())
    assert out["max_length"] == config.MAX_PROFILE_LENGTH
    assert [(r["agent_id"], r["user_id"]) for r in out["rows"]] == [("b", ""), ("a", ""), ("a", "guest")]
    first_a = out["rows"][1]
    # Whole, not a preview: the page shows exactly what a recall sends.
    assert first_a["content"] == long and first_a["length"] == len(long)
    # A recall attaches the agent's own profile, not one written for a named user.
    assert [r["in_recall"] for r in out["rows"]] == [True, True, False]
    assert [r["user_id"] for r in store.profiles(_read(), "a")["rows"]] == ["", "guest"]
    assert store.profiles(_read(), "nobody")["rows"] == []
    assert _answer("/api/overview")[1]["counts"]["profiles"] == 3


@pytest.mark.asyncio
async def test_a_viewer_sees_only_their_agents_profiles(db):
    await _memory(db, "a", "alpha note")
    await _memory(db, "b", "bravo note")
    await _profile(db, "a", "alpha profile", "2026-10-30 10:00:00")
    await _profile(db, "b", "bravo secret profile", "2026-10-30 11:00:00")
    only_a = Scope({"a": acl.PERM_READ})
    code, out = _answer("/api/profiles", only_a)
    assert code == 200 and [r["agent_id"] for r in out["rows"]] == ["a"]
    assert _answer("/api/profiles", only_a, agent="b")[1]["rows"] == []
    assert _answer("/api/overview", only_a)[1]["counts"]["profiles"] == 1
    # With both readable, b's profile is there: the checks above can fail.
    both = Scope({"*": acl.PERM_READ})
    assert "bravo secret" in json.dumps(_answer("/api/profiles", both)[1])


# ---- The overview's short lists


async def _memory_at(conn, agent, content, ts, recalled=0):
    cur = await conn.execute(
        "INSERT INTO memories (agent_id, content, timestamp, recall_count) VALUES (?, ?, ?, ?)", (agent, content, ts, recalled)
    )
    await conn.commit()
    return cur.lastrowid


@pytest.mark.asyncio
async def test_the_overview_lists_the_latest_memories_and_the_most_recalled(db):
    # Stored out of order, so a list in id order or in insertion order fails; of the
    # two recalled nine times, the later (day 3) is stored first, so it has the lower id.
    ids = {}
    for day, recalled in ((5, 2), (3, 9), (7, 0), (1, 9), (6, 4), (2, 1), (4, 0)):
        ids[day] = await _memory_at(db, "a", f"note of day {day}", f"2026-10-0{day}T12:00:00+00:00", recalled)
    out = store.overview(_read(), "t.db")
    assert store.OVERVIEW_ROWS == 5
    assert [r["id"] for r in out["recent"]] == [ids[d] for d in (7, 6, 5, 4, 3)]
    # Most recalled first, a tie broken by the later memory, and one never recalled
    # is left out even though fewer than five were recalled more.
    assert [r["id"] for r in out["recalled"]] == [ids[d] for d in (3, 1, 6, 5, 2)]
    first = out["recalled"][0]
    assert first == {"id": ids[3], "agent_id": "a", "timestamp": "2026-10-03T12:00:00+00:00", "recall_count": 9, "snippet": "note of day 3"}
    await db.execute("UPDATE memories SET recall_count = 0")
    await db.commit()
    assert store.overview(_read(), "t.db")["recalled"] == []


@pytest.mark.asyncio
async def test_a_viewer_sees_only_their_agents_memories_in_the_overview(db):
    await _memory_at(db, "a", "alpha note", "2026-10-01T00:00:00+00:00", 1)
    await _memory_at(db, "b", "bravo secret", "2026-10-02T00:00:00+00:00", 5)
    only_a = Scope({"a": acl.PERM_READ})
    code, out = _answer("/api/overview", only_a)
    assert code == 200
    assert [r["agent_id"] for r in out["recent"]] == ["a"] and [r["agent_id"] for r in out["recalled"]] == ["a"]
    # With both readable, b's memory leads both lists: the checks above can fail.
    both = _answer("/api/overview", Scope({"*": acl.PERM_READ}))[1]
    assert both["recent"][0]["agent_id"] == "b" and both["recalled"][0]["agent_id"] == "b"


# ---- The store's state


def _write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


@pytest.mark.asyncio
async def test_the_store_state_counts_what_a_vector_search_cannot_reach_and_reads_the_calibration(db):
    blob = np.ones(4, dtype=np.float32).tobytes()
    for agent, content, emb in (("a", "with a vector", blob), ("a", "without one", None), ("b", "bravo without", None)):
        await db.execute("INSERT INTO memories (agent_id, content, timestamp, embedding) VALUES (?, ?, '2026-10-01T00:00:00+00:00', ?)", (agent, content, emb))
    await db.commit()
    await _episode(db, "a", "a conversation", embedding=blob)
    await _episode(db, "b", "another", embedding=None)
    out = store.state(_read(), database.DB_PATH)
    assert out["memories"] == {"total": 3, "without_embedding": 2}
    assert out["episodes"] == {"total": 2, "without_embedding": 1}
    assert out["schema"] == database.SCHEMA_VERSION
    assert out["files"]["size"] > 0 and out["files"]["last_write"].endswith("+00:00")
    assert set(out["files"]["indexes"]) == {"memories", "episodes", "blocks"}
    side = database.DB_PATH + ".calibration.json"
    assert out["calibration"] is None  # nothing measured yet
    try:
        _write(side, "{not json")
        assert store.state(_read(), database.DB_PATH)["calibration"] == {"unreadable": True}
        _write(side, json.dumps({
            "embedding_model": "bge-m3", "embedding_dim": 1024, "scoring_version": "v9", "global_threshold": 0.55,
            "global_fused_gate": None, "fused_gate_signal": "confidence", "calibrated_at": "2026-10-30T22:14:05+00:00",
            "agent_thresholds": {"a": 0.6, "b": 0.5}, "agent_fused_gates": {"a": 0.05}, "agent_betas": {"a": 1},
        }))
        cal = store.state(_read(), database.DB_PATH)["calibration"]
        assert cal["global_threshold"] == 0.55 and cal["global_fused_gate"] is None and cal["scoring_version"] == "v9"
        assert cal["agents"] == [{"agent": "a", "threshold": 0.6, "fused_gate": 0.05}, {"agent": "b", "threshold": 0.5, "fused_gate": None}]
        # Only the keys the page shows: the betas are not sent.
        assert "agent_betas" not in cal
    finally:
        if os.path.exists(side):
            os.remove(side)


@pytest.mark.asyncio
async def test_a_viewer_of_some_agents_sees_their_counts_and_calibration_but_not_the_whole_store(db):
    await db.execute("INSERT INTO memories (agent_id, content, timestamp) VALUES ('a', 'alpha', '2026-10-01T00:00:00+00:00')")
    await db.execute("INSERT INTO memories (agent_id, content, timestamp) VALUES ('b', 'bravo', '2026-10-01T00:00:00+00:00')")
    await db.execute("INSERT INTO memories (agent_id, content, timestamp) VALUES ('b', 'bravo two', '2026-10-01T00:00:00+00:00')")
    await db.commit()
    side = database.DB_PATH + ".calibration.json"
    try:
        _write(side, json.dumps({"global_threshold": 0.55, "calibrated_at": "2026-10-30T22:14:05+00:00",
                                 "agent_thresholds": {"a": 0.6, "b": 0.5}, "agent_fused_gates": {"b": 0.07}}))
        only_a = Scope({"a": acl.PERM_READ})
        code, out = _answer("/api/state", only_a)
        assert code == 200
        assert out["memories"] == {"total": 1, "without_embedding": 1}
        # The files and the values for every agent describe the whole store.
        assert "files" not in out and "global_threshold" not in out["calibration"]
        assert out["calibration"]["agents"] == [{"agent": "a", "threshold": 0.6, "fused_gate": None}]
        # With every agent readable, all of it is there: the checks above can fail.
        everyone = _answer("/api/state", Scope({"*": acl.PERM_READ}))[1]
        assert everyone["memories"]["total"] == 3 and "files" in everyone
        assert everyone["calibration"]["global_threshold"] == 0.55 and len(everyone["calibration"]["agents"]) == 2
    finally:
        if os.path.exists(side):
            os.remove(side)
