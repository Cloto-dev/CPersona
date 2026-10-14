"""The checkup CLI checks the backend the server would use; remote recall clears a fault.

Two defects in how the embedding backend is observed outside the MCP server's own
recall-embed path.

1. ``python -m cpersona.checkup`` ran the check registry without ever installing an
   embedding client -- only ``server.main()`` built one. Every client-dependent check
   then behaved as if ``CPERSONA_EMBEDDING_MODE`` were ``none``: dimension drift was
   not looked for, a NULL-embedding backlog was demoted to ``info``, and ``--fix``
   re-embedded nothing. The CLI documented as the CI form of ``check_health`` exited
   0 on a database the MCP tool calls unhealthy. These tests run the real CLI in a
   subprocess against a fake ``/embed`` server on a loopback port.

2. Under ``CPERSONA_VECTOR_SEARCH_MODE=remote`` a recall whose remote ``/search``
   answers returns before the local query embed, the only call that recorded a
   recovery. A fault latched by two maintenance probes during a short outage was
   therefore never cleared, and every later recall carried the degraded advisory.
"""

from __future__ import annotations

import json
import os
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "test_2611a2_w07.db"))
os.environ.setdefault("CPERSONA_EMBEDDING_MODE", "none")

import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from cpersona import admin_handlers  # noqa: E402
from cpersona import checks  # noqa: E402
from cpersona import config  # noqa: E402
from cpersona import health  # noqa: E402
from cpersona import maintenance_handlers  # noqa: E402
from cpersona import memory_handlers  # noqa: E402
from cpersona import vector  # noqa: E402
from cpersona._vendored_mcp_common.embedding_client import EmbedOutcome  # noqa: E402
from cpersona.database import get_db  # noqa: E402

# --------------------------------------------------------------------------
# 1. checkup CLI installs the embedding client the server would build
# --------------------------------------------------------------------------

#: The dimension the fake backend answers with; the seeded drift rows are narrower.
BACKEND_DIM = 16
STALE_DIM = 8
#: Ports reserved for other services on the machines this suite runs on.
_RESERVED_PORTS = {8502, 18777, 18778}


class _FakeEmbedServer:
    """A loopback ``/embed`` endpoint that counts the texts it embedded."""

    def __init__(self) -> None:
        self.embedded_texts: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - http.server naming
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path != "/embed":
                    self.send_response(404)
                    self.end_headers()
                    return
                texts = body.get("texts") or []
                outer.embedded_texts.extend(texts)
                payload = json.dumps(
                    {"embeddings": [[0.1 * (i + 1)] * BACKEND_DIM for i, _ in enumerate(texts)]}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):  # keep the test output clean
                pass

        for _ in range(20):
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            if server.server_address[1] not in _RESERVED_PORTS:
                break
            server.server_close()
        self.server = server
        self.url = f"http://127.0.0.1:{server.server_address[1]}/embed"
        self.thread = threading.Thread(target=server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def _seed(db_path: str, *, stale_rows: int, null_rows: int) -> None:
    """Create the schema through the package, then write rows with chosen blobs."""
    seed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio\n"
            "from cpersona.database import get_db, close_db\n"
            "async def s():\n"
            "    await get_db()\n"
            "    await close_db()\n"
            "asyncio.run(s())",
        ],
        env=dict(os.environ, CPERSONA_DB_PATH=db_path, CPERSONA_EMBEDDING_MODE="none"),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert seed.returncode == 0, seed.stderr
    source = '{"type":"User","id":"u","name":"n"}'
    stale = struct.pack(f"<{STALE_DIM}f", *([0.1] * STALE_DIM))
    with sqlite3.connect(db_path) as raw:
        for i in range(stale_rows + null_rows):
            raw.execute(
                "INSERT INTO memories (agent_id, content, source, timestamp, embedding)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    "agent.w07",
                    f"row number {i} about topic {i * 7}",
                    source,
                    "2026-07-01T00:00:00+00:00",
                    stale if i < stale_rows else None,
                ),
            )


