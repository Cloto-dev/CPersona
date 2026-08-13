"""Observed reachability while unauthenticated (2.5.3, Goal #183).

The startup guard can only look at the bind address, and that is exactly the
premise that failed: a loopback bind said nothing about who could reach the
process. These tests cover the other half of the fix — the middleware reports
reachability it has actually observed, from a forwarding header or a peer that
is not this machine, rather than inferring it from an address.

The warning is deliberately observational: it never changes the response. An
operator who opted into running without auth gets told that the exposure is
real, not blocked mid-request.
"""

import pytest

from cpersona import server


async def _call(auth_token: str, *, headers=None, client=("127.0.0.1", 51234), middleware=None):
    """Drive the real middleware once and report whether the app was reached."""
    app_reached = False

    async def dummy_app(scope, receive, send):
        nonlocal app_reached
        app_reached = True

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [(k.encode(), v.encode()) for k, v in (headers or [])],
        "client": client,
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        pass

    mw = middleware or server.BearerTokenMiddleware(dummy_app, auth_token=auth_token)
    if middleware is not None:
        mw.app = dummy_app
    await mw(scope, receive, send)
    return app_reached, mw


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "header",
    [
        ("x-forwarded-for", "203.0.113.7"),
        ("forwarded", "for=203.0.113.7;proto=https"),
        ("x-real-ip", "203.0.113.7"),
        ("cf-connecting-ip", "203.0.113.7"),
        # 2.5.4: RFC 7230's own header was missing from the list, and so was
        # the proto/host pair a proxy sends when it rewrites the URL but not
        # the client address — the shape of an nginx block with no
        # `proxy_set_header X-Forwarded-For`.
        ("via", "1.1 nginx"),
        ("x-forwarded-proto", "https"),
        ("x-forwarded-host", "memory.example.com"),
        ("true-client-ip", "203.0.113.7"),
    ],
)
async def test_forwarded_header_on_unauthenticated_request_warns(caplog, header):
    """A forwarding header is proof the request came through something else.

    The socket peer here is 127.0.0.1 — exactly what a tunnel looks like from
    inside the host, and exactly the shape the old guard called safe.
    """
    with caplog.at_level("WARNING"):
        app_reached, _ = await _call("", headers=[header])

    assert app_reached is True, "the warning must observe, not block"
    assert "UNAUTHENTICATED" in caplog.text
    assert header[0] in caplog.text


@pytest.mark.asyncio
async def test_remote_peer_on_unauthenticated_request_warns(caplog):
    with caplog.at_level("WARNING"):
        app_reached, _ = await _call("", client=("203.0.113.7", 44321))

    assert app_reached is True
    assert "UNAUTHENTICATED" in caplog.text
    assert "203.0.113.7" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "client",
    [
        ("127.0.0.1", 5000),
        ("::1", 5000),
        # 2.5.4: a dual-stack listener (CPERSONA_HTTP_HOST=::) reports an IPv4
        # client on this host in this form. The string-set check called it
        # remote, and because the warning latches, that false positive spent
        # the one warning this process will ever emit on a local request — so
        # the genuine remote arrival afterwards was reported to nobody.
        ("::ffff:127.0.0.1", 5000),
        ("127.0.0.53", 5000),
        ("localhost", 5000),
    ],
)
async def test_local_peer_without_forwarding_stays_quiet(caplog, client):
    """No evidence of outside reach — say nothing.

    A warning on every local request would be noise, and noise is what buried
    the original one.
    """
    with caplog.at_level("WARNING"):
        app_reached, _ = await _call("", client=client)

    assert app_reached is True
    assert "UNAUTHENTICATED" not in caplog.text


