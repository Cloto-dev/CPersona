"""Regression test for the do_recall response path (v2.4.28).

v2.4.27 factored the post-recall scoring (episode penalty + confidence) out of
do_recall into _apply_recall_scoring, but do_recall's response-metadata loop and its
recall-count update reuse ``time_range_hours`` / ``recall_counts`` — which the refactor
left undefined in do_recall's scope. Under CONFIDENCE_ENABLED every recall then raised
``NameError: name 'recall_counts' is not defined``. The integration recall tests hang
without a resident embedding server, so this exercises do_recall's full code path with a
mocked DB + recall function instead, which runs in CI and catches this class of bug.
"""
import os
import tempfile

os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "x.db"))
os.environ["CPERSONA_EMBEDDING_MODE"] = "none"
os.environ["CPERSONA_CONFIDENCE_ENABLED"] = "true"  # the branch that regressed
os.environ["CPERSONA_RECALL_MODE"] = "rsf"

import httpx  # noqa: E402
import pytest  # noqa: E402

from cpersona import config # noqa: E402
from cpersona import health # noqa: E402
from cpersona import memory_handlers as M # noqa: E402
from cpersona import vector # noqa: E402
from cpersona._vendored_mcp_common.embedding_client import EmbeddingClient # noqa: E402


@pytest.fixture(autouse=True)
def _reset_health():
    """health is a process-level singleton; reset it around every test."""
    health._reset()
    yield
    health._reset()


class _FakeDB:
    """Answers only the queries do_recall / _apply_recall_scoring issue."""

    async def execute_fetchall(self, sql, params=()):
        s = " ".join(sql.split())
        if s.startswith("SELECT MIN(timestamp), MAX(timestamp)"):
            return [("2026-06-26T10:00:00+00:00", "2026-06-26T12:00:00+00:00")]
        if s.startswith("SELECT id, recall_count, last_recalled_at"):
            return [(1, 2, "2026-06-26T11:00:00+00:00"), (2, 0, "")]
        if s.startswith("SELECT COUNT(*)"):
            return [(3,)]
        if s.startswith("SELECT created_at FROM episodes"):
            return []
        return []

    def __init__(self):
        self.executed: list[str] = []

    async def execute(self, sql, params=()):
        self.executed.append(" ".join(sql.split()))
        return None

    async def commit(self):
        return None


async def _fake_rsf(db, agent_id, query, limit, deep, channel="", exclude_set=None,
                    project_id=None, source_id=""):
    return [
        {"id": 1, "content": "recall precision calibration gate", "source": {"System": "t"},
         "timestamp": "2026-06-26T12:00:00+00:00", "_cosine": 0.82, "_rsf_score": 0.82,
         "_rid": ("mem", 1)},
        {"id": 2, "content": "python asyncio tips", "source": {"System": "t"},
         "timestamp": "2026-06-26T10:30:00+00:00", "_cosine": 0.55, "_rsf_score": 0.55,
         "_rid": ("mem", 2)},
    ]


def _patch(monkeypatch):
    import contextlib

    fake = _FakeDB()

    # 2.5.0 C-seam: do_recall reads through connection() and bumps recall counts
    # through transaction(), so the DB fake is injected at the seam CMs (get_db is
    # internal to cpersona.database now).
    @contextlib.asynccontextmanager
    async def fake_cm():
        yield fake

    monkeypatch.setattr(M, "connection", fake_cm)
    monkeypatch.setattr(M, "transaction", fake_cm)
    monkeypatch.setattr(M, "_recall_rsf", _fake_rsf)
    # config.py reads the env once at import; memory_handlers binds CONFIDENCE_ENABLED /
    # RECALL_MODE by value at that point, so the module-level env writes above only take
    # effect when this file is imported before any other test imports config. Pin the two
    # values here so the test is deterministic regardless of collection order (otherwise an
    # alphabetically-earlier file that imports config first leaves CONFIDENCE off + mode rrf,
    # the _recall_rsf patch goes unused, and do_recall returns no messages).
    monkeypatch.setattr(M, "CONFIDENCE_ENABLED", True)
    monkeypatch.setattr(M, "RECALL_MODE", "rsf")
    return fake


