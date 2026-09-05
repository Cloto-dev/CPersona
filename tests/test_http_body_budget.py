"""The HTTP transport must count the body it is handed, not the size it is told.

bug-290. Every payload cap in this package is applied by a tool handler, which
runs after the whole body has been received and parsed; before this middleware
a 64 MiB POST reached the tool surface intact. The tests here drive real ASGI
requests through ``server._build_http_app`` — the assembled stack, not the class
in isolation — because a middleware that is written and unit-tested but never
mounted is indistinguishable at runtime from one that was never written. That is
the same reasoning as tests/test_253_middleware_wiring.py, and the same failure
it was written after.

Two properties are pinned that a Content-Length check could not give:

* a body sent in chunks with no Content-Length is measured, and
* a body whose Content-Length understates it by six orders of magnitude is
  measured by what arrived, not by what was claimed.

The boundary cases are exact: one byte below the budget, exactly the budget, and
one byte above it. A budget that fires at "about" the right size cannot be told
apart from one that fires at the wrong one.
"""

import contextlib
import logging

import pytest

from cpersona import config, server

BUDGET = 4096


def _make_app(*, max_bytes=BUDGET, mode="warn", auth_token=""):
    """The production app, with a sentinel endpoint that DRAINS the body.

    The endpoint records how many bytes it actually received, so "the body
    reached the tool surface" is observed rather than inferred from a status
    code — a middleware that answered 413 while still letting every byte through
    would pass any assertion made on the response alone.
    """
    seen = {"bytes": 0, "chunks": 0, "reached": False, "disconnected": False}

    async def endpoint(scope, receive, send):
        seen["reached"] = True
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                seen["disconnected"] = True
                break
            seen["bytes"] += len(message.get("body", b""))
            seen["chunks"] += 1
            if not message.get("more_body"):
                break
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"ok"})

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        yield

    app = server._build_http_app(auth_token, endpoint, lifespan)
    # The mounted instance, so a test can read the counter the production object
    # keeps rather than a second one built for the occasion.
    budget = _mounted_budget(app)
    budget.max_bytes = max_bytes
    budget.mode = mode
    return app, seen, budget


def _mounted_budget(app):
    """The instance the app will actually serve with — not a fresh copy of it.

    ``build_middleware_stack()`` constructs a NEW chain on every call, while
    ``Starlette.__call__`` builds one lazily and caches it in
    ``middleware_stack``. Walking the former hands back a throwaway object:
    settings written onto it are discarded, and every assertion made on its
    counters reads zero forever while the served middleware does the work. So
    materialise the cached stack first and walk that.
    """
    if app.middleware_stack is None:
        app.middleware_stack = app.build_middleware_stack()
    node = app.middleware_stack
    while node is not None:
        if isinstance(node, server.RequestBodyBudgetMiddleware):
            return node
        node = getattr(node, "app", None)
    raise AssertionError("RequestBodyBudgetMiddleware is not in the served stack")


async def _post(app, chunks, *, headers=(), path="/mcp"):
    """One real ASGI request, body delivered as the given sequence of chunks."""
    messages = []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers],
        "client": ("127.0.0.1", 51234),
        "server": ("127.0.0.1", 8402),
    }
    index = {"i": 0}

    async def receive():
        i = index["i"]
        if i >= len(chunks):
            return {"type": "http.disconnect"}
        index["i"] = i + 1
        return {"type": "http.request", "body": chunks[i], "more_body": i + 1 < len(chunks)}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    start = next((m for m in messages if m["type"] == "http.response.start"), None)
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return (start or {}).get("status"), body


