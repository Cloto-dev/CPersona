"""The evidence sequence (2.6.5a1, docs/EVIDENCE_ALLOCATION_DESIGN.md sections 3-4).

Every passage of the items' head records is placed in one order across records and the
budget cuts that order: the longest prefix that fits, so raising the budget never removes
a passage, and an item none of whose passages fit is not returned. Off by default
(CPERSONA_RECONSTRUCT_SEQUENCE=items).
"""

import pytest

from cpersona import blocks, config, excerpts, memory_handlers, reconstruct


def _ref(n: int) -> str:
    """A synthetic record ref: no stored record stands behind it."""
    return f"mem:{n}"


class _Claim:
    def __init__(self, ref=None, content=""):
        ref = ref or _ref(1)
        self.ref = ref
        self.kind = ref.split(":")[0]
        self.content = content


def _p(start, end, cosine=None, cut_from=None):
    return (start, end, cosine, cut_from)


@pytest.fixture
def rrf60(monkeypatch):
    monkeypatch.setattr(config, "RRF_K", 60)


# --- the passages of one record ---------------------------------------------------------


def test_a_record_within_the_cap_is_one_whole_passage():
    text, basis, passages = reconstruct.record_passages(_Claim(content="short"), None, None, set(), None, 800)
    assert (text, basis, passages) == ("short", "whole", [(0, 5, None, None)])


def test_an_episode_is_measured_in_its_stored_summary():
    ref = "ep" + ":" + "3"
    shown = excerpts.EPISODE_LABEL + "what the episode says"
    text, basis, passages = reconstruct.record_passages(_Claim(ref, shown), None, None, set(), None, 800)
    assert text == "what the episode says" and passages == [(0, len(text), None, None)]


def test_a_record_of_one_block_is_quoted_from_its_start():
    text = "one clause and nothing else"
    assert len(blocks.segment(text)) == 1, "the fixture must divide into a single block"
    _, basis, passages = reconstruct.record_passages(_Claim(content=text), None, None, set(), None, 10)
    assert basis == "start" and passages == [(0, 10, None, len(text))]


SENTENCES = [f"Sentence {n} about topic{n} with some words to fill it out a little more." for n in range(30)]
LONG = " ".join(SENTENCES)


def test_passages_are_governing_ranges_in_ranking_order_without_overlap():
    grams = reconstruct._trigrams("topic17")
    _, basis, passages = reconstruct.record_passages(_Claim(content=LONG), None, None, grams, None, 800)
    assert basis == "lexical"
    spans = [(s.start, s.end) for s in blocks.segment(LONG)]
    rows = [(i, s, e, None) for i, (s, e) in enumerate(spans)]
    best = reconstruct.rank_blocks(LONG, rows, None, grams, None)[0][0]
    assert passages[0][:2] == blocks.context_range(LONG, spans, best)[:2]
    assert "topic17" in LONG[passages[0][0]:passages[0][1]]
    covered = sorted(p[:2] for p in passages)
    assert len(covered) > 1 and all(a[1] <= b[0] for a, b in zip(covered, covered[1:])), "passages overlap"


def test_two_blocks_governed_by_one_range_give_one_passage():
    # "But" qualifies the sentence before it, so blocks 1 and 2 share one governing range.
    text = "Alpha went to the market. Bravo stayed home all day. But charlie called later. Delta slept."
    spans = [(s.start, s.end) for s in blocks.segment(text)]
    assert len(spans) == 4 and blocks.context_range(text, spans, 1)[:2] == blocks.context_range(text, spans, 2)[:2]
    grams = reconstruct._trigrams("bravo charlie")
    _, _, passages = reconstruct.record_passages(_Claim(content=text), None, None, grams, None, 60)
    ranges = [p[:2] for p in passages]
    assert len(ranges) == len(set(ranges)) == 3, "the shared range is offered once"


def test_walk_cuts_are_read_by_the_returned_items_positions():
    cuts = [{"hops"}, set(), {"evidence"}]
    assert reconstruct.returned_walk_cuts(cuts, [1, 2], 2) == {"evidence"}
    assert reconstruct.returned_walk_cuts(cuts, [0, 1, 2], 1) == {"hops"}


