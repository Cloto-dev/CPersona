"""bug-253: the stdio->HTTP bridge forwards messages concurrently.

Before this the proxy was a strictly serial pump: one line off stdin, the whole
round trip awaited inline (read timeout 300 s), the response written, and only
then the next line read. That is head-of-line blocking on a transport whose
protocol (JSON-RPC) and whose server (stateless HTTP) both allow several
outstanding requests, and it made cancellation structurally impossible — the
notifications/cancelled for a stuck request could not be sent until that
request finished.

Every test here drives the real ``main()`` over an in-memory httpx transport;
nothing touches the network or real stdout.
"""

import asyncio
import itertools
import json

import httpx
import pytest

from cpersona import proxy_stdio

# Captured before any monkeypatching, since the tests replace httpx.AsyncClient.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _client_factory(handler):
    """Build a drop-in httpx.AsyncClient that answers from ``handler``."""

    def factory(**kwargs):
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    return factory


def _scripted_thread(lines):
    """Stand in for the stdin reader thread and feed ``lines``, then EOF."""

    class _Thread:
        def __init__(self, target, args, daemon):
            self.queue, self.loop = args

        def start(self):
            for line in lines:
                self.loop.call_soon(self.queue.put_nowait, line)
            self.loop.call_soon(self.queue.put_nowait, None)

    return _Thread


def _request(req_id, method="tools/call"):
    return json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method}).encode()


def _result(req_id):
    return {"jsonrpc": "2.0", "id": req_id, "result": {}}