@pytest.mark.asyncio
async def test_do_recall_confidence_enabled_returns_messages(monkeypatch):
    """The regression: CONFIDENCE_ENABLED recall must reach the response loop + the
    recall-count update without a NameError on the moved scoring locals."""
    _patch(monkeypatch)
    out = await M.do_recall("agent.t", "recall precision calibration", limit=5)
    assert "messages" in out
    assert len(out["messages"]) == 2
    for m in out["messages"]:
        assert "confidence" in m and "score" in m["confidence"]


@pytest.mark.asyncio
async def test_do_recall_deep_skips_recall_count_update(monkeypatch):
    """deep=True takes the other recall_counts branch (`if not deep and recall_counts`)."""
    db = _patch(monkeypatch)
    out = await M.do_recall("agent.t", "x", limit=5, deep=True)
    assert "messages" in out and len(out["messages"]) == 2
    # 2.5.0b1 audit: assert the skip itself — without this the test passed even
    # if the recall-count UPDATE ran (the fake db swallowed it silently).
    bumps = [q for q in db.executed if q.startswith("UPDATE memories SET recall_count")]
    assert bumps == [], f"deep=True still bumped recall_count: {bumps}"


# --- degraded-advisory: health state machine (drive health.* directly, no DB) ---


def test_health_single_blip_is_debounced():
    health.observe_failure("conn refused")
    assert health.maybe_advisory() is None
    assert not health.is_faulted()


def test_health_fault_promotes_on_second_failure():
    health.observe_failure("conn refused")
    health.observe_failure("conn refused")
    adv = health.maybe_advisory()
    assert adv is not None
    assert adv["severity"] == "fault"
    assert adv["degraded"] is True
    assert "conn refused" in adv["evidence"]


def test_health_full_then_short_within_outage():
    health.observe_failure("e")
    health.observe_failure("e")
    first = health.maybe_advisory()
    second = health.maybe_advisory()
    assert len(first["runbook"]) > len(second["runbook"])
    assert "Notify the user" in first["runbook"]
    assert "Notify the user" not in second["runbook"]


def test_health_recovery_rearms_full():
    health.observe_failure("e")
    health.observe_failure("e")
    assert health.maybe_advisory() is not None  # full emitted
    health.observe_ok()
    assert health.maybe_advisory() is None  # healthy is silent
    health.observe_failure("e2")
    health.observe_failure("e2")
    adv = health.maybe_advisory()
    assert "Notify the user" in adv["runbook"]  # re-armed full
    assert "e2" in adv["evidence"]


def test_health_opt_out(monkeypatch):
    monkeypatch.setattr(config, "DEGRADED_ADVISORY_ENABLED", False)
    health.observe_failure("e")
    health.observe_failure("e")
    assert health.maybe_advisory() is None


# --- degraded-advisory: do_recall / do_recall_with_context integration ---


@pytest.mark.asyncio
async def test_do_recall_hint_advisory_when_mode_none(monkeypatch):
    """mode=none (the file's default env) -> observe_config sets hint -> advisory attached."""
    _patch(monkeypatch)
    out = await M.do_recall("agent.t", "x", limit=5)
    assert out["advisory"]["severity"] == "hint"
    assert out["advisory"]["degraded"] is True


@pytest.mark.asyncio
async def test_do_recall_no_advisory_when_healthy(monkeypatch):
    _patch(monkeypatch)
    monkeypatch.setattr(config, "EMBEDDING_MODE", "http")  # observe_config -> no-op
    health.observe_ok()
    out = await M.do_recall("agent.t", "x", limit=5)
    assert "advisory" not in out


@pytest.mark.asyncio
async def test_do_recall_fault_advisory(monkeypatch):
    _patch(monkeypatch)
    monkeypatch.setattr(config, "EMBEDDING_MODE", "http")  # keep observe_config a no-op
    health.observe_failure("connection refused")
    health.observe_failure("connection refused")
    out = await M.do_recall("agent.t", "x", limit=5)
    assert out["advisory"]["severity"] == "fault"
    assert "connection refused" in out["advisory"]["evidence"]


@pytest.mark.asyncio
async def test_recall_with_context_forwards_advisory(monkeypatch):
    """do_recall_with_context must forward the advisory do_recall produced (refinement 2)."""
    _patch(monkeypatch)
    monkeypatch.setattr(config, "EMBEDDING_MODE", "http")
    health.observe_failure("e")
    health.observe_failure("e")
    out = await M.do_recall_with_context("agent.t", "x", external_context=[], limit=5)
    assert "advisory" in out and out["advisory"]["severity"] == "fault"


