"""Declaring associative memory (docs/ASSOCIATIVE_MEMORY_DESIGN.md §2).

Normalization is the only processing a declaration receives; an alias resolves
to at most one entity per scope; a record endpoint must be this agent's; a
malformed item is reported and skipped, never a reason to lose the memory a
`store` carried it on; and the whole call is bounded.
"""

import pytest
import pytest_asyncio

from cpersona import associations, server, session
from cpersona.database import get_db

AGENT = "declare-a"


@pytest_asyncio.fixture
async def clean_db():
    session.reset_pauses_for_tests()
    db = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks", "entities",
                  "entity_aliases", "entity_mentions", "relations"):
        await db.execute(f"DELETE FROM {table}")
    await db.execute(
        "DELETE FROM sqlite_sequence WHERE name IN "
        "('memories','episodes','profiles','pending_memory_tasks','entities','relations')"
    )
    await db.commit()
    yield db
    session.reset_pauses_for_tests()


async def _store(content: str, associations_: dict | None = None, agent: str = AGENT, **kw) -> dict:
    return await server.do_store_boundary(agent, {"content": content}, associations=associations_, **kw)


async def _entities(db, agent: str = AGENT) -> dict[str, int]:
    rows = await db.execute_fetchall("SELECT name, id FROM entities WHERE agent_id = ? ORDER BY id", (agent,))
    return {r[0]: r[1] for r in rows}


async def _aliases(db, entity_id: int) -> set[str]:
    rows = await db.execute_fetchall("SELECT alias FROM entity_aliases WHERE entity_id = ?", (entity_id,))
    return {r[0] for r in rows}


async def _mentions(db, entity_id: int) -> set[str]:
    rows = await db.execute_fetchall("SELECT ref FROM entity_mentions WHERE entity_id = ?", (entity_id,))
    return {r[0] for r in rows}


async def _relations(db, agent: str = AGENT) -> list[tuple]:
    return await db.execute_fetchall(
        "SELECT subject_kind, subject_id, predicate, object_kind, object_id, anchor_ref, declared_by "
        "FROM relations WHERE agent_id = ? ORDER BY id",
        (agent,),
    )


# --------------------------------------------------------------------------
# normalization
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "declared, expected",
    [
        ("MizEye", "mizeye"),
        ("  Miz   Eye ", "miz eye"),
        ("ＭｉｚＥｙｅ", "mizeye"),  # full-width → NFKC
        ("ミズアイ", "ミズアイ"),
        ("Straße", "strasse"),  # casefold, not lower
        ("Ｋｉｒａｒｉ　ちゃん", "kirari ちゃん"),  # ideographic space collapses too
    ],
)
def test_normalize_is_nfkc_casefold_and_whitespace_collapse(declared, expected):
    assert associations.normalize(declared) == expected


def test_refs_parse_only_the_two_record_kinds():
    memory_id, episode_id = 12, 3
    assert associations.parse_ref(f"mem:{memory_id}") == ("mem", memory_id)
    assert associations.parse_ref(f" ep:{episode_id} ") == ("ep", episode_id)
    for bad in ("entity:1", "mem:", "mem:x", "12", None, 12):
        assert associations.parse_ref(bad) is None


# --------------------------------------------------------------------------
# riding on store
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_rider_registers_entities_aliases_mentions_and_anchored_relations(clean_db):
    res = await _store(
        "Kirari keeps MizEye running",
        {
            "entities": [{"name": "MizEye", "aliases": ["mizeye", "ミズアイ"]}, {"name": "Kirari"}],
            "relations": [{"subject": "Kirari", "predicate": "maintains", "object": "MizEye"}],
        },
    )
    assert res["result"] == "stored", res
    mem_id = res["id"]
    report = res["associations"]
    assert report["dropped"] == []
    names = await _entities(clean_db)
    assert set(names) == {"MizEye", "Kirari"}
    assert [e["name"] for e in report["entities"]] == ["MizEye", "Kirari"]
    assert all(e["created"] for e in report["entities"])
    assert await _aliases(clean_db, names["MizEye"]) == {"mizeye", "ミズアイ"}
    # Both named entities are mentioned by the stored memory.
    assert await _mentions(clean_db, names["MizEye"]) == {f"mem:{mem_id}"}
    assert await _mentions(clean_db, names["Kirari"]) == {f"mem:{mem_id}"}
    assert report["mentions"] == 2
    # The relation is anchored to the memory and its predicate is normalized.
    assert await _relations(clean_db) == [
        ("entity", names["Kirari"], "maintains", "entity", names["MizEye"], f"mem:{mem_id}", "agent")
    ]
    assert len(report["relations"]) == 1


