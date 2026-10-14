"""Regression tests: export and import keep what they carry, and say what they could not.

  bug-614  The exporter parsed ``source`` and ``metadata`` with a helper that answers
           {} for text that is not JSON, so a legacy attribution string was dropped
           from the backup without a word and the restored row held '{}' -- locked
           rows included, which the invalid-JSON repair deliberately leaves alone.
  bug-615  A NUL byte in an export or import path raised ValueError out of the path
           checks (``os.stat`` / ``realpath`` / ``mkstemp``), which the handlers did
           not catch, so the caller got a bare {"error"} with no ``ok: false``.
  bug-616  The sanitiser removed ``[Memory from ...]`` annotations in one pass, which
           can assemble a new annotation out of the text around the one it removed,
           and cut at the cap without trimming the whitespace the cut exposed. Import
           sanitises again, so a round trip changed the text and a re-import added a
           second, altered row. A truncation note was also reported for a row the
           import then skipped.
  bug-617  An export written before the project and channel axes were exported
           carries neither key, and the import put every such row in the global pool
           without saying so -- rows confined to a project became readable from all.
  bug-618  Values bound only on the real arm (created_at, last_recalled_at,
           recall_count, the episode times, the profile stamp) went to SQLite as
           whatever JSON type the file held, and a lone surrogate in any text, a
           non-object JSON line or a non-string agent id raised on both arms. Each
           aborted the whole restore -- after a preview that answered ok:true.
  bug-620  An import of a several-agent export into one target upserted every profile
           onto the same (target, user_id) key, so each overwrote the one before and
           only the last survived, reported as profile_updated:true.

Each test states the correct behaviour and was seen to fail on the unfixed code for
the reason named above, except where a docstring says it guards the fix instead.
"""

import json
import random
from pathlib import Path

import pytest
import pytest_asyncio

from cpersona import admin_handlers, config, session, utils
from cpersona.database import get_db
from cpersona.memory_handlers import do_store

TABLES = ("memories", "episodes", "profiles", "pending_memory_tasks")
AGENT = "agent.x03"


@pytest_asyncio.fixture
async def db():
    session.reset_pauses_for_tests()
    conn = await get_db()
    for table in TABLES:
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()
    yield conn
    session.reset_pauses_for_tests()


def _write_jsonl(path: Path, records) -> str:
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return str(path)


def _memory(content: str, **extra) -> dict:
    record = {
        "_type": "memory",
        "agent_id": AGENT,
        "msg_id": "",
        "content": content,
        "source": {},
        "timestamp": "2026-01-01T00:00:00+00:00",
        "metadata": {},
        "project_id": "",
        "channel": "",
    }
    record.update(extra)
    return record


def _episode(summary: str, **extra) -> dict:
    record = {
        "_type": "episode",
        "agent_id": AGENT,
        "summary": summary,
        "keywords": "",
        "start_time": "2026-01-01T00:00:00+00:00",
        "end_time": "2026-01-01T01:00:00+00:00",
        "project_id": "",
        "channel": "",
    }
    record.update(extra)
    return record


def _tally(result: dict) -> tuple:
    return (
        result.get("ok"),
        result.get("imported_memories"),
        result.get("skipped_memories"),
        result.get("imported_episodes"),
        result.get("skipped_episodes"),
    )


