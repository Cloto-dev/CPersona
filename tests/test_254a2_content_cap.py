"""an earlier decision: the write cap stops cutting tails, and the read side does not follow.

Two halves of one change, and they only make sense together.

The write cap went from 2000 to 16000 characters because the old one destroyed
the most valuable part of a long record. A memory written at length puts its
conclusion first and its hard-won detail last, so a cut at 2000 removed exactly
the part nothing else records — irreversibly, on every write. The 2.6 tree can
split what is stored; it cannot recover what was never stored.

Raising a WRITE bound quietly raises a READ one: ``get_contents`` capped only the
number of refs (20), so an 8x write cap would have carried one response from
~40,000 characters to ~320,000 without a line of that file changing. The budget
added here holds the response where it was.

The tests below are written so that each one fails against the other version of
the code. In particular ``test_the_same_write_loses_its_tail_under_the_old_cap``
is the differential: it re-runs the tail test at the old cap and asserts the
tail is *gone*, so the pair cannot both pass by accident (e.g. if the keyword
retriever were matching on something other than the tail).
"""

import pytest
import pytest_asyncio

from cpersona import admin_handlers, config, memory_handlers, utils
from cpersona._vendored_mcp_common import no_persist
from cpersona.database import get_db

AGENT = "content-cap-agent"
OLD_CAP = 2_000

# A term that appears nowhere else in the corpus and only in the tail of the
# record under test. Long enough to survive trigram tokenisation.
TAIL_MARKER = "quartzfeldspar"


@pytest_asyncio.fixture
async def db():
    no_persist.resume()
    conn = await get_db()
    for table in ("memories", "episodes", "profiles"):
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()
    return conn


def _record_with_tail_marker() -> str:
    """A record whose distinguishing term sits well past the old cap."""
    head = "Conclusion first, as long records are written. " * 60  # ~2.8k chars
    assert len(head) > OLD_CAP, "the marker must land past the old cap"
    return f"{head}\nAnd the detail that cost the most to learn: {TAIL_MARKER}."


async def _store(content: str, agent_id: str = AGENT) -> int:
    result = await memory_handlers.do_store(agent_id, {"content": content})
    assert result["result"] == "stored", result
    return result["id"]


# ---------------------------------------------------------------------------
# Write path — the new ceiling holds, and it is still a ceiling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_write_at_the_new_cap_survives_intact(db):
    text = "M" * config.MAX_CONTENT_LENGTH

    result = await memory_handlers.do_store(AGENT, {"content": text})

    assert result.get("truncated") is not True
    rows = await db.execute_fetchall(
        "SELECT content FROM memories WHERE agent_id = ?", (AGENT,)
    )
    assert rows[0][0] == text


@pytest.mark.asyncio
async def test_the_write_is_still_bounded(db):
    """Relaxed is not removed — #681 retires the length bound, this line keeps it."""
    result = await memory_handlers.do_store(
        AGENT, {"content": "M" * (config.MAX_CONTENT_LENGTH + 1)}
    )

    assert result.get("truncated") is True
    rows = await db.execute_fetchall(
        "SELECT length(content) FROM memories WHERE agent_id = ?", (AGENT,)
    )
    assert rows[0][0] == config.MAX_CONTENT_LENGTH


@pytest.mark.asyncio
async def test_the_profile_cap_did_not_move_with_it(db):
    """an earlier decision split the two constants so this relaxation would not spread.

    The profile is injected into every recall response and is never
    preview-trimmed (bug-117), so its cap is the only thing bounding it. This is
    the assertion that would have caught a shared constant being relaxed.
    """
    result = await admin_handlers.do_update_profile(
        AGENT, "P" * (config.MAX_PROFILE_LENGTH + 500)
    )

    assert result.get("truncated") is True
    rows = await db.execute_fetchall(
        "SELECT length(content) FROM profiles WHERE agent_id = ?", (AGENT,)
    )
    assert rows[0][0] == config.MAX_PROFILE_LENGTH
    assert config.MAX_PROFILE_LENGTH < config.MAX_CONTENT_LENGTH


# ---------------------------------------------------------------------------
# What the relaxation actually buys, today, without the 2.6 tree
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_tail_past_the_old_cap_is_reachable_by_keyword(db):
    """The stored tail is searchable immediately — the FTS triggers index it whole.

    This is the claim made in config.MAX_CONTENT_LENGTH's comment and in the
    README, so it is asserted rather than assumed. The embedding window (512
    tokens) is unchanged and does not cover this text; the keyword channel does,
    because the trigger inserts ``new.content``, not a windowed prefix.
    """
    row_id = await _store(_record_with_tail_marker())

    hits = await memory_handlers._search_memories_keyword(db, AGENT, TAIL_MARKER, 10)

    assert [h["id"] for h in hits] == [row_id]


