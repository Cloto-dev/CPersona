"""Per-subject partitioning (docs/OAUTH_DESIGN.md §12).

The boundary is restrictive: a per_subject principal reaches its own alias and
nothing else, whatever the grant table allows. The mutation duty is carried
behaviorally, as in test_acl.py: removing the boundary pass in ``acl._wrap`` —
or moving it after the grant loop — turns ``test_deny_overrides_wildcard_allow``
red, because that test holds a ``"*": read-write`` grant and still expects a
denial. Deleting the ``@me`` rewrite turns the resolution-order tests red: the
handler would see the literal sentinel instead of the alias.
"""

import json
import os

import pytest

from cpersona import acl, aliases, fileperms

ISSUER = "https://auth.example.com"
OAUTH_CLIENT = f"oauth:{ISSUER}:https://claude.ai/mcp-client"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_config(tmp_path, payload) -> str:
    path = tmp_path / "acl.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _per_subject_payload(grants=None, per_subject=True):
    return {
        "clients": [
            {
                "client_id": OAUTH_CLIENT,
                "token": None,
                "grants": grants or {"*": "read-write"},
                "per_subject": per_subject,
            },
            {
                "client_id": "static-admin",
                "token": "token-s",
                "grants": {"*": "read-write"},
            },
        ]
    }


def _activate(tmp_path, payload=None):
    acl.activate(acl.load_config(_write_config(tmp_path, payload or _per_subject_payload())))
    acl.activate_ledger(aliases.AliasLedger(str(tmp_path / "alias_ledger.json")))


def _subject_principal(subject="user-1"):
    return acl.Principal(client_id=OAUTH_CLIENT, issuer=ISSUER, subject=subject)


@pytest.fixture(autouse=True)
def _reset():
    yield
    acl.activate(None)
    acl.activate_ledger(None)


async def _stub_handler(arguments):
    return {"ok": True, "echo": arguments}


# ---------------------------------------------------------------------------
# 1. Config loader
# ---------------------------------------------------------------------------


def test_per_subject_rows_are_collected(tmp_path):
    config = acl.load_config(_write_config(tmp_path, _per_subject_payload()))
    assert config.per_subject_clients == frozenset({OAUTH_CLIENT})


def test_per_subject_on_a_static_token_row_is_a_load_error(tmp_path):
    payload = _per_subject_payload()
    payload["clients"][1]["per_subject"] = True  # static-admin carries a token
    with pytest.raises(acl.AclConfigError, match="resolver-asserted"):
        acl.load_config(_write_config(tmp_path, payload))


def test_per_subject_on_the_stdio_principal_is_a_load_error(tmp_path):
    payload = _per_subject_payload()
    payload["clients"].append(
        {"client_id": "local", "grants": {"alpha": "read"}, "per_subject": True}
    )
    with pytest.raises(acl.AclConfigError, match="resolver-asserted"):
        acl.load_config(_write_config(tmp_path, payload))


def test_per_subject_must_be_a_boolean(tmp_path):
    payload = _per_subject_payload()
    payload["clients"][0]["per_subject"] = "yes"
    with pytest.raises(acl.AclConfigError, match="true or false"):
        acl.load_config(_write_config(tmp_path, payload))


# ---------------------------------------------------------------------------
# 2. The deny boundary (explicit-deny beats every allow)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deny_overrides_wildcard_allow(tmp_path):
    """The mutation-detection test named in the module docstring.

    The client holds ``"*": read-write`` — without the boundary, every one of
    these calls succeeds. The named-agent write, the foreign-alias read and
    the unscoped sweep must all be refused; only the caller's own alias space
    (via @me) passes.
    """
    _activate(tmp_path)
    store = acl._wrap("store", _stub_handler)
    recall = acl._wrap("recall", _stub_handler)

    token = acl.set_principal(_subject_principal("user-1"))
    try:
        mine = await store({"agent_id": "@me", "content": "hello"})
        assert mine["ok"] is True
        own_alias = mine["resolved_agent_id"]

        foreign_write = await store({"agent_id": "someone-else", "content": "x"})
        assert foreign_write["ok"] is False
        assert foreign_write["error"] == "permission_denied"
        assert "@me" in foreign_write["detail"]

        # Another subject's alias is just another foreign name.
        other = acl.set_principal(_subject_principal("user-2"))
        try:
            theirs = await store({"agent_id": "@me", "content": "hi"})
            other_alias = theirs["resolved_agent_id"]
        finally:
            acl.reset_principal(other)
        assert other_alias != own_alias
        cross = await recall({"agent_id": other_alias, "query": ""})
        assert cross["ok"] is False

        sweep = await recall({"query": ""})  # no agent scope → "*" demand
        assert sweep["ok"] is False
        assert "every agent" in sweep["detail"]
    finally:
        acl.reset_principal(token)


