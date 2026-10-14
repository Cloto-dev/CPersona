"""Regression tests for export_memories / import_memories.

Three rules are pinned here:

1. With CPERSONA_EXPORT_DIR set, the export directory is shared by every agent
   and nothing in a file's name ties it to the agent that wrote it. The ACL
   demand therefore follows the file: import_memories demands read on every
   agent the file names (its header and its records) on top of write on the
   target, and export_memories demands write on the agent an existing file at
   the destination belongs to before it replaces that file.
2. A JSON null in a memory's msg_id / project_id / channel / timestamp, or in an
   episode's or profile's axes, is read as the empty value a missing key gets,
   on both arms. A memory timestamp that is not a string is a per-line error,
   and an INSERT that the database ignores for a reason other than the two
   dedup keys is reported per line instead of being counted as a duplicate.
3. A restore does not overwrite a stored profile that is newer than the file's
   copy; it keeps the stored row and says so per line.
"""

import os
import tempfile

os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "test_2612a1_u02.db"))
os.environ.setdefault("CPERSONA_EMBEDDING_MODE", "none")

import json  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from cpersona import acl, config, server, session  # noqa: E402
from cpersona import admin_handlers as admin  # noqa: E402
from cpersona.database import get_db  # noqa: E402

ALPHA = "u02.alpha"
BETA = "u02.beta"
GAMMA = "u02.gamma"
_AGENTS = (ALPHA, BETA, GAMMA)
SECRET = "beta secret: the vault code is 4417 zebraquokka"


async def _wipe(db, agents=_AGENTS):
    for table in ("memories", "episodes", "profiles"):
        for agent in agents:
            await db.execute(f"DELETE FROM {table} WHERE agent_id = ?", (agent,))
    await db.commit()


def _write_jsonl(path, records):
    with open(path, "w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")
    return str(path)


def _read_jsonl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


# ==========================================================================
# 1. The shared export directory: a file belongs to the agents it names.
# ==========================================================================


@pytest_asyncio.fixture
async def tenants(tmp_path, monkeypatch):
    """ACL active, EXPORT_DIR confined to a fresh directory, three clients.

    ``ca`` may write alpha only, ``cb`` may write beta only, ``ops`` may write
    every agent. The confined directory is the one all of them share.
    """
    exports = tmp_path / "exports"
    exports.mkdir()
    monkeypatch.setattr(config, "EXPORT_DIR", str(exports))
    session.reset_pauses_for_tests()
    db = await get_db()
    await _wipe(db)
    acl.activate(
        acl.AclConfig(
            grants_by_client={
                "ca": {ALPHA: acl.PERM_WRITE},
                "cb": {BETA: acl.PERM_WRITE},
                "ops": {"*": acl.PERM_WRITE},
            },
            token_entries=(("tok-ca", "ca"), ("tok-cb", "cb"), ("tok-ops", "ops")),
        )
    )
    try:
        yield exports
    finally:
        acl.activate(None)
        await _wipe(db)


async def _call(client, tool, args):
    """Call the served (guarded) handler as ``client``."""
    handler = server.registry._handlers[tool]
    assert getattr(handler, "_acl_guarded", False), f"{tool} is not behind the ACL guard"
    token = acl.set_principal(acl.Principal(client))
    try:
        return await handler(args)
    finally:
        acl.reset_principal(token)


async def _contents(agent):
    db = await get_db()
    rows = await db.execute_fetchall("SELECT content FROM memories WHERE agent_id = ?", (agent,))
    return [r[0] for r in rows]


async def _beta_backup(exports):
    """beta stores a secret and exports itself into the shared directory."""
    stored = await _call("cb", "store", {"agent_id": BETA, "content": SECRET})
    assert stored.get("result") == "stored", stored
    path = str(exports / "beta.jsonl")
    exported = await _call("cb", "export_memories", {"agent_id": BETA, "output_path": path})
    assert exported.get("ok") is True and exported.get("memories") == 1, exported
    return path


