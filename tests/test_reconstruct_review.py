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
    # The lowered ceiling is a fact of every call; "reached" only of one that met it.
    assert out["bounds"].get("reached") == (["top_k"] if n >= 2 else None)
    assert "omitted" not in out["bounds"]
    assert out["reconstruction"]["candidate_count"] == min(n, 2)
    assert out["returned_count"] == min(n, 2)


@pytest.mark.asyncio
async def test_unclamped_candidate_bound_retains_the_existing_shape():
    for i in range(5):
        await stored(i)
    out = await R.do_reconstruct(AGENT, "", count=5, top_k=20, trace=True)
    assert out["reconstruction"]["candidate_count"] == out["returned_count"] == 5
    assert out["bounds"] == {"top_k": 20, "max_hops": config.RECONSTRUCT_MAX_HOPS,
                             "max_evidence": config.RECONSTRUCT_MAX_EVIDENCE}


# --------------------------------------------------------------------------------------
# The envelope: a search that did what it was asked says so in two integers
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_request_honoured_as_asked_is_not_restated():
    for i in range(3):
        await stored(i)
    out = await R.do_reconstruct(AGENT, "", count=3, top_k=20, budget=5000)
    # (`advisory` is recall's once-per-session notice that embeddings are off here; it is forwarded, not part of the envelope)
    assert set(out) - {"advisory"} == {"items", "effective_count", "returned_count"}
    assert out["effective_count"] == out["returned_count"] == 3
    # The full audit is unchanged, and carries exactly what the compact form dropped.
    full = await R.do_reconstruct(AGENT, "", count=3, top_k=20, budget=5000, trace=True)
    assert set(full) - set(out) == {
        "requested_count", "count_policy", "requested_budget", "effective_budget", "used_budget",
        "budget_policy", "bounds", "reconstruction", "trace",
    }
    assert full["items"] == out["items"]


@pytest.mark.asyncio
async def test_a_short_return_still_says_why():
    await stored(0)
    out = await R.do_reconstruct(AGENT, "", count=5, top_k=20)
    assert out["returned_count"] == 1 and out["shortfall_reason"] == R.SHORTFALL_EXHAUSTED_CANDIDATES
    empty = await R.do_reconstruct("nobody", "", count=5, top_k=20)
    assert set(empty) - {"advisory"} == {"items", "effective_count", "returned_count", "shortfall_reason"}


@pytest.mark.asyncio
async def test_a_clamped_count_states_its_policy_and_a_clamped_budget_its_own(monkeypatch):
    await stored(0)
    out = await R.do_reconstruct(AGENT, "", count=config.RECONSTRUCT_MAX_COUNT + 5, top_k=20)
    assert out["requested_count"] == config.RECONSTRUCT_MAX_COUNT + 5 and out["count_policy"]["clamped"] is True
    assert "budget_policy" not in out  # each policy is stated for its own reason
    out = await R.do_reconstruct(AGENT, "", count=1, top_k=20, budget=config.RECONSTRUCT_MAX_BUDGET + 1)
    assert out["requested_budget"] == config.RECONSTRUCT_MAX_BUDGET + 1 and out["budget_policy"]["clamped"] is True
    assert "count_policy" not in out
    raised = await R.do_reconstruct(AGENT, "", count=1, top_k=20, budget=1)
    assert raised["budget_policy"]["reason"] == "raised_to_one_excerpt"


@pytest.mark.asyncio
async def test_a_lowered_ceiling_and_a_reached_depth_keep_their_bounds(monkeypatch):
    for i in range(3):
        await stored(i)
    monkeypatch.setattr(M, "RECALL_LIBRARY_MAX_LIMIT", 2)
    out = await R.do_reconstruct(AGENT, "", count=5, top_k=20)
    assert out["bounds"]["effective_top_k"] == 2 and out["bounds"]["reached"] == ["top_k"]


@pytest.mark.asyncio
async def test_a_lowered_ceiling_is_stated_even_when_the_pool_never_met_it(monkeypatch):
    await stored(0)
    monkeypatch.setattr(M, "RECALL_LIBRARY_MAX_LIMIT", 2)
    out = await R.do_reconstruct(AGENT, "", count=5, top_k=20)
    assert out["bounds"]["effective_top_k"] == 2 and "reached" not in out["bounds"]


@pytest.mark.asyncio
async def test_rows_excluded_for_missing_provenance_are_counted_without_a_trace(monkeypatch):
    await stored(0)
    real = M.do_recall

    async def with_a_refless_row(*args, **kwargs):
        result = await real(*args, **kwargs)
        result["messages"] = [{"content": "profile", "id": "-1"}, *result["messages"]]
        return result

    monkeypatch.setattr(M, "do_recall", with_a_refless_row)
    out = await R.do_reconstruct(AGENT, "", count=1, top_k=20)
    assert out["reconstruction"] == {"excluded_without_provenance": 1}