def test_a_range_longer_than_the_cap_is_cut_and_says_where_from():
    _, _, passages = reconstruct.record_passages(_Claim(content=LONG), None, None, set(), None, 30)
    cut = [p for p in passages if p[3] is not None]
    assert cut and all(p[1] - p[0] == 30 and p[3] > p[1] for p in cut)


# --- one order across records -----------------------------------------------------------


def test_a_close_passage_of_a_later_record_comes_before_a_weak_one_of_an_earlier(rrf60):
    first = [_p(0, 10, 0.1), _p(20, 30, 0.2), _p(40, 50, 0.3)]
    second = [_p(0, 10, 0.9)]
    order = reconstruct.evidence_order([first, second])
    assert [(i, j) for i, j, _ in order] == [(1, 0), (0, 0), (0, 1), (0, 2)]
    assert order[0][2] == (1, 0, 0), "record rank, inside rank, cosine rank of the closest passage"


def test_without_cosines_the_record_then_the_inside_rank_decides(rrf60):
    order = reconstruct.evidence_order([[_p(0, 5), _p(5, 9)], [_p(0, 4)]])
    # (0, 1) and (1, 0) fuse alike (inside rank 1 + record rank 0, against 0 + 1): the earlier record wins
    assert [(i, j) for i, j, _ in order] == [(0, 0), (0, 1), (1, 0)]
    assert all(ranks[2] is None for _, _, ranks in order)


def test_a_passage_without_a_cosine_ranks_after_those_with_one(rrf60):
    order = reconstruct.evidence_order([[_p(0, 5, None)], [_p(0, 5, 0.2)], [_p(0, 5, 0.1)]])
    ranks = {i: r[2] for i, _, r in order}
    assert ranks[1] == 0 and ranks[2] == 1 and ranks[0] == 2


def test_whole_puts_the_records_shown_whole_first_in_item_order(rrf60):
    records = [
        [_p(0, 10, 0.1), _p(20, 30, 0.9), _p(40, 50, 0.8)],
        [_p(0, 10, 0.2)],
        [_p(0, 10, 0.05), _p(20, 30, 0.95)],
        [_p(0, 10, 0.01)],
    ]
    whole = [False, True, False, True]
    evidence = [(i, j) for i, j, _ in reconstruct.evidence_order(records)]
    longs = [e for e in evidence if not whole[e[0]]]
    assert longs[0][1] != 0, "the fixture must let a long record's second passage lead"
    assert evidence[:2] != [(1, 0), (3, 0)], "the fixture must separate the floor from the evidence order"
    order = [(i, j) for i, j, _ in reconstruct.whole_order(records, whole)]
    assert order[:2] == [(1, 0), (3, 0)], "the records shown whole open the order, in item order"
    assert order[2:] == longs, "a long record's passages keep the evidence order, its best one included"
    every_best_first = sorted((e for e in evidence if e[1] == 0), key=lambda e: e[0])
    assert order[: len(every_best_first)] != every_best_first, "the floor is drawn by length, not by rank"
    assert sorted(reconstruct.whole_order(records, whole)) == sorted(reconstruct.evidence_order(records))


def test_every_sequence_reconstruct_names_is_a_setting_value():
    assert {
        "items",
        reconstruct.SEQUENCE_EVIDENCE,
        reconstruct.SEQUENCE_WHOLE,
    } == set(config.RECONSTRUCT_SEQUENCES)


def test_ties_go_to_the_earlier_record_then_the_earlier_text(rrf60):
    order = reconstruct.evidence_order([[_p(50, 60), _p(0, 10)]])
    assert [j for _, j, _ in order] == [0, 1], "inside rank first, not text position"
    tied = reconstruct.evidence_order([[_p(50, 60, 0.5)], [_p(0, 10, 0.5)]])
    assert [i for i, _, _ in tied][0] == 0


# --- the budget cuts the order ----------------------------------------------------------


def _quote_lengths(records, taken):
    texts = {i: "abcdefghij" * 20 for i in range(len(records))}
    out = 0
    for i, js in taken.items():
        q = reconstruct.sequence_quote(_Claim(), texts[i], "lexical", records[i], js)
        out += len(q["content"])
    return out


