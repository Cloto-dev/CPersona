"""The embedding model a recall trace names is the one that produced the vectors.

Over HTTP the request carries texts and the backend chooses the model, so the configured
default never reaches it. A trace that wrote that default named a model nothing had run --
on one evaluation every trace said `text-embedding-3-small` while `bge-m3` answered. The
trace now names what the backend reports, else what this process can vouch for, else
nothing.
"""
import pytest

from cpersona import config, generation, memory_handlers
from cpersona._vendored_mcp_common.embedding_client import BackendIdentity, EmbedOutcome

AGENT = "agent.trace-model"


class Backend:
    def __init__(self, identity):
        self._identity = identity

    async def capabilities_with_outcome(self):
        return self._identity, EmbedOutcome(attempted=True, ok=self._identity is not None, error=None)


@pytest.fixture(autouse=True)
def forget():
    generation.reset()
    yield
    generation.reset()


@pytest.mark.asyncio
@pytest.mark.parametrize("fingerprint, incomplete", [("1:abc", ()), (None, ("digests.graph",))])
async def test_the_trace_names_the_model_the_backend_reported(monkeypatch, fingerprint, incomplete):
    # A report that could not complete its identity still names its model.
    monkeypatch.setattr(config, "EMBEDDING_MODE", "http")
    monkeypatch.setattr(config, "EMBEDDING_MODEL", "text-embedding-3-small")
    await generation.refresh(Backend(BackendIdentity(fingerprint=fingerprint, fields={"model": "bge-m3"},
                                                     incomplete=incomplete)))
    assert generation.reported_model() == "bge-m3"
    assert generation.trace_model() == "bge-m3"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode, configured, expected", [
    ("http", False, ""),                       # the default never reached the backend: not known here
    ("http", True, "text-embedding-3-small"),  # the operator named it
    ("api", False, "text-embedding-3-small"),  # the api transport sends the model it names
])
async def test_without_a_report_the_trace_names_only_what_this_process_can_vouch_for(
    monkeypatch, mode, configured, expected
):
    monkeypatch.setattr(config, "EMBEDDING_MODE", mode)
    monkeypatch.setattr(config, "EMBEDDING_MODEL", "text-embedding-3-small")
    monkeypatch.setattr(config, "EMBEDDING_MODEL_CONFIGURED", configured)
    await generation.refresh(Backend(None))
    assert generation.reported_model() is None
    assert generation.trace_model() == expected


@pytest.mark.asyncio
async def test_a_traced_recall_writes_it_and_an_untraced_one_does_not_ask(fake_embedding_client, monkeypatch):
    await memory_handlers.do_store(AGENT, {"content": "harbor lighthouse keeper logbook", "source": {"System": "test"}})
    asked = []

    async def refresh(client=None):
        asked.append(1)

    monkeypatch.setattr(generation, "refresh", refresh)
    monkeypatch.setattr(generation, "reported_model", lambda: "bge-m3")
    await memory_handlers.do_recall(AGENT, "harbor lighthouse", limit=3)
    assert asked == []
    out = await memory_handlers.do_recall(AGENT, "harbor lighthouse", limit=3, trace=True)
    assert asked == [1]
    assert out["trace"]["config"]["embedding_model"] == "bge-m3"


@pytest.mark.asyncio
async def test_the_backend_report_outranks_the_configured_name(monkeypatch):
    monkeypatch.setattr(config, "EMBEDDING_MODE", "http")
    monkeypatch.setattr(config, "EMBEDDING_MODEL", "text-embedding-3-small")
    monkeypatch.setattr(config, "EMBEDDING_MODEL_CONFIGURED", True)
    await generation.refresh(Backend(BackendIdentity(fingerprint="1:abc", fields={"model": "bge-m3"}, incomplete=())))
    assert generation.trace_model() == "bge-m3"


@pytest.mark.asyncio
async def test_a_reported_name_is_forgotten_with_what_was_learned():
    await generation.refresh(Backend(BackendIdentity(fingerprint="1:abc", fields={"model": "bge-m3"}, incomplete=())))
    generation.reset()
    assert generation.reported_model() is None
    await generation.refresh(Backend(BackendIdentity(fingerprint="1:abc", fields={"model": "bge-m3"}, incomplete=())))
    generation._learned = None  # the refresh interval has passed
    await generation.refresh(object())  # a client that cannot be asked
    assert generation.reported_model() is None