# ---------------------------------------------------------------------------
# bug-614: text that is not JSON travels through the backup as text.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_carries_non_json_source_and_metadata_through_a_restore(db, tmp_path):
    await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp, metadata, locked)"
        " VALUES (?, 'legacy row', 'discord:user#1234', '2026-01-01T00:00:00+00:00', 'session=abc;chunk=2', 1)",
        (AGENT,),
    )
    # Control: a row whose columns are JSON keeps travelling as JSON.
    await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp, metadata)"
        " VALUES (?, 'json row', ?, '2026-01-01T00:00:00+00:00', ?)",
        (AGENT, json.dumps({"type": "User", "id": "u1"}), json.dumps({"k": 1})),
    )
    await db.commit()

    out = tmp_path / "legacy.jsonl"
    exp = await admin_handlers.do_export_memories(AGENT, str(out))
    assert exp["ok"] is True, exp
    records = {r["content"]: r for r in map(json.loads, out.read_text().splitlines()) if r["_type"] == "memory"}
    assert records["json row"]["source"] == {"type": "User", "id": "u1"}

    res = await admin_handlers.do_import_memories(str(out), target_agent_id="agent.x03.copy")
    assert res["ok"] is True and res["imported_memories"] == 2, res
    rows = {
        r[0]: (r[1], r[2], r[3])
        for r in await db.execute_fetchall(
            "SELECT content, source, metadata, locked FROM memories WHERE agent_id = 'agent.x03.copy'"
        )
    }
    assert rows["legacy row"] == ("discord:user#1234", "session=abc;chunk=2", 1), rows
    assert json.loads(rows["json row"][0]) == {"type": "User", "id": "u1"}
    assert json.loads(rows["json row"][1]) == {"k": 1}


# ---------------------------------------------------------------------------
# bug-615: a NUL byte in a path is refused in the error-response shape.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("confined", [False, True])
async def test_nul_byte_in_export_and_import_paths_is_an_error_response(db, tmp_path, monkeypatch, confined):
    if confined:
        monkeypatch.setattr(config, "EXPORT_DIR", str(tmp_path))
    else:
        monkeypatch.setattr(config, "EXPORT_DIR", "")
    bad = str(tmp_path / "rel\x00.jsonl")

    exp = await admin_handlers.do_export_memories(AGENT, bad)
    assert exp.get("ok") is False and exp.get("error"), exp
    imp = await admin_handlers.do_import_memories(bad)
    assert imp.get("ok") is False and imp.get("error"), imp
    imp = await admin_handlers.do_import_memories("\x00", dry_run=True)
    assert imp.get("ok") is False and imp.get("error"), imp
    # The ACL guard reads the same paths before either handler runs.
    assert admin_handlers.import_file_agents(bad) == set()
    assert admin_handlers.export_file_owner(bad) is None


# ---------------------------------------------------------------------------
# bug-616: sanitising twice gives what sanitising once gave.
# ---------------------------------------------------------------------------

NESTED = "[Memory fr[Memory from bot] om the harbour log] tide at 06:10"


def test_sanitiser_is_idempotent_on_an_annotation_assembled_by_its_own_removal():
    once = utils._sanitize_content(NESTED)
    assert utils._sanitize_content(once) == once
    assert once == "tide at 06:10"


def test_sanitiser_is_idempotent_when_the_cap_exposes_trailing_whitespace(monkeypatch):
    monkeypatch.setattr(utils, "MAX_CONTENT_LENGTH", 10)
    once, truncated = utils.sanitize_content_with_flag("abcdefghi  jklmnop")
    assert truncated is True
    assert utils.sanitize_content_with_flag(once) == (once, False)
    assert once == "abcdefghi"


@pytest.mark.parametrize("layers", [1, 2, 5, 40])
def test_sanitiser_removes_every_layer_of_a_nested_annotation(layers):
    text = "head " + "[Memory fr" * layers + "[Memory from x]" + " om x]" * layers + " tail"
    assert utils._sanitize_content(text) == "head tail"


def _fixpoint(text: str) -> str:
    pattern = utils._MEMORY_ANNOTATION_PATTERN
    while True:
        reduced = pattern.sub("", text)
        if reduced == text:
            return text
        text = reduced


def test_sanitiser_reaches_a_fixed_point_and_keeps_one_pass_results(monkeypatch):
    """Guards the fix: random texts built from annotation fragments. The result never
    holds an annotation; where one pass already reached a fixed point, the result is
    that pass's text (ordinary content is stored exactly as before)."""
    monkeypatch.setattr(utils, "MAX_CONTENT_LENGTH", 10**6)
    rng = random.Random(616)
    parts = ["[Memory from ", "[Memory fr", "om ", "x", "]", " ", "\n", "[", "a b", "Memory from "]
    for _ in range(4000):
        text = "".join(rng.choice(parts) for _ in range(rng.randint(0, 40)))
        once = utils._sanitize_content(text)
        assert not utils._MEMORY_ANNOTATION_PATTERN.search(once), (text, once)
        assert utils._sanitize_content(once) == once, (text, once)
        one_pass = utils._MEMORY_ANNOTATION_PATTERN.sub("", text).strip()
        if _fixpoint(one_pass) == one_pass:
            assert once == one_pass, (text, once, one_pass)