@pytest.mark.asyncio
async def test_own_alias_may_be_addressed_literally(tmp_path):
    """The echo makes the alias addressable; the boundary must honor it."""
    _activate(tmp_path)
    store = acl._wrap("store", _stub_handler)
    token = acl.set_principal(_subject_principal())
    try:
        first = await store({"agent_id": "@me", "content": "x"})
        literal = await store({"agent_id": first["resolved_agent_id"], "content": "y"})
        assert literal["ok"] is True
        # No resolution happened, so nothing is echoed.
        assert "resolved_agent_id" not in literal
    finally:
        acl.reset_principal(token)


@pytest.mark.asyncio
async def test_unscoped_reads_stay_reachable(tmp_path):
    """Authenticated-only demands touch no per-agent data; the boundary has no say."""
    _activate(tmp_path)
    guarded = acl._wrap("persistence_status", _stub_handler)
    token = acl.set_principal(_subject_principal())
    try:
        assert (await guarded({}))["ok"] is True
    finally:
        acl.reset_principal(token)


@pytest.mark.asyncio
async def test_boundary_without_subject_fails_closed(tmp_path):
    """A per_subject row matched by a subject-less principal is a resolver bug."""
    _activate(tmp_path)
    guarded = acl._wrap("recall", _stub_handler)
    token = acl.set_principal(acl.Principal(client_id=OAUTH_CLIENT))
    try:
        denied = await guarded({"agent_id": "anything", "query": ""})
        assert denied["ok"] is False
        assert "no subject" in denied["detail"]
    finally:
        acl.reset_principal(token)


# ---------------------------------------------------------------------------
# 3. @me resolution (resolve → ACL → query)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_me_resolves_before_the_query_and_echoes_the_alias(tmp_path):
    _activate(tmp_path)
    store = acl._wrap("store", _stub_handler)
    token = acl.set_principal(_subject_principal("user-1"))
    try:
        first = await store({"agent_id": "@me", "content": "x"})
        assert first["ok"] is True
        alias = first["resolved_agent_id"]
        assert alias.startswith(aliases.ALIAS_PREFIX)
        # The handler saw the alias, never the literal sentinel.
        assert first["echo"]["agent_id"] == alias
        # First sight issued it; the response says so exactly once.
        assert first["alias_issued"] is True
        second = await store({"agent_id": "@me", "content": "y"})
        assert second["resolved_agent_id"] == alias
        assert "alias_issued" not in second
    finally:
        acl.reset_principal(token)


@pytest.mark.asyncio
async def test_me_from_a_static_client_is_refused(tmp_path):
    _activate(tmp_path)
    store = acl._wrap("store", _stub_handler)
    token = acl.set_principal(acl.Principal("static-admin"))
    try:
        denied = await store({"agent_id": "@me", "content": "x"})
        assert denied["ok"] is False
        assert "per_subject" in denied["detail"]
    finally:
        acl.reset_principal(token)


@pytest.mark.asyncio
async def test_me_from_a_non_partitioned_oauth_client_is_refused(tmp_path):
    """The sentinel is honored only where the boundary is declared in writing."""
    _activate(tmp_path, _per_subject_payload(per_subject=False))
    store = acl._wrap("store", _stub_handler)
    token = acl.set_principal(_subject_principal())
    try:
        denied = await store({"agent_id": "@me", "content": "x"})
        assert denied["ok"] is False
        assert "per_subject" in denied["detail"]
    finally:
        acl.reset_principal(token)


