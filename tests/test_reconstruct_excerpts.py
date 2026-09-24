"""Reconstruction v1.1: the payload budget, excerpts, and quoting a long record by its node.

docs/RELIABLE_RECALL_2_6.md section 7 ("Breadth before depth", invariant 9) and
docs/OVERFLOW_TREE_DESIGN.md section 6. The allocation and the node choice are pure
and tested directly; the end-to-end tests build real nodes through the task queue with
the conftest token-report double.
"""

import json
import os
import tempfile

import numpy as np
import pytest

from cpersona import admin_handlers, config, database, memory_handlers, nodes, reconstruct, server, session, tasks
from cpersona.reconstruct import allocate, best_node, resolve_budget

AGENT = "agent.excerpts"
WINDOW = 24
# The end-to-end corpora are a handful of rows, which the quality gate would
# otherwise screen out; deep only lowers the gate on the candidate stage.


# --------------------------------------------------------------------------------------
# resolve_budget
# --------------------------------------------------------------------------------------


def test_budget_default_request_force_and_clamps(monkeypatch):
    monkeypatch.setattr(config, "RECALL_PREVIEW_CHARS", 500)
    monkeypatch.setattr(config, "RECONSTRUCT_QUOTE_CHARS", 0)  # the preview-sized head this arithmetic assumes
    monkeypatch.setattr(config, "RECONSTRUCT_DEFAULT_BUDGET", 4000)
    monkeypatch.setattr(config, "RECONSTRUCT_MAX_BUDGET", 20000)
    monkeypatch.setattr(config, "RECONSTRUCT_FORCED_BUDGET", None)

    assert resolve_budget(None) == (4000, {"source": "server_default", "clamped": False, "reason": "budget_omitted"})
    assert resolve_budget(9000) == (9000, {"source": "caller", "clamped": False, "reason": "budget_requested"})
    assert resolve_budget(99999) == (20000, {"source": "caller", "clamped": True, "reason": "budget_requested"})
    assert resolve_budget(10) == (500, {"source": "caller", "clamped": True, "reason": "raised_to_one_excerpt"})
    # The default grows to one quote per item of the window, and only the default:
    assert resolve_budget(None, 8) == (4000, {"source": "server_default", "clamped": False, "reason": "budget_omitted"})
    assert resolve_budget(None, 10) == (5000, {"source": "server_default", "clamped": False, "reason": "default_fits_the_window"})
    assert resolve_budget(4000, 10) == (4000, {"source": "caller", "clamped": False, "reason": "budget_requested"})
    monkeypatch.setattr(config, "RECONSTRUCT_MAX_BUDGET", 4500)
    assert resolve_budget(None, 10) == (4500, {"source": "server_default", "clamped": True, "reason": "default_fits_the_window"})
    monkeypatch.setattr(config, "RECONSTRUCT_MAX_BUDGET", 20000)

    monkeypatch.setattr(config, "RECONSTRUCT_FORCED_BUDGET", 1234)
    assert resolve_budget(9000) == (1234, {"source": "operator_forced", "clamped": False, "reason": "forced_budget_set"})


def test_a_configured_budget_that_cannot_hold_one_excerpt_is_a_startup_error(monkeypatch):
    monkeypatch.setattr(config, "RECALL_PREVIEW_CHARS", 500)
    monkeypatch.setattr(config, "RECONSTRUCT_DEFAULT_BUDGET", 499)
    with pytest.raises(ValueError, match="below one preview-tier excerpt"):
        config.validate_reconstruct_counts()
    monkeypatch.setattr(config, "RECONSTRUCT_DEFAULT_BUDGET", 30000)
    with pytest.raises(ValueError, match="exceeds"):
        config.validate_reconstruct_counts()


# --------------------------------------------------------------------------------------
# allocate -- invariant 9
# --------------------------------------------------------------------------------------


def _entries(spec):
    """spec: [(head_len, [excerpt_len, ...]), ...] -> allocate() input."""
    out = []
    for i, (head_len, excerpt_lens) in enumerate(spec):
        item = {"head_ref": f"mem:{i}", "claims": []}
        head = {"content": "h" * head_len}
        others = [{"ref": f"mem:{i}{j}", "content": "e" * n} for j, n in enumerate(excerpt_lens)]
        out.append((item, head, others))
    return out


