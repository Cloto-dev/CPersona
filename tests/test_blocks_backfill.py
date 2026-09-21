"""The backfill sweep: docs/BLOCK_REACH_DESIGN.md §6, §7.

Turning construction on gives a deployment the corpus it already had. What is
under test here is not the division (test_blocks_segment) or the build
(test_blocks_build) but the sweep around them: that one run is bounded, that its
continuation starts where it stopped without passing a record over, that a
record it could not build stays a candidate, and that what it reports about the
corpus is not the count of what it just did.

The queue is a real ``MemoryTaskQueue`` that is never started. Runs are invoked
directly rather than drained, because a drain also runs the continuation the
sweep queues for itself — which is the right behaviour in production and the
wrong instrument for measuring one bounded run.
"""

import json
import os
import tempfile

import pytest

from cpersona import (
    admin_handlers,
    blocks,
    config,
    database,
    generation,
    memory_handlers,
    nodes,
    session,
    tasks,
)

AGENT = "agent.backfill"

#: Divides into several blocks, short enough that no boundary is forced.
MANY = "一文目です。二文目です。\n\n三文目です。四文目です。"
#: One sentence, so the division is the record and no row is owed.
ONE = "ひとつの文だけ"


def _many(n: int) -> str:
    """A record that divides into several blocks, distinct from its neighbours.

    Distinct because a store of identical content is deduplicated, and a seed of
    three identical texts is one record. The marker is a whole sentence of its
    own, so it adds a block rather than moving a boundary inside the rest.
    """
    return f"{n}番目の記録です。" + MANY


class _TempDB:
    async def __aenter__(self):
        session.reset_pauses_for_tests()
        self._dir = tempfile.mkdtemp()
        self._saved = (database._db, database.DB_PATH, tasks._task_queue)
        database._db = None
        database.DB_PATH = os.path.join(self._dir, "blocks_backfill.db")
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

    async def blocks_of(self, kind="mem", parent_id=None):
        db = await database.get_db()
        return await db.execute_fetchall(
            "SELECT block_index, start_char, end_char FROM record_blocks "
            "WHERE parent_kind = ? AND parent_id = ? ORDER BY block_index",
            (kind, parent_id),
        )

    async def sweeps_queued(self):
        db = await database.get_db()
        rows = await db.execute_fetchall(
            "SELECT payload FROM pending_memory_tasks WHERE task_type = ? ORDER BY id",
            (blocks.BACKFILL_TASK_TYPE,),
        )
        return [json.loads(r[0]) for r in rows]


@pytest.fixture
def enabled(monkeypatch, fake_embedding_client):
    monkeypatch.setattr(config, "BLOCK_BUILD_ENABLED", True)
    return fake_embedding_client


async def _seed(tmp, monkeypatch, texts, kind="mem"):
    """Store records with construction off, so the sweep is what builds them.

    Returns [(id, text)] in store order, which is the order the cursor advances.
    """
    monkeypatch.setattr(config, "BLOCK_BUILD_ENABLED", False)
    seeded = []
    for text in texts:
        if kind == "mem":
            row_id = (await memory_handlers.do_store(AGENT, {"content": text}))["id"]
        else:
            row_id = (
                await memory_handlers.do_archive_episode(AGENT, [], summary=text)
            )["episode_id"]
        assert row_id is not None, "the fixture stored nothing"
        seeded.append((row_id, text))
    assert len({row_id for row_id, _ in seeded}) == len(texts), "a seed record was deduplicated"
    await tmp.drain()
    monkeypatch.setattr(config, "BLOCK_BUILD_ENABLED", True)
    return seeded


def _counted(monkeypatch, client):
    """Count the embedding requests a run spends."""
    calls = []
    original = client.embed

    async def counting(texts):
        calls.append(len(texts))
        return await original(texts)

    monkeypatch.setattr(client, "embed", counting)
    return calls


# --------------------------------------------------------------------------
# what the sweep walks
# --------------------------------------------------------------------------


def test_the_sweep_walks_every_kind_that_has_text():
    """A third parent kind must not be able to arrive and be left out of every
    sweep. The cursor names a kind, so the order is written down rather than
    taken from a dict at the point of use — this is what holds the two in step."""
    assert set(blocks.BACKFILL_KINDS) == set(nodes.PARENT_TEXT)
    assert len(blocks.BACKFILL_KINDS) == len(nodes.PARENT_TEXT), "a kind is listed twice"