@pytest.mark.asyncio
async def test_me_without_an_active_ledger_fails_closed(tmp_path):
    _activate(tmp_path)
    acl.activate_ledger(None)
    store = acl._wrap("store", _stub_handler)
    token = acl.set_principal(_subject_principal())
    try:
        denied = await store({"agent_id": "@me", "content": "x"})
        assert denied["ok"] is False
        assert "ledger" in denied["detail"]
    finally:
        acl.reset_principal(token)


# ---------------------------------------------------------------------------
# 4. The alias ledger
# ---------------------------------------------------------------------------


def test_issuance_is_idempotent_and_persists(tmp_path):
    path = str(tmp_path / "ledger.json")
    ledger = aliases.AliasLedger(path)
    alias, issued = ledger.resolve_or_issue(ISSUER, "user-1")
    assert issued is True
    again, issued_again = ledger.resolve_or_issue(ISSUER, "user-1")
    assert (again, issued_again) == (alias, False)
    # A fresh load — a restart — resolves to the same alias.
    reloaded = aliases.AliasLedger(path)
    assert reloaded.peek(ISSUER, "user-1") == alias


def test_operator_linking_two_subjects_to_one_alias_survives_load(tmp_path):
    """Manual account linking: the escape hatch for provider re-issue events."""
    path = tmp_path / "ledger.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "aliases": {
                    ISSUER: {"old-sub": "u-abcd1234abcd"},
                    "https://new-idp.example.com": {"new-sub": "u-abcd1234abcd"},
                },
            }
        ),
        encoding="utf-8",
    )
    ledger = aliases.AliasLedger(str(path))
    assert ledger.peek(ISSUER, "old-sub") == "u-abcd1234abcd"
    assert ledger.peek("https://new-idp.example.com", "new-sub") == "u-abcd1234abcd"


def test_a_corrupt_ledger_refuses_startup(tmp_path):
    path = tmp_path / "ledger.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(aliases.AliasLedgerError, match="not valid JSON"):
        aliases.AliasLedger(str(path))


def test_an_alias_outside_the_reserved_shape_refuses_startup(tmp_path):
    """An operator-written alias must stay inside the u-<hex> namespace."""
    path = tmp_path / "ledger.json"
    path.write_text(
        json.dumps({"version": 1, "aliases": {ISSUER: {"s": "prod-agent"}}}),
        encoding="utf-8",
    )
    with pytest.raises(aliases.AliasLedgerError, match="reserved shape"):
        aliases.AliasLedger(str(path))


def test_failed_persist_refuses_the_issuance(tmp_path, monkeypatch):
    """An alias that is not durable must not authorize anything (aliases.py)."""
    ledger = aliases.AliasLedger(str(tmp_path / "ledger.json"))
    monkeypatch.setattr(
        aliases.AliasLedger,
        "_persist",
        lambda self: (_ for _ in ()).throw(OSError("disk full")),
    )
    with pytest.raises(aliases.AliasLedgerError, match="could not be written"):
        ledger.resolve_or_issue(ISSUER, "user-1")
    # And the failed mint left no in-memory residue to diverge from disk.
    assert ledger.peek(ISSUER, "user-1") is None


# bug-351: the ledger was read once at startup and every issuance rewrote the
# whole file from that snapshot, so a write the process did not make was erased.
# The module docstring names both second writers this loses: the operator doing
# manual account linking, and another server process over the same path.