def test_heads_come_before_any_excerpt():
    items, used, cut = allocate(_entries([(10, [5, 5]), (10, [5])]), budget=25)
    # sequence: h0(10) h1(10) e00(5) e10(5) e01(5) -> prefix within 25 is h0 h1 e00
    assert [i["head_ref"] for i in items] == ["mem:0", "mem:1"]
    assert [len(i.get("excerpts", [])) for i in items] == [1, 0]
    assert [i["excerpts_omitted"] for i in items] == [1, 1]
    assert "excerpts" not in items[1]  # nothing carried: absent, not []
    assert (used, cut) == (25, False)


def test_excerpts_are_taken_round_robin_in_item_order():
    items, used, _ = allocate(_entries([(1, [1, 1, 1]), (1, [1])]), budget=5)
    # h0 h1 e00 e10 e01 | e02
    assert [len(i["excerpts"]) for i in items] == [2, 1]
    assert [i.get("excerpts_omitted") for i in items] == [1, None]  # nothing withheld: absent, not 0
    assert used == 5


def test_the_response_is_a_prefix_so_a_later_smaller_excerpt_does_not_jump_the_queue():
    items, used, _ = allocate(_entries([(10, [20, 1]), (10, [])]), budget=25)
    # h0 h1 then e00 (20) does not fit; e01 (1) would, but is after it in the sequence
    assert ["excerpts" in i for i in items] == [False, False]
    assert [i.get("excerpts_omitted") for i in items] == [2, None]
    assert used == 20


def test_an_item_whose_head_does_not_fit_is_dropped_and_said():
    items, used, cut = allocate(_entries([(10, []), (10, []), (10, [])]), budget=25)
    assert len(items) == 2 and cut is True and used == 20


def test_the_first_head_is_always_admitted():
    items, used, cut = allocate(_entries([(50, [])]), budget=10)
    assert len(items) == 1 and used == 50 and cut is False


def test_raising_the_budget_alone_never_removes_an_item_or_an_excerpt():
    spec = [(7, [3, 9, 2]), (11, [4]), (5, [8, 8]), (13, [])]
    previous = None
    for budget in range(1, 120):
        items, _, _ = allocate(_entries(spec), budget)
        shape = [(i["head_ref"], [e["ref"] for e in i.get("excerpts", [])]) for i in items]
        if previous is not None:
            assert len(shape) >= len(previous)
            for (ref, excerpts), (old_ref, old_excerpts) in zip(shape, previous):
                assert ref == old_ref and excerpts[: len(old_excerpts)] == old_excerpts
        previous = shape


# --------------------------------------------------------------------------------------
# best_node
# --------------------------------------------------------------------------------------


def _blob(v):
    return np.asarray(v, dtype=np.float32).tobytes()


TEXT = "alpha beta gamma. delta epsilon zeta. unique-marker here now."
NODES = [(0, 0, 18, _blob([1, 0])), (1, 18, 38, _blob([0, 1])), (2, 38, len(TEXT), _blob([0.7, 0.7]))]


def test_without_an_embedding_the_node_with_the_literal_match_wins():
    chosen = best_node(TEXT, NODES, None, reconstruct._trigrams("unique-marker"))
    assert chosen[0] == 2


def test_with_no_literal_match_the_embedding_decides_not_node_order():
    chosen = best_node(TEXT, NODES, np.asarray([0, 1], dtype=np.float32), reconstruct._trigrams("zzzz"))
    assert chosen[0] == 1


def test_equal_lexical_counts_do_not_give_the_earlier_node_a_head_start():
    # Two nodes, no literal match, the embedding prefers the second. Ranked by position
    # instead of sharing a rank, the first node would gain a lexical rank of 0 against
    # the second's 1, tie the fused score, and win on node order.
    two = [(0, 0, 18, _blob([1, 0])), (1, 18, 38, _blob([0, 1]))]
    assert best_node(TEXT, two, np.asarray([0, 1], dtype=np.float32), set())[0] == 1


def test_a_full_tie_goes_to_the_earlier_node():
    same = [(0, 0, 18, _blob([1, 0])), (1, 18, 38, _blob([1, 0]))]
    assert best_node(TEXT, same, np.asarray([1, 0], dtype=np.float32), set())[0] == 0


# --------------------------------------------------------------------------------------
# end to end
# --------------------------------------------------------------------------------------


class _TempDB:
    async def __aenter__(self):
        session.reset_pauses_for_tests()
        self._saved = (database._db, database.DB_PATH, tasks._task_queue)
        database._db = None
        database.DB_PATH = os.path.join(tempfile.mkdtemp(), "excerpts.db")
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