@pytest.mark.asyncio
async def test_a_round_trip_of_a_nested_annotation_neither_mutates_nor_duplicates(db, tmp_path):
    r = await do_store(AGENT, {"content": NESTED, "id": "m-nested"})
    assert r.get("ok") is True, r
    out = tmp_path / "nested.jsonl"
    assert (await admin_handlers.do_export_memories(AGENT, str(out)))["ok"] is True
    # Without a msg_id the content probe is the only dedup key a re-import has.
    lines = [json.loads(line) for line in out.read_text().splitlines()]
    for record in lines:
        if record["_type"] == "memory":
            record["msg_id"] = ""
    _write_jsonl(out, lines)

    res = await admin_handlers.do_import_memories(str(out))
    assert _tally(res)[:3] == (True, 0, 1), res
    rows = await db.execute_fetchall("SELECT content FROM memories WHERE agent_id = ?", (AGENT,))
    assert [r[0] for r in rows] == ["tide at 06:10"]


@pytest.mark.asyncio
async def test_no_truncation_note_for_a_row_the_import_skipped(db, tmp_path, monkeypatch):
    r = await do_store(AGENT, {"content": "x" * 50, "id": "m-long"})
    assert r.get("ok") is True, r
    out = tmp_path / "long.jsonl"
    assert (await admin_handlers.do_export_memories(AGENT, str(out)))["ok"] is True
    monkeypatch.setattr(utils, "MAX_CONTENT_LENGTH", 20)

    res = await admin_handlers.do_import_memories(str(out))
    assert _tally(res)[:3] == (True, 0, 1), res
    assert not any("truncated" in e for e in res.get("errors", [])), res


# ---------------------------------------------------------------------------
# bug-617: a record without the isolation axes is restored, and the import says where.
# ---------------------------------------------------------------------------


# The header the exporters of 2.4.34 to 2.4.37 wrote, key for key: the same version
# string a later export carries, so only the records can tell the file is older.
LEGACY_HEADER = {
    "_type": "header",
    "version": "cpersona-export/1.0",
    "agent_id": AGENT,
    "exported_at": "2026-01-01T00:00:00+00:00",
    "memory_count": 1,
    "episode_count": 1,
    "has_profile": False,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False])
async def test_records_without_the_project_axis_are_reported(db, tmp_path, dry_run):
    legacy_mem = _memory("project X only: the access code rotation plan")
    del legacy_mem["project_id"], legacy_mem["channel"]
    legacy_ep = _episode("an old project episode")
    del legacy_ep["project_id"], legacy_ep["channel"]
    path = _write_jsonl(tmp_path / "legacy.jsonl", [LEGACY_HEADER, legacy_mem, legacy_ep])

    res = await admin_handlers.do_import_memories(path, dry_run=dry_run)
    assert _tally(res) == (True, 1, 0, 1, 0), res
    notes = [e for e in res.get("errors", []) if "project_id" in e and "global pool" in e]
    assert len(notes) == 1, res
    assert "1 memory" in notes[0] and "1 episode" in notes[0], notes


@pytest.mark.asyncio
async def test_a_hand_written_file_without_a_header_raises_no_project_axis_note(db, tmp_path):
    """Guards the fix: without an export header, a missing key is the writer's way
    of saying the global pool, and the restore says nothing about it."""
    legacy_mem = _memory("a hand-written note")
    del legacy_mem["project_id"], legacy_mem["channel"]
    res = await admin_handlers.do_import_memories(_write_jsonl(tmp_path / "hand.jsonl", [legacy_mem]))
    assert _tally(res)[:2] == (True, 1) and "errors" not in res, res


