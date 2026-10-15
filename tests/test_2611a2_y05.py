"""reconstruct's quoting at its edges, and the Hamming fallback's choice of a record's block.

* The Hamming fallback represents a record by its best block, and two of its blocks
  at the same distance go to the lower block index, the order the cosine re-rank
  keeps too. Hamming distances are small integers, so such ties are common.
* A block quote whose governing range wanted more than the context limit says so:
  ``context_incomplete`` and an ``expand`` naming the block range it was reaching
  for, clamped to the record's blocks at both ends.
* A record exactly as long as the quote cap is quoted whole, and a governing range
  exactly as long as the cap is a passage that was not cut.
* Under a mode or the evidence sequence, a head longer than the cap is quoted from its
  stored block set, whose int8 cosines rank its passages in the evidence order.
* Under a mode, the held items on the whole-record floor do not hide a window the
  budget cut: the short window is still blamed on the budget.
"""

import numpy as np
import pytest

from cpersona import blocks, config, memory_handlers, reconstruct
from tests.test_reconstruct_filled_quote import _TempDB

AGENT = "agent.y05"


class _Claim:
    def __init__(self, content: str, ref: str = "mem:1"):
        self.ref = ref
        self.kind = ref.split(":")[0]
        self.content = content


@pytest.fixture
def blocks_on(monkeypatch, fake_embedding_client):
    monkeypatch.setattr(config, "BLOCK_BUILD_ENABLED", True)
    monkeypatch.setattr(config, "BLOCK_RETRIEVAL_ENABLED", True)
    return fake_embedding_client


async def _store(tmp, texts) -> list[str]:
    refs = []
    for text in texts:
        stored = await memory_handlers.do_store(AGENT, {"content": text})
        refs.append(f"mem:{stored['id']}")
    await tmp.drain()
    return refs


# --------------------------------------------------------------------------
# the Hamming fallback: a record's tied blocks
# --------------------------------------------------------------------------


def test_equal_hamming_distances_inside_one_record_go_to_the_lower_block():
    query = blocks.pack_bits([1.0] * 16)
    last_off = blocks.pack_bits([1.0] * 15 + [-1.0])
    first_off = blocks.pack_bits([-1.0] + [1.0] * 15)
    far = blocks.pack_bits([-1.0] * 16)
    # Three blocks of record 1 tie at distance 1. Neither the first one seen nor the
    # last one is the lowest index, so keeping either, or the highest, is caught.
    rows = [
        ("mem", 1, 3, last_off),
        ("mem", 1, 1, first_off),
        ("mem", 1, 2, last_off),
        ("mem", 1, 0, far),
        ("mem", 2, 4, first_off),
        ("mem", 2, 5, last_off),
    ]
    hits = blocks._hamming(rows, query)
    assert [(h.kind, h.parent_id, h.block_index, h.distance) for h in hits] == [
        ("mem", 1, 1, 1),
        ("mem", 2, 4, 1),
    ]


# --------------------------------------------------------------------------
# a block quote that wanted more context than the limit allows
# --------------------------------------------------------------------------

#: Lines with no sentence terminator, so every block wants the next one and the
#: previous one: the governing range grows until the context limit stops it. Each
#: line is one block of 72 characters; twenty fit the limit and twenty-one do not.
RUN_ON = [f"line{n:02d} " + "lorem ipsum dolor sit amet consectetur adipiscing elit sed do " + "xy\n"
          for n in range(61)]
# The store keeps a record's text without its trailing newline, so neither does this.
RUN_ON_TEXT = "".join(RUN_ON).rstrip("\n")


def _run_on_rows():
    spans = [(s.start, s.end) for s in blocks.segment(RUN_ON_TEXT)]
    expected = [(72 * n, min(72 * (n + 1), len(RUN_ON_TEXT))) for n in range(61)]
    assert spans == expected, "the fixture must divide into one block per line"
    assert 20 * 72 <= blocks.BLOCK_CONTEXT_CHARS < 21 * 72, "the context limit no longer holds twenty lines"
    return [(n, s, e, None) for n, (s, e) in enumerate(spans)]