def _write_ledger(path, mapping):
    path.write_text(
        json.dumps({"version": 1, "aliases": mapping}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def test_an_operator_edit_survives_a_later_issuance(tmp_path):
    """The documented escape hatch, made while the server is running.

    Losing it is not a cosmetic loss: after a restart the relinked subject mints
    a fresh alias and the memory space it already owns becomes unreachable.
    """
    path = tmp_path / "ledger.json"
    _write_ledger(path, {ISSUER: {"old-sub": "u-aaaaaaaaaaaa"}})
    ledger = aliases.AliasLedger(str(path))  # the process-lifetime instance

    _write_ledger(
        path,
        {ISSUER: {"old-sub": "u-aaaaaaaaaaaa", "reissued-sub": "u-aaaaaaaaaaaa"}},
    )
    ledger.resolve_or_issue(ISSUER, "brand-new-sub")  # any first sign-in

    on_disk = json.loads(path.read_text())["aliases"][ISSUER]
    assert on_disk["reissued-sub"] == "u-aaaaaaaaaaaa", on_disk
    assert on_disk["old-sub"] == "u-aaaaaaaaaaaa", on_disk
    assert "brand-new-sub" in on_disk, on_disk
    # And in memory, so this instance resolves the linked row without a restart.
    assert ledger.peek(ISSUER, "reissued-sub") == "u-aaaaaaaaaaaa"


def test_an_operator_relinking_an_existing_row_survives_a_later_issuance(tmp_path):
    """The linking edit is a *change* to a row, not only an addition.

    "Pointing two (issuer, subject) rows at one alias" rewrites the alias of a
    subject the running process already holds under a different one, so a merge
    that let this process's snapshot win for a row present in both would still
    erase it. Separated from the additive case above because only this one
    distinguishes the two merge directions.
    """
    path = tmp_path / "ledger.json"
    _write_ledger(path, {ISSUER: {"sub-a": "u-aaaaaaaaaaaa", "sub-b": "u-bbbbbbbbbbbb"}})
    ledger = aliases.AliasLedger(str(path))
    assert ledger.peek(ISSUER, "sub-b") == "u-bbbbbbbbbbbb"

    # The operator points sub-b at sub-a's space — the provider re-issue repair.
    _write_ledger(path, {ISSUER: {"sub-a": "u-aaaaaaaaaaaa", "sub-b": "u-aaaaaaaaaaaa"}})
    ledger.resolve_or_issue(ISSUER, "sub-c")

    on_disk = json.loads(path.read_text())["aliases"][ISSUER]
    assert on_disk["sub-b"] == "u-aaaaaaaaaaaa", on_disk
    assert ledger.peek(ISSUER, "sub-b") == "u-aaaaaaaaaaaa"


def test_two_processes_over_one_path_keep_both_issuances(tmp_path):
    """One ledger instance per process, one file — the second shape it loses."""
    path = tmp_path / "ledger.json"
    a = aliases.AliasLedger(str(path))
    b = aliases.AliasLedger(str(path))

    alias_a, _ = a.resolve_or_issue(ISSUER, "subject-a")
    alias_b, _ = b.resolve_or_issue(ISSUER, "subject-b")

    on_disk = json.loads(path.read_text())["aliases"][ISSUER]
    assert on_disk == {"subject-a": alias_a, "subject-b": alias_b}, on_disk


def test_an_alias_another_process_already_issued_is_adopted_not_rivalled(tmp_path):
    """Two processes, one *pair*: the row on disk is the one that survives a
    restart, so the second must return it rather than mint a rival for the same
    person — and must not claim the issuance."""
    path = tmp_path / "ledger.json"
    a = aliases.AliasLedger(str(path))
    b = aliases.AliasLedger(str(path))

    alias_a, issued_a = a.resolve_or_issue(ISSUER, "shared-sub")
    alias_b, issued_b = b.resolve_or_issue(ISSUER, "shared-sub")

    assert (alias_b, issued_a, issued_b) == (alias_a, True, False)
    assert json.loads(path.read_text())["aliases"][ISSUER] == {"shared-sub": alias_a}


def test_a_ledger_that_stopped_parsing_refuses_the_issuance(tmp_path):
    """The startup posture, at the write. Overwriting an unparseable ledger with
    this process's snapshot would destroy whatever it holds."""
    path = tmp_path / "ledger.json"
    ledger = aliases.AliasLedger(str(path))
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(aliases.AliasLedgerError, match="not valid JSON"):
        ledger.resolve_or_issue(ISSUER, "user-1")
    assert ledger.peek(ISSUER, "user-1") is None, "the refused mint left residue behind"
    assert path.read_text() == "{not json", "the unreadable ledger was overwritten"


def test_control_a_single_writer_keeps_its_own_rows(tmp_path):
    """The falsifier for the merge: with no second writer nothing changes."""
    path = tmp_path / "ledger.json"
    ledger = aliases.AliasLedger(str(path))
    first, _ = ledger.resolve_or_issue(ISSUER, "s1")
    second, _ = ledger.resolve_or_issue(ISSUER, "s2")
    assert json.loads(path.read_text())["aliases"][ISSUER] == {"s1": first, "s2": second}


@pytest.mark.asyncio
async def test_a_persist_failure_denies_the_call_rather_than_erroring(tmp_path, monkeypatch):
    _activate(tmp_path)
    monkeypatch.setattr(
        aliases.AliasLedger,
        "_persist",
        lambda self: (_ for _ in ()).throw(OSError("disk full")),
    )
    store = acl._wrap("store", _stub_handler)
    token = acl.set_principal(_subject_principal())
    try:
        denied = await store({"agent_id": "@me", "content": "x"})
        assert denied["ok"] is False
        assert denied["error"] == "permission_denied"
    finally:
        acl.reset_principal(token)


# ---------------------------------------------------------------------------
# 5. Reserved names at boot
# ---------------------------------------------------------------------------


def test_reserved_agent_id_collisions():
    ids = {"claude-code", "agent.sapphy", "@me", "u-abcd1234abcd", "user-1"}
    assert acl.reserved_agent_id_collisions(ids) == ["@me", "u-abcd1234abcd"]
    assert acl.reserved_agent_id_collisions({"claude-code"}) == []


def test_ledger_issued_aliases_are_exempt_from_the_collision_check():
    """bug-267: an alias the ledger records is issued subject space, not a squatter."""
    ids = {"@me", "u-abcd1234abcd", "u-0badc0dedead"}
    known = {"u-abcd1234abcd"}
    # The recorded alias is exempt; the unrecorded u- agent and @me still collide.
    assert acl.reserved_agent_id_collisions(ids, known) == ["@me", "u-0badc0dedead"]
    # @me is never exemptable — the ledger cannot issue it.
    assert acl.reserved_agent_id_collisions({"@me"}, {"@me"}) == ["@me"]


def test_issued_aliases_spans_issuers(tmp_path):
    path = tmp_path / "ledger.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "aliases": {
                    ISSUER: {"user-1": "u-abcd1234abcd", "user-2": "u-0123456789ab"},
                    "https://other.example.com": {"user-1": "u-feedfacecafe"},
                },
            }
        ),
        encoding="utf-8",
    )
    ledger = aliases.AliasLedger(str(path))
    assert ledger.issued_aliases() == {
        "u-abcd1234abcd",
        "u-0123456789ab",
        "u-feedfacecafe",
    }


