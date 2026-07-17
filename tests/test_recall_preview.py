"""Tests for the 2.5.0 recall preview tier (Task #193).

Three layers, each pinned separately so a regression localises:
1. Library: do_recall messages carry a stable `ref` handle (mem:/ep: — episodes
   had NO addressable id in the response before 2.5.0).
2. Boundary: server._apply_preview trims to a PURE prefix with
   content_truncated/content_len markers, honours full_content and the
   CPERSONA_RECALL_PREVIEW_CHARS=0 kill switch, and never touches short rows.
3. get_contents: refs resolve to full rows with the agent ownership predicate;
   malformed / foreign refs fail soft into `missing`.

Design authority: docs/RECALL_PREVIEW_TIER_DESIGN.md.
"""
import contextlib

import pytest

from cpersona import config
from cpersona import memory_handlers as M
from cpersona import server as S
from cpersona.database import get_db
from cpersona.memory_handlers import do_get_contents


# --- layer 1: refs in the do_recall response --------------------------------


class _FakeDB:
    """Answers only the scoring/metadata queries do_recall issues."""

    async def execute_fetchall(self, sql, params=()):
        s = " ".join(sql.split())
        if s.startswith("SELECT MIN(timestamp), MAX(timestamp)"):
            return [("2026-07-15T10:00:00+00:00", "2026-07-15T12:00:00+00:00")]
        if s.startswith("SELECT id, recall_count, last_recalled_at"):
            return []
        if s.startswith("SELECT COUNT(*)"):
            return [(2,)]
        return []

    async def execute(self, sql, params=()):
        return None

    async def commit(self):
        return None


async def _fake_rsf(db, agent_id, query, limit, deep, channel="", exclude_set=None,
                    project_id=None, source_id=""):
    return [
        {"id": 1, "msg_id": "m-1", "content": "memory row content",
         "source": '{"type": "User", "id": "u1", "name": "U"}',
         "timestamp": "2026-07-15T11:00:00+00:00", "_cosine": 0.9, "_rsf_score": 0.9,
         "_rid": ("mem", 1)},
        {"id": 7, "content": "[Episode] an archived episode summary",
         "source": {"System": "episode"}, "timestamp": "2026-07-15T10:30:00+00:00",
         "_resolved": False, "_cosine": 0.8, "_rsf_score": 0.8, "_rid": ("ep", 7)},
    ]


@pytest.mark.asyncio
async def test_do_recall_messages_carry_refs(monkeypatch):
    """Memory rows ref as mem:<db id>, episode rows as ep:<db id> — the episode
    case is the load-bearing one (episodes exposed no id at all before 2.5.0)."""
    fake = _FakeDB()

    @contextlib.asynccontextmanager
    async def fake_cm():
        yield fake

    monkeypatch.setattr(M, "connection", fake_cm)
    monkeypatch.setattr(M, "transaction", fake_cm)
    monkeypatch.setattr(M, "_recall_rsf", _fake_rsf)
    monkeypatch.setattr(M, "RECALL_MODE", "rsf")

    out = await M.do_recall("agent.pv", "episode summary", limit=5)
    refs = {m.get("ref") for m in out["messages"]}
    assert refs == {"mem:1", "ep:7"}


# --- layer 2: the boundary preview -------------------------------------------


def test_apply_preview_trims_to_pure_prefix(monkeypatch):
    monkeypatch.setattr(config, "RECALL_PREVIEW_CHARS", 10)
    long = "0123456789ABCDEF"
    result = {"messages": [{"ref": "mem:1", "content": long}]}
    out = S._apply_preview(result)
    m = out["messages"][0]
    assert m["content"] == long[:10]          # pure prefix — no ellipsis marker,
    assert not m["content"].endswith("…")     # or exclude_contents dedup breaks
    assert m["content_truncated"] is True
    assert m["content_len"] == len(long)


def test_apply_preview_leaves_short_rows_unmarked(monkeypatch):
    monkeypatch.setattr(config, "RECALL_PREVIEW_CHARS", 10)
    result = {"messages": [{"ref": "mem:1", "content": "short"}]}
    m = S._apply_preview(result)["messages"][0]
    assert m["content"] == "short"
    assert "content_truncated" not in m and "content_len" not in m


def test_apply_preview_zero_disables(monkeypatch):
    monkeypatch.setattr(config, "RECALL_PREVIEW_CHARS", 0)
    long = "x" * 5000
    m = S._apply_preview({"messages": [{"content": long}]})["messages"][0]
    assert m["content"] == long and "content_truncated" not in m


