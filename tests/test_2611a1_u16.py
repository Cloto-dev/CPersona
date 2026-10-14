"""Regression tests for the 2.6.11 isolation fixes.

Five rules, each pinned on the path a caller actually reaches:

  source_id       A per-user ``source_id`` matches one id exactly. Only a value
                  that ends in a character other than a letter or digit
                  (``discord:``, ``docs/``) is a namespace prefix. The SQL arms
                  and the contiguous index answer the same question.
  content dedup   A text already stored under a different source id is not
                  answered with that source's row: the caller is told its own
                  copy was not written, and the lock rider cannot reach the
                  other source's row.
  reserved names  While some ACL row declares per_subject, no runtime write may
                  add rows under a ``u-`` agent the alias ledger has not issued,
                  so a running server cannot put the database into a state the
                  next boot refuses.
  bug-531         The alias ledger follows its file: an operator's relink or
                  removal takes effect on the next call, and a removed row is
                  not written back by a later issuance.
  check_health    An agent-scoped health report names no absolute server path;
                  the unscoped (operator) report keeps them.
"""

import json
import os
import tempfile

os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "test_2611a1_u16.db"))
os.environ.setdefault("CPERSONA_EMBEDDING_MODE", "none")

import errno  # noqa: E402

import numpy as np  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from cpersona import (  # noqa: E402
    acl,
    admin_handlers,
    aliases,
    checks,
    fileperms,
    maintenance_handlers,
    memory_handlers,
    operating_context,
    server,
    session,
    vector_index,
)
from cpersona.database import get_db  # noqa: E402
from cpersona.isolation import source_id_where  # noqa: E402

STAMP = "2026-01-01T00:00:00+00:00"


@pytest_asyncio.fixture
async def db():
    session.reset_pauses_for_tests()
    conn = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()
    yield conn
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()


async def _insert(db, agent, content, source_id, *, embedding=None):
    await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp, created_at, embedding)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (agent, content, json.dumps({"type": "User", "id": source_id}), STAMP, "2026-01-01 00:00:00", embedding),
    )
    await db.commit()


# ==========================================================================
# source_id: one id exactly, a namespace only when the value ends in a
# delimiter.
# ==========================================================================
_ALICE, _BOB = "discord:12345", "discord:123456"


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["", "home address"])
async def test_a_per_user_source_id_does_not_admit_an_id_that_extends_it(db, query):
    """'discord:12345' is one user; 'discord:123456' is another whose id merely
    starts with the same characters. Both the keyword arm and the empty-query
    recency arm read the shared predicate."""
    agent = "srcid.agent"
    await _insert(db, agent, "alice home address is elm street", _ALICE)
    await _insert(db, agent, "bob home address is oak avenue", _BOB)

    rows = await memory_handlers._search_memories_keyword(db, agent, query, 10, source_id=_ALICE)
    assert [r["content"] for r in rows] == ["alice home address is elm street"], (
        f"a recall scoped to {_ALICE} returned another user's row: {[r['content'] for r in rows]}"
    )
    # The reverse scope was always exact; it stays so.
    rows = await memory_handlers._search_memories_keyword(db, agent, query, 10, source_id=_BOB)
    assert [r["content"] for r in rows] == ["bob home address is oak avenue"]


@pytest.mark.asyncio
async def test_do_recall_scoped_to_one_user_returns_only_that_user(db):
    agent = "srcid.recall"
    await memory_handlers.do_store(
        agent, {"content": "alice home address is elm street", "source": {"type": "User", "id": _ALICE}}
    )
    await memory_handlers.do_store(
        agent, {"content": "bob home address is oak avenue", "source": {"type": "User", "id": _BOB}}
    )
    result = await memory_handlers.do_recall(agent, "", 10, source_id=_ALICE)
    got = [(m["content"], m["source"]["id"]) for m in result["messages"]]
    assert got == [("alice home address is elm street", _ALICE)], got