@pytest.mark.asyncio
async def test_a_false_positive_would_spend_the_only_warning(caplog):
    """Why the peer check is arithmetic now, stated as a behaviour.

    The two halves of the defect only bite together: misclassifying a local
    peer is cheap on its own, and latching the warning is correct on its own.
    Together they mean the first local request over a dual-stack listener
    silences the real exposure that arrives later. This test pins the pair —
    a local dual-stack request, THEN a genuinely remote one that must still be
    reported.
    """
    async def noop(scope, receive, send):
        pass

    mw = server.BearerTokenMiddleware(noop, auth_token="")

    with caplog.at_level("WARNING"):
        await _call("", client=("::ffff:127.0.0.1", 5000), middleware=mw)
        assert "UNAUTHENTICATED" not in caplog.text, "a local peer must not spend the warning"
        await _call("", client=("203.0.113.7", 44321), middleware=mw)

    assert caplog.text.count("UNAUTHENTICATED") == 1
    assert "203.0.113.7" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "peer,expected",
    [
        ("127.0.0.1", False),
        ("127.0.0.53", False),
        ("::1", False),
        ("::ffff:127.0.0.1", False),
        ("localhost", False),
        ("", False),
        ("203.0.113.7", True),
        ("::ffff:203.0.113.7", True),
        ("2001:db8::1", True),
        ("not-an-address", True),
    ],
)
async def test_peer_classification(peer, expected):
    """The predicate itself, including the deliberate default.

    An unparseable peer counts as remote: the cost of erring loud is one log
    line, and the cost of erring quiet is missing the case nobody anticipated.
    """
    assert server._peer_is_remote(peer) is expected


@pytest.mark.asyncio
async def test_authenticated_server_does_not_warn(caplog):
    """With a token set there is no exposure to report, forwarded or not."""
    with caplog.at_level("WARNING"):
        app_reached, _ = await _call(
            "s3cret",
            headers=[("authorization", "Bearer s3cret"), ("x-forwarded-for", "203.0.113.7")],
        )

    assert app_reached is True
    assert "UNAUTHENTICATED" not in caplog.text


@pytest.mark.asyncio
async def test_warning_is_emitted_once_per_process(caplog):
    """It is a standing condition, not a per-request event.

    One line per request would push the warning out of any log an operator
    actually reads — the failure mode this whole change exists to fix.
    """
    async def noop(scope, receive, send):
        pass

    mw = server.BearerTokenMiddleware(noop, auth_token="")

    with caplog.at_level("WARNING"):
        await _call("", headers=[("x-forwarded-for", "203.0.113.7")], middleware=mw)
        first = caplog.text.count("UNAUTHENTICATED")
        await _call("", headers=[("x-forwarded-for", "203.0.113.8")], middleware=mw)
        second = caplog.text.count("UNAUTHENTICATED")

    assert first == 1
    assert second == 1, "the second exposed request must not repeat the warning"


# ---------------------------------------------------------------------------
# 2.5.4 — what this detector cannot do, pinned so it stays said out loud
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_headerless_loopback_relay_is_not_detected(caplog):
    """The blind spot, as a test rather than a footnote.

    ``ssh -L``, ``socat``, ``kubectl port-forward`` and a bare nginx
    ``proxy_pass`` all arrive exactly like this: peer 127.0.0.1, no forwarding
    headers. Nothing at this layer distinguishes them from a local client, at
    any price — the information is not in the request. This test exists so the
    limitation is a recorded property rather than a gap someone later reads as
    coverage, and it is paired with the opt-in warning below, which is where an
    operator can still act on it.
    """
    with caplog.at_level("WARNING"):
        app_reached, _ = await _call("", client=("127.0.0.1", 51234))

    assert app_reached is True
    assert "UNAUTHENTICATED" not in caplog.text


def test_the_opt_in_warning_admits_the_blind_spot(caplog):
    """Silence from the detector must not read as 'nothing can reach this'."""
    with caplog.at_level("WARNING"):
        server._assert_safe_http_bind("", "127.0.0.1", allow_unauthenticated=True)

    assert "CANNOT" in caplog.text
    assert "ssh -L" in caplog.text
    assert "silence is not evidence" in caplog.text


# ---------------------------------------------------------------------------
# 2.5.4 — scopes other than http, and the guard's position in startup
# ---------------------------------------------------------------------------