# --------------------------------------------------------------------------
# opt-in and one-at-a-time (§7)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nothing_is_queued_when_construction_is_off(fake_embedding_client):
    async with _TempDB() as tmp:
        assert await blocks.queue_backfill() is False
        assert await tmp.sweeps_queued() == []


@pytest.mark.asyncio
async def test_a_second_sweep_is_not_queued_while_one_is_pending(enabled):
    """One sweep at a time, checked against the queue's own table so a restart
    that left one queued does not acquire a second."""
    async with _TempDB() as tmp:
        assert await blocks.queue_backfill() is True
        assert await blocks.queue_backfill() is False
        assert len(await tmp.sweeps_queued()) == 1


@pytest.mark.asyncio
async def test_a_sweep_queued_before_the_gate_closed_builds_nothing(monkeypatch, enabled):
    async with _TempDB() as tmp:
        [(mem_id, _)] = await _seed(tmp, monkeypatch, [MANY])
        await blocks.queue_backfill()
        monkeypatch.setattr(config, "BLOCK_BUILD_ENABLED", False)

        await tmp.drain()

        assert await tmp.blocks_of(parent_id=mem_id) == []


# --------------------------------------------------------------------------
# the sweep itself
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_sweep_builds_the_records_that_were_stored_before_it(monkeypatch, enabled):
    async with _TempDB() as tmp:
        seeded = await _seed(tmp, monkeypatch, [_many(1), _many(2), _many(3)])
        for row_id, _ in seeded:
            assert await tmp.blocks_of(parent_id=row_id) == []

        await blocks.queue_backfill()
        await tmp.drain()

        for row_id, text in seeded:
            rows = await tmp.blocks_of(parent_id=row_id)
            assert len(rows) > 1
            assert rows[0][1] == 0 and rows[-1][2] == len(text)


@pytest.mark.asyncio
async def test_episodes_are_swept_as_well_as_memories(monkeypatch, enabled):
    async with _TempDB() as tmp:
        [(mem_id, _)] = await _seed(tmp, monkeypatch, [MANY])
        [(ep_id, _)] = await _seed(tmp, monkeypatch, [MANY], kind="ep")

        await blocks.queue_backfill()
        await tmp.drain()

        assert len(await tmp.blocks_of("mem", mem_id)) > 1
        assert len(await tmp.blocks_of("ep", ep_id)) > 1


@pytest.mark.asyncio
async def test_a_record_whose_blocks_are_current_costs_no_embedding_call(monkeypatch, enabled):
    """Idempotence is what makes a repeated sweep affordable: the second run
    reads, and pays the embedding server nothing."""
    async with _TempDB() as tmp:
        await _seed(tmp, monkeypatch, [_many(1), _many(2)])
        assert "built 2 records" in await blocks.backfill({})

        calls = _counted(monkeypatch, enabled)
        report = await blocks.backfill({})

        assert calls == [], "a current record was re-embedded"
        assert "built 0 records" in report


@pytest.mark.asyncio
async def test_a_record_that_needs_no_blocks_is_reported_as_such(monkeypatch, enabled):
    async with _TempDB() as tmp:
        await _seed(tmp, monkeypatch, [ONE])
        report = await blocks.backfill({})
        assert "built 0 records" in report and "1 needed none" in report


# --------------------------------------------------------------------------
# bounds and the resume cursor
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_record_cap_stops_the_run_and_the_continuation_resumes(monkeypatch, enabled):
    """The cursor names the last record a run finished, so the record the bound
    declined to start is the first one its continuation reaches. A cursor that
    advanced before the bound check would pass that record over for the life of
    the sweep, and nothing else in the run would look wrong."""
    async with _TempDB() as tmp:
        seeded = await _seed(tmp, monkeypatch, [_many(1), _many(2), _many(3)])
        monkeypatch.setattr(blocks, "BACKFILL_RECORD_CAP", 1)

        payload: dict = {}
        for index, (expected, _) in enumerate(seeded):
            report = await blocks.backfill(payload)
            assert "built 1 records" in report, "a run built something other than one record"
            assert len(await tmp.blocks_of(parent_id=expected)) > 1
            if index < len(seeded) - 1:
                assert "stopped at the record cap" in report
                queued = await tmp.sweeps_queued()
                assert queued[-1] == {"kind": "mem", "after_id": expected}
                payload = queued[-1]
            else:
                # Nothing followed the last record, so the run ended by running
                # out of corpus rather than by reaching the cap — and queued no
                # successor. The earlier runs' rows are still here because these
                # runs are invoked directly rather than drained, so the count is
                # what shows that this run added none.
                assert "swept the corpus" in report
                assert len(await tmp.sweeps_queued()) == index

        for row_id, text in seeded:
            rows = await tmp.blocks_of(parent_id=row_id)
            assert [r[0] for r in rows] == list(range(len(rows))), "a record was built twice"
            assert rows[0][1] == 0 and rows[-1][2] == len(text)


