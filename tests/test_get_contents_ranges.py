"""Reconstruction v1.1: get_contents expands part of a record -- a node range or a character span.

docs/OVERFLOW_TREE_DESIGN.md section 6 ("a caller can fetch one node rather than the
whole record") and docs/RELIABLE_RECALL_2_6.md section 7. Nodes are built through the
task queue with the conftest token-report double, as in test_reconstruct_excerpts.
"""

import pytest

from cpersona import config, database, memory_handlers, reconstruct, server
from cpersona.memory_handlers import _parse_ref_entry, do_get_contents
from tests.test_reconstruct_excerpts import LONG, WINDOW, _TempDB

AGENT = "agent.ranges"


@pytest.fixture
def windowed(fake_embedding_client, monkeypatch):
    fake_embedding_client.token_window = WINDOW
    monkeypatch.setattr(config, "RECALL_PREVIEW_CHARS", 500)
    return fake_embedding_client


async def _node_rows(parent_kind, parent_id):
    db = await database.get_db()
    return await db.execute_fetchall(
        "SELECT node_index, start_char, end_char FROM record_nodes "
        "WHERE parent_kind = ? AND parent_id = ? ORDER BY node_index",
        (parent_kind, parent_id),
    )


async def _long_memory(tmp, agent=AGENT, text=LONG):
    stored = await memory_handlers.do_store(agent, {"content": text})
    await tmp.drain()
    rows = await _node_rows("mem", stored["id"])
    assert len(rows) >= 4, "the fixture must divide into several nodes"
    return f"mem:{stored['id']}", rows


# --------------------------------------------------------------------------------------
# parsing -- shape only
# --------------------------------------------------------------------------------------


# The parser never reads the id; any well-formed ref will do.
REF = f"mem:{7}"


@pytest.mark.parametrize(
    "entry, expected",
    [
        (REF, (REF, None, None)),
        ({"ref": REF}, (REF, None, None)),
        ({"ref": REF, "node": 2}, (REF, {"node": (2, 2)}, None)),
        ({"ref": REF, "node": [2, 4]}, (REF, {"node": (2, 4)}, None)),
        ({"ref": REF, "span": [0, 1]}, (REF, {"span": (0, 1)}, None)),
        ({"ref": REF, "node": 1, "span": [0, 5]}, (REF, None, "invalid_range")),
        ({"ref": REF, "node": [3, 2]}, (REF, None, "invalid_range")),
        ({"ref": REF, "node": -1}, (REF, None, "invalid_range")),
        ({"ref": REF, "node": True}, (REF, None, "invalid_range")),
        ({"ref": REF, "node": [1]}, (REF, None, "invalid_range")),
        ({"ref": REF, "node": "2"}, (REF, None, "invalid_range")),
        ({"ref": REF, "span": [5, 5]}, (REF, None, "invalid_range")),
        ({"ref": REF, "span": [-1, 5]}, (REF, None, "invalid_range")),
        ({"ref": REF, "span": [0, 2.5]}, (REF, None, "invalid_range")),
    ],
)
def test_ref_entries_parse_to_a_ref_and_a_range(entry, expected):
    assert _parse_ref_entry(entry) == expected


# --------------------------------------------------------------------------------------
# end to end
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_node_and_a_node_range_return_exactly_their_characters(windowed):
    async with _TempDB() as tmp:
        ref, rows = await _long_memory(tmp)
        out = await do_get_contents(AGENT, [{"ref": ref, "node": 1}, {"ref": ref, "node": [1, 3]}])
        assert out["missing"] == [] and "unresolved" not in out
        one, three = out["items"]

        assert one["content"] == LONG[rows[1][1] : rows[1][2]]
        assert one["range"] == {"span": [rows[1][1], rows[1][2]], "node": [1, 1], "of": len(rows), "content_len": len(LONG)}
        # inclusive on both ends: node 3's last character is served, node 4's first is not
        assert three["content"] == LONG[rows[1][1] : rows[3][2]]
        assert three["range"]["span"] == [rows[1][1], rows[3][2]] and three["range"]["node"] == [1, 3]
        # the rest of the row's metadata travels with a slice as with the whole row
        whole = (await do_get_contents(AGENT, [ref]))["items"][0]
        assert {k: v for k, v in one.items() if k not in ("content", "range")} == {
            k: v for k, v in whole.items() if k != "content"
        }
        assert "range" not in whole and whole["content"] == LONG


@pytest.mark.asyncio
async def test_the_last_node_is_served_and_one_past_it_is_refused(windowed):
    async with _TempDB() as tmp:
        ref, rows = await _long_memory(tmp)
        last = len(rows) - 1
        out = await do_get_contents(AGENT, [{"ref": ref, "node": last}, {"ref": ref, "node": [last, last + 1]}])
        assert [i["content"] for i in out["items"]] == [LONG[rows[last][1] :]]
        assert out["unresolved"] == [{"ref": ref, "reason": "node_out_of_range"}]
        far = await do_get_contents(AGENT, [{"ref": ref, "node": last + 7}])
        assert far["items"] == [] and far["unresolved"] == [{"ref": ref, "reason": "node_out_of_range"}]


