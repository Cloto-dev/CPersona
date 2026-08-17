"""Regression tests for bug-217: exclude_contents needs a length floor before prefix matching.

``_content_excluded`` returns True when EITHER string prefixes the other. That
bidirectional test absorbs the truncation asymmetry between a 500-char context echo and
a full stored memory — but it had no minimum length, and ``do_recall_with_context`` feeds
it EVERY external_context entry. A one-word acknowledgement ("OK", "yes", "はい") therefore
prefix-matched every memory beginning with those letters and removed it from the candidate
set inside the retrievers, before ranking: the agent got a result set with the most
relevant rows missing and nothing in the response explaining it (``context_filter_only``
reports non-user/assistant ROLES, not over-broad matches).

bug-121 fixed the empty-CONTENT side of the same class; this is the short-ENTRY side.
Entries below ``EXCLUDE_PREFIX_MIN_CHARS`` must match exactly; the dedup contract the
prefix test exists for is unchanged above the floor (pinned below).
"""

import os
import tempfile

os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "test_bug217.db"))
os.environ.setdefault("CPERSONA_EMBEDDING_MODE", "none")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from cpersona import memory_handlers as M  # noqa: E402
from cpersona.database import get_db  # noqa: E402
from cpersona.utils import EXCLUDE_PREFIX_MIN_CHARS, _content_excluded  # noqa: E402

AGENT = "agent.bug217"
MEMORY = "OK, we decided to roll back before the deploy on Friday afternoon"
LONG_MEMORY = "deploy checklist: " + "verify the migration and the rollback plan. " * 20


@pytest_asyncio.fixture(autouse=True)
async def _fresh_db():
    db = await get_db()
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM episodes")
    await db.commit()
    yield


def test_short_exclude_entries_match_exactly():
    """bug-217: a 2-char acknowledgement must not prefix-match unrelated memories."""
    for ack in ("ok", "yes", "no", "はい"):
        assert _content_excluded(f"{ack.upper()}, and then a whole unrelated memory", {ack}) is False
    # An exact (case-insensitive) match is still an exclusion — the caller does hold that text.
    assert _content_excluded("OK", {"ok"}) is True
    assert _content_excluded("  ok  ", {"ok"}) is True


def test_prefix_matching_survives_above_the_floor():
    """The truncation asymmetry the prefix test exists for is untouched."""
    echo = LONG_MEMORY[:200]
    assert len(echo) >= EXCLUDE_PREFIX_MIN_CHARS
    assert _content_excluded(LONG_MEMORY, {echo.lower()}) is True  # stored longer than the echo
    assert _content_excluded(echo, {LONG_MEMORY.lower()}) is True  # and the reverse


@pytest.mark.asyncio
async def test_recall_with_context_keeps_memories_a_short_ack_would_have_dropped(
    fake_embedding_client,
):
    """bug-217 end to end: an 'OK' in external_context must not delete the answer."""
    stored = await M.do_store(AGENT, {"content": MEMORY, "source": {"System": "t"}})
    assert stored["result"] == "stored", stored

    out = await M.do_recall_with_context(
        AGENT, MEMORY, external_context=[{"role": "user", "content": "OK"}], limit=5
    )
    contents = [m["content"] for m in out["messages"]]
    assert MEMORY in contents, f"a 2-character context entry suppressed the memory: {contents}"


@pytest.mark.asyncio
async def test_recall_with_context_still_dedups_a_truncated_echo(fake_embedding_client):
    """The dedup the exclusion exists for still fires above the floor."""
    stored = await M.do_store(AGENT, {"content": LONG_MEMORY, "source": {"System": "t"}})
    assert stored["result"] == "stored", stored

    echo = LONG_MEMORY[:200]
    out = await M.do_recall_with_context(
        AGENT, LONG_MEMORY, external_context=[{"role": "user", "content": echo}], limit=5
    )
    contents = [m["content"] for m in out["messages"]]
    assert LONG_MEMORY not in contents, "the caller already holds this text; it must be filtered"
    assert echo in contents, "the context entry itself is still merged into the response"
