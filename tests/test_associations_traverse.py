"""traverse — a declared entity's neighbourhood, as a graph (docs/ASSOCIATIVE_MEMORY_DESIGN.md §4).

The graph query tool: aliases, relations to the hop bound in either direction,
the entities reached, and the refs of the readable records that mention each.
Deterministic in order, bounded by `limit`, no record text.
"""

import pytest
import pytest_asyncio

from cpersona import associations, server, session
from cpersona import memory_handlers as M
from cpersona.database import get_db

AGENT = "assoc-traverse"
OTHER = "assoc-traverse-other"


@pytest_asyncio.fixture(autouse=True)
async def clean_db():
    session.reset_pauses_for_tests()
    db = await get_db()
    for table in ("memories", "episodes", "entities", "entity_aliases", "entity_mentions", "relations"):
        await db.execute(f"DELETE FROM {table}")
    await db.execute("DELETE FROM sqlite_sequence WHERE name IN ('memories','episodes','entities','relations')")
    await db.commit()
    yield db
    session.reset_pauses_for_tests()


async def _mem(content: str, agent: str = AGENT, source: str = "u-main", **kw) -> str:
    out = await M.do_store(agent, {"content": content, "source": {"type": "User", "id": source, "name": source}}, **kw)
    assert out["result"] == "stored", out
    return f"mem:{out['id']}"


async def _declare(agent: str = AGENT, anchor: str = "", **graph) -> dict:
    report = await associations.declare(agent, graph, anchor_ref=anchor)
    assert not report["dropped"], report
    return report


async def _relate(subject: str, predicate: str, obj: str, declared_at: str = "", agent: str = AGENT, **kw) -> int:
    report = await associations.declare(
        agent, {"relations": [{"subject": subject, "predicate": predicate, "object": obj}]}, **kw
    )
    assert not report["dropped"], report
    rel_id = report["relations"][0]
    if declared_at:
        db = await get_db()
        await db.execute("UPDATE relations SET declared_at = ? WHERE id = ?", (declared_at, rel_id))
        await db.commit()
    return rel_id


def _by_name(out: dict) -> dict[str, dict]:
    return {e["name"]: e for e in out["entities"]}


async def _seed_chain() -> dict:
    """MizEye (alias ミズアイ) <-maintains- Kirari -member_of-> MIZPRISM -based_in-> Tokyo."""
    refs = {"eye": await _mem("mizeye shipped"), "kirari": await _mem("kirari wrote the viewer"),
            "prism": await _mem("mizprism brand notes"), "tokyo": await _mem("tokyo office")}
    await _declare(entities=[{"name": "MizEye", "aliases": ["ミズアイ", "miz-eye"]}], anchor=refs["eye"])
    await _declare(entities=[{"name": "Kirari"}], anchor=refs["kirari"])
    await _declare(entities=[{"name": "MIZPRISM"}], anchor=refs["prism"])
    await _declare(entities=[{"name": "Tokyo"}], anchor=refs["tokyo"])
    rels = {
        "maintains": await _relate("Kirari", "maintains", "MizEye", declared_at="2026-01-01T00:00:00+00:00"),
        "member_of": await _relate("Kirari", "member_of", "MIZPRISM", declared_at="2026-02-01T00:00:00+00:00"),
        "based_in": await _relate("MIZPRISM", "based_in", "Tokyo", declared_at="2026-03-01T00:00:00+00:00"),
    }
    return {"refs": refs, "rels": rels}


@pytest.mark.asyncio
async def test_the_neighbourhood_to_the_hop_bound():
    seeded = await _seed_chain()
    out = await associations.traverse(AGENT, "ミズアイ", max_hops=2, limit=20)
    names = _by_name(out)
    assert [e["name"] for e in out["entities"]] == ["MizEye", "Kirari", "MIZPRISM"]
    assert [e["hops"] for e in out["entities"]] == [0, 1, 2]
    assert names["MizEye"]["aliases"] == ["miz-eye", "ミズアイ"]
    assert names["Kirari"]["mentions"] == [seeded["refs"]["kirari"]]
    assert "Tokyo" not in names
    assert out.get("bounds") == {"omitted": ["max_hops"]}
    # Relations between returned entities, newest first; ends are entity ids.
    ids = {e["name"]: e["id"] for e in out["entities"]}
    assert [(r["subject"], r["predicate"], r["object"]) for r in out["relations"]] == [
        (ids["Kirari"], "member_of", ids["MIZPRISM"]),
        (ids["Kirari"], "maintains", ids["MizEye"]),
    ]
    assert [r["id"] for r in out["relations"]] == [seeded["rels"]["member_of"], seeded["rels"]["maintains"]]
    whole = await associations.traverse(AGENT, "MizEye", max_hops=3, limit=20)
    assert [e["name"] for e in whole["entities"]] == ["MizEye", "Kirari", "MIZPRISM", "Tokyo"]
    assert "bounds" not in whole
    alone = await associations.traverse(AGENT, "mizeye", max_hops=0, limit=20)
    assert [e["name"] for e in alone["entities"]] == ["MizEye"] and alone["relations"] == []
    assert alone.get("bounds") == {"omitted": ["max_hops"]}


