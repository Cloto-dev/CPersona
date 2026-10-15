"""Server responses and startup: shapes a refusal still owes, and startup order.

- bug-711: store under lock:true promises `locked` on every response. The three
  refusals at the boundary (mixed forms, no agent_id, the operating-context gate)
  returned before the lock rider and so carried no `locked` at all.
- bug-713: declare_associations documents `result` on every response; its
  operating-context refusal carried none, unlike store's.
- bug-714: a full_content recall row that the 200,000-character budget cut back to
  the preview carried no `excerpt`, though a preview-cut row promises one, because
  the boundary asked the library for none whenever full_content was set.
- bug-725: the startup identity probe (GET /capabilities) was awaited for the
  whole embedding timeout before the database and the transport opened.
- bug-727: an unknown CPERSONA_TRANSPORT was refused only after the database, the
  calibration, the queue and the block index had started.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time

import pytest

from cpersona import (
    admin_handlers,
    config,
    database,
    generation,
    memory_handlers,
    nodes,
    server,
    session,
    tasks,
    vector,
)
from test_generation_key import FINGERPRINT, Backend, _identity

# ---------------------------------------------------------------------------
# operating context in reject mode (the same sidecar test_operating_context uses)
# ---------------------------------------------------------------------------

_REJECT_SIDECAR = """
version = 1
context_revision = "2026-10-14.1"

[instructions]
summary = "registry: \\"\\" and \\"known\\"."

[registry]
project_ids = ["", "known"]
enforce = "reject"
"""


@pytest.fixture
def reject_context(tmp_path, monkeypatch):
    path = tmp_path / "operating-context.toml"
    path.write_text(_REJECT_SIDECAR, encoding="utf-8")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    monkeypatch.setenv("CPERSONA_OPERATING_CONTEXT", "on")
    monkeypatch.setenv("CPERSONA_OPERATING_CONTEXT_PATH", str(path))


async def _handler_must_not_run(*args, **kwargs):  # pragma: no cover - reached only on a regression
    raise AssertionError("a refused call reached the handler")


# ---------------------------------------------------------------------------
# bug-711 — every store refusal under lock:true says locked:false
# ---------------------------------------------------------------------------

_REFUSALS = {
    "mixed-forms": dict(agent_id="a", message={"content": "x"}, content="y"),
    "no-agent-id": dict(agent_id=None, content="x"),
    "operating-context-gate": dict(agent_id="a", content="x", project_id="ghost"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("path", list(_REFUSALS))
async def test_a_refused_store_under_lock_says_it_locked_nothing(reject_context, monkeypatch, path):
    monkeypatch.setattr(server, "do_store", _handler_must_not_run)
    response = await server.do_store_boundary(**_REFUSALS[path], lock=True)
    assert response["ok"] is False and response["result"] == "rejected", response
    assert response.get("locked", "absent") is False, (
        f"a lock:true store refused on the {path} path must say locked:false: {response}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("path", list(_REFUSALS))
async def test_a_refused_store_without_lock_keeps_its_shape(reject_context, monkeypatch, path):
    """The field is the lock rider's answer, so a store that asked for no lock has none."""
    monkeypatch.setattr(server, "do_store", _handler_must_not_run)
    response = await server.do_store_boundary(**_REFUSALS[path])
    assert response["result"] == "rejected" and "locked" not in response, response


@pytest.mark.asyncio
async def test_the_gate_refusal_keeps_the_fields_the_other_gated_tools_share(reject_context, monkeypatch):
    monkeypatch.setattr(server, "do_store", _handler_must_not_run)
    response = await server.do_store_boundary("a", content="x", project_id="ghost", lock=True)
    assert "not in registry" in response["error"] and response["reason"] == response["error"]
    assert response["operating_context_revision"] == "2026-10-14.1"


# ---------------------------------------------------------------------------
# bug-713 — declare_associations' gate refusal carries result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_declare_associations_gate_refusal_carries_result(reject_context, monkeypatch):
    monkeypatch.setattr(server.associations_module, "declare", _handler_must_not_run)
    response = await server.do_declare_associations_boundary(
        "a", associations={"entities": [{"name": "X"}]}, project_id="ghost"
    )
    assert response["ok"] is False and "not in registry" in response["error"], response
    assert response.get("result") == "rejected", response
    assert response["entities"] == [] and response["dropped"] == [], response


# ---------------------------------------------------------------------------
# bug-714 — a row the full_content budget cut carries its excerpt
# ---------------------------------------------------------------------------