@pytest.mark.asyncio
async def test_recall_boundary_previews_by_default_and_full_content_bypasses(monkeypatch):
    monkeypatch.setattr(config, "RECALL_PREVIEW_CHARS", 10)
    long = "0123456789ABCDEF"

    async def fake_do_recall(agent_id, query, limit, **kw):
        return {"messages": [{"ref": "mem:1", "content": long}]}

    monkeypatch.setattr(S, "do_recall", fake_do_recall)

    out = await S.do_recall_boundary("a", "q", 5, False, "", [], None, "")
    assert out["messages"][0]["content"] == long[:10]

    out = await S.do_recall_boundary("a", "q", 5, False, "", [], None, "", full_content=True)
    assert out["messages"][0]["content"] == long
    assert "content_truncated" not in out["messages"][0]


def test_recall_tool_schemas_declare_preview_contract():
    tools = {t.name: t for t in S.registry._tools}
    for name in ("recall", "recall_with_context"):
        props = tools[name].inputSchema["properties"]
        assert props["full_content"]["type"] == "boolean", f"{name}: full_content missing"
        assert props["full_content"]["default"] is False
    gc = tools["get_contents"].inputSchema["properties"]
    assert gc["refs"]["maxItems"] == 20
    assert tools["get_contents"].annotations.readOnlyHint is True


# --- layer 3: get_contents ----------------------------------------------------


_seed_seq = iter(range(1, 10_000))


async def _seed_rows():
    """Insert one long memory + one episode under a test-unique agent. Each call
    seeds distinct msg_id/content so the (agent_id, project_id, msg_id) and
    content dedup UNIQUE indexes never collide across tests."""
    db = await get_db()
    n = next(_seed_seq)
    long_content = f"preview tier seed {n} " * 70  # >1000 chars, > default 500 cap
    cur = await db.execute(
        "INSERT INTO memories (agent_id, msg_id, content, source, timestamp) "
        "VALUES ('pv-agent', ?, ?, '{\"type\": \"User\", \"id\": \"u\", \"name\": \"n\"}', "
        "'2026-07-15T11:00:00+00:00')",
        (f"pv-m{n}", long_content),
    )
    mem_id = cur.lastrowid
    cur = await db.execute(
        "INSERT INTO episodes (agent_id, summary, start_time, resolved) "
        "VALUES ('pv-agent', 'a seeded episode summary', '2026-07-15T10:00:00+00:00', 1)"
    )
    ep_id = cur.lastrowid
    await db.commit()
    return long_content, f"pv-m{n}", mem_id, ep_id


@pytest.mark.asyncio
async def test_get_contents_resolves_full_rows():
    long_content, msg_id, mem_id, ep_id = await _seed_rows()
    out = await do_get_contents("pv-agent", [f"mem:{mem_id}", f"ep:{ep_id}"])
    assert out["missing"] == [] and out["count"] == 2
    by_ref = {i["ref"]: i for i in out["items"]}
    mem = by_ref[f"mem:{mem_id}"]
    assert mem["content"] == long_content          # full, untrimmed
    assert mem["id"] == msg_id
    assert mem["source"]["type"] == "User"
    ep = by_ref[f"ep:{ep_id}"]
    assert ep["content"] == "[Episode] a seeded episode summary"
    assert ep["resolved"] is True and ep["source"] == {"System": "episode"}


@pytest.mark.asyncio
async def test_get_contents_enforces_ownership_and_fails_soft():
    _, _, mem_id, ep_id = await _seed_rows()
    out = await do_get_contents(
        "other-agent",
        [f"mem:{mem_id}", f"ep:{ep_id}", "bogus", "mem:abc", "mem:-1", "profile:1"],
    )
    # Another agent's rows and every malformed ref land in `missing`; the batch
    # itself never aborts (fail-soft).
    assert out["count"] == 0 and out["items"] == []
    assert set(out["missing"]) == {
        f"mem:{mem_id}", f"ep:{ep_id}", "bogus", "mem:abc", "mem:-1", "profile:1",
    }


@pytest.mark.asyncio
async def test_get_contents_input_validation():
    assert "error" in await do_get_contents("", ["mem:1"])
    assert "error" in await do_get_contents("pv-agent", [])
    assert "error" in await do_get_contents("pv-agent", "mem:1")  # not a list
    too_many = [f"mem:{i}" for i in range(1, 23)]
    out = await do_get_contents("pv-agent", too_many)
    assert "error" in out and "max 20" in out["error"]