@pytest.mark.asyncio
async def test_a_current_export_raises_no_project_axis_note(db, tmp_path):
    """Guards the fix: the note is about files that lack the key, not about the
    global pool -- a current export of global rows says nothing."""
    r = await do_store(AGENT, {"content": "a global note", "id": "m-g"})
    assert r.get("ok") is True, r
    r = await do_store(AGENT, {"content": "a project note", "id": "m-p"}, project_id="projX")
    assert r.get("ok") is True, r
    out = tmp_path / "current.jsonl"
    assert (await admin_handlers.do_export_memories(AGENT, str(out)))["ok"] is True
    res = await admin_handlers.do_import_memories(str(out), target_agent_id="agent.x03.copy")
    assert res["ok"] is True and res["imported_memories"] == 2, res
    assert "errors" not in res, res
    rows = await db.execute_fetchall(
        "SELECT project_id FROM memories WHERE agent_id = 'agent.x03.copy' ORDER BY project_id"
    )
    assert [r[0] for r in rows] == ["", "projX"]


# ---------------------------------------------------------------------------
# bug-618: a bad value is refused for its line; the preview and the restore agree.
# ---------------------------------------------------------------------------

BAD_LINES = {
    "mem_dict_created_at": _memory("m1", created_at={"a": 1}),
    "mem_list_last_recalled_at": _memory("m2", last_recalled_at=[1]),
    "mem_word_recall_count": _memory("m3", recall_count="many"),
    "mem_huge_recall_count": _memory("m4", recall_count=2**70),
    "mem_dict_project_id": _memory("m5", project_id={"p": 1}),
    "mem_surrogate_content": _memory("tide \ud800 log"),
    "mem_list_content": _memory(["not", "text"]),
    "mem_dict_source_text": _memory("m6", source_text={"a": 1}),
    "ep_list_start_time": _episode("e1", start_time=[1]),
    "ep_dict_end_time": _episode("e2", end_time={"a": 1}),
    "ep_int_created_at": _episode("e3", created_at=17),
    "ep_surrogate_summary": _episode("summary \udfff here"),
    "ep_dict_channel": _episode("e4", channel={"c": 1}),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(BAD_LINES))
async def test_a_bad_field_refuses_its_line_not_the_restore(db, tmp_path, name):
    good = _memory("a good memory that must be restored")
    path = _write_jsonl(tmp_path / f"{name}.jsonl", [good, BAD_LINES[name]])

    preview = await admin_handlers.do_import_memories(path, dry_run=True)
    real = await admin_handlers.do_import_memories(path)
    assert real["ok"] is True, real
    assert _tally(preview) == _tally(real), (preview, real)
    assert real["imported_memories"] == 1 and real.get("imported_episodes") == 0, real
    assert any(e.startswith("Line 2:") and "not imported" in e for e in real.get("errors", [])), real
    rows = await db.execute_fetchall("SELECT content FROM memories WHERE agent_id = ?", (AGENT,))
    assert [r[0] for r in rows] == ["a good memory that must be restored"]


@pytest.mark.asyncio
async def test_a_numeric_recall_count_string_and_a_null_stamp_still_import(db, tmp_path):
    """Guards the fix: the values the old binding accepted keep being accepted."""
    path = _write_jsonl(
        tmp_path / "ok.jsonl",
        [
            _memory("m1", recall_count="5", created_at=None, last_recalled_at=None),
            _memory("m2", recall_count=3, created_at="2025-01-01 00:00:00", last_recalled_at="2025-02-01 00:00:00"),
            _episode("e1", created_at=None, start_time=None, end_time=None),
        ],
    )
    res = await admin_handlers.do_import_memories(path)
    assert _tally(res) == (True, 2, 0, 1, 0), res
    assert "errors" not in res, res
    rows = await db.execute_fetchall(
        "SELECT content, recall_count, created_at, last_recalled_at FROM memories ORDER BY content"
    )
    assert rows[0][1] == 5 and rows[0][2] and rows[0][3] is None
    assert tuple(rows[1]) == ("m2", 3, "2025-01-01 00:00:00", "2025-02-01 00:00:00")