@pytest.mark.asyncio
async def test_import_of_another_agents_export_needs_read_on_that_agent(tenants):
    beta_file = await _beta_backup(tenants)

    # Control: every direct route from ca to beta is refused, so a leak below
    # can only have come through the file.
    direct = await _call("ca", "list_memories", {"agent_id": BETA})
    assert direct.get("error") == "permission_denied", direct

    for dry_run in (True, False):
        result = await _call(
            "ca",
            "import_memories",
            {"input_path": beta_file, "target_agent_id": ALPHA, "dry_run": dry_run},
        )
        assert result.get("error") == "permission_denied", (dry_run, result)
        assert result.get("agent_id") == BETA and result.get("required") == "read", result

    assert not any(SECRET in c for c in await _contents(ALPHA)), (
        "beta's export was imported under alpha by a client that may not read beta"
    )

    # The rule follows the grant, not the directory: a client that may read
    # beta can still restore beta's file anywhere it may write.
    restored = await _call("ops", "import_memories", {"input_path": beta_file, "target_agent_id": ALPHA})
    assert restored.get("ok") is True and restored.get("imported_memories") == 1, restored
    # And beta's own client restores its own file.
    own = await _call(
        "cb", "import_memories", {"input_path": beta_file, "target_agent_id": BETA, "dry_run": True}
    )
    assert own.get("ok") is True, own


@pytest.mark.asyncio
async def test_import_of_ones_own_export_is_unaffected(tenants):
    stored = await _call("ca", "store", {"agent_id": ALPHA, "content": "alpha's own row"})
    assert stored.get("result") == "stored", stored
    path = str(tenants / "alpha.jsonl")
    exported = await _call("ca", "export_memories", {"agent_id": ALPHA, "output_path": path})
    assert exported.get("ok") is True, exported
    result = await _call("ca", "import_memories", {"input_path": path, "target_agent_id": ALPHA})
    assert result.get("ok") is True and result.get("skipped_memories") == 1, result


@pytest.mark.asyncio
async def test_export_does_not_replace_a_file_another_agent_wrote(tenants):
    beta_file = await _beta_backup(tenants)
    stored = await _call("ca", "store", {"agent_id": ALPHA, "content": "alpha's own row"})
    assert stored.get("result") == "stored", stored

    result = await _call("ca", "export_memories", {"agent_id": ALPHA, "output_path": beta_file})
    assert result.get("error") == "permission_denied", result
    assert result.get("agent_id") == BETA and result.get("required") == "read-write", result

    after = _read_jsonl(beta_file)
    assert after[0].get("agent_id") == BETA, f"beta's backup now carries {after[0]!r}"
    assert [r.get("content") for r in after if r.get("_type") == "memory"] == [SECRET]

    # ca may still write and re-write its own file.
    alpha_file = str(tenants / "alpha.jsonl")
    for _ in range(2):
        mine = await _call("ca", "export_memories", {"agent_id": ALPHA, "output_path": alpha_file})
        assert mine.get("ok") is True, mine
    # A client that may write beta may replace beta's file.
    ops = await _call("ops", "export_memories", {"agent_id": ALPHA, "output_path": beta_file})
    assert ops.get("ok") is True, ops


@pytest.mark.asyncio
async def test_export_does_not_replace_a_file_no_export_header_claims(tenants):
    """A file that is not an export cannot be shown to be the caller's."""
    other = tenants / "ledger.json"
    other.write_text('{"issued": {"someone": "u-1"}}\n', encoding="utf-8")
    stored = await _call("ca", "store", {"agent_id": ALPHA, "content": "alpha's own row"})
    assert stored.get("result") == "stored", stored

    result = await _call("ca", "export_memories", {"agent_id": ALPHA, "output_path": str(other)})
    assert result.get("error") == "permission_denied", result
    assert result.get("agent_id") == "*", result
    # The caller did scope the call; the denial names the file as the cause
    # rather than telling it to pass agent_id.
    assert "belongs to every agent" in result.get("detail", ""), result
    assert "no agent scope was sent" not in result.get("detail", ""), result
    assert other.read_text(encoding="utf-8") == '{"issued": {"someone": "u-1"}}\n'


