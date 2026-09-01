"""The maintenance surface can tell an unreachable embedding backend from a green run.

``docs/operations.md`` warned its readers in prose that a green ``check_health`` is not
evidence the embedding server is up: an unreachable endpoint made the dimension check skip
rather than fail, and nothing else here watched liveness. These tests pin the finding that
replaces the warning, and the two states that must NOT be reported as defects.
"""

import pytest
import pytest_asyncio

from cpersona import checks, findings, health, vector
from cpersona.database import get_db


@pytest_asyncio.fixture
async def db():
    conn = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()
    return conn


@pytest.fixture(autouse=True)
def reset_health_state():
    health._reset()
    yield
    health._reset()


class _Backend:
    """Answers or does not, the way the real client answers or does not."""

    EVIDENCE = "mode=http / POST http://127.0.0.1:8401/embed failed: ConnectError: connection refused"

    def __init__(self, *, up=True, cached=False):
        self.up = up
        # bug-248: the real client answers a repeated single-text embed from its TTL LRU
        # cache with attempted=False — the same vector, without a request leaving the
        # process. Nothing about `up` is observed on that path, which is the point.
        self.cached = cached
        self.calls = 0

    async def embed(self, texts):
        result, _ = await self.embed_with_outcome(texts)
        return result

    async def embed_with_outcome(self, texts):
        from cpersona._vendored_mcp_common.embedding_client import EmbedOutcome

        self.calls += 1
        if self.cached:
            return [[0.1, 0.2, 0.3] for _ in texts], EmbedOutcome(attempted=False, ok=True)
        if not self.up:
            return None, EmbedOutcome(attempted=True, ok=False, error=self.EVIDENCE)
        return [[0.1, 0.2, 0.3] for _ in texts], EmbedOutcome(attempted=True, ok=True)


def _only(issues):
    assert len(issues) == 1, issues
    return issues[0]


@pytest.mark.asyncio
async def test_a_configured_backend_that_did_not_answer_is_reported_with_its_evidence(
    db, monkeypatch
):
    """The state operations.md said had no red check."""
    monkeypatch.setattr(vector, "_embedding_client", _Backend(up=False))
    health.observe_failure(_Backend.EVIDENCE)

    issues = await checks.check_embedding_backend(
        db, "", fix=True, embedding_cache={"expected_dim": None}
    )

    issue = _only(issues)
    assert issue["type"] == "embedding_backend_unreachable"
    assert issue["severity"] == "warn"
    assert issue["evidence"] == _Backend.EVIDENCE


@pytest.mark.asyncio
async def test_an_answering_backend_produces_no_finding(db, monkeypatch):
    monkeypatch.setattr(vector, "_embedding_client", _Backend(up=True))

    issues = await checks.check_embedding_backend(
        db, "", fix=True, embedding_cache={"expected_dim": 3, "dim_probe_reached_backend": True}
    )

    assert issues == []


@pytest.mark.asyncio
async def test_a_run_that_did_not_probe_says_so_rather_than_implying_health(db, monkeypatch):
    """fix=False makes no network call, so the honest answer is 'not tested'.

    Reporting nothing here would rebuild the silence this check exists to remove: the
    caller could not tell a backend that answered from one nobody asked.
    """
    monkeypatch.setattr(vector, "_embedding_client", _Backend(up=True))

    issues = await checks.check_embedding_backend(db, "", fix=False)

    issue = _only(issues)
    assert issue["type"] == "embedding_backend_not_probed"
    assert issue.get("severity", "info") == "info"


@pytest.mark.asyncio
async def test_an_unprobed_run_still_reports_a_fault_a_recall_already_latched(db, monkeypatch):
    """Nothing was probed, but a recall's own embed already answered the question."""
    monkeypatch.setattr(vector, "_embedding_client", _Backend(up=False))
    for _ in range(health.FAULT_PROMOTE_THRESHOLD):
        health.observe_failure(_Backend.EVIDENCE)

    issues = await checks.check_embedding_backend(db, "", fix=False)

    assert _only(issues)["type"] == "embedding_backend_unreachable"


@pytest.mark.asyncio
async def test_no_backend_configured_is_not_a_finding(db, monkeypatch):
    """A decision, not an omission — see the runner's docstring.

    It is the one state that cannot be mistaken for "the server is up", it is permanent,
    and a finding that can never be resolved is what teaches an operator to skim.
    """
    monkeypatch.setattr(vector, "_embedding_client", None)

    assert await checks.check_embedding_backend(db, "", fix=True, embedding_cache={}) == []
    assert await checks.check_embedding_backend(db, "", fix=False) == []


@pytest.mark.asyncio
async def test_the_dimension_probe_reports_a_dead_backend(monkeypatch):
    """The probe is the only network call the unlocked phase makes when no row needs
    re-embedding, so a fully-embedded database and a dead backend used to be silent
    together."""
    client = _Backend(up=False)
    monkeypatch.setattr(vector, "_embedding_client", client)

    assert await checks.probe_embedding_dim() == (None, True)
    assert client.calls == 1
    assert health.observed_state()["evidence"] == _Backend.EVIDENCE