AGENT = "agent.x10-excerpt"
TAIL = "zzarquon nebulite flimsy was decided against for the pilot."
QUERY = "zzarquon nebulite flimsy"
#: Two long records, each past the preview with the matching sentence at its end.
#: One of them is the top row (admitted whole); the other is cut by the budget.
LONG_A = "Record alpha.\n\n" + "\n\n".join(
    f"Paragraph {i} is about budget headcount procurement and warehouse staffing." for i in range(20)
) + "\n\n" + TAIL
LONG_B = "Record bravo.\n\n" + "\n\n".join(
    f"Section {i} covers ferry timetables, harbour dues and lighthouse upkeep." for i in range(20)
) + "\n\n" + TAIL
FILLER = [f"note {i} on topic {i} with words alpha{i} beta{i}" for i in range(30)]


class _TempDB:
    async def __aenter__(self):
        session.reset_pauses_for_tests()
        self._saved = (database._db, database.DB_PATH, tasks._task_queue)
        database._db = None
        database.DB_PATH = os.path.join(tempfile.mkdtemp(), "x10.db")
        self.queue = tasks.MemoryTaskQueue()
        self.queue._running = True
        tasks._task_queue = self.queue
        await database.get_db()
        return self

    async def __aexit__(self, *exc):
        await database.close_db()
        database._db, database.DB_PATH, tasks._task_queue = self._saved
        session.reset_pauses_for_tests()

    async def drain(self):
        await self.queue._drain(admin_handlers, memory_handlers, nodes)


async def _seed(tmp):
    # One memory and one episode: each is the first row of its own lexical arm, so
    # the gate admits both, and the episode twin of the path is covered as well.
    await memory_handlers.do_archive_episode(AGENT, [], summary=LONG_B, keywords="kw")
    for text in (LONG_A, *FILLER):
        await memory_handlers.do_store(AGENT, {"content": text})
    await tmp.drain()


def _long_rows(result):
    return [m for m in result["messages"] if "Record " in m["content"][:40]]


def _assert_cut_row_carries_its_excerpt(result):
    assert result.get("full_content_budget_chars") == server.RECALL_FULL_CONTENT_MAX_CHARS, (
        "the budget has to bite for this test to say anything"
    )
    rows = _long_rows(result)
    assert len(rows) == 2, [m["content"][:30] for m in result["messages"]]
    whole = [m for m in rows if not m.get("content_truncated")]
    cut = [m for m in rows if m.get("content_truncated")]
    assert len(whole) == 1 and len(cut) == 1, rows
    row = cut[0]
    assert TAIL not in row["content"] and len(row["content"]) == config.RECALL_PREVIEW_CHARS
    assert TAIL in row.get("excerpt", ""), (
        f"a row the full_content budget cut back to the preview must carry the excerpt "
        f"a preview-cut row promises: {sorted(row)}"
    )
    assert row["excerpt_basis"] in ("blocks", "lexical", "start")
    assert len(row["excerpt"]) <= config.RECALL_EXCERPT_CHARS
    # The row shown whole needs none, as before.
    assert TAIL in whole[0]["content"] and "excerpt" not in whole[0] and "excerpt_basis" not in whole[0]


@pytest.mark.usefixtures("blocks_off")
@pytest.mark.asyncio
async def test_recall_full_content_row_cut_by_the_budget_carries_its_excerpt(fake_embedding_client, monkeypatch):
    # The top row is admitted whatever its size; one character of budget then cuts
    # every other row that has a ref.
    monkeypatch.setattr(server, "RECALL_FULL_CONTENT_MAX_CHARS", 1)
    async with _TempDB() as tmp:
        await _seed(tmp)
        result = await server.do_recall_boundary(AGENT, QUERY, 5, False, "", [], None, "", full_content=True)
        _assert_cut_row_carries_its_excerpt(result)


@pytest.mark.usefixtures("blocks_off")
@pytest.mark.asyncio
async def test_recall_with_context_full_content_row_cut_by_the_budget_carries_its_excerpt(
    fake_embedding_client, monkeypatch
):
    monkeypatch.setattr(server, "RECALL_FULL_CONTENT_MAX_CHARS", 1)
    async with _TempDB() as tmp:
        await _seed(tmp)
        result = await server.do_recall_with_context_boundary(
            AGENT, QUERY, [], 5, "", False, None, "", full_content=True
        )
        _assert_cut_row_carries_its_excerpt(result)


@pytest.mark.usefixtures("blocks_off")
@pytest.mark.asyncio
async def test_full_content_within_the_budget_still_carries_no_excerpt(fake_embedding_client):
    async with _TempDB() as tmp:
        await _seed(tmp)
        result = await server.do_recall_boundary(AGENT, QUERY, 5, False, "", [], None, "", full_content=True)
        assert "full_content_budget_chars" not in result
        rows = _long_rows(result)
        assert rows and all(TAIL in m["content"] and "excerpt" not in m for m in rows), rows


