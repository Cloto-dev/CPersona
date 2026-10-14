"""Maintenance repairs must not delete data a reader still needs.

* duplicate_content: a copy in channel B is a duplicate only when a reader of
  channel B can still see the content after the repair -- through a
  channel='' copy, or an older copy in channel B itself. The same text stored
  in two sibling channels is two memories, exactly as the store treats it.
* deep_check short_content: the "trivial content" length counts a CJK
  character as the several Latin characters its meaning takes, so a complete
  Japanese fact of four or five characters is not deleted with "ok".
* the anonymous_source hint names the scoped deep_check call, so following it
  repairs attribution without running every other deep repair.
"""

import re

import pytest
import pytest_asyncio

from cpersona import checks, maintenance_handlers, memory_handlers, server, session
from cpersona.database import get_db

TEXT = "deploy window is friday 18:00 for the payments service"
QUERY = "deploy window friday payments"


@pytest_asyncio.fixture
async def clean_db():
    session.reset_pauses_for_tests()
    db = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()
    return db


async def _recall_contents(agent_id: str, channel: str) -> list[str]:
    res = await server.do_recall_boundary(agent_id, QUERY, 10, False, channel, [], None, "")
    return [m.get("content", "") for m in res.get("messages", [])]


async def _store(agent_id: str, channel: str) -> int:
    r = await server.do_store_boundary(agent_id, None, channel=channel, content=TEXT)
    assert r.get("result") == "stored", r
    return r["id"]


async def _rows(db, agent_id: str) -> list[tuple]:
    return [
        tuple(r)
        for r in await db.execute_fetchall(
            "SELECT id, channel FROM memories WHERE agent_id = ? ORDER BY id", (agent_id,)
        )
    ]


def _dup_issue(health: dict) -> dict | None:
    return next((i for i in health.get("issues", []) if i.get("type") == "duplicate_content"), None)


# ---------------------------------------------------------------------------
# duplicate_content keeps a copy every channel-scoped reader can see
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sibling_channel_copy_survives_duplicate_repair(clean_db, fake_embedding_client):
    agent = "u09.sibling"
    await _store(agent, "chanA")
    b = await _store(agent, "chanB")
    assert TEXT in await _recall_contents(agent, "chanB"), "precondition: chanB reader sees it"

    health = await maintenance_handlers.do_check_health(agent, fix=True, checks=["duplicate_content"])

    rows = await _rows(clean_db, agent)
    assert (b, "chanB") in rows, f"chanB's only visible copy was deleted: rows={rows} health={health}"
    assert TEXT in await _recall_contents(agent, "chanB")
    assert TEXT in await _recall_contents(agent, "chanA")


@pytest.mark.asyncio
async def test_store_and_repair_agree_on_what_a_duplicate_is(clean_db, fake_embedding_client):
    """The store accepts the same text in two sibling channels as two memories,
    so the repair must not report that pair as a duplicate either."""
    agent = "u09.agree"
    await _store(agent, "chanA")
    await _store(agent, "chanB")

    dry = await maintenance_handlers.do_check_health(agent, fix=False, checks=["duplicate_content"])
    assert _dup_issue(dry) is None, dry


@pytest.mark.asyncio
async def test_channel_copies_covered_by_a_shared_row_are_still_repaired(clean_db, fake_embedding_client):
    """Control: when a channel='' copy exists, every channel reader keeps seeing
    the content through it, so the channel copies are true duplicates."""
    agent = "u09.covered"
    a = await _store(agent, "chanA")
    b = await _store(agent, "chanB")
    # A shared-channel write probes only channel '', so it lands beside the two
    # channel copies -- the shape the repair exists to collapse.
    shared = await _store(agent, "")

    dry = await maintenance_handlers.do_check_health(agent, fix=False, checks=["duplicate_content"])
    issue = _dup_issue(dry)
    assert issue is not None, dry
    assert (issue["groups"], issue["total_extra"], issue["repairable"]) == (1, 2, 2), issue

    await maintenance_handlers.do_check_health(agent, fix=True, checks=["duplicate_content"])
    rows = await _rows(clean_db, agent)
    assert rows == [(shared, "")], rows
    assert a not in {r[0] for r in rows} and b not in {r[0] for r in rows}
    assert TEXT in await _recall_contents(agent, "chanB")


@pytest.mark.asyncio
async def test_locked_covered_copy_counts_as_extra_but_not_repairable(clean_db):
    agent = "u09.locked"
    await clean_db.execute(
        "INSERT INTO memories (agent_id, channel, content, source, timestamp, locked) "
        "VALUES (?, '', 'locked dup', '{}', '2026-01-01T00:00:00+00:00', 0)",
        (agent,),
    )
    cur = await clean_db.execute(
        "INSERT INTO memories (agent_id, channel, content, source, timestamp, locked) "
        "VALUES (?, 'X', 'locked dup', '{}', '2026-01-01T00:00:00+00:00', 1)",
        (agent,),
    )
    await clean_db.commit()
    issues = await checks.check_duplicate_content(clean_db, agent, fix=True)
    assert issues == [
        {"type": "duplicate_content", "groups": 1, "total_extra": 1, "repairable": 0}
    ], issues
    assert cur.lastrowid in {r[0] for r in await _rows(clean_db, agent)}


