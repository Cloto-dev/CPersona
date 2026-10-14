"""Import and export: what a refusal names, how much an import reports, and the guards
around the file paths and the move.

    - The export directory admits a path only inside it. A sibling directory whose name
      merely starts with the root's name (`exports-old`, `exports2`) is outside it, for
      the write an export makes and for the read an import makes.
    - The import size refusal names the environment variable that sets the cap, and the
      configuration reference documents it, so an operator who follows the message
      changes the setting that is read.
    - An import's `errors` list is bounded however many lines of the file are bad: a
      file of short non-export lines used to answer with one message per line, a
      response some 24 times the size of the file.
    - A move that copies some profiles or episodes and skips others deletes only the
      ones it copied. A skipped row exists nowhere else, so deleting it is data loss.
"""

import json
import re
from pathlib import Path

import pytest
import pytest_asyncio

from cpersona import admin_handlers, config, session
from cpersona.database import get_db

REPO = Path(__file__).resolve().parent.parent
TABLES = ("memories", "episodes", "profiles", "pending_memory_tasks")


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


# ---------------------------------------------------------------------------
# The export directory: a sibling that shares the root's name as a prefix is outside.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sibling_directory_sharing_the_root_prefix_is_outside_the_export_dir(db, tmp_path, monkeypatch):
    await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp) VALUES ('w01', 'row', '{}', 't')"
    )
    await db.commit()
    root = tmp_path / "exports"
    old = tmp_path / "exports-old"
    two = tmp_path / "exports2"
    for d in (root, old, two):
        d.mkdir()
    monkeypatch.setattr(config, "EXPORT_DIR", str(root))

    # Control: a path inside the root is accepted, so a rejection below is the guard.
    inside = await admin_handlers.do_export_memories("w01", str(root / "in.jsonl"))
    assert inside.get("ok") is not False, inside
    assert (root / "in.jsonl").exists()
    inside_import = await admin_handlers.do_import_memories(str(root / "in.jsonl"), dry_run=True)
    assert inside_import["ok"] is True, inside_import

    # The write escape: an export into a directory that only shares the prefix.
    escaped = old / "x.jsonl"
    res = await admin_handlers.do_export_memories("w01", str(escaped))
    assert res.get("ok") is False and "outside export dir" in res.get("error", ""), res
    assert not escaped.exists(), "export wrote into a sibling directory outside the export dir"
    assert admin_handlers.export_file_owner(str(escaped)) is None

    # The read escape: an import from a directory that only shares the prefix.
    src = _write_jsonl(two / "any.jsonl", [{"_type": "memory", "agent_id": "w01imp", "content": "smuggled"}])
    res = await admin_handlers.do_import_memories(src, dry_run=True)
    assert res.get("ok") is False and "outside export dir" in res.get("error", ""), res
    assert admin_handlers.import_file_agents(src) == set(), "the ACL pre-read accepted a sibling directory"
    res = await admin_handlers.do_import_memories(src)
    assert res.get("ok") is False, res
    rows = await db.execute_fetchall("SELECT COUNT(*) FROM memories WHERE agent_id = 'w01imp'")
    assert rows[0][0] == 0


# ---------------------------------------------------------------------------
# The import size refusal names the setting that is read.
# ---------------------------------------------------------------------------

_SETTING = "CPERSONA_MAX_IMPORT_BYTES"


def _oversized(tmp_path: Path) -> str:
    return _write_jsonl(tmp_path / "big.jsonl", [{"_type": "memory", "agent_id": "w01", "content": "x" * 64}])


def _assert_names_the_read_setting(res: dict) -> None:
    assert res["ok"] is False, res
    named = set(re.findall(r"\b[A-Z0-9_]*MAX_IMPORT_BYTES\b", res["error"]))
    assert named == {_SETTING}, f"the refusal names {named or 'no setting'}: {res['error']}"
    # And that is the name config reads, not one that merely resembles it.
    assert f'"{_SETTING}"' in (REPO / "cpersona" / "config.py").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_size_refusal_before_reading_names_the_env_setting(db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MAX_IMPORT_BYTES", 16)
    _assert_names_the_read_setting(await admin_handlers.do_import_memories(_oversized(tmp_path), dry_run=True))


@pytest.mark.asyncio
async def test_size_refusal_of_the_bounded_read_names_the_env_setting(db, tmp_path, monkeypatch):
    # The second refusal covers a file that grew after the stat; let it past the stat.
    path = _oversized(tmp_path)
    monkeypatch.setattr(admin_handlers, "_validate_import_preconditions", lambda p, d, k="": (p, None))
    monkeypatch.setattr(config, "MAX_IMPORT_BYTES", 16)
    _assert_names_the_read_setting(await admin_handlers.do_import_memories(path, dry_run=True))


@pytest.mark.parametrize("page", ["configuration.md", "configuration.ja.md", "operations.md", "operations.ja.md"])
def test_import_cap_is_documented(page):
    text = (REPO / "docs" / page).read_text(encoding="utf-8")
    assert f"`{_SETTING}`" in text, f"docs/{page} does not name {_SETTING}"


# ---------------------------------------------------------------------------
# An import's error report is bounded.
# ---------------------------------------------------------------------------


# How many messages an import lists before it counts the rest.
ERRORS_SHOWN = 100


def _bad_lines(path: Path, n: int, header: dict | None = None) -> str:
    with open(path, "w", encoding="utf-8") as fh:
        if header is not None:
            fh.write(json.dumps(header) + "\n")
        fh.write("ab\n" * n)
    return str(path)


def _assert_bounded(errors: list, bad: int, first_line: int) -> None:
    shown = ERRORS_SHOWN
    assert len(errors) == shown + 1, f"{len(errors)} messages for {bad} bad lines"
    # The first messages are kept as they were, in file order.
    assert errors[0].startswith(f"Line {first_line}: invalid JSON"), errors[0]
    assert errors[shown - 1].startswith(f"Line {first_line + shown - 1}: invalid JSON"), errors[shown - 1]
    # The rest are counted, not dropped silently.
    assert errors[-1] == f"{bad - shown} more errors were not listed", errors[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False])
