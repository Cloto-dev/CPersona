"""`mode` on reconstruct (2.6.8): a cap on the tokens of the returned JSON.

`mode: "lite"` is `lite: true`; `mode: "pro"` widens the window and quotes as much of the
whole sequence as its cap holds. The response is the longest prefix of the payload
sequence whose serialized form fits the cap, so a larger cap never removes an item or an
excerpt, and `used_tokens` is the count of the JSON the caller receives.
"""

import hashlib
import json

import pytest

from cpersona import config, memory_handlers, reconstruct, server, tokens
from tests.test_reconstruct_evidence_sequence import RECORDS, SHORT, sized  # noqa: F401 (fixture)

AGENT = "agent.modes"
QUERY = "filler2x7 lorem"


async def _store_corpus(tmp):
    for text in RECORDS + SHORT:
        await memory_handlers.do_store(AGENT, {"content": text})
    await tmp.drain()


def _taken(result: dict) -> list[tuple[str, tuple[int, int]]]:
    return [(t["ref"], tuple(t["span"])) for t in result["trace"]["sequence"]["taken"]]


# --- counting ---------------------------------------------------------------------------

# Counts tiktoken 0.14.0's own cl100k_base gives these strings (2026-10-09).
REFERENCE_COUNTS = [
    ("hello world", 2),
    ("日本語の記録と英語の text を混ぜる。", 21),
    ('{"items": [{"content": "x", "as_of": "2026-10-09T11:42:55.938115+00:00"}]}', 35),
    ("<|endoftext|> special", 8),
    ("   trailing  \n\n", 3),
    ("def f(x):\n    return x**2  # code", 12),
    ("ℝ∑ emoji 😀 and ID item:4821 node:77", 15),
    # Digits split in threes before the merges: a pattern that kept them whole counts 9 and 9.
    ("used_tokens: 123456789012", 8),
    ("20261009114255938115", 7),
]


@pytest.mark.parametrize(("text", "expected"), REFERENCE_COUNTS)
def test_the_shipped_vocabulary_counts_as_tiktoken_does(text, expected):
    assert tokens.count(text) == expected


def test_the_vocabulary_ships_and_is_the_one_the_count_is_defined_by():
    assert hashlib.sha256(tokens.VOCAB_PATH.read_bytes()).hexdigest() == tokens.VOCAB_SHA256
    assert (tokens.VOCAB_PATH.parent / "LICENSE").read_text().count("MIT License") == 1


def test_a_vocabulary_that_does_not_match_its_digest_is_refused(tmp_path, monkeypatch):
    altered = tmp_path / "cl100k_base.tiktoken"
    altered.write_bytes(tokens.VOCAB_PATH.read_bytes().replace(b"IQ== 0", b"IQ== 9", 1))
    monkeypatch.setattr(tokens, "VOCAB_PATH", altered)
    monkeypatch.setattr(tokens, "_encoding", None)
    with pytest.raises(tokens.VocabularyError):
        tokens.count("x")


@pytest.mark.parametrize("pad", range(990, 1006))
def test_used_tokens_counts_its_own_digits(pad):
    # Around 1,000 a count's own digits go from one token to two.
    out = {"items": [], "content": "a " * pad}
    n = reconstruct._settle_tokens(out)
    assert n == out["used_tokens"] == tokens.count_json(out)


# --- the arguments ----------------------------------------------------------------------


@pytest.mark.usefixtures("blocks_off", "sized")
@pytest.mark.asyncio
async def test_lite_true_is_mode_lite_and_both_together_are_refused(fake_embedding_client, monkeypatch):
    from tests.test_reconstruct_filled_quote import _TempDB

    async with _TempDB() as tmp:
        await _store_corpus(tmp)
        alias = await reconstruct.do_reconstruct(AGENT, QUERY, count=9, deep=True, lite=True)
        named = await reconstruct.do_reconstruct(AGENT, QUERY, count=9, deep=True, mode="lite")
        both = await reconstruct.do_reconstruct(AGENT, QUERY, deep=True, lite=True, mode="lite")
        unknown = await reconstruct.do_reconstruct(AGENT, QUERY, deep=True, mode="max")
        plain = await reconstruct.do_reconstruct(AGENT, QUERY, count=9, deep=True)
    assert alias == named and named["mode"] == "lite" and named["items"]
    assert named["cap"] == reconstruct.MODE_CAPS["lite"] and named["used_tokens"] <= named["cap"]
    assert both["ok"] is False and both["items"] == [] and unknown["ok"] is False
    assert not {"mode", "cap", "used_tokens", "lite"} & set(plain), "a call that names no mode is unchanged"