@pytest.mark.asyncio
async def test_same_channel_copies_collapse_to_the_oldest(clean_db):
    """A corpus that predates the exact-bucket UNIQUE index can hold two copies in
    one channel; the newer one is invisible to nobody and is still removed."""
    agent = "u09.samechan"
    await clean_db.execute("DROP INDEX IF EXISTS idx_memories_dedup_content")
    try:
        ids = []
        for _ in range(2):
            cur = await clean_db.execute(
                "INSERT INTO memories (agent_id, channel, content, source, timestamp) "
                "VALUES (?, 'X', 'same bucket twice', '{}', '2026-01-01T00:00:00+00:00')",
                (agent,),
            )
            ids.append(cur.lastrowid)
        await clean_db.commit()
        issues = await checks.check_duplicate_content(clean_db, agent, fix=True)
        assert issues == [
            {"type": "duplicate_content", "groups": 1, "total_extra": 1, "repairable": 1}
        ], issues
        assert await _rows(clean_db, agent) == [(ids[0], "X")]
    finally:
        await clean_db.execute("DELETE FROM memories WHERE agent_id = ?", (agent,))
        await clean_db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_dedup_content "
            "ON memories(agent_id, project_id, channel, content)"
        )
        await clean_db.commit()


# ---------------------------------------------------------------------------
# the anonymous_source hint names the scoped call
# ---------------------------------------------------------------------------


def _checks_named_by(hint: str) -> list[str]:
    """Follow the hint literally: the checks it names, or [] (every check) when
    it names none -- which is what deep_check runs when given no checks."""
    m = re.search(r"checks=\[([^\]]*)\]", hint)
    return re.findall(r"'([^']+)'", m.group(1)) if m else []


@pytest.mark.asyncio
async def test_following_the_anonymous_source_hint_deletes_nothing_else(clean_db):
    agent = "u09.anon"
    for content, src in [
        ("[Alice] I prefer tea in the morning", {"type": "User", "id": "", "name": ""}),
        ("Rust", {"type": "User", "id": "u1", "name": "Bob"}),
    ]:
        r = await memory_handlers.do_store(agent, {"content": content, "source": src})
        assert r.get("result") == "stored", r

    health = await maintenance_handlers.do_check_health(agent, checks=["anonymous_source"])
    issue = next(i for i in health["issues"] if i.get("type") == "anonymous_source")
    named = _checks_named_by(issue["hint"])

    result = await maintenance_handlers.do_deep_check(agent, fix=True, checks=named)

    assert result["results"]["anonymous_source"].get("fixed") == 1, result
    remaining = [r[0] for r in await clean_db.execute_fetchall(
        "SELECT content FROM memories WHERE agent_id = ?", (agent,)
    )]
    assert "Rust" in remaining, (
        f"following the hint ({issue['hint']!r} -> checks={named}) deleted an "
        f"unrelated short memory: remaining={remaining}"
    )


def test_deep_check_description_says_the_short_content_repair_deletes():
    desc = next(t.description for t in server.registry._tools if t.name == "deep_check")
    assert re.search(r"short_content\W[^.]*\bdeletes\b", desc), desc


# ---------------------------------------------------------------------------
# short_content does not delete complete CJK facts
# ---------------------------------------------------------------------------

CJK_FACTS = ["妻は看護師", "東京在住", "締切は金曜", "花粉症", "犬が好き"]
TRIVIAL = ["ok", "hmm", "  yes "]


@pytest.mark.asyncio
async def test_short_content_repair_keeps_short_cjk_facts(clean_db):
    agent = "u09.cjk"
    for t in [*CJK_FACTS, "wife is a nurse", *TRIVIAL]:
        r = await memory_handlers.do_store(agent, {"content": t, "source": {"type": "User", "id": "u"}})
        assert r.get("result") == "stored", r

    result = await maintenance_handlers.do_deep_check(agent, fix=True, checks=["short_content"])
    short = result["results"]["short_content"]

    survivors = {r[0] for r in await clean_db.execute_fetchall(
        "SELECT content FROM memories WHERE agent_id = ?", (agent,)
    )}
    lost = [t for t in CJK_FACTS if t not in survivors]
    assert not lost, f"complete CJK facts deleted as trivial: {lost}; result={short}"
    # The repair still works on the noise it exists for, and reports only that.
    assert survivors == {*CJK_FACTS, "wife is a nurse"}, survivors
    assert short["count"] == short["fixed"] == len(TRIVIAL), short