@pytest.mark.asyncio
async def test_the_same_write_loses_its_tail_under_the_old_cap(db, monkeypatch):
    """The differential: at 2000 the marker never reaches the database at all.

    Without this, the test above could pass for the wrong reason. Here the only
    thing that changes is the cap, and the row becomes unfindable — which is
    what was happening to every long write until this task.
    """
    monkeypatch.setattr(utils, "MAX_CONTENT_LENGTH", OLD_CAP)

    await _store(_record_with_tail_marker())

    stored = await db.execute_fetchall(
        "SELECT content FROM memories WHERE agent_id = ?", (AGENT,)
    )
    assert TAIL_MARKER not in stored[0][0]
    assert await memory_handlers._search_memories_keyword(db, AGENT, TAIL_MARKER, 10) == []


# ---------------------------------------------------------------------------
# get_contents — the read budget that must not follow the write cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_contents_stops_at_its_budget_and_names_the_rest(db):
    """Four rows at the new cap are 64,000 characters; the batch stops at two.

    Run against the pre-#680 handler this returns all four (~64,000 characters)
    — the blast radius the budget exists to prevent.
    """
    refs = [f"mem:{await _store(ch * config.MAX_CONTENT_LENGTH)}" for ch in "ABCD"]

    result = await memory_handlers.do_get_contents(AGENT, refs)

    assert result["count"] == 2
    assert [item["ref"] for item in result["items"]] == refs[:2]
    assert result["deferred"] == refs[2:]
    assert result["budget_chars"] == memory_handlers.GET_CONTENTS_MAX_CHARS
    delivered = sum(len(item["content"]) for item in result["items"])
    assert delivered <= memory_handlers.GET_CONTENTS_MAX_CHARS


@pytest.mark.asyncio
async def test_get_contents_never_cuts_a_row_to_fit(db):
    """Whole rows only. A trimmed row here would be a preview with no way out.

    ``get_contents`` is the only path back to a row's full text, so trimming to
    make one more row fit would recreate bug-117 (content with no remaining
    handle) at the very seam that exists to escape the preview tier.
    """
    body = "E" * (config.MAX_CONTENT_LENGTH - 1)
    written = {}
    for ch in "XYZ":
        text = ch + body
        written[f"mem:{await _store(text)}"] = text

    result = await memory_handlers.do_get_contents(AGENT, list(written))

    assert result["items"], "the batch must deliver something to be able to cut it"
    for item in result["items"]:
        assert item["content"] == written[item["ref"]]


@pytest.mark.asyncio
async def test_get_contents_returns_a_single_oversized_row_whole(db, monkeypatch):
    """One row alone over the budget is still delivered — unreachable is worse.

    Reachable only through this tool means the budget cannot be allowed to
    refuse a row outright. It stops the BATCH instead: the oversized row comes
    back complete and everything after it is deferred, so no ref is silently
    dropped. Only reachable by raising the write cap, which is exactly the
    scenario the budget is here to survive.
    """
    monkeypatch.setattr(utils, "MAX_CONTENT_LENGTH", 64_000)
    big = f"mem:{await _store('B' * 64_000)}"
    small = f"mem:{await _store('small one')}"

    result = await memory_handlers.do_get_contents(AGENT, [big, small])

    assert result["count"] == 1
    assert len(result["items"][0]["content"]) == 64_000
    assert result["deferred"] == [small]


@pytest.mark.asyncio
async def test_get_contents_keeps_its_shape_when_the_batch_fits(db):
    """A caller that never meets the budget sees the response it always saw."""
    refs = [f"mem:{await _store(f'short {n}')}" for n in range(3)]

    result = await memory_handlers.do_get_contents(AGENT, refs)

    assert result["count"] == 3
    assert "deferred" not in result
    assert "budget_chars" not in result


@pytest.mark.asyncio
async def test_a_missing_ref_is_still_missing_not_deferred(db):
    """The two exits mean different things: not yours / not yet fetched."""
    real = f"mem:{await _store('present')}"

    result = await memory_handlers.do_get_contents(AGENT, [real, "mem:999999"])

    assert result["missing"] == ["mem:999999"]
    assert "deferred" not in result
