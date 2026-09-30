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

  blocks   the record's current block set, ranked lexically, and by its bits
           when the query was embedded at their width (bug-467: without such a
           vector the set is ranked lexically alone and is still reported as
           `blocks`, because the division is the stored one)
  lexical  divided at read time (no current block set), ranked lexically only
  start    the record divides into one block, so its start is shown

A stored block set is read only while block retrieval is on, as the quotation
reads it: off means the index may exist and nothing reads it (bug-469).

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


#: How recall shows an episode: this label, then the stored summary. Offsets into an
#: episode are measured in the summary alone.
EPISODE_LABEL = "[Episode] "


def stored_text(ref: str, shown: str) -> str:
    """The record's text as stored, from the text a recall row shows."""
    if ref.startswith("ep:") and shown.startswith(EPISODE_LABEL):
        return shown[len(EPISODE_LABEL):]
    return shown


def select(
    text: str, stored_rows: list | None, query_bits, query_grams: set[str], cap: int
) -> tuple[str, list[tuple[int, int]], bool, list[tuple], list[tuple[int, int]]]:
    """The passages of ``text`` to show: ``(basis, ranges, severed, ranked, spans)``.

    One rule for the recall excerpt and the reconstruct head quote (bug-480: each
    carried its own copy, and they had diverged). ``stored_rows`` is the record's
    current block set, or None to divide the text at read time. ``basis`` is
    'start' when the record divides into one block; its range is then the start.
    """
    from cpersona import reconstruct

    if stored_rows is not None:
        rows, basis, bits = stored_rows, "blocks", query_bits
    else:
        divided = blocks.segment(text)
        rows = [(i, s.start, s.end, None) for i, s in enumerate(divided)]
        basis, bits = "lexical", None
    if len(rows) <= 1:
        return "start", [(0, min(cap, len(text)))], len(text) > cap, [], []
    spans = [(start, end) for _, start, end, _ in rows]
    ranked = reconstruct.rank_blocks(text, rows, bits, query_grams)
    ranges, severed = fill_ranges(text, spans, ranked, cap)
    return basis, ranges, severed, ranked, spans


async def for_refs(
    agent_id: str, refs: list[str], query: str, query_vec, cap: int, texts: dict[str, str] | None = None
) -> dict[str, dict]:
    """``ref -> {"excerpt", "basis"}`` for the recall rows named by ``refs``.

    ``query_vec`` is the vector the recall embedded, or None when it has none;
    without it a stored block set is still ranked, lexically. Refs whose record
    is gone are left out.

    ``texts`` maps a ref to the text its recall row already carries (bug-491: each
    record without a current block set was read again, one SELECT per ref, on a
    connection of its own). A ref it leaves out is read, in one query per kind.
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
    block_sets = await reconstruct._current_block_sets(agent_id, parsed) if blocks.retrieval_enabled() else {}
    known = {ref: stored_text(ref, shown) for ref, shown in (texts or {}).items() if shown}
    unread = [p for p in parsed if p.ref not in block_sets and p.ref not in known]
    if unread:
        async with connection() as db:
            for kind, (table, column) in nodes.PARENT_TEXT.items():
                ids = [p.row_id for p in unread if p.kind == kind]
                for offset in range(0, len(ids), 500):
                    batch = ids[offset : offset + 500]
                    marks = ",".join("?" for _ in batch)
                    for row_id, text in await db.execute_fetchall(
                        f"SELECT id, {column} FROM {table} WHERE agent_id = ? AND id IN ({marks})",
                        [agent_id, *batch],
                    ):
                        if text:
                            known[f"{kind}:{row_id}"] = text

    query_bits = blocks.pack_bits(list(query_vec)) if query_vec is not None else None
    grams = reconstruct._trigrams(query)
    out: dict[str, dict] = {}
    for p in parsed:
        if p.ref in block_sets:
            text, stored_rows = block_sets[p.ref]
        elif p.ref in known:
            text, stored_rows = known[p.ref], None
        else:
            continue
        basis, ranges, _, _, _ = select(text, stored_rows, query_bits, grams, cap)
        out[p.ref] = {"excerpt": SEPARATOR.join(text[s:e] for s, e in ranges), "basis": basis}
    return out
