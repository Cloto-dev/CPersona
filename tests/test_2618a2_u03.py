"""Associations: re-channelling carries record-to-record relations, and traverse
lists a relation's anchor only when the call could read the anchored record.

bug-814: migrate_channel_axis raised KeyError (and rolled the whole migration
back) when a moved memory carried a relation whose two ends are records and no
entity had been carried into that channel first.

bug-920: traverse returned a relation's anchor_ref without the readability
filter its mention refs go through, so a source-, project- or channel-scoped
call could list the ref of a record that the same call could not read.
"""

import pytest
import pytest_asyncio

from cpersona import associations, maintenance_handlers, server, session
from cpersona import memory_handlers as M
from cpersona.database import get_db

AGENT = "u03-assoc"
TABLES = ("memories", "episodes", "profiles", "pending_memory_tasks", "entities",
          "entity_aliases", "entity_mentions", "relations")


@pytest_asyncio.fixture(autouse=True)
async def db():
    session.reset_pauses_for_tests()
    conn = await get_db()
    for table in TABLES:
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()
    yield conn
    session.reset_pauses_for_tests()


# ---- bug-814 ------------------------------------------------------------------------

async def _legacy(content: str, sid: str) -> int:
    r = await server.do_store_boundary(AGENT, content=content, channel="discord",
                                       metadata={"session_id": sid})
    assert r["result"] == "stored", r
    return r["id"]


async def _state(db):
    mems = await db.execute_fetchall(
        "SELECT id, channel FROM memories WHERE agent_id = ? ORDER BY id", (AGENT,))
    rels = await db.execute_fetchall(
        "SELECT channel, subject_kind, subject_id, object_kind, object_id, anchor_ref FROM relations "
        "WHERE agent_id = ? ORDER BY id", (AGENT,))
    return [tuple(m) for m in mems], [tuple(r) for r in rels]


@pytest.mark.asyncio
@pytest.mark.parametrize("anchored", [False, True])
async def test_bug_814_mem_to_mem_relation_follows_its_records(db, anchored):
    m1 = await _legacy("first event", "1234567:42:0")
    m2 = await _legacy("second event", "1234567:42:1")
    anchor = f"mem:{m1}" if anchored else ""
    report = await associations.declare(
        AGENT, {"relations": [{"subject": f"mem:{m1}", "predicate": "follows", "object": f"mem:{m2}"}]},
        channel="discord", anchor_ref=anchor)
    assert report["relations"] and not report["dropped"], report

    out = await maintenance_handlers.do_migrate_channel_axis(agent_id=AGENT, dry_run=False)

    assert out["migrated"] == 2, out
    mems, rels = await _state(db)
    assert mems == [(m1, "1234567"), (m2, "1234567")]
    # The relation moved with its records, ends and anchor intact, and no copy stayed behind.
    assert rels == [("1234567", "mem", m1, "mem", m2, anchor)]


@pytest.mark.asyncio
async def test_bug_814_mixed_relation_kinds_in_one_channel(db):
    """A record-only relation next to an entity-ended one: both land in the new channel."""
    m1 = await _legacy("first event", "7654321:42:0")
    m2 = await _legacy("second event", "7654321:42:1")
    await associations.declare(
        AGENT, {"relations": [{"subject": f"mem:{m1}", "predicate": "follows", "object": f"mem:{m2}"}]},
        channel="discord")
    await associations.declare(
        AGENT, {"entities": [{"name": "Alice"}],
                "relations": [{"subject": "Alice", "predicate": "wrote", "object": f"mem:{m2}"}]},
        channel="discord")

    out = await maintenance_handlers.do_migrate_channel_axis(agent_id=AGENT, dry_run=False)

    assert out["migrated"] == 2, out
    _, rels = await _state(db)
    assert sorted((r[0], r[1], r[3]) for r in rels) == [
        ("7654321", "entity", "mem"), ("7654321", "mem", "mem")]