def _split(total, chunk=1024):
    out = [b"x" * chunk] * (total // chunk)
    if total % chunk:
        out.append(b"x" * (total % chunk))
    return out or [b""]


# ---------------------------------------------------------------------------
# Mounting
# ---------------------------------------------------------------------------


def test_the_budget_is_mounted_between_cors_and_authentication():
    """Position is the whole design; assert it on the served chain.

    Outside CORS, a 413 would reach a browser without the headers that let it
    read the status. Inside BearerTokenMiddleware, the budget would cover the
    mounts and nothing else.
    """
    from starlette.middleware.cors import CORSMiddleware

    app, _, _ = _make_app()
    chain = []
    node = app.middleware_stack  # the served chain; see _mounted_budget
    while node is not None:
        chain.append(node)
        node = getattr(node, "app", None)
    types = [type(n) for n in chain]

    budgets = [n for n in chain if isinstance(n, server.RequestBodyBudgetMiddleware)]
    assert budgets, f"RequestBodyBudgetMiddleware is not in the served stack: {types}"
    bearers = [n for n in chain if isinstance(n, server.BearerTokenMiddleware)]
    assert bearers, "BearerTokenMiddleware disappeared from the stack"

    assert types.index(CORSMiddleware) < types.index(type(budgets[0])), (
        "the budget must sit inside CORS so a 413 carries CORS headers"
    )
    assert types.index(type(budgets[0])) < types.index(type(bearers[0])), (
        "the budget must sit outside authentication so it covers everything below it"
    )


def test_the_mounted_budget_takes_its_settings_from_config():
    """A mounted middleware built with a hard-coded limit enforces the wrong one."""
    app, _, _ = _make_app()
    budget = _mounted_budget(app)
    # _make_app overwrites these; rebuild one untouched to read what wiring gives.
    fresh = server.RequestBodyBudgetMiddleware(budget.app)
    assert fresh.max_bytes == config.HTTP_MAX_BODY_BYTES
    assert fresh.mode == config.HTTP_BODY_LIMIT_MODE


def test_the_shipped_default_is_measure_only():
    """2.5.x reports; it does not refuse. The default is the whole claim."""
    assert config.HTTP_BODY_LIMIT_MODE == "warn"
    assert config.HTTP_MAX_BODY_BYTES == 4194304


# ---------------------------------------------------------------------------
# Boundaries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "size,expected_status,over",
    [
        (BUDGET - 1, 200, False),
        (BUDGET, 200, False),
        (BUDGET + 1, 413, True),
    ],
)
async def test_the_boundary_is_exact_in_reject_mode(size, expected_status, over):
    app, seen, budget = _make_app(mode="reject")
    status, _ = await _post(app, _split(size), headers=[("content-length", str(size))])
    assert status == expected_status, f"{size} bytes against a budget of {BUDGET}"
    assert budget.over_budget_requests == (1 if over else 0)
    if not over:
        assert seen["bytes"] == size, "an accepted body must arrive whole"


@pytest.mark.asyncio
async def test_an_oversized_body_stops_being_read_in_reject_mode():
    """413 must also mean "we stopped reading", not merely "we said no".

    A middleware that answers 413 and then drains the rest has refused the
    request while paying its full cost, which is the cost this exists to bound.
    """
    app, seen, _ = _make_app(mode="reject")
    status, body = await _post(app, _split(BUDGET * 8))
    assert status == 413
    assert b"payload_too_large" in body
    assert seen["bytes"] <= BUDGET + 1024, (
        f"kept reading past the budget: {seen['bytes']} bytes reached the endpoint"
    )


@pytest.mark.asyncio
async def test_the_chunk_that_crossed_the_budget_is_not_handed_on():
    """The chunk that broke the budget must not reach the application either.

    Written after a mutation survived: with 1 KiB chunks, deleting the abort
    after the 413 changes nothing observable, because the guard at the top of
    the next ``receive()`` already stops the read. The difference only appears
    when ONE chunk is itself oversized — then the deleted line is the difference
    between the application never seeing it and the application receiving the
    whole thing and parsing it, which is where the measured 3.25x amplification
    is paid. So the fixture, not the assertion, was what could not see the bug.

    uvicorn's h11 reader happens to hand up ~64 KiB at a time, so this shape is
    not what that server produces today. The ASGI contract puts no bound on a
    chunk, and the property is about this middleware, not about one server's
    buffering.
    """
    app, seen, _ = _make_app(mode="reject")
    status, _ = await _post(app, [b"x" * (BUDGET * 16)])
    assert status == 413
    assert seen["bytes"] == 0, (
        f"the oversized chunk was handed to the application anyway: {seen['bytes']} bytes"
    )


