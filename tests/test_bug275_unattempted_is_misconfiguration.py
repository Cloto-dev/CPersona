"""bug-275: a failure that never reached the backend is not an unreachable backend.

`EmbedOutcome` carries `attempted`, and bug-248 gated the *success* side on it —
a cache hit is not an observation of the endpoint. The failure side was ungated:
any falsy embed called `observe_failure`, and the maintenance check read "no
dimension came back" as "the backend did not answer".

An invalid `CPERSONA_EMBEDDING_MODE` produces exactly that shape
(`attempted=False, ok=False, error='mode=<x> is not a supported embedding
mode'`), so the operator was handed a runbook whose four investigation steps are
all network-layer — is the process alive, is the port reachable, curl the URL,
was the model downloaded — for a backend nothing had contacted.

Two halves are pinned here, because either alone leaves the other's failure
mode: the classification follows `attempted` on both surfaces, and the mode is
validated once at startup rather than arriving at every call site as a per-call
failure. The advisory is deliberately still raised — recall really is degraded.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from cpersona import checks, config, health, vector
from cpersona.database import get_db

UNATTEMPTED = "mode=openai is not a supported embedding mode"
REACHED = "mode=http / POST http://127.0.0.1:8401/embed failed: ConnectError: connection refused"


@pytest.fixture(autouse=True)
def reset_health_state():
    health._reset()
    yield
    health._reset()


@pytest_asyncio.fixture
async def db():
    conn = await get_db()
    return conn


def _latch(evidence: str, *, attempted: bool) -> None:
    """Two failures: the promotion threshold is a debounce, not a formality."""
    health.observe_failure(evidence, attempted=attempted)
    health.observe_failure(evidence, attempted=attempted)


def test_an_unattempted_failure_is_classified_as_configuration():
    _latch(UNATTEMPTED, attempted=False)
    state = health.observed_state()

    assert state["state"] == "fault", "recall is degraded either way — do not suppress it"
    assert state["fault_kind"] == "misconfigured"
    assert "never contacted" in state["reason"]


def test_a_failure_that_reached_the_backend_is_still_an_outage():
    """The repair must not relabel every failure; only the ones that never left."""
    _latch(REACHED, attempted=True)
    state = health.observed_state()

    assert state["fault_kind"] == "unreachable"
    assert "unreachable" in state["reason"]


def test_the_advisory_names_the_mode_and_not_the_network():
    _latch(UNATTEMPTED, attempted=False)
    runbook = health.maybe_advisory()["runbook"]

    assert "CPERSONA_EMBEDDING_MODE" in runbook, "the repair must name what is actually wrong"
    for network_step in ("curl the embedding URL", "is its port reachable", "EMBEDDING_HTTP_URL"):
        assert network_step not in runbook, (
            f"the misconfiguration runbook still sends the operator to {network_step!r}, "
            "which is an investigation of a backend nothing contacted"
        )


def test_the_outage_advisory_keeps_its_network_runbook():
    """The other side of the split — otherwise this could pass by emptying both."""
    _latch(REACHED, attempted=True)
    runbook = health.maybe_advisory()["runbook"]

    assert "curl the embedding URL" in runbook
    assert "CPERSONA_EMBEDDING_MODE" not in runbook


class _NeverContacted:
    """The client the invalid mode produces: refuses without issuing a request."""

    async def embed_with_outcome(self, texts):
        from cpersona._vendored_mcp_common.embedding_client import EmbedOutcome

        return None, EmbedOutcome(attempted=False, ok=False, error=UNATTEMPTED)

    async def embed(self, texts):
        result, _ = await self.embed_with_outcome(texts)
        return result


@pytest.mark.asyncio
async def test_the_maintenance_finding_follows_the_same_axis(db, monkeypatch):
    monkeypatch.setattr(vector, "_embedding_client", _NeverContacted())
    _latch(UNATTEMPTED, attempted=False)

    issues = await checks.check_embedding_backend(
        db, "", fix=True, embedding_cache={"expected_dim": None}
    )

    assert len(issues) == 1, issues
    issue = issues[0]
    assert issue["type"] == "embedding_backend_misconfigured"
    assert issue["status"] == "misconfigured"
    assert "CPERSONA_EMBEDDING_MODE" in issue["hint"]
    assert "restart" not in issue["hint"].split("Correct it and ")[0], (
        "the hint still opens by telling the operator to restart a backend that "
        "was never contacted"
    )


@pytest.mark.asyncio
async def test_the_maintenance_finding_still_reports_a_real_outage(db, monkeypatch):
    monkeypatch.setattr(vector, "_embedding_client", _NeverContacted())
    _latch(REACHED, attempted=True)

    issues = await checks.check_embedding_backend(
        db, "", fix=True, embedding_cache={"expected_dim": None}
    )

    assert issues[0]["type"] == "embedding_backend_unreachable"


@pytest.mark.parametrize("mode", ["none", "http", "api"])
def test_supported_modes_start(mode):
    config.assert_embedding_mode_supported(mode)


@pytest.mark.parametrize("mode", ["openai", "HTTP", "", "htttp"])
def test_an_unsupported_mode_fails_at_startup(mode):
    """The complementary half: one loud failure at boot beats a per-call one.

    `''` and a case variant are in here because both were accepted before and
    both reach the client as a mode it does not recognise.
    """
    with pytest.raises(ValueError) as exc:
        config.assert_embedding_mode_supported(mode)
    assert "CPERSONA_EMBEDDING_MODE" in str(exc.value)
    assert "none, http, api" in str(exc.value), "the message must say what is accepted"
