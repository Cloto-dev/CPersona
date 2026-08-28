"""The resource-server half of OAuth: a client must be able to *find* the issuer.

docs/OAUTH_DESIGN.md §7. The server verifies no tokens — that is the other
half — so everything pinned here is discovery: the RFC 9728 document, and the
``WWW-Authenticate`` header that points at it.

Two properties carry the weight, and both are the kind that a plausible
refactor destroys without failing anything else:

* **The metadata document must be readable without credentials.** A client that
  needs a token to learn where tokens come from has learned nothing, and that
  is the exact failure this work exists to fix. So the middleware exempts one
  path — and the exemption is matched *exactly*. A ``startswith`` would turn
  one public document into an unauthenticated subtree while every other test
  here stayed green, so the suffix case below is not a formality.
* **Off must be byte-identical to the server that never heard of OAuth.** Not
  "close enough": the bare ``Bearer`` challenge, character for character, and a
  401 on the metadata path.

Requests are driven through the real assembled app, following
tests/test_253_middleware_wiring.py, because a middleware that behaves in
isolation tells you nothing about the stack that is actually served.
"""

import contextlib
import json

import pytest

from cpersona import acl, config, server

RESOURCE = "https://memory.example.com/mcp"
AUTH_SERVER = "https://auth.example.com"
METADATA_PATH = "/.well-known/oauth-protected-resource/mcp"
METADATA_URL = "https://memory.example.com/.well-known/oauth-protected-resource/mcp"


@pytest.fixture
def oauth_off(monkeypatch):
    """The shipped default: every OAuth setting unset."""
    monkeypatch.setattr(config, "OAUTH_RESOURCE", "")
    monkeypatch.setattr(config, "OAUTH_AUTHORIZATION_SERVERS", "")
    monkeypatch.setattr(config, "OAUTH_SCOPES", "cpersona:read cpersona:write")


@pytest.fixture
def oauth_on(monkeypatch):
    monkeypatch.setattr(config, "OAUTH_RESOURCE", RESOURCE)
    monkeypatch.setattr(config, "OAUTH_AUTHORIZATION_SERVERS", AUTH_SERVER)
    monkeypatch.setattr(config, "OAUTH_SCOPES", "cpersona:read cpersona:write")


@pytest.fixture(autouse=True)
def _deactivate_acl():
    """Every test leaves the process in legacy mode."""
    yield
    acl.activate(None)


def _make_app(auth_token="s3cret", acl_config=None):
    """The production app with a sentinel endpoint in place of the MCP mount.

    ``reached`` records every path that got past authentication, so "the tool
    surface was reached" is observed rather than read off a status code.
    """
    reached = []

    async def endpoint(scope, receive, send):
        reached.append(scope["path"])
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"mcp"})

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        yield

    app = server._build_http_app(auth_token, endpoint, lifespan, acl_config=acl_config)
    return app, reached


async def _request(app, *, method="GET", path=METADATA_PATH, headers=()):
    """One real ASGI request through the whole assembled stack."""
    messages = []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
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
    return start["status"], dict(start.get("headers", [])), body


def _acl_config(tmp_path):
    path = tmp_path / "acl.json"
    path.write_text(
        json.dumps({"clients": [{"client_id": "a", "token": "token-a", "grants": {"*": "read"}}]}),
        encoding="utf-8",
    )
    return acl.load_config(str(path))


# ---------------------------------------------------------------------------
# 1. Unconfigured: nothing changes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [METADATA_PATH, "/.well-known/oauth-authorization-server"])
async def test_metadata_path_is_refused_when_oauth_is_unset(oauth_off, path):
    """No configuration, no public path. The exemption must not be free."""
    app, reached = _make_app()
    status, _, _ = await _request(app, path=path)

    assert status == 401, f"{path!r} answered {status} with OAuth unconfigured"
    assert reached == [], f"an unauthenticated request reached the endpoint: {reached}"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [METADATA_PATH, "/mcp", "/"])
async def test_challenge_is_exactly_bearer_when_oauth_is_unset(oauth_off, path):
    """Byte-for-byte the header this server has always sent.

    Asserted as equality, not as "contains Bearer": a challenge that gained a
    ``resource_metadata`` pointing at a route nobody mounted would send every
    conformant client to a 401, which is a worse failure than the one this
    feature fixes.
    """
    app, _ = _make_app()
    status, headers, _ = await _request(app, method="POST", path=path)

    assert status == 401
    assert headers.get(b"www-authenticate") == b"Bearer"


def test_no_metadata_route_is_mounted_when_oauth_is_unset(oauth_off):
    """The 401 above could also come from a mounted route behind auth.

    Checking the route table separates "refused" from "absent" — with OAuth
    off, the served path list must be the two mounts and nothing else.
    """
    app, _ = _make_app()
    # Mount("/") normalises its path to "" — this is the untouched route table.
    paths = [getattr(r, "path", None) for r in app.routes]
    assert paths == ["/mcp", ""], paths


