"""Regressions for the I4-admin cluster (252-a2 audit): bug-148 and bug-149.

bug-148: do_delete_episode must remove the ``ep:{id}`` vector from the remote index in
remote mode, mirroring the bug-023 removal already done by do_delete_memory.

bug-149: do_set_recall_precision's failure rollback must not clobber a concurrent
successful writer for the same agent_id (compare-and-restore, not a blind restore
of the pre-await snapshot).
"""

import pytest
import pytest_asyncio

from cpersona import admin_handlers, vector
from cpersona.database import get_db


@pytest_asyncio.fixture
async def clean_db():
    db = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()
    return db


class _RecordingHttpClient:
    """Records every POST (url, json) and optionally raises to model a remote fault."""

    def __init__(self, raise_on_post: bool = False):
        self.calls: list[tuple[str, dict]] = []
        self._raise = raise_on_post

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs.get("json")))
        if self._raise:
            raise RuntimeError("simulated remote failure")
        return None


class _RecordingRemoteClient:
    """Stand-in for vector._embedding_client with a remote HTTP endpoint configured."""

    def __init__(self, raise_on_post: bool = False):
        self._http_url = "http://embedding.test/embed"
        self._client = _RecordingHttpClient(raise_on_post)


# --- bug-148: delete_episode purges the remote vector index -----------------------


@pytest.mark.asyncio
async def test_bug148_delete_episode_removes_remote_vector(clean_db, monkeypatch):
    cur = await clean_db.execute(
        "INSERT INTO episodes (agent_id, summary) VALUES ('remote-ep', 'cell biology session')"
    )
    await clean_db.commit()
    eid = cur.lastrowid

    client = _RecordingRemoteClient()
    monkeypatch.setattr(admin_handlers, "VECTOR_SEARCH_MODE", "remote")
    monkeypatch.setattr(vector, "_embedding_client", client)

    res = await admin_handlers.do_delete_episode(eid, agent_id="remote-ep")

    assert res == {"ok": True, "deleted_id": eid}
    assert client._client.calls, "expected a remote /remove POST on delete_episode"
    url, payload = client._client.calls[0]
    assert url == "http://embedding.test/remove"
    assert payload == {"namespace": "cpersona:remote-ep", "ids": [f"ep:{eid}"]}


@pytest.mark.asyncio
async def test_bug148_delete_episode_resolves_owner_namespace_when_unscoped(clean_db, monkeypatch):
    """An unscoped delete (agent_id omitted) must still remove under the owner's namespace."""
    cur = await clean_db.execute(
        "INSERT INTO episodes (agent_id, summary) VALUES ('owner-x', 'owned episode')"
    )
    await clean_db.commit()
    eid = cur.lastrowid

    client = _RecordingRemoteClient()
    monkeypatch.setattr(admin_handlers, "VECTOR_SEARCH_MODE", "remote")
    monkeypatch.setattr(vector, "_embedding_client", client)

    res = await admin_handlers.do_delete_episode(eid)

    assert res["ok"] is True
    assert client._client.calls, "expected a remote /remove POST even when unscoped"
    _url, payload = client._client.calls[0]
    assert payload["namespace"] == "cpersona:owner-x"
    assert payload["ids"] == [f"ep:{eid}"]


@pytest.mark.asyncio
async def test_bug148_remote_removal_failure_does_not_fail_delete(clean_db, monkeypatch):
    """A remote /remove fault is non-fatal: the SQLite delete still succeeds."""
    cur = await clean_db.execute(
        "INSERT INTO episodes (agent_id, summary) VALUES ('remote-ep', 'boom')"
    )
    await clean_db.commit()
    eid = cur.lastrowid

    client = _RecordingRemoteClient(raise_on_post=True)
    monkeypatch.setattr(admin_handlers, "VECTOR_SEARCH_MODE", "remote")
    monkeypatch.setattr(vector, "_embedding_client", client)

    res = await admin_handlers.do_delete_episode(eid, agent_id="remote-ep")

    assert res == {"ok": True, "deleted_id": eid}
    assert client._client.calls, "removal was attempted before the (tolerated) failure"
    rows = await clean_db.execute_fetchall("SELECT id FROM episodes WHERE id = ?", (eid,))
    assert rows == []


# --- bug-149: precision-rollback must not clobber a concurrent writer -------------


@pytest.mark.asyncio
async def test_bug149_failed_rollback_preserves_concurrent_writer(monkeypatch):
    """R1 fails calibration after R2 applied+persisted a new beta for the same agent.

    R1's rollback must NOT restore its stale pre-await snapshot over R2's value.
    """
    agent = "cbeta-agent"
    # A fresh, isolated overrides dict with agent's persisted override beta=1.0.
    monkeypatch.setattr(vector, "_agent_betas", {agent: 1.0})

    async def fake_calibrate(agent_id: str):
        # Simulate the concurrent writer R2 applying + persisting beta=0.5 while R1
        # is suspended inside calibration, then R1's own calibration failing transiently.
        vector._agent_betas[agent_id] = 0.5
        return {"ok": False, "error": "database is locked"}

    monkeypatch.setattr(admin_handlers, "do_calibrate_threshold", fake_calibrate)

    # R1 asks for strict (beta=2.0); it applies 2.0, awaits calibrate (which interleaves
    # R2's 0.5 and then fails), and must leave R2's 0.5 in place on its failure rollback.
    res = await admin_handlers.do_set_recall_precision(agent_id=agent, precision="strict")

    assert res["ok"] is False
    assert vector._agent_betas[agent] == 0.5, (
        "R2's applied+persisted value was clobbered by R1's stale-snapshot rollback"
    )


@pytest.mark.asyncio
async def test_bug149_solo_failure_still_rolls_back_own_write(monkeypatch):
    """No concurrent writer: R1's own failed write must still roll back to prev_beta."""
    agent = "solo-agent"
    monkeypatch.setattr(vector, "_agent_betas", {agent: 1.0})

    async def fake_calibrate(agent_id: str):
        return {"ok": False, "error": "too few embeddings"}

    monkeypatch.setattr(admin_handlers, "do_calibrate_threshold", fake_calibrate)

    res = await admin_handlers.do_set_recall_precision(agent_id=agent, precision="strict")

    assert res["ok"] is False
    assert vector._agent_betas[agent] == 1.0, "solo failure must restore the prior beta"


@pytest.mark.asyncio
async def test_bug149_solo_clear_failure_restores_absence(monkeypatch):
    """No concurrent writer, clearing an unset agent: failed clear must leave it unset."""
    agent = "unset-agent"
    monkeypatch.setattr(vector, "_agent_betas", {})

    async def fake_calibrate(agent_id: str):
        return {"ok": False, "error": "too few embeddings"}

    monkeypatch.setattr(admin_handlers, "do_calibrate_threshold", fake_calibrate)

    res = await admin_handlers.do_set_recall_precision(agent_id=agent)  # clear path

    assert res["ok"] is False
    assert agent not in vector._agent_betas
