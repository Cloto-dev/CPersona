"""Reconstructive recall v1.2 (2.6.4): what reaches the reader, made smaller and closer to the answer.

Four changes, each pinned here, none of which moves the candidate pool, the clusters
or the item order:

- a record's blocks are ranked by the cosine of their stored int8 vectors when every
  block has one, and by their bits otherwise (all or nothing);
- passages that touch are shown as one, so a sentence running across two blocks is
  not cut by a separator; only passages with text between them are separated;
- the head quote's size follows the item's place: full for the first
  RECONSTRUCT_FULL_QUOTES items, RECONSTRUCT_TAIL_QUOTE_CHARS after them, and the
  default budget is the sum of those sizes;
- the default response leaves to the trace what a reader does not act on: an
  item's independence_reason when it is "singleton", a claim's why when it is
  "seed", quote_basis and content_truncated. A value other than the default stays.
"""

import numpy as np
import pytest

from cpersona import blocks, config, excerpts, memory_handlers, reconstruct

DIM = 16


def _ref(n: int) -> str:
    """A synthetic record ref: no stored record stands behind it."""
    return f"mem:{n}"


def _unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


def _i8(v):
    # Python floats: the storage refusal rejects numpy scalars as non-numbers.
    return blocks.pack_int8(_unit(v).tolist())


QUERY = _unit([1.0] * DIM)
TEXT = "Alpha one. Bravo two. Charlie three. Delta four."
SPANS = [(0, 10), (10, 21), (21, 36), (36, 48)]


def _rows(vectors, bits):
    return [(i, s, e, bits[i], vectors[i]) for i, (s, e) in enumerate(SPANS)]


# --- ranking: the int8 vectors when every block has one ---------------------------------