@pytest.mark.asyncio
async def test_valid_credentials_still_work_when_oauth_is_unset(oauth_off):
    """The guard must not become a wall while nobody asked for OAuth."""
    app, reached = _make_app()
    status, _, body = await _request(
        app, method="POST", path="/mcp", headers=[("authorization", "Bearer s3cret")]
    )

    assert status == 200 and body == b"mcp" and reached == ["/mcp"]


def test_resource_without_an_authorization_server_stays_off(monkeypatch, caplog):
    """Half a configuration is off, and says so.

    RFC 9728 §2 makes ``authorization_servers`` the point of the document; a
    resource URI on its own would publish a document that answers nobody's
    question. Off is the safe reading, and the warning is what keeps it from
    being a silent one.
    """
    monkeypatch.setattr(config, "OAUTH_RESOURCE", RESOURCE)
    monkeypatch.setattr(config, "OAUTH_AUTHORIZATION_SERVERS", "   ")

    with caplog.at_level("WARNING"):
        assert server._oauth_discovery() is None
    assert "AUTHORIZATION_SERVERS" in caplog.text


def test_malformed_resource_warns_and_stays_off(monkeypatch, caplog):
    """A typo in an optional setting must not stop the server from serving."""
    monkeypatch.setattr(config, "OAUTH_RESOURCE", "not-a-url")
    monkeypatch.setattr(config, "OAUTH_AUTHORIZATION_SERVERS", AUTH_SERVER)

    with caplog.at_level("WARNING"):
        assert server._oauth_discovery() is None
    assert "not-a-url" in caplog.text


# ---------------------------------------------------------------------------
# 2. Configured: the document is published, and is public
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metadata_document_is_served_without_credentials(oauth_on):
    """The whole point: no Authorization header, and the client learns the issuer."""
    app, reached = _make_app()
    status, headers, body = await _request(app)

    assert status == 200, f"the metadata document answered {status} to an anonymous read"
    assert reached == [], "the metadata document must be served by its route, not the MCP mount"
    assert b"application/json" in headers.get(b"content-type", b"")

    document = json.loads(body)
    assert document["resource"] == RESOURCE
    # bug-266: was AUTH_SERVER + "/" — the expectation had been written around
    # the model's normalisation rather than around what a client may use.
    assert document["authorization_servers"] == [AUTH_SERVER]
    assert document["scopes_supported"] == ["cpersona:read", "cpersona:write"]


@pytest.mark.asyncio
async def test_metadata_document_is_public_in_acl_mode_too(oauth_on, tmp_path):
    """Both credential modes owe the same public document.

    ACL mode resolves a principal instead of comparing a token, and it is a
    separate branch of the middleware; an exemption written into one branch
    only would leave half the deployments undiscoverable.
    """
    app, reached = _make_app(auth_token="", acl_config=_acl_config(tmp_path))
    status, _, body = await _request(app)

    assert status == 200 and reached == []
    assert json.loads(body)["resource"] == RESOURCE


@pytest.mark.asyncio
async def test_authorization_servers_accept_whitespace_or_commas(monkeypatch):
    """Operators write lists both ways; both must parse."""
    monkeypatch.setattr(config, "OAUTH_RESOURCE", RESOURCE)
    monkeypatch.setattr(
        config, "OAUTH_AUTHORIZATION_SERVERS", "https://a.example.com, https://b.example.com"
    )
    monkeypatch.setattr(config, "OAUTH_SCOPES", "cpersona:read")

    app, _ = _make_app()
    status, _, body = await _request(app)

    assert status == 200
    # Separators are what this test is about; the spelling is bug-266's.
    assert json.loads(body)["authorization_servers"] == [
        "https://a.example.com",
        "https://b.example.com",
    ]


# ---------------------------------------------------------------------------
# 3. Configured: the 401 points at the document, in both credential modes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/mcp", "/"])
async def test_static_bearer_401_advertises_metadata_and_scope(oauth_on, path):
    """RFC 9728 §5.1's other discovery route, plus the scope lever.

    ``scope`` is not decoration. Measured in docs/OAUTH_DESIGN.md §2: the
    client sends back exactly the scope the 401 advertised, so this parameter
    is where scope design is decided. Drop it and the decision moves to the
    client.
    """
    app, reached = _make_app()
    status, headers, _ = await _request(app, method="POST", path=path)

    assert status == 401 and reached == []
    challenge = headers.get(b"www-authenticate", b"").decode()
    assert f'resource_metadata="{METADATA_URL}"' in challenge, challenge
    assert 'scope="cpersona:read cpersona:write"' in challenge, challenge


