"""The part of a record that matched a query, under a character cap.

A recall row's `content` is cut to the preview tier as a pure prefix: the start
of the record, whatever the query asked. Where the answer sits past that cut the
row arrives and the answer does not. Measured on LongMemEval with an answer
reader, the preview's first 500 characters answered 260 of 500 questions and the
full records 351; this excerpt, filled to 800 characters, answered 341.

The excerpt is made from the pieces the reconstruction exit quotes with: the
record's blocks, ranked by `reconstruct.rank_blocks` (lexical overlap fused with
Hamming distance), each extended to the range that governs it by
`blocks.context_range`. What is new here is only the filling: governing ranges
are taken in ranking order while they fit the cap without overlapping, and are
shown in text order, joined by a separator, so the excerpt reads forwards.

Which blocks, and how they were ranked, is stated in `basis`:

  blocks   the record's current block set, ranked lexically and by its bits
  lexical  divided at read time (no current block set), ranked lexically only
  start    the record divides into one block, so its start is shown

Everything here is deterministic and calls no model: the query vector is the one
the recall already embedded.
"""
from __future__ import annotations

from types import SimpleNamespace

from cpersona import blocks, nodes

#: Joins the governing ranges an excerpt shows, which are not contiguous.
SEPARATOR = " … "


def fill(text: str, spans: list[tuple[int, int]], ranked: list[tuple], cap: int) -> str:
    """Governing ranges of ``ranked`` blocks, in that order, while they fit ``cap``.

    ``spans`` partition ``text``; ``ranked`` are block rows (index first), best
    first. A range overlapping one already taken is skipped. The best range alone
    being longer than the cap is cut to the cap rather than dropped, so an
    excerpt is never empty. The ranges are shown in text order.
    """
    return SEPARATOR.join(text[s:e] for s, e in fill_ranges(text, spans, ranked, cap)[0])


def fill_ranges(
    text: str, spans: list[tuple[int, int]], ranked: list[tuple], cap: int
) -> tuple[list[tuple[int, int]], bool]:
    """The ranges `fill` shows, in text order, and whether the best one was cut to the cap."""
    chosen: list[tuple[int, int]] = []
    used = 0
    for row in ranked:
        start, end, _ = blocks.context_range(text, spans, row[0])
        if any(start < e and s < end for s, e in chosen):
            continue
        cost = (end - start) + (len(SEPARATOR) if chosen else 0)
        if used + cost > cap:
            if not chosen:
                return [(start, start + cap)], True
            break
        chosen.append((start, end))
        used += cost
    return sorted(chosen), False


async def for_refs(agent_id: str, refs: list[str], query: str, query_vec, cap: int) -> dict[str, dict]:
    """``ref -> {"excerpt", "basis"}`` for the recall rows named by ``refs``.

    ``query_vec`` is the vector the recall embedded, or None when it has none;
    without it a stored block set is still ranked, lexically. Refs whose record
    is gone are left out.
    """
    # Imported here: reconstruct imports this package's recall path lazily, and
    # this module is imported by it.
    from cpersona import reconstruct
    from cpersona.database import connection

    parsed = []
    for ref in refs:
        kind, _, number = ref.partition(":")
        if kind in nodes.PARENT_TEXT and number.isdigit():
            parsed.append(SimpleNamespace(ref=ref, kind=kind, row_id=int(number)))
    block_sets = await reconstruct._current_block_sets(agent_id, parsed)
    missing = [p for p in parsed if p.ref not in block_sets]
    texts: dict[str, str] = {}
    if missing:
        async with connection() as db:
            for p in missing:
                table, column = nodes.PARENT_TEXT[p.kind]
                rows = await db.execute_fetchall(
                    f"SELECT {column} FROM {table} WHERE agent_id = ? AND id = ?", (agent_id, p.row_id)
                )
                if rows and rows[0][0]:
                    texts[p.ref] = rows[0][0]

    query_bits = blocks.pack_bits(list(query_vec)) if query_vec is not None else None
    grams = reconstruct._trigrams(query)
    out: dict[str, dict] = {}
    for p in parsed:
        if p.ref in block_sets:
            text, block_rows = block_sets[p.ref]
            basis, bits = "blocks", query_bits
        elif p.ref in texts:
            text = texts[p.ref]
            divided = blocks.segment(text)
            if len(divided) <= 1:
                out[p.ref] = {"excerpt": text[:cap], "basis": "start"}
                continue
            block_rows = [(i, s.start, s.end, None) for i, s in enumerate(divided)]
            basis, bits = "lexical", None
        else:
            continue
        spans = [(start, end) for _, start, end, _ in block_rows]
        ranked = reconstruct.rank_blocks(text, block_rows, bits, grams)
        out[p.ref] = {"excerpt": fill(text, spans, ranked, cap), "basis": basis}
    return out
