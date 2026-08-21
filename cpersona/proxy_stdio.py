"""
CPersona stdio-to-HTTP proxy for Claude Code.

Bridges local MCP stdio transport to a remote CPersona Streamable HTTP server,
enabling Claude Code (which only supports stdio MCP) to use a remote DB.

Env vars:
  CPERSONA_REMOTE_URL          - Remote MCP endpoint (default: http://localhost:8402/mcp)
  CPERSONA_AUTH_TOKEN          - Bearer token for authentication (required)
  CPERSONA_PROXY_MAX_INFLIGHT  - Messages accepted from stdin but not yet answered
                                 (default: 8). Also the cap on concurrent POSTs.
"""

import asyncio
import json
import logging
import os
import sys
import threading
from functools import partial
from typing import NamedTuple

import httpx

logger = logging.getLogger("cpersona-proxy")

REMOTE_URL = os.environ.get("CPERSONA_REMOTE_URL", "http://localhost:8402/mcp")
AUTH_TOKEN = os.environ.get("CPERSONA_AUTH_TOKEN", "")

# bug-253: how many messages may be read from stdin without having been answered
# yet. It doubles as the concurrency cap, because a message is only read once a
# slot is free, so at most this many POSTs can be in flight. 8 keeps every
# concurrent forward on a pooled connection (httpx keeps 20 keep-alive
# connections by default, so nothing here forces a new handshake) while being
# far above what one MCP client has outstanding in practice (an initialize, a
# few tool calls, the odd notification).
DEFAULT_MAX_INFLIGHT = 8


def _max_inflight() -> int:
    """Resolve the in-flight cap from the environment, clamped to a usable value."""
    raw = os.environ.get("CPERSONA_PROXY_MAX_INFLIGHT", "").strip()
    if not raw:
        return DEFAULT_MAX_INFLIGHT
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Ignoring non-integer CPERSONA_PROXY_MAX_INFLIGHT=%r; using %d", raw, DEFAULT_MAX_INFLIGHT
        )
        return DEFAULT_MAX_INFLIGHT
    if value < 1:
        logger.warning("CPERSONA_PROXY_MAX_INFLIGHT=%d is below 1; using 1", value)
        return 1
    return value


def _read_stdin_lines(queue: asyncio.Queue, loop: asyncio.AbstractEventLoop, slots=None):
    """Read lines from stdin in a background thread (Windows-compatible).

    bug-253: ``slots`` is the backpressure gate. A slot is taken BEFORE the next
    line is read and returned by the forwarder that answers it, so stdin is not
    drained faster than the remote answers and the hand-off queue cannot grow
    without bound. Reading first and gating afterwards would let one extra line
    through per cycle and, more importantly, would not be "stop reading".

    The EOF sentinel is pushed without a slot: once EOF has been READ, shutdown
    is never gated on work that is still in flight. The read itself is another
    matter — while the cap is saturated, NOTHING further is read, the next
    request, a notifications/cancelled and the EOF alike. That is the price of
    genuine backpressure: prompt cancel delivery is only guaranteed while fewer
    than max_inflight requests are outstanding; at saturation the head-of-line
    wait returns, bounded by the cap rather than by one request.
    """
    try:
        stream = sys.stdin.buffer
        while True:
            if slots is not None:
                slots.acquire()
            line = stream.readline()
            if not line:
                if slots is not None:
                    slots.release()
                break
            line = line.strip()
            if not line:
                # A blank line owes no answer, so nothing will release its slot.
                if slots is not None:
                    slots.release()
                continue
            loop.call_soon_threadsafe(queue.put_nowait, line)
    except (EOFError, OSError):
        pass
    finally:
        loop.call_soon_threadsafe(queue.put_nowait, None)


class _ErrorFor(NamedTuple):
    """An output item asking the writer to emit the error owed for a request line."""

    request_line: bytes | str
    message: str


