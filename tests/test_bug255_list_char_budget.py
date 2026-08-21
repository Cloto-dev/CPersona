"""bug-255: list_memories / list_episodes are bounded in characters, not just in rows.

Both tools returned every selected row's text verbatim, bounded only by the row
caps (_clamp_limit 500 / 200). That was a real bound while the write cap was
2,000 characters; the cap was later raised to 16,000 and the worst cases grew
with it (8M characters for memories, 6.4M across the episodes' two text
columns) with nothing in these tools changing. These tests pin the budget, the shape a row
takes when the budget reaches it, and the invariant that a response under budget
looks exactly as it always did.
"""

import pytest
import pytest_asyncio

from cpersona import admin_handlers, config
from cpersona.database import get_db

AGENT = "bug255"


@pytest_asyncio.fixture
async def clean_db():
    db = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()
    return db


async def _seed_memories(db, contents):
    """Insert rows with explicit descending created_at, so list order is decided."""
    for index, content in enumerate(contents):
        await db.execute(
            "INSERT INTO memories (agent_id, content, timestamp, created_at) VALUES (?, ?, '', ?)",
            (AGENT, content, f"2026-08-{20 - index:02d} 00:00:00"),
        )
    await db.commit()


async def _seed_episodes(db, pairs):
    for index, (summary, keywords) in enumerate(pairs):
        await db.execute(
            "INSERT INTO episodes (agent_id, summary, keywords, created_at) VALUES (?, ?, ?, ?)",
            (AGENT, summary, keywords, f"2026-08-{20 - index:02d} 00:00:00"),
        )
    await db.commit()


def test_budgets_are_pinned_at_the_old_worst_case():
    """The numbers are derived, and derived from the OLD write cap on purpose."""
    assert admin_handlers.LIST_MEMORIES_MAX_CHARS == 500 * 2000
    assert admin_handlers.LIST_EPISODES_MAX_CHARS == 200 * 2 * 2000
    # Not derived from MAX_CONTENT_LENGTH: at today's cap the row limit alone
    # admits 8x each budget, which is the hole bug-255 closed. A budget computed
    # from the write cap would fail here — as it should.
    assert admin_handlers.LIST_MEMORIES_MAX_CHARS < 500 * config.MAX_CONTENT_LENGTH
    assert admin_handlers.LIST_EPISODES_MAX_CHARS < 200 * 2 * config.MAX_CONTENT_LENGTH


@pytest.mark.asyncio
async def test_under_budget_response_is_unchanged(clean_db):
    await _seed_memories(clean_db, ["short one", "short two"])

    result = await admin_handlers.do_list_memories(AGENT, 10)

    assert result["count"] == 2
    assert [m["content"] for m in result["memories"]] == ["short one", "short two"]
    assert "budget_chars" not in result
    for memory in result["memories"]:
        assert "content_truncated" not in memory
        assert "content_len" not in memory
        assert "ref" not in memory


@pytest.mark.asyncio
async def test_memories_past_the_budget_degrade_to_a_marked_prefix(clean_db, monkeypatch):
    monkeypatch.setattr(admin_handlers, "LIST_MEMORIES_MAX_CHARS", 1000)
    monkeypatch.setattr(config, "RECALL_PREVIEW_CHARS", 50)
    await _seed_memories(clean_db, ["a" * 800, "b" * 800, "c" * 800])

    result = await admin_handlers.do_list_memories(AGENT, 10)

    # Nothing is dropped: a listing that returns fewer rows than it found is
    # indistinguishable from a smaller corpus.
    assert result["count"] == 3
    assert result["budget_chars"] == 1000
    newest, second, third = result["memories"]

    # The first row is admitted whole (it fit, and one row must never become a
    # wall of prefix on its own).
    assert newest["content"] == "a" * 800
    assert "content_truncated" not in newest

    for degraded, letter in ((second, "b"), (third, "c")):
        assert degraded["content"] == letter * 50, "not a pure prefix of the stored text"
        assert degraded["content_len"] == 800, "content_len must be the ORIGINAL length"
        assert degraded["content_truncated"] is True
        assert degraded["ref"] == f"mem:{degraded['id']}"


