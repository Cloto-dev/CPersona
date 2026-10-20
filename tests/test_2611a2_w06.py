"""check_health and the embedding backend: what a run says it saw, and what it touches.

- bug-543: a fix run whose own dimension probe reached the backend and got a vector
  back still reported the backend unreachable when an earlier fault was latched, and
  the NULL-embedding prefetch refused to run under that latch, so a restarted backend
  could never be confirmed (or backfilled) from this surface.
- bug-567: under fix=true the verdict came from a residual re-run that had no probe
  result, so a backend that failed this run's probe was listed as a warn issue while
  severity_summary counted no warn and status said healthy.
- bug-566: a fix run re-embedded and wrote NULL embeddings even when ``checks``
  excluded both null-embedding checks; its second pass also took a fresh
  REEMBED_ROW_CAP of rows beyond the first, and its writes were reported nowhere.
- bug-583: the documentation said a report-only run contacts no embedding backend.
  It does (the dimension check embeds a probe text), so the sentence is pinned here
  against the behaviour.
"""

from __future__ import annotations

import pathlib
import struct

import pytest
import pytest_asyncio

from cpersona import checks, health, maintenance_handlers, server, session, vector
from cpersona._vendored_mcp_common.embedding_client import EmbedOutcome
from cpersona.database import get_db

from conftest import FakeEmbeddingClient

AGENT = "agent.w06-health"
ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest_asyncio.fixture
async def db():
    session.reset_pauses_for_tests()
    conn = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()
    yield conn
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()


@pytest.fixture(autouse=True)
def reset_health_state():
    health._reset()
    yield
    health._reset()


class _Backend:
    """Every call reaches the backend (attempted=True); ``up`` decides the answer.

    Never answers from a cache, so a probe that succeeds is a probe that went out.
    """

    mode = "http"
    _http_url = None
    _client = None
    EVIDENCE = "mode=http / POST http://127.0.0.1:1/embed failed: ConnectError: refused"

    def __init__(self, *, up: bool = True):
        self.up = up
        self.texts: list[str] = []

    async def embed(self, texts):
        result, _ = await self.embed_with_outcome(texts)
        return result

    async def embed_with_outcome(self, texts):
        self.texts.extend(texts)
        if not self.up:
            return None, EmbedOutcome(attempted=True, ok=False, error=self.EVIDENCE)
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts], EmbedOutcome(attempted=True, ok=True)

    async def count_tokens(self, texts):
        return None

    @staticmethod
    def pack_embedding(embedding):
        return struct.pack(f"<{len(embedding)}f", *embedding)


class _Counting(FakeEmbeddingClient):
    """The deterministic test client, recording every text it is asked to embed."""

    def __init__(self):
        self.texts: list[str] = []

    async def embed(self, texts):
        self.texts.extend(texts)
        return await super().embed(texts)

    def row_texts(self) -> list[str]:
        # The dimension probe embeds the constant "test"; everything else is a row.
        return [t for t in self.texts if t != "test"]


async def _seed_null_memories(conn, n: int, prefix: str = "null memory row") -> None:
    for i in range(n):
        await conn.execute(
            "INSERT INTO memories (agent_id, content, timestamp) VALUES (?, ?, datetime('now'))",
            (AGENT, f"{prefix} {i}"),
        )
    await conn.commit()


async def _seed_null_episode(conn, summary: str = "null episode summary") -> None:
    await conn.execute("INSERT INTO episodes (agent_id, summary) VALUES (?, ?)", (AGENT, summary))
    await conn.commit()


async def _count(conn, sql: str) -> int:
    return (await conn.execute_fetchall(sql, (AGENT,)))[0][0]


def _only(issues, check):
    found = [i for i in issues if i.get("check") == check]
    assert len(found) == 1, issues
    return found[0]


# ---------------------------------------------------------------------------
# bug-543 — this run's own probe outranks a latched fault
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_probe_that_reached_the_backend_is_not_overruled_by_a_latched_fault(db, monkeypatch):
    monkeypatch.setattr(vector, "_embedding_client", _Backend(up=True))
    for _ in range(health.FAULT_PROMOTE_THRESHOLD):
        health.observe_failure(_Backend.EVIDENCE)
    assert health.is_faulted()

    issues = await checks.check_embedding_backend(
        db, AGENT, fix=True, embedding_cache={"expected_dim": 4, "dim_probe_reached_backend": True}
    )

    assert issues == [], "a probe that reached the backend and got a vector back is a connected backend"