async def _stdout_writer(out_queue: asyncio.Queue):
    """Drain the output queue; the only coroutine that ever touches stdout.

    bug-253: with forwards running concurrently the responses no longer arrive
    in request order, so emission is funnelled through one consumer. Items are
    whole responses (a JSON reply is one line, an SSE body can be several), which
    is what keeps the lines of one response contiguous instead of interleaved
    with another response that happened to finish in between.
    """
    while True:
        item = await out_queue.get()
        if item is None:
            return
        # Per-item guard: one poisoned response must not kill the only consumer.
        # An unguarded writer dies silently here (create_task holds the exception
        # until the final await), and the bridge then keeps reading stdin and
        # POSTing to the remote while emitting nothing — side effects execute,
        # answers never arrive, and nothing is logged for the whole session.
        # With the guard, that one request stays unanswered (its client times
        # out, same as a transport error) and every later response still flows.
        try:
            if isinstance(item, _ErrorFor):
                _write_error(item.request_line, item.message)
            else:
                for message in item:
                    _write_stdout(message)
        except Exception:
            logger.exception("Failed to emit a response; dropping it and continuing")


def _response_messages(response: httpx.Response) -> list[str]:
    """Extract the JSON-RPC messages carried by a successful remote response."""
    content_type = response.headers.get("content-type", "")
    messages: list[str] = []
    if "text/event-stream" in content_type:
        # SSE: extract data lines
        for sse_line in response.text.split("\n"):
            # bug-135: SSE permits data fields with or without a space.
            if sse_line.startswith("data:"):
                data = sse_line[len("data:") :].strip()
                if data:
                    messages.append(data)
    else:
        # JSON response
        text = response.text.strip()
        if text:
            messages.append(text)
    return messages


async def _forward(
    client: httpx.AsyncClient,
    line: bytes | str,
    session: dict,
    out_queue: asyncio.Queue,
    slots=None,
):
    """Forward one stdin message to the remote and queue whatever it owes back.

    bug-253: this used to be the body of the read loop, which made the bridge a
    strictly serial pump — one round trip (up to the 300 s read timeout) blocked
    every other message on the connection, and a notifications/cancelled for the
    stuck request could not be delivered until that request finished, i.e. never.
    JSON-RPC allows several outstanding requests on one connection and the remote
    is stateless, so each message is now its own task.
    """
    try:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if AUTH_TOKEN:
            headers["Authorization"] = f"Bearer {AUTH_TOKEN}"
        # Read at send time, not at loop time, so a session id established by an
        # earlier response is picked up by later requests.
        if session.get("id"):
            headers["Mcp-Session-Id"] = session["id"]

        try:
            response = await client.post(REMOTE_URL, content=line, headers=headers)
        except (httpx.TransportError, httpx.HTTPError) as e:
            # bug-051: catch the whole transport/HTTP hierarchy, not just
            # ConnectError + ReadTimeout. ConnectTimeout/PoolTimeout/WriteTimeout
            # (TimeoutException) and RemoteProtocolError (ProtocolError) are
            # NEITHER of those, so before this any of them escaped the loop and
            # killed main() — a single transient network blip permanently took
            # down the whole stdio bridge for the session. Report a JSON-RPC
            # error for this one request and keep the bridge alive.
            out_queue.put_nowait(_ErrorFor(line, f"Remote request failed: {type(e).__name__}: {e}"))
            return

        # Track session ID. The remote runs stateless and never issues one; if a
        # deployment ever does, the first response that carries it wins and the
        # rest of the session reuses it (assignment cannot interleave — the tasks
        # share one event loop thread).
        if "mcp-session-id" in response.headers:
            session["id"] = response.headers["mcp-session-id"]

        # bug-063: httpx does not raise for 4xx/5xx without raise_for_status(). Before
        # this a non-2xx body (a 502 gateway HTML page, a 401 auth JSON, a 500 stack
        # trace) with a non-SSE content-type fell through to _write_stdout and was
        # emitted verbatim as if it were a JSON-RPC message — desyncing the client's
        # line reader and leaving this request unanswered (it hangs until timeout).
        # Surface it as a proper id-keyed JSON-RPC error like the transport path and
        # keep the bridge alive, instead of corrupting the stream.
        # bug-082: the >= 400 guard left 3xx open. The client is built without
        # follow_redirects, so a reverse proxy's 301/302/307/308 (http->https or
        # trailing-slash canonicalization of /mcp) surfaced here with an HTML body
        # and fell through to _write_stdout — the same stream corruption, from the
        # redirect range. Only a 2xx can carry a JSON-RPC/SSE payload; reject
        # everything else.
        if not (200 <= response.status_code < 300):
            out_queue.put_nowait(
                _ErrorFor(line, f"Remote returned HTTP {response.status_code}: {response.text.strip()[:200]}")
            )
            return

        out_queue.put_nowait(_response_messages(response))
    except Exception as e:  # noqa: BLE001 - a forward must never take the bridge down
        logger.exception("Unhandled error while forwarding a message")
        out_queue.put_nowait(_ErrorFor(line, f"Proxy error: {type(e).__name__}: {e}"))
    finally:
        # Answered (or given up on): let the reader take the next line.
        if slots is not None:
            slots.release()


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s: %(message)s", stream=sys.stderr)
    max_inflight = _max_inflight()
    logger.info("Proxy starting: %s (max in-flight: %d)", REMOTE_URL, max_inflight)

    session: dict = {"id": None}
    queue: asyncio.Queue = asyncio.Queue()
    out_queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()
    # A plain Semaphore, not a BoundedSemaphore: an unmatched release must not
    # raise inside a forward and take the bridge down with it.
    slots = threading.Semaphore(max_inflight)

    # Start stdin reader thread
    reader_thread = threading.Thread(
        target=partial(_read_stdin_lines, slots=slots), args=(queue, loop), daemon=True
    )
    reader_thread.start()

    writer = asyncio.create_task(_stdout_writer(out_queue))
    in_flight: set[asyncio.Task] = set()

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=300.0)) as client:
            while True:
                line = await queue.get()
                if line is None:
                    break

                # The writer has a per-item guard, so it only dies on something
                # structural. If it does, stop accepting work: a bridge that
                # forwards requests it can never answer is worse than one that
                # exits loudly (the await in `finally` re-raises its exception).
                if writer.done():
                    logger.error("stdout writer terminated unexpectedly; shutting down the bridge")
                    break

                task = asyncio.create_task(_forward(client, line, session, out_queue, slots))
                in_flight.add(task)
                task.add_done_callback(in_flight.discard)

            # stdin is closed, but requests already accepted still owe answers.
            if in_flight:
                await asyncio.gather(*in_flight, return_exceptions=True)
    finally:
        out_queue.put_nowait(None)
        await writer


