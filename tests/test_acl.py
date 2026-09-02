"""ACL v1 — the eight test families of docs/ACL_DESIGN.md §8.

The mutation-detection duty is carried behaviorally: dropping the guard wrap
turns the denial tests green→red (they call through the registry's served
handlers), inverting a grant flips the allow/deny pair, and removing a
classification row fails the exhaustiveness test. See the PR notes.
"""

import contextlib
import json

import pytest

from cpersona import acl, config, server


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_config(tmp_path, payload) -> str:
    path = tmp_path / "acl.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _basic_payload():
    return {
        "clients": [
            {
                "client_id": "assistant-a",
                "token": "token-a",
                "grants": {"alpha": "read-write", "*": "read"},
            },
            {"client_id": "reader", "token": "token-r", "grants": {"beta": "read"}},
            {"client_id": "local", "grants": {"alpha": "read"}},
        ]
    }


def _load(tmp_path, payload=None) -> acl.AclConfig:
    return acl.load_config(_write_config(tmp_path, payload or _basic_payload()))


@pytest.fixture(autouse=True)
def _deactivate_acl():
    """Every test leaves the process in legacy mode."""
    yield
    acl.activate(None)


async def _stub_handler(arguments):
    return {"ok": True, "echo": arguments}


# ---------------------------------------------------------------------------
# 1. Config loader (fail-closed, §4/§7)
# ---------------------------------------------------------------------------


def test_load_valid_config(tmp_path):
    config = _load(tmp_path)
    assert set(config.grants_by_client) == {"assistant-a", "reader", "local"}
    assert config.grants_by_client["assistant-a"]["alpha"] == acl.PERM_WRITE
    assert config.grants_by_client["assistant-a"][acl.WILDCARD] == acl.PERM_READ
    # "local" contributes grants but no token entry (§5.4).
    assert sorted(cid for _, cid in config.token_entries) == ["assistant-a", "reader"]


def test_explicit_null_token_declares_a_row_no_caller_can_present(tmp_path):
    """A grant row for a principal some resolver asserts (§4, §5.4 generalised).

    Both halves are the point. The row must carry grants, and it must
    contribute no token entry — a row that quietly became presentable would
    hand out a live credential nobody meant to issue, which is exactly what
    writing a placeholder token to get the grants in would have done.
    """
    resolver_asserted = "oauth:https://idp.example:web"
    payload = _basic_payload()
    payload["clients"].append(
        {"client_id": resolver_asserted, "token": None, "grants": {"alpha": "read"}}
    )
    config = _load(tmp_path, payload)

    assert config.grants_by_client[resolver_asserted]["alpha"] == acl.PERM_READ
    assert all(cid != resolver_asserted for _, cid in config.token_entries), (
        "a row declared credential-less must be unreachable by presenting one"
    )


def test_omitting_the_token_key_is_not_that_declaration(tmp_path):
    """Forgetting a token still fails at load; saying null does not.

    The two are one keystroke apart in the file and opposite in consequence,
    so the loader is pinned on the difference rather than on either alone: a
    static client whose token was forgotten must fail loudly (§7), while a
    principal that genuinely arrives another way must be writable.
    """
    omitted = _basic_payload()
    omitted["clients"][0].pop("token")
    with pytest.raises(acl.AclConfigError):
        _load(tmp_path, omitted)

    declared = _basic_payload()
    declared["clients"][0]["token"] = None
    config = _load(tmp_path, declared)
    assert config.grants_by_client["assistant-a"]["alpha"] == acl.PERM_WRITE
    assert all(cid != "assistant-a" for _, cid in config.token_entries)


def test_the_stdio_principal_may_state_its_absence_explicitly(tmp_path):
    """"local" carries no credential either way (§5.4); null spells it out.

    The reserved principal was the first row that had grants and no token, and
    it said so by leaving the key out. Now that null means the same thing, the
    two spellings must agree — a non-null token here is still refused, which
    the defect table above pins.
    """
    payload = _basic_payload()
    payload["clients"][2]["token"] = None
    config = _load(tmp_path, payload)
    assert config.grants_by_client[acl.LOCAL_CLIENT_ID]["alpha"] == acl.PERM_READ
    assert sorted(cid for _, cid in config.token_entries) == ["assistant-a", "reader"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update({"extra": 1}),                                      # unknown top-level key
        lambda p: p.update({"clients": []}),                                   # empty clients
        lambda p: p["clients"][0].pop("token"),                                # missing token
        lambda p: p["clients"][0].update({"token": ""}),                       # empty token
        lambda p: p["clients"][0].update({"client_id": ""}),                   # empty client_id
        lambda p: p["clients"][0].update({"grant": {}}),                       # unknown client key (typo)
        lambda p: p["clients"][0]["grants"].update({"alpha": "write"}),        # unknown permission
        lambda p: p["clients"][1].update({"client_id": "assistant-a"}),        # duplicate client_id
        lambda p: p["clients"][1].update({"token": "token-a"}),                # duplicate token
        lambda p: p["clients"][2].update({"token": "x"}),                      # token on "local"
        lambda p: p["clients"][0]["grants"].update({"": "read"}),              # empty grant key
        lambda p: p["clients"][0].update({"token": "pässwort"}),               # non-ASCII token (latin-1 header decode would mangle it)
        lambda p: p["clients"][0].update({"token": 5}),                       # non-string token (null is the only non-string form)
    ],
)
def test_load_rejects_defects(tmp_path, mutate):
    payload = _basic_payload()
    mutate(payload)
    with pytest.raises(acl.AclConfigError):
        _load(tmp_path, payload)