@pytest.mark.asyncio
async def test_acl_401_advertises_metadata_and_scope(oauth_on, tmp_path):
    """The ACL branch returns its own 401 and owes the same header."""
    app, reached = _make_app(auth_token="", acl_config=_acl_config(tmp_path))
    status, headers, _ = await _request(
        app, method="POST", path="/mcp", headers=[("authorization", "Bearer wrong")]
    )

    assert status == 401 and reached == []
    challenge = headers.get(b"www-authenticate", b"").decode()
    assert f'resource_metadata="{METADATA_URL}"' in challenge, challenge
    assert 'scope="cpersona:read cpersona:write"' in challenge, challenge


@pytest.mark.asyncio
async def test_configured_scopes_reach_the_challenge(monkeypatch):
    """The advertised scope is the configured one, not a constant."""
    monkeypatch.setattr(config, "OAUTH_RESOURCE", RESOURCE)
    monkeypatch.setattr(config, "OAUTH_AUTHORIZATION_SERVERS", AUTH_SERVER)
    monkeypatch.setattr(config, "OAUTH_SCOPES", "custom:one custom:two")

    app, _ = _make_app()
    _, headers, _ = await _request(app, method="POST", path="/mcp")

    assert 'scope="custom:one custom:two"' in headers[b"www-authenticate"].decode()


# ---------------------------------------------------------------------------
# 4. The exemption is one path, matched exactly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        METADATA_PATH + "/extra",              # a suffix hung off the exempt path
        METADATA_PATH + "/../../mcp",          # traversal behind the same prefix
        METADATA_PATH + "x",
        "/.well-known/oauth-authorization-server",   # a different well-known
        "/.well-known/openid-configuration",
        "/.well-known/",
    ],
)
async def test_only_the_exact_metadata_path_is_public(oauth_on, path):
    """A prefix match here would expose an unauthenticated subtree.

    This test exists to fail if ``==`` ever becomes ``startswith``. Every path
    below shares the exempt path's prefix or its directory, and every one of
    them must still be refused.
    """
    app, reached = _make_app()
    status, _, _ = await _request(app, path=path)

    assert status == 401, f"{path!r} was served without credentials ({status})"
    assert reached == [], f"an unauthenticated request reached the endpoint: {reached}"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
async def test_the_metadata_path_is_public_for_reads_only(oauth_on, method):
    """The document is fetched with GET; a write there has no exemption to claim."""
    app, reached = _make_app()
    status, _, _ = await _request(app, method=method)

    assert status == 401, f"{method} on the metadata path answered {status}"
    assert reached == [], f"an unauthenticated {method} reached the endpoint: {reached}"


@pytest.mark.asyncio
async def test_tools_are_still_refused_while_oauth_is_configured(oauth_on):
    """Discovery is additive: it publishes a document, it opens no tool.

    Nothing here verifies a token, so a caller holding an OAuth access token
    is still refused — correctly. Only the existing credential works.
    """
    app, reached = _make_app()

    unauthorised, _, _ = await _request(app, method="POST", path="/mcp")
    with_oauth_token, _, _ = await _request(
        app, method="POST", path="/mcp", headers=[("authorization", "Bearer an-oauth-token")]
    )
    authorised, _, body = await _request(
        app, method="POST", path="/mcp", headers=[("authorization", "Bearer s3cret")]
    )

    assert (unauthorised, with_oauth_token, authorised) == (401, 401, 200)
    assert body == b"mcp" and reached == ["/mcp"]


# --------------------------------------------------------------------------------------
# The challenge is assembled from configuration, so configuration can break it.
#
# `scope` is interpolated into a quoted WWW-Authenticate parameter. A double quote
# arriving from a typo would close that parameter early and hand the client a malformed
# challenge — and this is the one header discovery depends on when the well-known path
# is not tried first. RFC 6749 §3.3 already defines what a scope token may contain, so
# the boundary has a rule to enforce rather than a guess.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, kept",
    [
        ("cpersona:read cpersona:write", ["cpersona:read", "cpersona:write"]),
        ("cpersona:read,cpersona:write", ["cpersona:read", "cpersona:write"]),
        ('cpersona:read bad"quote cpersona:write', ["cpersona:read", "cpersona:write"]),
        ("back\\slash", []),
        ("with\ttab", ["with", "tab"]),
        ("", []),
    ],
)
def test_only_rfc6749_scope_tokens_survive(raw, kept):
    assert server._scope_tokens(raw) == kept


def test_a_quote_from_configuration_cannot_reach_the_challenge(monkeypatch):
    """The header stays parseable even when the setting is not."""
    monkeypatch.setattr(config, "OAUTH_RESOURCE", RESOURCE)
    monkeypatch.setattr(config, "OAUTH_AUTHORIZATION_SERVERS", AUTH_SERVER)
    monkeypatch.setattr(config, "OAUTH_SCOPES", 'cpersona:read evil"; drop="everything')

    challenge = server._oauth_discovery().challenge

    assert challenge.count('"') % 2 == 0, f"unbalanced quotes in {challenge!r}"
    assert 'drop="everything' not in challenge
    assert 'scope="cpersona:read"' in challenge, challenge