@pytest.mark.asyncio
async def test_warn_mode_delivers_the_whole_body_and_still_counts_it():
    """The shipped default changes nothing a caller can observe.

    That is the point of it: the acceptance axis for this line is that no
    payload that worked yesterday fails today. What warn mode buys is the
    number, not the refusal.
    """
    app, seen, budget = _make_app(mode="warn")
    size = BUDGET * 4
    status, _ = await _post(app, _split(size))
    assert status == 200
    assert seen["bytes"] == size, "warn mode must not truncate the body"
    assert budget.over_budget_requests == 1


@pytest.mark.asyncio
async def test_off_mode_does_not_account_at_all():
    app, seen, budget = _make_app(mode="off")
    size = BUDGET * 4
    status, _ = await _post(app, _split(size))
    assert status == 200
    assert seen["bytes"] == size
    assert budget.over_budget_requests == 0


# ---------------------------------------------------------------------------
# The header is a claim, the chunks are evidence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_content_length_that_understates_the_body_is_ignored():
    """Ten bytes declared, 8x the budget delivered. Measured by what arrived."""
    app, _, budget = _make_app(mode="reject")
    status, _ = await _post(app, _split(BUDGET * 8), headers=[("content-length", "10")])
    assert status == 413
    assert budget.over_budget_requests == 1


@pytest.mark.asyncio
async def test_a_body_with_no_content_length_is_measured():
    """Chunked transfer declares no length; the sum over receive() still holds."""
    app, _, budget = _make_app(mode="reject")
    status, _ = await _post(app, _split(BUDGET * 3, chunk=97))
    assert status == 413
    assert budget.over_budget_requests == 1