@pytest.mark.asyncio
async def test_a_restarted_backend_is_confirmed_and_backfilled_by_one_fix_run(db, monkeypatch):
    """The runbook says to restart the backend and re-run the check to confirm recovery.

    Before the fix, every re-run said unreachable (the latch decided, not the probe) and
    re-embedded nothing (the prefetch skipped under the latch), so the instruction could
    never be satisfied from check_health.
    """
    backend = _Backend(up=False)
    monkeypatch.setattr(vector, "_embedding_client", backend)
    await _seed_null_memories(db, 1)
    for _ in range(health.FAULT_PROMOTE_THRESHOLD):
        health.observe_failure(_Backend.EVIDENCE)
    assert health.is_faulted()

    backend.up = True
    res = await maintenance_handlers.do_check_health(
        agent_id=AGENT, fix=True, checks=["embedding_backend", "null_embedding"]
    )

    types = [i["type"] for i in res["issues"]]
    assert "embedding_backend_unreachable" not in types, res["issues"]
    assert res["status"] == "healthy", res
    assert _only(res["issues"], "null_embedding")["re_embedded"] == 1
    assert await _count(db, "SELECT COUNT(*) FROM memories WHERE agent_id = ? AND embedding IS NULL") == 0


@pytest.mark.asyncio
async def test_a_failed_probe_under_a_latch_still_skips_the_backfill(db, monkeypatch):
    """The latch guard is kept for the case it was written for: a backend still down is
    not sent a batch per NULL row."""
    backend = _Backend(up=False)
    monkeypatch.setattr(vector, "_embedding_client", backend)
    await _seed_null_memories(db, 3)
    for _ in range(health.FAULT_PROMOTE_THRESHOLD):
        health.observe_failure(_Backend.EVIDENCE)

    res = await maintenance_handlers.do_check_health(
        agent_id=AGENT, fix=True, checks=["embedding_backend", "null_embedding"]
    )

    assert [t for t in backend.texts if t != "test"] == []
    assert _only(res["issues"], "embedding_backend")["type"] == "embedding_backend_unreachable"


# ---------------------------------------------------------------------------
# bug-567 — the verdict counts the liveness warn its own issue list carries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_backend_that_failed_this_runs_probe_degrades_the_fix_verdict(db, monkeypatch):
    """One failed probe in a fresh process does not latch a fault, which is all the
    residual re-run used to be able to see."""
    monkeypatch.setattr(vector, "_embedding_client", _Backend(up=False))

    res = await maintenance_handlers.do_check_health(agent_id=AGENT, fix=True, checks=["embedding_backend"])

    assert not health.is_faulted(), "precondition: a single failure must not latch"
    assert _only(res["issues"], "embedding_backend")["type"] == "embedding_backend_unreachable"
    listed_warn = sum(1 for i in res["issues"] if i["severity"] == "warn")
    assert res["severity_summary"]["warn"] == listed_warn == 1, res
    assert res["status"] == "degraded", res


@pytest.mark.asyncio
async def test_a_full_fix_run_counts_the_backend_warn_in_its_summary(db, monkeypatch):
    """The same with every check selected: whatever else the run finds, the residual
    verdict must include the backend's warn the issue list carries."""
    monkeypatch.setattr(vector, "_embedding_client", _Backend(up=False))

    res = await maintenance_handlers.do_check_health(agent_id=AGENT, fix=True)

    assert _only(res["issues"], "embedding_backend")["type"] == "embedding_backend_unreachable"
    assert res["severity_summary"]["warn"] >= 1, res
    assert res["status"] != "healthy", res


# ---------------------------------------------------------------------------
# bug-566 — a fix run re-embeds only what the caller selected, within the cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_subset_fix_run_does_not_re_embed_unselected_null_rows(db, monkeypatch):
    client = _Counting()
    monkeypatch.setattr(vector, "_embedding_client", client)
    await _seed_null_memories(db, 3)
    await _seed_null_episode(db)

    res = await maintenance_handlers.do_check_health(agent_id=AGENT, fix=True, checks=["stale_pending_tasks"])

    assert res["checks_run"] == ["stale_pending_tasks"]
    assert client.row_texts() == [], "no row text may be sent to the backend for an unselected check"
    assert await _count(db, "SELECT COUNT(*) FROM memories WHERE agent_id = ? AND embedding IS NULL") == 3
    assert await _count(db, "SELECT COUNT(*) FROM episodes WHERE agent_id = ? AND embedding IS NULL") == 1


