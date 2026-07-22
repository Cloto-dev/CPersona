"""Source-contract normalization (2.5.2, Task #282 items 1 + 1b).

Three surfaces move together on the canonical ``source = {type,id,name}``
contract:

1. ``normalize_source`` (utils) — the pure mapping table shared by the write
   path and the health-check fixer. Unit tests below pin every branch it
   commits to (canonical fast-path, case-insensitive vocabulary, Rust serde
   externally-tagged shape, bare-string shorthand, and the deliberate
   *unmapped* verdict for shapes we don't understand).
2. ``do_store`` — the write seam MUST call the normalizer before ``json.dumps``
   so newly-written rows land canonical. Unknown shapes are stored verbatim so
   ``check_invalid_source_type`` still surfaces them for human review.
3. ``check_invalid_source_type(fix=True)`` — replaces the pre-2.5.2 blanket
   overwrite that stamped every offending row with an anonymous
   ``{"type":"User","id":"","name":""}`` sentinel (lossy: destroyed attribution
   wholesale). The mapping-based fixer rewrites only rows we recognise,
   preserves id / name from the legacy shape, and returns residual
   ``mapped`` / ``unmapped`` counts.
"""

import json

import pytest
import pytest_asyncio

from cpersona import checks, maintenance_handlers, memory_handlers
from cpersona._vendored_mcp_common import no_persist
from cpersona.database import get_db
from cpersona.utils import normalize_source


# ---------------------------------------------------------------------------
# unit: normalize_source
# ---------------------------------------------------------------------------


class TestNormalizeSourceUnit:
    """Every branch of the mapping table lives here.

    The invariant: ``mapped=False`` MUST leave the input identical (``is``
    identity for dicts / str, so a caller can persist it verbatim without
    fabricating anything).
    """

    # (1) already canonical — the 2.5.x fast path
    def test_canonical_user_untouched(self):
        src = {"type": "User", "id": "u1", "name": "Alice"}
        out, mapped = normalize_source(src)
        assert mapped is False
        assert out is src

    def test_canonical_agent_untouched(self):
        src = {"type": "Agent", "id": "a1", "name": "Bot"}
        out, mapped = normalize_source(src)
        assert mapped is False
        assert out is src

    def test_canonical_system_untouched(self):
        src = {"type": "System", "id": "profile", "name": ""}
        out, mapped = normalize_source(src)
        assert mapped is False
        assert out is src

    # (2) case-insensitive vocabulary variants
    def test_lowercase_user_maps_and_preserves_id_name(self):
        out, mapped = normalize_source({"type": "user", "id": "u1", "name": "Alice"})
        assert mapped is True
        # id / name MUST survive — the blanket-User fixer was lossy here.
        assert out == {"type": "User", "id": "u1", "name": "Alice"}

    def test_lowercase_agent_maps(self):
        out, mapped = normalize_source({"type": "agent", "id": "a"})
        assert mapped is True and out["type"] == "Agent" and out["id"] == "a"

    def test_lowercase_system_maps(self):
        out, mapped = normalize_source({"type": "system"})
        assert mapped is True and out["type"] == "System"

    def test_assistant_folds_to_agent(self):
        # The enum stays 3-valued; Assistant does not become a fourth type
        # (marketplace / ClotoCore serde alignment).
        out, mapped = normalize_source({"type": "assistant", "id": "sonnet"})
        assert mapped is True
        assert out == {"type": "Agent", "id": "sonnet"}

    def test_ai_folds_to_agent(self):
        out, mapped = normalize_source({"type": "ai", "name": "gpt"})
        assert mapped is True and out["type"] == "Agent" and out["name"] == "gpt"

    def test_session_folds_to_system(self):
        out, mapped = normalize_source({"type": "session", "id": "sess1"})
        assert mapped is True and out["type"] == "System" and out["id"] == "sess1"

    def test_unknown_vocabulary_untouched(self):
        # 'migration' etc. — we won't guess a discriminator.
        src = {"type": "migration", "id": "m1"}
        out, mapped = normalize_source(src)
        assert mapped is False and out is src

    # (3) Rust serde externally-tagged dict
    def test_serde_user_string_inner(self):
        # {"User": "u1"} — ClotoCore's historical wire shape.
        out, mapped = normalize_source({"User": "u1"})
        assert mapped is True
        assert out == {"type": "User", "id": "u1", "name": "u1"}

    def test_serde_system_string_inner(self):
        # System has always carried a bare label like 'profile' / 'episode'.
        out, mapped = normalize_source({"System": "episode"})
        assert mapped is True
        assert out == {"type": "System", "id": "episode", "name": ""}

    def test_serde_agent_dict_inner(self):
        out, mapped = normalize_source({"Agent": {"id": "a1", "name": "Bot"}})
        assert mapped is True
        assert out["type"] == "Agent" and out["id"] == "a1" and out["name"] == "Bot"

    def test_serde_two_key_dict_not_matched(self):
        # Only single-key externally-tagged dicts are the serde shape.
        src = {"User": "u1", "extra": 1}
        out, mapped = normalize_source(src)
        assert mapped is False and out is src

    # (4) bare-string sources
    def test_bare_string_user_maps(self):
        out, mapped = normalize_source("user")
        assert mapped is True
        assert out == {"type": "User", "id": "", "name": ""}

    def test_bare_string_assistant_folds_to_agent(self):
        out, mapped = normalize_source("Assistant")
        assert mapped is True and out["type"] == "Agent"

    def test_bare_string_ai_folds_to_agent(self):
        out, mapped = normalize_source("AI")
        assert mapped is True and out["type"] == "Agent"

    def test_bare_string_unknown_untouched(self):
        # 'claude-code' / arbitrary agent-ids stay for the human-reviewed
        # migration path (1a) — never guessed at.
        out, mapped = normalize_source("claude-code")
        assert mapped is False and out == "claude-code"

    # (5) unmapped fallthroughs
    def test_empty_dict_untouched(self):
        src = {}
        out, mapped = normalize_source(src)
        assert mapped is False and out is src

    def test_dict_without_type_key_untouched(self):
        src = {"id": "u1", "name": "Alice"}
        out, mapped = normalize_source(src)
        assert mapped is False and out is src

    def test_none_untouched(self):
        out, mapped = normalize_source(None)
        assert mapped is False and out is None