@pytest.mark.asyncio
async def test_boot_guard_exempts_aliases_the_ledger_issued(tmp_path):
    """bug-267 end to end: the server's own issuance must not brick the next boot.

    Mutation duty: dropping the ``known_aliases`` argument from the guard's
    collision call (or the exemption clause from the collision function) turns
    the first assertion into a raise; dropping the u- refusal entirely turns
    the second one green-silent.
    """
    from cpersona import database, memory_handlers, server

    await database.init_db()
    _activate(tmp_path)
    ledger = acl.active_ledger()
    alias, issued = ledger.resolve_or_issue(ISSUER, "user-1")
    assert issued
    squatter = "u-0badc0dedead"
    try:
        stored = await memory_handlers.do_store(alias, {"content": "issued-alias row"})
        assert stored["ok"] is True
        # A database whose only reserved-prefix agent is a ledger-issued alias boots.
        await server._assert_no_reserved_agent_ids()

        # A u- agent the ledger does not record is still refused, by name.
        stored = await memory_handlers.do_store(squatter, {"content": "squatter row"})
        assert stored["ok"] is True
        with pytest.raises(RuntimeError, match=squatter):
            await server._assert_no_reserved_agent_ids()
    finally:
        # The suite shares one database file per session; leave no reserved-name
        # rows behind for unrelated tests to trip over.
        async with database.connection() as db:
            await db.execute(
                "DELETE FROM memories WHERE agent_id IN (?, ?)", (alias, squatter)
            )
            await db.commit()