@pytest.mark.parametrize(
    ("token", "span", "wanted"),
    [
        # The first line: the range grows forwards to lines 0-19; the block before the
        # first is clamped to 0.
        ("line00", [0, 1440], [0, 20]),
        # A middle line: backwards first, to lines 21-40; it wanted 20 and 41.
        ("line40", [21 * 72, 41 * 72], [20, 41]),
        # The last line: lines 41-60; the block after the last is clamped to 60.
        ("line60", [41 * 72, len(RUN_ON_TEXT)], [40, 60]),
    ],
)
def test_a_block_quote_past_the_context_limit_names_the_range_it_wanted(token, span, wanted):
    claim = _Claim(RUN_ON_TEXT, ref="mem:7")
    quote = reconstruct._block_quote(claim, (RUN_ON_TEXT, _run_on_rows()), None, reconstruct._trigrams(token))
    assert token in quote["content"], "the fixture's query did not pick its line"
    assert quote["block"]["span"] == span
    assert quote["content"] == RUN_ON_TEXT[span[0]:span[1]]
    assert quote["context_incomplete"] is True
    assert quote["expand"]["block"] == wanted
    assert quote["expand"] == {
        "ref": "mem:7",
        "block": wanted,
        "revision": blocks.text_revision(RUN_ON_TEXT),
    }


@pytest.mark.asyncio
async def test_reconstruct_reports_a_block_quote_that_wanted_more_context(blocks_on, monkeypatch):
    """The single-passage head quote (CPERSONA_RECONSTRUCT_QUOTE_CHARS=0) of a record of
    run-on lines, read from its stored block set. The preview cut is lifted, so the
    only thing that can mark the quote incomplete is the context limit."""
    monkeypatch.setattr(config, "RECONSTRUCT_QUOTE_CHARS", 0)
    monkeypatch.setattr(config, "RECALL_PREVIEW_CHARS", 5000)
    async with _TempDB() as tmp:
        (ref,) = await _store(tmp, [RUN_ON_TEXT])
        out = await reconstruct.do_reconstruct(AGENT, "line60", count=1, deep=True, trace=True)
    (item,) = out["items"]
    assert item["head_ref"] == ref
    assert item["block"]["span"] == [41 * 72, len(RUN_ON_TEXT)]
    assert item["content"] == RUN_ON_TEXT[41 * 72:]
    assert "content_truncated" not in item, "the preview cut fired, so this does not test the context limit"
    assert item["context_incomplete"] is True
    assert item["expand"]["block"] == [40, 60]
    assert item["expand"] == {"ref": ref, "block": [40, 60], "revision": blocks.text_revision(RUN_ON_TEXT)}


# --------------------------------------------------------------------------
# the quote cap's boundaries
# --------------------------------------------------------------------------

#: Sentences of different lengths, so a cap equal to one range is shorter than others.
SENTENCES = [
    "Alpha went to the market early. ",
    "Bravo stayed at home for the whole of the long rainy afternoon. ",
    "Charlie called. ",
    "Delta slept until noon and then read the paper. ",
]
SENTENCE_TEXT = "".join(SENTENCES).rstrip()


def test_a_record_exactly_as_long_as_the_cap_is_quoted_whole():
    cap = len(SENTENCE_TEXT)
    quote = reconstruct._filled_quote(_Claim(SENTENCE_TEXT), None, None, reconstruct._trigrams("charlie"), cap)
    assert "content_truncated" not in quote, "a record shown in full was reported as cut"
    assert quote == {"content": SENTENCE_TEXT, "quote_basis": "whole", "ranges": [[0, cap]]}


def test_a_record_exactly_as_long_as_the_cap_is_one_whole_passage():
    cap = len(SENTENCE_TEXT)
    text, basis, passages = reconstruct.record_passages(
        _Claim(SENTENCE_TEXT), None, None, reconstruct._trigrams("charlie"), None, cap
    )
    assert basis == "whole"
    assert (text, basis, passages) == (SENTENCE_TEXT, "whole", [(0, cap, None, None)])