@pytest.mark.asyncio
async def test_a_run_starts_one_record_even_when_a_bound_is_already_past(monkeypatch, enabled):
    """A bound that is spent before the run begins must not stop it having done
    nothing: that run would queue a successor in the same state, and the sweep
    would be an unbounded series of runs that each build nothing.

    The clock is the bound that can be past at the start — the counted ones all
    begin the run at zero — so it is the one that measures the guard."""
    async with _TempDB() as tmp:
        seeded = await _seed(tmp, monkeypatch, [_many(1), _many(2)])
        monkeypatch.setattr(blocks, "BACKFILL_SECONDS", -1.0)

        report = await blocks.backfill({})

        assert "built 1 records" in report
        assert "stopped at the time limit" in report
        assert (await tmp.sweeps_queued())[-1] == {"kind": "mem", "after_id": seeded[0][0]}


@pytest.mark.asyncio
async def test_a_bound_smaller_than_one_record_still_lets_the_sweep_move(monkeypatch, enabled):
    """Bounds are checked between records, never inside one, so a run overshoots
    by the record it began — and a record larger than a whole bound is built
    rather than being the place every run stops forever."""
    async with _TempDB() as tmp:
        seeded = await _seed(tmp, monkeypatch, [_many(1), _many(2)])
        monkeypatch.setattr(blocks, "BACKFILL_CHAR_CAP", 1)

        report = await blocks.backfill({})

        assert "built 1 records" in report
        assert "stopped at the character cap" in report
        assert (await tmp.sweeps_queued())[-1] == {"kind": "mem", "after_id": seeded[0][0]}


@pytest.mark.asyncio
async def test_the_time_limit_stops_a_run_that_finds_only_current_records(monkeypatch, enabled):
    """The clock is the only bound a page of records that all turn out to be
    current can reach. Without it a swept corpus is read start to finish in one
    task, and the queue waits for it."""
    async with _TempDB() as tmp:
        await _seed(tmp, monkeypatch, [_many(1), _many(2)])
        await blocks.backfill({})
        monkeypatch.setattr(blocks, "BACKFILL_SECONDS", -1.0)

        report = await blocks.backfill({})

        assert "stopped at the time limit" in report
        assert await tmp.sweeps_queued()


@pytest.mark.asyncio
async def test_a_completed_sweep_queues_no_continuation(monkeypatch, enabled):
    async with _TempDB() as tmp:
        await _seed(tmp, monkeypatch, [MANY])
        assert "swept the corpus" in await blocks.backfill({})
        assert await tmp.sweeps_queued() == []


@pytest.mark.asyncio
async def test_a_cursor_this_module_did_not_write_is_discarded(enabled):
    """Not rounded to the beginning: a sweep that quietly restarted on a cursor
    it could not read would redo the corpus and report it as a resumption."""
    async with _TempDB():
        assert await blocks.backfill({"kind": "mem"}) == "malformed payload, discarded"
        assert await blocks.backfill({"kind": "nope", "after_id": 1}) == (
            "malformed payload, discarded"
        )
        assert await blocks.backfill({"kind": "mem", "after_id": True}) == (
            "malformed payload, discarded"
        )


# --------------------------------------------------------------------------
# what a run does with what it cannot build
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_record_that_moved_under_the_build_is_not_counted_as_built(monkeypatch, enabled):
    async with _TempDB() as tmp:
        [(mem_id, _)] = await _seed(tmp, monkeypatch, [MANY])
        original = blocks.prepare_blocks

        async def rewrite_then_prepare(kind, parent_id, text, node_bounds):
            prepared = await original(kind, parent_id, text, node_bounds)
            db = await database.get_db()
            await db.execute(
                "UPDATE memories SET content = ? WHERE id = ?", ("別の本文。", parent_id)
            )
            await db.commit()
            return prepared

        monkeypatch.setattr(blocks, "prepare_blocks", rewrite_then_prepare)
        report = await blocks.backfill({})

        assert "built 0 records" in report and "1 moved under the build" in report
        assert await tmp.blocks_of(parent_id=mem_id) == []