@pytest.mark.asyncio
async def test_no_record_text_is_returned():
    await _seed_chain()
    out = await associations.traverse(AGENT, "MizEye", max_hops=3, limit=20)
    text = repr(out)
    for fragment in ("shipped", "wrote the viewer", "brand notes", "office"):
        assert fragment not in text


@pytest.mark.asyncio
async def test_entities_are_ordered_by_hops_then_relation_recency_then_id():
    hub = await _mem("hub note")
    await _declare(entities=[{"name": "Hub"}], anchor=hub)
    await _relate("Hub", "uses", "Old", declared_at="2026-01-01T00:00:00+00:00")
    await _relate("Hub", "uses", "New", declared_at="2026-05-01T00:00:00+00:00")
    await _relate("Hub", "uses", "Mid", declared_at="2026-03-01T00:00:00+00:00")
    await _relate("New", "uses", "Far", declared_at="2026-09-01T00:00:00+00:00")
    out = await associations.traverse(AGENT, "Hub", max_hops=2, limit=20)
    assert [e["name"] for e in out["entities"]] == ["Hub", "New", "Mid", "Old", "Far"]


@pytest.mark.asyncio
async def test_limit_bounds_entities_and_mentions_and_says_so():
    hub = await _mem("hub note")
    await _declare(entities=[{"name": "Hub"}], anchor=hub)
    for i in range(4):
        await _relate("Hub", "uses", f"Tool{i}", declared_at=f"2026-0{i + 1}-01T00:00:00+00:00")
    refs = [await _mem(f"tool three note {i}") for i in range(5)]
    for ref in refs:
        await _declare(entities=[{"name": "Tool3"}], anchor=ref)
    out = await associations.traverse(AGENT, "Hub", max_hops=1, limit=3)
    assert [e["name"] for e in out["entities"]] == ["Hub", "Tool3", "Tool2"]
    assert out["entities_omitted"] == 2
    tool3 = _by_name(out)["Tool3"]
    assert tool3["mentions"] == refs[:3] and tool3.get("mentions_omitted") == 2
    assert out.get("bounds") == {"omitted": ["limit"]}
    ids = {e["id"] for e in out["entities"]}
    assert all(r["subject"] in ids and r["object"] in ids for r in out["relations"])
    assert len(out["relations"]) == 2


@pytest.mark.asyncio
async def test_an_unknown_name_says_so():
    await _seed_chain()
    out = await associations.traverse(AGENT, "Nobody", max_hops=2, limit=5)
    assert out["entities"] == [] and out.get("reason") == "no_such_entity"
    blank = await associations.traverse(AGENT, "   ", max_hops=2, limit=5)
    assert blank.get("reason") == "no_such_entity"


@pytest.mark.asyncio
async def test_isolation_agent_project_and_readable_records():
    """Another agent's same-named entity is invisible; a project's relation is followed
    only where the project is read; a record the call could not read is not listed."""
    await _seed_chain()
    await _mem("their mizeye", agent=OTHER)
    await associations.declare(OTHER, {"entities": [{"name": "MizEye"}],
                                       "relations": [{"subject": "Stranger", "predicate": "owns", "object": "MizEye"}]})
    await _relate("MizEye", "shown_at", "Expo", project_id="p1")
    hidden = await _mem("mizeye in project two", project_id="p2")
    other_user = await _mem("mizeye from someone else", source="u-other")
    await _declare(entities=[{"name": "MizEye"}], anchor=hidden)
    await _declare(entities=[{"name": "MizEye"}], anchor=other_user)

    out = await associations.traverse(AGENT, "MizEye", max_hops=1, limit=20, project_id="")
    starts = [e for e in out["entities"] if e["hops"] == 0]
    assert len(starts) == 1, f"another agent's entity became a start: {starts}"
    names = _by_name(out)
    assert "Stranger" not in names and "Expo" not in names
    assert hidden not in names["MizEye"]["mentions"]
    in_p1 = await associations.traverse(AGENT, "MizEye", max_hops=1, limit=20, project_id="p1")
    assert "Expo" in _by_name(in_p1)
    unfiltered = await associations.traverse(AGENT, "MizEye", max_hops=1, limit=20)
    assert hidden in _by_name(unfiltered)["MizEye"]["mentions"], "the fixture never lists the project-two record"
    by_source = await associations.traverse(AGENT, "MizEye", max_hops=1, limit=20, source_id="u-main")
    assert other_user not in _by_name(by_source)["MizEye"]["mentions"]
    assert other_user in _by_name(unfiltered)["MizEye"]["mentions"]