@pytest.mark.asyncio
async def test_the_shared_predicate_matches_numeric_ids_exactly(db):
    """A JSON number id is compared as its text, as it always was, and exactly."""
    agent = "srcid.numeric"
    for uid, content in ((1234, "four digits"), (12345, "five digits"), (123456, "six digits")):
        await db.execute(
            "INSERT INTO memories (agent_id, content, source, timestamp) VALUES (?, ?, ?, ?)",
            (agent, content, json.dumps({"type": "User", "id": uid}), STAMP),
        )
    await db.commit()
    src = source_id_where("12345")
    rows = await db.execute_fetchall(
        f"SELECT content FROM memories WHERE agent_id = ?{src.and_clause} ORDER BY id",
        (agent, *src.params),
    )
    assert [r[0] for r in rows] == ["five digits"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_id, expected",
    [
        ("discord:", ["alice", "bob"]),  # a namespace: ends in ':'
        ("docs/", ["doc a", "doc a backup"]),  # a namespace: ends in '/'
        ("docs/a.md", ["doc a"]),  # one document, not its '.bak' sibling
        ("slack:", []),
    ],
)
async def test_a_value_ending_in_a_delimiter_is_still_a_namespace(db, source_id, expected):
    agent = "srcid.namespace"
    await _insert(db, agent, "alice", _ALICE)
    await _insert(db, agent, "bob", _BOB)
    await _insert(db, agent, "doc a", "docs/a.md")
    await _insert(db, agent, "doc a backup", "docs/a.md.bak")
    rows = await memory_handlers._search_memories_keyword(db, agent, "", 10, source_id=source_id)
    assert sorted(r["content"] for r in rows) == expected


@pytest.mark.asyncio
async def test_the_contiguous_index_applies_the_same_boundary(db, tmp_path):
    """The index resolves the axis on its own, against its string table; it must
    answer the question the SQL answers, or the two arms disagree on who a
    per-user recall is about."""
    agent = "srcid.index"
    for n, (uid, content) in enumerate(
        ((_ALICE, "alice"), (_BOB, "bob"), ("docs/a.md", "doc a"), ("docs/a.md.bak", "doc a backup"))
    ):
        blob = np.random.default_rng(n).standard_normal(6).astype(np.float32).tobytes()
        await _insert(db, agent, content, uid, embedding=blob)
    path = str(tmp_path / "memories.index")
    await vector_index.start_logging()
    built = await vector_index.build_index(db, "memories", path)
    assert built["built"], built
    index = vector_index.load_index("memories", path)

    def offered(source_id):
        positions = vector_index.select(
            index, agent_id=agent, project_id=None, channel="", source_id=source_id, limit=0
        )
        ids = [int(index.ids[p]) for p in positions]
        return ids

    async def admitted(source_id):
        src = source_id_where(source_id)
        rows = await db.execute_fetchall(
            f"SELECT id FROM memories WHERE agent_id = ?{src.and_clause}", (agent, *src.params)
        )
        return sorted(r[0] for r in rows)

    by_content = dict(await db.execute_fetchall("SELECT content, id FROM memories WHERE agent_id = ?", (agent,)))
    assert offered(_ALICE) == [by_content["alice"]], "the index offered another user's row"
    assert offered("docs/a.md") == [by_content["doc a"]], "the index offered the '.bak' sibling"
    for source_id in (_ALICE, "docs/a.md", "discord:", "docs/"):
        assert sorted(offered(source_id)) == await admitted(source_id), (
            f"index and SQL disagree on {source_id!r}"
        )


# ==========================================================================
# content dedup: a second source's identical text is not answered with the
# first source's row.
# ==========================================================================
_PEANUTS = "I am allergic to peanuts"


async def _store(source_id, *, lock=False, channel="555", project_id=""):
    return await server.do_store_boundary(
        "bot",
        channel=channel,
        project_id=project_id,
        content=_PEANUTS,
        source={"type": "User", "id": source_id, "name": source_id.split(":")[-1]},
        lock=lock,
    )


@pytest.mark.asyncio
async def test_an_identical_text_from_another_source_is_not_answered_with_its_row(db):
    alice = await _store("discord:alice")
    assert alice["result"] == "stored", alice

    bob = await _store("discord:bob", lock=True)
    # Bob is not told his memory is stored (or harmlessly skipped) when it is
    # Alice's row that answered, and is not handed her row's id.
    assert bob["result"] == "rejected" and bob["ok"] is False, bob
    assert bob.get("id") != alice["id"], bob
    assert bob.get("locked") is False, bob

    # The lock rider did not lock Alice's row, so she still owns it.
    rows = await db.execute_fetchall("SELECT locked FROM memories WHERE id = ?", (alice["id"],))
    assert rows == [(0,)], rows
    deleted = await admin_handlers.do_delete_memory(alice["id"], agent_id="bot")
    assert deleted.get("ok") is True, deleted


