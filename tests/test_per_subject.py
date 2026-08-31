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

import pytest

from cpersona import acl, aliases

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
