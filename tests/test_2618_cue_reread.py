"""bug-795: a delete between the time cue's two reads does not hide a record.

The cue arm reads its period's first ``MAX_MEMORIES`` records (the capped part) and
the records past them (the remainder) in two statements, cut by scan position over
the live table. A delete committed between them moved the remainder's first record
into the capped part's last place after that part was read, so neither read saw it.
The arm now reads the pair again, once, when the database changed between them, as
the near and far vector windows do (bug-742).
"""
import pytest

from cpersona import config, database, far_seats
from tests import test_2611a2_w11 as w11
from tests.test_2611a2_w11 import _cue_ids, _seed_cue

# The cue store fixtures of the 2.6.11a2 tests, shared.
clean = w11.clean
cue_store = w11.cue_store


@pytest.mark.asyncio
async def test_a_delete_between_the_two_reads_hides_no_record(cue_store, monkeypatch):
    # Scan order 6, 5, 4 | 3, 2, 1 with a cap of 3. Deleting 6 moves 3 into the cap.
    monkeypatch.setattr(config, "CUE_COARSE_MODE", "on")  # the live store answers the remainder
    async with database.transaction() as w:
        await _seed_cue(w)

    real = far_seats.ranked_with_source
    landed = []

    async def a_delete_lands_first(*args, **kwargs):
        if not landed:
            landed.append(True)
            async with database.transaction() as w:
                await w.execute("DELETE FROM memories WHERE id = 6")
        return await real(*args, **kwargs)

    monkeypatch.setattr(far_seats, "ranked_with_source", a_delete_lands_first)
    async with database.connection() as r:
        raced = await _cue_ids(r)
    monkeypatch.setattr(far_seats, "ranked_with_source", real)
    async with database.connection() as r:
        quiet = await _cue_ids(r)

    assert quiet == [5, 3, 2, 1, 4]
    # With the defect: [5, 2, 1, 4] -- record 3 was in neither read.
    assert raced == quiet


@pytest.mark.asyncio
async def test_a_quiet_store_is_read_once(cue_store, monkeypatch):
    monkeypatch.setattr(config, "CUE_COARSE_MODE", "on")
    async with database.transaction() as w:
        await _seed_cue(w)
    real = far_seats.ranked_with_source
    calls = []

    async def counting(*args, **kwargs):
        calls.append(1)
        return await real(*args, **kwargs)

    monkeypatch.setattr(far_seats, "ranked_with_source", counting)
    async with database.connection() as r:
        assert await _cue_ids(r) == [6, 5, 3, 2, 1, 4]
    assert len(calls) == 1