@pytest.mark.asyncio
async def test_one_record_that_cannot_be_built_does_not_cost_the_rest(monkeypatch, enabled):
    async with _TempDB() as tmp:
        seeded = await _seed(tmp, monkeypatch, [_many(1), "壊れた記録です。" + MANY])
        original = enabled.embed

        async def selective(texts):
            if any("壊れた" in t for t in texts):
                raise RuntimeError("the embedding server is down")
            return await original(texts)

        monkeypatch.setattr(enabled, "embed", selective)
        report = await blocks.backfill({})

        assert "1 failed" in report
        assert len(await tmp.blocks_of(parent_id=seeded[0][0])) > 1
        assert await tmp.blocks_of(parent_id=seeded[1][0]) == []


@pytest.mark.asyncio
async def test_a_run_that_built_nothing_but_failures_raises(monkeypatch, enabled):
    """A backend that is not answering is not a run that succeeded at doing
    nothing: raising puts the retry on the queue's backoff and keeps this
    cursor, instead of reporting success and queueing another run just like it."""
    async with _TempDB() as tmp:
        await _seed(tmp, monkeypatch, [_many(1), _many(2)])

        async def broken(texts):
            raise RuntimeError("the embedding server is down")

        monkeypatch.setattr(enabled, "embed", broken)
        with pytest.raises(RuntimeError, match="failed and none were built"):
            await blocks.backfill({})

        assert await tmp.sweeps_queued() == [], "a run that achieved nothing queued a successor"


# --------------------------------------------------------------------------
# reporting (§11): what this run did, and what the corpus holds
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_report_separates_this_run_from_the_corpus(monkeypatch, enabled):
    """Records built is not coverage. A run that builds every record it is
    allowed to touch says nothing about how much of the corpus holds blocks, and
    an operator watching a backfill needs the second number moving."""
    async with _TempDB() as tmp:
        seeded = await _seed(tmp, monkeypatch, [_many(1), _many(2), ONE])
        monkeypatch.setattr(blocks, "BACKFILL_RECORD_CAP", 1)

        first = await blocks.backfill({})
        second = await blocks.backfill({"kind": "mem", "after_id": seeded[0][0]})

        # The second run builds one record and the corpus then holds two, so a
        # report that printed the run's own count where the corpus count belongs
        # reads differently in each half.
        assert "built 1 records" in first
        assert "corpus 3 records, 1 hold blocks from this model" in first
        assert "built 1 records" in second
        assert "corpus 3 records, 2 hold blocks from this model" in second


@pytest.mark.asyncio
async def test_coverage_counts_a_memory_and_an_episode_of_the_same_id_apart(monkeypatch, enabled):
    """Ids are unique within a kind, not across them. Counting parent_id alone
    would fold memory 1 and episode 1 into one record and report a corpus
    smaller than it is."""
    async with _TempDB() as tmp:
        [(mem_id, _)] = await _seed(tmp, monkeypatch, [MANY])
        [(ep_id, _)] = await _seed(tmp, monkeypatch, [MANY], kind="ep")
        assert mem_id == ep_id == 1, "the fixture no longer exercises the collision"

        await blocks.backfill({})
        db = await database.get_db()
        total, held = await blocks.coverage(db, generation.block_keys())

        assert (total, held) == (2, 2)


# --------------------------------------------------------------------------
# the write paths that keep the sweep from being the only builder
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_archived_episode_queues_its_own_blocks(enabled):
    async with _TempDB() as tmp:
        result = await memory_handlers.do_archive_episode(AGENT, [], summary=MANY)
        assert result["blocks"] == {"status": "queued"}
        await tmp.drain()
        assert len(await tmp.blocks_of("ep", result["episode_id"])) > 1


@pytest.mark.asyncio
async def test_an_edited_memory_queues_its_blocks_again(enabled):
    """The UPDATE's trigger drops the blocks of the old text. Without a queue
    here the edited record has none until a sweep reaches it."""
    async with _TempDB() as tmp:
        stored = await memory_handlers.do_store(AGENT, {"content": MANY})
        await tmp.drain()
        before = await tmp.blocks_of(parent_id=stored["id"])
        assert before

        rewritten = MANY + "五文目です。六文目です。"
        result = await admin_handlers.do_update_memory(stored["id"], rewritten, agent_id=AGENT)
        assert result["blocks"] == {"status": "queued"}
        await tmp.drain()

        rows = await tmp.blocks_of(parent_id=stored["id"])
        assert rows and rows[-1][2] == len(rewritten)