@pytest.mark.asyncio
async def test_a_malformed_declaration_is_reported_and_the_memory_is_still_stored(clean_db):
    res = await _store(
        "still stored",
        {"entities": [{"name": ""}, "not an object", {"name": "Fine", "aliases": "not a list"}], "relations": 3},
    )
    assert res["result"] == "stored", res
    report = res["associations"]
    assert [e["name"] for e in report["entities"]] == ["Fine"]
    reasons = {d["item"]: d["reason"] for d in report["dropped"]}
    assert set(reasons) == {"entities[0]", "entities[1]", "entities[2].aliases", "relations"}
    rows = await clean_db.execute_fetchall("SELECT COUNT(*) FROM memories WHERE agent_id = ?", (AGENT,))
    assert rows[0][0] == 1


@pytest.mark.asyncio
async def test_without_the_rider_store_writes_nothing_to_the_graph(clean_db):
    res = await _store("plain")
    assert res["result"] == "stored" and "associations" not in res
    for table in ("entities", "entity_aliases", "entity_mentions", "relations"):
        assert (await clean_db.execute_fetchall(f"SELECT COUNT(*) FROM {table}"))[0][0] == 0


@pytest.mark.asyncio
async def test_a_dedup_skipped_store_still_declares_against_the_existing_row(clean_db):
    first = await _store("the same text", None)
    second = await _store("the same text", {"entities": [{"name": "Later"}]})
    assert second["result"] == "skipped" and second["id"] == first["id"], second
    names = await _entities(clean_db)
    assert await _mentions(clean_db, names["Later"]) == {f"mem:{first['id']}"}


@pytest.mark.asyncio
async def test_a_rejected_store_declares_nothing(clean_db):
    res = await _store("", {"entities": [{"name": "Orphan"}]})
    assert res["result"] == "rejected" and "associations" not in res
    assert await _entities(clean_db) == {}


# --------------------------------------------------------------------------
# one entity per normalized name, one owner per alias
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_declarations_that_normalize_alike_are_one_entity(clean_db):
    await _store("first", {"entities": [{"name": "MizEye", "aliases": ["ミズアイ"]}]})
    res = await _store("second", {"entities": [{"name": "ＭＩＺＥＹＥ"}, {"name": "ミズアイ"}],
                                  "relations": [{"subject": " mizeye ", "predicate": "is", "object": "MizEye"}]})
    names = await _entities(clean_db)
    assert list(names) == ["MizEye"], names  # the stored name is the first declared form
    assert [e["created"] for e in res["associations"]["entities"]] == [False, False]
    # Resolving the relation's endpoints registered nothing new either.
    (row,) = await _relations(clean_db)
    assert row[:5] == ("entity", names["MizEye"], "is", "entity", names["MizEye"])


@pytest.mark.asyncio
async def test_an_alias_that_already_names_another_entity_in_the_scope_is_dropped(clean_db):
    await _store("one", {"entities": [{"name": "Kirari", "aliases": ["kr"]}]})
    res = await _store("two", {"entities": [{"name": "Kirari Remover", "aliases": ["kr", "remover"]}]})
    report = res["associations"]
    assert [d["item"] for d in report["dropped"]] == ["entities[0].aliases[0]"]
    assert "another entity" in report["dropped"][0]["reason"]
    names = await _entities(clean_db)
    assert await _aliases(clean_db, names["Kirari"]) == {"kr"}
    assert await _aliases(clean_db, names["Kirari Remover"]) == {"remover"}


