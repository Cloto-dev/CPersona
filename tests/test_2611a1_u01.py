"""The declared graph travels with the records it is about.

Entities, aliases, mentions and relations name records by `mem:<id>` / `ep:<id>`
refs and carry their own scope columns, and the delete triggers drop every
declaration that names a deleted record. So each path that gives a record a new
identity -- a new id (merge, import, a dedup repair) or a new channel
(migrate_channel_axis) -- has to take the declarations along, or they are lost
or left where no reader of the record can see them.

The graphs below are compared with every id resolved to what it names (an entity
to its name, a record to its owner and text), so a declaration copied with its
old ids, or pointed at the source agent's records, does not compare equal.
"""

import json

import pytest
import pytest_asyncio

from cpersona import admin_handlers, associations, maintenance_handlers, server, session
from cpersona.database import get_db

TABLES = ("memories", "episodes", "profiles", "pending_memory_tasks", "entities",
          "entity_aliases", "entity_mentions", "relations")
SRC = "u01-src"
DST = "u01-dst"


@pytest_asyncio.fixture
async def db():
    session.reset_pauses_for_tests()
    conn = await get_db()
    for table in TABLES:
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()
    yield conn
    session.reset_pauses_for_tests()


async def _record(db, ref: str) -> tuple:
    """(kind, owner, text) of the record a ref names; ('missing', ref) when there is none."""
    parsed = associations.parse_ref(ref)
    if parsed is None:
        return ("missing", ref)
    kind, row_id = parsed
    sql = (
        "SELECT agent_id, content FROM memories WHERE id = ?"
        if kind == "mem"
        else "SELECT agent_id, summary FROM episodes WHERE id = ?"
    )
    rows = await db.execute_fetchall(sql, (row_id,))
    return (kind, rows[0][0], rows[0][1]) if rows else ("missing", ref)


async def _graph(db, agent: str, channel: str | None = None) -> dict:
    """The agent's declarations with every id resolved; `channel` narrows to one scope."""
    where, params = "agent_id = ?", [agent]
    if channel is not None:
        where += " AND channel = ?"
        params.append(channel)
    ents = await db.execute_fetchall(
        f"SELECT id, name, project_id, channel FROM entities WHERE {where} ORDER BY id", params
    )
    names = {e[0]: (e[1], e[2], e[3]) for e in ents}
    aliases = {}
    mentions = []
    for entity_id, (name, project, chan) in names.items():
        rows = await db.execute_fetchall(
            "SELECT alias FROM entity_aliases WHERE entity_id = ? ORDER BY normalized", (entity_id,)
        )
        aliases[(name, project, chan)] = [r[0] for r in rows]
        for (ref,) in await db.execute_fetchall(
            "SELECT ref FROM entity_mentions WHERE entity_id = ?", (entity_id,)
        ):
            mentions.append(((name, project, chan), await _record(db, ref)))

    async def end(kind, row_id):
        if kind == "entity":
            rows = await db.execute_fetchall(
                "SELECT agent_id, name, project_id, channel FROM entities WHERE id = ?", (row_id,)
            )
            return ("entity", *rows[0]) if rows else ("missing entity", row_id)
        return await _record(db, f"{kind}:{row_id}")

    relations = []
    for sk, sid, predicate, ok, oid, anchor, project, chan in await db.execute_fetchall(
        "SELECT subject_kind, subject_id, predicate, object_kind, object_id, anchor_ref, project_id, channel "
        f"FROM relations WHERE {where} ORDER BY id",
        params,
    ):
        relations.append((
            await end(sk, sid), predicate, await end(ok, oid),
            await _record(db, anchor) if anchor else None, project, chan,
        ))
    return {"aliases": aliases, "mentions": sorted(mentions), "relations": sorted(relations)}


def _norm(graph: dict) -> dict:
    """JSON round trip, so tuples and lists compare alike."""
    return json.loads(json.dumps({**graph, "aliases": sorted(map(list, graph["aliases"].items()))}))


def _as_agent(graph: dict, old: str, new: str) -> dict:
    """The same graph with every owner `old` read as `new` (what a carried graph must equal)."""
    return json.loads(json.dumps(_norm(graph)).replace(json.dumps(old), json.dumps(new)))