@pytest.mark.asyncio
async def test_the_same_text_from_the_same_source_is_still_a_harmless_skip(db):
    first = await _store("discord:alice")
    again = await _store("discord:alice")
    assert again["result"] == "skipped" and again.get("reason") == "duplicate content", again
    assert again["id"] == first["id"]


@pytest.mark.asyncio
async def test_another_sources_copy_is_written_where_the_bucket_allows_it(db):
    """Alice's row sits in the global pool; Bob writes into project 'X'. The two
    can coexist (the unique index is per exact bucket), so Bob's copy is stored
    and his own scoped recall finds it."""
    await _store("discord:alice", project_id="")
    bob = await _store("discord:bob", project_id="X")
    assert bob["result"] == "stored", bob
    recalled = await memory_handlers.do_recall(
        "bot", "", 10, channel="555", project_id="X", source_id="discord:bob"
    )
    assert [(m["content"], m["source"]["id"]) for m in recalled["messages"]] == [
        (_PEANUTS, "discord:bob")
    ]


# ==========================================================================
# reserved names: no runtime write adds rows under an unissued u- agent while
# per_subject is configured.
# ==========================================================================
_ISSUER = "https://auth.example.com"
_PS_CLIENT = f"oauth:{_ISSUER}:https://claude.ai/mcp-client"
_PLAIN_OAUTH = f"oauth:{_ISSUER}:https://other.example/client"
_SQUATTER = "u-0badc0dedead"


def _activate_acl(tmp_path, *, per_subject=True):
    clients = [
        {"client_id": _PLAIN_OAUTH, "token": None, "grants": {"*": "read-write"}},
        {"client_id": "static-admin", "token": "token-s", "grants": {"*": "read-write"}},
    ]
    if per_subject:
        clients.insert(
            0, {"client_id": _PS_CLIENT, "token": None, "grants": {"*": "read-write"}, "per_subject": True}
        )
    path = tmp_path / "acl.json"
    path.write_text(json.dumps({"clients": clients}), encoding="utf-8")
    os.chmod(path, 0o600)
    acl.activate(acl.load_config(str(path)))
    ledger = aliases.AliasLedger(str(tmp_path / "alias_ledger.json"))
    acl.activate_ledger(ledger)
    return ledger


@pytest.fixture
def acl_reset():
    yield
    acl.activate(None)
    acl.activate_ledger(None)


async def _call(client, tool, arguments, **principal):
    token = acl.set_principal(acl.Principal(client_id=client, **principal))
    try:
        return await server.registry._handlers[tool](arguments)
    finally:
        acl.reset_principal(token)


async def _agents_in_use(db) -> set:
    found = set()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        found.update(r[0] for r in await db.execute_fetchall(f"SELECT DISTINCT agent_id FROM {table}"))
    return found


@pytest.mark.asyncio
@pytest.mark.parametrize("client", ["static-admin", _PLAIN_OAUTH])
@pytest.mark.parametrize(
    "tool, arguments",
    [
        ("store", {"agent_id": _SQUATTER, "content": "admin note"}),
        ("archive_episode", {"agent_id": _SQUATTER, "summary": "an episode"}),
        ("update_profile", {"agent_id": _SQUATTER, "profile": "a profile"}),
        (
            "merge_memories",
            {"source_agent_id": "srcagent", "target_agent_id": _SQUATTER},
        ),
    ],
)
async def test_a_write_cannot_create_an_unissued_alias_while_per_subject_is_on(
    db, tmp_path, acl_reset, client, tool, arguments
):
    _activate_acl(tmp_path)
    await _insert(db, "srcagent", "a row to merge", "user:1")

    result = await _call(client, tool, arguments)

    assert result.get("ok") is False and result.get("error") == "permission_denied", result
    assert _SQUATTER not in await _agents_in_use(db)
    # The invariant the boot check enforces still holds after the call.
    await server._assert_no_reserved_agent_ids()


