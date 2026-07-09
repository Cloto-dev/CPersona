"""
CPersona stdio-to-HTTP proxy for Claude Code.

Bridges local MCP stdio transport to a remote CPersona Streamable HTTP server,
enabling Claude Code (which only supports stdio MCP) to use a remote DB.

Env vars:
  CPERSONA_REMOTE_URL  - Remote MCP endpoint (default: http://localhost:8402/mcp)
  CPERSONA_AUTH_TOKEN  - Bearer token for authentication (required)
"""

import asyncio
import json
import logging
import os
import sys
import threading

import httpx

logger = logging.getLogger("cpersona-proxy")

REMOTE_URL = os.environ.get("CPERSONA_REMOTE_URL", "http://localhost:8402/mcp")
AUTH_TOKEN = os.environ.get("CPERSONA_AUTH_TOKEN", "")


def _read_stdin_lines(queue: asyncio.Queue, loop: asyncio.AbstractEventLoop):
    """Read lines from stdin in a background thread (Windows-compatible)."""
    try:
        for line in sys.stdin.buffer:
            line = line.strip()
            if line:
                loop.call_soon_threadsafe(queue.put_nowait, line)
    except (EOFError, OSError):
        pass
    finally:
        loop.call_soon_threadsafe(queue.put_nowait, None)


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s: %(message)s", stream=sys.stderr)
    logger.info("Proxy starting: %s", REMOTE_URL)

    session_id: str | None = None
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    # Start stdin reader thread
    reader_thread = threading.Thread(target=_read_stdin_lines, args=(queue, loop), daemon=True)
    reader_thread.start()

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=300.0)) as client:
        while True:
            line = await queue.get()
            if line is None:
                break

            # Forward to remote HTTP server
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            }
            if AUTH_TOKEN:
                headers["Authorization"] = f"Bearer {AUTH_TOKEN}"
            if session_id:
                headers["Mcp-Session-Id"] = session_id

            try:
                response = await client.post(REMOTE_URL, content=line, headers=headers)
            except (httpx.TransportError, httpx.HTTPError) as e:
                # bug-051: catch the whole transport/HTTP hierarchy, not just
                # ConnectError + ReadTimeout. ConnectTimeout/PoolTimeout/WriteTimeout
                # (TimeoutException) and RemoteProtocolError (ProtocolError) are
                # NEITHER of those, so before this any of them escaped the loop and
                # killed main() — a single transient network blip permanently took
                # down the whole stdio bridge for the session. Report a JSON-RPC
                # error for this one request and keep the loop alive.
                _write_error(line, f"Remote request failed: {type(e).__name__}: {e}")
                continue

            # Track session ID
            if "mcp-session-id" in response.headers:
                session_id = response.headers["mcp-session-id"]

            # bug-063: httpx does not raise for 4xx/5xx without raise_for_status(). Before
            # this a non-2xx body (a 502 gateway HTML page, a 401 auth JSON, a 500 stack
            # trace) with a non-SSE content-type fell through to _write_stdout and was
            # emitted verbatim as if it were a JSON-RPC message — desyncing the client's
            # line reader and leaving this request unanswered (it hangs until timeout).
            # Surface it as a proper id-keyed JSON-RPC error like the transport path and
            # keep the loop alive, instead of corrupting the stream.
            # bug-082: the >= 400 guard left 3xx open. The client is built without
            # follow_redirects, so a reverse proxy's 301/302/307/308 (http->https or
            # trailing-slash canonicalization of /mcp) surfaced here with an HTML body
            # and fell through to _write_stdout — the same stream corruption, from the
            # redirect range. Only a 2xx can carry a JSON-RPC/SSE payload; reject
            # everything else.
            if not (200 <= response.status_code < 300):
                _write_error(line, f"Remote returned HTTP {response.status_code}: {response.text.strip()[:200]}")
                continue

            # Parse response based on content type
            content_type = response.headers.get("content-type", "")
            if "text/event-stream" in content_type:
                # SSE: extract data lines
                for sse_line in response.text.split("\n"):
                    if sse_line.startswith("data: "):
                        data = sse_line[6:].strip()
                        if data:
                            _write_stdout(data)
            else:
                # JSON response
                if response.text.strip():
                    _write_stdout(response.text.strip())


def _write_stdout(message: str):
    """Write a JSON-RPC message to stdout."""
    sys.stdout.write(message + "\n")
    sys.stdout.flush()


def _write_error(request_line: bytes | str, error_msg: str):
    """Write a JSON-RPC error response to stdout."""
    try:
        req = json.loads(request_line)
        req_id = req.get("id")
    except (json.JSONDecodeError, AttributeError):
        req_id = None

    if req_id is not None:
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