async def _seed(db, agent: str = SRC) -> dict:
    """Two memories and one episode with entity, record->record and episode-anchored
    declarations, all written through the public boundary."""
    s1 = await server.do_store_boundary(
        agent, {"content": "Alice met Bob in Kyoto to plan the launch"},
        associations={
            "entities": [{"name": "Alice", "aliases": ["A."]}, {"name": "Bob"}],
            "relations": [{"subject": "Alice", "predicate": "works_with", "object": "Bob"}],
        },
    )
    s2 = await server.do_store_boundary(agent, {"content": "Bob wrote the launch checklist"})
    assert s1["result"] == "stored" and s2["result"] == "stored", (s1, s2)
    m1, m2 = s1["id"], s2["id"]
    d = await server.do_declare_associations_boundary(
        agent,
        associations={
            "entities": [{"name": "Bob"}],
            "relations": [{"subject": f"mem:{m2}", "predicate": "follows_up", "object": f"mem:{m1}"}],
        },
        anchor_ref=f"mem:{m2}",
    )
    assert d.get("ok") is True and not d.get("dropped"), d
    await server.do_archive_episode_boundary(
        agent, [{"role": "user", "content": "Kyoto offsite planning"}],
        summary="Planned the Kyoto offsite with Alice", keywords="kyoto offsite",
    )
    (e1,) = [r[0] for r in await db.execute_fetchall("SELECT id FROM episodes WHERE agent_id = ?", (agent,))]
    d2 = await server.do_declare_associations_boundary(
        agent,
        associations={
            "entities": [{"name": "Kyoto"}],
            "relations": [{"subject": "Alice", "predicate": "visited", "object": "Kyoto"}],
        },
        anchor_ref=f"ep:{e1}",
    )
    assert d2.get("ok") is True and not d2.get("dropped"), d2
    graph = await _graph(db, agent)
    # The seed really declared a graph, so an empty one later is a loss, not a no-op.
    assert len(graph["aliases"]) == 3 and len(graph["relations"]) == 3 and len(graph["mentions"]) == 4, graph
    return graph


# ------------------------------------------------------------------------------------
# merge_memories(mode='move') carries the graph instead of deleting it
# ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_move_carries_the_declared_graph_to_the_target(db):
    before = await _seed(db)
    walked_before = await associations.traverse(SRC, "A.")
    r = await admin_handlers.do_merge_memories(SRC, DST, mode="move")
    assert r["ok"] is True and r["merged_memories"] == 2 and r["merged_episodes"] == 1, r

    # Every declaration arrived, naming the target's copies of the records.
    assert _norm(await _graph(db, DST)) == _as_agent(before, SRC, DST)
    # ... and walks as it did: same entities in the same order (declared times kept).
    walked = await associations.traverse(DST, "A.")
    assert [e["name"] for e in walked["entities"]] == [e["name"] for e in walked_before["entities"]], walked
    assert [rel["predicate"] for rel in walked["relations"]] == [
        rel["predicate"] for rel in walked_before["relations"]], walked
    assert {rel["predicate"] for rel in walked["relations"]} == {"works_with", "visited"}, walked
    assert r["associations"] == {"entities": 3, "mentions": 4, "relations": 3}, r


@pytest.mark.asyncio
async def test_merge_move_that_empties_the_source_leaves_no_graph_behind(db):
    await _seed(db)
    r = await admin_handlers.do_merge_memories(SRC, DST, mode="move")
    assert r["left_at_source"] == {"memories": 0, "episodes": 0, "profiles": 0}, r
    left = await _graph(db, SRC)
    assert left == {"aliases": {}, "mentions": [], "relations": []}, left


@pytest.mark.asyncio
async def test_merge_move_that_cannot_carry_an_alias_keeps_and_reports_the_source_graph(db):
    """An alias the target already uses for another entity cannot come along. The
    move then keeps the source's graph rather than delete its only copy, and says so."""
    await server.do_declare_associations_boundary(DST, associations={"entities": [{"name": "Bob"}]})
    s = await server.do_store_boundary(
        SRC, {"content": "Robert signed off the budget"},
        associations={"entities": [{"name": "Robert", "aliases": ["Bob"]}]},
    )
    assert s["associations"]["mentions"] == 1, s
    r = await admin_handlers.do_merge_memories(SRC, DST, mode="move")
    assert r["left_at_source"] == {"memories": 0, "episodes": 0, "profiles": 0, "entities": 1}, r
    assert (await _graph(db, SRC))["aliases"] == {("Robert", "", ""): ["Bob"]}
    assert (await _graph(db, DST))["mentions"] == [
        (("Robert", "", ""), ("mem", DST, "Robert signed off the budget"))]
    assert r["associations"] == {"entities": 1, "mentions": 1, "relations": 0, "not_carried": 1}, r


