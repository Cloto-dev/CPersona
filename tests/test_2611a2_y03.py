"""Boundaries that every other test of these paths happens to stay clear of.

Each test here pins one check whose removal used to leave the suite green,
because the existing fixtures never reach the case it decides:

- get_contents: a range whose ``revision`` is not a string is a malformed
  request, not a record that was rewritten; a node or block set that is
  contiguous and ends at the end of the text but does not start at offset 0 is
  not a partition; the block branch refuses a block equal to the block count
  and reports the record's block count, not the last block asked for plus one.
- the time cue: a ``before`` equal to its ``after`` names an empty period,
  including the date-only pair one day apart in reverse, where a date-only
  ``before`` becomes the end of its day.
- the coarse index answers while the agent still holds a row that never had an
  embedding (one the build named instead of holding); only an embedding lost
  since the build among the selected rows sends the question elsewhere.
- the far seats skip a record the caller's content exclusion names, never seat
  more than ``SEATS`` rows when a refill chunk follows a short first one, and
  drop a candidate whose stored vector changed width after it was offered.
"""

import os
import tempfile

import numpy as np
import pytest
import pytest_asyncio

from cpersona import (
    admin_handlers,
    blocks,
    coarse_index,
    coarse_search,
    config,
    cue,
    database,
    far_seats,
    memory_handlers,
    nodes,
    session,
    tasks,
    vector,
    vector_index,
)
from cpersona.database import get_db
from cpersona.memory_handlers import _parse_ref_entry, do_get_contents
from tests import test_coarse_search as coarse_t
from tests import test_far_seats as far_t
from tests.test_reconstruct_excerpts import LONG, WINDOW

AGENT = "agent.y03"

#: Two blocks: the second sentence qualifies the first.
DECISION = "Aを採用する。ただし、本番ではまだ有効にしない。"


class _TempDB:
    """A database of its own with a queue this test drains, so nodes and blocks get built."""

    async def __aenter__(self):
        session.reset_pauses_for_tests()
        self._saved = (database._db, database.DB_PATH, tasks._task_queue)
        database._db = None
        database.DB_PATH = os.path.join(tempfile.mkdtemp(), "y03.db")
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
    fake_embedding_client.token_window = WINDOW
    monkeypatch.setattr(config, "RECALL_PREVIEW_CHARS", 500)
    return fake_embedding_client


@pytest.fixture
def quoting(monkeypatch, fake_embedding_client):
    monkeypatch.setattr(config, "BLOCK_BUILD_ENABLED", True)
    monkeypatch.setattr(config, "BLOCK_RETRIEVAL_ENABLED", True)
    monkeypatch.setattr(config, "RECALL_PREVIEW_CHARS", 500)
    return fake_embedding_client


async def _rows(table, index_col, parent_kind, parent_id):
    db = await database.get_db()
    return await db.execute_fetchall(
        f"SELECT {index_col}, start_char, end_char FROM {table} "
        "WHERE parent_kind = ? AND parent_id = ? ORDER BY " + index_col,
        (parent_kind, parent_id),
    )


# --------------------------------------------------------------------------------------
# get_contents: the revision's type
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("revision", [123, 0, 1.5, True, ["r"], {"r": 1}])
def test_a_revision_that_is_not_a_string_is_an_invalid_range(revision):
    # A revision is only ever handed out as a string; anything else is the
    # caller's mistake, and must not reach the comparison with the record's
    # revision, where it would read as "the record changed".
    for key, value in (("span", [0, 10]), ("node", 0), ("block", 0)):
        entry = {"ref": "mem:5", key: value, "revision": revision}
        assert _parse_ref_entry(entry) == ("mem:5", None, memory_handlers.RANGE_INVALID), entry


@pytest.mark.asyncio
async def test_get_contents_reports_a_non_string_revision_as_invalid_not_stale(windowed):
    async with _TempDB():
        stored = await memory_handlers.do_store(AGENT, {"content": "0123456789abcdef"})
        ref = f"mem:{stored['id']}"
        good = blocks.text_revision("0123456789abcdef")
        out = await do_get_contents(
            AGENT,
            [{"ref": ref, "span": [0, 10], "revision": 123}, {"ref": ref, "span": [0, 4], "revision": good}],
        )
        assert out["unresolved"] == [{"ref": ref, "reason": memory_handlers.RANGE_INVALID}]
        # The same record with its real revision is served: the refusal is about the request.
        assert [i["content"] for i in out["items"]] == ["0123"]