def test_raising_the_budget_never_removes_a_passage():
    # A long second passage, then a short third: skipping what does not fit would take the
    # third at a small budget and lose it at a larger one.
    records = [[_p(0, 5), _p(50, 60), _p(100, 103)]]
    order = [(0, 0, None), (0, 1, None), (0, 2, None)]
    previous: set = set()
    for budget in range(1, 40):
        taken, _ = reconstruct.cut_sequence(order, records, budget)
        now = {(i, j) for i, js in taken.items() for j in js}
        assert previous <= now, f"budget {budget} removed {previous - now}"
        previous = now
    assert reconstruct.cut_sequence(order, records, 9)[0] == {0: [0]}, "the cut is a prefix: the third waits"


def test_the_first_passage_is_always_taken():
    taken, used = reconstruct.cut_sequence([(0, 0, None), (1, 0, None)], [[_p(0, 50)], [_p(0, 5)]], 10)
    assert taken == {0: [0]} and used == 50


def test_used_counts_exactly_what_the_quotes_carry():
    # Two separated passages pay one separator; a third filling the gap joins all three.
    records = [[_p(0, 10), _p(20, 30), _p(10, 20)], [_p(0, 7)]]
    order = [(0, 0, None), (1, 0, None), (0, 1, None), (0, 2, None)]
    for budget in (10, 17, 30, 35, 40, 47, 100):
        taken, used = reconstruct.cut_sequence(order, records, budget)
        assert used == _quote_lengths(records, taken), budget
    taken, used = reconstruct.cut_sequence(order, records, 100)
    assert used == 30 + 7, "the gap filled, the record's three passages are one range"


# --- the head quote from the passages taken ---------------------------------------------


def test_the_quote_shows_its_passages_in_text_order():
    text = "0123456789" * 10
    q = reconstruct.sequence_quote(_Claim(), text, "blocks", [_p(50, 60), _p(0, 10), _p(10, 20)], [0, 1])
    assert q["ranges"] == [[0, 10], [50, 60]]
    assert q["content"] == text[0:10] + excerpts.SEPARATOR + text[50:60]
    assert q["content_len"] == 100 and q["content_truncated"] is True
    joined = reconstruct.sequence_quote(_Claim(), text, "blocks", [_p(50, 60), _p(0, 10), _p(10, 20)], [1, 2])
    assert joined["ranges"] == [[0, 20]] and excerpts.SEPARATOR not in joined["content"]


def test_a_whole_record_is_quoted_as_it_is():
    q = reconstruct.sequence_quote(_Claim(), "abc", "whole", [_p(0, 3)], [0])
    assert q == {"content": "abc", "quote_basis": "whole", "ranges": [[0, 3]]}


def test_a_cut_passage_says_it_is_incomplete_and_where_to_read_on():
    q = reconstruct.sequence_quote(_Claim(_ref(7)), "y" * 100, "blocks", [_p(10, 20, None, 60)], [0])
    assert q["context_incomplete"] is True and q["expand"] == {"ref": _ref(7), "span": [10, 60]}
    start = reconstruct.sequence_quote(_Claim(_ref(7)), "y" * 100, "start", [_p(0, 30, None, 100)], [0])
    assert "context_incomplete" not in start and start["expand"] == {"ref": _ref(7), "span": [0, 100]}


# --- through do_reconstruct ---------------------------------------------------------------


RECORDS = [
    f"record {n} " + " ".join(f"filler{n}x{i} " + "lorem ipsum dolor sit amet " * 5 + "." for i in range(30))
    for n in range(6)
]


@pytest.fixture
def sized(monkeypatch):
    monkeypatch.setattr(config, "RECONSTRUCT_QUOTE_CHARS", 800)
    monkeypatch.setattr(config, "RECONSTRUCT_FULL_QUOTES", 5)
    monkeypatch.setattr(config, "RECONSTRUCT_TAIL_QUOTE_CHARS", 400)
    monkeypatch.setattr(config, "RECALL_PREVIEW_CHARS", 500)
    monkeypatch.setattr(config, "RECONSTRUCT_DEFAULT_BUDGET", 4000)
    monkeypatch.setattr(config, "RECONSTRUCT_MAX_BUDGET", 20000)
    monkeypatch.setattr(config, "RECONSTRUCT_FORCED_BUDGET", None)


async def _run(budget, mode, monkeypatch, count=6):
    monkeypatch.setattr(config, "RECONSTRUCT_SEQUENCE", mode)
    return await reconstruct.do_reconstruct("agent.seq", "filler2x7 lorem", count=count, deep=True, trace=True,
                                            budget=budget)


