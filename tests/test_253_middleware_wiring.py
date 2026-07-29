"""The HTTP transport's authentication must be mounted, not merely written.

tests/test_v2435_bugfixes.py and tests/test_253_reachability.py both drive
``BearerTokenMiddleware`` by constructing it directly, which proves the class
behaves — and proves nothing about whether the running server puts it in front
of anything. Deleting the ``Middleware(BearerTokenMiddleware, ...)`` entry from
the app leaves bearer authentication AND the bug-198 reachability warning gone
from production while every one of those tests stays green: the two defences
that survived the 13-day unauthenticated exposure would vanish together,
silently.

So these tests build the real app through ``server._build_http_app`` and push
real ASGI requests through the assembled stack. Substring-matching
``inspect.getsource`` is deliberately avoided: it only shows that a line exists
in the file, and cannot tell a mounted middleware from one made unreachable by
a never-true guard.
"""

import contextlib

import pytest
import uvicorn

from cpersona import server


def _make_app(auth_token):
    """Build the production app with a sentinel endpoint in place of MCP.

    The endpoint records every request that reaches it, so "the tool surface was
    reached" is observed rather than inferred from a status code.
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

    return server._build_http_app(auth_token, endpoint, lifespan), reached


async def _request(app, *, method="POST", path="/mcp", headers=(), client=("127.0.0.1", 51234)):
    """Send one real ASGI request through the whole assembled middleware stack."""
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
        "client": client,
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


def _stack(app):
    """Walk the middleware chain the app actually serves, outermost first."""
    node = app.build_middleware_stack()
    chain = []
    while node is not None:
        chain.append(node)
        node = getattr(node, "app", None)
    return chain


def test_bearer_middleware_is_mounted_with_the_configured_token():
    """The class must be in the chain that serves requests, holding the token.

    Both halves matter: an entry built with a hard-coded empty ``auth_token``
    would be mounted and still enforce nothing.
    """
    from starlette.middleware.cors import CORSMiddleware

    app, _ = _make_app("s3cret")
    chain = _stack(app)
    types = [type(node) for node in chain]

    bearers = [n for n in chain if isinstance(n, server.BearerTokenMiddleware)]
    assert bearers, f"BearerTokenMiddleware is not in the served stack: {types}"
    assert bearers[0].auth_token == "s3cret", "the configured token did not reach the middleware"

    assert CORSMiddleware in types, f"CORSMiddleware is not in the served stack: {types}"
    assert types.index(CORSMiddleware) < types.index(type(bearers[0])), (
        "CORS must stay outside the auth layer: a browser preflight carries no "
        "Authorization header and would be answered with 401"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        (),                                          # no credentials at all
        (("authorization", "Bearer wrong"),),         # wrong token
        (("authorization", "s3cret"),),               # right token, no Bearer scheme
        (("authorization", "Basic czNjcmV0"),),       # wrong scheme
    ],
)
@pytest.mark.parametrize("path", ["/mcp", "/"])
async def test_unauthenticated_request_never_reaches_the_mcp_endpoint(headers, path):
    """Both mounts are behind the token — ``/`` serves the same endpoint as ``/mcp``."""
    app, reached = _make_app("s3cret")
    status, _, body = await _request(app, path=path, headers=headers)

    assert status == 401, f"the app answered {status} for {headers!r} on {path!r}"
    assert b"unauthorized" in body
    assert reached == [], f"an unauthenticated request reached the MCP endpoint: {reached}"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/mcp", "/"])
async def test_valid_bearer_token_reaches_the_mcp_endpoint(path):
    """The guard must not be a wall: correct credentials still get through.

    Without this, a middleware that rejects everything would pass the test
    above and break every client.
    """
    app, reached = _make_app("s3cret")
    status, _, body = await _request(
        app, path=path, headers=[("authorization", "Bearer s3cret")]
    )

    assert status == 200 and body == b"mcp"
    assert reached == [path], f"an authenticated request did not reach the endpoint: {reached}"


@pytest.mark.asyncio
async def test_cors_preflight_is_answered_without_credentials():
    """An OPTIONS preflight must survive the auth layer (claude.ai connector)."""
    app, reached = _make_app("s3cret")
    status, headers, _ = await _request(
        app,
        method="OPTIONS",
        headers=[
            ("origin", "https://claude.ai"),
            ("access-control-request-method", "POST"),
            ("access-control-request-headers", "authorization,content-type"),
        ],
    )

    assert status == 200, "the CORS preflight was rejected before CORS could answer it"
    assert headers.get(b"access-control-allow-origin") == b"https://claude.ai"
    assert reached == [], "a preflight must be answered by CORS, not forwarded to MCP"


@pytest.mark.asyncio
async def test_mounted_middleware_carries_the_reachability_warning(caplog):
    """bug-198's observation only exists if the middleware is in the request path.

    The forwarded header is the evidence a tunnel or reverse proxy leaves; the
    socket peer is still 127.0.0.1, which is exactly the shape the withdrawn
    bind-address guard called safe.
    """
    app, reached = _make_app("")

    with caplog.at_level("WARNING"):
        status, _, _ = await _request(app, headers=[("x-forwarded-for", "203.0.113.7")])

    assert status == 200 and reached == ["/mcp"], "the warning must observe, not block"
    assert "UNAUTHENTICATED" in caplog.text
    assert "x-forwarded-for" in caplog.text


@pytest.mark.asyncio
async def test_run_http_server_serves_the_app_from_the_factory(monkeypatch):
    """The factory must be the app uvicorn is handed, not an orphan helper.

    Extracting the builder makes the wiring testable only while the running
    transport still goes through it; an inlined ``Starlette(...)`` in
    ``_run_http_server`` would leave the tests above passing against code that
    nothing serves.
    """
    sentinel = object()
    calls = []

    def fake_build(auth_token, mcp_endpoint, lifespan):
        calls.append((auth_token, mcp_endpoint, lifespan))
        return sentinel

    served = {}

    class _StubConfig:
        def __init__(self, app, host=None, port=None, **kwargs):
            self.app, self.host, self.port = app, host, port

    class _StubServer:
        def __init__(self, config):
            self.config = config

        async def serve(self):
            served["config"] = self.config

    monkeypatch.setenv("CPERSONA_AUTH_TOKEN", "s3cret")
    monkeypatch.setenv("CPERSONA_HTTP_HOST", "127.0.0.1")
    monkeypatch.setattr(server, "_build_http_app", fake_build)
    monkeypatch.setattr(uvicorn, "Config", _StubConfig)
    monkeypatch.setattr(uvicorn, "Server", _StubServer)

    await server._run_http_server()

    assert len(calls) == 1, "_run_http_server did not build its app through _build_http_app"
    assert calls[0][0] == "s3cret", "the env token was not passed to the app factory"
    assert served["config"].app is sentinel, "uvicorn was handed some other app"