# ---------------------------------------------------------------------------
# bug-322 / bug-348 — a persist that failed the wrong way left the alias live.
# ---------------------------------------------------------------------------
#
# Two halves of one line. The ledger wrote its file with a bare ``os.fchmod``,
# an attribute that does not exist on Windows while this package ships as
# OS-independent (bug-322); and the issuance rolled back on ``OSError`` only, so
# anything else the persist raised skipped the rollback (bug-348). Together: the
# first sign-in raised the wrong error type, the alias stayed live in memory, the
# temp file and its descriptor leaked, and the immediately following call was
# authorised under an alias that had never been written to disk. On restart that
# subject is re-minted and whatever the first session stored is stranded, which
# is the exact loss the module docstring says it prevents.
#
# The trigger is simulated by removing the attribute rather than run on Windows.
# Both are worth keeping: one removes this trigger, the other removes the class.


def _ledger(tmp_path):
    return aliases.AliasLedger(str(tmp_path / "alias_ledger.json"))


def test_a_ledger_written_where_fchmod_is_absent_still_persists(tmp_path, monkeypatch):
    """The Windows shape: no fchmod, and the issuance must still succeed."""
    monkeypatch.delattr(os, "fchmod", raising=False)
    ledger = _ledger(tmp_path)

    alias, issued = ledger.resolve_or_issue(ISSUER, "subject-1")

    assert issued and alias.startswith(aliases.ALIAS_PREFIX)
    assert aliases.AliasLedger(str(tmp_path / "alias_ledger.json")).peek(
        ISSUER, "subject-1"
    ) == alias


def test_a_persist_that_fails_any_way_refuses_the_issuance(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("not an OSError")

    monkeypatch.setattr(ledger, "_persist", _boom)

    with pytest.raises(aliases.AliasLedgerError):
        ledger.resolve_or_issue(ISSUER, "subject-1")

    # The rollback is what makes the refusal mean something: without it the
    # retry takes the cached fast path and hands out the unwritten alias.
    assert ledger.peek(ISSUER, "subject-1") is None
    with pytest.raises(aliases.AliasLedgerError):
        ledger.resolve_or_issue(ISSUER, "subject-1")


def test_an_oserror_persist_still_refuses_and_rolls_back(tmp_path, monkeypatch):
    """The control: the class that always worked must keep working."""
    ledger = _ledger(tmp_path)
    monkeypatch.setattr(
        ledger, "_persist", lambda *a, **k: (_ for _ in ()).throw(PermissionError("nope"))
    )

    with pytest.raises(aliases.AliasLedgerError):
        ledger.resolve_or_issue(ISSUER, "subject-1")

    assert ledger.peek(ISSUER, "subject-1") is None


def test_a_refused_issuance_leaves_no_temp_file_behind(tmp_path, monkeypatch):
    """The descriptor and the temp file both leaked on the non-OSError path."""
    monkeypatch.setattr(
        fileperms, "tighten", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    ledger = _ledger(tmp_path)

    with pytest.raises(aliases.AliasLedgerError):
        ledger.resolve_or_issue(ISSUER, "subject-1")

    leftovers = [n for n in os.listdir(tmp_path) if n.startswith(".alias_ledger.")]
    assert leftovers == [], leftovers
    assert ledger.peek(ISSUER, "subject-1") is None


def test_the_ledger_goes_through_the_packages_permissions_helper(tmp_path, monkeypatch):
    """Not a style point: the helper is what knows the attribute can be absent."""
    calls = []
    real = fileperms.tighten
    monkeypatch.setattr(fileperms, "tighten", lambda fd, path: (calls.append(path), real(fd, path))[1])

    _ledger(tmp_path).resolve_or_issue(ISSUER, "subject-1")

    assert calls, "the ledger wrote its file without going through fileperms.tighten"