# --- degraded-advisory: probe unit ---


class _FakeHTTPClient:
    def __init__(self, exc=None):
        self._exc = exc

    async def post(self, url, json=None, timeout=None):
        if self._exc is not None:
            raise self._exc

        class _Resp:
            def raise_for_status(self):
                return None

        return _Resp()


# --- the evidence comes from the call that failed, not from a second one ---------------
#
# These replace two tests that called a `_probe_embedding_health()` helper directly. The
# helper sent a second POST after a failed embed, purely to recover an error string. It is
# gone: `embed_with_outcome()` returns the evidence alongside the value, so the tests now
# drive the real recall path and read what it recorded.


class _CountingTransport:
    """Stands in for httpx.AsyncClient. Counts POSTs so a second one cannot hide."""

    def __init__(self, exc=None, payload=None):
        self._exc = exc
        self._payload = payload if payload is not None else {"embeddings": [[1.0, 0.0]]}
        self.post_count = 0

    async def post(self, url, json=None, **kwargs):
        self.post_count += 1
        if self._exc:
            raise self._exc

        payload = self._payload

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return payload

        return _Resp()


def _failing_client(exc=None):
    """A real EmbeddingClient over a fake transport, so the outcome logic is the real one."""
    client = EmbeddingClient(mode="http", http_url="http://127.0.0.1:9/embed")
    client._client = _CountingTransport(exc or httpx.ConnectError("connection refused"))
    return client


async def _recall_once(monkeypatch, client):
    monkeypatch.setattr(vector, "_embedding_client", client)
    return await vector._search_vector(_FakeDB(), "agent.t", "any query", limit=5)


@pytest.mark.asyncio
async def test_a_a_failed_embed_carries_its_own_evidence_into_health(monkeypatch):
    """Test A: the real failure reaches the health layer, with the real error in it."""
    client = _failing_client()

    assert await _recall_once(monkeypatch, client) == []
    assert not health.is_faulted()  # debounced
    assert await _recall_once(monkeypatch, client) == []

    assert health.is_faulted()
    advisory = health.maybe_advisory()
    assert advisory["severity"] == "fault"
    assert "ConnectError" in advisory["evidence"]
    assert "connection refused" in advisory["evidence"]
    assert "127.0.0.1:9/embed" in advisory["evidence"]


@pytest.mark.asyncio
async def test_b_a_failing_recall_sends_exactly_one_request(monkeypatch):
    """Test B: the point of the change. One recall, one POST — the probe is gone.

    Counted rather than timed: "no second request" is the claim, and a faster recall
    would be consistent with the probe still being sent to a closer endpoint.
    """
    client = _failing_client()

    await _recall_once(monkeypatch, client)

    assert client._client.post_count == 1, (
        f"a failing recall sent {client._client.post_count} requests; the second one is "
        "the probe this change removed"
    )


@pytest.mark.asyncio
async def test_b_the_probe_helper_is_gone(monkeypatch):
    """Test B, the other half: the counter above cannot notice a probe that is
    reintroduced somewhere the fake transport does not see, so name the helper too."""
    assert not hasattr(vector, "_probe_embedding_health")
    assert not hasattr(vector, "PROBE_TIMEOUT_SECS")


@pytest.mark.asyncio
async def test_c_the_advisory_payload_keeps_its_shape(monkeypatch):
    """Test C: the contract callers read is unchanged by the swap of signal source."""
    client = _failing_client()
    await _recall_once(monkeypatch, client)
    await _recall_once(monkeypatch, client)

    advisory = health.maybe_advisory()

    assert set(advisory) == {
        "degraded",
        "severity",
        "reason",
        "evidence",
        "runbook",
        "advisory_scope",
    }
    assert advisory["degraded"] is True
    assert "Notify the user" in advisory["runbook"]


@pytest.mark.asyncio
async def test_d_a_single_failure_is_still_debounced(monkeypatch):
    """Test D: one blip must not raise an advisory. The threshold did not move."""
    await _recall_once(monkeypatch, _failing_client())

    assert health.maybe_advisory() is None
    assert not health.is_faulted()