@pytest.mark.asyncio
async def test_a_per_subject_client_cannot_restore_another_subjects_export(tmp_path, monkeypatch):
    """The same rule under per_subject: "@me" reaches the caller's alias, and the
    file of another subject's alias is that alias's, not the caller's."""
    from cpersona import aliases

    issuer = "https://auth.example.com"
    client = f"oauth:{issuer}:u02-client"
    exports = tmp_path / "exports"
    exports.mkdir()
    monkeypatch.setattr(config, "EXPORT_DIR", str(exports))
    session.reset_pauses_for_tests()
    acl.activate(
        acl.AclConfig(
            grants_by_client={client: {"*": acl.PERM_WRITE}},
            token_entries=(),
            per_subject_clients=frozenset({client}),
        )
    )
    ledger = aliases.AliasLedger(str(tmp_path / "alias_ledger.json"))
    acl.activate_ledger(ledger)

    async def as_subject(subject, tool, args):
        token = acl.set_principal(acl.Principal(client_id=client, issuer=issuer, subject=subject))
        try:
            return await server.registry._handlers[tool](args)
        finally:
            acl.reset_principal(token)

    issued = []
    try:
        stored = await as_subject("user-2", "store", {"agent_id": "@me", "content": SECRET})
        assert stored.get("result") == "stored", stored
        issued.append(stored["resolved_agent_id"])
        theirs = str(exports / "user-2.jsonl")
        exported = await as_subject("user-2", "export_memories", {"agent_id": "@me", "output_path": theirs})
        assert exported.get("ok") is True, exported

        mine = await as_subject("user-1", "list_memories", {"agent_id": "@me"})
        issued.append(mine["resolved_agent_id"])
        result = await as_subject("user-1", "import_memories", {"input_path": theirs, "target_agent_id": "@me"})
        assert result.get("error") == "permission_denied", result
        assert not any(SECRET in c for c in await _contents(issued[1]))

        overwrite = await as_subject("user-1", "export_memories", {"agent_id": "@me", "output_path": theirs})
        assert overwrite.get("error") == "permission_denied", overwrite
        assert _read_jsonl(theirs)[0].get("agent_id") == issued[0]
    finally:
        acl.activate(None)
        acl.activate_ledger(None)
        await _wipe(await get_db(), tuple(issued))


def test_import_demand_names_every_agent_in_the_file(tmp_path, monkeypatch):
    exports = tmp_path / "exports"
    exports.mkdir()
    demands = acl.ACL_CLASSIFICATION["import_memories"]
    two_agents = _write_jsonl(
        exports / "two.jsonl",
        [
            {"_type": "header", "agent_id": BETA, "memory_count": 1, "episode_count": 1},
            {"_type": "memory", "agent_id": BETA, "content": "b"},
            {"_type": "episode", "agent_id": GAMMA, "summary": "g"},
            {"_type": "memory", "agent_id": ALPHA, "content": "a"},
        ],
    )
    every_agent = _write_jsonl(
        exports / "all.jsonl",
        [{"_type": "header", "agent_id": ""}, {"_type": "memory", "agent_id": BETA, "content": "b"}],
    )
    monkeypatch.setattr(config, "EXPORT_DIR", str(exports))
    assert demands({"input_path": two_agents, "target_agent_id": ALPHA}) == [
        (ALPHA, acl.PERM_WRITE),
        (BETA, acl.PERM_READ),
        (GAMMA, acl.PERM_READ),
    ]
    # An export of every agent (empty header agent_id) belongs to every agent.
    assert demands({"input_path": every_agent, "target_agent_id": ALPHA}) == [
        (ALPHA, acl.PERM_WRITE),
        ("*", acl.PERM_READ),
    ]
    # Unchanged without the confinement: the all-agents write already covers it.
    monkeypatch.setattr(config, "EXPORT_DIR", "")
    assert demands({"input_path": two_agents, "target_agent_id": ALPHA}) == [("*", acl.PERM_WRITE)]