@pytest.mark.asyncio
async def test_selecting_one_null_check_re_embeds_only_its_own_table(db, monkeypatch):
    """The episode twin: null_episode_embedding alone leaves memory rows alone, and the
    reverse."""
    client = _Counting()
    monkeypatch.setattr(vector, "_embedding_client", client)
    await _seed_null_memories(db, 2)
    await _seed_null_episode(db)

    await maintenance_handlers.do_check_health(agent_id=AGENT, fix=True, checks=["null_episode_embedding"])

    assert await _count(db, "SELECT COUNT(*) FROM episodes WHERE agent_id = ? AND embedding IS NULL") == 0
    assert await _count(db, "SELECT COUNT(*) FROM memories WHERE agent_id = ? AND embedding IS NULL") == 2

    await maintenance_handlers.do_check_health(agent_id=AGENT, fix=True, checks=["null_embedding"])

    assert await _count(db, "SELECT COUNT(*) FROM memories WHERE agent_id = ? AND embedding IS NULL") == 0


@pytest.mark.asyncio
async def test_one_fix_run_writes_no_more_rows_than_the_cap_and_reports_them(db, monkeypatch):
    client = _Counting()
    monkeypatch.setattr(vector, "_embedding_client", client)
    monkeypatch.setattr(checks, "REEMBED_ROW_CAP", 10)
    await _seed_null_memories(db, 25)

    res = await maintenance_handlers.do_check_health(agent_id=AGENT, fix=True, checks=["null_embedding"])

    issue = _only(res["issues"], "null_embedding")
    written = await _count(db, "SELECT COUNT(*) FROM memories WHERE agent_id = ? AND embedding IS NOT NULL")
    assert issue["repairable"] == 10
    assert written == issue["re_embedded"] == 10, (written, issue)


@pytest.mark.asyncio
async def test_rows_the_second_pass_repairs_are_counted_in_re_embedded(db, monkeypatch):
    """embedding_dimension NULLs a wrong-width blob inside the locked run; the prefetch
    could not have seen it, so the second pass embeds it. That write belongs in the
    null_embedding finding's re_embedded, which is the repair's reported outcome."""
    client = _Counting()
    monkeypatch.setattr(vector, "_embedding_client", client)
    await db.execute(
        "INSERT INTO memories (agent_id, content, timestamp, embedding) VALUES (?, ?, datetime('now'), ?)",
        (AGENT, "a row with a wrong-width vector", struct.pack("<3f", 0.1, 0.2, 0.3)),
    )
    await db.commit()

    res = await maintenance_handlers.do_check_health(
        agent_id=AGENT, fix=True, checks=["embedding_dimension", "null_embedding"]
    )

    assert _only(res["issues"], "embedding_dimension")["count"] == 1
    assert await _count(db, "SELECT COUNT(*) FROM memories WHERE agent_id = ? AND embedding IS NULL") == 0
    assert _only(res["issues"], "null_embedding")["re_embedded"] == 1, res["issues"]


# ---------------------------------------------------------------------------
# bug-583 — the documentation says what a report-only run contacts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_report_only_run_contacts_the_backend_and_the_docs_say_so(db, monkeypatch):
    backend = _Backend(up=True)
    monkeypatch.setattr(vector, "_embedding_client", backend)

    await maintenance_handlers.do_check_health(agent_id=AGENT, fix=False)

    assert backend.texts, "precondition: a report-only run sends the dimension probe"
    description = next(t for t in server.registry._tools if t.name == "check_health").description
    false_claims = [
        ("cpersona/server.py check_health description", description, "Nothing contacts the embedding backend unless fix=true"),
        ("docs/operations.md", (ROOT / "docs/operations.md").read_text(encoding="utf-8"), "makes no\n   network call"),
        ("docs/operations.ja.md", (ROOT / "docs/operations.ja.md").read_text(encoding="utf-8"), "はネットワーク呼び出しを行わないので"),
        ("plugin/skills/cpersona-memory/SKILL.md", (ROOT / "plugin/skills/cpersona-memory/SKILL.md").read_text(encoding="utf-8"), "makes no network call"),
        (
            "plugin/skills/cpersona-memory/references/embedding-backend-repair.md",
            (ROOT / "plugin/skills/cpersona-memory/references/embedding-backend-repair.md").read_text(encoding="utf-8"),
            "makes no network call",
        ),
    ]
    stale = [where for where, text, claim in false_claims if claim in text]
    assert stale == [], f"these still say a report-only run makes no network call: {stale}"