@pytest.mark.asyncio
async def test_merge_move_keeps_a_relation_whose_endpoint_the_target_already_held(db):
    """The copy pass skips a record the target already holds; a relation naming it
    has to be carried onto the target's equal record, or deleting its anchor drops it."""
    held = await server.do_store_boundary(DST, {"content": "the shared runbook"})
    s1 = await server.do_store_boundary(SRC, {"content": "Kenji revised the runbook"})
    s2 = await server.do_store_boundary(SRC, {"content": "the shared runbook"})
    d = await server.do_declare_associations_boundary(
        SRC,
        associations={"relations": [{"subject": f"mem:{s1['id']}", "predicate": "revises",
                                     "object": f"mem:{s2['id']}"}]},
        anchor_ref=f"mem:{s1['id']}",
    )
    assert d["relations"], d
    r = await admin_handlers.do_merge_memories(SRC, DST, mode="move")
    assert r["merged_memories"] == 1 and r["skipped_memories"] == 1, r
    copied = (await db.execute_fetchall(
        "SELECT id FROM memories WHERE agent_id = ? AND content = ?", (DST, "Kenji revised the runbook")
    ))[0][0]
    rels = await db.execute_fetchall(
        "SELECT subject_kind, subject_id, predicate, object_kind, object_id, anchor_ref FROM relations "
        "WHERE agent_id = ?", (DST,)
    )
    assert rels == [("mem", copied, "revises", "mem", held["id"], f"mem:{copied}")], rels


# ------------------------------------------------------------------------------------
# export / import and merge(copy) carry the graph
# ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_import_round_trip_restores_the_declared_graph(db, tmp_path):
    before = await _seed(db)
    path = str(tmp_path / "backup.jsonl")
    ex = await admin_handlers.do_export_memories(SRC, path, False)
    assert ex["ok"] is True, ex
    assert (await admin_handlers.do_delete_agent_data(SRC))["ok"] is True
    assert await _graph(db, SRC) == {"aliases": {}, "mentions": [], "relations": []}

    imp = await admin_handlers.do_import_memories(path, "", False)
    assert imp["ok"] is True and "errors" not in imp, imp
    assert _norm(await _graph(db, SRC)) == _norm(before)
    walked = await server.do_traverse_boundary(SRC, "A.")
    assert {rel["predicate"] for rel in walked["relations"]} == {"works_with", "visited"}, walked
    assert ex["associations"] == {"entities": 3, "mentions": 4, "relations": 3}, ex
    assert imp["associations"] == {"entities": 3, "mentions": 4, "relations": 3}, imp

    # A second restore of the same file changes nothing.
    again = await admin_handlers.do_import_memories(path, "", False)
    assert again["ok"] is True and again["imported_memories"] == 0, again
    assert _norm(await _graph(db, SRC)) == _norm(before)
    counts = [(await db.execute_fetchall(f"SELECT COUNT(*) FROM {t}"))[0][0]
              for t in ("entities", "entity_aliases", "entity_mentions", "relations")]
    assert counts == [3, 1, 4, 3], counts


@pytest.mark.asyncio
async def test_import_into_another_agent_points_the_graph_at_that_agents_records(db, tmp_path):
    before = await _seed(db)
    path = str(tmp_path / "backup.jsonl")
    await admin_handlers.do_export_memories(SRC, path, False)
    preview = await admin_handlers.do_import_memories(path, DST, True)
    imp = await admin_handlers.do_import_memories(path, DST, False)
    assert imp["ok"] is True and "errors" not in imp, imp
    assert _norm(await _graph(db, DST)) == _as_agent(before, SRC, DST)
    assert _norm(await _graph(db, SRC)) == _norm(before)  # the source is untouched
    assert preview["associations"] == imp["associations"], (preview, imp)


@pytest.mark.asyncio
async def test_merge_copy_carries_the_declared_graph(db):
    before = await _seed(db)
    preview = await admin_handlers.do_merge_memories(SRC, DST, mode="copy", dry_run=True)
    r = await admin_handlers.do_merge_memories(SRC, DST, mode="copy")
    assert r["ok"] is True, r
    assert _norm(await _graph(db, DST)) == _as_agent(before, SRC, DST)
    assert _norm(await _graph(db, SRC)) == _norm(before)
    assert preview["associations"] == r["associations"] == {"entities": 3, "mentions": 4, "relations": 3}, (
        preview, r)