async def _wait_until(predicate, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached within timeout")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_slow_request_does_not_block_the_next_one(monkeypatch):
    """A request that cannot finish until a later one is SENT still lets it through.

    Under the serial pump request 2 was never posted while request 1 was in
    flight, so this deadlocks and the wait_for turns it into a failure.
    """
    unblock = asyncio.Event()

    async def handler(request):
        payload = json.loads(request.content)
        if payload["id"] == 1:
            await unblock.wait()
        else:
            unblock.set()
        return httpx.Response(200, json=_result(payload["id"]))

    written = []
    monkeypatch.setattr(proxy_stdio, "_write_stdout", written.append)
    monkeypatch.setattr(proxy_stdio.httpx, "AsyncClient", _client_factory(handler))
    monkeypatch.setattr(proxy_stdio.threading, "Thread", _scripted_thread([_request(1), _request(2)]))

    await asyncio.wait_for(proxy_stdio.main(), timeout=10)

    assert [json.loads(m)["id"] for m in written] == [2, 1]


@pytest.mark.asyncio
async def test_concurrent_responses_do_not_corrupt_stdout(monkeypatch):
    """Multi-line responses that all come back at once are emitted whole.

    Every handler waits on a barrier that only opens once all six requests have
    arrived, so the responses are released in the same event-loop tick — the one
    situation in which a forwarder writing its own lines could interleave them
    with another's. The barrier also means a serial pump never opens it at all.
    """
    parts = 3
    ids = list(range(1, 7))
    arrived = []
    released = asyncio.Event()

    async def handler(request):
        req_id = json.loads(request.content)["id"]
        arrived.append(req_id)
        if len(arrived) == len(ids):
            released.set()
        await released.wait()
        body = "".join(
            f'data: {{"jsonrpc":"2.0","id":{req_id},"part":{p}}}\n\n' for p in range(parts)
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    written = []
    monkeypatch.setattr(proxy_stdio, "_write_stdout", written.append)
    monkeypatch.setattr(proxy_stdio.httpx, "AsyncClient", _client_factory(handler))
    monkeypatch.setattr(
        proxy_stdio.threading, "Thread", _scripted_thread([_request(i) for i in ids])
    )

    await asyncio.wait_for(proxy_stdio.main(), timeout=10)

    assert sorted(arrived) == ids, "not every request was in flight at once"
    assert len(written) == len(ids) * parts
    decoded = [json.loads(line) for line in written]  # every line is a whole JSON message
    groups = [key for key, _ in itertools.groupby(msg["id"] for msg in decoded)]
    assert sorted(groups) == ids, f"a response was interleaved with another: {groups}"
    for req_id, group in itertools.groupby(decoded, key=lambda msg: msg["id"]):
        assert [msg["part"] for msg in group] == list(range(parts)), f"id {req_id} reordered"


@pytest.mark.asyncio
async def test_backpressure_stops_reading_stdin_at_the_cap(monkeypatch):
    """Stdin is not drained past the in-flight cap, so nothing queues without bound.

    This one runs the real reader thread against a fake stdin, because the cap is
    enforced by that thread blocking before it reads the next line.
    """

    class _CountingStdin:
        def __init__(self, lines):
            self._lines = list(lines)
            self.handed_out = 0

        def readline(self):
            if not self._lines:
                return b""
            self.handed_out += 1
            return self._lines.pop(0) + b"\n"

    class _Stdin:
        def __init__(self, buffer):
            self.buffer = buffer

    ids = list(range(1, 6))
    source = _CountingStdin([_request(i) for i in ids])
    gate = asyncio.Event()
    seen = []

    async def handler(request):
        seen.append(json.loads(request.content)["id"])
        await gate.wait()
        return httpx.Response(200, json=_result(json.loads(request.content)["id"]))

    written = []
    monkeypatch.setenv("CPERSONA_PROXY_MAX_INFLIGHT", "2")
    monkeypatch.setattr(proxy_stdio.sys, "stdin", _Stdin(source))
    monkeypatch.setattr(proxy_stdio, "_write_stdout", written.append)
    monkeypatch.setattr(proxy_stdio.httpx, "AsyncClient", _client_factory(handler))

    main_task = asyncio.create_task(proxy_stdio.main())
    try:
        await _wait_until(lambda: len(seen) >= 2)
        await asyncio.sleep(0.2)  # give a leak time to show up
        assert seen == [1, 2], f"more than the cap was forwarded: {seen}"
        assert source.handed_out == 2, f"stdin was read past the cap: {source.handed_out}"
    finally:
        gate.set()
        try:
            await asyncio.wait_for(main_task, timeout=10)
        except BaseException:
            main_task.cancel()
            raise

    assert source.handed_out == len(ids)
    assert sorted(json.loads(m)["id"] for m in written) == ids


@pytest.mark.asyncio
async def test_cancellation_notification_is_delivered_while_a_request_is_in_flight(monkeypatch):
    """notifications/cancelled reaches the remote before its target returns.

    Under the serial pump the cancel sat unread behind the very request it was
    cancelling, so it could only be delivered after that request had finished —
    cancellation was structurally impossible, not merely slow.
    """
    cancelled = asyncio.Event()
    cancel_targets = []

    async def handler(request):
        payload = json.loads(request.content)
        if payload.get("method") == "notifications/cancelled":
            cancel_targets.append(payload["params"]["requestId"])
            cancelled.set()
            return httpx.Response(202)
        await cancelled.wait()
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "error": {"code": -32800, "message": "Request cancelled"},
            },
        )

    cancel_line = json.dumps(
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}}
    ).encode()

    written = []
    monkeypatch.setattr(proxy_stdio, "_write_stdout", written.append)
    monkeypatch.setattr(proxy_stdio.httpx, "AsyncClient", _client_factory(handler))
    monkeypatch.setattr(
        proxy_stdio.threading, "Thread", _scripted_thread([_request(1), cancel_line])
    )

    await asyncio.wait_for(proxy_stdio.main(), timeout=10)

    assert cancel_targets == [1], "the cancel never reached the remote in time"
    # 202 with an empty body owes nothing back; only the cancelled call answers.
    assert [json.loads(m) for m in written] == [
        {"jsonrpc": "2.0", "id": 1, "error": {"code": -32800, "message": "Request cancelled"}}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["transport", "http-502"])
