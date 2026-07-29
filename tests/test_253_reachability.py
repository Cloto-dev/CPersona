"""Observed reachability while unauthenticated (2.5.3, an earlier decision).

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
@pytest.mark.parametrize("client", [("127.0.0.1", 5000), ("::1", 5000)])
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
