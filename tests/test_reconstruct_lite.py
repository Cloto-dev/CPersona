"""`lite=true` on reconstruct (2.6.5): the lite budget, the whole sequence and the lite shape.

A preset for a question one or two records can answer: the budget is LITE_BUDGET unless
the caller names one, the sequence is `whole` whatever CPERSONA_RECONSTRUCT_SEQUENCE says,
and the response leaves out the fields a reader of such an answer does not act on.
`trace=true` returns every field.
"""

import pytest

from cpersona import config, memory_handlers, reconstruct
from tests.test_reconstruct_evidence_sequence import RECORDS, SHORT, sized  # noqa: F401 (fixture)

QUERY = "filler2x7 lorem"


def _ref(n: int) -> str:
    """A synthetic record ref: no stored record stands behind it."""
    return f"mem:{n}"


async def _call(monkeypatch, *, lite, trace=False, budget=None, sequence="items"):
    monkeypatch.setattr(config, "RECONSTRUCT_SEQUENCE", sequence)
    return await reconstruct.do_reconstruct(
        "agent.lite", QUERY, count=9, deep=True, trace=trace, budget=budget, lite=lite
    )


@pytest.mark.usefixtures("blocks_off", "sized")
@pytest.mark.asyncio
async def test_lite_through_do_reconstruct(fake_embedding_client, monkeypatch):
    from tests.test_reconstruct_filled_quote import _TempDB

    async with _TempDB() as tmp:
        for text in RECORDS + SHORT:
            await memory_handlers.do_store("agent.lite", {"content": text})
        await tmp.drain()
        traced = await _call(monkeypatch, lite=True, trace=True)
        named = await _call(monkeypatch, lite=True, trace=True, budget=1000)
        lite = await _call(monkeypatch, lite=True)
        whole = await _call(monkeypatch, lite=False, trace=True, budget=reconstruct.LITE_BUDGET, sequence="whole")
        plain = await _call(monkeypatch, lite=False)

    # The preset: the whole sequence at the lite budget, though the operator's sequence is items.
    assert traced["trace"]["sequence"]["mode"] == reconstruct.SEQUENCE_WHOLE
    assert traced["effective_budget"] == reconstruct.LITE_BUDGET
    assert named["effective_budget"] == 1000, "a budget the caller names wins over the preset's"
    assert lite["items"], "the fixture must return items"
    assert [i["head_ref"] for i in lite["items"]] == [i["head_ref"] for i in whole["items"]]
    assert [i["content"] for i in lite["items"]] == [i["content"] for i in whole["items"]], (
        "lite quotes what whole quotes at the lite budget"
    )

    # The shape.
    assert lite["lite"] is True and "lite" not in plain
    assert {"effective_count", "returned_count"} <= set(lite), "the counts are always stated"
    assert lite.get("shortfall_reason") == whole.get("shortfall_reason"), "a short return still says why"
    assert not set(reconstruct._LITE_ENVELOPE) & set(lite), "the envelope a small budget makes routine is left out"
    full = {i["head_ref"]: i for i in traced["items"]}
    for item in lite["items"]:
        assert "ranges" not in item
        assert "claims" not in item, "every item of this fixture has one claim, its head"
        assert item["as_of"] == full[item["head_ref"]]["claims"][0]["as_of"], "the lone claim's as_of moves onto the item"
    assert all("ranges" in i and "claims" in i for i in traced["items"]), "trace=true returns every field"
    assert all("ranges" in i and "claims" in i for i in plain["items"]), "the default shape is unchanged"


def test_a_claim_that_says_more_keeps_the_list():
    head = {"head_ref": _ref(1), "content": "x", "ranges": [[0, 1]]}
    lone = reconstruct._lite_item({**head, "claims": [{"ref": _ref(1), "as_of": "t"}]})
    assert "claims" not in lone and lone["as_of"] == "t" and "ranges" not in lone
    for claims in (
        [{"ref": _ref(1), "as_of": "t", "why": "cluster:episode"}],
        [{"ref": _ref(1), "as_of": "t", "roles": [{"ref": _ref(2), "role": "supersedes"}]}],
        [{"ref": _ref(1), "as_of": "t"}, {"ref": _ref(2), "as_of": "u"}],
        [{"ref": _ref(2), "as_of": "t"}],
    ):
        kept = reconstruct._lite_item({**head, "claims": claims})
        assert kept["claims"] == claims and "as_of" not in kept, claims


def test_lite_leaves_the_compact_shape_alone_when_off():
    response = {
        "items": [{"head_ref": _ref(1), "content": "x", "ranges": [[0, 1]], "claims": [{"ref": _ref(1), "as_of": "t"}]}],
        "effective_count": 1,
        "returned_count": 1,
        "effective_budget": 100,
        "used_budget": 1,
        "shortfall_reason": reconstruct.SHORTFALL_BUDGET_EXHAUSTED,
        "count_policy": {"source": "caller", "clamped": False, "reason": "count_requested"},
        "requested_count": 1,
        "budget_policy": {"source": "caller", "clamped": False, "reason": "budget_requested"},
        "requested_budget": 100,
        "bounds": {"reached": ["top_k"]},
        "reconstruction": {"excluded_without_provenance": 0},
    }
    off = reconstruct._compact(dict(response))
    assert "lite" not in off and "bounds" in off and "effective_budget" in off and "ranges" in off["items"][0]
    on = reconstruct._compact(dict(response), lite=True)
    assert on["lite"] is True and "bounds" not in on and "effective_budget" not in on and "used_budget" not in on
    assert on["items"] == [{"head_ref": _ref(1), "content": "x", "as_of": "t"}]


@pytest.mark.asyncio
async def test_the_tool_forwards_lite(monkeypatch):
    from cpersona import server

    seen: dict = {}

    async def fake(agent_id, query, **kwargs):
        seen.update(kwargs)
        return {"items": [], "returned_count": 0}

    monkeypatch.setattr(server, "do_reconstruct", fake)
    await server.registry._handlers["reconstruct"]({"agent_id": "agent.lite", "query": "q", "lite": True})
    assert seen.get("lite") is True
    seen.clear()
    await server.registry._handlers["reconstruct"]({"agent_id": "agent.lite", "query": "q"})
    assert seen.get("lite") is False, "omitted, lite is off"
    schema = next(t for t in server.registry._tools if t.name == "reconstruct").inputSchema
    assert schema["properties"]["lite"]["type"] == "boolean"