def test_load_rejects_invalid_json(tmp_path):
    path = tmp_path / "acl.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(acl.AclConfigError):
        acl.load_config(str(path))


def test_env_reference_resolves_and_fails_closed(tmp_path, monkeypatch):
    payload = _basic_payload()
    payload["clients"][0]["token"] = "${CPERSONA_TEST_ACL_TOKEN}"
    monkeypatch.setenv("CPERSONA_TEST_ACL_TOKEN", "from-env")
    config = _load(tmp_path, payload)
    assert acl.resolve_token(config, "from-env").client_id == "assistant-a"

    monkeypatch.delenv("CPERSONA_TEST_ACL_TOKEN")
    with pytest.raises(acl.AclConfigError):
        _load(tmp_path, payload)


def test_world_readable_file_warns(tmp_path, caplog):
    path = _write_config(tmp_path, _basic_payload())
    import os

    os.chmod(path, 0o644)
    with caplog.at_level("WARNING"):
        acl.load_config(path)
    assert any("group/world-accessible" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 2. Resolver (identity seam v1, §3.1/§5.1)
# ---------------------------------------------------------------------------


def test_resolver_token_to_principal(tmp_path):
    config = _load(tmp_path)
    assert acl.resolve_token(config, "token-a") == acl.Principal("assistant-a")
    assert acl.resolve_token(config, "token-r") == acl.Principal("reader")
    assert acl.resolve_token(config, "wrong") is None
    assert acl.resolve_token(config, "") is None


# ---------------------------------------------------------------------------
# 3. Permission lattice + wildcard semantics (D6)
# ---------------------------------------------------------------------------


def test_exact_match_beats_wildcard_in_both_directions():
    grants = {"*": acl.PERM_READ, "up": acl.PERM_WRITE, "down": acl.PERM_NONE}
    assert acl.effective_permission(grants, "up") == acl.PERM_WRITE     # exact raises
    assert acl.effective_permission(grants, "down") == acl.PERM_NONE    # exact lowers
    assert acl.effective_permission(grants, "other") == acl.PERM_READ   # wildcard fallback
    assert acl.effective_permission({}, "any") == acl.PERM_NONE


def test_wildcard_demand_needs_the_wildcard_grant():
    named_only = {"a": acl.PERM_WRITE, "b": acl.PERM_WRITE}
    assert acl.effective_permission(named_only, acl.WILDCARD) == acl.PERM_NONE


# ---------------------------------------------------------------------------
# 4. Scope resolution (§6)
# ---------------------------------------------------------------------------


def _demands(tool, args):
    return acl.ACL_CLASSIFICATION[tool](args)


def test_merge_grant_matrix():
    copy = _demands("merge_memories", {"source_agent_id": "s", "target_agent_id": "t"})
    assert copy == [("s", acl.PERM_READ), ("t", acl.PERM_WRITE)]
    move = _demands(
        "merge_memories", {"source_agent_id": "s", "target_agent_id": "t", "mode": "move"}
    )
    assert move == [("s", acl.PERM_WRITE), ("t", acl.PERM_WRITE)]


def test_empty_agent_id_resolves_to_wildcard():
    assert _demands("check_health", {"agent_id": ""}) == [("*", acl.PERM_READ)]
    assert _demands("migrate_channel_axis", {}) == [("*", acl.PERM_WRITE)]
    assert _demands("import_memories", {"target_agent_id": ""}) == [("*", acl.PERM_WRITE)]
    assert _demands("export_memories", {"agent_id": ""}) == [("*", acl.PERM_WRITE)]


def test_fix_true_escalates_health_checks_to_write():
    assert _demands("deep_check", {"agent_id": "a"}) == [("a", acl.PERM_READ)]
    assert _demands("deep_check", {"agent_id": "a", "fix": True}) == [("a", acl.PERM_WRITE)]


def test_process_wide_and_unscoped_tools():
    assert _demands("pause_persistence", {}) == [("*", acl.PERM_WRITE)]
    assert _demands("persistence_status", {}) == [("", acl.PERM_READ)]


# ---------------------------------------------------------------------------
# 5. Exhaustiveness + guard coverage (§5.2/§8) — the fail-closed pins
# ---------------------------------------------------------------------------


def test_every_registered_tool_is_classified_and_no_row_is_stale():
    registered = set(server.registry._handlers)
    classified = set(acl.ACL_CLASSIFICATION)
    assert registered == classified, (
        f"unclassified tools: {sorted(registered - classified)}; "
        f"stale classification rows: {sorted(classified - registered)}"
    )


def test_every_served_handler_carries_the_guard():
    unwrapped = [
        name
        for name, handler in server.registry._handlers.items()
        if not getattr(handler, "_acl_guarded", False)
    ]
    assert not unwrapped, f"handlers served without the ACL guard: {unwrapped}"


@pytest.mark.asyncio
async def test_unclassified_tool_is_denied_at_runtime(tmp_path):
    acl.activate(_load(tmp_path))
    token = acl.set_principal(acl.Principal("assistant-a"))
    try:
        guarded = acl._wrap("brand_new_tool", _stub_handler)
        result = await guarded({})
    finally:
        acl.reset_principal(token)
    assert result["ok"] is False and result["error"] == "permission_denied"
    assert "not classified" in result["detail"]


# ---------------------------------------------------------------------------
# 6. Annotations cross-check (§8-4): hints and classification stay honest
# ---------------------------------------------------------------------------

# Tools whose readOnlyHint=False annotation coexists with a read-level baseline
# classification because an argument escalates them (fix=true, apply=true).
_ESCALATING_READ_TOOLS = {"check_health", "deep_check", "check_update"}


def _baseline_required(tool: str) -> int:
    args = {
        "agent_id": "x",
        "source_agent_id": "x",
        "target_agent_id": "y",
        "input_path": "p",
    }
    return max(required for _, required in acl.ACL_CLASSIFICATION[tool](args))


def test_annotations_agree_with_classification():
    for tool in server.registry._tools:
        annotations = tool.annotations
        assert annotations is not None, f"{tool.name}: ToolAnnotations missing"
        baseline = _baseline_required(tool.name)
        if annotations.readOnlyHint:
            assert baseline == acl.PERM_READ, (
                f"{tool.name}: annotated read-only but classified read-write"
            )
        elif baseline == acl.PERM_READ:
            assert tool.name in _ESCALATING_READ_TOOLS, (
                f"{tool.name}: classified read at baseline but not annotated "
                "read-only and not a known escalating tool"
            )


# ---------------------------------------------------------------------------
# 7. Guard behavior + legacy equivalence (§4.1/§5.2/§5.3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_mode_contributes_zero_decisions():
    guarded = acl._wrap("store", _stub_handler)
    # No config, no principal — the call passes through untouched.
    result = await guarded({"agent_id": "anyone"})
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_grants_allow_and_deny_with_the_documented_shape(tmp_path):
    acl.activate(_load(tmp_path))
    guarded = acl._wrap("store", _stub_handler)
    read_guarded = acl._wrap("recall", _stub_handler)

    token = acl.set_principal(acl.Principal("assistant-a"))
    try:
        allowed = await guarded({"agent_id": "alpha"})       # alpha: read-write
        assert allowed["ok"] is True

        wildcard_read = await read_guarded({"agent_id": "elsewhere"})  # "*": read
        assert wildcard_read["ok"] is True

        denied = await guarded({"agent_id": "elsewhere"})    # write needs more than "*": read
        assert denied == {
            "ok": False,
            "error": "permission_denied",
            "tool": "store",
            "agent_id": "elsewhere",
            "required": "read-write",
            "client_id": "assistant-a",
        }
    finally:
        acl.reset_principal(token)


@pytest.mark.asyncio
async def test_missing_principal_is_denied_as_wiring_regression(tmp_path, caplog):
    acl.activate(_load(tmp_path))
    guarded = acl._wrap("recall", _stub_handler)
    with caplog.at_level("ERROR"):
        result = await guarded({"agent_id": "alpha"})
    assert result["ok"] is False and result["error"] == "permission_denied"
    assert any("wiring regression" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_denials_are_logged_at_warning(tmp_path, caplog):
    acl.activate(_load(tmp_path))
    guarded = acl._wrap("store", _stub_handler)
    token = acl.set_principal(acl.Principal("reader"))
    try:
        with caplog.at_level("WARNING"):
            result = await guarded({"agent_id": "alpha"})
    finally:
        acl.reset_principal(token)
    assert result["ok"] is False
    assert any("ACL denial" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 8. stdio principal (§5.4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stdio_local_principal_uses_its_grants(tmp_path):
    acl.activate(_load(tmp_path))  # local: {alpha: read}
    read_guarded = acl._wrap("recall", _stub_handler)
    write_guarded = acl._wrap("store", _stub_handler)
    token = acl.set_principal(acl.Principal(acl.LOCAL_CLIENT_ID))
    try:
        assert (await read_guarded({"agent_id": "alpha"}))["ok"] is True
        assert (await write_guarded({"agent_id": "alpha"}))["ok"] is False
    finally:
        acl.reset_principal(token)


@pytest.mark.asyncio
async def test_stdio_unlisted_local_is_denied(tmp_path):
    payload = {"clients": [{"client_id": "a", "token": "t", "grants": {"*": "read"}}]}
    acl.activate(_load(tmp_path, payload))
    guarded = acl._wrap("recall", _stub_handler)
    token = acl.set_principal(acl.Principal(acl.LOCAL_CLIENT_ID))
    try:
        assert (await guarded({"agent_id": "alpha"}))["ok"] is False
    finally:
        acl.reset_principal(token)


# ---------------------------------------------------------------------------
# 9. Transport wiring end-to-end (§5.1/§8-2): middleware → contextvar → guard
# ---------------------------------------------------------------------------


def _make_acl_app(config):
    """The production app via server._build_http_app, ACL config included.

    The sentinel endpoint calls a guarded handler, so a request that reaches it
    exercises middleware → contextvar → guard exactly as the MCP mount would.
    """
    observations = []
    guarded = acl._wrap("recall", _stub_handler)

    async def endpoint(scope, receive, send):
        result = await guarded({"agent_id": "alpha"})
        observations.append((acl.current_principal(), result))
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": json.dumps(result).encode()})

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        yield

    return server._build_http_app("", endpoint, lifespan, acl_config=config), observations


async def _request(app, headers=()):
    messages = []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "root_path": "",
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers],
        "client": ("127.0.0.1", 51234),
        "server": ("127.0.0.1", 8402),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    start = next(m for m in messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return start["status"], body


@pytest.mark.asyncio
async def test_http_request_resolves_principal_through_to_the_guard(tmp_path):
    config = _load(tmp_path)
    acl.activate(config)
    app, observations = _make_acl_app(config)

    status, body = await _request(app, headers=(("authorization", "Bearer token-a"),))
    assert status == 200
    principal, result = observations[0]
    assert principal == acl.Principal("assistant-a")
    assert result["ok"] is True  # assistant-a holds alpha: read-write ⊇ read

    status, body = await _request(app, headers=(("authorization", "Bearer token-r"),))
    assert status == 200
    principal, result = observations[1]
    assert principal == acl.Principal("reader")
    assert result["ok"] is False  # reader holds beta only — denial shape travels
    assert json.loads(body)["error"] == "permission_denied"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        (),
        (("authorization", "Bearer wrong"),),
        (("authorization", "token-a"),),  # right token, no Bearer scheme
    ],
)
async def test_http_request_without_valid_token_is_401(tmp_path, headers):
    config = _load(tmp_path)
    acl.activate(config)
    app, observations = _make_acl_app(config)
    status, _ = await _request(app, headers=headers)
    assert status == 401
    assert observations == []


@pytest.mark.asyncio
async def test_principal_is_reset_after_the_request(tmp_path):
    config = _load(tmp_path)
    acl.activate(config)
    app, _ = _make_acl_app(config)
    await _request(app, headers=(("authorization", "Bearer token-a"),))
    assert acl.current_principal() is None


# ---------------------------------------------------------------------------
# 10. Startup seams (§4.1/§7): preflight accepts ACL mode as authentication
# ---------------------------------------------------------------------------


def test_preflight_accepts_acl_mode_without_a_token(tmp_path, monkeypatch):
    monkeypatch.setenv("CPERSONA_TRANSPORT", "streamable-http")
    monkeypatch.delenv("CPERSONA_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("CPERSONA_ALLOW_UNAUTHENTICATED_HTTP", raising=False)

    # Without ACL: the empty-token refusal fires (existing behavior).
    monkeypatch.delenv("CPERSONA_ACL_FILE", raising=False)
    with pytest.raises(SystemExit):
        server._preflight_http_auth()

    # ACL env set but nothing activated: die cheaply, before the expensive
    # initialisation main() runs after this preflight.
    monkeypatch.setenv("CPERSONA_ACL_FILE", _write_config(tmp_path, _basic_payload()))
    acl.activate(None)
    with pytest.raises(RuntimeError, match="legacy authentication path"):
        server._preflight_http_auth()

    # ACL configured AND active: authentication exists; preflight passes.
    acl.activate(_load(tmp_path))
    server._preflight_http_auth()


# ---------------------------------------------------------------------------
# 11. Review-driven pins (PR #112 adversarial review findings)
# ---------------------------------------------------------------------------


def test_sweep_is_bounded_by_named_exceptions():
    """{"*": rw, "prod": none}: an all-agents call must not reach prod."""
    grants = {acl.WILDCARD: acl.PERM_WRITE, "prod": acl.PERM_NONE}
    assert acl.effective_permission(grants, acl.WILDCARD) == acl.PERM_NONE
    read_capped = {acl.WILDCARD: acl.PERM_WRITE, "audit": acl.PERM_READ}
    assert acl.effective_permission(read_capped, acl.WILDCARD) == acl.PERM_READ
    unexcepted = {acl.WILDCARD: acl.PERM_WRITE}
    assert acl.effective_permission(unexcepted, acl.WILDCARD) == acl.PERM_WRITE


@pytest.mark.asyncio
async def test_sweep_call_is_denied_for_a_client_with_an_exception(tmp_path):
    payload = {
        "clients": [
            {
                "client_id": "sweeper",
                "token": "token-s",
                "grants": {"*": "read-write", "prod": "none"},
            }
        ]
    }
    acl.activate(_load(tmp_path, payload))
    guarded = acl._wrap("check_health", _stub_handler)
    token = acl.set_principal(acl.Principal("sweeper"))
    try:
        named = await guarded({"agent_id": "staging", "fix": True})
        assert named["ok"] is True  # exact/wildcard path unaffected
        swept = await guarded({"agent_id": "", "fix": True})
        assert swept["ok"] is False and swept["agent_id"] == "*"
    finally:
        acl.reset_principal(token)


def test_non_string_agent_arguments_resolve_to_the_wildcard_demand():
    """The guard sees raw arguments; unvalidated shapes must not crash it."""
    assert _demands("store", {"agent_id": 123}) == [("*", acl.PERM_WRITE)]
    assert _demands("store", {"agent_id": ["prod"]}) == [("*", acl.PERM_WRITE)]
    assert _demands("merge_memories", {"source_agent_id": None, "target_agent_id": 5}) == [
        ("*", acl.PERM_READ),
        ("*", acl.PERM_WRITE),
    ]


@pytest.mark.asyncio
async def test_unhashable_agent_argument_is_denied_not_crashed(tmp_path, caplog):
    acl.activate(_load(tmp_path))
    guarded = acl._wrap("store", _stub_handler)
    token = acl.set_principal(acl.Principal("reader"))
    try:
        with caplog.at_level("WARNING"):
            result = await guarded({"agent_id": ["prod"]})
    finally:
        acl.reset_principal(token)
    assert result["ok"] is False and result["error"] == "permission_denied"
    assert result["agent_id"] == "*"
    assert "non-string agent_id" in result["detail"]  # §5.3 self-diagnosis
    assert any("ACL denial" in r.message for r in caplog.records)


def test_export_demand_escalates_to_wildcard_without_export_dir(monkeypatch):
    monkeypatch.setattr(acl.config, "EXPORT_DIR", "")
    assert _demands("export_memories", {"agent_id": "a"}) == [("*", acl.PERM_WRITE)]
    assert _demands("import_memories", {"target_agent_id": "a"}) == [("*", acl.PERM_WRITE)]
    monkeypatch.setattr(acl.config, "EXPORT_DIR", "/srv/exports")
    assert _demands("export_memories", {"agent_id": "a"}) == [("a", acl.PERM_WRITE)]
    assert _demands("import_memories", {"target_agent_id": "a"}) == [("a", acl.PERM_WRITE)]


def test_partial_env_reference_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("CPERSONA_TEST_ACL_TOKEN", "x")
    for bad in ("pre${CPERSONA_TEST_ACL_TOKEN}", "${CPERSONA_TEST_ACL_TOKEN}post"):
        payload = _basic_payload()
        payload["clients"][0]["token"] = bad
        with pytest.raises(acl.AclConfigError):
            _load(tmp_path, payload)


def test_non_ascii_token_resolves_to_none_without_raising(tmp_path):
    config = _load(tmp_path)
    assert acl.resolve_token(config, "tökén") is None


def test_resolver_visits_every_entry_even_after_a_match(tmp_path, monkeypatch):
    """§5.1 'no early exit': a dict-by-token 'optimization' must fail here."""
    config = _load(tmp_path)
    calls = []
    real = acl.hmac.compare_digest

    def counting(a, b):
        calls.append(1)
        return real(a, b)

    monkeypatch.setattr(acl.hmac, "compare_digest", counting)
    # "token-a" matches the FIRST entry; the loop must still visit all.
    principal = acl.resolve_token(config, "token-a")
    assert principal == acl.Principal("assistant-a")
    assert len(calls) == len(config.token_entries)


@pytest.mark.asyncio
async def test_non_ascii_bearer_is_401_on_both_auth_branches(tmp_path):
    """bug-259: a remote-controlled header must never 500 the middleware."""
    config = _load(tmp_path)
    acl.activate(config)
    app, observations = _make_acl_app(config)
    status, _ = await _request(app, headers=(("authorization", "Bearer tökén"),))
    assert status == 401 and observations == []

    # Legacy single-token branch has the same remote-controlled input.
    legacy_reached = []

    async def endpoint(scope, receive, send):
        legacy_reached.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        yield

    legacy_app = server._build_http_app("s3cret", endpoint, lifespan)
    status, _ = await _request(legacy_app, headers=(("authorization", "Bearer tökén"),))
    assert status == 401 and legacy_reached == []


@pytest.mark.asyncio
async def test_bearer_scheme_is_case_insensitive_in_acl_mode(tmp_path):
    config = _load(tmp_path)
    acl.activate(config)
    app, observations = _make_acl_app(config)
    status, _ = await _request(app, headers=(("authorization", "bearer token-a"),))
    assert status == 200
    assert observations[0][0] == acl.Principal("assistant-a")


@pytest.mark.asyncio
async def test_run_http_server_hands_the_acl_config_to_the_factory(tmp_path, monkeypatch):
    """M11/M12 pins: the active config reaches the middleware factory, the
    legacy token is blanked (D3), and an env/activation mismatch refuses to
    serve instead of falling open onto the legacy token path."""
    import uvicorn

    config = _load(tmp_path)
    calls = []
    sentinel = object()

    def fake_build(auth_token, mcp_endpoint, lifespan, acl_config=None):
        calls.append((auth_token, acl_config))
        return sentinel

    class _StubConfig:
        def __init__(self, app, host=None, port=None, **kwargs):
            self.app = app

    class _StubServer:
        def __init__(self, config):
            self.config = config

        async def serve(self):
            pass

    monkeypatch.setenv("CPERSONA_ACL_FILE", "/tmp/acl.json")
    monkeypatch.setenv("CPERSONA_AUTH_TOKEN", "legacy-token")
    monkeypatch.setattr(server, "_build_http_app", fake_build)
    monkeypatch.setattr(uvicorn, "Config", _StubConfig)
    monkeypatch.setattr(uvicorn, "Server", _StubServer)

    # Env set but nothing activated: refuse to serve (fail closed, not open).
    acl.activate(None)
    with pytest.raises(RuntimeError, match="legacy authentication path"):
        await server._run_http_server()
    assert calls == []

    # Activated: the config reaches the factory and the legacy token does not.
    acl.activate(config)
    await server._run_http_server()
    assert calls == [("", config)], (
        "ACL mode must hand the active config to the app factory and blank "
        "the legacy token (D3)"
    )


@pytest.mark.asyncio
async def test_stdio_transport_enters_the_local_principal(tmp_path, monkeypatch):
    """M13 pin: a dropped set_principal turns every stdio call in ACL mode
    into a wholesale 'no principal resolved' outage."""
    observed = {}

    @contextlib.asynccontextmanager
    async def fake_stdio_server():
        observed["principal"] = acl.current_principal()
        yield (None, None)

    async def fake_run(*args, **kwargs):
        pass

    monkeypatch.setattr(server, "stdio_server", fake_stdio_server)
    monkeypatch.setattr(server.registry.server, "run", fake_run)
    monkeypatch.setattr(
        server.registry.server, "create_initialization_options", lambda: None
    )

    import asyncio

    # Each run in its own task: set_principal is deliberately context-sticky
    # for the process lifetime in production (main() runs once), so the test
    # isolates the two scenarios the way separate processes would be.
    acl.activate(_load(tmp_path))
    await asyncio.create_task(server._run_stdio_server())
    assert observed["principal"] == acl.Principal(acl.LOCAL_CLIENT_ID)

    acl.activate(None)
    observed.clear()
    await asyncio.create_task(server._run_stdio_server())
    assert observed["principal"] is None  # legacy mode: no principal, no ACL


@pytest.mark.asyncio
async def test_stdio_refuses_env_activation_mismatch(tmp_path, monkeypatch):
    """The stdio transport carries the same fail-closed invariant as HTTP:
    ACL requested by env but nothing active must not serve unrestricted."""
    monkeypatch.setenv("CPERSONA_ACL_FILE", "/tmp/acl.json")
    acl.activate(None)
    with pytest.raises(RuntimeError, match="stdio transport unrestricted"):
        await server._run_stdio_server()


# ---------------------------------------------------------------------------
# 9. Denials say why (§5.3, extended): the response is the only channel a
#    caller has. "agent_id": "*" shows the consequence of an unscoped call, not
#    its cause — a caller would have to already know that "*" means "you did
#    not scope this" to act on it. These cases split by who can fix them —
#    the caller, or nobody but the operator.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unscoped_write_says_the_scope_was_missing_and_the_advice_works(tmp_path):
    """The fixable case: the caller sent no scope, and passing one is the fix.

    Pinned in both directions — the message is only worth shipping if the
    action it recommends actually changes the outcome.
    """
    acl.activate(_load(tmp_path))
    guarded = acl._wrap("delete_memory", _stub_handler)
    token = acl.set_principal(acl.Principal("assistant-a"))  # {"alpha": rw, "*": read}
    try:
        denied = await guarded({"memory_id": 1})
        scoped = await guarded({"memory_id": 1, "agent_id": "alpha"})
    finally:
        acl.reset_principal(token)

    assert denied["error"] == "permission_denied" and denied["agent_id"] == "*"
    assert "no agent scope was sent" in denied["detail"]
    assert "pass agent_id" in denied["detail"]
    assert scoped["ok"] is True and "error" not in scoped, (
        "the denial tells the caller to pass agent_id; the same call with one "
        "must therefore succeed, or the advice is wrong"
    )


@pytest.mark.asyncio
async def test_the_advice_names_the_argument_the_tool_actually_takes(tmp_path):
    """The same advice, on a tool that has no agent_id.

    merge_memories scopes itself with source_agent_id / target_agent_id, and
    accepts no agent_id at all. A denial that says "pass agent_id" is advice a
    caller can follow to the letter and be denied again — the failure the
    counterfactual exists to prevent, reappearing because the argument was
    assumed rather than measured. Both halves are checked: the half that was
    sent must not be demanded back, and the half that was missing must be.
    """
    acl.activate(_load(tmp_path))
    guarded = acl._wrap("merge_memories", _stub_handler)
    token = acl.set_principal(acl.Principal("assistant-a"))  # {"alpha": rw, "*": read}
    try:
        denied = await guarded({"source_agent_id": "alpha"})
        scoped = await guarded({"source_agent_id": "alpha", "target_agent_id": "alpha"})
    finally:
        acl.reset_principal(token)

    assert denied["error"] == "permission_denied" and denied["agent_id"] == "*"
    assert "target_agent_id" in denied["detail"], denied["detail"]
    assert "pass agent_id" not in denied["detail"], (
        "merge_memories has no agent_id argument, so naming it is advice that "
        "cannot be followed"
    )
    assert scoped["ok"] is True and "error" not in scoped, (
        "the denial names target_agent_id; supplying it must therefore succeed, "
        "or the advice is wrong"
    )


@pytest.mark.asyncio
async def test_the_advice_names_every_missing_half_not_just_one(tmp_path):
    """With neither side sent, one name would still leave the caller denied."""
    acl.activate(_load(tmp_path))
    guarded = acl._wrap("merge_memories", _stub_handler)
    token = acl.set_principal(acl.Principal("assistant-a"))
    try:
        denied = await guarded({})
        scoped = await guarded({"source_agent_id": "alpha", "target_agent_id": "alpha"})
    finally:
        acl.reset_principal(token)

    assert denied["error"] == "permission_denied"
    assert "source_agent_id" in denied["detail"] and "target_agent_id" in denied["detail"], (
        denied["detail"]
    )
    assert scoped["ok"] is True


@pytest.mark.asyncio
async def test_ungranted_agent_denial_stays_at_the_documented_shape(tmp_path):
    """The unfixable case deliberately gains no prose.

    A named agent the grants do not reach already names itself in the response:
    `agent_id` is the agent, `required` is the level it was short of. Adding a
    sentence saying that again would restate two fields the caller already has
    and would contradict the exact denial documented in docs/ACL_DESIGN.md §5.3
    — which uses this very case as its example. The caller cannot fix this one
    from its side either way; what it needs is the fields, which it has.
    """
    acl.activate(_load(tmp_path))
    guarded = acl._wrap("store", _stub_handler)
    token = acl.set_principal(acl.Principal("assistant-a"))
    try:
        result = await guarded({"agent_id": "beta", "message": {"content": "x"}})
    finally:
        acl.reset_principal(token)

    assert result == {
        "ok": False,
        "error": "permission_denied",
        "tool": "store",
        "agent_id": "beta",
        "required": "read-write",
        "client_id": "assistant-a",
    }


@pytest.mark.asyncio
async def test_file_io_wildcard_is_not_blamed_on_a_missing_scope(tmp_path, monkeypatch):
    """The case that makes the counterfactual necessary rather than decorative.

    export_memories escalates to the all-agents demand on its own while
    EXPORT_DIR is unset — the path, not the scope, is what widened it. The
    caller here DID pass agent_id, so "pass agent_id to scope it" would be
    advice that cannot work. _widened_by_omission asks instead of assuming.
    """
    monkeypatch.setattr(acl.config, "EXPORT_DIR", "")
    acl.activate(_load(tmp_path))
    guarded = acl._wrap("export_memories", _stub_handler)
    token = acl.set_principal(acl.Principal("assistant-a"))
    try:
        result = await guarded({"agent_id": "alpha", "output_path": "/tmp/x.json"})
    finally:
        acl.reset_principal(token)

    assert result["error"] == "permission_denied" and result["agent_id"] == "*"
    assert "pass agent_id" not in result.get("detail", ""), (
        "the export demand is unconditional while EXPORT_DIR is unset; blaming "
        "the scope argument would send the caller after a fix that does nothing"
    )


@pytest.mark.asyncio
async def test_a_client_with_no_row_is_told_that_rather_than_to_scope_its_call(tmp_path):
    """The cause no caller can fix, and the one that hides best.

    A principal with no row authenticates and is then refused every scoped
    tool while the unscoped ones answer normally — a connection that looks
    healthy and remembers nothing. Scope advice is actively wrong here: no
    agent is reachable at any level, so a caller could pass one and be denied
    again, the failure the counterfactuals exist to prevent.
    """
    unknown = "oauth:https://idp.example:web"
    acl.activate(_load(tmp_path))
    recall = acl._wrap("recall", _stub_handler)
    status = acl._wrap("persistence_status", _stub_handler)

    token = acl.set_principal(acl.Principal(unknown))
    try:
        scoped_denial = await recall({"agent_id": "alpha"})
        unscoped_denial = await recall({})
        answered = await status({})
    finally:
        acl.reset_principal(token)

    assert scoped_denial["error"] == "permission_denied"
    assert "no entry in the ACL grant table" in scoped_denial["detail"]
    assert answered["ok"] is True, (
        "the unscoped tools answer for any authenticated principal — the half "
        "that makes this look like a working connection rather than a gap"
    )
    assert "no agent scope was sent" not in unscoped_denial.get("detail", ""), (
        "an unscoped call from a client with no row must not be blamed on the "
        "scope: supplying one changes nothing"
    )
    assert "no entry in the ACL grant table" in unscoped_denial["detail"]

    granted = _basic_payload()
    granted["clients"].append(
        {"client_id": unknown, "token": None, "grants": {"alpha": "read"}}
    )
    acl.activate(_load(tmp_path, granted))
    token = acl.set_principal(acl.Principal(unknown))
    try:
        allowed = await recall({"agent_id": "alpha"})
    finally:
        acl.reset_principal(token)
    assert allowed["ok"] is True and "error" not in allowed, (
        "the denial says an operator must state this principal; stating one "
        "must therefore succeed, or the message is wrong"
    )


# ---------------------------------------------------------------------------
# 10. A caller that asked for every agent on purpose is told something else
#     (bug-263). "You sent no scope" is the right diagnosis for an omitted
#     argument and a false one for `agent_id="*"`, which IS a scope — the
#     broadest one. The probes cannot separate the two: both overwrite every
#     scope key before the demand is computed, so the original value is gone
#     by the time anyone could look at it. That is why the arguments are read
#     directly for this distinction, and why these tests pin the wording of
#     each cause against the other's.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_explicit_wildcard_is_not_reported_as_a_missing_scope(tmp_path):
    """The caller passed agent_id; being told to pass agent_id is unfollowable."""
    acl.activate(_load(tmp_path))
    guarded = acl._wrap("delete_memory", _stub_handler)
    token = acl.set_principal(acl.Principal("assistant-a"))  # {"alpha": rw, "*": read}
    try:
        denied = await guarded({"memory_id": 1, "agent_id": "*"})
        scoped = await guarded({"memory_id": 1, "agent_id": "alpha"})
    finally:
        acl.reset_principal(token)

    assert denied["error"] == "permission_denied" and denied["agent_id"] == "*"
    assert "no agent scope was sent" not in denied["detail"], (
        "the scope was sent — as the wildcard. Repeating the omitted-scope "
        "advice here tells the caller to do what it already did"
    )
    assert 'sent as "*"' in denied["detail"]
    # The reach, because "denied" alone does not say whether naming an agent helps.
    assert "read on *" in denied["detail"], denied["detail"]
    assert scoped["ok"] is True and "error" not in scoped, (
        "the denial says to name a single agent; doing so must therefore "
        "succeed, or the advice is wrong"
    )


@pytest.mark.asyncio
async def test_a_client_with_no_wildcard_grant_is_told_that_naming_agents_is_the_way(tmp_path):
    """The other way a sweep fails, and it takes a different answer.

    With no wildcard grant at all, no set of named grants ever satisfies a
    sweep (§3, D6) — so the useful thing to say is not "you are one level
    short" but "this shape of call is not available to you; name an agent".
    """
    acl.activate(_load(tmp_path))
    guarded = acl._wrap("recall", _stub_handler)
    token = acl.set_principal(acl.Principal("reader"))  # {"beta": read}, no wildcard
    try:
        denied = await guarded({"agent_id": "*", "query": "x"})
        scoped = await guarded({"agent_id": "beta", "query": "x"})
    finally:
        acl.reset_principal(token)

    assert denied["error"] == "permission_denied"
    assert "holds no all-agents grant" in denied["detail"], denied["detail"]
    assert "never add up" in denied["detail"]
    assert scoped["ok"] is True and "error" not in scoped


@pytest.mark.asyncio
async def test_only_the_half_that_was_widened_is_named(tmp_path):
    """merge_memories, one side scoped and the other sent as the wildcard."""
    acl.activate(_load(tmp_path))
    guarded = acl._wrap("merge_memories", _stub_handler)
    token = acl.set_principal(acl.Principal("assistant-a"))
    try:
        denied = await guarded({"source_agent_id": "alpha", "target_agent_id": "*"})
    finally:
        acl.reset_principal(token)

    assert 'target_agent_id was sent as "*"' in denied["detail"], denied["detail"]
    assert "source_agent_id" not in denied["detail"], (
        "the half that was scoped correctly must not be demanded back"
    )


@pytest.mark.asyncio
async def test_both_causes_in_one_call_are_both_reported(tmp_path):
    """One argument omitted, the other widened: two causes, two fixes."""
    acl.activate(_load(tmp_path))
    guarded = acl._wrap("merge_memories", _stub_handler)
    token = acl.set_principal(acl.Principal("assistant-a"))
    try:
        denied = await guarded({"target_agent_id": "*"})
        scoped = await guarded({"source_agent_id": "alpha", "target_agent_id": "alpha"})
    finally:
        acl.reset_principal(token)

    detail = denied["detail"]
    assert "no agent scope was sent" in detail and "pass source_agent_id" in detail, detail
    assert 'target_agent_id was sent as "*"' in detail, detail
    assert scoped["ok"] is True


# ---------------------------------------------------------------------------
# bug-264: a sweep no argument can narrow says why, instead of saying nothing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unconfined_file_io_denial_names_the_cause_not_the_scope(tmp_path, monkeypatch):
    """The caller scoped the call correctly and was still refused.

    With CPERSONA_EXPORT_DIR unset the path argument is caller-chosen anywhere
    on the filesystem, so the demand escalates to every agent whatever agent_id
    says. The counterfactuals rightly decline to claim scoping would have
    helped; before bug-264 the branch after them said nothing at all, leaving a
    client that holds read-write on exactly the agent it named reading
    ``agent_id: "*"`` with no reason it could act on.
    """
    monkeypatch.setattr(config, "EXPORT_DIR", "")
    acl.activate(_load(tmp_path))
    guarded = acl._wrap("export_memories", _stub_handler)
    token = acl.set_principal(acl.Principal("assistant-a"))  # {"alpha": rw, "*": read}
    try:
        denied = await guarded({"agent_id": "alpha", "output_path": "/tmp/x.json"})
    finally:
        acl.reset_principal(token)

    assert denied["error"] == "permission_denied" and denied["agent_id"] == "*"
    assert "CPERSONA_EXPORT_DIR" in denied["detail"], denied["detail"]
    assert "no agent scope narrows this call" in denied["detail"], denied["detail"]
    # The advice for the OTHER cause must not appear here: this caller did send
    # a scope, and telling it to send one is what bug-263 exists to prevent.
    assert "no agent scope was sent" not in denied["detail"], denied["detail"]


@pytest.mark.asyncio
async def test_the_advice_that_works_is_the_one_offered_for_unconfined_io(tmp_path, monkeypatch):
    """Pinned in both directions, like the omitted-scope advice above.

    The denial names CPERSONA_EXPORT_DIR; setting it must therefore change the
    outcome, or the message recommends an action that does not work.
    """
    acl.activate(_load(tmp_path))
    guarded = acl._wrap("export_memories", _stub_handler)
    token = acl.set_principal(acl.Principal("assistant-a"))
    try:
        monkeypatch.setattr(config, "EXPORT_DIR", "")
        denied = await guarded({"agent_id": "alpha", "output_path": "/tmp/x.json"})
        monkeypatch.setattr(config, "EXPORT_DIR", str(tmp_path / "exports"))
        allowed = await guarded({"agent_id": "alpha", "output_path": "/tmp/x.json"})
    finally:
        acl.reset_principal(token)

    assert denied["error"] == "permission_denied"
    assert allowed["ok"] is True and "error" not in allowed


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["pause_persistence", "resume_persistence"])
async def test_process_wide_denial_says_no_scope_would_have_helped(tmp_path, tool):
    """The same silence covered every intrinsic sweep, not only file I/O.

    A process-wide switch demands every agent by its nature. Classifying it as
    a sweep and explaining why now live on the same object, so a tool cannot be
    one without the other.
    """
    acl.activate(_load(tmp_path))
    guarded = acl._wrap(tool, _stub_handler)
    token = acl.set_principal(acl.Principal("assistant-a"))
    try:
        denied = await guarded({"agent_id": "alpha"})
    finally:
        acl.reset_principal(token)

    assert denied["error"] == "permission_denied"
    assert "process-wide" in denied["detail"], denied["detail"]
    assert "no agent scope narrows this call" in denied["detail"], denied["detail"]


def test_every_intrinsic_sweep_classification_can_explain_itself():
    """A classification that always demands "*" must carry a cause.

    The guard falls back to the always-true half when it does not, so this is
    the check that keeps the fallback from quietly becoming the norm — the
    silence bug-264 removed would otherwise return one tool at a time.
    """
    silent = []
    for name, demands in acl.ACL_CLASSIFICATION.items():
        probe = {key: "\x00probe" for key in acl._SCOPE_KEYS}
        try:
            patterns = [pattern for pattern, _ in demands(probe)]
        except Exception:  # pragma: no cover - a demands function that dislikes the probe
            continue
        if acl.WILDCARD in patterns and not getattr(demands, "_sweep_cause", None):
            silent.append(name)
    assert not silent, (
        "these tools demand every agent no matter how the call is scoped but "
        f"cannot say why: {sorted(silent)}"
    )