# ---------------------------------------------------------------------------
# integration: write path (do_store) applies the normalizer before persist
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def clean_db():
    """Truncate + sequence reset — same shape as the other 2.5.2 fixtures."""
    no_persist.resume()
    db = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await db.execute(f"DELETE FROM {table}")
    await db.execute(
        "DELETE FROM sqlite_sequence WHERE name IN "
        "('memories','episodes','profiles','pending_memory_tasks')"
    )
    await db.commit()
    return db


@pytest.mark.asyncio
async def test_store_normalizes_lowercase_type_word(clean_db):
    """A ``{"type":"user"}`` write MUST land canonical. This was a bug: the
    write path json.dumps'd whatever the caller passed with zero validation,
    which is how ~75 % of production rows drifted."""
    db = clean_db
    res = await memory_handlers.do_store(
        "a1",
        {"content": "lowercase-type row", "source": {"type": "user", "id": "u1", "name": "Alice"}},
    )
    assert res["ok"] is True and not res.get("skipped"), res
    row = await db.execute_fetchall("SELECT source FROM memories WHERE id = ?", (res["id"],))
    parsed = json.loads(row[0][0])
    # Discriminator canonicalised, id / name preserved (would be destroyed by
    # the pre-2.5.2 blanket-User fixer even if it caught this row later).
    assert parsed == {"type": "User", "id": "u1", "name": "Alice"}


@pytest.mark.asyncio
async def test_store_normalizes_bare_string_assistant(clean_db):
    """A bare-string ``"assistant"`` source folds to Agent (the enum stays
    3-valued), with empty id / name because the shorthand carries neither."""
    db = clean_db
    res = await memory_handlers.do_store(
        "a1", {"content": "bare assistant row", "source": "assistant"}
    )
    assert res["ok"] is True and not res.get("skipped"), res
    row = await db.execute_fetchall("SELECT source FROM memories WHERE id = ?", (res["id"],))
    assert json.loads(row[0][0]) == {"type": "Agent", "id": "", "name": ""}