@pytest.mark.asyncio
async def test_a_multibyte_body_is_measured_in_bytes_not_characters():
    """UTF-8: the budget is a memory budget, so it counts what memory holds.

    Three-byte characters under the budget in count and over it in bytes must be
    refused — counting characters would admit 3x the payload this bounds.
    """
    app, _, budget = _make_app(mode="reject")
    chunk = "記".encode()  # 3 bytes
    body = [chunk * 512] * ((BUDGET // (512 * 3)) + 2)
    status, _ = await _post(app, body)
    assert sum(len(c) for c in body) > BUDGET
    assert status == 413
    assert budget.over_budget_requests == 1


# ---------------------------------------------------------------------------
# What the budget must NOT touch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_ordinary_payload_is_untouched_at_the_shipped_budget():
    """The largest legitimate call this server accepts must not be near the line.

    Measured: the biggest possible single store — content, metadata and source
    each at their cap, in Japanese, JSON-escaped — is ~144 KB, and a
    recall_with_context of 200 turns x 2000 characters is ~407 KB. Both are
    driven here at the SHIPPED budget rather than the small one the rest of the
    file uses, because the question is whether the default is safe.
    """
    app, seen, budget = _make_app(max_bytes=config.HTTP_MAX_BODY_BYTES, mode="reject")
    for size in (144_225, 406_762):
        status, _ = await _post(app, _split(size, chunk=65536))
        assert status == 200, f"a {size}-byte payload was refused by the shipped budget"
    assert budget.over_budget_requests == 0
    assert seen["bytes"] == 144_225 + 406_762


@pytest.mark.asyncio
async def test_a_preflight_is_answered_without_the_budget_interfering():
    """CORS is outside the budget; a bodyless OPTIONS must still be answered."""
    app, _, _ = _make_app(mode="reject")
    messages = []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "OPTIONS",
        "scheme": "http",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "root_path": "",
        "query_string": b"",
        "headers": [
            (b"origin", b"https://claude.ai"),
            (b"access-control-request-method", b"POST"),
        ],
        "client": ("127.0.0.1", 51234),
        "server": ("127.0.0.1", 8402),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    start = next(m for m in messages if m["type"] == "http.response.start")
    assert start["status"] == 200
    assert dict(start["headers"]).get(b"access-control-allow-origin") == b"https://claude.ai"


@pytest.mark.asyncio
async def test_authentication_still_decides_before_the_budget_can_refuse():
    """A 401 is not turned into a 413 by an oversized unauthenticated body.

    The budget counts bytes the application pulls, and the credential check
    returns without pulling any. Stating it as a test because the alternative —
    a 413 that tells an unauthenticated prober the size of our budget — is the
    kind of thing a later reordering would introduce silently.
    """
    app, seen, budget = _make_app(mode="reject", auth_token="s3cret")
    status, _ = await _post(app, _split(BUDGET * 8))
    assert status == 401
    assert not seen["reached"], "an unauthenticated request reached the tool surface"
    assert budget.over_budget_requests == 0, "nothing pulled the body, so nothing was counted"


def test_the_budget_is_not_applied_to_lifespan_or_websockets():
    """stdio has no ASGI scope at all; the non-http scopes here must pass through."""
    calls = []

    async def app(scope, receive, send):
        calls.append(scope["type"])

    budget = server.RequestBodyBudgetMiddleware(app, max_bytes=1, mode="reject")

    import asyncio

    async def drive(scope_type):
        await budget({"type": scope_type}, None, None)

    asyncio.run(drive("lifespan"))
    asyncio.run(drive("websocket"))
    assert calls == ["lifespan", "websocket"]


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crossings_are_reported_at_decades_not_once_and_not_every_time(caplog):
    """One line per request buries the condition; one line per process hides its scale.

    The exact set is asserted, so a change of rule fails here rather than
    becoming a log volume nobody notices.
    """
    app, _, budget = _make_app(mode="warn")
    with caplog.at_level(logging.WARNING, logger="cpersona.server"):
        for _ in range(11):
            await _post(app, _split(BUDGET * 2))

    lines = [r for r in caplog.records if "exceeded CPERSONA_HTTP_MAX_BODY_BYTES" in r.getMessage()]
    assert budget.over_budget_requests == 11
    assert len(lines) == 2, f"expected the 1st and 10th crossing, got {len(lines)}"
    assert "Occurrence 1 " in lines[0].getMessage()
    assert "Occurrence 10 " in lines[1].getMessage()


@pytest.mark.asyncio
async def test_the_warning_names_the_setting_and_says_what_it_did(caplog):
    """An operator reading one line must be able to act without reading this file."""
    app, _, _ = _make_app(mode="warn")
    with caplog.at_level(logging.WARNING, logger="cpersona.server"):
        await _post(app, _split(BUDGET * 2))
    message = caplog.records[-1].getMessage()
    assert "CPERSONA_HTTP_MAX_BODY_BYTES" in message
    assert "CPERSONA_HTTP_BODY_LIMIT_MODE=reject" in message
    assert "The body was served in full" in message

    caplog.clear()
    app, _, _ = _make_app(mode="reject")
    with caplog.at_level(logging.WARNING, logger="cpersona.server"):
        await _post(app, _split(BUDGET * 2))
    message = caplog.records[-1].getMessage()
    assert "answered 413" in message


def test_an_unknown_mode_falls_back_to_the_documented_default(caplog):
    with caplog.at_level(logging.WARNING, logger="cpersona.server"):
        budget = server.RequestBodyBudgetMiddleware(lambda *a: None, mode="strict")
    assert budget.mode == "warn"
    assert any("not one of warn/reject/off" in r.getMessage() for r in caplog.records)
