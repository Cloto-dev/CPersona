"""What a traced recall records for a replay that starts from one of its stages.

A traced recall names the provider set it ran with, and for each stage records
how many rows the stage received and a digest of which rows in which order
(recall_trace.stage_input). Pinned here: the digests agree with what the rest of
the trace says each stage saw, two runs over the same state agree, and when one
stage changes its output only the stages after it see a different input.
"""
import dataclasses
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from cpersona import builtin_providers, memory_handlers, providers, recall_trace
from cpersona.database import get_db

AGENT = "agent.trace-seams"
NOW = datetime.now(timezone.utc)
QUERY = "harbor lighthouse keeper logbook"
CORPUS = [(f"harbor lighthouse keeper logbook entry {i}", 10 * i + 1) for i in range(12)]


def _ts(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


async def _seed(rows=CORPUS, agent=AGENT):
    db = await get_db()
    for table in ("memories", "episodes", "profiles"):
        await db.execute(f"DELETE FROM {table} WHERE agent_id = ?", (agent,))
    await db.commit()
    for content, days_ago in rows:
        await memory_handlers.do_store(
            agent, {"content": content, "source": {"System": "test"}, "timestamp": _ts(days_ago)}
        )


def _digest(refs: list[str]) -> str:
    return hashlib.sha256(json.dumps(refs, separators=(",", ":")).encode()).hexdigest()[:16]


@pytest.fixture
def installed():
    previous = providers.active()
    yield providers.install
    providers.install(previous)


def _reversing_prior() -> providers.Providers:
    """A prior that reverses what it is given -- a reorder, which its contract allows."""
    base = builtin_providers.Prior
    cls = type("ReversingPrior", (base,), {
        "manifest": dataclasses.replace(base.manifest, provider_id="reversing"),
        "apply": lambda self, results, span, now: list(reversed(results)),
    })
    allow = {name: dict(ids) for name, ids in builtin_providers.ALLOWLIST.items()}
    allow["prior"]["reversing"] = cls
    return providers.resolve(allow, {"prior": "reversing"})


def test_the_digest_is_of_refs_in_order():
    rows = [{"id": 3, "_rid": ("mem", 3)}, {"id": 1, "_rid": ("ep", 1)}]
    assert recall_trace.order_digest(rows) == _digest(["mem:3", "ep:1"])
    assert recall_trace.order_digest(rows[::-1]) == _digest(["ep:1", "mem:3"])
    assert recall_trace.order_digest(rows) != recall_trace.order_digest(rows[::-1])


@pytest.mark.asyncio
async def test_a_traced_recall_names_the_provider_set(fake_embedding_client, installed):
    await _seed()
    out = await memory_handlers.do_recall(AGENT, QUERY, limit=5, trace=True)
    active = providers.active()
    assert out["trace"]["providers"] == {"digest": active.digest, "slots": active.describe()}
    assert {s["provider_id"] for s in out["trace"]["providers"]["slots"].values()} == {"builtin"}
    installed(_reversing_prior())
    swapped = await memory_handlers.do_recall(AGENT, QUERY, limit=5, trace=True)
    assert swapped["trace"]["providers"]["digest"] != out["trace"]["providers"]["digest"]
    assert swapped["trace"]["providers"]["slots"]["prior"]["provider_id"] == "reversing"


@pytest.mark.asyncio
async def test_the_digests_agree_with_what_the_trace_says_each_stage_saw(fake_embedding_client):
    await _seed()
    out = await memory_handlers.do_recall(AGENT, QUERY, limit=5, trace=True)
    trace = out["trace"]
    inputs = trace["stage_inputs"]
    assert set(inputs) == {"scoring", "gate", "autocut", "prior", "cut", "output"}
    # The gate decides every row it receives.
    assert inputs["gate"]["rows"] == len(trace["gate"]["decisions"])
    assert inputs["gate"]["order"] == _digest([d["ref"] for d in trace["gate"]["decisions"]])
    # The cut receives the order the trace records before it.
    before_cut = [r["ref"] for r in trace["order"]["before_cut"]]
    assert inputs["cut"] == {"rows": len(before_cut), "order": _digest(before_cut)}
    # What comes out is the response, best first (the response lists it reversed).
    returned = [m["ref"] for m in reversed(out["messages"])]
    assert inputs["output"] == {"rows": len(returned), "order": _digest(returned)}


@pytest.mark.asyncio
async def test_two_runs_over_the_same_state_record_the_same_inputs(fake_embedding_client):
    await _seed()
    first = await memory_handlers.do_recall(AGENT, QUERY, limit=5, trace=True)
    second = await memory_handlers.do_recall(AGENT, QUERY, limit=5, trace=True)
    assert first["trace"]["stage_inputs"] == second["trace"]["stage_inputs"]


@pytest.mark.asyncio
async def test_only_the_stages_after_a_changed_stage_see_a_different_input(fake_embedding_client, installed):
    await _seed()
    plain = (await memory_handlers.do_recall(AGENT, QUERY, limit=5, trace=True))["trace"]["stage_inputs"]
    installed(_reversing_prior())
    changed = (await memory_handlers.do_recall(AGENT, QUERY, limit=5, trace=True))["trace"]["stage_inputs"]
    assert plain["cut"]["rows"] >= 2, "the fixture must give the prior something to reorder"
    for stage in ("scoring", "gate", "autocut", "prior"):
        assert changed[stage] == plain[stage], stage
    for stage in ("cut", "output"):
        assert changed[stage]["order"] != plain[stage]["order"], stage


@pytest.mark.asyncio
async def test_the_selector_input_is_recorded_when_a_cue_runs(fake_embedding_client):
    await _seed()
    time_cue = {"after": _ts(75)[:10], "before": _ts(46)[:10], "confidence": "sure"}
    out = await memory_handlers.do_recall(AGENT, QUERY, limit=12, trace=True, time_cue=time_cue)
    inputs = out["trace"]["stage_inputs"]
    # The selector receives what the count cut left, in the order the cut left it.
    kept = [r["ref"] for r in out["trace"]["order"]["before_cut"]][: out["trace"]["order"]["limit"]]
    assert inputs["selector"] == {"rows": len(kept), "order": _digest(kept)}
