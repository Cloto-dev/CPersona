"""Reconstruct: a node expand refuses a rewritten record, and a shortfall says when the
quality gate refused the rows that were found.

bug-831: a node expand carried no revision, so once update_memory had rebuilt the
record's nodes, get_contents served the new text under the old node index.
bug-832: the shortfall reason was chosen from gate_fallback alone, which the default
configuration never sets, so a pool the gate emptied read as "no relevant evidence"
(docs/behavior-contracts.md section 12).
"""

import pytest

from cpersona import admin_handlers, blocks, config, memory_handlers, reconstruct
from tests.test_reconstruct_excerpts import LONG, WINDOW, _TempDB

AGENT = "agent.u07"
NEW = " ".join(f"other words number {i} on unrelated matters." for i in range(40)) + " Nothing about vaults."


@pytest.mark.usefixtures("blocks_off")
@pytest.mark.asyncio
async def test_bug_831_node_expand_refuses_a_record_rewritten_and_rebuilt(fake_embedding_client, monkeypatch):
    monkeypatch.setattr(config, "RECONSTRUCT_QUOTE_CHARS", 0)
    monkeypatch.setattr(config, "RECALL_PREVIEW_CHARS", 10)
    fake_embedding_client.token_window = WINDOW
    async with _TempDB() as tmp:
        await memory_handlers.do_store(AGENT, {"content": LONG})
        await tmp.drain()
        (item,) = (await reconstruct.do_reconstruct(AGENT, "vault combination", deep=True))["items"]
        expand = item["expand"]
        assert expand == {
            "ref": item["head_ref"], "node": item["node"]["index"], "revision": blocks.text_revision(LONG),
        }
        # Before the rewrite the expand still reads the rest of its node.
        (before,) = (await memory_handlers.do_get_contents(AGENT, [expand]))["items"]
        assert before["content"].startswith(item["content"]) and "7431" in before["content"]

        mid = int(item["head_ref"].split(":")[1])
        assert (await admin_handlers.do_update_memory(mid, NEW, agent_id=AGENT)).get("ok")
        await tmp.drain()
        # The nodes were rebuilt for the new text, so the bare node index resolves again.
        (rebuilt,) = (await memory_handlers.do_get_contents(AGENT, [{"ref": expand["ref"], "node": expand["node"]}]))[
            "items"
        ]
        assert rebuilt["content"] in NEW
        # The expand that was handed out names the old text and is refused.
        after = await memory_handlers.do_get_contents(AGENT, [expand])
        assert not [i for i in after.get("items", []) if "content" in i], after
        assert after["unresolved"] == [{"ref": expand["ref"], "reason": "stale_revision"}]


async def _store_three():
    for text in ("the walnut desk arrived on Monday", "studio lamp replaced", "chair delivery delayed"):
        await memory_handlers.do_store(AGENT, {"content": text})


@pytest.mark.usefixtures("blocks_off", "no_keyword_seat")
@pytest.mark.asyncio
async def test_bug_832_a_pool_the_gate_emptied_reports_below_quality_threshold(fake_embedding_client):
    async with _TempDB() as tmp:
        await _store_three()
        await tmp.drain()
        out = await reconstruct.do_reconstruct(AGENT, "walnut quokka zebra marmot", count=3, trace=True)
        decisions = out["trace"]["recall"]["gate"]["decisions"]
        # Precondition: a row was found and the gate refused every one.
        assert decisions and all(d["passed"] is False for d in decisions)
        assert "gate_fallback" not in out
        assert out["items"] == []
        assert out["shortfall_reason"] == "below_quality_threshold"


@pytest.mark.usefixtures("blocks_off", "no_keyword_seat")
@pytest.mark.asyncio
async def test_bug_832_nothing_found_still_reports_no_relevant_evidence(fake_embedding_client):
    async with _TempDB():
        out = await reconstruct.do_reconstruct(AGENT, "walnut quokka zebra marmot", count=3, trace=True)
        assert out["trace"]["recall"].get("gate", {}).get("decisions", []) == []
        assert out["items"] == []
        assert out["shortfall_reason"] == "no_relevant_evidence"


@pytest.mark.usefixtures("blocks_off", "no_keyword_seat")
@pytest.mark.asyncio
async def test_bug_832_a_pool_the_gate_passed_is_not_below_quality(fake_embedding_client):
    # The same store with a query that clears the gate: a short window here is an
    # exhausted pool, not a refused one.
    async with _TempDB() as tmp:
        await _store_three()
        await tmp.drain()
        out = await reconstruct.do_reconstruct(AGENT, "walnut desk arrived", count=3, deep=True, trace=True)
        assert out["items"]
        assert out.get("shortfall_reason") != "below_quality_threshold"


@pytest.mark.usefixtures("blocks_off")
@pytest.mark.asyncio
async def test_bug_832_a_short_window_after_the_gate_emptied_the_pool_says_so(fake_embedding_client):
    # The keyword seat sits beside the gate, so it can still hand back the row the gate
    # refused. The window is short because the gate refused what was found.
    async with _TempDB() as tmp:
        await _store_three()
        await tmp.drain()
        out = await reconstruct.do_reconstruct(AGENT, "Monday quokka", count=3, trace=True)
        decisions = out["trace"]["recall"]["gate"]["decisions"]
        assert decisions and all(d["passed"] is False for d in decisions)
        assert out["items"] and all(i.get("admission") == "reservation" for i in out["items"]), out["items"]
        assert out["shortfall_reason"] == "below_quality_threshold"
