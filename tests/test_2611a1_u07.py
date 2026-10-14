"""Remote vector index coherence after maintenance repairs, and endpoint credentials
kept out of every error string and log line.

- bug-539: the health and deep-check repairs that delete or rewrite memory rows
  left the remote vector index (VECTOR_SEARCH_MODE=remote) holding vectors for rows
  that no longer exist and for text a row no longer holds. The user-facing delete
  and update paths already post /remove and /index; the repairs posted nothing.
- The embedding client removed a credential from httpx's exception text by
  replacing the configured URL spelling. httpx renders its own normalised form of
  the URL (lower-case host, no default port, percent-encoded userinfo and query),
  so any other spelling went through to the log and to the returned evidence that
  recall advisories quote.
- httpx logs every request at INFO with the full URL, userinfo and query included.
- The remote /search and /index paths logged the raw HTTPStatusError, whose message
  carries the request URL with its userinfo.
"""

import logging

import httpx
import pytest

from cpersona import admin_handlers as admin
from cpersona import checks
from cpersona import maintenance_handlers as maint
from cpersona import memory_handlers as mh
from cpersona import session
from cpersona import vector
from cpersona._vendored_mcp_common.embedding_client import EmbeddingClient
from cpersona.config import MAX_CONTENT_LENGTH
from cpersona.database import connection, get_db, transaction
from cpersona.isolation import isolation_where


# ==========================================================================
# bug-539 -- maintenance repairs must keep the remote index in step.
# ==========================================================================


class _FakeRemoteIndex:
    """An in-memory vector service honouring /index, /remove and /search."""

    def __init__(self):
        self.spaces: dict[str, dict[str, str]] = {}
        self.paths: list[str] = []

    def respond(self, request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content or b"{}")
        self.paths.append(request.url.path)
        space = self.spaces.setdefault(body.get("namespace", ""), {})
        if request.url.path == "/index":
            for item in body["items"]:
                space[item["id"]] = item["text"]
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/remove":
            for ref in body["ids"]:
                space.pop(ref, None)
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)


@pytest.fixture
def remote_index(monkeypatch, fake_embedding_client):
    session.reset_pauses_for_tests()
    service = _FakeRemoteIndex()
    for module in (admin, checks, mh, vector):
        monkeypatch.setattr(module, "VECTOR_SEARCH_MODE", "remote")
    monkeypatch.setattr(fake_embedding_client, "_http_url", "http://remote.invalid/embed")
    client = httpx.AsyncClient(transport=httpx.MockTransport(service.respond))
    monkeypatch.setattr(fake_embedding_client, "_client", client)
    yield service


async def _seed(agent: str, service: _FakeRemoteIndex, rows: list[tuple[str, str]]) -> dict[str, int]:
    """Insert (channel, content) rows directly and index each one remotely as written."""
    ids = {}
    space = service.spaces.setdefault(f"cpersona:{agent}", {})
    async with transaction() as db:
        for channel, content in rows:
            cursor = await db.execute(
                "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp)"
                " VALUES (?, '', ?, ?, '{}', '2026-01-01T00:00:00+00:00')",
                (agent, channel, content),
            )
            ids[content] = cursor.lastrowid
            space[f"mem:{cursor.lastrowid}"] = content
    return ids


async def _local(agent: str) -> dict[str, str]:
    async with connection() as db:
        rows = await db.execute_fetchall("SELECT id, content FROM memories WHERE agent_id = ?", (agent,))
    return {f"mem:{row_id}": content for row_id, content in rows}