async def _call_scope(scope_type: str, auth_token: str):
    """Drive the middleware with a non-http scope; report reach and sent messages."""
    app_reached = False
    sent = []

    async def dummy_app(scope, receive, send):
        nonlocal app_reached
        app_reached = True

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        sent.append(message)

    mw = server.BearerTokenMiddleware(dummy_app, auth_token=auth_token)
    await mw({"type": scope_type, "path": "/mcp", "headers": []}, receive, send)
    return app_reached, sent


@pytest.mark.asyncio
async def test_lifespan_passes_through():
    """Startup/shutdown has nobody to authenticate; blocking it stops the server."""
    app_reached, sent = await _call_scope("lifespan", "s3cret")

    assert app_reached is True
    assert sent == []


@pytest.mark.asyncio
async def test_websocket_is_refused_when_a_token_is_configured():
    """'Not http' was read as 'not a request'. A websocket is a request.

    Its reach is limited today — no websockets/wsproto is installed, so uvicorn
    never completes the upgrade — but the contract this middleware states is
    that no unauthenticated request reaches a tool, and a contract that holds
    only as far as the dependency list allows is not the contract.
    """
    app_reached, sent = await _call_scope("websocket", "s3cret")

    assert app_reached is False
    assert sent == [{"type": "websocket.close", "code": 1008}]


@pytest.mark.asyncio
async def test_websocket_passes_through_when_unauthenticated_is_opted_into():
    """Matches the http path: with no token there is nothing to check."""
    app_reached, _ = await _call_scope("websocket", "")

    assert app_reached is True


def test_preflight_refuses_before_any_resource_is_touched(monkeypatch):
    """The guard's inputs are two env vars, so it belongs at the top of main().

    Under the production unit (Restart=always, RestartSec=10, EnvironmentFile)
    a token that fails to load used to mean: open and migrate the DB, call the
    embedding backend to calibrate, start and stop the queue, exit 1 — every
    ten seconds.
    """
    monkeypatch.setenv("CPERSONA_TRANSPORT", "streamable-http")
    monkeypatch.delenv("CPERSONA_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("CPERSONA_ALLOW_UNAUTHENTICATED_HTTP", raising=False)

    with pytest.raises(SystemExit):
        server._preflight_http_auth()


@pytest.mark.asyncio
async def test_main_refuses_before_it_opens_the_database(monkeypatch):
    """The pin that matters: a guard main() does not call is a guard that is not there.

    Every other test here can pass with the call site deleted — the refusal
    still happens, just at the end of startup, which is the defect. This one
    fails, because init_db was reached.
    """
    calls = []

    monkeypatch.setenv("CPERSONA_TRANSPORT", "streamable-http")
    monkeypatch.delenv("CPERSONA_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("CPERSONA_ALLOW_UNAUTHENTICATED_HTTP", raising=False)

    async def spy_init_db():
        calls.append("init_db")

    async def spy_close_db():
        calls.append("close_db")

    monkeypatch.setattr(server, "init_db", spy_init_db)
    monkeypatch.setattr(server, "close_db", spy_close_db)

    with pytest.raises(SystemExit):
        await server.main()

    assert calls == [], f"startup work ran before the guard refused: {calls}"


def test_preflight_is_silent_for_stdio(monkeypatch):
    """stdio has no bind and no token; the guard must not speak for it."""
    monkeypatch.setenv("CPERSONA_TRANSPORT", "stdio")
    monkeypatch.delenv("CPERSONA_AUTH_TOKEN", raising=False)

    server._preflight_http_auth()  # must not raise


def test_preflight_does_not_duplicate_the_opt_in_warning(caplog, monkeypatch):
    """_run_http_server keeps its own call, so the warning would print twice."""
    monkeypatch.setenv("CPERSONA_TRANSPORT", "streamable-http")
    monkeypatch.delenv("CPERSONA_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("CPERSONA_ALLOW_UNAUTHENTICATED_HTTP", "true")

    with caplog.at_level("WARNING"):
        server._preflight_http_auth()

    assert "UNAUTHENTICATED" not in caplog.text