@pytest.mark.usefixtures("blocks_off", "sized")
@pytest.mark.asyncio
async def test_pro_widens_the_window_past_the_configured_maximum(fake_embedding_client, monkeypatch):
    from tests.test_reconstruct_filled_quote import _TempDB

    monkeypatch.setattr(config, "RECONSTRUCT_MAX_COUNT", 10)
    notes = [f"lorem note {n}: the plan for item {n} is short." for n in range(17)]
    async with _TempDB() as tmp:
        for text in notes:
            await memory_handlers.do_store(AGENT, {"content": text})
        await tmp.drain()
        pro = await reconstruct.do_reconstruct(AGENT, "lorem note plan", deep=True, mode="pro", trace=True)
        asked = await reconstruct.do_reconstruct(AGENT, "lorem note plan", count=12, deep=True, mode="pro", trace=True)
        lite = await reconstruct.do_reconstruct(AGENT, "lorem note plan", deep=True, mode="lite", trace=True)
    assert pro["effective_count"] == reconstruct.PRO_COUNT == 15
    assert pro["returned_count"] == 15, "every note fits the cap, so the window is full"
    assert asked["effective_count"] == 12 and lite["effective_count"] == 10


@pytest.mark.usefixtures("blocks_off", "sized")
@pytest.mark.asyncio
async def test_only_the_first_items_of_pro_join_the_floor_of_whole_records(fake_embedding_client, monkeypatch):
    from tests.test_reconstruct_filled_quote import _TempDB

    seen = []
    real = reconstruct.whole_order

    def spy(records, whole):
        seen.append(list(whole))
        return real(records, whole)

    monkeypatch.setattr(reconstruct, "whole_order", spy)
    monkeypatch.setattr(reconstruct, "PRO_WHOLE_FLOOR_ITEMS", 2)
    async with _TempDB() as tmp:
        await _store_corpus(tmp)
        lite = await reconstruct.do_reconstruct(AGENT, QUERY, count=9, deep=True, mode="lite", trace=True)
        pro = await reconstruct.do_reconstruct(AGENT, QUERY, count=9, deep=True, mode="pro", trace=True)
    lite_flags, pro_flags = seen
    shorts = [i for i, flag in enumerate(lite_flags) if flag]
    assert any(i >= 2 for i in shorts), "the fixture must place a short record after the first two items"
    assert pro_flags == [flag and i < 2 for i, flag in enumerate(lite_flags)]
    assert lite["items"] and pro["items"]


# --- the cap ----------------------------------------------------------------------------


@pytest.mark.usefixtures("blocks_off", "sized")
@pytest.mark.asyncio
async def test_the_cap_holds_counts_itself_and_returns_the_longest_prefix(fake_embedding_client, monkeypatch):
    from tests.test_reconstruct_filled_quote import _TempDB

    caps = list(range(500, 4200, 150))
    plain, traced = {}, {}
    async with _TempDB() as tmp:
        await _store_corpus(tmp)
        for cap in caps:
            monkeypatch.setattr(reconstruct, "MODE_CAPS", {"lite": cap, "pro": cap})
            plain[cap] = await reconstruct.do_reconstruct(AGENT, QUERY, count=9, deep=True, mode="pro")
            traced[cap] = await reconstruct.do_reconstruct(AGENT, QUERY, count=9, deep=True, mode="pro", trace=True)
    sizes = set()
    for cap in caps:
        out = plain[cap]
        assert out["cap"] == cap and out["used_tokens"] <= cap, f"cap {cap}: {out.get('used_tokens')} tokens"
        assert out["used_tokens"] == tokens.count(json.dumps(out, ensure_ascii=False))
        assert traced[cap]["used_tokens"] == out["used_tokens"], "the trace does not change what is returned"
        assert [i["content"] for i in traced[cap]["items"]] == [i["content"] for i in out["items"]]
        sizes.add(len(_taken(traced[cap])))
    assert len(sizes) > 5, "the caps must cut the sequence at many places"
    for small, large in zip(caps, caps[1:]):
        a, b = _taken(traced[small]), _taken(traced[large])
        assert b[: len(a)] == a, f"raising the cap from {small} to {large} removed a passage"
        assert {i["head_ref"] for i in plain[small]["items"]} <= {i["head_ref"] for i in plain[large]["items"]}
    for small in caps:
        for large in caps:
            if large > small and plain[large]["used_tokens"] <= small:
                # A response that fits the smaller cap is what the smaller cap returns.
                assert _taken(traced[small]) == _taken(traced[large]), f"cap {small} returned a shorter prefix"


@pytest.mark.usefixtures("blocks_off", "sized")
@pytest.mark.asyncio
async def test_one_token_under_the_whole_response_drops_exactly_one_step(fake_embedding_client, monkeypatch):
    from tests.test_reconstruct_filled_quote import _TempDB

    # Caps of four digits throughout, so the `cap` field costs the same tokens in every call.
    async with _TempDB() as tmp:
        await _store_corpus(tmp)
        monkeypatch.setattr(reconstruct, "MODE_CAPS", {"lite": 9999, "pro": 9999})
        whole = await reconstruct.do_reconstruct(AGENT, QUERY, count=4, deep=True, mode="pro", trace=True)
        assert len(_taken(whole)) == whole["trace"]["sequence"]["candidates"], "the fixture must fit whole"
        top = whole["used_tokens"]
        monkeypatch.setattr(reconstruct, "MODE_CAPS", {"lite": top - 1, "pro": top - 1})
        under = await reconstruct.do_reconstruct(AGENT, QUERY, count=4, deep=True, mode="pro", trace=True)
    assert under["used_tokens"] <= top - 1
    assert _taken(under) == _taken(whole)[:-1], "the longest prefix under the whole response is one step shorter"


