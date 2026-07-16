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


class _FakeEmbeddingClient:
    def __init__(self, exc=None):
        self.mode = "http"
        self._http_url = "http://127.0.0.1:9/embed"
        self._client = _FakeHTTPClient(exc)


@pytest.mark.asyncio
async def test_probe_reports_failure(monkeypatch):
    monkeypatch.setattr(
        vector, "_embedding_client", _FakeEmbeddingClient(httpx.ConnectError("connection refused"))
    )
    ok, evidence = await vector._probe_embedding_health()
    assert ok is False
    assert "POST http://127.0.0.1:9/embed failed" in evidence
    assert "connection refused" in evidence


@pytest.mark.asyncio
async def test_probe_reports_ok(monkeypatch):
    monkeypatch.setattr(vector, "_embedding_client", _FakeEmbeddingClient(None))
    ok, evidence = await vector._probe_embedding_health()
    assert ok is True and evidence is None


# --- 2.5.0 Task #190: the recall limit cap is layered — library clamps to the scan
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