@pytest.mark.asyncio
async def test_a_file_cut_inside_the_graph_section_is_refused(db, tmp_path):
    path = tmp_path / "cut.jsonl"
    lines = [
        {"_type": "header", "version": "cpersona-export/1.1", "memory_count": 1, "episode_count": 0,
         "profile_count": 0, "entity_count": 1, "mention_count": 1, "relation_count": 1},
        {"_type": "memory", "id": 7, "agent_id": SRC, "content": "Kenji leads payments"},
        {"_type": "entity", "id": 3, "agent_id": SRC, "name": "Kenji", "aliases": [],
         "declared_by": "agent", "created_at": "2026-01-01T00:00:00+00:00"},
        {"_type": "mention", "entity": 3, "ref": "mem:7", "declared_by": "agent",
         "created_at": "2026-01-01T00:00:00+00:00"},
    ]
    path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    imp = await admin_handlers.do_import_memories(str(path), "", False)
    assert imp["ok"] is False and "relations" in imp["error"], imp
    left = (await db.execute_fetchall("SELECT COUNT(*) FROM memories"))[0][0]
    assert left == 0  # rolled back, not half-restored


@pytest.mark.asyncio
async def test_malformed_graph_records_are_reported_not_fatal(db, tmp_path):
    """A hand-edited file: what cannot be read as a declaration is reported, and the
    rest of the restore -- records and well-formed declarations -- still lands."""
    path = tmp_path / "edited.jsonl"
    lines = [
        {"_type": "memory", "id": 7, "agent_id": SRC, "content": "Kenji leads payments"},
        {"_type": "entity", "id": 3, "agent_id": SRC, "name": "Kenji", "aliases": [],
         "declared_by": "agent", "created_at": "2026-01-01T00:00:00+00:00"},
        {"_type": "mention", "entity": 3, "ref": "mem:7", "declared_by": "agent", "created_at": ""},
        {"_type": "mention", "entity": [3], "ref": "mem:7", "declared_by": "agent"},
        {"_type": "mention", "entity": 3, "ref": "mem:99", "declared_by": "agent"},
        {"_type": "relation", "agent_id": SRC, "subject_kind": "entity", "subject_id": {"x": 1},
         "predicate": "leads", "object_kind": "mem", "object_id": 7, "declared_by": "agent"},
    ]
    path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    imp = await admin_handlers.do_import_memories(str(path), "", False)
    assert imp["ok"] is True and imp["imported_memories"] == 1, imp
    graph = await _graph(db, SRC)
    assert graph["mentions"] == [(("Kenji", "", ""), ("mem", SRC, "Kenji leads payments"))], graph
    assert imp["associations"] == {"entities": 1, "mentions": 1, "relations": 0}, imp
    assert any("3 declared associations were not restored" in e for e in imp["errors"]), imp


@pytest.mark.asyncio
async def test_a_file_without_graph_records_imports_as_before(db, tmp_path):
    """Files written before the graph was exported carry no graph records and no graph
    counts in their header; they restore exactly as they always did."""
    path = tmp_path / "old.jsonl"
    lines = [
        {"_type": "header", "version": "cpersona-export/1.0", "memory_count": 1, "episode_count": 1,
         "profile_count": 0},
        {"_type": "memory", "id": 1, "agent_id": SRC, "content": "an old backup row"},
        {"_type": "episode", "id": 1, "agent_id": SRC, "summary": "an old backup episode"},
    ]
    path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    imp = await admin_handlers.do_import_memories(str(path), "", False)
    assert imp == {
        "ok": True, "dry_run": False, "imported_memories": 1, "skipped_memories": 0,
        "imported_episodes": 1, "skipped_episodes": 0, "profile_updated": False,
    }, imp