async def test_import_error_list_is_bounded_on_success(db, tmp_path, dry_run):
    bad = 5000
    res = await admin_handlers.do_import_memories(_bad_lines(tmp_path / "log.txt", bad), dry_run=dry_run)
    assert res["ok"] is True and res["imported_memories"] == 0, {k: v for k, v in res.items() if k != "errors"}
    _assert_bounded(res["errors"], bad, first_line=1)
    assert len(json.dumps(res)) < 50_000


@pytest.mark.asyncio
async def test_import_error_list_is_bounded_on_abort(db, tmp_path):
    bad = 5000
    header = {"_type": "header", "memory_count": 3, "episode_count": 0, "profile_count": 0}
    res = await admin_handlers.do_import_memories(_bad_lines(tmp_path / "torn.jsonl", bad, header))
    assert res["ok"] is False and "truncated" in res["error"], {k: v for k, v in res.items() if k != "errors"}
    _assert_bounded(res["errors"], bad, first_line=2)


@pytest.mark.asyncio
async def test_import_error_list_under_the_bound_is_unchanged(db, tmp_path):
    bad = ERRORS_SHOWN
    res = await admin_handlers.do_import_memories(_bad_lines(tmp_path / "few.txt", bad), dry_run=True)
    assert len(res["errors"]) == bad
    assert all(e.startswith(f"Line {i}: invalid JSON") for i, e in enumerate(res["errors"], 1))


# ---------------------------------------------------------------------------
# A move deletes only what it copied, when it both copies and skips.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_move_keeps_the_skipped_profile_when_another_is_copied(db):
    await db.execute("INSERT INTO profiles (agent_id, user_id, content) VALUES ('src', '', 'src default profile')")
    await db.execute("INSERT INTO profiles (agent_id, user_id, content) VALUES ('src', 'alice', 'src alice profile')")
    await db.execute("INSERT INTO profiles (agent_id, user_id, content) VALUES ('dst', '', 'dst default profile')")
    await db.commit()

    res = await admin_handlers.do_merge_memories("src", "dst", mode="move")
    assert res["ok"] and res["profile_copied"] is True and res["skipped_profile"] is True, res
    assert res["source_deleted"]["deleted_profiles"] == 1, res
    assert res["left_at_source"]["profiles"] == 1, res
    rows = await db.execute_fetchall("SELECT agent_id, user_id, content FROM profiles ORDER BY agent_id, user_id")
    assert rows == [
        ("dst", "", "dst default profile"),
        ("dst", "alice", "src alice profile"),
        ("src", "", "src default profile"),
    ], rows


@pytest.mark.asyncio
async def test_move_keeps_the_skipped_episode_when_another_is_copied(db):
    # Skipped: the target holds the same summary, but the source copy carries other
    # keywords, so the source row is reproduced nowhere.
    await db.execute("INSERT INTO episodes (agent_id, summary, keywords) VALUES ('src', 'shared summary', 'src-kw')")
    await db.execute("INSERT INTO episodes (agent_id, summary, keywords) VALUES ('src', 'unique summary', 'k')")
    await db.execute("INSERT INTO episodes (agent_id, summary, keywords) VALUES ('dst', 'shared summary', 'dst-kw')")
    await db.commit()

    res = await admin_handlers.do_merge_memories("src", "dst", mode="move")
    assert res["ok"] and res["merged_episodes"] == 1 and res["skipped_episodes"] == 1, res
    assert res["source_deleted"]["deleted_episodes"] == 1, res
    assert res["left_at_source"]["episodes"] == 1, res
    rows = await db.execute_fetchall("SELECT agent_id, summary, keywords FROM episodes ORDER BY agent_id, summary")
    assert rows == [
        ("dst", "shared summary", "dst-kw"),
        ("dst", "unique summary", "k"),
        ("src", "shared summary", "src-kw"),
    ], rows
