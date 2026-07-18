"""Execution tests for the remote stdio transport proxy."""

import json

import pytest

from cpersona import proxy_stdio


class _FakeResponse:
    status_code = 200
    headers = {"content-type": "text/event-stream"}
    text = 'data: {"id":1}\n\ndata:{"id":2}\n'


class _FakeClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def post(self, url, content, headers):
        return _FakeResponse()


class _FakeThread:
    def __init__(self, target, args, daemon):
        self.queue, self.loop = args

    def start(self):
        self.loop.call_soon(self.queue.put_nowait, b'{"id":1}')
        self.loop.call_soon(self.queue.put_nowait, None)


@pytest.mark.asyncio
async def test_sse_data_lines_allow_optional_space(monkeypatch):
    written = []
    monkeypatch.setattr(proxy_stdio.httpx, "AsyncClient", lambda **kwargs: _FakeClient())
    monkeypatch.setattr(proxy_stdio.threading, "Thread", _FakeThread)
    monkeypatch.setattr(proxy_stdio, "_write_stdout", written.append)

    await proxy_stdio.main()

    assert written == ['{"id":1}', '{"id":2}']


@pytest.mark.parametrize(
    ("request_line", "expected_id"),
    [('{"jsonrpc":"2.0","id":42}', 42), ("{malformed", None)],
)
def test_write_error_always_emits_response(monkeypatch, request_line, expected_id):
    written = []
    monkeypatch.setattr(proxy_stdio, "_write_stdout", written.append)

    proxy_stdio._write_error(request_line, "remote failed")

    assert len(written) == 1
    error = json.loads(written[0])
    assert error == {
        "jsonrpc": "2.0",
        "id": expected_id,
        "error": {"code": -32000, "message": "remote failed"},
    }