@pytest.mark.usefixtures("blocks_off", "sized")
@pytest.mark.asyncio
async def test_a_cap_below_the_first_passage_is_an_error(fake_embedding_client, monkeypatch):
    from tests.test_reconstruct_filled_quote import _TempDB

    monkeypatch.setattr(reconstruct, "MODE_CAPS", {"lite": 40, "pro": 40})
    async with _TempDB() as tmp:
        await _store_corpus(tmp)
        out = await reconstruct.do_reconstruct(AGENT, QUERY, count=9, deep=True, mode="lite")
    assert out["ok"] is False and out["error"] == reconstruct.CAP_BELOW_MINIMUM
    assert out["items"] == [] and out["cap"] == 40 and out["mode"] == "lite"


@pytest.mark.usefixtures("blocks_off", "sized")
@pytest.mark.asyncio
async def test_the_cap_counts_what_the_tool_boundary_adds(fake_embedding_client, monkeypatch):
    from tests.test_reconstruct_filled_quote import _TempDB

    async with _TempDB() as tmp:
        await _store_corpus(tmp)
        out = await server.registry._handlers["reconstruct"](
            {"agent_id": AGENT, "query": QUERY, "deep": True, "mode": "lite", "project_id": "@auto"}
        )
    assert "resolved_project_id" in out and out["items"]
    assert out["used_tokens"] == tokens.count(json.dumps(out, ensure_ascii=False))


# --- excerpts follow every passage (bug fixed in 2.6.8) ----------------------------------

HEAD = " ".join(f"lorem sentence {n} of the long note says something about the plan." for n in range(40))


async def _burst(texts, source_id, minute):
    for i, text in enumerate(texts):
        await memory_handlers.do_store(
            AGENT,
            {
                "content": text,
                "source": {"type": "User", "id": source_id, "name": "u"},
                "timestamp": f"2026-09-17T10:{minute:02d}:{i:02d}+00:00",
            },
        )


@pytest.mark.usefixtures("blocks_off", "sized")
@pytest.mark.asyncio
async def test_raising_the_budget_never_takes_an_excerpt_away(fake_embedding_client, monkeypatch):
    from tests.test_reconstruct_filled_quote import _TempDB

    monkeypatch.setattr(config, "RECONSTRUCT_SEQUENCE", "whole")
    # Short excerpts, so that what the passages leave of a budget often holds one.
    monkeypatch.setattr(config, "RECALL_PREVIEW_CHARS", 60)
    budgets = list(range(300, 9000, 61))
    async with _TempDB() as tmp:
        await _burst([HEAD, "lorem: the plan moved to friday", "lorem: the owner of the plan is kim"], "u1", 0)
        await _burst([RECORDS[0], "lorem: a second plan note"], "u2", 10)
        await tmp.drain()
        runs = {
            b: await reconstruct.do_reconstruct(AGENT, "lorem plan", count=5, budget=b, deep=True, trace=True)
            for b in budgets
        }
    clustered = [i for i in runs[budgets[-1]]["items"] if len(i["claims"]) > 1]
    assert clustered, "the fixture must bundle records into an item with other claims"
    assert any(i.get("excerpts") for out in runs.values() for i in out["items"]), "an excerpt must be carried somewhere"
    previous: dict[str, int] = {}
    for b in budgets:
        out = runs[b]
        carried = {i["head_ref"]: len(i.get("excerpts", [])) for i in out["items"]}
        for ref, n in previous.items():
            assert ref in carried, f"raising the budget to {b} removed {ref}"
            assert carried[ref] >= n, f"raising the budget to {b} took an excerpt of {ref} away"
        if any(carried.values()):
            assert len(_taken(out)) == out["trace"]["sequence"]["candidates"], (
                f"budget {b}: an excerpt was carried while a passage was left out"
            )
        previous = carried


# --- the tool ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_tool_forwards_mode(monkeypatch):
    seen = {}

    async def fake(agent_id, query, **kwargs):
        seen.update(kwargs)
        return {"items": [], "returned_count": 0}

    monkeypatch.setattr(server, "do_reconstruct", fake)
    await server.registry._handlers["reconstruct"]({"agent_id": AGENT, "query": "q", "mode": "pro"})
    assert seen.get("mode") == "pro" and seen.get("lite") is False and seen.get("envelope") == {}
    await server.registry._handlers["reconstruct"]({"agent_id": AGENT, "query": "q"})
    assert seen.get("mode") is None
    schema = next(t for t in server.registry._tools if t.name == "reconstruct").inputSchema
    assert schema["properties"]["mode"]["enum"] == ["lite", "pro"]