# ------------------------------------------------------------------------------------
# a dedup repair re-points the deleted copy's declarations at the survivor
# ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_content_repair_moves_declarations_to_the_survivor(db):
    text = "Alice owns the billing service"
    a = await server.do_store_boundary(SRC, content=text, channel="ops")
    b = await server.do_store_boundary(SRC, content=text, channel="")
    d = await server.do_declare_associations_boundary(
        SRC,
        associations={"entities": [{"name": "Alice"}, {"name": "billing"}],
                      "relations": [{"subject": "Alice", "predicate": "owns", "object": "billing"}]},
        anchor_ref=f"mem:{a['id']}", channel="ops",
    )
    rel_id = d["relations"][0]
    r = await maintenance_handlers.do_check_health(agent_id=SRC, fix=True, checks=["duplicate_content"])
    assert [i["type"] for i in r["issues"]] == ["duplicate_content"], r
    survivors = await db.execute_fetchall("SELECT id FROM memories WHERE agent_id = ?", (SRC,))
    assert survivors == [(b["id"],)], survivors  # the channel '' copy is the one kept

    graph = await _graph(db, SRC)
    assert sorted(m[0][0] for m in graph["mentions"]) == ["Alice", "billing"], graph
    assert {m[1] for m in graph["mentions"]} == {("mem", SRC, text)}, graph
    rels = await db.execute_fetchall("SELECT id, anchor_ref, channel FROM relations WHERE agent_id = ?", (SRC,))
    assert rels == [(rel_id, f"mem:{b['id']}", "ops")], rels  # same declaration, re-anchored
    walked = await associations.traverse(SRC, "Alice", channel="ops")
    assert walked["relations"] and walked["entities"][0]["mentions"] == [f"mem:{b['id']}"], walked


@pytest.mark.asyncio
async def test_duplicate_content_repair_keeps_declarations_in_the_deleted_rows_channel(db):
    """With no channel '' copy, a same-channel duplicate is covered by the oldest copy in
    its own channel. A copy in a sibling channel survives the repair too, but readers of
    this channel do not see it, so the declarations must not move there."""
    text = "Kenji leads the payments rewrite"
    await db.execute("DROP INDEX IF EXISTS idx_memories_dedup_content")
    try:
        ids = {}
        for name, channel in (("sibling", "b"), ("older", "a"), ("newer", "a")):
            cur = await db.execute(
                "INSERT INTO memories (agent_id, channel, content, source, timestamp) "
                "VALUES (?, ?, ?, '{}', '2026-01-01T00:00:00+00:00')",
                (SRC, channel, text),
            )
            ids[name] = cur.lastrowid
        await db.commit()
        await server.do_declare_associations_boundary(
            SRC,
            associations={"entities": [{"name": "Kenji"}]},
            anchor_ref=f"mem:{ids['newer']}", channel="a",
        )
        r = await maintenance_handlers.do_check_health(agent_id=SRC, fix=True, checks=["duplicate_content"])
        assert [i["type"] for i in r["issues"]] == ["duplicate_content"], r
        survivors = await db.execute_fetchall("SELECT id FROM memories WHERE agent_id = ? ORDER BY id", (SRC,))
        assert survivors == [(ids["sibling"],), (ids["older"],)], survivors
        mentions = await db.execute_fetchall(
            "SELECT m.ref FROM entity_mentions m JOIN entities e ON e.id = m.entity_id WHERE e.agent_id = ?",
            (SRC,),
        )
        assert mentions == [(f"mem:{ids['older']}",)], mentions
    finally:
        await db.execute("DELETE FROM memories WHERE agent_id = ?", (SRC,))
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_dedup_content "
            "ON memories(agent_id, project_id, channel, content)"
        )
        await db.commit()


@pytest.mark.asyncio
async def test_rewrite_collision_moves_declarations_to_the_row_it_collided_with(db):
    """A content-rewriting fixer deletes the row whose cleaned text already exists."""
    await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp) "
        "VALUES (?, '[Memory from Kirari] the deploy is on Friday', '{}', 't')", (SRC,)
    )
    await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp) "
        "VALUES (?, 'the deploy is on Friday', '{}', 't')", (SRC,)
    )
    await db.commit()
    rows = dict(await db.execute_fetchall("SELECT content, id FROM memories WHERE agent_id = ?", (SRC,)))
    annotated, plain = rows["[Memory from Kirari] the deploy is on Friday"], rows["the deploy is on Friday"]
    d = await server.do_declare_associations_boundary(
        SRC,
        associations={"entities": [{"name": "deploy"}],
                      "relations": [{"subject": f"mem:{annotated}", "predicate": "about",
                                     "object": "deploy"}]},
        anchor_ref=f"mem:{annotated}",
    )
    assert d["mentions"] == 1 and d["relations"], d
    await maintenance_handlers.do_check_health(agent_id=SRC, fix=True, checks=["memory_annotation"])
    left = await db.execute_fetchall("SELECT id FROM memories WHERE agent_id = ?", (SRC,))
    assert left == [(plain,)], left  # the collision deleted the annotated row
    mentions = await db.execute_fetchall("SELECT ref FROM entity_mentions")
    rels = await db.execute_fetchall("SELECT subject_kind, subject_id, anchor_ref FROM relations")
    assert mentions == [(f"mem:{plain}",)], mentions
    assert rels == [("mem", plain, f"mem:{plain}")], rels


