"""recall_with_context merges by the instant a stamp names, not by how it is spelled.

bug-287. ``do_recall_with_context`` interleaves the rows recall ranked with the
conversation entries the caller supplied, and it used to order that merged list by
the raw timestamp string. An ISO-8601 stamp's byte order equals its chronological
order only while every stamp compared carries the same UTC offset, and this is the
one surface where that assumption has no owner: the database wrote one side, a
caller wrote the other, and nothing makes them agree on a spelling.

The behaviour matrix records the same fix from the outside (the ``rwc-mixed-offset``
scenario replays the real recall path against a golden). What that recording cannot
say is WHY the order is the one it holds -- it pins a list, not an invariant. These
tests state the invariant, so a future change that reorders the list has to argue
with a sentence rather than re-record a blob.

The merge tail is what is under test, so ``do_recall`` is stubbed here: its ranking
is a different question, answered by its own tests and by the golden replay. The
stub returns only ``{"messages": [...]}``, which is the whole of what the tail reads
from it.
"""
import pytest

from cpersona import memory_handlers as M
from cpersona.utils import _parse_timestamp_utc


def _memory(content, ts):
    """A row shaped like the ones recall returns."""
    return {"content": content, "source": {"type": "User", "id": "u"}, "timestamp": ts}


def _ctx_user(content, ts=None):
    entry = {"role": "user", "name": "alice", "user_id": "u-1", "content": content}
    if ts is not None:
        entry["timestamp"] = ts
    return entry


def _stub_recall(monkeypatch, rows):
    async def _fake(*args, **kwargs):
        return {"messages": [dict(r) for r in rows]}

    monkeypatch.setattr(M, "do_recall", _fake)


async def _merge(monkeypatch, rows, ctx):
    _stub_recall(monkeypatch, rows)
    out = await M.do_recall_with_context("a1", "q", external_context=ctx, limit=10)
    return [m["content"] for m in out["messages"]]


# Every stamp below names an instant, and each is spelled so that its byte order
# differs from its instant order. A fixture whose two orders agree cannot tell a
# byte sort from an instant sort, so it would stay green through the defect and
# through the fix alike; the assertion here is what keeps that fixture honest.
_SPELLINGS = [
    ("jst-03", "2026-01-01T09:00:03+09:00"),   # 00:00:03Z, but '9' > '0' in column 11
    ("est-07", "2025-12-31T19:00:07-05:00"),   # 00:00:07Z, but the date reads 2025
    ("utc-05", "2026-01-01T00:00:05+00:00"),   # 00:00:05Z, but '+' < 'Z'
    ("frac-01", "2026-01-01T00:00:01.500000Z"),  # 00:00:01.5Z, but '.' < 'Z'
    ("z-04", "2026-01-01T00:00:04Z"),
]


def test_the_fixture_spells_time_so_that_byte_order_and_instant_order_disagree():
    stamps = [ts for _, ts in _SPELLINGS]
    by_bytes = sorted(stamps)
    by_instant = sorted(stamps, key=_parse_timestamp_utc)
    assert by_bytes != by_instant, "fixture cannot discriminate a byte sort from an instant sort"


@pytest.mark.asyncio
async def test_a_stamp_in_another_offset_is_merged_at_its_instant_not_at_its_spelling(monkeypatch):
    # Half the stamps arrive as recall rows and half as caller entries, because the
    # defect lives exactly at that seam: each side is internally consistent, and it
    # is comparing one against the other that the byte order got wrong.
    rows = [_memory(name, ts) for name, ts in _SPELLINGS[:2]]
    ctx = [_ctx_user(name, ts) for name, ts in _SPELLINGS[2:]]

    order = await _merge(monkeypatch, rows, ctx)

    expected = [name for name, _ in sorted(_SPELLINGS, key=lambda p: _parse_timestamp_utc(p[1]))]
    assert order == expected
    assert order == ["frac-01", "jst-03", "z-04", "utc-05", "est-07"]


@pytest.mark.asyncio
async def test_two_spellings_of_one_instant_keep_the_order_they_were_merged_in(monkeypatch):
    # Nothing in the data breaks this tie, so the tie has to be broken by position,
    # and the position that means something is the merge order: recall's ranked rows
    # first, then the caller's entries as given. A sort that reordered equal keys
    # would make the response depend on the sort's internals.
    rows = [_memory("row-a", "2026-01-01T00:00:03Z"), _memory("row-b", "2026-01-01T00:00:03+00:00")]
    ctx = [_ctx_user("ctx-a", "2026-01-01T09:00:03+09:00"), _ctx_user("ctx-b", "2026-01-01T00:00:03Z")]

    order = await _merge(monkeypatch, rows, ctx)

    assert order == ["row-a", "row-b", "ctx-a", "ctx-b"]


@pytest.mark.asyncio
async def test_a_stamp_that_names_no_instant_is_never_placed_among_the_dated_ones(monkeypatch):
    # "1999-ish" is the case the old key made dangerous: it sorts before every 2026
    # stamp by bytes, so an unreadable stamp landed in the middle of the chronology
    # and posed as a dated turn at a specific point in the conversation. "zzz" is the
    # same fault at the other end, where last reads as most recent. Both name no
    # instant, so both belong in the one place a message can sit without claiming a
    # time -- ahead of everything dated, which is where a missing stamp already sat.
    rows = [_memory("dated-03", "2026-01-01T00:00:03Z"), _memory("dated-07", "2026-01-01T00:00:07Z")]
    ctx = [
        _ctx_user("looks-early", "1999-ish"),
        _ctx_user("looks-late", "zzz"),
        _ctx_user("no-stamp-key"),
        _ctx_user("empty-stamp", ""),
    ]

    order = await _merge(monkeypatch, rows, ctx)

    undated = ["looks-early", "looks-late", "no-stamp-key", "empty-stamp"]
    assert order[: len(undated)] == undated
    assert order[len(undated):] == ["dated-03", "dated-07"]


@pytest.mark.asyncio
async def test_a_naive_stamp_is_read_as_utc_rather_than_sorted_off_on_its_own(monkeypatch):
    # A stamp SQLite wrote with datetime('now') carries no offset and a space instead
    # of the 'T'. By bytes that puts it in its own neighbourhood ahead of every
    # offset-bearing stamp regardless of when it happened; read as UTC (the bug-114
    # invariant) it takes its place among them.
    rows = [_memory("db-naive-06", "2026-01-01 00:00:06")]
    ctx = [_ctx_user("ctx-02", "2026-01-01T00:00:02Z"), _ctx_user("ctx-09", "2026-01-01T00:00:09Z")]

    order = await _merge(monkeypatch, rows, ctx)

    assert order == ["ctx-02", "db-naive-06", "ctx-09"]


def test_the_sort_key_puts_every_undated_message_ahead_of_every_dated_one():
    # The property the two groups rest on, stated where a change to the key's shape
    # has to face it directly: the flag is compared before the instant, so no stamp
    # can sort into or past the undated group by naming an early enough time.
    undated = M._ts_sort_key({"timestamp": "not-a-timestamp"})
    earliest_dated = M._ts_sort_key({"timestamp": "0001-01-01T00:00:00Z"})
    assert undated < earliest_dated
    assert M._ts_sort_key({"timestamp": ""}) == M._ts_sort_key({})