# --------------------------------------------------------------------------------------
# get_contents: a derived set that does not start at offset 0
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_node_set_that_starts_after_offset_zero_is_not_a_partition(windowed):
    # Numbering, every seam and the last end all still look right; only the
    # first node's start has moved off 0, so serving node 0 would silently drop
    # the start of the record.
    async with _TempDB() as tmp:
        stored = await memory_handlers.do_store(AGENT, {"content": LONG})
        await tmp.drain()
        rows = await _rows("record_nodes", "node_index", "mem", stored["id"])
        assert len(rows) >= 2 and rows[0][1] == 0 and rows[0][2] > 7
        db = await database.get_db()
        await db.execute(
            "UPDATE record_nodes SET start_char = 7 WHERE parent_kind = 'mem' AND parent_id = ? AND node_index = 0",
            (stored["id"],),
        )
        await db.commit()
        ref = f"mem:{stored['id']}"
        out = await do_get_contents(AGENT, [{"ref": ref, "node": 0}, {"ref": ref, "node": 1}])
        assert out["items"] == []
        assert out["unresolved"] == [
            {"ref": ref, "reason": memory_handlers.RANGE_NO_CURRENT_NODES},
            {"ref": ref, "reason": memory_handlers.RANGE_NO_CURRENT_NODES},
        ]


@pytest.mark.asyncio
async def test_a_block_set_that_starts_after_offset_zero_is_not_a_partition(quoting):
    async with _TempDB() as tmp:
        stored = await memory_handlers.do_store(AGENT, {"content": DECISION})
        await tmp.drain()
        rows = await _rows("record_blocks", "block_index", "mem", stored["id"])
        assert len(rows) == 2 and rows[0][1] == 0 and rows[0][2] > 1
        db = await database.get_db()
        await db.execute(
            "UPDATE record_blocks SET start_char = 1 WHERE parent_kind = 'mem' AND parent_id = ? AND block_index = 0",
            (stored["id"],),
        )
        await db.commit()
        out = await do_get_contents(AGENT, [{"ref": f"mem:{stored['id']}", "block": 0}])
        assert out["items"] == []
        assert out["unresolved"] == [
            {"ref": f"mem:{stored['id']}", "reason": memory_handlers.RANGE_NO_CURRENT_BLOCKS}
        ]


# --------------------------------------------------------------------------------------
# get_contents: the block branch's boundary and count
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_block_equal_to_the_block_count_is_out_of_range_and_the_batch_goes_on(quoting):
    async with _TempDB() as tmp:
        stored = await memory_handlers.do_store(AGENT, {"content": DECISION})
        await tmp.drain()
        rows = await _rows("record_blocks", "block_index", "mem", stored["id"])
        count = len(rows)
        assert count == 2
        ref = f"mem:{stored['id']}"
        out = await do_get_contents(
            AGENT,
            [{"ref": ref, "block": count}, {"ref": ref, "block": [0, count]}, {"ref": ref, "block": count - 1}],
        )
        assert out["unresolved"] == [
            {"ref": ref, "reason": memory_handlers.RANGE_BLOCK_OUT_OF_RANGE},
            {"ref": ref, "reason": memory_handlers.RANGE_BLOCK_OUT_OF_RANGE},
        ]
        (last,) = out["items"]
        assert last["content"] == DECISION[rows[-1][1] :]


@pytest.mark.asyncio
async def test_a_block_range_reports_the_records_block_count(quoting):
    # The count is of the record, not of the range asked for: a caller reading
    # block 0 of a two-block record must learn there is a second one.
    async with _TempDB() as tmp:
        stored = await memory_handlers.do_store(AGENT, {"content": DECISION})
        await tmp.drain()
        rows = await _rows("record_blocks", "block_index", "mem", stored["id"])
        assert len(rows) == 2
        (item,) = (await do_get_contents(AGENT, [{"ref": f"mem:{stored['id']}", "block": [0, 0]}]))["items"]
        assert item["content"] == DECISION[: rows[0][2]]
        assert item["range"]["block"] == [0, 0] and item["range"]["of"] == len(rows)


# --------------------------------------------------------------------------------------
# the time cue: an empty period
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "after, before",
    [
        ("2026-08-01T12:00:00+00:00", "2026-08-01T12:00:00+00:00"),
        ("2026-08-01T12:00:00Z", "2026-08-01T21:00:00+09:00"),  # one instant, two offsets
        # A date-only `before` is the end of its day, which is where `after` begins.
        ("2026-08-02", "2026-08-01"),
    ],
)
def test_a_before_equal_to_its_after_is_refused(after, before):
    with pytest.raises(cue.TimeCueError, match="later than"):
        cue.parse({"after": after, "before": before, "confidence": "sure"})


def test_a_period_one_second_long_is_still_a_period():
    # The control: the refusal is of an empty period, not of a short one.
    tc = cue.parse({"after": "2026-08-01T12:00:00Z", "before": "2026-08-01T12:00:01Z", "confidence": "sure"})
    assert tc is not None and tc.before > tc.after


# --------------------------------------------------------------------------------------
# the coarse index: a row that never had an embedding
# --------------------------------------------------------------------------------------


@pytest_asyncio.fixture
async def coarse_db():
    conn = await get_db()
    await conn.execute("DELETE FROM memories")
    await conn.commit()
    yield conn
    await conn.execute("DELETE FROM memories")
    await conn.commit()