@pytest.mark.asyncio
async def test_a_refused_range_does_not_stop_the_rest_of_the_batch(windowed):
    async with _TempDB() as tmp:
        ref, rows = await _long_memory(tmp)
        out = await do_get_contents(
            AGENT, [{"ref": ref, "node": [2, 1]}, {"ref": ref, "node": 99}, {"ref": ref, "node": 0}, ref]
        )
        assert [i.get("range", {}).get("node") for i in out["items"]] == [[0, 0], None]
        assert [u["reason"] for u in out["unresolved"]] == ["invalid_range", "node_out_of_range"]


@pytest.mark.asyncio
async def test_an_object_without_a_string_ref_is_missing_not_an_error(windowed):
    async with _TempDB():
        out = await do_get_contents(AGENT, [{"ref": 5, "node": 0}, {"node": 0}, {"ref": None}])
        assert out["items"] == [] and "unresolved" not in out
        assert out["missing"] == ["5", "None", "None"]


@pytest.mark.asyncio
async def test_a_span_is_served_as_given_and_its_end_is_clamped_to_the_text(windowed):
    async with _TempDB():
        stored = await memory_handlers.do_store(AGENT, {"content": "0123456789"})
        ref = f"mem:{stored['id']}"
        out = await do_get_contents(
            AGENT,
            [{"ref": ref, "span": [2, 5]}, {"ref": ref, "span": [7, 99]}, {"ref": ref, "span": [10, 12]}],
        )
        assert [i["content"] for i in out["items"]] == ["234", "789"]
        assert [i["range"] for i in out["items"]] == [
            {"span": [2, 5], "content_len": 10},
            {"span": [7, 10], "content_len": 10},
        ]
        assert out["unresolved"] == [{"ref": ref, "reason": "span_out_of_range"}]


@pytest.mark.asyncio
async def test_a_node_range_on_a_record_without_a_complete_node_set_is_refused_not_widened(windowed):
    async with _TempDB() as tmp:
        short = await memory_handlers.do_store(AGENT, {"content": "fits in one window"})
        ref, rows = await _long_memory(tmp)
        db = await database.get_db()
        await db.execute(
            "DELETE FROM record_nodes WHERE parent_kind = 'mem' AND parent_id = ? AND node_index = ?",
            (int(ref.split(":")[1]), len(rows) - 1),
        )
        await db.commit()

        out = await do_get_contents(AGENT, [{"ref": f"mem:{short['id']}", "node": 0}, {"ref": ref, "node": 0}])
        assert out["items"] == []
        assert out["unresolved"] == [
            {"ref": f"mem:{short['id']}", "reason": "no_current_nodes"},
            {"ref": ref, "reason": "no_current_nodes"},
        ]


@pytest.mark.asyncio
async def test_a_missing_middle_node_is_not_a_partition(windowed):
    async with _TempDB() as tmp:
        ref, rows = await _long_memory(tmp)
        db = await database.get_db()
        await db.execute(
            "DELETE FROM record_nodes WHERE parent_kind = 'mem' AND parent_id = ? AND node_index = 1",
            (int(ref.split(":")[1]),),
        )
        await db.commit()
        out = await do_get_contents(AGENT, [{"ref": ref, "node": 0}])
        assert out["unresolved"] == [{"ref": ref, "reason": "no_current_nodes"}]


@pytest.mark.asyncio
async def test_nodes_that_overlap_or_leave_a_gap_are_not_a_partition(windowed):
    # Numbering, first start and last end all still look right; only the seam
    # between node 1 and node 2 is wrong.
    async with _TempDB() as tmp:
        ref, rows = await _long_memory(tmp)
        db = await database.get_db()
        await db.execute(
            "UPDATE record_nodes SET start_char = start_char + 1 "
            "WHERE parent_kind = 'mem' AND parent_id = ? AND node_index = 2",
            (int(ref.split(":")[1]),),
        )
        await db.commit()
        out = await do_get_contents(AGENT, [{"ref": ref, "node": 0}])
        assert out["unresolved"] == [{"ref": ref, "reason": "no_current_nodes"}]


@pytest.mark.asyncio
async def test_node_numbers_that_do_not_count_from_zero_are_not_a_partition(windowed):
    # Offsets still tile the text, but position i no longer holds node i, so
    # serving by position would answer a request for node 0 with node 1's text.
    async with _TempDB() as tmp:
        ref, rows = await _long_memory(tmp)
        db = await database.get_db()
        await db.execute(
            "UPDATE record_nodes SET node_index = node_index + 100 WHERE parent_kind = 'mem' AND parent_id = ?",
            (int(ref.split(":")[1]),),
        )
        await db.commit()
        out = await do_get_contents(AGENT, [{"ref": ref, "node": 0}])
        assert out["unresolved"] == [{"ref": ref, "reason": "no_current_nodes"}]