@pytest.mark.asyncio
async def test_an_import_cannot_create_an_unissued_alias_while_per_subject_is_on(db, tmp_path, acl_reset):
    _activate_acl(tmp_path)
    dump = tmp_path / "dump.jsonl"
    dump.write_text(
        "\n".join(
            json.dumps(r)
            for r in (
                {"_type": "memory", "agent_id": _SQUATTER, "content": "imported under a reserved name"},
                {"_type": "memory", "agent_id": "plain.agent", "content": "imported under a plain name"},
            )
        )
        + "\n",
        encoding="utf-8",
    )
    result = await _call("static-admin", "import_memories", {"input_path": str(dump)})

    in_use = await _agents_in_use(db)
    assert _SQUATTER not in in_use, result
    assert "plain.agent" in in_use, result
    assert any(_SQUATTER in e for e in result.get("errors", [])), result
    await server._assert_no_reserved_agent_ids()


@pytest.mark.asyncio
async def test_issued_aliases_and_the_subject_itself_still_write(db, tmp_path, acl_reset):
    ledger = _activate_acl(tmp_path)
    mine = await _call(
        _PS_CLIENT, "store", {"agent_id": "@me", "content": "my own space"}, issuer=_ISSUER, subject="user-1"
    )
    assert mine.get("result") == "stored", mine
    alias = mine["resolved_agent_id"]
    assert alias in ledger.issued_aliases()

    # An operator writing into an issued alias is maintenance, not a squatter.
    admin = await _call("static-admin", "store", {"agent_id": alias, "content": "operator note"})
    assert admin.get("result") == "stored", admin
    await server._assert_no_reserved_agent_ids()


@pytest.mark.asyncio
async def test_without_per_subject_a_u_prefixed_agent_is_an_ordinary_name(db, tmp_path, acl_reset):
    """The reservation exists only while some row opts in; an existing deployment
    keeps every name it has."""
    _activate_acl(tmp_path, per_subject=False)
    result = await _call("static-admin", "store", {"agent_id": _SQUATTER, "content": "ordinary"})
    assert result.get("result") == "stored", result


# ==========================================================================
# bug-531: the alias ledger follows its file.
# ==========================================================================
_A, _B = "u-aaaaaaaaaaaa", "u-bbbbbbbbbbbb"


def _write_ledger(path, mapping):
    path.write_text(json.dumps({"version": 1, "aliases": mapping}, sort_keys=True), encoding="utf-8")


def _on_disk(path):
    return json.loads(path.read_text(encoding="utf-8"))["aliases"]


def test_bug531_an_operator_relink_takes_effect_on_the_next_call(tmp_path):
    """The operator mistakenly linked subB to subA's space, then points it at its
    own alias while the server runs. The next call must reach the new alias, not
    keep reading and writing subA's memories until a restart."""
    path = tmp_path / "alias_ledger.json"
    _write_ledger(path, {_ISSUER: {"subA": _A, "subB": _A}})
    ledger = aliases.AliasLedger(str(path))
    assert ledger.resolve_or_issue(_ISSUER, "subB") == (_A, False)

    _write_ledger(path, {_ISSUER: {"subA": _A, "subB": _B}})

    assert ledger.resolve_or_issue(_ISSUER, "subB") == (_B, False)
    assert ledger.peek(_ISSUER, "subB") == _B


def test_bug531_a_removed_row_is_neither_served_nor_written_back(tmp_path):
    path = tmp_path / "alias_ledger.json"
    _write_ledger(path, {_ISSUER: {"subA": _A, "subB": _A}})
    ledger = aliases.AliasLedger(str(path))
    assert ledger.peek(_ISSUER, "subB") == _A

    _write_ledger(path, {_ISSUER: {"subA": _A}})
    assert ledger.peek(_ISSUER, "subB") is None, "the removed row is still served"

    ledger.resolve_or_issue(_ISSUER, "subC")  # an unrelated first sign-in
    assert "subB" not in _on_disk(path)[_ISSUER], _on_disk(path)
    assert aliases.AliasLedger(str(path)).peek(_ISSUER, "subB") is None