@pytest.mark.asyncio
async def test_a_row_shorter_than_the_preview_cap_is_never_marked(clean_db, monkeypatch):
    """The markers say the text WAS cut; a row that fits keeps its exact value."""
    monkeypatch.setattr(admin_handlers, "LIST_MEMORIES_MAX_CHARS", 100)
    monkeypatch.setattr(config, "RECALL_PREVIEW_CHARS", 50)
    await _seed_memories(clean_db, ["x" * 200, "tiny"])

    result = await admin_handlers.do_list_memories(AGENT, 10)

    assert result["budget_chars"] == 100
    tiny = result["memories"][1]
    assert tiny["content"] == "tiny"
    assert "content_truncated" not in tiny
    assert "content_len" not in tiny
    assert "ref" not in tiny


@pytest.mark.asyncio
async def test_first_row_is_admitted_whole_even_alone_over_budget(clean_db, monkeypatch):
    monkeypatch.setattr(admin_handlers, "LIST_MEMORIES_MAX_CHARS", 100)
    monkeypatch.setattr(config, "RECALL_PREVIEW_CHARS", 50)
    await _seed_memories(clean_db, ["z" * 5000])

    result = await admin_handlers.do_list_memories(AGENT, 10)

    assert result["memories"][0]["content"] == "z" * 5000
    assert "budget_chars" not in result


@pytest.mark.asyncio
async def test_episodes_budget_spans_summary_and_keywords(clean_db, monkeypatch):
    monkeypatch.setattr(admin_handlers, "LIST_EPISODES_MAX_CHARS", 1000)
    monkeypatch.setattr(config, "RECALL_PREVIEW_CHARS", 50)
    # Each row charges 800 (summary) + 400 (keywords): the second row is over the
    # budget only because keywords count too — the half that used to be ignored.
    await _seed_episodes(clean_db, [("s" * 800, "k" * 400), ("t" * 800, "j" * 400)])

    result = await admin_handlers.do_list_episodes(AGENT, 10)

    assert result["count"] == 2
    assert result["budget_chars"] == 1000
    newest, second = result["episodes"]
    assert newest["summary"] == "s" * 800 and newest["keywords"] == "k" * 400
    assert second["summary"] == "t" * 50
    assert second["summary_len"] == 800
    assert second["summary_truncated"] is True
    assert second["keywords"] == "j" * 50
    assert second["keywords_len"] == 400
    assert second["keywords_truncated"] is True
    assert second["ref"] == f"ep:{second['id']}"


@pytest.mark.asyncio
async def test_episodes_under_budget_are_unchanged(clean_db):
    await _seed_episodes(clean_db, [("a summary", "alpha beta")])

    result = await admin_handlers.do_list_episodes(AGENT, 10)

    episode = result["episodes"][0]
    assert episode["summary"] == "a summary"
    assert episode["keywords"] == "alpha beta"
    assert "budget_chars" not in result
    assert not [k for k in episode if k.endswith(("_truncated", "_len"))]


@pytest.mark.asyncio
async def test_preview_opt_out_disables_the_budget(clean_db, monkeypatch):
    """RECALL_PREVIEW_CHARS=0 is the operator disabling trimming; degradation to a
    disabled tier would be deletion, so the budget stands down with it."""
    monkeypatch.setattr(admin_handlers, "LIST_MEMORIES_MAX_CHARS", 100)
    monkeypatch.setattr(admin_handlers, "LIST_EPISODES_MAX_CHARS", 100)
    monkeypatch.setattr(config, "RECALL_PREVIEW_CHARS", 0)
    await _seed_memories(clean_db, ["m" * 500, "n" * 500])
    await _seed_episodes(clean_db, [("p" * 500, ""), ("q" * 500, "")])

    memories = await admin_handlers.do_list_memories(AGENT, 10)
    episodes = await admin_handlers.do_list_episodes(AGENT, 10)

    assert [m["content"] for m in memories["memories"]] == ["m" * 500, "n" * 500]
    assert [e["summary"] for e in episodes["episodes"]] == ["p" * 500, "q" * 500]
    assert "budget_chars" not in memories
    assert "budget_chars" not in episodes
