"""CSC #677: the profile's ceiling is its own, not the memory content cap.

Why the split exists. A memory is preview-trimmed on the way out and its full
text stays reachable through ``ref`` + ``get_contents``, so MAX_CONTENT_LENGTH
does not govern what a recall response costs. The profile is injected as the
id=-1 sentinel row, which ``_apply_preview`` skips because a ref-less row cannot
be expanded again (bug-117) — so its cap is the only bound it has, and it is
paid in every recall response. While one constant served both, relaxing the
memory cap (the 2.6 tree, CSC #680) would have put an unbounded profile into
every response without a line of code mentioning profiles.

Both defaults are 2000, so no behaviour changes today. That is exactly why these
tests move the two constants apart: an assertion written against equal numbers
cannot tell a separated cap from a shared one. Each test below drives one
constant away from the other and pins which path follows it.
"""

import json

import pytest
import pytest_asyncio

from cpersona import admin_handlers, checks, config, memory_handlers, utils
from cpersona._vendored_mcp_common import no_persist
from cpersona.database import get_db

AGENT = "profile-cap-agent"


@pytest_asyncio.fixture
async def db():
    no_persist.resume()
    conn = await get_db()
    for table in ("memories", "episodes", "profiles"):
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()
    return conn


async def _profile_of(conn, agent_id: str = AGENT) -> str | None:
    rows = await conn.execute_fetchall(
        "SELECT content FROM profiles WHERE agent_id = ?", (agent_id,)
    )
    return rows[0][0] if rows else None


async def _set_profile(conn, content: str, agent_id: str = AGENT) -> None:
    """Write a profile row directly, bypassing the capped writer."""
    await conn.execute(
        """INSERT INTO profiles (agent_id, user_id, content, updated_at)
           VALUES (?, '', ?, datetime('now'))
           ON CONFLICT(agent_id, user_id) DO UPDATE SET content = excluded.content""",
        (agent_id, content),
    )
    await conn.commit()


# ---------------------------------------------------------------------------
# Write path — the profile follows its own constant, in both directions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_profile_write_does_not_follow_the_memory_cap(db, monkeypatch):
    """The point of the task: move the memory cap, the profile stays put.

    This is the failure #680 would otherwise cause in reverse — a relaxed
    memory cap taking the profile with it. Pinned in the direction that is
    cheap to assert (tightening), because the seam is the same one.
    """
    monkeypatch.setattr(utils, "MAX_CONTENT_LENGTH", 50)
    text = "P" * 1_500

    result = await admin_handlers.do_update_profile(AGENT, text)

    assert result["profiles_updated"] == 1
    assert "truncated" not in result
    assert await _profile_of(db) == text


@pytest.mark.asyncio
async def test_profile_write_follows_the_profile_cap(db, monkeypatch):
    """And it does follow its own — a cap nothing reads is not a cap."""
    monkeypatch.setattr(utils, "MAX_PROFILE_LENGTH", 300)
    head = "Prefers terse answers.\n"

    result = await admin_handlers.do_update_profile(AGENT, head + "z" * 5_000)

    assert result.get("truncated") is True
    stored = await _profile_of(db)
    assert len(stored) == 300
    assert stored.startswith(head)


@pytest.mark.asyncio
async def test_memory_write_does_not_follow_the_profile_cap(db, monkeypatch):
    """The opposite direction, so the separation cannot be wired backwards.

    Unlike the test above this one cannot fail against the pre-split code (the
    constant did not exist to move); it holds the line going forward, when
    MAX_PROFILE_LENGTH is a small number someone could reach for by mistake.
    """
    monkeypatch.setattr(utils, "MAX_PROFILE_LENGTH", 50)
    text = "M" * 1_500

    await memory_handlers.do_store(AGENT, {"content": text})

    rows = await db.execute_fetchall(
        "SELECT content FROM memories WHERE agent_id = ?", (AGENT,)
    )
    assert rows[0][0] == text