# ---------------------------------------------------------------------------
# bug-725 — the startup identity probe does not hold startup for the timeout
# ---------------------------------------------------------------------------


class _ReachedInitDb(Exception):
    """Raised in place of init_db: startup got as far as opening the database."""


class _StartupBackend(Backend):
    """An embedding client main() can build, whose /capabilities answer takes `delay`."""

    def __init__(self, identity, delay: float):
        super().__init__(identity)
        self._delay = delay
        self._http_url = "http://backend.invalid/embed"

    async def initialize(self):
        pass

    async def close(self):
        pass

    async def capabilities_with_outcome(self):
        await asyncio.sleep(self._delay)
        return await super().capabilities_with_outcome()


def _drive_main_to_init_db(monkeypatch, backend):
    monkeypatch.delenv("CPERSONA_ACL_FILE", raising=False)
    monkeypatch.setenv("CPERSONA_TRANSPORT", "stdio")
    monkeypatch.setattr(server.vector, "build_embedding_client", lambda: backend)
    monkeypatch.setattr(vector, "_embedding_client", None)
    reached: dict = {}

    async def _init_db():
        reached["fingerprint"] = generation.fingerprint()
        raise _ReachedInitDb()

    monkeypatch.setattr(server, "init_db", _init_db)
    return reached


@pytest.mark.asyncio
async def test_a_backend_that_does_not_answer_does_not_hold_startup(monkeypatch):
    """The probe waits for an unreachable host's whole connect timeout (30 s by
    default); startup must open the database long before that."""
    monkeypatch.setattr(server, "STARTUP_IDENTITY_PROBE_SECONDS", 0.2, raising=False)
    backend = _StartupBackend(_identity(FINGERPRINT), delay=30)
    reached = _drive_main_to_init_db(monkeypatch, backend)
    started = time.monotonic()
    try:
        with pytest.raises(_ReachedInitDb):
            await asyncio.wait_for(server.main(), timeout=5)
    except asyncio.TimeoutError:
        pytest.fail("main() was still waiting on the identity probe after 5 s; the database never opened")
    assert time.monotonic() - started < 5
    assert reached["fingerprint"] is None, "an unanswered probe leaves the identity unknown"


@pytest.mark.asyncio
async def test_a_backend_that_answers_promptly_is_known_before_the_database_opens(monkeypatch):
    """The bound must not turn the probe into a background guess: a backend that
    answers inside it is learned before anything can write a vector."""
    backend = _StartupBackend(_identity(FINGERPRINT), delay=0)
    reached = _drive_main_to_init_db(monkeypatch, backend)
    with pytest.raises(_ReachedInitDb):
        await asyncio.wait_for(server.main(), timeout=5)
    assert reached["fingerprint"] == FINGERPRINT and backend.asked == 1


# ---------------------------------------------------------------------------
# bug-727 — an unknown transport is refused before anything expensive starts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["streamable_http", "http", "Streamable-HTTP", ""])
async def test_an_unknown_transport_is_refused_before_startup(monkeypatch, name):
    monkeypatch.delenv("CPERSONA_ACL_FILE", raising=False)
    monkeypatch.setenv("CPERSONA_TRANSPORT", name)
    started: list[str] = []

    def _built():
        started.append("embedding client")
        return None

    async def _init_db():
        started.append("database")

    async def _close_db():
        pass

    monkeypatch.setattr(server.vector, "build_embedding_client", _built)
    monkeypatch.setattr(server, "init_db", _init_db)
    monkeypatch.setattr(server, "close_db", _close_db)
    monkeypatch.setattr(server, "install_mgp_validation_filter", lambda: None)
    monkeypatch.setattr(server, "_schedule_startup_calibration", lambda: started.append("calibration"))
    monkeypatch.setattr(server, "_schedule_startup_update_check", lambda: started.append("update check"))
    monkeypatch.setattr(server, "TASK_QUEUE_ENABLED", False)

    async def _block_index_started():
        started.append("block index")

    async def _vector_index_started():
        started.append("vector index")

    monkeypatch.setattr(server.block_index, "on_startup", _block_index_started)
    monkeypatch.setattr(server.vector_index, "on_startup", _vector_index_started)
    with pytest.raises(ValueError, match="Unknown transport"):
        await server.main()
    assert started == [], f"an unknown transport was refused only after: {started}"


@pytest.mark.parametrize("name", ["stdio", "streamable-http"])
def test_the_known_transports_pass_the_startup_check(monkeypatch, name):
    monkeypatch.setenv("CPERSONA_TRANSPORT", name)
    config.validate_transport()