@pytest.mark.asyncio
async def test_bug_814_globalized_records_take_their_relation_along(db):
    """Unrecoverable rows go to the global channel; their record-to-record relation follows."""
    r1 = await server.do_store_boundary(AGENT, content="no session one", channel="discord")
    r2 = await server.do_store_boundary(AGENT, content="no session two", channel="discord")
    m1, m2 = r1["id"], r2["id"]
    await associations.declare(
        AGENT, {"relations": [{"subject": f"mem:{m1}", "predicate": "follows", "object": f"mem:{m2}"}]},
        channel="discord")

    out = await maintenance_handlers.do_migrate_channel_axis(
        agent_id=AGENT, dry_run=False, globalize_unrecoverable=True)

    assert out["globalized"] == 2, out
    mems, rels = await _state(db)
    assert mems == [(m1, ""), (m2, "")]
    assert rels == [("", "mem", m1, "mem", m2, "")]


# ---- bug-920 ------------------------------------------------------------------------

async def _mem(content: str, source: str = "u-main", **kw) -> str:
    out = await M.do_store(AGENT, {"content": content, "source": {"type": "User", "id": source, "name": source}}, **kw)
    assert out["result"] == "stored", out
    return f"mem:{out['id']}"


async def _relate_on(anchor: str, subject: str, obj: str, **kw) -> int:
    report = await associations.declare(
        AGENT, {"relations": [{"subject": subject, "predicate": "knows", "object": obj}]},
        anchor_ref=anchor, **kw)
    assert not report["dropped"], report
    return report["relations"][0]


def _anchors(out: dict) -> dict[int, str | None]:
    return {r["id"]: r.get("anchor_ref") for r in out["relations"]}


@pytest.mark.asyncio
async def test_bug_920_anchor_from_another_source_is_not_listed():
    theirs = await _mem("alice and bob met", source="u-111")
    rel = await _relate_on(theirs, "Alice", "Bob")

    other = await associations.traverse(AGENT, "Alice", source_id="u-222")
    # The relation itself stays; only the ref of the unreadable record goes.
    assert rel in _anchors(other), other
    assert _anchors(other)[rel] is None, f"listed a record of another source: {other}"
    assert "mentions" not in {e["name"]: e for e in other["entities"]}["Alice"]

    own = await associations.traverse(AGENT, "Alice", source_id="u-111")
    assert _anchors(own)[rel] == theirs, own
    unfiltered = await associations.traverse(AGENT, "Alice")
    assert _anchors(unfiltered)[rel] == theirs, unfiltered


@pytest.mark.asyncio
async def test_bug_920_anchor_in_another_project_is_not_listed():
    in_q = await _mem("carol bought the shop", project_id="Q")
    in_p = await _mem("carol sold it again", project_id="P")
    rel_q = await _relate_on(in_q, "Carol", "Dave", project_id="P")
    rel_p = await _relate_on(in_p, "Carol", "Erin", project_id="P")

    out = await associations.traverse(AGENT, "Carol", project_id="P")
    assert _anchors(out) == {rel_q: None, rel_p: in_p}, out
    in_q_view = await associations.traverse(AGENT, "Carol")
    assert _anchors(in_q_view) == {rel_q: in_q, rel_p: in_p}, in_q_view


@pytest.mark.asyncio
async def test_bug_920_anchor_in_another_channel_is_not_listed():
    in_y = await _mem("frank met gina", channel="y")
    rel = await _relate_on(in_y, "Frank", "Gina", channel="x")

    out = await associations.traverse(AGENT, "Frank", channel="x")
    assert _anchors(out) == {rel: None}, out
    whole = await associations.traverse(AGENT, "Frank")
    assert _anchors(whole) == {rel: in_y}, whole


@pytest.mark.asyncio
async def test_bug_920_episode_anchor_follows_the_episode_rule():
    """An episode is readable only without a source filter, or with a channel (as recall)."""
    out = await M.do_archive_episode(AGENT, [{"role": "user", "content": "planning"}],
                                     summary="hana planning", keywords="", channel="c")
    ep = f"ep:{out['episode_id']}"
    rel = await _relate_on(ep, "Hana", "Ivan", channel="c")

    by_source = await associations.traverse(AGENT, "Hana", source_id="u-main")
    assert _anchors(by_source) == {rel: None}, by_source
    with_channel = await associations.traverse(AGENT, "Hana", source_id="u-main", channel="c")
    assert _anchors(with_channel) == {rel: ep}, with_channel
    whole = await associations.traverse(AGENT, "Hana")
    assert _anchors(whole) == {rel: ep}, whole
