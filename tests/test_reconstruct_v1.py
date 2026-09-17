"""Observable reconstruction contracts on stored conversation records."""

import pytest
import pytest_asyncio

from cpersona import config
from cpersona import memory_handlers as M
from cpersona import reconstruct as R
from cpersona.database import get_db

AGENT = "reconstruction-v1"


def test_unconfigured_count_policy_is_the_declared_experimental_default():
    import json
    import os
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if not k.startswith("CPERSONA_RECONSTRUCT_")}
    code = "from cpersona.reconstruct import resolve_count; import json; print(json.dumps([resolve_count(None),resolve_count(99)]))"
    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)
    default, maximum = json.loads(result.stdout)
    assert default == [1, {"source": "server_default", "clamped": False, "reason": "count_omitted"}]
    assert maximum == [10, {"source": "caller", "clamped": True, "reason": "count_requested"}]


@pytest_asyncio.fixture(autouse=True)
async def corpus(fake_embedding_client, monkeypatch):
    db = await get_db()
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM episodes")
    await db.commit()
    monkeypatch.setattr(config, "RECONSTRUCT_ADJACENCY_SECONDS", 60)
    monkeypatch.setattr(config, "RECONSTRUCT_MAX_COUNT", 10)
    monkeypatch.setattr(config, "RECONSTRUCT_FORCED_COUNT", None)


async def store(i, *, second=0, channel="", project="", source_type="User"):
    out = await M.do_store(
        AGENT,
        {"id": f"record-{i}", "content": f"deployment evidence number {i}",
         "timestamp": f"2026-01-01T10:{second // 60:02d}:{second % 60:02d}+00:00",
         "source": {"type": source_type, "id": "shared"}},
        channel=channel, project_id=project,
    )
    assert out["result"] == "stored"


@pytest.mark.asyncio
async def test_zero_count_on_an_empty_corpus_reports_the_requested_zero():
    out = await R.do_reconstruct(AGENT, "", count=0, trace=True)
    assert out["requested_count"] == out["effective_count"] == out["returned_count"] == 0
    assert out["items"] == []
    assert out["shortfall_reason"] == "count_zero"
    assert out["trace"] == {"candidate_refs": [], "clusters": []}


@pytest.mark.asyncio
async def test_requested_and_forced_five_return_only_two_available_items(monkeypatch):
    await store(1, channel="one")
    await store(2, channel="two")
    expected_refs = None
    for requested, forced, source in ((5, None, "caller"), (1, 5, "operator_forced")):
        monkeypatch.setattr(config, "RECONSTRUCT_FORCED_COUNT", forced)
        out = await R.do_reconstruct(AGENT, "", count=requested, top_k=20)
        # A forced count is the server overriding the caller, so the policy is stated;
        # a request honoured as asked is not restated.
        assert ("count_policy" in out) == ("requested_count" in out) == (forced is not None)
        out = await R.do_reconstruct(AGENT, "", count=requested, top_k=20, trace=True)
        assert out["requested_count"] == requested
        assert out["effective_count"] == 5
        assert out["returned_count"] == len(out["items"]) == 2
        assert out["count_policy"]["source"] == source
        assert out["shortfall_reason"] == R.SHORTFALL_EXHAUSTED_CANDIDATES
        refs = [item["claims"][0]["ref"] for item in out["items"]]
        assert len(set(refs)) == 2
        if expected_refs is None:
            expected_refs = refs
        assert refs == expected_refs


@pytest.mark.asyncio
async def test_concurrent_channels_projects_and_source_types_are_separate():
    await store(1, channel="one")
    await store(2, channel="two")
    await store(3, channel="one", project="other")
    await store(4, channel="one", source_type="Agent")
    out = await R.do_reconstruct(AGENT, "", count=10, top_k=20, trace=True)
    assert out["returned_count"] == 4
    assert all(len(item["claims"]) == 1 for item in out["items"])
    assert out["reconstruction"]["cluster_count"] == 4


@pytest.mark.asyncio
async def test_adjacency_bounds_the_whole_burst_not_each_gap():
    for i, seconds in enumerate((0, 40, 80, 120)):
        await store(i, second=seconds)
    out = await R.do_reconstruct(AGENT, "", count=10, top_k=20)
    assert out["returned_count"] == 2
    assert sorted(len(item["claims"]) for item in out["items"]) == [2, 2]