def _write_stdout(message: str):
    """Write a JSON-RPC message to stdout."""
    sys.stdout.write(message + "\n")
    sys.stdout.flush()


def _write_error(request_line: bytes | str, error_msg: str):
    """Write a JSON-RPC error response to stdout — unless the line was a notification.

    bug-240: a notification is a well-formed object with no ``id``, and
    JSON-RPC 2.0 says a server MUST NOT reply to one. Answering it with the
    ``id: null`` error emitted a response the client is not tracking (an SDK
    either logs a protocol error or tears the session down) for a
    fire-and-forget message that needed no answer. The ``id: null`` reply
    belongs to a DIFFERENT case — bug-135's line whose id could not be PARSED,
    where the client IS waiting and has no other way to learn the request
    failed. Keep it there, and only there.
    """
    parsed = True
    try:
        req = json.loads(request_line)
    except ValueError:
        # ValueError, not JSONDecodeError: json.loads(b'\x80...') raises
        # UnicodeDecodeError BEFORE any JSON parsing happens, and both are
        # ValueError subclasses. Catching only the narrower one let a line that
        # is invalid UTF-8 (rather than invalid JSON) escape and kill the caller.
        parsed, req = False, None

    if parsed and isinstance(req, dict):
        if "id" not in req:
            logger.warning(
                "notification %r not forwarded (no JSON-RPC reply is owed): %s",
                req.get("method", "<unknown>"),
                error_msg,
            )
            return
        req_id = req["id"]
    else:
        # Unparseable, or a shape with no single id to answer under (a batch
        # array): the client is waiting on something it can only be released
        # from by a reply, so keep bug-135's id:null error.
        req_id = None

    error = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32000, "message": error_msg},
        }
    )
    _write_stdout(error)


if __name__ == "__main__":
    asyncio.run(main())