def _checkup(db_path: str, *args: str, mode: str, url: str = "") -> subprocess.CompletedProcess:
    env = dict(os.environ, CPERSONA_EMBEDDING_MODE=mode, CPERSONA_EMBEDDING_URL=url)
    env.pop("CPERSONA_VECTOR_SEARCH_MODE", None)
    return subprocess.run(
        [sys.executable, "-m", "cpersona.checkup", "--db", db_path, *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def _issues(report: dict) -> dict[str, dict]:
    return {issue["type"]: issue for issue in report["issues"]}


def test_checkup_reports_dimension_drift_and_null_backlog_like_check_health(tmp_path):
    """Two rows of a narrower model and eight unembedded rows, backend answering 16-d.

    check_health over MCP calls this database unhealthy on two critical findings. The
    CLI used to exit 0 with ``healthy``: no client meant no dimension check and a
    NULL backlog demoted to ``info``.
    """
    db_path = str(tmp_path / "drift.db")
    _seed(db_path, stale_rows=2, null_rows=8)
    with _FakeEmbedServer() as backend:
        result = _checkup(db_path, "--json", "--strict", mode="http", url=backend.url)
    assert result.returncode == 2, (result.returncode, result.stdout[-2000:], result.stderr[-2000:])
    report = json.loads(result.stdout)
    assert report["status"] != "healthy"
    issues = _issues(report)
    assert issues["embedding_dimension_mismatch"]["severity"] == "critical"
    assert issues["embedding_dimension_mismatch"]["count"] == 2
    assert issues["embedding_dimension_mismatch"]["expected_dim"] == BACKEND_DIM
    assert issues["null_embedding"]["severity"] == "critical"


def test_checkup_fix_reembeds_through_the_configured_backend(tmp_path):
    """``--fix`` re-embeds the backlog and the drifted rows, so the run converges.

    Without a client the repair had no writer: nothing was sent to the backend and the
    rows stayed NULL, while the hint told the operator to restart CPersona.
    """
    db_path = str(tmp_path / "fix.db")
    _seed(db_path, stale_rows=2, null_rows=3)
    with _FakeEmbedServer() as backend:
        result = _checkup(db_path, "--json", "--strict", "--fix", mode="http", url=backend.url)
        embedded = list(backend.embedded_texts)
    assert result.returncode == 0, (result.returncode, result.stdout[-2000:], result.stderr[-2000:])
    with sqlite3.connect(db_path) as raw:
        lengths = [r[0] for r in raw.execute("SELECT length(embedding) FROM memories ORDER BY id")]
    assert lengths == [BACKEND_DIM * 4] * 5
    # Every row's own text went to the backend (the probe text rides along too).
    assert {f"row number {i} about topic {i * 7}" for i in range(5)} <= set(embedded)


def test_checkup_refuses_an_unsupported_embedding_mode(tmp_path):
    """The server refuses to start on a mode the client cannot act on; the gate must
    not pass that configuration as healthy."""
    db_path = str(tmp_path / "bogus.db")
    _seed(db_path, stale_rows=0, null_rows=2)
    result = _checkup(db_path, "--json", "--strict", mode="bogus")
    assert result.returncode == 2, (result.returncode, result.stdout[-2000:], result.stderr[-2000:])
    assert "CPERSONA_EMBEDDING_MODE" in result.stderr


def test_checkup_mode_none_still_reports_null_as_info(tmp_path):
    """Control: under mode=none a NULL corpus is the resting state, and the run passes."""
    db_path = str(tmp_path / "none.db")
    _seed(db_path, stale_rows=0, null_rows=3)
    result = _checkup(db_path, "--json", "--strict", mode="none")
    assert result.returncode == 0, (result.returncode, result.stdout[-2000:], result.stderr[-2000:])
    issues = _issues(json.loads(result.stdout))
    assert issues["null_embedding"]["severity"] == "info"


# --------------------------------------------------------------------------
# 2. A remote /search answer clears a fault latched by maintenance probes
# --------------------------------------------------------------------------

AGENT = "agent.w07-remote"


class _RemoteClient:
    """An http-mode client whose ``/embed`` can be taken down; ``/search`` and
    ``/index`` are answered by an in-process transport."""

    mode = "http"
    _http_url = "http://fake.invalid/embed"

    def __init__(self, http: httpx.AsyncClient, state: dict) -> None:
        self._client = http
        self._state = state

    async def embed(self, texts):
        embeddings, _ = await self.embed_with_outcome(texts)
        return embeddings

    async def embed_with_outcome(self, texts):
        if not self._state["embed_up"]:
            return None, EmbedOutcome(
                attempted=True, ok=False, error="ConnectError at http://fake.invalid/embed"
            )
        return [[0.1 * (i + 1)] * 8 for i, _ in enumerate(texts)], EmbedOutcome(attempted=True, ok=True)

    @staticmethod
    def pack_embedding(embedding):
        return struct.pack(f"<{len(embedding)}f", *embedding)


@pytest_asyncio.fixture
async def remote_mode(monkeypatch):
    index: dict[str, str] = {}
    state = {"embed_up": True, "search_up": True}

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        if request.url.path == "/index":
            for item in body["items"]:
                index[item["id"]] = item["text"]
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/search":
            if not state["search_up"]:
                return httpx.Response(503, json={"error": "down"})
            hits = [{"id": i, "score": 0.9} for i in index][: body["limit"]]
            return httpx.Response(200, json={"results": hits})
        return httpx.Response(404)

    monkeypatch.setattr(config, "EMBEDDING_MODE", "http")
    for module in (vector, admin_handlers, memory_handlers, checks):
        monkeypatch.setattr(module, "VECTOR_SEARCH_MODE", "remote")
    db = await get_db()
    await db.execute("DELETE FROM memories WHERE agent_id = ?", (AGENT,))
    await db.commit()
    health._reset()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        monkeypatch.setattr(vector, "_embedding_client", _RemoteClient(http, state))
        yield state
    health._reset()


async def _latch_fault_with_maintenance(state: dict) -> None:
    stored = await memory_handlers.do_store(AGENT, {"content": "the deploy runbook lives in ops"})
    assert stored.get("result") == "stored", stored
    state["embed_up"] = False
    for _ in range(2):
        await maintenance_handlers.do_check_health(AGENT, fix=True)
    assert health.is_faulted(), health.observed_state()
    state["embed_up"] = True


@pytest.mark.asyncio
async def test_remote_search_answer_clears_a_fault_latched_by_maintenance(remote_mode):
    await _latch_fault_with_maintenance(remote_mode)

    res = await memory_handlers.do_recall(AGENT, "deploy runbook", 5)
    assert res.get("messages"), res

    assert not health.is_faulted(), health.observed_state()
    assert health.observed_state()["state"] == "healthy"
    # The next recall carries no degraded advisory: the backend answered.
    again = await memory_handlers.do_recall(AGENT, "deploy runbook", 5)
    assert (again.get("advisory") or {}).get("severity") != "fault", again.get("advisory")


@pytest.mark.asyncio
async def test_remote_search_failure_does_not_clear_the_fault(remote_mode):
    """Control: a /search that did not answer is no evidence of recovery, and the local
    fallback's own embed is what decides -- here it fails, so the fault stays."""
    await _latch_fault_with_maintenance(remote_mode)
    remote_mode["search_up"] = False
    remote_mode["embed_up"] = False

    await memory_handlers.do_recall(AGENT, "deploy runbook", 5)
    assert health.is_faulted(), health.observed_state()