# ==========================================================================
# 2. JSON null in a NOT NULL column (bug-527).
# ==========================================================================

NULLS = "u02.nulls"


@pytest_asyncio.fixture
async def clean_nulls(monkeypatch):
    monkeypatch.setattr(config, "EXPORT_DIR", "")
    session.reset_pauses_for_tests()
    db = await get_db()
    await _wipe(db, (NULLS,))
    yield db
    await _wipe(db, (NULLS,))


async def _count(db, table, agent=NULLS):
    rows = await db.execute_fetchall(f"SELECT COUNT(*) FROM {table} WHERE agent_id = ?", (agent,))
    return rows[0][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["channel", "project_id", "msg_id", "timestamp"])
async def test_null_memory_field_is_imported_on_both_arms(clean_nulls, tmp_path, field):
    good = {"_type": "memory", "agent_id": NULLS, "content": f"good row {field}", "timestamp": "2026-01-01T00:00:00Z"}
    null = dict(good, content=f"null {field} row", **{field: None})
    path = _write_jsonl(tmp_path / f"null_{field}.jsonl", [good, null])

    dry = await admin.do_import_memories(path, dry_run=True)
    real = await admin.do_import_memories(path)
    assert dry.get("ok") is True and real.get("ok") is True, (dry, real)
    assert dry["imported_memories"] == real["imported_memories"] == 2, (dry, real)
    assert real["skipped_memories"] == 0, real
    assert await _count(clean_nulls, "memories") == 2

    # The null is read as the value a missing key gets.
    rows = await clean_nulls.execute_fetchall(
        f"SELECT {field} FROM memories WHERE agent_id = ? AND content = ?", (NULLS, f"null {field} row")
    )
    assert rows == [("",)], rows


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["channel", "project_id"])
async def test_null_episode_axis_does_not_abort_the_restore(clean_nulls, tmp_path, field):
    episode = {"_type": "episode", "agent_id": NULLS, "summary": f"an episode {field}", field: None}
    path = _write_jsonl(tmp_path / f"ep_null_{field}.jsonl", [episode])

    dry = await admin.do_import_memories(path, dry_run=True)
    real = await admin.do_import_memories(path)
    assert dry.get("ok") is True and real.get("ok") is True, (dry, real)
    assert dry["imported_episodes"] == real["imported_episodes"] == 1, (dry, real)
    assert await _count(clean_nulls, "episodes") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["project_id", "user_id"])
async def test_null_profile_key_does_not_abort_the_restore(clean_nulls, tmp_path, field):
    profile = {"_type": "profile", "agent_id": NULLS, "content": "a profile", field: None}
    path = _write_jsonl(tmp_path / f"prof_null_{field}.jsonl", [profile])

    real = await admin.do_import_memories(path)
    assert real.get("ok") is True and real.get("profile_updated") is True, real
    assert await _count(clean_nulls, "profiles") == 1


@pytest.mark.asyncio
async def test_non_string_memory_timestamp_is_a_per_line_error(clean_nulls, tmp_path):
    good = {"_type": "memory", "agent_id": NULLS, "content": "dated", "timestamp": "2026-01-01T00:00:00Z"}
    numeric = {"_type": "memory", "agent_id": NULLS, "content": "numeric stamp", "timestamp": 1700000000}
    path = _write_jsonl(tmp_path / "numeric_ts.jsonl", [good, numeric])

    dry = await admin.do_import_memories(path, dry_run=True)
    real = await admin.do_import_memories(path)
    for result in (dry, real):
        assert result.get("ok") is True, result
        assert result["imported_memories"] == 1 and result["skipped_memories"] == 1, result
        assert any(e.startswith("Line 2:") and "timestamp" in e for e in result.get("errors", [])), result
    assert await _count(clean_nulls, "memories") == 1