@pytest.mark.usefixtures("blocks_off", "sized")
@pytest.mark.asyncio
async def test_the_evidence_sequence_through_do_reconstruct(fake_embedding_client, monkeypatch):
    from tests.test_reconstruct_filled_quote import _TempDB

    async with _TempDB() as tmp:
        for text in RECORDS:
            await memory_handlers.do_store("agent.seq", {"content": text})
        await tmp.drain()
        items_mode = await _run(1500, "items", monkeypatch)
        runs = {b: await _run(b, "evidence", monkeypatch) for b in (100, 500, 1000, 3000, 6000)}
    assert "sequence" not in items_mode["trace"], "the default sequence is unchanged and says nothing new"
    previous: dict = {}
    for budget, out in runs.items():
        assert out["trace"]["sequence"]["mode"] == "evidence"
        carried = sum(len(i["content"]) for i in out["items"])
        assert out["used_budget"] == carried <= max(budget, 800)
        for item in out["items"]:
            parts = item["content"].split(excerpts.SEPARATOR)
            assert any(all(part in text for part in parts) for text in RECORDS), "a quote is one record's own text"
        spans = {i["head_ref"]: {tuple(r) for r in i["ranges"]} for i in out["items"]}
        for ref, ranges in previous.items():
            assert ref in spans, f"raising the budget to {budget} removed {ref}"
            covered = spans[ref]
            assert all(any(a <= s and e <= b for a, b in covered) for s, e in ranges), (
                f"raising the budget to {budget} shrank {ref}"
            )
        previous = spans
    small = runs[100]
    assert len(small["items"]) < 6 and small["shortfall_reason"] == "budget_exhausted"
    order = [i["head_ref"] for i in runs[6000]["items"]]
    assert [r for r in order if r in {i["head_ref"] for i in small["items"]}] == [i["head_ref"] for i in small["items"]], (
        "returned items keep the recall order"
    )


SHORT = [f"note {n}: lorem ipsum, kept short." for n in range(3)]


@pytest.mark.usefixtures("blocks_off", "sized")
@pytest.mark.asyncio
async def test_the_whole_sequence_through_do_reconstruct(fake_embedding_client, monkeypatch):
    from tests.test_reconstruct_filled_quote import _TempDB

    async with _TempDB() as tmp:
        for text in RECORDS + SHORT:
            await memory_handlers.do_store("agent.seq", {"content": text})
        await tmp.drain()
        runs = {b: await _run(b, "whole", monkeypatch, count=9) for b in (100, 500, 1000, 3000, 6000)}
        evidence = {b: await _run(b, "evidence", monkeypatch, count=9) for b in (500, 1000)}
    short_refs = {i["head_ref"] for i in runs[6000]["items"] if i["content"] in SHORT}
    assert short_refs, "the fixture must return short records"
    floor = sum(len(i["content"]) for i in runs[6000]["items"] if i["head_ref"] in short_refs)
    previous: dict = {}
    for budget, out in runs.items():
        sequence = out["trace"]["sequence"]
        assert sequence["mode"] == "whole"
        taken = [t["ref"] for t in sequence["taken"]]
        shorts_taken = [r for r in taken if r in short_refs]
        assert taken[: len(shorts_taken)] == shorts_taken, f"budget {budget}: a long passage came before a whole record"
        for item in out["items"]:
            if item["head_ref"] in short_refs:
                assert item["content"] in SHORT and item["quote_basis"] == "whole", "a short record is shown whole"
        if budget >= floor:
            returned = {i["head_ref"] for i in out["items"]}
            assert short_refs <= returned, f"budget {budget} covers the floor but dropped a short record"
        assert out["used_budget"] == sum(len(i["content"]) for i in out["items"]) <= max(budget, 800)
        spans = {i["head_ref"]: {tuple(r) for r in i["ranges"]} for i in out["items"]}
        for ref, ranges in previous.items():
            assert ref in spans, f"raising the budget to {budget} removed {ref}"
            assert all(any(a <= s and e <= b for a, b in spans[ref]) for s, e in ranges), (
                f"raising the budget to {budget} shrank {ref}"
            )
        previous = spans
    for budget in (500, 1000):
        kept = {i["head_ref"] for i in evidence[budget]["items"]} & short_refs
        assert len(kept) < len(short_refs), f"budget {budget}: the fixture must let the evidence order drop a short record"
