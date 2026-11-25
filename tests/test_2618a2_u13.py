"""HTTP transport: foreign Host and Origin without authentication, and a token
whose algorithm belongs to another key family.

bug-919: running without authentication (CPERSONA_ALLOW_UNAUTHENTICATED_HTTP),
the Host and Origin headers are the only thing that tells a local client from a
web page that pointed its own host name at this machine. A request naming a
foreign host or coming from a foreign page must be refused before a tool runs.
With credentials configured nothing changes: a page cannot present a token.

bug-829: ``verify_token`` returns None for every rejection. A token whose header
names an allowed algorithm of the other key family (an ES algorithm for an RSA
key, or the reverse) made PyJWT raise TypeError, which escaped as a 500.
"""

import asyncio
import base64
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from transport_harness import run_with_real_transport, tool_result, write_acl_config

from cpersona import acl, oauth, server

# ---------------------------------------------------------------------------
# bug-919
# ---------------------------------------------------------------------------

# Its own agent: the suite shares one database per process.
AGENT = "u13-bug919-victim"


async def _post(app, tool, arguments, headers):
    """One real tools/call POST with the given extra headers, from loopback."""
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}
    ).encode()
    raw_headers = [
        (b"content-type", b"application/json"),
        (b"accept", b"application/json, text/event-stream"),
    ] + [(k.encode(), v.encode()) for k, v in headers]
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
        "headers": raw_headers,
        "client": ("127.0.0.1", 51234),
        "server": ("127.0.0.1", 8402),
    }
    sent = {"done": False}

    async def receive():
        if sent["done"]:
            await asyncio.Event().wait()
        sent["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    start = next(m for m in messages if m["type"] == "http.response.start")
    raw = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return start["status"], raw


@pytest.mark.asyncio
async def test_bug_919_rebinding_request_does_not_reach_the_tool():
    """A loopback peer with a foreign Host and Origin: refused, and nothing stored.

    The follow-up request is the control: the same call addressed to 127.0.0.1
    is served, so the refusal is about the header and not about the call.
    """
    foreign = [
        ("host", "attacker.example:8402"),
        ("origin", "http://attacker.example:8402"),
        ("sec-fetch-site", "same-origin"),
    ]
    local = [("host", "127.0.0.1:8402")]

    async def drive(app):
        refused = await _post(app, "store", {"agent_id": AGENT, "content": "planted by a web page"}, foreign)
        listed = await _post(app, "list_memories", {"agent_id": AGENT}, local)
        stored = await _post(app, "store", {"agent_id": AGENT, "content": "stored by a local client"}, local)
        return refused, listed, stored

    (status, raw), (list_status, list_raw), (store_status, store_raw) = await run_with_real_transport(None, drive)

    assert status == 421, f"a request naming a foreign host was answered {status}: {raw[:200]!r}"
    assert b"planted" not in list_raw, "the refused request still wrote to the store"
    assert list_status == 200
    assert store_status == 200 and tool_result(store_raw)["result"] == "stored"


async def _through_middleware(headers, *, auth_token=""):
    reached = []

    async def app(scope, receive, send):
        reached.append(True)

    sent = []
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [(k.encode(), v.encode()) for k, v in headers],
        "client": ("127.0.0.1", 51234),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await server.BearerTokenMiddleware(app, auth_token=auth_token)(scope, receive, send)
    start = next((m for m in sent if m["type"] == "http.response.start"), None)
    return bool(reached), (start or {}).get("status")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ([("host", "attacker.example:8402")], 421),
        ([("host", "localhost.attacker.example:8402")], 421),
        ([("host", "127.0.0.1.nip.io:8402")], 421),
        ([("host", "127.0.0.1:8402"), ("origin", "http://attacker.example:8402")], 403),
        ([("host", "127.0.0.1:8402"), ("origin", "null")], 403),
        ([("host", "localhost:8402"), ("origin", "http://localhost.attacker.example")], 403),
    ],
)
async def test_bug_919_foreign_host_or_origin_is_refused_without_auth(headers, status):
    reached, answered = await _through_middleware(headers)
    assert not reached, f"{headers!r} reached the app"
    assert answered == status


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        [],  # a client that sends no Host is not a browser
        [("host", "127.0.0.1:8402")],
        [("host", "localhost:8402")],
        [("host", "LOCALHOST:8402")],
        [("host", "[::1]:8402")],
        [("host", "127.0.0.1:8402"), ("origin", "http://127.0.0.1:8402")],
        [("host", "localhost:8402"), ("origin", "http://localhost:5173")],
        [("host", "127.0.0.1:8402"), ("origin", "https://claude.ai")],
    ],
)
async def test_bug_919_local_requests_are_still_served_without_auth(headers):
    reached, answered = await _through_middleware(headers)
    assert reached, f"{headers!r} was refused ({answered})"