@pytest.mark.asyncio
async def test_health_repairs_update_the_remote_index(remote_index):
    agent = "u07-health-repairs"
    oversized = "z" * (MAX_CONTENT_LENGTH + 40)
    ids = await _seed(
        agent,
        remote_index,
        [
            ("", "deploy runbook alpha"),  # duplicate_content survivor
            ("ch2", "deploy runbook alpha"),  # covered by the channel '' copy: deleted
            ("", "[Memory from carol] legacy deploy text"),  # memory_annotation rewrite
            ("", "<@123> mention body"),  # discord_mention rewrite
            ("", "keeper text"),
            ("", "[Memory from bob] keeper text"),  # rewrite collides -> deleted
            ("", "   "),  # empty_content delete
            ("", oversized),  # oversized_content rewrite (truncate)
        ],
    )
    before = dict(remote_index.spaces[f"cpersona:{agent}"])
    assert len(before) == 8

    result = await maint.do_check_health(
        agent,
        fix=True,
        checks=["memory_annotation", "discord_mention", "duplicate_content", "empty_content", "oversized_content"],
    )
    assert result["fixed"] is True, result

    local = await _local(agent)
    # The repairs did what they always did locally; this is the control that the
    # comparison below is about a run that actually changed rows.
    assert len(local) == 5, local
    assert local[f"mem:{ids['[Memory from carol] legacy deploy text']}"] == "legacy deploy text"
    assert local[f"mem:{ids['<@123> mention body']}"] == "mention body"
    assert local[f"mem:{ids[oversized]}"] == oversized[:MAX_CONTENT_LENGTH]

    remote = remote_index.spaces[f"cpersona:{agent}"]
    assert remote == local, {
        "ghost ids (deleted locally, still indexed)": sorted(set(remote) - set(local)),
        "stale text (rewritten locally, old text indexed)": sorted(
            k for k in remote if k in local and remote[k] != local[k]
        ),
    }


@pytest.mark.asyncio
async def test_deep_short_content_removes_from_the_remote_index(remote_index):
    agent = "u07-deep-short"
    ids = await _seed(agent, remote_index, [("", "ok"), ("", "hi"), ("", "a memory long enough to keep")])
    async with transaction() as db:
        await db.execute("UPDATE memories SET locked = 1 WHERE id = ?", (ids["hi"],))

    result = await maint.do_deep_check(agent, fix=True, checks=["short_content"])
    assert result["results"]["short_content"]["fixed"] == 1, result

    local = await _local(agent)
    assert set(local) == {f"mem:{ids['hi']}", f"mem:{ids['a memory long enough to keep']}"}
    assert remote_index.spaces[f"cpersona:{agent}"] == local


@pytest.mark.asyncio
async def test_a_crashed_repair_tells_the_remote_index_nothing(remote_index, monkeypatch):
    """A check that raises after writing is rolled back to its savepoint; the remote
    index must not be told about rows that are, after all, unchanged."""
    agent = "u07-crashed-repair"
    ids = await _seed(agent, remote_index, [("", "[Memory from dave] rolled back text")])
    check = checks._CHECKS_BY_NAME["memory_annotation"]
    real = check.runner

    async def crashing(db, agent_id, fix):
        await real(db, agent_id, fix)
        raise RuntimeError("probe crash after the rewrite")

    monkeypatch.setattr(check, "runner", crashing)
    remote_index.paths.clear()
    result = await maint.do_check_health(agent, fix=True, checks=["memory_annotation"])
    assert any(i.get("type") == "check_crashed" for i in result["issues"]), result
    local = await _local(agent)
    assert local == {f"mem:{ids['[Memory from dave] rolled back text']}": "[Memory from dave] rolled back text"}
    assert remote_index.paths == []
    assert remote_index.spaces[f"cpersona:{agent}"] == local


@pytest.mark.asyncio
async def test_a_report_only_run_posts_nothing(remote_index):
    agent = "u07-report-only"
    await _seed(agent, remote_index, [("ch1", "same text"), ("ch2", "same text")])
    remote_index.paths.clear()
    await maint.do_check_health(agent, fix=False, checks=["duplicate_content"])
    await maint.do_deep_check(agent, fix=False, checks=["short_content"])
    assert remote_index.paths == []
    assert len(remote_index.spaces[f"cpersona:{agent}"]) == 2


# ==========================================================================
# Endpoint credentials in the client's own failure evidence.
# ==========================================================================


def _failing(status: int, **kw) -> EmbeddingClient:
    client = EmbeddingClient(**kw)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(status)))
    return client


