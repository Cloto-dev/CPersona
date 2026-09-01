"""The re-embed prefetch reports a failing backend to the health breaker.

bug-129 wired ``prefetch_null_embeddings`` to both consult and feed the breaker, and its
regressions covered the consulting half (skip an already-faulted backend) and the batching.
Nothing drove the feeding half, and it could not have fired: the call it watched reports an
unreachable endpoint by returning no embeddings rather than by raising, so the ``except``
that held the report was dead on the one failure that matters most. A re-embed pass with the
backend down did no work and reported success.

These tests drive that path with a double that fails the way the real client fails.
"""

import pytest
import pytest_asyncio

from cpersona import checks, health, vector
from cpersona.database import get_db


@pytest_asyncio.fixture
async def clean_db():
    db = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()
    return db


@pytest.fixture(autouse=True)
def reset_health_state():
    health._reset()
    yield
    health._reset()


class _UnreachableBackend:
    """Fails the way the real client fails: no embeddings, reason in the outcome."""

    EVIDENCE = "mode=http / POST http://127.0.0.1:8401/embed failed: ConnectError: connection refused"

    def __init__(self):
        self.calls = 0

    async def embed(self, texts):
        result, _ = await self.embed_with_outcome(texts)
        return result

    async def embed_with_outcome(self, texts):
        from cpersona._vendored_mcp_common.embedding_client import EmbedOutcome

        self.calls += 1
        return None, EmbedOutcome(attempted=True, ok=False, error=self.EVIDENCE)


class _WorkingBackend:
    def __init__(self):
        self.calls = 0

    async def embed(self, texts):
        result, _ = await self.embed_with_outcome(texts)
        return result

    async def embed_with_outcome(self, texts):
        from cpersona._vendored_mcp_common.embedding_client import EmbedOutcome

        self.calls += 1
        result = [[1.0] for _ in texts]
        return result, EmbedOutcome(attempted=True, ok=True)

    @staticmethod
    def pack_embedding(embedding):
        return bytes([int(embedding[0])])


class _OutOfContractBackend:
    """Raises instead of reporting — the case the ``except`` branch is actually for."""

    async def embed(self, texts):
        raise RuntimeError("driver exploded")

    async def embed_with_outcome(self, texts):
        raise RuntimeError("driver exploded")


async def _insert(db, agent_id, count):
    for i in range(count):
        await db.execute(
            "INSERT INTO memories (agent_id, content, timestamp) VALUES (?, ?, '')",
            (agent_id, f"row {i}"),
        )
    await db.commit()


@pytest.mark.asyncio
async def test_unreachable_backend_reaches_health_with_the_call_s_own_evidence(
    clean_db, monkeypatch
):
    """The failure is reported, and the evidence is the client's, not a constant.

    Two batches: the first failure is debounced, the second latches the fault, so the
    advisory a user would receive can be read back and its evidence checked.
    """
    health._reset()
    client = _UnreachableBackend()
    monkeypatch.setattr(vector, "_embedding_client", client)
    await _insert(clean_db, "down", checks.EMBED_BATCH_SIZE + 1)

    cache = await checks.prefetch_null_embeddings(clean_db, "down")

    assert client.calls == 2, "both chunks were attempted before the breaker latched"
    assert cache == {"memories": {}, "episodes": {}}
    assert health.is_faulted()

    advisory = health.maybe_advisory()
    assert advisory is not None
    assert advisory["severity"] == "fault"
    assert advisory["evidence"] == _UnreachableBackend.EVIDENCE, (
        "the advisory must carry what the failing call reported, not a fixed string"
    )


@pytest.mark.asyncio
async def test_unreachable_backend_stops_after_the_breaker_latches(clean_db, monkeypatch):
    """The circuit break bug-129 asked for: no further batches once the fault latches."""
    health._reset()
    client = _UnreachableBackend()
    monkeypatch.setattr(vector, "_embedding_client", client)
    await _insert(clean_db, "down", checks.EMBED_BATCH_SIZE * 4)

    await checks.prefetch_null_embeddings(clean_db, "down")

    assert client.calls == health.FAULT_PROMOTE_THRESHOLD, (
        "the loop must stop at the threshold, not walk every remaining chunk"
    )


@pytest.mark.asyncio
async def test_a_successful_prefetch_does_not_clear_a_pending_failure(clean_db, monkeypatch):
    """Recovery stays on the recall path — this one only reports failures.

    Written as behaviour rather than as a call assertion: seed one failure, run a prefetch
    that succeeds, then fail once more. If the prefetch had re-armed health, the counter
    would have reset and this second failure would not reach the threshold.
    """
    health._reset()
    client = _WorkingBackend()
    monkeypatch.setattr(vector, "_embedding_client", client)
    await _insert(clean_db, "mixed", 2)

    health.observe_failure("a recall's embed failed")
    assert not health.is_faulted()

    cache = await checks.prefetch_null_embeddings(clean_db, "mixed")
    assert len(cache["memories"]) == 2, "the successful path still fills the cache"

    health.observe_failure("the next recall's embed failed too")
    assert health.is_faulted(), (
        "a maintenance run must not erase a fault a user's recall was about to surface"
    )


@pytest.mark.asyncio
async def test_an_out_of_contract_exception_is_named_in_the_evidence(clean_db, monkeypatch):
    """The constant that used to sit here said nothing about what went wrong."""
    health._reset()
    monkeypatch.setattr(vector, "_embedding_client", _OutOfContractBackend())
    await _insert(clean_db, "broken", checks.EMBED_BATCH_SIZE + 1)

    await checks.prefetch_null_embeddings(clean_db, "broken")

    advisory = health.maybe_advisory()
    assert advisory is not None
    assert "RuntimeError" in advisory["evidence"]