# ------------------------------------------------------------------------------------
# migrate_channel_axis takes a memory's declarations to its new channel
# ------------------------------------------------------------------------------------


async def _discord_store(content: str, session_id: str | None, decl: dict | None = None) -> int:
    metadata = {"session_id": session_id} if session_id else {}
    r = await server.do_store_boundary(SRC, content=content, channel="discord", metadata=metadata,
                                       associations=decl)
    assert r["result"] == "stored", r
    return r["id"]


@pytest.mark.asyncio
async def test_migration_takes_declarations_to_the_recovered_channel(db):
    m = await _discord_store(
        "Miz Eye shipped the release notes", "1234567:42:0",
        {"entities": [{"name": "Miz Eye", "aliases": ["MizEye bot"]}, {"name": "release notes"}],
         "relations": [{"subject": "Miz Eye", "predicate": "authored", "object": "release notes"}]},
    )
    before, _ = await associations.query_terms(SRC, "what did miz eye do", channel="discord")
    assert before == ["MizEye bot"], before
    r = await maintenance_handlers.do_migrate_channel_axis(agent_id=SRC, dry_run=False)
    assert r["migrated"] == 1, r

    terms, _ = await associations.query_terms(SRC, "what did miz eye do", channel="1234567")
    assert terms == ["MizEye bot"], terms
    walked = await associations.traverse(SRC, "Miz Eye", channel="1234567")
    assert walked["entities"][0]["mentions"] == [f"mem:{m}"], walked
    assert [rel["predicate"] for rel in walked["relations"]] == ["authored"], walked
    # Nothing about the memory is left behind in the channel it left.
    assert await _graph(db, SRC, channel="discord") == {"aliases": {}, "mentions": [], "relations": []}


@pytest.mark.asyncio
async def test_migration_splits_an_entity_whose_records_land_in_different_channels(db):
    decl = {"entities": [{"name": "Kirari"}]}
    m1 = await _discord_store("Kirari opened the incident", "111:9:0", decl)
    m2 = await _discord_store("Kirari closed the incident", "222:9:0", decl)
    episode = await server.do_archive_episode_boundary(
        SRC, [{"role": "user", "content": "incident review"}], summary="Incident review with Kirari",
        channel="discord",
    )
    e1 = episode["episode_id"]
    await server.do_declare_associations_boundary(
        SRC, associations=decl, anchor_ref=f"ep:{e1}", channel="discord"
    )
    await maintenance_handlers.do_migrate_channel_axis(agent_id=SRC, dry_run=False)

    for channel, ref in (("111", f"mem:{m1}"), ("222", f"mem:{m2}"), ("discord", f"ep:{e1}")):
        walked = await associations.traverse(SRC, "Kirari", channel=channel)
        assert [e.get("mentions") for e in walked["entities"]] == [[ref]], (channel, walked)
    # The episode stayed in 'discord', so the entity it mentions stays there too.
    left = await _graph(db, SRC, channel="discord")
    assert left["mentions"] == [(("Kirari", "", "discord"), ("ep", SRC, "Incident review with Kirari"))], left


@pytest.mark.asyncio
async def test_globalized_memories_take_their_declarations_to_the_global_channel(db):
    m = await _discord_store("Hoshi runs the night shift", None, {"entities": [{"name": "Hoshi"}]})
    r = await maintenance_handlers.do_migrate_channel_axis(agent_id=SRC, dry_run=False,
                                                           globalize_unrecoverable=True)
    assert r["globalized"] == 1, r
    walked = await associations.traverse(SRC, "Hoshi", channel="any-channel")
    assert [e.get("mentions") for e in walked["entities"]] == [[f"mem:{m}"]], walked
    assert (await _graph(db, SRC, channel="discord"))["aliases"] == {}