@pytest.mark.asyncio
async def test_bug_919_check_applies_only_without_credentials():
    """With a token, a public host name behind a proxy is a normal deployment."""
    headers = [
        ("host", "memory.example.com"),
        ("origin", "https://elsewhere.example"),
        ("authorization", "Bearer s3cret"),
    ]
    reached, answered = await _through_middleware(headers, auth_token="s3cret")
    assert reached, f"an authenticated request was refused ({answered})"


# ---------------------------------------------------------------------------
# bug-829
# ---------------------------------------------------------------------------

ISSUER = "https://auth.example.com"
RESOURCE = "https://memory.example.com/mcp"
JWKS_URL = f"{ISSUER}/oauth2/jwks"
RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
EC_KEY = ec.generate_private_key(ec.SECP256R1())


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _num(value: int, size: int | None = None) -> str:
    return _b64(value.to_bytes(size or (value.bit_length() + 7) // 8, "big"))


def _fetch_for(family):
    if family == "RSA":
        n = RSA_KEY.public_key().public_numbers()
        jwk = {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": "k1", "n": _num(n.n), "e": _num(n.e)}
    else:
        n = EC_KEY.public_key().public_numbers()
        jwk = {"kty": "EC", "use": "sig", "crv": "P-256", "kid": "k1", "x": _num(n.x, 32), "y": _num(n.y, 32)}

    async def fetch(url):
        if url == f"{ISSUER}/.well-known/oauth-authorization-server":
            return 200, json.dumps({"issuer": ISSUER, "jwks_uri": JWKS_URL}).encode()
        if url == JWKS_URL:
            return 200, json.dumps({"keys": [jwk]}).encode()
        return 404, b""

    return fetch


def _claims():
    return {"iss": ISSUER, "aud": RESOURCE, "exp": int(time.time()) + 300, "sub": "x", "client_id": "c"}


def _forged(alg):
    header = _b64(json.dumps({"alg": alg, "kid": "k1"}).encode())
    return ".".join([header, _b64(json.dumps(_claims()).encode()), "AAAA"])


WRONG_FAMILY = [("RSA", "ES256"), ("RSA", "ES384"), ("EC", "RS256"), ("EC", "PS256")]


@pytest.mark.asyncio
@pytest.mark.parametrize(("family", "alg"), WRONG_FAMILY)
async def test_bug_829_wrong_key_family_is_a_refusal_not_an_exception(family, alg):
    verifier = oauth.IdpTokenVerifier([ISSUER], RESOURCE, fetch=_fetch_for(family))
    try:
        result = await verifier.verify_token(_forged(alg))
    except Exception as exc:  # noqa: BLE001 - the contract is "no exception"
        pytest.fail(f"{family} key, alg={alg}: verify_token raised {type(exc).__name__}: {exc}")
    assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("family", "key", "alg"), [("RSA", RSA_KEY, "RS256"), ("EC", EC_KEY, "ES256")])
async def test_bug_829_a_genuine_token_of_either_family_still_verifies(family, key, alg):
    verifier = oauth.IdpTokenVerifier([ISSUER], RESOURCE, fetch=_fetch_for(family))
    token = jwt.encode(_claims(), key, algorithm=alg, headers={"kid": "k1"})
    verified = await verifier.verify_token(token)
    assert verified is not None and verified.client_id == oauth.namespaced_client_id(ISSUER, "c")


@pytest.mark.asyncio
async def test_bug_829_middleware_answers_401_for_a_wrong_family_token(tmp_path):
    config = write_acl_config(tmp_path, [{"client_id": "operator", "token": "op-token", "grants": {"*": "read"}}])
    verifier = oauth.IdpTokenVerifier([ISSUER], RESOURCE, fetch=_fetch_for("RSA"))
    reached, sent = [], []

    async def app(scope, receive, send):
        reached.append(True)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [(b"authorization", f"Bearer {_forged('ES256')}".encode())],
        "client": ("127.0.0.1", 51234),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    middleware = server.BearerTokenMiddleware(app, acl_config=config, oauth_verifier=verifier)
    try:
        await middleware(scope, receive, send)
    finally:
        acl.activate(None)
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert not reached
    assert start["status"] == 401