@pytest.mark.asyncio
async def test_episode_in_other_channel_is_not_supporting_evidence():
    await store(1, second=20, channel="one")
    episode = await M.do_archive_episode(
        AGENT,
        [{"role": "user", "content": "deployment starts", "timestamp": "2026-01-01T10:00:00+00:00"},
         {"role": "assistant", "content": "deployment ends", "timestamp": "2026-01-01T10:01:00+00:00"}],
        summary="deployment evidence", keywords="deployment", channel="two",
    )
    assert episode["episode_id"]
    # The lexical query retrieves the episode as well as the memory. Disable
    # only the gate in this focused relation test; ranking is not the subject.
    from unittest.mock import patch
    with patch.object(M, "_apply_quality_gate", side_effect=lambda rows, *a, **k: rows):
        out = await R.do_reconstruct(AGENT, "deployment", count=10, top_k=20)
    refs = [claim["ref"] for item in out["items"] for claim in item["claims"]]
    assert any(ref.startswith("ep:") for ref in refs)
    assert any(ref.startswith("mem:") for ref in refs)
    assert out["returned_count"] == 2
    assert not any("roles" in claim for item in out["items"] for claim in item["claims"])


@pytest.mark.asyncio
async def test_evidence_ceiling_bounds_claims_and_resolvable_refs():
    for i in range(4):
        await store(i, second=i * 10)
    out = await R.do_reconstruct(AGENT, "", count=1, top_k=20, max_evidence=2)
    item = out["items"][0]
    refs = {row["ref"] for row in item["claims"]}
    assert len(refs) == len(item["claims"]) == 2
    assert all(role["ref"] in refs for claim in item["claims"] for role in claim.get("roles", []))
    expanded = await M.do_get_contents(AGENT, sorted(refs))
    assert expanded["missing"] == []
    contents = {row["ref"]: row["content"] for row in expanded["items"]}
    assert item["head_ref"] in refs
    assert contents[item["head_ref"]] == item["content"]
    assert out["bounds"]["omitted"] == ["max_evidence"]
    assert item["claims_omitted"] == 2


@pytest.mark.asyncio
async def test_count_trace_explains_pool_and_selection_without_changing_breadth():
    for i in range(8):
        await store(i, second=i * 120)
    seen = []
    for count in (0, 1, 2, 4, 8):
        out = await R.do_reconstruct(AGENT, "", count=count, top_k=20, trace=True)
        assert out["returned_count"] == len(out["items"]) == count
        assert out["reconstruction"] == {
            "policy": "v1", "candidate_count": 8, "cluster_count": 8,
            "selected_count": count, "excluded_without_provenance": 0,
        }
        seen.append(out["trace"]["candidate_refs"])
        assert len(out["trace"]["clusters"]) == 8
        if count == 0:
            assert out["shortfall_reason"] == "count_zero"
    assert all(refs == seen[0] for refs in seen)
    assert "trace" not in await R.do_reconstruct(AGENT, "", count=1)


@pytest.mark.asyncio
async def test_gate_fallback_is_visible_even_when_count_is_filled(monkeypatch):
    await store(1)
    original = M.do_recall

    async def rescued(*args, **kwargs):
        out = await original(*args, **kwargs)
        out["gate_fallback"] = True
        return out

    monkeypatch.setattr(M, "do_recall", rescued)
    out = await R.do_reconstruct(AGENT, "", count=1)
    assert out["returned_count"] == 1
    assert out["gate_fallback"] is True


@pytest.mark.asyncio
async def test_registered_mcp_tool_preserves_trace_preview_and_read_scope(tmp_path, monkeypatch):
    import json
    from cpersona import acl, server

    await store(1)
    await store(2, second=120)
    path = tmp_path / "reader-acl.json"
    path.write_text(json.dumps({"clients": [{"client_id": "v1-reader", "token": None,
                                           "grants": {AGENT: "read"}}]}))
    monkeypatch.setattr(config, "RECALL_PREVIEW_CHARS", 5)
    previous = acl.current_principal()
    acl.activate(acl.load_config(str(path)))
    token = acl.set_principal(acl.Principal("v1-reader"))
    try:
        handler = server.registry._handlers["reconstruct"]
        out = await handler({"agent_id": AGENT, "query": "", "count": 2, "trace": True})
        denied = await handler({"agent_id": "another-agent", "query": ""})
    finally:
        acl.reset_principal(token)
        acl.activate(None)
    assert acl.current_principal() == previous
    assert out["returned_count"] == 2
    assert len(out["trace"]["candidate_refs"]) == 2
    assert all(item["content"] == "deplo" and item["content_truncated"] for item in out["items"])
    assert denied["error"] == "permission_denied"