@pytest.mark.asyncio
async def test_an_insert_ignored_for_another_reason_is_not_a_silent_duplicate(clean_nulls, tmp_path):
    """INSERT OR IGNORE also swallows constraint failures that are not the dedup
    keys. Both keys are probed before the insert, so a row it still drops must be
    reported, not counted as a duplicate with nothing said."""
    db = clean_nulls
    await db.execute(
        "CREATE TEMP TRIGGER u02_drop_marked BEFORE INSERT ON main.memories"
        " WHEN NEW.content = 'dropped by the database' BEGIN SELECT RAISE(IGNORE); END"
    )
    try:
        path = _write_jsonl(
            tmp_path / "ignored.jsonl",
            [{"_type": "memory", "agent_id": NULLS, "content": "dropped by the database", "timestamp": "2026-01-01"}],
        )
        real = await admin.do_import_memories(path)
    finally:
        await db.execute("DROP TRIGGER IF EXISTS temp.u02_drop_marked")
        await db.commit()
    assert real.get("ok") is True, real
    assert real["imported_memories"] == 0, real
    assert any(e.startswith("Line 1:") and "not stored" in e for e in real.get("errors", [])), real


# ==========================================================================
# 3. A restore keeps a stored profile that is newer than the file's (bug-528).
# ==========================================================================

DRILL = "u02.drill"
OLD_STAMP = "2026-08-01 00:00:00"
NEW_STAMP = "2026-10-01 00:00:00"
V1 = "Drill profile v1: prefers concise answers"
V2 = "Drill profile v2: switched to detailed answers"


@pytest_asyncio.fixture
async def clean_drill(monkeypatch):
    monkeypatch.setattr(config, "EXPORT_DIR", "")
    session.reset_pauses_for_tests()
    db = await get_db()
    await _wipe(db, (DRILL,))
    yield db
    await _wipe(db, (DRILL,))


async def _profile(db):
    rows = await db.execute_fetchall(
        "SELECT content, updated_at FROM profiles WHERE agent_id = ? AND user_id = ''", (DRILL,)
    )
    assert len(rows) == 1, rows
    return tuple(rows[0])


async def _set_profile(db, content, stamp):
    await db.execute(
        "INSERT INTO profiles (agent_id, project_id, user_id, content, updated_at) VALUES (?, '', '', ?, ?)"
        " ON CONFLICT(agent_id, user_id) DO UPDATE SET content = excluded.content, updated_at = excluded.updated_at",
        (DRILL, content, stamp),
    )
    await db.commit()


@pytest.mark.asyncio
async def test_restore_drill_keeps_the_newer_stored_profile(clean_drill, tmp_path):
    db = clean_drill
    await _set_profile(db, V1, OLD_STAMP)
    backup = str(tmp_path / "backup.jsonl")
    exported = await admin.do_export_memories(DRILL, backup)
    assert exported.get("profiles") == 1, exported
    # The live profile moves on after the backup.
    await _set_profile(db, V2, NEW_STAMP)

    dry = await admin.do_import_memories(backup, target_agent_id=DRILL, dry_run=True)
    real = await admin.do_import_memories(backup, target_agent_id=DRILL)
    assert await _profile(db) == (V2, NEW_STAMP), "the restore replaced a newer stored profile"
    for result in (dry, real):
        assert result.get("ok") is True, result
        assert result.get("profile_updated") is False, result
        assert any("newer" in e and "kept" in e for e in result.get("errors", [])), result


@pytest.mark.asyncio
async def test_restore_still_replaces_an_older_stored_profile(clean_drill, tmp_path):
    db = clean_drill
    await _set_profile(db, V2, NEW_STAMP)
    backup = str(tmp_path / "backup.jsonl")
    await admin.do_export_memories(DRILL, backup)
    await _set_profile(db, V1, OLD_STAMP)

    real = await admin.do_import_memories(backup, target_agent_id=DRILL)
    assert real.get("ok") is True and real.get("profile_updated") is True, real
    assert await _profile(db) == (V2, NEW_STAMP)
    # Re-importing the same file leaves the same state and says nothing new.
    again = await admin.do_import_memories(backup, target_agent_id=DRILL)
    assert again.get("profile_updated") is True and not again.get("errors"), again
    assert await _profile(db) == (V2, NEW_STAMP)