# (configured URL, secrets that must not appear in any spelling, host that must)
_SPELLINGS = [
    ("http://Embed.Local:8401/embed?token=SECRET1", ["SECRET1"], "embed.local"),
    ("http://embed.local:80/embed?token=SECRET2", ["SECRET2"], "embed.local"),
    ("http://svc:PASSWORD3@LOCALHOST:8401/embed?key=SECRET3", ["PASSWORD3", "SECRET3"], "localhost"),
    ("http://svc:Pw@SECRET4@embed.local/embed?k=v", ["SECRET4"], "embed.local"),
    ("http://embed.local:8401/embed?token=SE CRET5", ["CRET5"], "embed.local"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("url,secrets,host", _SPELLINGS, ids=[f"s{i}" for i in range(len(_SPELLINGS))])
@pytest.mark.parametrize("call", ["embed", "capabilities", "count_tokens"])
async def test_failure_evidence_carries_no_credential_in_any_spelling(caplog, url, secrets, host, call):
    client = _failing(503, mode="http", http_url=url)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="cpersona._vendored_mcp_common.embedding_client"):
        if call == "embed":
            _, outcome = await client.embed_with_outcome(["x"])
        elif call == "capabilities":
            _, outcome = await client.capabilities_with_outcome()
        else:
            _, outcome = await client.count_tokens_with_outcome(["x"])
    assert outcome.attempted and not outcome.ok
    # Control: the evidence still says where and how it failed, so a sanitiser that
    # passed by dropping the message would not look like this.
    assert host in outcome.error.lower() and "503" in outcome.error, outcome.error
    for secret in secrets:
        assert secret not in outcome.error, outcome.error
        assert secret not in caplog.text, caplog.text


@pytest.mark.asyncio
async def test_api_mode_evidence_carries_no_credential_with_uppercase_host_and_default_port(caplog):
    client = _failing(
        401,
        mode="api",
        api_url="https://user:PASSWORD6@Api.Example.com:443/v1/embeddings?key=SECRET6",
        api_key="k",
        model="m",
    )
    with caplog.at_level(logging.WARNING):
        _, outcome = await client.embed_with_outcome(["x"])
    assert "api.example.com" in outcome.error.lower() and "401" in outcome.error, outcome.error
    for secret in ("PASSWORD6", "SECRET6"):
        assert secret not in outcome.error, outcome.error
        assert secret not in caplog.text, caplog.text


# ==========================================================================
# httpx's own INFO request line.
# ==========================================================================


@pytest.mark.asyncio
async def test_httpx_request_log_line_carries_no_credential(caplog):
    client = EmbeddingClient(mode="http", http_url="http://svc:PASSWORD7@embed.local:8401/embed?token=SECRET7")
    await client.initialize()
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"embeddings": [[0.1, 0.2]]}))
    )
    caplog.clear()
    with caplog.at_level(logging.INFO):
        vectors = await client.embed(["hello"])
    await client._client.aclose()
    assert vectors == [[0.1, 0.2]]
    request_lines = [r.getMessage() for r in caplog.records if r.name == "httpx"]
    # Control: httpx did log the request (at INFO, which the server configures), so
    # the assertion below is about the line's content, not about its absence.
    assert any("HTTP Request: POST" in line and "embed.local" in line for line in request_lines), request_lines
    assert "PASSWORD7" not in caplog.text and "SECRET7" not in caplog.text, request_lines


# ==========================================================================
# Remote /search and /index failure warnings.
# ==========================================================================


@pytest.fixture
def failing_remote(monkeypatch, fake_embedding_client):
    session.reset_pauses_for_tests()
    for module in (mh, vector):
        monkeypatch.setattr(module, "VECTOR_SEARCH_MODE", "remote")
    monkeypatch.setattr(fake_embedding_client, "_http_url", "http://svc:PASSWORD8@embed.local:8401/embed")
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    monkeypatch.setattr(fake_embedding_client, "_client", client)
    return fake_embedding_client


@pytest.mark.asyncio
async def test_remote_search_failure_warning_carries_no_credential(caplog, failing_remote):
    db = await get_db()
    iso = isolation_where(agent_id="u07-remote-search")
    with caplog.at_level(logging.WARNING, logger=vector.logger.name):
        hits = await vector._search_vector_remote(
            db, "u07-remote-search", "anything", 3, 0.0,
            iso_fetch=iso, iso_ep_fetch=iso, src_clause="", src_params=(), src_like="", channel="",
        )
    assert hits is None
    lines = [r.getMessage() for r in caplog.records if "Remote vector search failed" in r.getMessage()]
    assert lines and "embed.local" in lines[0] and "500" in lines[0], caplog.text
    assert "PASSWORD8" not in caplog.text, lines


@pytest.mark.asyncio
async def test_remote_index_push_failure_warning_carries_no_credential(caplog, failing_remote):
    with caplog.at_level(logging.WARNING, logger=mh.logger.name):
        result = await mh.do_store("u07-remote-index", {"content": "apples and pears for the push"})
    assert result.get("ok") is not False, result
    lines = [r.getMessage() for r in caplog.records if "Remote index push failed" in r.getMessage()]
    assert lines and "embed.local" in lines[0] and "500" in lines[0], caplog.text
    assert "PASSWORD8" not in caplog.text, lines