@pytest.mark.asyncio
async def test_store_leaves_unknown_source_verbatim(clean_db):
    """An unknown ``{"type":"migration"}`` shape MUST be stored as-is. Silent
    fabrication of a discriminator would corrupt attribution AND hide the row
    from ``check_invalid_source_type`` — the whole point of item (1) is that
    the health check stays the single detector."""
    db = clean_db
    unknown = {"type": "migration", "id": "m1", "name": "seed"}
    res = await memory_handlers.do_store(
        "a1", {"content": "unknown-type row", "source": unknown}
    )
    assert res["ok"] is True and not res.get("skipped"), res
    row = await db.execute_fetchall("SELECT source FROM memories WHERE id = ?", (res["id"],))
    assert json.loads(row[0][0]) == unknown


# ---------------------------------------------------------------------------
# integration: check_invalid_source_type fix path is mapping-based, not lossy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fix_maps_known_legacy_shapes_preserving_id_and_name(clean_db):
    """The core (1b) regression: the pre-2.5.2 fix path blanket-overwrote
    every offending row to ``{"type":"User","id":"","name":""}``. This test
    seeds legacy shapes, runs fix=True, and asserts that:

    - Each legacy shape lands on its correct canonical discriminator.
    - id / name from the legacy row survive (would be destroyed by the
      blanket-User overwrite — the whole reason we rewrote the fixer).
    - Locked rows and unknown shapes stay byte-identical.
    """
    db = clean_db
    # Insert directly — bypassing do_store — so we can seed shapes the new
    # write path would normalize on the way in. The fixer under test is what
    # we're exercising, not the write path.
    seeds = [
        # id 1: lowercase 'user' with id/name — MUST become User + preserve.
        ('{"type":"user","id":"u1","name":"Alice"}', 0),
        # id 2: assistant → Agent, preserve name.
        ('{"type":"assistant","id":"sonnet","name":"Sonnet"}', 0),
        # id 3: serde externally-tagged System.
        ('{"System":"episode"}', 0),
        # id 4: bare string 'user' (whole source is a JSON string).
        ('"user"', 0),
        # id 5: unknown vocabulary — MUST stay verbatim so the check still fires.
        ('{"type":"migration","id":"m1"}', 0),
        # id 6: locked row with a mappable legacy shape — MUST NOT be touched.
        ('{"type":"user","id":"locked-uid","name":"Do Not Rewrite"}', 1),
    ]
    for idx, (src, locked) in enumerate(seeds, start=1):
        # Distinct content per row so the v12 (agent_id, project_id, channel,
        # content) UNIQUE index doesn't collide two seeds whose truncated
        # source strings happen to prefix-match.
        await db.execute(
            "INSERT INTO memories (agent_id, content, source, timestamp, locked) "
            "VALUES (?, ?, ?, ?, ?)",
            ("fix", f"seed-row-{idx}", src, "2026-07-22T00:00:00+00:00", locked),
        )
    await db.commit()

    # Snapshot the two rows that MUST NOT change (locked + unknown).
    before = {
        row_id: src
        for row_id, src in await db.execute_fetchall(
            "SELECT id, source FROM memories WHERE agent_id = 'fix' ORDER BY id"
        )
    }

    res = await maintenance_handlers.do_check_health(agent_id="fix", fix=True)

    # After fix, run again with fix=False to read the residual issue (bug-059
    # pattern from the surrounding fixers): the fix run's own return is the
    # first-pass finding, and we want the second-pass surface for the count.
    rows = {
        row_id: src
        for row_id, src in await db.execute_fetchall(
            "SELECT id, source FROM memories WHERE agent_id = 'fix' ORDER BY id"
        )
    }

    # id 1: lowercase user → User, preserving id/name.
    assert json.loads(rows[1]) == {"type": "User", "id": "u1", "name": "Alice"}
    # id 2: assistant → Agent, preserving id/name.
    assert json.loads(rows[2]) == {"type": "Agent", "id": "sonnet", "name": "Sonnet"}
    # id 3: serde System → canonical System with the label lifted into id.
    assert json.loads(rows[3]) == {"type": "System", "id": "episode", "name": ""}
    # id 4: bare string "user" → canonical empty-id User (the shorthand carries no id).
    assert json.loads(rows[4]) == {"type": "User", "id": "", "name": ""}
    # id 5: unknown — untouched, still surfaces on the next health check.
    assert rows[5] == before[5]
    # id 6: locked — byte-identical to the seed even though its shape is mappable.
    assert rows[6] == before[6]

    # And the issue dict carries the additive mapped / unmapped counts.
    findings = [
        i for i in res["issues"] if i.get("type") == "invalid_source_type"
    ]
    assert len(findings) >= 1, res["issues"]
    # The first pass ran the fixer; the second pass's finding (bug-059
    # residual re-run) is what do_check_health returns. Either surface MUST
    # carry the additive counters somewhere in the response tree, so search.
    ran = None
    # do_check_health calls the runner twice: once with fix=True (issue dict
    # gets the additive counters), then re-runs fix=False to derive the
    # residual issues in the response. Pull the counters off the fix-time
    # log instead by calling the check directly.
    from cpersona.database import transaction

    async with transaction() as db_tx:
        # Re-insert one mappable row so the fixer has something to count.
        await db_tx.execute(
            "INSERT INTO memories (agent_id, content, source, timestamp) "
            "VALUES ('fix2', 'x', '{\"type\":\"user\",\"id\":\"u2\"}', 't')"
        )
        await db_tx.execute(
            "INSERT INTO memories (agent_id, content, source, timestamp) "
            "VALUES ('fix2', 'y', '{\"type\":\"migration\"}', 't')"
        )
        ran = await checks.check_invalid_source_type(db_tx, "fix2", fix=True)
    assert ran and ran[0]["type"] == "invalid_source_type"
    assert ran[0]["mapped"] == 1 and ran[0]["unmapped"] == 1, ran

    # Nothing in the fixed corpus lost id/name to a blanket-User sentinel
    # (the (1b) anti-regression: any row landing on empty id AND empty name
    # AND type=User where we had an id to preserve would be the old bug).
    for row_id in (1, 2):
        parsed = json.loads(rows[row_id])
        assert parsed["id"] != "" or parsed["name"] != "", (
            f"row {row_id} lost attribution to a blanket-User overwrite"
        )