@pytest.fixture
def windowed(fake_embedding_client, monkeypatch):
    # These tests pin the single-passage head quote, which 2.6 keeps behind
    # CPERSONA_RECONSTRUCT_QUOTE_CHARS=0. The filled quote that is now the default is
    # pinned in tests/test_reconstruct_filled_quote.py.
    monkeypatch.setattr(config, "RECONSTRUCT_QUOTE_CHARS", 0)
    fake_embedding_client.token_window = WINDOW
    monkeypatch.setattr(config, "RECALL_PREVIEW_CHARS", 500)
    return fake_embedding_client


# The only mention of the query term sits at the end of a long record.
LONG = " ".join(f"filler sentence {i} about routine things." for i in range(40)) + " The vault combination is 7431."


def _untimed(trace: dict) -> dict:
    """The trace without its inner recall's timings, the one part that differs run to run."""
    recall = {k: v for k, v in trace.get("recall", {}).items() if k != "timing_ms"}
    return {**trace, "recall": recall}


def _shape(result):
    return [(i["head_ref"], [c["ref"] for c in i["claims"]]) for i in result["items"]]


@pytest.mark.asyncio
async def test_a_long_record_is_quoted_from_the_node_that_matches_and_the_items_do_not_move(windowed):
    async with _TempDB() as tmp:
        stored = await memory_handlers.do_store(AGENT, {"content": LONG})
        before = await reconstruct.do_reconstruct(AGENT, "vault combination", count=3, trace=True, deep=True)
        (item,) = before["items"]
        assert "vault" not in item["content"]  # no nodes yet: quoted from the start
        assert "node" not in item
        # ...and the item says its cut quote is only the start, and why
        assert item["content_truncated"] and item["node_unavailable"] == "no_nodes"
        assert "node_order" not in before["trace"]

        await tmp.drain()
        after = await reconstruct.do_reconstruct(AGENT, "vault combination", count=3, trace=True, deep=True)

        (item,) = after["items"]
        assert "vault combination is 7431" in item["content"]
        start, end = item["node"]["span"]
        assert LONG[start:end] == item["content"]
        assert item["node"]["of"] > 1 and item["node"]["index"] == item["node"]["of"] - 1
        # A quote that carries its whole node has nothing more to hand over.
        assert "content_truncated" not in item and "expand" not in item
        # tree invariant 1 for this tool: nodes change the quote, never the items
        assert _shape(after) == _shape(before)
        assert "node_unavailable" not in item and "quote_selection" not in after
        # The trace lists the best few nodes of the quoted record, by index, best first:
        # the runner-up is a place to read next. It adds nothing else to the trace.
        order = after["trace"].pop("node_order")
        assert list(order) == [item["head_ref"]]
        assert order[item["head_ref"]][0] == item["node"]["index"]
        assert len(order[item["head_ref"]]) == reconstruct.TRACE_NODE_ORDER < item["node"]["of"]
        assert len(set(order[item["head_ref"]])) == reconstruct.TRACE_NODE_ORDER
        assert _untimed(after["trace"]) == _untimed(before["trace"])
        plain = await reconstruct.do_reconstruct(AGENT, "vault combination", count=3, deep=True)
        assert "trace" not in plain and "node_order" not in json.dumps(plain)
        assert stored["id"] == int(item["head_ref"].split(":")[1])


@pytest.mark.asyncio
async def test_nodes_from_another_model_are_not_quoted(windowed, monkeypatch):
    async with _TempDB() as tmp:
        await memory_handlers.do_store(AGENT, {"content": LONG})
        await tmp.drain()
        monkeypatch.setattr(config, "EMBEDDING_MODEL", "a-newer-model")
        (item,) = (await reconstruct.do_reconstruct(AGENT, "vault combination", deep=True))["items"]
        assert "node" not in item and "vault" not in item["content"]
        assert item["node_unavailable"] == "not_current"


@pytest.mark.asyncio
async def test_an_incomplete_node_set_is_not_quoted(windowed):
    async with _TempDB() as tmp:
        stored = await memory_handlers.do_store(AGENT, {"content": LONG})
        await tmp.drain()
        db = await database.get_db()
        await db.execute(
            "DELETE FROM record_nodes WHERE parent_id = ? AND node_index = "
            "(SELECT MAX(node_index) FROM record_nodes WHERE parent_id = ?)",
            (stored["id"], stored["id"]),
        )
        await db.commit()
        (item,) = (await reconstruct.do_reconstruct(AGENT, "vault combination", deep=True))["items"]
        assert "node" not in item
        assert item["node_unavailable"] == "not_current"