async def test_failed_forward_still_answers_through_the_single_writer(monkeypatch, failure):
    """A failing forward reports its own error and leaves its neighbour untouched.

    The error paths (bug-051 transport failures, bug-063/bug-082 non-2xx) now
    hand their reply to the writer instead of writing it inline, so they are
    exercised here in the concurrent flow rather than only in isolation.
    """

    async def handler(request):
        req_id = json.loads(request.content)["id"]
        if req_id != 1:
            return httpx.Response(200, json=_result(req_id))
        if failure == "transport":
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(502, text="<html>bad gateway</html>")

    written = []
    monkeypatch.setattr(proxy_stdio, "_write_stdout", written.append)
    monkeypatch.setattr(proxy_stdio.httpx, "AsyncClient", _client_factory(handler))
    monkeypatch.setattr(proxy_stdio.threading, "Thread", _scripted_thread([_request(1), _request(2)]))

    await asyncio.wait_for(proxy_stdio.main(), timeout=10)

    by_id = {json.loads(m)["id"]: json.loads(m) for m in written}
    assert set(by_id) == {1, 2}
    assert by_id[2] == _result(2)
    assert by_id[1]["error"]["code"] == -32000


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, proxy_stdio.DEFAULT_MAX_INFLIGHT),
        ("", proxy_stdio.DEFAULT_MAX_INFLIGHT),
        ("16", 16),
        ("not-a-number", proxy_stdio.DEFAULT_MAX_INFLIGHT),
        ("0", 1),
        ("-4", 1),
    ],
)
def test_max_inflight_env_override(monkeypatch, raw, expected):
    """The cap is tunable, and a nonsense value degrades to something workable."""
    if raw is None:
        monkeypatch.delenv("CPERSONA_PROXY_MAX_INFLIGHT", raising=False)
    else:
        monkeypatch.setenv("CPERSONA_PROXY_MAX_INFLIGHT", raw)

    assert proxy_stdio._max_inflight() == expected


@pytest.mark.asyncio
async def test_invalid_utf8_line_gets_an_id_null_error_and_kills_nothing(monkeypatch):
    """A line that is invalid UTF-8 (not merely invalid JSON) is answered with the
    bug-135 id:null error instead of killing the emitter.

    json.loads(b'\\x80...') raises UnicodeDecodeError before any JSON parsing —
    a ValueError that is NOT a JSONDecodeError. Before the review fix it escaped
    _write_error inside the writer task, and the bridge kept forwarding requests
    while emitting nothing for the rest of the session.
    """
    poison = b"\x80\x81\x82"

    async def handler(request):
        try:
            payload = json.loads(request.content)
        except ValueError:
            return httpx.Response(502, text="bad request bytes")
        return httpx.Response(200, json=_result(payload["id"]))

    written = []
    monkeypatch.setattr(proxy_stdio, "_write_stdout", written.append)
    monkeypatch.setattr(proxy_stdio.httpx, "AsyncClient", _client_factory(handler))
    monkeypatch.setattr(proxy_stdio.threading, "Thread", _scripted_thread([poison, _request(2)]))

    await asyncio.wait_for(proxy_stdio.main(), timeout=10)

    by_id = {json.loads(m)["id"]: json.loads(m) for m in written}
    assert set(by_id) == {None, 2}, "both the poison line and its neighbour must be answered"
    assert by_id[None]["error"]["code"] == -32000
    assert by_id[2] == _result(2)


@pytest.mark.asyncio
async def test_writer_survives_a_poisoned_emit_and_later_responses_flow(monkeypatch):
    """One response whose emission raises is dropped; every later response is
    still written and main() exits cleanly (the per-item guard in the writer)."""
    written = []
    calls = {"n": 0}

    def flaky_write(message):
        calls["n"] += 1
        if calls["n"] == 1:
            raise BrokenPipeError("simulated torn pipe on one write")
        written.append(message)

    unblock = asyncio.Event()

    async def handler(request):
        payload = json.loads(request.content)
        if payload["id"] == 1:
            unblock.set()
        else:
            await unblock.wait()  # id 2 finishes after id 1, so id 1 is emitted first
        return httpx.Response(200, json=_result(payload["id"]))

    monkeypatch.setattr(proxy_stdio, "_write_stdout", flaky_write)
    monkeypatch.setattr(proxy_stdio.httpx, "AsyncClient", _client_factory(handler))
    monkeypatch.setattr(proxy_stdio.threading, "Thread", _scripted_thread([_request(1), _request(2)]))

    await asyncio.wait_for(proxy_stdio.main(), timeout=10)

    assert [json.loads(m)["id"] for m in written] == [2], (
        "the poisoned emit (id 1) is dropped; the survivor (id 2) must still flow"
    )