@pytest.mark.asyncio
@pytest.mark.parametrize("line", ["[1, 2]", '"text"', "17", "[" * 100000 + "]" * 100000])
async def test_a_line_that_is_not_an_object_is_reported_per_line(db, tmp_path, line):
    path = tmp_path / "shapes.jsonl"
    path.write_text(json.dumps(_memory("kept")) + "\n" + line + "\n", encoding="utf-8")
    for dry_run in (True, False):
        res = await admin_handlers.do_import_memories(str(path), dry_run=dry_run)
        assert res["ok"] is True and res["imported_memories"] == 1, res
        assert any(e.startswith("Line 2:") for e in res.get("errors", [])), res


@pytest.mark.asyncio
async def test_a_non_string_agent_id_refuses_its_line(db, tmp_path):
    path = _write_jsonl(tmp_path / "aid.jsonl", [_memory("kept"), _memory("bad", agent_id={"a": 1})])
    for dry_run in (True, False):
        res = await admin_handlers.do_import_memories(path, dry_run=dry_run)
        assert res["ok"] is True and res["imported_memories"] == 1, res
        assert any(e.startswith("Line 2:") and "agent_id" in e for e in res.get("errors", [])), res


@pytest.mark.asyncio
async def test_a_bad_profile_stamp_refuses_the_profile_not_the_restore(db, tmp_path):
    path = _write_jsonl(
        tmp_path / "prof.jsonl",
        [
            _memory("kept"),
            {"_type": "profile", "agent_id": AGENT, "user_id": "", "content": "p", "updated_at": {"a": 1}},
        ],
    )
    preview = await admin_handlers.do_import_memories(path, dry_run=True)
    real = await admin_handlers.do_import_memories(path)
    assert real["ok"] is True and real["imported_memories"] == 1, real
    assert real["profile_updated"] is False and preview["profile_updated"] is False, (preview, real)
    assert any(e.startswith("Line 2:") and "updated_at" in e for e in real.get("errors", [])), real


# ---------------------------------------------------------------------------
# bug-620: several agents' profiles imported into one target do not collapse silently.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False])
async def test_profiles_colliding_on_one_target_keep_the_first_and_report_the_rest(db, tmp_path, dry_run):
    path = _write_jsonl(
        tmp_path / "all.jsonl",
        [
            {"_type": "profile", "agent_id": "alpha", "user_id": "", "content": "Alpha profile: concise"},
            {"_type": "profile", "agent_id": "beta", "user_id": "", "content": "Beta profile: verbose"},
        ],
    )
    res = await admin_handlers.do_import_memories(path, target_agent_id="zeta", dry_run=dry_run)
    assert res["ok"] is True and res["profile_updated"] is True, res
    notes = [e for e in res.get("errors", []) if e.startswith("Line 2:") and "profile" in e]
    assert len(notes) == 1 and "from line 1" in notes[0], res
    rows = await db.execute_fetchall("SELECT content FROM profiles WHERE agent_id = 'zeta'")
    assert [r[0] for r in rows] == ([] if dry_run else ["Alpha profile: concise"])


@pytest.mark.asyncio
async def test_profiles_of_distinct_users_still_all_import(db, tmp_path):
    """Guards the fix: only a collision on the target's (agent, user) key is refused."""
    path = _write_jsonl(
        tmp_path / "users.jsonl",
        [
            {"_type": "profile", "agent_id": "alpha", "user_id": "u1", "content": "first"},
            {"_type": "profile", "agent_id": "beta", "user_id": "u2", "content": "second"},
        ],
    )
    res = await admin_handlers.do_import_memories(path, target_agent_id="zeta")
    assert res["ok"] is True and "errors" not in res, res
    rows = await db.execute_fetchall("SELECT user_id, content FROM profiles WHERE agent_id = 'zeta' ORDER BY user_id")
    assert [tuple(r) for r in rows] == [("u1", "first"), ("u2", "second")]