# ---------------------------------------------------------------------------
# check_health — detector and repair read the same number, and it is the
# profile's number
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detector_does_not_follow_the_memory_cap(db, monkeypatch):
    """A profile within the profile cap is not a finding, whatever memories cap at."""
    monkeypatch.setattr(checks, "MAX_CONTENT_LENGTH", 100)
    await _set_profile(db, "x" * 500)

    assert await checks.check_oversized_profile(db, AGENT, fix=False) == []


@pytest.mark.asyncio
async def test_detector_follows_the_profile_cap(db, monkeypatch):
    monkeypatch.setattr(checks, "MAX_PROFILE_LENGTH", 500)
    await _set_profile(db, "x" * 1_000)

    found = await checks.check_oversized_profile(db, AGENT, fix=False)

    assert len(found) == 1
    assert found[0]["type"] == "oversized_profile"
    assert found[0]["max_len"] == 1_000


@pytest.mark.asyncio
async def test_detection_and_repair_read_one_number(db, monkeypatch):
    """Split constants are where a detector and its repair start disagreeing.

    With the two caps apart, a repair still cutting to MAX_CONTENT_LENGTH leaves
    a row the detector keeps reporting: the check would fix and re-find forever,
    reporting a warn nobody can clear. The re-check is the assertion that catches
    it — the length alone would not, since 1000 < the memory cap either way.
    """
    monkeypatch.setattr(checks, "MAX_PROFILE_LENGTH", 500)
    await _set_profile(db, "y" * 1_000)

    await checks.check_oversized_profile(db, AGENT, fix=True)

    rows = await db.execute_fetchall(
        "SELECT length(content) FROM profiles WHERE agent_id = ?", (AGENT,)
    )
    assert rows[0][0] == 500
    assert await checks.check_oversized_profile(db, AGENT, fix=False) == []


# ---------------------------------------------------------------------------
# Import path — the second writer, and the number it reports
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_cuts_to_the_profile_cap_and_reports_that_number(
    db, tmp_path, monkeypatch
):
    """The message quotes the cut it made, so it cannot drift from the cut.

    The per-line error used to render MAX_CONTENT_LENGTH while the cut came from
    the sanitiser; with one constant the two agreed by accident. The number now
    comes from the text that survived, which is the cap by construction.
    """
    monkeypatch.setattr(utils, "MAX_PROFILE_LENGTH", 300)
    export_root = tmp_path / "confined"
    export_root.mkdir(exist_ok=True)
    monkeypatch.setattr(config, "EXPORT_DIR", str(export_root))
    path = export_root / "import.jsonl"
    record = {
        "_type": "profile",
        "agent_id": AGENT,
        "user_id": "",
        "content": "Prefers terse answers.\n" + "z" * 5_000,
    }
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    result = await admin_handlers.do_import_memories(str(path))

    assert result["profile_updated"] is True
    stored = await _profile_of(db)
    assert len(stored) == 300
    assert any("300-character" in e for e in result["errors"]), result.get("errors")


# ---------------------------------------------------------------------------
# The boundary contract — a lossy bound the tool never mentions is invisible
# ---------------------------------------------------------------------------


def test_update_profile_tool_states_its_cap_and_flag():
    """The cut is unrecoverable, so the tool has to announce it.

    store and update_memory both document their cap and their truncated flag;
    update_profile was the one write tool whose bound was invisible at the
    boundary, and the profile row is the ONLY copy of that text (no ref, not a
    memory row). A caller with no reason to look for `truncated` reported success
    while the remainder was gone for good.
    """
    from cpersona import server

    tool = next(t for t in server.registry._tools if t.name == "update_profile")
    surfaces = [tool.description, tool.inputSchema["properties"]["profile"]["description"]]
    for text in surfaces:
        assert "CPERSONA_MAX_PROFILE_LENGTH" in text, text
        assert str(config.MAX_PROFILE_LENGTH) in text, text
        assert "truncated" in text, text