@pytest.mark.asyncio
async def test_bug531_the_boundary_follows_a_relink_without_a_restart(db, tmp_path, acl_reset):
    """End to end: the per-subject boundary decides `own` from the ledger, so a
    relink the operator makes is the space the subject's next '@me' reaches."""
    _activate_acl(tmp_path)
    path = tmp_path / "alias_ledger.json"
    _write_ledger(path, {_ISSUER: {"subA": _A, "subB": _A}})
    acl.activate_ledger(aliases.AliasLedger(str(path)))

    first = await _call(_PS_CLIENT, "store", {"agent_id": "@me", "content": "one"}, issuer=_ISSUER, subject="subB")
    assert first["resolved_agent_id"] == _A, first

    _write_ledger(path, {_ISSUER: {"subA": _A, "subB": _B}})
    second = await _call(_PS_CLIENT, "store", {"agent_id": "@me", "content": "two"}, issuer=_ISSUER, subject="subB")
    assert second["resolved_agent_id"] == _B, second
    # And subB can no longer name subA's space explicitly.
    denied = await _call(_PS_CLIENT, "store", {"agent_id": _A, "content": "three"}, issuer=_ISSUER, subject="subB")
    assert denied.get("error") == "permission_denied", denied


def test_bug531_a_broken_edit_keeps_the_last_good_rows_until_it_parses(tmp_path):
    """A save that does not parse is not an instruction to forget every subject:
    reads keep the rows last read and the corrected file is picked up."""
    path = tmp_path / "alias_ledger.json"
    _write_ledger(path, {_ISSUER: {"subA": _A}})
    ledger = aliases.AliasLedger(str(path))

    path.write_text("{not json", encoding="utf-8")
    assert ledger.peek(_ISSUER, "subA") == _A

    _write_ledger(path, {_ISSUER: {"subA": _B}})
    assert ledger.peek(_ISSUER, "subA") == _B


# ==========================================================================
# check_health: an agent-scoped report names no absolute server path.
# ==========================================================================
@pytest.fixture
def exposed_file(tmp_path, monkeypatch):
    target = tmp_path / "srv" / "alias_ledger.json"
    target.parent.mkdir()
    target.write_text("{}", encoding="utf-8")
    os.chmod(target, 0o666)
    monkeypatch.setattr(fileperms, "mode_bits_are_enforced", lambda *a, **k: True)
    monkeypatch.setattr(
        fileperms,
        "owned_paths",
        lambda: [fileperms.OwnedPath(str(target), "file", "which memory space each caller reaches")],
    )
    return target


@pytest.mark.asyncio
async def test_an_agent_scoped_health_report_names_no_server_path(db, exposed_file):
    scoped = await maintenance_handlers.do_check_health(agent_id="u-abcdef012345", checks=["file_permissions"])
    found = [i for i in scoped["issues"] if i["type"] == "file_reachable_by_other_accounts"]
    assert found, scoped  # the finding itself is still reported
    assert str(exposed_file.parent) not in json.dumps(scoped), found

    # The operator's whole-database view keeps the path it needs to act on.
    unscoped = await maintenance_handlers.do_check_health(agent_id="", checks=["file_permissions"])
    paths = [i.get("path") for i in unscoped["issues"] if i["type"] == "file_reachable_by_other_accounts"]
    assert paths == [str(exposed_file)], unscoped


@pytest.mark.asyncio
async def test_a_scoped_repair_failure_names_no_server_path(db, exposed_file, monkeypatch):
    def refuse(path, mode):
        raise PermissionError(errno.EPERM, os.strerror(errno.EPERM), path)

    monkeypatch.setattr(os, "chmod", refuse)
    issues = await checks.check_file_permissions(db, agent_id="u-abcdef012345", fix=True)
    assert issues and issues[0]["fixed"] is False, issues
    assert str(exposed_file.parent) not in json.dumps(issues), issues


@pytest.mark.asyncio
async def test_a_scoped_operating_context_finding_names_no_server_path(db, monkeypatch):
    sidecar = "/srv/private/home/.cpersona/operating-context.toml"
    monkeypatch.setattr(
        operating_context,
        "load_state",
        lambda: {
            "enabled": True,
            "path": sidecar,
            "present": True,
            "parse_error": f"[Errno 13] Permission denied: '{sidecar}'",
            "summary_len": 10**6,
        },
    )
    for check in (checks.check_operating_context_parse, checks.check_operating_context_size):
        scoped = await check(db, agent_id="u-abcdef012345")
        assert scoped and "/srv/private" not in json.dumps(scoped), scoped
        unscoped = await check(db, agent_id="")
        assert unscoped[0]["path"] == sidecar, unscoped