@pytest.mark.asyncio
async def test_a_record_quoted_whole_says_nothing_about_nodes(windowed):
    async with _TempDB():
        await memory_handlers.do_store(AGENT, {"content": "the vault combination is 7431"})
        result = await reconstruct.do_reconstruct(AGENT, "vault combination", deep=True)
        (item,) = result["items"]
        assert "content_truncated" not in item and "node_unavailable" not in item and "expand" not in item
        assert "quote_selection" not in result


@pytest.mark.asyncio
async def test_nodes_ranked_without_a_query_embedding_say_so(windowed, monkeypatch):
    """The embedding server going away must not silently turn node choice into trigram matching."""
    async with _TempDB() as tmp:
        await memory_handlers.do_store(AGENT, {"content": LONG})
        await tmp.drain()

        async def down(texts):
            return []  # what the real client answers when the server is unreachable

        monkeypatch.setattr(windowed, "embed", down)
        result = await reconstruct.do_reconstruct(AGENT, "vault combination", deep=True)
        (item,) = result["items"]
        assert result["quote_selection"] == "lexical_only"
        assert "vault combination is 7431" in item["content"] and "node_unavailable" not in item


@pytest.mark.asyncio
async def test_no_query_embedding_is_not_reported_when_no_node_was_ranked(windowed, monkeypatch):
    async with _TempDB():
        await memory_handlers.do_store(AGENT, {"content": LONG})  # never drained: no nodes

        async def down(texts):
            return []  # what the real client answers when the server is unreachable

        monkeypatch.setattr(windowed, "embed", down)
        result = await reconstruct.do_reconstruct(AGENT, "vault combination", deep=True)
        assert result["returned_count"] == 1 and "quote_selection" not in result


async def _burst(texts, source_id="u1"):
    for i, text in enumerate(texts):
        await memory_handlers.do_store(
            AGENT,
            {
                "content": text,
                "source": {"type": "User", "id": source_id, "name": "u"},
                "timestamp": f"2026-09-17T10:00:{i:02d}+00:00",
            },
        )


@pytest.mark.asyncio
async def test_other_claims_come_back_as_excerpts_and_the_budget_omits_them_before_items(windowed, monkeypatch):
    async with _TempDB():
        await _burst(["deploy window moved to friday", "deploy needs the rollback plan", "deploy owner is kim"])
        await _burst(["deploy dashboard link lives in the wiki"], source_id="u2")

        wide = await reconstruct.do_reconstruct(AGENT, "deploy", count=5, budget=20000, trace=True, deep=True)
        assert wide["returned_count"] == 2
        burst = next(i for i in wide["items"] if len(i["claims"]) == 3)
        assert len(burst["excerpts"]) == 2 and "excerpts_omitted" not in burst
        assert {e["ref"] for e in burst["excerpts"]} == {c["ref"] for c in burst["claims"]} - {burst["head_ref"]}
        assert wide["used_budget"] == sum(
            len(i["content"]) + sum(len(e["content"]) for e in i.get("excerpts", [])) for i in wide["items"]
        )

        # Quotes cut to 20 characters and a budget of 50: both heads (40) fit, the
        # excerpts (20 each) do not, so depth is cut and breadth is not.
        monkeypatch.setattr(config, "RECALL_PREVIEW_CHARS", 20)
        tight = await reconstruct.do_reconstruct(AGENT, "deploy", count=5, budget=50, trace=True, deep=True)
        assert _shape(tight) == _shape(wide)
        assert _untimed(tight["trace"]) == _untimed(wide["trace"])  # the budget never moves the pool or the clusters
        assert tight["used_budget"] == 40 <= tight["effective_budget"] == 50
        tight_burst = next(i for i in tight["items"] if len(i["claims"]) == 3)
        assert "excerpts" not in tight_burst and tight_burst["excerpts_omitted"] == 2
        # The budget withheld text, so the compact response keeps the figures that say so;
        # a response the budget never touched does not carry them.
        plain = await reconstruct.do_reconstruct(AGENT, "deploy", count=5, budget=50, deep=True)
        assert (plain["effective_budget"], plain["used_budget"]) == (50, 40)
        assert plain["shortfall_reason"] != "budget_exhausted"  # excerpts were withheld, no item was
        monkeypatch.setattr(config, "RECALL_PREVIEW_CHARS", 500)
        roomy = await reconstruct.do_reconstruct(AGENT, "deploy", count=5, budget=20000, deep=True)
        assert "effective_budget" not in roomy and "used_budget" not in roomy
        assert "shortfall_reason" not in tight or tight["shortfall_reason"] != "budget_exhausted"