@pytest.mark.asyncio
async def test_nodes_from_another_model_still_expand_because_offsets_depend_on_the_text(windowed, monkeypatch):
    async with _TempDB() as tmp:
        ref, rows = await _long_memory(tmp)
        monkeypatch.setattr(config, "EMBEDDING_MODEL", "a-newer-model")
        (item,) = (await do_get_contents(AGENT, [{"ref": ref, "node": 0}]))["items"]
        assert item["content"] == LONG[: rows[0][2]]


@pytest.mark.asyncio
async def test_a_range_on_another_agents_row_is_missing_and_says_nothing_about_its_nodes(windowed):
    async with _TempDB() as tmp:
        ref, _ = await _long_memory(tmp, agent="agent.owner")
        out = await do_get_contents(
            AGENT, [{"ref": ref, "node": 0}, {"ref": ref, "node": 999}, {"ref": ref, "node": 1, "span": [0, 1]}]
        )
        assert out["items"] == [] and "unresolved" not in out
        assert out["missing"] == [ref, ref, ref]


@pytest.mark.asyncio
async def test_an_episode_range_is_measured_in_its_summary(windowed):
    async with _TempDB() as tmp:
        stored = await memory_handlers.do_archive_episode(AGENT, [], summary=LONG, keywords="vault")
        await tmp.drain()
        ep_id = stored["episode_id"]
        rows = await _node_rows("ep", ep_id)
        assert len(rows) >= 2
        ref = f"ep:{ep_id}"
        out = await do_get_contents(AGENT, [{"ref": ref, "node": len(rows) - 1}, {"ref": ref, "span": [0, 6]}])
        node_item, span_item = out["items"]
        assert node_item["content"] == LONG[rows[-1][1] :]
        assert span_item["content"] == LONG[:6]  # no "[Episode] " label inside the offsets
        assert node_item["resolved"] is not None and node_item["source"] == {"System": "episode"}


@pytest.mark.asyncio
async def test_only_the_slice_counts_against_the_budget_and_deferred_echoes_the_range(windowed, monkeypatch):
    monkeypatch.setattr(memory_handlers, "GET_CONTENTS_MAX_CHARS", 900)
    async with _TempDB() as tmp:
        ref, rows = await _long_memory(tmp)
        assert len(LONG) > 900
        # Three whole rows would defer after the first; three one-node slices fit.
        slices = [{"ref": ref, "node": i} for i in range(3)]
        out = await do_get_contents(AGENT, slices)
        assert out["count"] == 3 and "deferred" not in out

        out = await do_get_contents(AGENT, [ref, {"ref": ref, "node": 0}, ref])
        assert out["count"] == 1
        assert out["deferred"] == [{"ref": ref, "node": 0}, ref]


@pytest.mark.asyncio
async def test_a_reconstruct_node_quote_expands_to_itself_and_its_neighbours(windowed):
    async with _TempDB() as tmp:
        ref, rows = await _long_memory(tmp)
        (item,) = (await reconstruct.do_reconstruct(AGENT, "vault combination", deep=True))["items"]
        index = item["node"]["index"]
        assert item["head_ref"] == ref and index == len(rows) - 1

        (same,) = (await do_get_contents(AGENT, [{"ref": ref, "node": index}]))["items"]
        assert same["content"] == item["content"] and same["range"]["span"] == item["node"]["span"]

        (around,) = (await do_get_contents(AGENT, [{"ref": ref, "node": [index - 1, index]}]))["items"]
        assert around["content"] == LONG[rows[index - 1][1] :]


def test_the_mcp_schema_admits_range_objects_and_strings():
    tools = {t.name: t for t in server.registry._tools}
    items = tools["get_contents"].inputSchema["properties"]["refs"]["items"]
    assert [branch["type"] for branch in items["anyOf"]] == ["string", "object"]
    pair = {"type": "array", "items": {"type": "integer", "minimum": 0}, "minItems": 2, "maxItems": 2}
    index_or_pair = {"anyOf": [{"type": "integer", "minimum": 0}, pair]}
    assert items["anyOf"][1] == {
        "type": "object",
        "properties": {
            "ref": {"type": "string"},
            "node": index_or_pair,
            # Blocks take the same shape as nodes, and `revision` is the digest a
            # reconstruct quote's expand hands out for the text its offsets were
            # measured in (docs/BLOCK_REACH_DESIGN.md invariant 9).
            "block": index_or_pair,
            "span": pair,
            "revision": {"type": "string"},
        },
        "required": ["ref"],
    }