@pytest.mark.asyncio
async def test_a_success_still_clears_the_degraded_state(monkeypatch):
    """Recovery is read from the same call, so a working recall must re-arm health."""
    failing = _failing_client()
    await _recall_once(monkeypatch, failing)
    await _recall_once(monkeypatch, failing)
    assert health.is_faulted()

    working = EmbeddingClient(mode="http", http_url="http://127.0.0.1:9/embed")
    working._client = _CountingTransport()
    await _recall_once(monkeypatch, working)

    assert not health.is_faulted()
    assert health.maybe_advisory() is None


@pytest.mark.asyncio
async def test_e_the_plain_embed_entry_point_is_unchanged(monkeypatch):
    """Test E: every other consumer calls `embed()`, which must not have moved.

    store, the maintenance re-embed and the admin paths all read this one method. The
    values below are the pre-change ones — an `embeddings: []` response reaches a caller
    as `[]`, not as `None`, and both are falsy, so a substitution would pass unnoticed.
    """
    ok = EmbeddingClient(mode="http", http_url="http://x/embed")
    ok._client = _CountingTransport()
    assert await ok.embed(["a"]) == [[1.0, 0.0]]

    empty = EmbeddingClient(mode="http", http_url="http://x/embed")
    empty._client = _CountingTransport(payload={"embeddings": []})
    assert await empty.embed(["a"]) == []

    missing = EmbeddingClient(mode="http", http_url="http://x/embed")
    missing._client = _CountingTransport(payload={"dimensions": 2})
    assert await missing.embed(["a"]) is None

    dead = _failing_client()
    assert await dead.embed(["a"]) is None

    assert await EmbeddingClient(mode="none").embed(["a"]) is None


# --- 2.5.0: the recall limit cap is layered — library clamps to the scan
# window (MAX_MEMORIES) only; the agent-facing 100 cap lives in the MCP boundary's
# JSON Schema. These pin both layers so neither regresses silently.


def _patch_capture_limit(monkeypatch):
    """_patch + record the limit do_recall hands to the retrieval driver."""
    _patch(monkeypatch)
    seen: dict = {}

    async def _capture_rsf(db, agent_id, query, limit, deep, channel="", exclude_set=None,
                           project_id=None, source_id=""):
        seen["limit"] = limit
        return await _fake_rsf(db, agent_id, query, limit, deep, channel, exclude_set,
                               project_id, source_id)

    monkeypatch.setattr(M, "_recall_rsf", _capture_rsf)
    return seen


@pytest.mark.asyncio
async def test_do_recall_limit_above_100_is_not_clamped(monkeypatch):
    """The pre-2.5.0 in-library 100 cap is gone: a library caller asking for depth
    250 gets depth 250 (rrf/rsf fusion-list depth tracks limit, so the old cap
    collapsed deep-ranking quality — bge-m3 LongMemEval 81.17 -> 48.98)."""
    seen = _patch_capture_limit(monkeypatch)
    await M.do_recall("agent.t", "x", limit=250)
    assert seen["limit"] == 250


@pytest.mark.asyncio
async def test_do_recall_limit_clamps_to_scan_window(monkeypatch):
    """The library ceiling is the vector scan window (MAX_MEMORIES), not unbounded."""
    seen = _patch_capture_limit(monkeypatch)
    monkeypatch.setattr(M, "MAX_MEMORIES", 500)
    await M.do_recall("agent.t", "x", limit=99999)
    assert seen["limit"] == 500


@pytest.mark.asyncio
async def test_do_recall_negative_limit_still_clamps_to_zero(monkeypatch):
    """bug-032 stays closed: a negative limit floors at 0 instead of reaching
    SQLite as `LIMIT -1` (unbounded full-corpus scan)."""
    seen = _patch_capture_limit(monkeypatch)
    await M.do_recall("agent.t", "x", limit=-5)
    assert seen["limit"] == 0


def test_recall_tool_schemas_cap_limit_at_100():
    """The agent-facing 100 cap moved to the MCP boundary: both recall tools'
    JSON Schema must declare maximum:100 (and a non-negative minimum) on limit."""
    from cpersona import server

    tools = {t.name: t for t in server.registry._tools}
    for name in ("recall", "recall_with_context"):
        limit_schema = tools[name].inputSchema["properties"]["limit"]
        assert limit_schema["maximum"] == 100, f"{name}: agent-facing limit cap missing"
        assert limit_schema["minimum"] == 0, f"{name}: limit minimum missing"
