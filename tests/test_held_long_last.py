"""Pro's held items of long records: their later passages after every other passage of the sequence.

A held item quotes one or two passages. Of a short record that is much of it; of a long
conversation it seldom reaches the evidence and takes the cap from the window's own
quotes. Under mode "pro" the later passages of held items whose record is longer than
R.PRO_HELD_LONG_RECORD_CHARS stored characters therefore follow every other passage of
the payload sequence, while each such item's first passage keeps its place: moving that
one too left a long answer session out of the response when the window's quotes filled
the cap. A window item keeps its place whatever its length, a record at the limit keeps
its place, and lite and a call that names no mode keep the fused order.

Fixture: the extra block places' fixture (test_pro_block_seats) -- a keyword-only row and
records only a stubbed block arm reaches, in a fixed order -- whose first block record is
longer than the limit and whose second is shorter. The fused order holds the long record's
first passage first and lets both records grow together after it, so the smallest budget
that quotes the shorter record, and the smallest that quotes as much of it as the full
cap does, show where the long record's passages sit in the sequence (a larger budget
never removes an item: invariant 9).
"""

from __future__ import annotations

import pytest
import pytest_asyncio

import test_keyword_seats as kw
import test_pro_block_seats as pb
from cpersona import config
from cpersona import memory_handlers as M
from cpersona import reconstruct as R
from cpersona.database import get_db

LONG = "tail record long beyond the window. " + "Filler words about nothing in particular here. " * 140
MEDIUM = "tail record medium beyond the window. " + "Other filler words, also about nothing much. " * 42
CAPS = dict(R.MODE_CAPS)  # the shipped caps; the tests narrow R.MODE_CAPS


@pytest_asyncio.fixture(autouse=True)
async def _fresh(monkeypatch, blocks_off, fake_embedding_client):
    db = await get_db()
    for table in ("memories", "episodes", "profiles"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()
    kw._reset_gate()
    monkeypatch.setattr(M, "CONFIDENCE_ENABLED", False)
    monkeypatch.setattr(config, "FUSED_GATE_ENABLED", True)
    monkeypatch.setattr(M, "RECALL_MODE", "rrf")
    yield
    kw._reset_gate()


def _order(*items):
    return [(i, j, 10) for i, j in items]


def _records(*lengths):
    return [("x" * n, "whole", []) for n in lengths]


def test_later_passages_of_long_held_records_follow_every_other_passage():
    limit = R.PRO_HELD_LONG_RECORD_CHARS
    order = _order((0, 0), (2, 1), (1, 0), (0, 1), (3, 0), (2, 0), (3, 1), (4, 0), (5, 0), (5, 1))
    records = _records(limit + 1, 10, limit + 1, limit, 10, limit + 1)
    # items 0 and 1 are the window's (0 is long); 2 and 5 are held and long, 3 is held at the
    # limit, 4 held and short. A held long item's first passage is its first in the order,
    # whichever passage of the record it is.
    assert R.held_long_last(order, 2, records) == _order(
        (0, 0), (2, 1), (1, 0), (0, 1), (3, 0), (3, 1), (4, 0), (5, 0), (2, 0), (5, 1)
    )


def test_nothing_moves_without_a_long_held_record():
    order = _order((0, 0), (1, 0), (1, 1), (2, 0))
    records = _records(R.PRO_HELD_LONG_RECORD_CHARS * 3, R.PRO_HELD_LONG_RECORD_CHARS, 10)
    assert R.held_long_last(order, 1, records) == order


def test_the_limit_is_six_thousand_characters():
    assert R.PRO_HELD_LONG_RECORD_CHARS == 6000


def _set_cap(monkeypatch, mode, cap):
    monkeypatch.setattr(R, "MODE_CAPS", {**R.MODE_CAPS, mode: cap})


async def _quoted(monkeypatch, refs, mode, cap):
    """Each block record's quoted characters at ``cap`` tokens, in item order (none below the minimum)."""
    _set_cap(monkeypatch, mode, cap)
    out = await R.do_reconstruct(kw.AGENT, pb.QUERY, count=5, mode=mode)
    if "items" not in out:
        return {}
    return {it["head_ref"]: len(it["content"]) for it in out["items"] if it["head_ref"] in refs}


async def _smallest_cap(monkeypatch, refs, mode, holds):
    """The answer at the smallest cap whose quotes satisfy ``holds`` (a larger cap never
    removes an item, and a record's quote only grows with the cap)."""
    lo, hi = 1, CAPS[mode]
    assert holds(await _quoted(monkeypatch, refs, mode, hi)), "the fixture does not reach the state"
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if holds(await _quoted(monkeypatch, refs, mode, mid)):
            hi = mid
        else:
            lo = mid
    return await _quoted(monkeypatch, refs, mode, hi)


@pytest.mark.asyncio
async def test_pro_keeps_a_long_held_records_first_passage_and_quotes_the_rest_after_a_shorter_one(monkeypatch):
    (long_ref, medium_ref), _ = await pb._seed_blocks(monkeypatch, [LONG, MEDIUM])
    assert len(LONG) > R.PRO_HELD_LONG_RECORD_CHARS > len(MEDIUM) > config.RECONSTRUCT_QUOTE_CHARS
    out = await R.do_reconstruct(kw.AGENT, pb.QUERY, count=5, mode="pro")
    held = [it["head_ref"] for it in out["items"] if it.get("admission") == "reservation"]
    assert held[:2] == [long_ref, medium_ref], out["items"]  # the fused order holds the long one first
    refs = {long_ref, medium_ref}
    first = await _smallest_cap(monkeypatch, refs, "pro", lambda q: medium_ref in q)
    assert long_ref in first, first  # the long record's first passage comes before the medium record
    whole = await _smallest_cap(monkeypatch, refs, "pro", lambda q: q.get(medium_ref) == len(MEDIUM))
    assert whole[long_ref] == first[long_ref], (first, whole)  # and nothing more of it until that is whole


@pytest.mark.asyncio
async def test_lite_keeps_the_fused_order(monkeypatch):
    (long_ref, medium_ref), _ = await pb._seed_blocks(monkeypatch, [LONG, MEDIUM])
    refs = {long_ref, medium_ref}
    full = await _quoted(monkeypatch, refs, "lite", CAPS["lite"])
    first = await _smallest_cap(monkeypatch, refs, "lite", lambda q: medium_ref in q)
    assert long_ref in first, first  # the long record is quoted no later than the medium one
    whole = await _smallest_cap(monkeypatch, refs, "lite", lambda q: q.get(medium_ref) == full[medium_ref])
    assert whole[long_ref] > first[long_ref], (first, whole)  # and grows while the medium one does
