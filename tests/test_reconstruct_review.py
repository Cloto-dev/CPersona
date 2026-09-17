"""Regressions for scoped identity, delivered notices and effective bounds."""

import pytest
import pytest_asyncio

from cpersona import config, health, memory_handlers as M, reconstruct as R, update_check
from cpersona.database import get_db

AGENT = "reconstruct-review"


@pytest_asyncio.fixture(autouse=True)
async def isolated_state(monkeypatch):
    db = await get_db()
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM episodes")
    await db.commit()
    monkeypatch.setattr(config, "RECONSTRUCT_FORCED_COUNT", None)
    monkeypatch.setattr(config, "RECONSTRUCT_MAX_COUNT", 10)
    monkeypatch.setattr(config, "RECONSTRUCT_ADJACENCY_SECONDS", 0)
    monkeypatch.setattr(config, "EMBEDDING_MODE", "none")
    monkeypatch.setattr(config, "DEGRADED_ADVISORY_ENABLED", True)
    monkeypatch.setattr(config, "UPDATE_CHECK_ENABLED", True)
    health._reset()
    update_check._reset()
    yield
    health._reset()
    update_check._reset()


async def stored(i, project="", msg_id=None, stamp=None):
    out = await M.do_store(AGENT, {
        "id": msg_id or f"record-{i}", "content": f"independent project fact {i}",
        "timestamp": stamp or f"2026-01-0{i + 1}T10:00:00Z",
    }, project_id=project)
    assert out["result"] == "stored"
    return f"mem:{out['id']}"


def candidate(i, project, stamp="2026-01-01T10:00:00Z"):
    row = R._Candidate({"ref": f"mem:{i}", "id": "shared-id", "content": f"fact {i}",
                        "timestamp": stamp}, rank=i)
    row.context = None if project is None else (project, "")
    return row


@pytest.mark.asyncio
@pytest.mark.parametrize("projects", [("alpha", "beta"), ("alpha", "")])
async def test_same_message_id_in_distinct_projects_stays_independent(projects):
    refs = {await stored(i, project, "shared-id") for i, project in enumerate(projects)}
    out = await R.do_reconstruct(AGENT, "", count=5, trace=True)
    assert out["returned_count"] == 2
    assert {item["claims"][0]["ref"] for item in out["items"]} == refs
    assert all(len(item["claims"]) == 1 for item in out["items"])
    assert all("roles" not in claim for item in out["items"] for claim in item["claims"])
    assert all("conflicts" not in item for item in out["items"])


@pytest.mark.parametrize("other_project", ["beta", None])
def test_supersession_requires_known_matching_identity_namespace(other_project):
    old = candidate(1, "alpha")
    new = candidate(2, other_project, "2026-01-02T10:00:00Z")
    assert R._roles_for(old, [old, new], {}) == []


@pytest.mark.parametrize("other_project", ["beta", None])
def test_conflict_requires_known_matching_identity_namespace(other_project):
    assert R._conflicts([candidate(1, "alpha"), candidate(2, other_project)]) == []


def test_missing_context_cannot_establish_identity():
    rows = [candidate(1, None), candidate(2, None)]
    union = R.bundle(rows, {})
    assert union.find(0) != union.find(1)
    assert R._conflicts(rows) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("with_record,count", [(False, 1), (True, 1), (True, 0)])
async def test_reconstruction_delivers_full_degradation_notice_once(with_record, count):
    if with_record:
        await stored(0)
    first = await R.do_reconstruct(AGENT, "", count=count, session_key="review-advisory")
    second = await R.do_reconstruct(AGENT, "", count=count, session_key="review-advisory")
    fresh = await M.do_recall(AGENT, "", 20, session_key="fresh-advisory")
    assert first["advisory"] == fresh["advisory"]
    assert first["advisory"]["degraded"] is True
    assert second["advisory"]["runbook"] == health.HINT_RUNBOOK_SHORT
    assert first["advisory"]["runbook"] != second["advisory"]["runbook"]
    assert first["returned_count"] == int(with_record and count > 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_record", [False, True])
async def test_reconstruction_delivers_update_before_session_suppression(monkeypatch, with_record):
    if with_record:
        await stored(0)
    monkeypatch.setattr(update_check, "_verdict", update_check._verdict_dict(
        update_check.STATE_NEWER, "0.0.1", update_check.KIND_NEWER, "0.0.2", None))
    monkeypatch.setattr(update_check, "detect_install", lambda available: {
        "method": "unknown", "command": None})
    monkeypatch.setattr(update_check, "describe", lambda verdict, install: "Test update available")
    first = await R.do_reconstruct(AGENT, "", session_key="review-update")
    second = await R.do_reconstruct(AGENT, "", session_key="review-update")
    assert first["update"]["available"] == "0.0.2"
    assert first["update"]["kind"] == update_check.KIND_NEWER
    assert "update" not in second
    assert "update" not in await M.do_recall(AGENT, "", 20, session_key="review-update")
    assert "update" in await R.do_reconstruct(AGENT, "", session_key="another-update")


@pytest.mark.asyncio
@pytest.mark.parametrize("n", [0, 1, 5])
async def test_library_ceiling_reports_requested_and_effective_candidate_bounds(monkeypatch, n):
    for i in range(n):
        await stored(i)
    monkeypatch.setattr(M, "RECALL_LIBRARY_MAX_LIMIT", 2)
    out = await R.do_reconstruct(AGENT, "", count=5, top_k=20, trace=True)
    assert out["bounds"]["top_k"] == 20
    assert out["bounds"]["effective_top_k"] == 2
    assert out["bounds"]["truncated"] is True
    assert out["reconstruction"]["candidate_count"] == min(n, 2)
    assert out["returned_count"] == min(n, 2)


@pytest.mark.asyncio
async def test_unclamped_candidate_bound_retains_the_existing_shape():
    for i in range(5):
        await stored(i)
    out = await R.do_reconstruct(AGENT, "", count=5, top_k=20)
    assert out["reconstruction"]["candidate_count"] == out["returned_count"] == 5
    assert out["bounds"] == {"top_k": 20, "max_hops": config.RECONSTRUCT_MAX_HOPS,
                             "max_evidence": config.RECONSTRUCT_MAX_EVIDENCE, "truncated": False}