@pytest.mark.asyncio
async def test_the_dimension_probe_does_not_re_arm_health_on_success(monkeypatch):
    """Same asymmetry as the prefetch: failures are reported, recovery is not."""
    monkeypatch.setattr(vector, "_embedding_client", _Backend(up=True))
    health.observe_failure("a recall's embed failed")

    assert await checks.probe_embedding_dim() == (3, True)

    health.observe_failure("the next recall's embed failed too")
    assert health.is_faulted(), "a maintenance probe must not reset the failure counter"


def test_reading_the_state_does_not_consume_the_advisory():
    """``observed_state`` exists because ``maybe_advisory`` decides something.

    Reading through the advisory would spend a user's one full runbook on a maintenance
    surface they never saw; the next real recall would get the short reminder for a
    message it never received — the defect the session-scoped suppression was built for.
    """
    for _ in range(health.FAULT_PROMOTE_THRESHOLD):
        health.observe_failure("backend down")

    health.observed_state()
    health.observed_state()

    assert health.maybe_advisory()["runbook"] == health.FAULT_RUNBOOK_FULL.format(
        evidence="backend down"
    )


def test_unreachable_is_mapped_to_warn_in_the_static_severity_map():
    """A stamped severity that the map does not know is reported as info (the fallback),
    which would make a dead backend look like an observation."""
    assert findings.severity_for_kind("embedding_backend_unreachable") == "warn"
    assert (
        findings.finding_kind(
            {"check": "embedding_backend", "type": "embedding_backend_unreachable", "severity": "warn"}
        )
        == "embedding_backend_unreachable"
    )
    assert (
        findings.finding_kind(
            {"check": "embedding_backend", "type": "embedding_backend_not_probed", "severity": "info"}
        )
        == "embedding_backend"
    )


# --- bug-248: a value from the embedding cache is not an observation of the backend -----


@pytest.mark.asyncio
async def test_a_dimension_answered_from_cache_is_not_reported_as_connected(db, monkeypatch):
    """The check built to remove this silence used to be answered by it.

    The probe embeds the constant ``"test"``, which makes it the most cache-warm key in
    the process. A cached vector carries the right dimension without a request leaving
    the process, so ``expected_dim`` came back on a run that never reached the endpoint —
    and a dead backend was reported as connected.
    """
    monkeypatch.setattr(vector, "_embedding_client", _Backend(up=False))

    issues = await checks.check_embedding_backend(
        db,
        "",
        fix=True,
        embedding_cache={"expected_dim": 3, "dim_probe_reached_backend": False},
    )

    issue = _only(issues)
    assert issue["type"] == "embedding_backend_not_probed"
    assert issue["reason"] == "served_from_cache"
    assert "cache" in issue["hint"]


@pytest.mark.asyncio
async def test_the_two_ways_liveness_goes_untested_are_told_apart(db, monkeypatch):
    """Both are ``not_probed``; an operator still has to know which one happened.

    ``fix=False`` is a choice the caller made and can undo by passing ``fix=true``. A
    cache hit is not — the same call has to be made again after the entry expires.
    """
    monkeypatch.setattr(vector, "_embedding_client", _Backend(up=True))

    unprobed = _only(await checks.check_embedding_backend(db, "", fix=False))

    assert unprobed["reason"] == "no_probe_on_this_run"


@pytest.mark.asyncio
async def test_the_probe_reports_a_cache_hit_as_not_having_reached_the_backend(monkeypatch):
    """The dimension is still returned — a cached vector has the right length, which is
    all check_embedding_dimension needs. Only the second value carries the liveness."""
    monkeypatch.setattr(vector, "_embedding_client", _Backend(cached=True))

    assert await checks.probe_embedding_dim() == (3, False)


@pytest.mark.asyncio
async def test_a_cache_hit_does_not_clear_a_fault_a_recall_latched(db, monkeypatch):
    """``probe_embedding_dim`` never re-arms, but the value it hands on must not either.

    A latched fault outranks a cached dimension: the run reports ``unreachable``, not the
    ``not_probed`` the cache hit alone would have produced.
    """
    monkeypatch.setattr(vector, "_embedding_client", _Backend(cached=True))
    for _ in range(health.FAULT_PROMOTE_THRESHOLD):
        health.observe_failure(_Backend.EVIDENCE)

    dim, reached = await checks.probe_embedding_dim()
    assert (dim, reached) == (3, False)
    assert health.is_faulted(), "a cache hit must not clear a latched fault"

    issue = _only(
        await checks.check_embedding_backend(
            db, "", fix=True, embedding_cache={"expected_dim": dim, "dim_probe_reached_backend": reached}
        )
    )
    assert issue["type"] == "embedding_backend_unreachable"