@pytest.mark.asyncio
async def test_fix_reports_locked_rows_it_cannot_touch(clean_db):
    """bug-139: ``count`` spans every offending row, but the fixer only sees
    ``locked = 0`` (bug-098 invariant). The additive ``locked`` counter
    reconciles the arithmetic (count == mapped + unmapped + locked) so a
    locked-heavy corpus doesn't read as "found N, processed 0"."""
    from cpersona.database import transaction

    async with transaction() as db_tx:
        # Locked mappable offender — counted, never rewritten.
        await db_tx.execute(
            "INSERT INTO memories (agent_id, content, source, timestamp, locked) "
            "VALUES ('lockfix', 'a', '{\"type\":\"user\",\"id\":\"u1\"}', 't', 1)"
        )
        # Unlocked mappable offender — rewritten by the fixer.
        await db_tx.execute(
            "INSERT INTO memories (agent_id, content, source, timestamp) "
            "VALUES ('lockfix', 'b', '{\"type\":\"assistant\"}', 't')"
        )
        ran = await checks.check_invalid_source_type(db_tx, "lockfix", fix=True)

    assert ran and ran[0]["type"] == "invalid_source_type"
    assert ran[0]["count"] == 2, ran
    assert ran[0]["mapped"] == 1
    assert ran[0]["unmapped"] == 0
    assert ran[0]["locked"] == 1
    assert (
        ran[0]["mapped"] + ran[0]["unmapped"] + ran[0]["locked"] == ran[0]["count"]
    )