@pytest.mark.asyncio
async def test_a_head_the_budget_cannot_carry_is_dropped_with_budget_exhausted(windowed, monkeypatch):
    monkeypatch.setattr(config, "RECALL_PREVIEW_CHARS", 40)
    async with _TempDB():
        for i in range(4):
            await memory_handlers.do_store(AGENT, {"content": f"deploy note number {i} " + "x" * 60})
        result = await reconstruct.do_reconstruct(AGENT, "deploy note", count=4, budget=100, deep=True)
        assert result["returned_count"] == 2
        assert result["shortfall_reason"] == "budget_exhausted"
        assert result["used_budget"] == 80


@pytest.mark.asyncio
async def test_the_mcp_boundary_forwards_the_budget(windowed):
    async with _TempDB():
        await memory_handlers.do_store(AGENT, {"content": "deploy on friday"})
        result = await server.do_reconstruct_boundary(
            AGENT, "deploy", None, None, None, None, False, "", None, "", trace=True, budget=777
        )
        assert result["requested_budget"] == 777 and result["effective_budget"] == 777
        assert result["budget_policy"]["source"] == "caller"


@pytest.mark.asyncio
async def test_a_one_row_item_carries_its_row_once(windowed):
    # Item metadata is compressed: the ref, time and reason of a row appear in its
    # claim and nowhere else, and fields with nothing to say are absent.
    async with _TempDB():
        stored = await memory_handlers.do_store(
            AGENT, {"content": "deploy on friday", "timestamp": "2026-09-17T10:00:00+00:00"}
        )
        (item,) = (await reconstruct.do_reconstruct(AGENT, "deploy", count=3, deep=True))["items"]
        ref = f"mem:{stored['id']}"
        assert item == {
            "content": "deploy on friday",
            "head_ref": ref,
            "claims": [{"ref": ref, "as_of": "2026-09-17T10:00:00+00:00", "why": "seed"}],
            "independence_reason": "singleton",
        }


@pytest.mark.asyncio
async def test_a_cut_node_quote_hands_over_the_argument_that_reads_the_rest_of_its_node(windowed, monkeypatch):
    # The node that matches is several times the preview width, so its quote is cut
    # and the answer lies beyond the cut -- the case a reader used to meet by
    # expanding the whole record.
    monkeypatch.setattr(config, "RECALL_PREVIEW_CHARS", 10)
    async with _TempDB() as tmp:
        await memory_handlers.do_store(AGENT, {"content": LONG})
        await tmp.drain()
        (item,) = (await reconstruct.do_reconstruct(AGENT, "vault combination", deep=True))["items"]
        assert item["content_truncated"] and "7431" not in item["content"]
        assert item["expand"] == {"ref": item["head_ref"], "node": item["node"]["index"]}
        (more,) = (await memory_handlers.do_get_contents(AGENT, [item["expand"]]))["items"]
        assert more["content"].startswith(item["content"]) and "7431" in more["content"]
        assert len(more["content"]) < len(LONG) / 2


@pytest.mark.asyncio
async def test_a_count_the_caller_named_is_not_cut_by_a_budget_it_never_set(windowed):
    # Ten one-row items whose quotes are each cut at the preview width cost 10 x 500
    # characters of heads. The configured default carries eight.
    async with _TempDB():
        for i in range(10):
            await memory_handlers.do_store(AGENT, {"content": f"deploy note {i:02d} " + "x" * 600})
        asked = await reconstruct.do_reconstruct(AGENT, "deploy note", count=10, top_k=20, deep=True)
        assert asked["returned_count"] == asked["effective_count"] == 10 and "shortfall_reason" not in asked
        # ...while a budget the caller does name is taken as given, and says what it cost.
        named = await reconstruct.do_reconstruct(AGENT, "deploy note", count=10, top_k=20, budget=4000, deep=True)
        assert named["returned_count"] == 8 and named["shortfall_reason"] == "budget_exhausted"
        assert [i["head_ref"] for i in named["items"]] == [i["head_ref"] for i in asked["items"]][:8]