@pytest.mark.asyncio
async def test_hops_and_limit_are_clamped_to_their_ceilings():
    await _seed_chain()
    out = await associations.traverse(AGENT, "MizEye", max_hops=99, limit=10_000)
    assert out["max_hops"] == associations.TRAVERSE_MAX_HOPS and out["limit"] == associations.TRAVERSE_MAX_LIMIT
    low = await associations.traverse(AGENT, "MizEye", max_hops=-3, limit=0)
    assert low["max_hops"] == 0 and low["limit"] == 1


@pytest.mark.asyncio
async def test_the_tool_answers_through_the_mcp_boundary():
    seeded = await _seed_chain()
    out = await server.do_traverse_boundary(AGENT, "ミズアイ", 1, 5, "", None, "")
    assert [e["name"] for e in out["entities"]] == ["MizEye", "Kirari"]
    assert out["relations"][0]["id"] == seeded["rels"]["maintains"]


async def _episode(summary: str, **kw) -> str:
    out = await M.do_archive_episode(AGENT, [{"role": "user", "content": summary}], summary=summary,
                                     keywords="", **kw)
    assert out.get("episode_id"), out
    return f"ep:{out['episode_id']}"


@pytest.mark.asyncio
async def test_episodes_are_listed_and_counted_like_memories():
    """An episode that mentions the entity is a ref like a memory, and counts toward the cut.

    Episodes are readable only where recall would return one: without a source
    filter, or with a channel.
    """
    mem = await _mem("mizeye shipped")
    eps = [await _episode(f"viewer planning session {i}") for i in range(3)]
    for ref in (mem, *eps):
        await _declare(entities=[{"name": "MizEye"}], anchor=ref)
    whole = await associations.traverse(AGENT, "MizEye", max_hops=0, limit=10)
    # Lowest id first; a memory and an episode can share an id, and then the kind orders them.
    by_id = sorted([mem, *eps], key=lambda r: (int(r.split(":")[1]), r.split(":")[0]))
    assert _by_name(whole)["MizEye"]["mentions"] == by_id
    cut = await associations.traverse(AGENT, "MizEye", max_hops=0, limit=2)
    assert len(_by_name(cut)["MizEye"]["mentions"]) == 2
    assert _by_name(cut)["MizEye"].get("mentions_omitted") == 2
    by_source = await associations.traverse(AGENT, "MizEye", max_hops=0, limit=10, source_id="u-main")
    assert _by_name(by_source)["MizEye"]["mentions"] == [mem]
    assert "mentions_omitted" not in _by_name(by_source)["MizEye"]
    with_channel = await associations.traverse(AGENT, "MizEye", max_hops=0, limit=10, source_id="u-main", channel="c")
    assert set(_by_name(with_channel)["MizEye"]["mentions"]) == {mem, *eps}


def test_the_schema_states_the_bounds_the_library_enforces():
    """The advertised maximum and default are the ones the code applies."""
    import inspect

    props = next(t for t in server.registry._tools if t.name == "traverse").inputSchema["properties"]
    assert props["max_hops"]["maximum"] == associations.TRAVERSE_MAX_HOPS
    assert props["limit"]["maximum"] == associations.TRAVERSE_MAX_LIMIT
    assert props["max_hops"]["minimum"] == 0 and props["limit"]["minimum"] == 1
    boundary = inspect.signature(server.do_traverse_boundary).parameters
    library = inspect.signature(associations.traverse).parameters
    for name in ("max_hops", "limit"):
        assert props[name]["default"] == boundary[name].default == library[name].default, name