@pytest.mark.asyncio
async def test_the_index_answers_while_a_never_embedded_row_is_still_unembedded(coarse_db, tmp_path):
    # The build names a row with no embedding instead of holding it, and the
    # row is still without one when the question is asked. It never was in the
    # scan, so no position the index counts has moved: the index answers, and
    # in the index-only mode the answer is the index's candidates, not "skipped".
    db = coarse_db
    for seed in range(1, 41):
        await coarse_t._insert(db, seed, agent_id=coarse_t.AGENT, project_id="", channel="")
    hole = await coarse_t._insert(db, 901, agent_id=coarse_t.AGENT, embedding=None, created_at="2026-03-01 00:00:03")
    await db.commit()
    path = str(tmp_path / "coarse-hole")
    await vector_index.start_logging()
    built = await coarse_index.build_coarse_index(db, "memories", path)
    assert built["built"] and built["unembedded"] == 1, built
    index = coarse_index.load_coarse_index(path=path)
    assert hole <= index.watermark and hole not in set(index.ids.tolist())
    still_null = await db.execute_fetchall("SELECT embedding IS NULL FROM memories WHERE id = ?", (hole,))
    assert still_null == [(1,)]

    for n, scope in enumerate(
        [dict(agent_id=coarse_t.AGENT, k=5), dict(agent_id=coarse_t.AGENT, start=3, end=25, k=4)]
    ):
        query = coarse_t._vector(8100 + n)
        expected = await coarse_t._reference(db, query, **scope)
        assert expected, scope
        for live in (False, True):
            got = await coarse_search.coarse_candidates(db, query, path=path, live=live, **scope)
            assert got.source == "index", (scope, live)
            assert got.rows() == expected, (scope, live)


# --------------------------------------------------------------------------------------
# far seats: exclusion, the seat bound on a refill, a vector that changed width
# --------------------------------------------------------------------------------------


@pytest_asyncio.fixture
async def far_db(monkeypatch):
    monkeypatch.setattr(vector, "_embedding_client", far_t.OneHotClient())
    monkeypatch.setattr(vector, "MAX_MEMORIES", far_t.NEAR)
    monkeypatch.setattr(vector, "VECTOR_REACH", 0)
    conn = await get_db()
    await conn.execute("DELETE FROM memories")
    await conn.commit()
    yield conn
    await conn.execute("DELETE FROM memories")
    await conn.commit()


def _seated(out):
    # The response lists the answer in reverse.
    return [m["ref"] for m in far_t._far(out["messages"])][::-1]


@pytest.mark.asyncio
async def test_a_far_record_the_caller_excluded_is_not_seated(far_db, monkeypatch):
    ids = await far_t._seed(far_db)
    monkeypatch.setattr(config, "FAR_SEATS_ENABLED", True)
    # Without the exclusion the best far record takes the first seat.
    plain = await far_t._recall()
    assert _seated(plain) == [f"mem:{ids[45]}", f"mem:{ids[50]}"]

    out = await far_t._recall(exclude_contents=[far_t.WORDS[45]])
    refs = [m["ref"] for m in out["messages"] if "ref" in m]
    assert f"mem:{ids[45]}" not in refs
    # Its place goes to the next far record, not to nobody.
    assert _seated(out) == [f"mem:{ids[50]}", f"mem:{ids[30]}"]


@pytest.mark.asyncio
async def test_a_refill_after_a_short_first_chunk_seats_no_more_than_the_seats(far_db):
    ids = await far_t._seed(far_db)
    gone = max(ids.values()) + 1000  # read by id, it names no row: the first chunk seats one
    hits = [far_seats.FarHit(gone, 0.99, 21)] + [
        far_seats.FarHit(ids[n], far_t.HIGH.get(n, 0.5), n) for n in (45, 50, 30)
    ]
    assert len(hits) == 2 * far_seats.SEATS
    seated = await far_seats.seat_rows(
        far_db, hits, agent_id=far_t.AGENT, project_id=None, channel="", source_id="",
        excluded=lambda content: False,
    )
    assert [r["id"] for r in seated] == [ids[45], ids[50]]
    assert len(seated) == far_seats.SEATS


@pytest.mark.asyncio
async def test_a_candidate_whose_vector_changed_width_is_dropped(far_db, monkeypatch):
    # The width is checked again on the vector read by id: one rewritten at
    # another width after the coarse search offered it is dropped, and the rest
    # are still ranked, rather than the ranking failing on a ragged matrix.
    ids = await far_t._seed(far_db)
    real = coarse_search.coarse_candidates

    async def then_rewritten(db, *a, **kw):
        found = await real(db, *a, **kw)
        assert ids[45] in found.ids
        wide = np.zeros(far_t.DIM * 2, dtype=np.float32)
        wide[0] = 1.0
        await db.execute("UPDATE memories SET embedding = ? WHERE id = ?", (wide.tobytes(), ids[45]))
        await db.commit()
        return found

    monkeypatch.setattr(coarse_search, "coarse_candidates", then_rewritten)
    hits = await far_seats.ranked(
        far_db, far_t.ONE_HOT, agent_id=far_t.AGENT, project_id=None, channel="", source_id="", floor=0.5,
    )
    assert [h.id for h in hits] == [ids[50], ids[30]]