@pytest.mark.asyncio
async def test_the_same_alias_can_belong_to_different_agents(clean_db):
    await _store("a", {"entities": [{"name": "Kirari", "aliases": ["kr"]}]}, agent="agent-one")
    res = await _store("b", {"entities": [{"name": "Somebody", "aliases": ["kr"]}]}, agent="agent-two")
    assert res["associations"]["dropped"] == []


@pytest.mark.asyncio
async def test_a_project_declaration_reuses_the_agents_global_entity_and_creates_new_ones_in_the_project(clean_db):
    await _store("global", {"entities": [{"name": "MizEye"}]})
    res = await _store("scoped", {"entities": [{"name": "mizeye"}, {"name": "Local"}]}, project_id="proj")
    entities = res["associations"]["entities"]
    assert [(e["name"], e["created"]) for e in entities] == [("mizeye", False), ("Local", True)]
    rows = await clean_db.execute_fetchall(
        "SELECT name, project_id FROM entities WHERE agent_id = ? ORDER BY id", (AGENT,)
    )
    assert rows == [("MizEye", ""), ("Local", "proj")]


# --------------------------------------------------------------------------
# record endpoints and anchors must be this agent's
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_endpoints_must_exist_in_this_agents_store(clean_db):
    mine = await _store("mine")
    theirs = await _store("theirs", agent="someone-else")
    absent = 999_999
    res = await server.do_declare_associations_boundary(
        AGENT,
        {
            "relations": [
                {"subject": f"mem:{mine['id']}", "predicate": "supersedes", "object": f"mem:{mine['id']}"},
                {"subject": f"mem:{theirs['id']}", "predicate": "supersedes", "object": f"mem:{mine['id']}"},
                {"subject": f"mem:{absent}", "predicate": "supersedes", "object": "Named"},
                {"subject": "Named", "predicate": " ", "object": "Named"},
            ]
        },
    )
    assert res["result"] == "declared"
    assert len(res["relations"]) == 1
    items = [d["item"] for d in res["dropped"]]
    assert items == ["relations[1].subject", "relations[2].subject", "relations[3]"]
    assert "not a record of this agent" in res["dropped"][0]["reason"]
    # The entity in the dropped relation's object was never reached, so nothing was registered for it.
    assert await _entities(clean_db) == {}


@pytest.mark.asyncio
async def test_an_anchor_that_is_not_this_agents_record_is_dropped_and_nothing_is_anchored(clean_db):
    theirs = await _store("theirs", agent="someone-else")
    res = await server.do_declare_associations_boundary(
        AGENT,
        {"entities": [{"name": "Kirari"}], "relations": [{"subject": "Kirari", "predicate": "likes", "object": "tea"}]},
        anchor_ref=f"mem:{theirs['id']}",
    )
    assert res["dropped"][0]["item"] == "anchor_ref"
    names = await _entities(clean_db)
    assert await _mentions(clean_db, names["Kirari"]) == set() and res["mentions"] == 0
    (row,) = await _relations(clean_db)
    assert row[5] == ""


@pytest.mark.asyncio
async def test_declare_associations_anchors_to_an_episode_too(clean_db):
    cur = await clean_db.execute(
        "INSERT INTO episodes (agent_id, summary, keywords) VALUES (?, ?, ?)", (AGENT, "a session", "kw")
    )
    await clean_db.commit()
    res = await server.do_declare_associations_boundary(
        AGENT, {"entities": [{"name": "Kirari"}]}, anchor_ref=f"ep:{cur.lastrowid}"
    )
    names = await _entities(clean_db)
    assert await _mentions(clean_db, names["Kirari"]) == {f"ep:{cur.lastrowid}"} and res["mentions"] == 1