def test_every_scope_rejected_drops_the_parameter_rather_than_emptying_it(monkeypatch):
    """An empty `scope=""` would advertise "no scopes" to a client that copies it.

    Measured in the design record: the client adopts the advertised scope verbatim.
    Advertising an empty one is worse than advertising none, because none lets the
    client fall back to its own default while empty looks deliberate.
    """
    monkeypatch.setattr(config, "OAUTH_RESOURCE", RESOURCE)
    monkeypatch.setattr(config, "OAUTH_AUTHORIZATION_SERVERS", AUTH_SERVER)
    monkeypatch.setattr(config, "OAUTH_SCOPES", '"" \\\\')

    challenge = server._oauth_discovery().challenge

    assert "scope=" not in challenge, challenge
    assert challenge == f'Bearer resource_metadata="{METADATA_URL}"'


# ---------------------------------------------------------------------------
# bug-266: the published identifiers are the ones the operator wrote
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("configured", "why"),
    [
        ("https://as.example", "a path-less issuer is what an authorization server actually publishes"),
        ("https://as.example/", "an operator who writes the slash gets the slash"),
        ("https://as.example/tenant", "a path is left alone in both directions"),
        ("https://as.example/tenant/", "including its own terminating slash"),
    ],
)
async def test_authorization_server_is_published_exactly_as_configured(monkeypatch, configured, why):
    """RFC 8414 §3.3 compares identifiers by identity, not by equivalence.

    A client reads `authorization_servers[0]`, fetches that server's metadata,
    and MUST NOT use the response unless the `issuer` it reads back is
    identical to the value it started from. An authorization server whose
    issuer has no path returns it without a trailing slash forever, so a
    document that adds one describes a server that does not exist.
    """
    monkeypatch.setattr(config, "OAUTH_RESOURCE", RESOURCE)
    monkeypatch.setattr(config, "OAUTH_AUTHORIZATION_SERVERS", configured)
    monkeypatch.setattr(config, "OAUTH_SCOPES", "cpersona:read cpersona:write")

    app, _ = _make_app()
    status, _, body = await _request(app)

    assert status == 200
    published = json.loads(body)["authorization_servers"]
    assert published == [configured], f"{why}: published {published}, configured {configured!r}"


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", ["https://resource.example", "https://resource.example/mcp"])
async def test_resource_is_published_exactly_as_configured(monkeypatch, configured):
    """RFC 9728 §3.3 states the same requirement for `resource`.

    It goes unnoticed while the configured resource carries a path, which is
    the shape this server is deployed with — the normalisation only rewrites
    an identifier that has none. Pinned for the deployment that does not.
    """
    monkeypatch.setattr(config, "OAUTH_RESOURCE", configured)
    monkeypatch.setattr(config, "OAUTH_AUTHORIZATION_SERVERS", AUTH_SERVER)
    monkeypatch.setattr(config, "OAUTH_SCOPES", "cpersona:read cpersona:write")

    app, _ = _make_app()
    metadata_path = server._oauth_discovery().metadata_path
    status, _, body = await _request(app, path=metadata_path)

    assert status == 200
    assert json.loads(body)["resource"] == configured


@pytest.mark.asyncio
async def test_several_authorization_servers_keep_their_own_spelling(monkeypatch):
    """Order and spelling are both part of the identifier list."""
    configured = ["https://first.example", "https://second.example/", "https://third.example/t"]
    monkeypatch.setattr(config, "OAUTH_RESOURCE", RESOURCE)
    monkeypatch.setattr(config, "OAUTH_AUTHORIZATION_SERVERS", " ".join(configured))
    monkeypatch.setattr(config, "OAUTH_SCOPES", "")

    app, _ = _make_app()
    status, _, body = await _request(app)

    assert status == 200
    assert json.loads(body)["authorization_servers"] == configured


def test_a_malformed_issuer_still_disables_discovery(monkeypatch, caplog):
    """Keeping the written string must not stop it from being validated.

    The strings are published verbatim, so nothing downstream would reject a
    typo — validation is the only thing standing between an operator's slip
    and a document telling every client to authenticate somewhere unusable.
    """
    monkeypatch.setattr(config, "OAUTH_RESOURCE", RESOURCE)
    monkeypatch.setattr(config, "OAUTH_AUTHORIZATION_SERVERS", "not-a-url")
    monkeypatch.setattr(config, "OAUTH_SCOPES", "")

    with caplog.at_level("WARNING"):
        assert server._oauth_discovery() is None
    assert any("invalid OAuth discovery configuration" in r.message for r in caplog.records)