def _disagreeing_rows():
    """Block 0 matches the query's signs exactly but points away from it in magnitude;
    block 2 has the opposite signs on most dimensions but points along the query.
    The bits put 0 first, the int8 cosine puts 2 first."""
    q_bits = blocks.pack_bits(QUERY.tolist())
    away = [1.0] + [0.001] * (DIM - 1)          # same signs as the query, small cosine
    along = [-0.01] * (DIM // 2) + [1.0] * (DIM // 2)  # half the signs flipped, large cosine
    bits = [q_bits, blocks.pack_bits([-1.0] * DIM), blocks.pack_bits(along), blocks.pack_bits([-1.0] * DIM)]
    vectors = [_i8(away), _i8([-1.0] * DIM), _i8(along), _i8([-1.0] * DIM)]
    assert all(b is not None for b in bits) and all(v is not None for v in vectors), "the fixture was refused"
    return bits, vectors, q_bits


def test_the_int8_vectors_rank_the_blocks_when_every_block_has_one():
    bits, vectors, q_bits = _disagreeing_rows()
    rows = _rows(vectors, bits)
    cos_away = float(np.dot(_unit([1.0] + [0.001] * (DIM - 1)), QUERY))
    cos_along = float(np.dot(_unit([-0.01] * (DIM // 2) + [1.0] * (DIM // 2)), QUERY))
    assert cos_along > cos_away, "the fixture's cosines must disagree with its bits, or this proves nothing"
    by_bits = reconstruct.rank_blocks(TEXT, rows, q_bits, set())
    assert by_bits[0][0] == 0, "without the query vector the bits decide"
    by_vector = reconstruct.rank_blocks(TEXT, rows, q_bits, set(), QUERY)
    assert by_vector[0][0] == 2


def test_one_block_without_a_vector_leaves_the_ranking_to_the_bits():
    """All or nothing: a cosine and a Hamming distance cannot share one order."""
    bits, vectors, q_bits = _disagreeing_rows()
    vectors[3] = None
    assert reconstruct.rank_blocks(TEXT, _rows(vectors, bits), q_bits, set(), QUERY)[0][0] == 0


def test_a_vector_of_another_width_leaves_the_ranking_to_the_bits():
    bits, vectors, q_bits = _disagreeing_rows()
    vectors[1] = _i8([1.0] * (DIM * 2))
    assert reconstruct.rank_blocks(TEXT, _rows(vectors, bits), q_bits, set(), QUERY)[0][0] == 0


def test_four_field_rows_still_rank():
    """Rows divided at read time, and older callers, carry no vector field."""
    rows = [(i, s, e, None) for i, (s, e) in enumerate(SPANS)]
    ranked = reconstruct.rank_blocks(TEXT, rows, None, reconstruct._trigrams("Charlie"), QUERY)
    assert ranked[0][0] == 2


def test_the_head_quote_is_ranked_with_the_query_vector():
    """The filled head quote passes the query vector to the shared rule: with room for
    one block, the block the int8 cosine prefers is the one quoted."""
    from types import SimpleNamespace

    bits, vectors, q_bits = _disagreeing_rows()
    claim = SimpleNamespace(ref=_ref(1), content=TEXT)
    quote = reconstruct._filled_quote(claim, (TEXT, _rows(vectors, bits)), q_bits, set(), 15, QUERY)
    assert quote["content"] == TEXT[21:36]
    without = reconstruct._filled_quote(claim, (TEXT, _rows(vectors, bits)), q_bits, set(), 15)
    assert without["content"] == TEXT[0:10], "without the vector the bits decide, so the test can tell"


def test_an_excerpt_is_ranked_with_the_query_vector():
    from types import SimpleNamespace

    bits, vectors, q_bits = _disagreeing_rows()
    claim = SimpleNamespace(ref=_ref(1), content=TEXT)
    sets = {_ref(1): (TEXT, _rows(vectors, bits))}
    assert reconstruct._quote(claim, {}, QUERY, set(), 0, block_sets=sets, query_bits=q_bits)["content"] == TEXT[21:36]
    assert reconstruct._quote(claim, {}, None, set(), 0, block_sets=sets, query_bits=q_bits)["content"] == TEXT[0:10]


@pytest.mark.asyncio
async def test_a_block_set_is_read_with_its_stored_vectors(fake_embedding_client, monkeypatch):
    from types import SimpleNamespace

    from tests.test_reconstruct_filled_quote import LONG, _TempDB

    monkeypatch.setattr(config, "BLOCK_BUILD_ENABLED", True)
    monkeypatch.setattr(config, "BLOCK_RETRIEVAL_ENABLED", True)
    async with _TempDB() as tmp:
        stored = await memory_handlers.do_store("agent.v12", {"content": LONG})
        await tmp.drain()
        ref = f"mem:{stored['id']}"
        sets = await reconstruct._current_block_sets(
            "agent.v12", [SimpleNamespace(ref=ref, kind="mem", row_id=stored["id"])]
        )
    _, rows = sets[ref]
    assert len(rows) > 1 and all(isinstance(row[4], bytes) and len(row[4]) > 0 for row in rows)


# --- passages that touch are one passage ---------------------------------------------------


def test_touching_passages_are_joined_without_a_separator():
    ranges, _ = excerpts.fill_ranges(TEXT, SPANS, [(1,), (2,)], 200)
    assert ranges == [(10, 36)]
    assert excerpts.SEPARATOR.join(TEXT[s:e] for s, e in ranges) == TEXT[10:36]


def test_passages_with_text_between_them_keep_the_separator():
    ranges, _ = excerpts.fill_ranges(TEXT, SPANS, [(0,), (2,)], 200)
    assert ranges == [(0, 10), (21, 36)]
    shown = excerpts.SEPARATOR.join(TEXT[s:e] for s, e in ranges)
    assert excerpts.SEPARATOR in shown


def test_a_touching_passage_costs_no_separator():
    """Two neighbours whose lengths fill the cap exactly fit; with a separator charged
    between them the second would not."""
    cap = (21 - 10) + (36 - 21)
    ranges, _ = excerpts.fill_ranges(TEXT, SPANS, [(1,), (2,)], cap)
    assert ranges == [(10, 36)]
    ranges, _ = excerpts.fill_ranges(TEXT, SPANS, [(0,), (2,)], (10 - 0) + (36 - 21))
    assert ranges == [(0, 10)], "a separated passage still pays for its separator"


# --- the head quote's size follows the item's place -----------------------------------------


@pytest.fixture
def sized(monkeypatch):
    monkeypatch.setattr(config, "RECONSTRUCT_QUOTE_CHARS", 800)
    monkeypatch.setattr(config, "RECONSTRUCT_FULL_QUOTES", 5)
    monkeypatch.setattr(config, "RECONSTRUCT_TAIL_QUOTE_CHARS", 400)
    monkeypatch.setattr(config, "RECALL_PREVIEW_CHARS", 500)
    monkeypatch.setattr(config, "RECONSTRUCT_DEFAULT_BUDGET", 4000)
    monkeypatch.setattr(config, "RECONSTRUCT_MAX_BUDGET", 20000)
    monkeypatch.setattr(config, "RECONSTRUCT_FORCED_BUDGET", None)


def test_the_first_items_are_full_and_the_rest_tail_sized(sized):
    assert [reconstruct._head_cap_at(p) for p in range(7)] == [800] * 5 + [400] * 2


def test_the_default_budget_sums_the_sizes(sized):
    assert reconstruct.resolve_budget(None, 10)[0] == 5 * 800 + 5 * 400
    assert reconstruct.resolve_budget(None, 6)[0] == 4400


def test_filling_off_sizes_every_item_alike(sized, monkeypatch):
    monkeypatch.setattr(config, "RECONSTRUCT_QUOTE_CHARS", 0)
    assert {reconstruct._head_cap_at(p) for p in range(8)} == {500}


@pytest.mark.parametrize("tail", [0, 801])
def test_a_tail_outside_the_head_size_is_a_startup_error(sized, monkeypatch, tail):
    monkeypatch.setattr(config, "RECONSTRUCT_DEFAULT_COUNT", 10)
    monkeypatch.setattr(config, "RECONSTRUCT_MAX_COUNT", 50)
    monkeypatch.setattr(config, "RECONSTRUCT_FORCED_COUNT", None)
    monkeypatch.setattr(config, "RECONSTRUCT_TAIL_QUOTE_CHARS", tail)
    with pytest.raises(ValueError, match="RECONSTRUCT_TAIL_QUOTE_CHARS"):
        config.validate_reconstruct_counts()
    monkeypatch.setattr(config, "RECONSTRUCT_TAIL_QUOTE_CHARS", 800)
    config.validate_reconstruct_counts()


LONG_WORDS = [f"record {n} " + " ".join(f"filler{n}x{i} lorem ipsum dolor." for i in range(60)) for n in range(8)]


@pytest.mark.usefixtures("blocks_off")
@pytest.mark.asyncio
async def test_a_reconstruction_quotes_its_later_items_at_the_tail_size(sized, fake_embedding_client):
    from tests.test_reconstruct_filled_quote import _TempDB

    async with _TempDB() as tmp:
        for text in LONG_WORDS:
            await memory_handlers.do_store("agent.v12", {"content": text})
        await tmp.drain()
        out = await reconstruct.do_reconstruct("agent.v12", "record lorem ipsum", count=8, deep=True, trace=True)
    items = out["items"]
    assert len(items) >= 7, "the window must reach past the full-size items for this to prove anything"
    assert all(len(t) > 800 for t in LONG_WORDS), "the records must be longer than a full quote"
    assert max(len(i["content"]) for i in items[:5]) > 400, "a full-size item was cut to the tail size"
    assert all(len(i["content"]) <= 400 for i in items[5:])


# --- the default response leaves defaults to the trace --------------------------------------


def test_compaction_drops_only_the_defaults_and_the_redundant():
    item = {
        "content": "x", "head_ref": _ref(1), "quote_basis": "blocks", "content_len": 900, "content_truncated": True,
        "ranges": [[0, 1]], "independence_reason": "singleton",
        "claims": [{"ref": _ref(1), "as_of": "t", "why": "seed"}, {"ref": _ref(2), "as_of": "t", "why": "cluster:episode"}],
        "excerpts": [{"ref": _ref(2), "content": "y", "content_truncated": True, "content_len": 9}],
    }
    out = reconstruct._compact_item(item)
    assert out == {
        "content": "x", "head_ref": _ref(1), "content_len": 900, "ranges": [[0, 1]],
        "claims": [{"ref": _ref(1), "as_of": "t"}, {"ref": _ref(2), "as_of": "t", "why": "cluster:episode"}],
        "excerpts": [{"ref": _ref(2), "content": "y", "content_len": 9}],
    }
    bundled = reconstruct._compact_item({**item, "independence_reason": "cluster:episode"})
    assert bundled["independence_reason"] == "cluster:episode", "a reason other than the default is kept"
    assert item["quote_basis"] == "blocks" and item["claims"][0]["why"] == "seed", "the input is not modified"