# --------------------------------------------------------------------------
# retraction, bounds, pause
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retract_removes_only_this_agents_relations_and_mentions(clean_db):
    mine = await _store("mine", {"entities": [{"name": "Kirari"}],
                                 "relations": [{"subject": "Kirari", "predicate": "likes", "object": "tea"}]})
    theirs = await _store("theirs", {"entities": [{"name": "Kirari"}],
                                     "relations": [{"subject": "Kirari", "predicate": "likes", "object": "tea"}]},
                          agent="someone-else")
    my_entity = (await _entities(clean_db))["Kirari"]
    their_entity = (await _entities(clean_db, "someone-else"))["Kirari"]
    res = await server.do_declare_associations_boundary(
        AGENT,
        retract={
            "relations": [mine["associations"]["relations"][0], theirs["associations"]["relations"][0], "x"],
            "mentions": [
                {"entity": my_entity, "ref": f"mem:{mine['id']}"},
                {"entity": their_entity, "ref": f"mem:{theirs['id']}"},
                {"entity": "bad"},
            ],
        },
    )
    assert res["retracted"] == {"relations": 1, "mentions": 1}
    assert [d["item"] for d in res["dropped"]] == ["retract.relations[2]", "retract.mentions[2]"]
    assert await _relations(clean_db) == []
    assert len(await _relations(clean_db, "someone-else")) == 1
    assert await _mentions(clean_db, my_entity) == set()
    assert await _mentions(clean_db, their_entity) == {f"mem:{theirs['id']}"}


@pytest.mark.asyncio
async def test_a_call_is_bounded_and_says_where_it_cut(clean_db):
    many = [{"name": f"E{i}"} for i in range(associations.MAX_ITEMS + 2)]
    res = await server.do_declare_associations_boundary(AGENT, {"entities": many})
    assert len(res["entities"]) == associations.MAX_ITEMS
    assert [d["item"] for d in res["dropped"]] == [
        f"entities[{associations.MAX_ITEMS}]",
        f"entities[{associations.MAX_ITEMS + 1}]",
    ]
    assert "bound" in res["dropped"][0]["reason"]


@pytest.mark.asyncio
async def test_a_paused_session_declares_nothing(clean_db):
    session.pause_for("declare-pause", True, 60)
    try:
        res = await server.do_declare_associations_boundary(
            AGENT, {"entities": [{"name": "Kirari"}]}, session_key="declare-pause"
        )
    finally:
        session.reset_pauses_for_tests()
    assert res["result"] == "skipped" and res["persisted"] is False, res
    assert await _entities(clean_db) == {}


# --------------------------------------------------------------------------
# through the registered handler — the path an MCP client takes
# --------------------------------------------------------------------------
#
# The MCP argument validator hands an omitted object argument over as {}, not
# None. The boundary functions default to None, so a test that calls them
# directly never sees what a client sends; these go through the handler.


@pytest.mark.asyncio
async def test_a_store_without_associations_declares_nothing_and_says_nothing(clean_db):
    out = await server.registry._handlers["store"]({"agent_id": AGENT, "message": {"content": "plain memory"}})
    assert out["result"] == "stored"
    assert "associations" not in out, out
    assert await _entities(clean_db) == {}


@pytest.mark.asyncio
async def test_a_store_with_associations_still_declares_through_the_handler(clean_db):
    out = await server.registry._handlers["store"](
        {"agent_id": AGENT, "message": {"content": "mizeye note"},
         "associations": {"entities": [{"name": "MizEye"}]}}
    )
    assert out["associations"]["entities"][0]["name"] == "MizEye"
    assert set(await _entities(clean_db)) == {"MizEye"}


@pytest.mark.asyncio
async def test_declare_associations_without_retract_reports_no_retraction(clean_db):
    out = await server.registry._handlers["declare_associations"](
        {"agent_id": AGENT, "associations": {"entities": [{"name": "MizEye"}]}}
    )
    assert out["result"] == "declared" and "retracted" not in out, out
    empty = await server.registry._handlers["declare_associations"]({"agent_id": AGENT})
    assert empty["entities"] == [] and "retracted" not in empty, empty