def test_a_governing_range_exactly_as_long_as_the_cap_is_not_cut():
    spans = [(s.start, s.end) for s in blocks.segment(SENTENCE_TEXT)]
    assert len(spans) == len(SENTENCES), "the fixture must divide into one block per sentence"
    ranges = [blocks.context_range(SENTENCE_TEXT, spans, i)[:2] for i in range(len(spans))]
    assert ranges == spans, "each sentence must govern only itself"
    cap = max(e - s for s, e in ranges)
    assert cap < len(SENTENCE_TEXT)
    _, basis, passages = reconstruct.record_passages(
        _Claim(SENTENCE_TEXT), None, None, reconstruct._trigrams("bravo"), None, cap
    )
    assert basis == "lexical"
    assert sorted(p[:2] for p in passages) == sorted(ranges), "a range was cut or left out"
    exact = [p for p in passages if p[1] - p[0] == cap]
    assert exact, "no passage is exactly as long as the cap"
    assert all(p[3] is None for p in passages), f"a range no longer than the cap was marked cut: {passages}"


# --------------------------------------------------------------------------
# a mode's head quoted from its stored block set
# --------------------------------------------------------------------------


def _i8(values):
    return np.asarray(values, dtype=np.int8).tobytes()


def test_record_passages_ranks_a_stored_block_set_by_its_int8_cosines():
    """No shared words, so the order inside the record is the cosine's, and each
    passage carries the cosine of the block that produced it."""
    width = 4
    query = np.asarray([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    vectors = [[0, 100, 0, 0], [100, 0, 0, 0], [60, 80, 0, 0], [-100, 0, 0, 0]]
    spans = [(s.start, s.end) for s in blocks.segment(SENTENCE_TEXT)]
    rows = [(i, s, e, b"\x00", _i8(v)) for i, ((s, e), v) in enumerate(zip(spans, vectors))]
    assert all(len(row[4]) == width for row in rows)
    text, basis, passages = reconstruct.record_passages(
        _Claim(SENTENCE_TEXT), (SENTENCE_TEXT, rows), None, set(), query, 70
    )
    assert (text, basis) == (SENTENCE_TEXT, "blocks")
    assert [p[:2] for p in passages] == [spans[1], spans[2], spans[0], spans[3]]
    assert [round(p[2], 6) for p in passages] == [1.0, 0.6, 0.0, -1.0]
    # The same passages without their cosines are ordered by place alone, so the cosine
    # is what moves the evidence order across records.
    other = [(0, 10, 0.9, None), (10, 20, 0.95, None)]
    with_cosines = reconstruct.evidence_order([other, passages])
    without = reconstruct.evidence_order([other, [(s, e, None, c) for s, e, _, c in passages]])
    assert all(entry[2][2] is not None for entry in with_cosines if entry[0] == 1)
    assert [e[:2] for e in with_cosines] != [e[:2] for e in without]


#: Four long records of thirty sentences each, past the quote cap, so a mode's head is
#: divided into passages; the query names one sentence of record 2.
MODE_QUERY = "filler2x7 lorem"
MODE_RECORDS = [
    f"record {n} " + " ".join(f"filler{n}x{i} " + "lorem ipsum dolor sit amet " * 5 + "." for i in range(30))
    for n in range(4)
]


@pytest.mark.parametrize(
    ("mode", "setting", "expected"),
    [("lite", "items", "whole"), ("pro", "items", "whole"), (None, "evidence", "evidence")],
)
@pytest.mark.asyncio
async def test_a_mode_quotes_a_long_head_from_its_stored_block_set(blocks_on, monkeypatch, mode, setting, expected):
    """Block build and retrieval are on by default, so this is the path a long head
    takes under lite and pro, and under the evidence sequence without a mode."""
    monkeypatch.setattr(config, "RECONSTRUCT_QUOTE_CHARS", 800)
    monkeypatch.setattr(config, "RECONSTRUCT_SEQUENCE", setting)
    async with _TempDB() as tmp:
        await _store(tmp, MODE_RECORDS)
        out = await reconstruct.do_reconstruct(AGENT, MODE_QUERY, count=3, deep=True, trace=True, mode=mode)
    sequence = out["trace"]["sequence"]
    assert sequence["mode"] == expected
    assert out["items"] and all(item.get("quote_basis") == "blocks" for item in out["items"]), [
        item.get("quote_basis") for item in out["items"]
    ]
    cosine_ranks = [taken["ranks"]["cosine"] for taken in sequence["taken"]]
    assert cosine_ranks and all(rank is not None for rank in cosine_ranks), cosine_ranks


# --------------------------------------------------------------------------
# a mode's budget cut, judged without the held items
# --------------------------------------------------------------------------

BUDGET_QUERY = "harvest orchard"
#: Two long records the gate admits (the test double embeds a bag of tokens, so the
#: query's two words, repeated, keep them close to it) and a short one.
LONG_A = " ".join(f"harvest orchard harvest orchard harvest orchard itemA{i} ." for i in range(40))
LONG_B = " ".join(f"harvest orchard harvest orchard harvest orchard itemB{i} ." for i in range(40))
SHORT_HELD = "harvest orchard keyword note ."


@pytest.mark.parametrize("mode", ["lite", "pro"])
@pytest.mark.asyncio
async def test_a_budget_that_cut_the_window_is_blamed_even_with_held_items_returned(
    fake_embedding_client, blocks_off, monkeypatch, mode
):
    """Under a mode a short held record sits on the whole-record floor, ahead of every
    passage of the window's long records, so a budget can return it while it cuts a
    window item. A held item does not fill the window, so the window is short because
    of the budget, and the response says so."""
    # A small head cap, so the floor of one head quote that a named budget is raised
    # to (the first item must always fit) holds the held record and one passage, and
    # not a second passage.
    monkeypatch.setattr(config, "RECONSTRUCT_QUOTE_CHARS", 100)
    async with _TempDB() as tmp:
        long_a, long_b, held_ref = await _store(tmp, [LONG_A, LONG_B, SHORT_HELD])
        original = memory_handlers.do_recall

        async def seated(*args, **kwargs):
            # The short record arrives as a keyword seat does: held beside the window.
            out = await original(*args, **kwargs)
            for message in out["messages"]:
                if message.get("ref") == held_ref:
                    message["match_reason"] = {"signal": "keyword", "admission": "reservation", "seat": 1}
            return out

        monkeypatch.setattr(memory_handlers, "do_recall", seated)
        full = await reconstruct.do_reconstruct(AGENT, BUDGET_QUERY, count=2, budget=20000, trace=True, mode=mode)
        assert [i["head_ref"] for i in full["items"]] in ([long_a, long_b, held_ref], [long_b, long_a, held_ref])
        assert full["items"][-1]["admission"] == "reservation"
        taken = full["trace"]["sequence"]["taken"]
        assert taken[0]["ref"] == held_ref, "the held record is not on the whole-record floor"
        first = taken[1]
        assert first["ref"] in (long_a, long_b)
        # The floor and the first passage of a window record, and nothing more.
        budget = max(len(SHORT_HELD) + (first["span"][1] - first["span"][0]), reconstruct._head_cap())
        second = taken[2]
        assert budget < len(SHORT_HELD) + sum(t["span"][1] - t["span"][0] for t in (first, second))
        out = await reconstruct.do_reconstruct(AGENT, BUDGET_QUERY, count=2, budget=budget, trace=True, mode=mode)
    heads = [i["head_ref"] for i in out["items"]]
    assert sorted(heads) == sorted([first["ref"], held_ref]), heads
    assert out["reserved_count"] == 1
    assert out["shortfall_reason"] == "budget_exhausted", out.get("shortfall_reason")
