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

    def __init__(self, *, up=True):
        self.up = up
        self.calls = 0

    async def embed(self, texts):
        result, _ = await self.embed_with_outcome(texts)
        return result

    async def embed_with_outcome(self, texts):
        from cpersona._vendored_mcp_common.embedding_client import EmbedOutcome

        self.calls += 1
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
        db, "", fix=True, embedding_cache={"expected_dim": 3}
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

    assert await checks.probe_embedding_dim() is None
    assert client.calls == 1
    assert health.observed_state()["evidence"] == _Backend.EVIDENCE


@pytest.mark.asyncio
async def test_the_dimension_probe_does_not_re_arm_health_on_success(monkeypatch):
    """Same asymmetry as the prefetch: failures are reported, recovery is not."""
    monkeypatch.setattr(vector, "_embedding_client", _Backend(up=True))
    health.observe_failure("a recall's embed failed")

    assert await checks.probe_embedding_dim() == 3

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
