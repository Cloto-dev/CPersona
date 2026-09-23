"""Divide a record into clause-sized blocks (docs/BLOCK_REACH_DESIGN.md §2).

Deterministic and offline. No model is called and no token report is fetched:
the same text, node layout and policy produce the same spans on any machine,
which is what lets a stored block set be checked against its parent without
asking the embedding server what it thinks today.

The policy is deliberately timid. It cuts at structure — a blank line, a line
break, the end of a sentence — and nowhere else. A comma is never a reason to
cut, because the clause after one routinely carries the negation, the condition
or the object that the clause before it is about, and a block that ends before
"ただし、本番では有効にしない" says the opposite of the text it came from. Where
the policy cannot tell, it keeps the larger unit: an undivided sentence is a
correct block, a sentence cut before its negation is not.

Two regions are never cut into: a fenced code block, and the inside of a
bracket or a quotation. A sentence end inside 「」 ends the quoted sentence,
not the sentence doing the quoting.

Node boundaries are the one hard constraint. A block never spans two nodes, so
every node boundary is a cut whether or not the policy would have chosen it —
the alternative is a schema in which one block's vector is built from text that
two different node vectors also cover.

Only a block that would still be too long to embed in one piece is split by
force, and such a block records that its end was forced. On this project's
corpus that is 42 blocks out of 107,428.

The divider is the offline half of this module. The build path at the bottom is
the other half: it calls the embedding server, exactly as the node builder does,
and writes what comes back twice: as one bit per dimension, which the Hamming
pass scans, and as one byte per dimension, which re-ranks what that pass ranked
highest (§4b).
"""

import logging
import re
import time
from dataclasses import dataclass

from cpersona import config, generation, tasks, vector
from cpersona.database import connection, transaction
from cpersona.isolation import isolation_where
from cpersona.nodes import PARENT_TEXT

logger = logging.getLogger(__name__)

#: The queue task type that builds one record's blocks.
TASK_TYPE = "build_blocks"

#: Spans per embedding request. Blocks are short, so a request carries many more
#: of them than the node builder's batch of the same size carries nodes.
_EMBED_BATCH = 64

#: Where a forced boundary is taken, in characters.
#:
#: 739 is where the 512-token embedding window begins to be able to close inside
#: a single block on this corpus (measured for bge-m3; the window certainly
#: closes by 1,787). Staying under the lower figure keeps every block embeddable
#: in one piece rather than silently truncated. Server policy, deliberately not
#: derived from anything a caller asks for (§7).
MAX_BLOCK_CHARS = 739

# Structural boundaries, strongest first. The match END is the cut, so the
# separator stays with the block before it and the spans still partition the
# text.
#
# Sentence ends: the full-width marks end a sentence wherever they stand,
# because Japanese and Chinese prose puts the next sentence directly after
# them. The half-width marks count only before whitespace or the end of the
# text, so a decimal point, a version number or a file extension is not read as
# a sentence end. This is the node divider's rule, and it is shared on purpose:
# two answers to "where does this sentence end" would be one answer too many.
_BOUNDARIES: tuple[re.Pattern, ...] = (
    re.compile(r"\n[ \t]*\n"),  # blank line (paragraph)
    re.compile(r"\n"),  # line break (line, list item, table row)
    re.compile(r"[。．！？]|[.!?](?=\s|$)"),  # sentence end
)

#: Weak boundaries, used ONLY when a block must be split by force. A comma is
#: here and nowhere else: it is better than cutting mid-word, and worse than
#: everything above.
_WEAK: tuple[re.Pattern, ...] = (
    re.compile(r"[、，,;；:：]"),
    re.compile(r"\s"),
)

_FENCE = re.compile(r"^[ \t]*```", re.M)

_BRACKETS = {
    "「": "」",
    "『": "』",
    "（": "）",
    "(": ")",
    "［": "］",
    "[": "]",
    "【": "】",
    "《": "》",
    "〈": "〉",
}


@dataclass(frozen=True)
class BlockSpan:
    """A half-open [start, end) range of the parent's text.

    ``forced`` marks a block whose END was chosen by the length limit rather
    than by structure — the one case where the policy cut somewhere it would
    rather not have.
    """

    start: int
    end: int
    forced: bool = False


def _fence_ranges(text: str) -> list[tuple[int, int]]:
    """Half-open ranges covering fenced code blocks.

    An unterminated fence runs to the end of the text: a half-written code
    block is still code, and cutting into it on every blank line is the failure
    this protects against.
    """
    marks = [m.start() for m in _FENCE.finditer(text)]
    ranges: list[tuple[int, int]] = []
    for i in range(0, len(marks) - 1, 2):
        ranges.append((marks[i], marks[i + 1]))
    if len(marks) % 2:
        ranges.append((marks[-1], len(text)))
    return ranges


def _bracket_depth(text: str) -> list[int]:
    """Bracket depth at each offset, length ``len(text) + 1``.

    Unbalanced input degrades to "not inside anything" rather than to an
    exception, and it takes two passes to do that honestly. A single pass that
    pushed every opening bracket would let one unclosed 「 protect the entire
    rest of the record, which is how a whole long memory becomes one block
    because somebody typed a quotation mark and never finished it. So the first
    pass finds the openings that never close, and the second pass counts depth
    without them. A stray closing bracket closes nothing, which needs no
    special case.
    """
    pending: list[tuple[str, int]] = []
    for i, ch in enumerate(text):
        if ch in _BRACKETS:
            pending.append((_BRACKETS[ch], i))
        elif pending and ch == pending[-1][0]:
            pending.pop()
    unmatched = {pos for _, pos in pending}

    depth = [0] * (len(text) + 1)
    stack: list[str] = []
    for i, ch in enumerate(text):
        depth[i] = len(stack)
        if ch in _BRACKETS and i not in unmatched:
            stack.append(_BRACKETS[ch])
        elif stack and ch == stack[-1]:
            stack.pop()
    depth[len(text)] = len(stack)
    return depth


def _structural_cuts(text: str) -> set[int]:
    """Offsets the policy is willing to end a block at."""
    protected = _fence_ranges(text)
    depth = _bracket_depth(text)
    cuts: set[int] = set()
    for pattern in _BOUNDARIES:
        for m in pattern.finditer(text):
            end = m.end()
            if end <= 0 or end >= len(text):
                continue
            if any(a < end < b for a, b in protected):
                continue
            if depth[end] > 0:
                continue
            cuts.add(end)
    return cuts


def _last_weak_cut(text: str, start: int, limit: int) -> int | None:
    """The last weak boundary in ``(start, limit]``, or None.

    Used only inside a forced split. A weak boundary that would leave less than
    half the allowance is passed over for the next class, and then given up on:
    a block of three characters is not an improvement on a block cut mid-word.
    """
    floor = start + (limit - start) // 2
    for pattern in _WEAK:
        best = None
        for m in pattern.finditer(text, start, limit):
            if m.end() > start:
                best = m.end()
        if best is not None and best >= floor:
            return best
    return None


def _force_split(text: str, start: int, end: int, max_chars: int) -> list[BlockSpan]:
    """Cut a span that no structural boundary made short enough.

    Every piece but the last ends on a forced boundary; the last one ends where
    the caller's span already ended, so it is not forced.
    """
    spans: list[BlockSpan] = []
    pos = start
    while end - pos > max_chars:
        limit = pos + max_chars
        cut = _last_weak_cut(text, pos, limit)
        if cut is None or cut <= pos:
            cut = limit
        spans.append(BlockSpan(pos, cut, forced=True))
        pos = cut
    if pos < end:
        spans.append(BlockSpan(pos, end))
    return spans


def segment(
    text: str,
    *,
    node_bounds: tuple[int, ...] | list[int] = (),
    max_chars: int = MAX_BLOCK_CHARS,
) -> list[BlockSpan]:
    """Divide ``text`` into blocks.

    ``node_bounds`` are offsets at which a node ends. Each becomes a cut
    regardless of what the policy would have chosen, because a block may not
    span two nodes (§2). Offsets outside ``(0, len(text))`` are ignored, so a
    caller may pass a node layout's ends verbatim.

    The returned spans cover ``[0, len(text))`` with no gap and no overlap, in
    order. Empty text yields no spans.
    """
    if not text:
        return []
    if max_chars < 1:
        raise ValueError(f"max_chars must be at least 1, got {max_chars}")

    cuts = _structural_cuts(text)
    cuts.update(b for b in node_bounds if 0 < b < len(text))

    bounds = [0, *sorted(cuts), len(text)]
    spans: list[BlockSpan] = []
    for a, b in zip(bounds, bounds[1:]):
        if b <= a:
            continue
        if b - a > max_chars:
            spans.extend(_force_split(text, a, b, max_chars))
        else:
            spans.append(BlockSpan(a, b))
    return spans


def covers(text: str, spans: list[BlockSpan]) -> bool:
    """Whether ``spans`` partition ``[0, len(text))`` — no gap, no overlap.

    The invariant §8.4 states, in a form a caller can assert cheaply before
    writing a block set.
    """
    if not text:
        return not spans
    if not spans:
        return False
    if spans[0].start != 0 or spans[-1].end != len(text):
        return False
    return all(a.end == b.start for a, b in zip(spans, spans[1:]))


# --------------------------------------------------------------------------------------
# building: the queue task, shaped like the node builder
# --------------------------------------------------------------------------------------


def pack_bits(embedding: object) -> bytes | None:
    """Sign-quantise a vector to one bit per dimension, most significant first.

    None when the vector must not be stored. That verdict is delegated to
    ``vector.pack_for_storage`` rather than restated here: it documents itself
    as the one place a vector becomes a BLOB this process will store, and a
    second validator would be a second answer to "is this vector usable". Its
    float32 bytes are discarded — what is wanted is the refusal, which still
    matters after quantisation: a NaN does not survive ``> 0`` as anything
    meaningful, it survives as a zero bit indistinguishable from a real one.
    """
    if vector.pack_for_storage(embedding) is None:
        return None
    import numpy as np

    return np.packbits(np.asarray(embedding, dtype=np.float32) > 0).tobytes()


def pack_int8(embedding: object) -> bytes | None:
    """Quantise a vector to one signed byte per dimension, for the re-rank (§4b).

    Scaled so the largest component is 127, then rounded; the scale is not kept,
    because the only thing ever computed from these bytes is a cosine and a
    cosine does not see it. None when the vector must not be stored — the same
    refusal :func:`pack_bits` delegates — and None for a vector that is zero
    everywhere, which has no direction to compare and whose cosine would be a
    division by zero.
    """
    if vector.pack_for_storage(embedding) is None:
        return None
    import numpy as np

    v = np.asarray(embedding, dtype=np.float32)
    peak = float(np.abs(v).max())
    if peak == 0.0:
        return None
    return np.round(v * (127.0 / peak)).astype(np.int8).tobytes()


def building_enabled() -> bool:
    """Whether a write may queue block construction at all.

    Opt-in first (§7), so a deployment that has not asked for blocks pays
    nothing to find out — then the same runtime conditions the node builder
    needs: a queue to run on and a client to embed with. Unlike the node
    builder this needs no token report, because the divider never asks for one.
    """
    if not config.BLOCK_BUILD_ENABLED:
        return False
    return tasks._task_queue is not None and vector._embedding_client is not None


async def queue_build(kind: str, parent_id: int, agent_id: str, session_key: str) -> dict | None:
    """Queue one record's block construction. Returns the response field, or None.

    None when the enqueue failed: the record is stored and searchable by every
    path that existed before, and the blocks are a derived layer whose absence
    changes no answer. A write that succeeded does not report a failure it
    cannot act on.
    """
    queue = tasks._task_queue
    if queue is None:
        return None
    try:
        await queue.enqueue(
            TASK_TYPE, agent_id, {"kind": kind, "id": parent_id}, session_key=session_key
        )
    except Exception as e:
        logger.warning("could not queue blocks for %s:%s: %s", kind, parent_id, e)
        return None
    return {"status": "queued"}


async def _parent_text(db, kind: str, parent_id: int) -> str | None:
    table, column = PARENT_TEXT[kind]
    rows = await db.execute_fetchall(f"SELECT {column} FROM {table} WHERE id = ?", (parent_id,))
    return rows[0][0] if rows else None


async def _parent_axes(db, kind: str, parent_id: int) -> tuple[str, str, str] | None:
    """The isolation axes to copy onto the rows, read inside the write's own
    transaction so they match the record as it is at that moment."""
    table, _ = PARENT_TEXT[kind]
    rows = await db.execute_fetchall(
        f"SELECT agent_id, project_id, channel FROM {table} WHERE id = ?", (parent_id,)
    )
    return tuple(rows[0]) if rows else None


async def _node_bounds(db, kind: str, parent_id: int) -> tuple[int, ...]:
    """Where this record's nodes end, which a block may not cross (§2).

    A record with no nodes fits the embedding window and has none, and the empty
    result is the right answer rather than a missing one.
    """
    rows = await db.execute_fetchall(
        "SELECT end_char FROM record_nodes WHERE parent_kind = ? AND parent_id = ? "
        "ORDER BY node_index",
        (kind, parent_id),
    )
    return tuple(r[0] for r in rows)


def _set_is_current(count, first, last, current, text_len: int) -> bool:
    """Whether a stored block set spans ``text_len`` characters and came from the
    model it was asked about.

    One predicate, called from both the builder and the backfill sweep, because
    two answers to "are this record's blocks current" would eventually disagree:
    the builder would write a set the sweep keeps rebuilding, or the sweep would
    pass over a record the builder thinks it still owes.

    The model half is where the care is. ``current`` counts the rows written under
    either key :func:`generation.block_keys` returns: the backend's own fingerprint
    when it could be established, and the key this deployment wrote before it could
    ask. Both are real values — the second still follows the configuration, so an
    operator who renames the model invalidates the rows that named the old one —
    and neither is a wildcard: a row from a backend this server never ran is not
    current. What the pair deliberately does not do is declare the existing corpus
    stale the day a fingerprint first arrives; see :mod:`cpersona.generation`.

    When nothing could be established the two collapse to one empty string, which
    compares equal to empty and never to a named model: the resolved default must
    not stand in for an identity nobody reported (§6).
    """
    return bool(count) and first == 0 and last == text_len and current == count


#: The aggregate both currentness reads compute over a record's block rows:
#: count, first start, last end, and how many rows are current. A row is current
#: when a known model produced it AND its re-rank vector is there (§4b) — a set
#: built before the vectors existed is rebuilt rather than half-used, because a
#: re-rank that finds any candidate without a vector falls back to Hamming order
#: for the whole query. Written once so the builder and the sweep cannot drift
#: apart on it (see :func:`_set_is_current`).
_SET_AGGREGATE = (
    "COUNT(*), MIN(b.start_char), MAX(b.end_char), "
    "SUM(b.embedding_model IN (?, ?) AND v.block_index IS NOT NULL) "
    "FROM record_blocks b LEFT JOIN record_block_vectors v "
    "ON v.parent_kind = b.parent_kind AND v.parent_id = b.parent_id "
    "AND v.block_index = b.block_index "
)


async def _blocks_current(db, kind: str, parent_id: int, text_len: int, keys: tuple[str, str]) -> bool:
    """Whether this record's stored blocks were built from its current text by a
    known model. The one-record read behind :func:`_set_is_current`."""
    rows = await db.execute_fetchall(
        f"SELECT {_SET_AGGREGATE}WHERE b.parent_kind = ? AND b.parent_id = ?",
        (*keys, kind, parent_id),
    )
    count, first, last, current = rows[0]
    return _set_is_current(count, first, last, current, text_len)


async def records_without_current_blocks(db, iso) -> list[tuple[str, int, str, tuple[int, ...]]]:
    """Every record in scope that should hold blocks and holds no current set.

    ``(kind, id, text, node bounds)`` for each, in kind then id order. "Should"
    is decided the way the builder decides it, and offline: the record divides
    into more than one block under its current node layout. A record that
    divides into one block has none by design (see :func:`prepare_blocks`), so
    counting it would report a gap that no build can close. "Current" is
    :func:`_set_is_current` over :data:`_SET_AGGREGATE`, the same predicate the
    builder and the sweep read, so the three cannot disagree about a record.

    Locked records are included: building blocks never modifies the record.
    """
    keys = generation.block_keys()
    out: list[tuple[str, int, str, tuple[int, ...]]] = []
    for kind in BACKFILL_KINDS:
        table, column = PARENT_TEXT[kind]
        rows = await db.execute_fetchall(
            f"SELECT r.id, r.{column}, s.* FROM {table} r "
            f"LEFT JOIN (SELECT b.parent_id, {_SET_AGGREGATE}"
            "WHERE b.parent_kind = ? GROUP BY b.parent_id) s ON s.parent_id = r.id "
            f"WHERE 1=1{iso.and_clause} ORDER BY r.id",
            (*keys, kind, *iso.params),
        )
        stale = [
            (row_id, text)
            for row_id, text, _, count, first, last, current in rows
            if text and not _set_is_current(count, first, last, current, len(text))
        ]
        if not stale:
            continue
        bounds: dict[int, list[int]] = {}
        for parent_id, end_char in await db.execute_fetchall(
            "SELECT parent_id, end_char FROM record_nodes WHERE parent_kind = ? "
            "ORDER BY parent_id, node_index",
            (kind,),
        ):
            bounds.setdefault(parent_id, []).append(end_char)
        for row_id, text in stale:
            node_bounds = tuple(bounds.get(row_id, ()))
            if len(segment(text, node_bounds=node_bounds)) > 1:
                out.append((kind, row_id, text, node_bounds))
    return out


@dataclass(frozen=True)
class PreparedBlocks:
    """One record's division and its quantised vectors, computed outside any
    write lock."""

    kind: str
    parent_id: int
    text: str
    spans: list[BlockSpan]
    bits: list[bytes]
    vectors: list[bytes]
    model: str


async def prepare_blocks(
    kind: str, parent_id: int, text: str, node_bounds: tuple[int, ...]
) -> PreparedBlocks | None:
    """Divide ``text`` and quantise an embedding for each block.

    None when the division yields a single block, because that block is the
    record: its vector would be the record's own vector, and a row holding a
    copy of something the search already has is cost without reach. The node
    builder declines the same case for the same reason.

    Network I/O only: nothing is read from or written to the database, so a
    caller holding no lock can run it and hand the result to ``write_blocks``
    later. Raises ``RuntimeError`` when the embedding request fails or returns
    a vector that must not be stored — the queue's retry path handles it, and a
    partial set is never written.
    """
    client = vector._embedding_client
    if client is None:
        raise RuntimeError("no embedding client to build blocks with")
    spans = segment(text, node_bounds=node_bounds)
    if len(spans) <= 1:
        return None
    bits: list[bytes] = []
    vectors_i8: list[bytes] = []
    for start in range(0, len(spans), _EMBED_BATCH):
        batch = spans[start : start + _EMBED_BATCH]
        vectors = await client.embed([text[s.start : s.end] for s in batch])
        if not vectors or len(vectors) != len(batch):
            raise RuntimeError("embedding request returned no vectors for the block spans")
        for v in vectors:
            packed = pack_bits(v)
            quantised = pack_int8(v)
            if packed is None or quantised is None:
                raise RuntimeError("embedding for a block was refused for storage")
            bits.append(packed)
            vectors_i8.append(quantised)
    return PreparedBlocks(
        kind, parent_id, text, spans, bits, vectors_i8, generation.block_keys()[0]
    )


async def write_blocks(db, prepared: PreparedBlocks) -> bool:
    """Replace a record's blocks inside the caller's open transaction. False if stale.

    The division was computed without a lock, so the text may have changed or
    the record may be gone since. The triggers only remove blocks that already
    existed at that moment, so a set written now from text that is no longer
    there would survive and be offered to searches as though it were true. This
    comparison is what closes the unlocked window; removing it is the mutation
    the tests are built to catch.

    The whole set — its rows and their vectors — is written inside the caller's
    transaction, so a crash between the delete and the inserts leaves the
    previous set intact rather than half of a new one.
    """
    if await _parent_text(db, prepared.kind, prepared.parent_id) != prepared.text:
        return False
    axes = await _parent_axes(db, prepared.kind, prepared.parent_id)
    if axes is None:
        return False
    agent_id, project_id, channel = axes
    await db.execute(
        "DELETE FROM record_blocks WHERE parent_kind = ? AND parent_id = ?",
        (prepared.kind, prepared.parent_id),
    )
    await db.executemany(
        "INSERT INTO record_blocks (parent_kind, parent_id, block_index, agent_id, project_id, "
        "channel, start_char, end_char, forced_boundary, embedding_bits, embedding_model) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                prepared.kind,
                prepared.parent_id,
                i,
                agent_id,
                project_id,
                channel,
                s.start,
                s.end,
                int(s.forced),
                packed,
                prepared.model,
            )
            for i, (s, packed) in enumerate(zip(prepared.spans, prepared.bits))
        ],
    )
    # After the blocks, because the DELETE above removed the old vectors through
    # the trigger on record_blocks, and in the same transaction, so a set is
    # never visible with its bits but without its vectors.
    await db.executemany(
        "INSERT INTO record_block_vectors (parent_kind, parent_id, block_index, embedding_i8) "
        "VALUES (?, ?, ?, ?)",
        [
            (prepared.kind, prepared.parent_id, i, quantised)
            for i, quantised in enumerate(prepared.vectors)
        ],
    )
    return True


async def build_blocks(payload: dict) -> str:
    """Build the blocks of one record. Returns what happened, for the queue's log.

    Raises on a failure worth retrying (the embedding request failed); the
    queue's retry path handles it. Returns without writing when there is nothing
    to do: the feature is off, the record is gone, or its blocks are already
    current.
    """
    kind = payload.get("kind") if isinstance(payload, dict) else None
    parent_id = payload.get("id") if isinstance(payload, dict) else None
    if kind not in PARENT_TEXT or not isinstance(parent_id, int) or isinstance(parent_id, bool):
        return "malformed payload, discarded"
    if not config.BLOCK_BUILD_ENABLED:
        # A task queued before the gate was closed must not build now: off means
        # no rows, and a queue that outlives a setting change would write them.
        return "block building is disabled"

    keys = generation.block_keys()
    async with connection() as db:
        text = await _parent_text(db, kind, parent_id)
        if text is None:
            return "parent gone"
        if not text:
            return "parent has no text"
        if await _blocks_current(db, kind, parent_id, len(text), keys):
            return "blocks already current"
        node_bounds = await _node_bounds(db, kind, parent_id)

    prepared = await prepare_blocks(kind, parent_id, text, node_bounds)
    if prepared is None:
        return "one block, which is the record itself"
    async with transaction() as db:
        if not await write_blocks(db, prepared):
            return "parent changed during the build, discarded"
    return f"built {len(prepared.spans)} blocks"


# --------------------------------------------------------------------------------------
# backfill: the corpus that was already there when the deployment opted in
# --------------------------------------------------------------------------------------

#: The queue task type that sweeps the corpus for records without current blocks.
BACKFILL_TASK_TYPE = "backfill_blocks"

#: The parent kinds the sweep walks, in the order it walks them.
#:
#: Written out rather than read from PARENT_TEXT where it is used, because the
#: resume cursor names one of these: a run that visited them in one order and its
#: continuation in another would resume inside a kind it had never started, and
#: every record before the cursor in the kind it skipped would be passed over for
#: the life of the sweep. A test holds this equal to the kinds that have text, so
#: a third kind cannot be added and silently left out of every backfill.
BACKFILL_KINDS: tuple[str, ...] = ("mem", "ep")

#: Records read from a parent table in one statement. This bounds what one page
#: costs to hold, not what the run does: the caps below decide when it stops.
_BACKFILL_PAGE = 500

#: What one backfill run may spend before it stops and queues its continuation.
#:
#: Server policy (§7): fixed here, registered before anything is measured, and
#: derived from nothing a caller asks for. Four bounds rather than one because
#: they run out for different reasons — a corpus of many short records exhausts
#: the record count, one of few long records the characters, a slow or distant
#: embedding server the clock.
#:
#: The volume bound counts characters, not tokens. The divider is offline by
#: design (it asks no model and fetches no token report), which is the same
#: reason the table has no token_count column: a bound only enforceable by
#: fetching a token report would put a network call in front of the decision not
#: to make one.
#:
#: Every bound is checked before a record is started, never inside one — a
#: half-built record is not a state this design has (§6) — so a run can overshoot
#: by the last record it began. A run that has not yet started any record starts
#: the one in front of it whatever its size: otherwise a record larger than a
#: single bound would stop every run at the same place, and the sweep would never
#: get past it.
BACKFILL_RECORD_CAP = 500
BACKFILL_CHAR_CAP = 500_000
BACKFILL_REQUEST_CAP = 250
BACKFILL_SECONDS = 120.0


async def _backfill_pending(db) -> bool:
    """Whether a sweep is already queued.

    Global on purpose, and the three reads of this sweep say so with the typed
    no-filter helper rather than by omitting a predicate: a backfill belongs to
    no agent — it is the server rebuilding its own derived index — and the rows
    it writes carry their parent's axes for the retrieval path to filter on.
    """
    iso = isolation_where(agent_id=None)
    rows = await db.execute_fetchall(
        f"SELECT 1 FROM pending_memory_tasks WHERE task_type = ?{iso.and_clause} LIMIT 1",
        (BACKFILL_TASK_TYPE, *iso.params),
    )
    return bool(rows)


async def _enqueue_backfill(payload: dict) -> bool:
    """Put one sweep on the queue. False when there is no queue to put it on."""
    queue = tasks._task_queue
    if queue is None:
        return False
    try:
        await queue.enqueue(BACKFILL_TASK_TYPE, "", payload)
    except Exception as e:
        # Same stance as queue_build: the corpus is unchanged and every path that
        # existed before still answers, so a failure to schedule derived work is
        # logged rather than raised at whoever happened to be starting up.
        logger.warning("could not queue the block backfill: %s", e)
        return False
    return True


async def queue_backfill() -> bool:
    """Schedule a sweep of the existing corpus. True when one was queued.

    Called once per process start, which is what "turning construction on starts
    a bounded backfill" means in practice (§7): the records a deployment already
    had are built without an operator asking for them, a bounded run at a time.

    One sweep at a time, and the check is the queue's own table rather than a
    flag in this process: a sweep a restart interrupted is still queued, and a
    second one would re-divide the same records to arrive at the same rows. Off
    means no sweep at all, rather than a sweep that starts and finds the gate
    closed.
    """
    if not building_enabled():
        return False
    async with connection() as db:
        if await _backfill_pending(db):
            logger.info("block backfill: a sweep is already queued")
            return False
    return await _enqueue_backfill({})


def _resume_point(payload: object) -> tuple[int, int] | None:
    """Where this run starts: (index into BACKFILL_KINDS, last id finished there).

    An empty payload is the start of the corpus, which is what a fresh sweep
    enqueues. None is a payload this module did not write, and the caller
    discards it rather than rounding it to the beginning: a sweep that quietly
    restarted on a cursor it could not read would do the whole corpus again and
    report it as a resumption.
    """
    if not isinstance(payload, dict):
        return None
    if not payload:
        return (0, 0)
    kind = payload.get("kind")
    after_id = payload.get("after_id")
    if kind not in BACKFILL_KINDS or not isinstance(after_id, int) or isinstance(after_id, bool):
        return None
    return (BACKFILL_KINDS.index(kind), after_id)


async def _page(db, kind: str, after_id: int, limit: int) -> list[tuple[int, str]]:
    """One page of records after the cursor, in the order the cursor advances."""
    table, column = PARENT_TEXT[kind]
    iso = isolation_where(agent_id=None)
    rows = await db.execute_fetchall(
        f"SELECT id, {column} FROM {table} WHERE id > ?{iso.and_clause} ORDER BY id LIMIT ?",
        (after_id, *iso.params, limit),
    )
    return [(row[0], row[1] or "") for row in rows]


async def _sets_for(db, kind: str, ids: list[int], keys: tuple[str, str]) -> dict[int, tuple]:
    """The stored block sets of one page, keyed by parent id.

    One aggregate over a range of the primary key rather than a query per record.
    The page is bounded, so the range is, and the sweep's cost stays in the
    embedding calls, where it belongs.
    """
    if not ids:
        return {}
    rows = await db.execute_fetchall(
        f"SELECT b.parent_id, {_SET_AGGREGATE}"
        "WHERE b.parent_kind = ? AND b.parent_id BETWEEN ? AND ? GROUP BY b.parent_id",
        (*keys, kind, ids[0], ids[-1]),
    )
    return {row[0]: tuple(row[1:]) for row in rows}


async def coverage(db, keys: tuple[str, str]) -> tuple[int, int]:
    """(records in the corpus, records holding blocks from ``model``).

    The second is deliberately the weaker claim: it counts the records that hold
    blocks this model produced, not the records whose set was checked against the
    text it must span. That check is the sweep's own work, record by record, and
    repeating it here would be a second pass over the corpus for a number that is
    already out of date by the time it is logged.

    The gap between the two readings is narrow in practice — the triggers drop a
    record's blocks when the text they quote changes, and a set is written whole
    or not at all — but it is a gap, and the number is reported as what it is.

    A distinct count over both key columns, not over parent_id alone: ids are
    unique within a kind, so counting parent_id by itself would fold memory 5 and
    episode 5 into one record and report a corpus smaller than it is.
    """
    total = 0
    iso = isolation_where(agent_id=None)
    for kind in BACKFILL_KINDS:
        table, _ = PARENT_TEXT[kind]
        rows = await db.execute_fetchall(
            f"SELECT COUNT(*) FROM {table} WHERE 1=1{iso.and_clause}", iso.params
        )
        total += rows[0][0]
    rows = await db.execute_fetchall(
        "SELECT COUNT(*) FROM (SELECT DISTINCT parent_kind, parent_id FROM record_blocks "
        "WHERE embedding_model IN (?, ?))",
        keys,
    )
    return total, rows[0][0]


def _bound_reached(processed: int, characters: int, requests: int, deadline: float) -> str | None:
    """Which bound stopped this run, or None while it may start another record."""
    if processed >= BACKFILL_RECORD_CAP:
        return "the record cap"
    if characters >= BACKFILL_CHAR_CAP:
        return "the character cap"
    if requests >= BACKFILL_REQUEST_CAP:
        return "the embedding-request cap"
    if time.monotonic() >= deadline:
        return "the time limit"
    return None


async def _build_one(kind: str, row_id: int, text: str) -> tuple[str, int, int]:
    """Divide and write one record's blocks. Returns (outcome, requests, blocks).

    The outcomes are "built", "none" (the division is the record, so there is
    nothing a row would add), "refused" (the record moved while the build was in
    flight, and the compare-and-swap declined the set), and "failed".

    A failure is counted and passed over rather than raised: one unreachable
    record must not cost the run the rest of the page, and the record is still a
    candidate the next time the sweep reaches it. The run as a whole raises only
    if it managed nothing else, which is the shape of a backend being down rather
    than a record being awkward.
    """
    async with connection() as db:
        node_bounds = await _node_bounds(db, kind, row_id)
    try:
        prepared = await prepare_blocks(kind, row_id, text, node_bounds)
    except Exception as e:
        logger.warning("block backfill: %s:%s could not be built: %s", kind, row_id, e)
        return "failed", 0, 0
    if prepared is None:
        return "none", 0, 0
    spent = -(-len(prepared.spans) // _EMBED_BATCH)
    async with transaction() as db:
        if not await write_blocks(db, prepared):
            return "refused", spent, 0
    return "built", spent, len(prepared.spans)


async def backfill(payload: dict) -> str:
    """Build blocks for the records that were stored before construction was on.

    Returns what happened, for the queue's log. Bounded by the caps above,
    resumable through the continuation it queues for itself, and idempotent: a
    record whose set is already current is passed over without an embedding call,
    so a sweep that runs a second time costs the reads and nothing more.

    The revision each record is built against is re-checked at the write, by the
    same compare-and-swap the queued build uses — a cursor says where to carry on,
    and says nothing about what the records there said when the run began.
    """
    if not config.BLOCK_BUILD_ENABLED:
        # The same re-check the queued build does, for the same reason: a sweep
        # enqueued while the gate was open must not write rows after it closed.
        return "block building is disabled"
    start = _resume_point(payload)
    if start is None:
        return "malformed payload, discarded"
    kind_index, after_id = start
    keys = generation.block_keys()
    deadline = time.monotonic() + BACKFILL_SECONDS

    built = blocks_written = needed_none = refused = failed = 0
    characters = requests = 0
    cursor = (BACKFILL_KINDS[kind_index], after_id)
    stopped_by: str | None = None

    for kind in BACKFILL_KINDS[kind_index:]:
        page_after = after_id if kind == BACKFILL_KINDS[kind_index] else 0
        cursor = (kind, page_after)
        while stopped_by is None:
            async with connection() as db:
                page = await _page(db, kind, page_after, _BACKFILL_PAGE)
                sets = await _sets_for(db, kind, [row_id for row_id, _ in page], keys)
            if not page:
                break
            for row_id, text in page:
                # No row in the aggregate means no blocks, which the predicate
                # reads as a count of zero rather than as an absence it has to
                # have a second answer for.
                stored = sets.get(row_id, (0, None, None, 0))
                if text and not _set_is_current(*stored, len(text)):
                    processed = built + needed_none + refused + failed
                    if processed:
                        stopped_by = _bound_reached(processed, characters, requests, deadline)
                        if stopped_by is not None:
                            # Before the record, so the cursor still names the last
                            # one this run finished and the continuation starts here.
                            break
                    outcome, spent, wrote = await _build_one(kind, row_id, text)
                    characters += len(text)
                    requests += spent
                    if outcome == "built":
                        built += 1
                        blocks_written += wrote
                    elif outcome == "none":
                        needed_none += 1
                    elif outcome == "refused":
                        refused += 1
                    else:
                        failed += 1
                page_after = row_id
                cursor = (kind, row_id)
            if stopped_by is None and time.monotonic() >= deadline:
                # The clock is the one bound a page of records that all turn out
                # to be current can still reach, and it is checked here as well
                # as before a build for exactly that case: a swept corpus is read
                # a page at a time, and a run that never finds work would
                # otherwise hold the queue for as long as the reads take.
                stopped_by = "the time limit"
        if stopped_by is not None:
            break

    if failed and not (built or needed_none or refused):
        # Nothing but failures: the shape of an embedding backend that is not
        # answering rather than of records that are awkward. Raised so the queue's
        # retry path waits and tries this same cursor again, instead of reporting
        # a successful run that did nothing and queueing another just like it.
        raise RuntimeError(f"block backfill: {failed} records failed and none were built")

    async with connection() as db:
        total, held = await coverage(db, keys)
    report = (
        f"built {built} records ({blocks_written} blocks) in {requests} embedding requests; "
        f"{needed_none} needed none, {refused} moved under the build, {failed} failed; "
        f"corpus {total} records, {held} hold blocks from this model"
    )
    if stopped_by is None:
        return f"backfill swept the corpus: {report}"
    await _enqueue_backfill({"kind": cursor[0], "after_id": cursor[1]})
    return f"backfill stopped at {stopped_by} after {cursor[0]}:{cursor[1]}: {report}"


# --------------------------------------------------------------------------------------
# retrieval: Hamming candidates, collapsed to their parent
# --------------------------------------------------------------------------------------

#: What one recall may examine. Server policy (§7), fixed here and derived from
#: nothing the caller asks for — invariant 5 is that changing the response count
#: alone leaves the candidate id set unchanged, and a cap read off the count
#: would break it.
#:
#: The examined cap is above the whole index of the deployment this was measured
#: on (107,428 rows scanning in about 9 ms), so it bounds a corpus that has grown
#: rather than shaping today's answers. When it does bind it truncates in primary
#: key order, which is the order the rows are read in; that is arbitrary with
#: respect to the query, and the ceiling it defends against is the scan going
#: unbounded rather than a claim that the surviving set is the best one.
BLOCK_EXAMINED_CAP = 250_000

#: How much of that cap one record may take. A record divides into 19.9 blocks
#: on average and into far more than that at the tail, so without this a handful
#: of long records could fill the examined set on their own.
BLOCK_PER_PARENT_CAP = 64

#: How many of the examined rows, in Hamming order, are re-ranked by their stored
#: vector (§4b). Server policy, derived from nothing the caller asks for, like
#: the two caps above.
#:
#: Chosen from a measured curve, not tuned to it: over the pre-registered query
#: families the reservation's reach rose from 57 to 78 of 195 and from 64 to 81
#: of 189 at this depth, which is within one query of re-ranking every examined
#: row by its vector. The curve is flat from about 50 upwards, so the depth is a
#: bound on how many rows are read per recall rather than a knob with an optimum
#: near it.
BLOCK_RERANK_DEPTH = 200

#: How many result places block hits may fill (§5). A configured bound with a
#: conservative default, not a tuned parameter: tuning it needs a reader-based
#: measurement, which belongs to the precision step. It is an upper bound on
#: what a bad block hit can cost, not a claim that block hits are good.
BLOCK_RESERVATION = 2

#: Population count of every byte value, built once and kept. uint16 so a row's
#: sum cannot wrap. `numpy.bitwise_count` would say the same thing and arrived
#: in numpy 2.0, which this project does not require; one implementation that
#: works on both is better than two that have to agree.
#:
#: Built on first use rather than at import, because numpy is imported inside
#: the functions that need it everywhere else in this package and a module-level
#: table here would pull it into every process that imports this module.
_POPCOUNT = None


def _popcount_table():
    global _POPCOUNT
    if _POPCOUNT is None:
        import numpy as np

        _POPCOUNT = (
            np.unpackbits(np.arange(256, dtype=np.uint8)[:, None], axis=1)
            .sum(axis=1)
            .astype(np.uint16)
        )
    return _POPCOUNT


def retrieval_enabled() -> bool:
    """Whether the block arm may run during recall."""
    return config.BLOCK_RETRIEVAL_ENABLED


@dataclass(frozen=True)
class BlockHit:
    """One record, represented by its best-matching block (§4).

    A record several of whose blocks match is one hit, not several, and the
    distances of its other blocks are not added to this one: they are correlated
    observations of a single source, and summing them would turn the length of a
    record into evidence.
    """

    kind: str
    parent_id: int
    block_index: int
    distance: int
    #: The cosine between the query and this block's stored vector, when the
    #: re-rank ordered the hits (§4b). None when they are in Hamming order.
    cosine: float | None = None


async def _examined(db, iso, keys: tuple[str, str]) -> list[tuple]:
    """The block rows one call may look at, in the order the caps truncate.

    The per-parent cap is applied before the examined cap, so a long record
    spends at most its share and the cap cannot be consumed by one parent. The
    isolation axes on the row filter here rather than after ranking: a bucket
    that is one per cent of the corpus would otherwise spend the whole cut on
    rows the authority then drops.
    """
    return await db.execute_fetchall(
        "SELECT parent_kind, parent_id, block_index, embedding_bits FROM ("
        "  SELECT parent_kind, parent_id, block_index, embedding_bits,"
        "         ROW_NUMBER() OVER ("
        "             PARTITION BY parent_kind, parent_id ORDER BY block_index"
        "         ) AS within_parent"
        "    FROM record_blocks"
        f"   WHERE embedding_bits IS NOT NULL AND embedding_model IN (?, ?){iso.and_clause}"
        ") WHERE within_parent <= ? "
        "ORDER BY parent_kind, parent_id, block_index LIMIT ?",
        (*keys, *iso.params, BLOCK_PER_PARENT_CAP, BLOCK_EXAMINED_CAP),
    )


def _hamming(rows: list[tuple], query_bits: bytes) -> list[BlockHit]:
    """Rank the examined rows by Hamming distance, collapsed to their parent.

    Rows whose bit string is a different width are skipped rather than compared:
    a different width is a different dimension, and a distance between the two
    would be a number with no meaning rather than a large one.

    The order is total and written down (invariant 3): distance, then kind, then
    parent id, then block index. Nothing here consults the response count.
    """
    import numpy as np

    width = len(query_bits)
    usable = [row for row in rows if row[3] is not None and len(row[3]) == width]
    if not usable:
        return []
    packed = np.frombuffer(b"".join(row[3] for row in usable), dtype=np.uint8).reshape(
        len(usable), width
    )
    query = np.frombuffer(query_bits, dtype=np.uint8)
    distances = _popcount_table()[np.bitwise_xor(packed, query)].sum(axis=1)
    best: dict[tuple[str, int], BlockHit] = {}
    for row, distance in zip(usable, distances):
        kind, parent_id, block_index = row[0], row[1], row[2]
        key = (kind, parent_id)
        hit = BlockHit(kind, parent_id, int(block_index), int(distance))
        held = best.get(key)
        if held is None or (hit.distance, hit.block_index) < (held.distance, held.block_index):
            best[key] = hit
    return sorted(best.values(), key=lambda h: (h.distance, h.kind, h.parent_id, h.block_index))


def _hamming_order(rows: list[tuple], query_bits: bytes) -> list[tuple[tuple, int]]:
    """The first :data:`BLOCK_RERANK_DEPTH` usable rows in the total order of
    :func:`_hamming` — distance, kind, parent id, block index — with their distance,
    before any collapse to the parent.

    The depth is cut on rows, not parents, because a row is what has a vector to
    read. Ties at the cut are settled by the same written-down order, so the set
    re-ranked is a function of the database and the query alone.
    """
    import numpy as np

    width = len(query_bits)
    usable = [row for row in rows if row[3] is not None and len(row[3]) == width]
    if not usable:
        return []
    packed = np.frombuffer(b"".join(row[3] for row in usable), dtype=np.uint8).reshape(
        len(usable), width
    )
    query = np.frombuffer(query_bits, dtype=np.uint8)
    distances = _popcount_table()[np.bitwise_xor(packed, query)].sum(axis=1)
    depth = min(BLOCK_RERANK_DEPTH, len(usable))
    # Every row no farther than the depth-th distance, then the full order over
    # that handful: sorting the whole examined set in Python to keep 200 of it
    # would cost more than the scan.
    cut = int(np.partition(distances, depth - 1)[depth - 1])
    near = [(usable[j], int(distances[j])) for j in np.flatnonzero(distances <= cut)]
    near.sort(key=lambda rd: (rd[1], rd[0][0], rd[0][1], rd[0][2]))
    return near[:depth]


async def _stored_vectors(db, keys: list[tuple[str, int, int]]) -> dict[tuple, bytes]:
    """The re-rank vectors of ``keys``, by (kind, parent id, block index)."""
    if not keys:
        return {}
    marks = ", ".join("(?, ?, ?)" for _ in keys)
    rows = await db.execute_fetchall(
        "SELECT parent_kind, parent_id, block_index, embedding_i8 FROM record_block_vectors "
        f"WHERE (parent_kind, parent_id, block_index) IN (VALUES {marks})",
        [value for key in keys for value in key],
    )
    return {(r[0], r[1], r[2]): r[3] for r in rows}


def _rerank(
    near: list[tuple[tuple, int]], stored: dict[tuple, bytes], embedding: object
) -> list[BlockHit] | None:
    """The rows the Hamming pass ranked highest, ordered by the cosine of their
    stored vector and collapsed to their parent. None when any of them has no
    usable vector.

    All or nothing, because a cosine and a Hamming distance cannot be put in one
    order: a row with a vector would be compared on one scale and a row without
    on another. A set built before the vectors existed is found not current and
    rebuilt (see :data:`_SET_AGGREGATE`), so the fallback is the state of a
    deployment part-way through that rebuild, and the answer it gives is the one
    the previous release gave.

    The parent takes its best block's cosine and nothing is summed, for the
    reason :class:`BlockHit` gives. The order is written down: cosine descending,
    then kind, parent id and block index. Parents none of whose blocks made the
    depth are not candidates.
    """
    import numpy as np

    if not near:
        return []
    width = len(near[0][0][3]) * 8
    query = np.asarray(embedding, dtype=np.float32)
    if query.shape != (width,):
        return None
    blobs = [stored.get((row[0], row[1], row[2])) for row, _ in near]
    if any(blob is None or len(blob) != width for blob in blobs):
        return None
    matrix = np.frombuffer(b"".join(blobs), dtype=np.int8).reshape(len(blobs), width)
    matrix = matrix.astype(np.float32)
    norms = np.linalg.norm(matrix, axis=1) * float(np.linalg.norm(query))
    if not np.all(norms > 0):
        return None
    cosines = (matrix @ query) / norms
    best: dict[tuple[str, int], BlockHit] = {}
    for (row, distance), cosine in zip(near, cosines):
        kind, parent_id, block_index = row[0], row[1], row[2]
        hit = BlockHit(kind, parent_id, int(block_index), distance, float(cosine))
        held = best.get((kind, parent_id))
        if held is None or (-hit.cosine, hit.block_index) < (-held.cosine, held.block_index):
            best[(kind, parent_id)] = hit
    return sorted(best.values(), key=lambda h: (-h.cosine, h.kind, h.parent_id, h.block_index))


async def search(db, embedding: object, iso) -> list[BlockHit]:
    """Every record the block index reaches for this query, best block first.

    Best by the stored vector of the rows the Hamming pass ranked highest (§4b),
    or by Hamming distance over every examined row when any of those rows has no
    vector yet — never a mixture of the two.

    ``iso`` is the caller's isolation predicate over the axes copied onto each
    row. Those copies are not a second authority — what this returns must be a
    superset of what the authority admits, and the hydrate re-applies the real
    predicate fail-closed — but filtering here is what keeps the cap from being
    spent on rows that will be dropped.

    Returns an empty list rather than raising when the query cannot be
    quantised: a recall whose other arms answered must not fail because a
    derived one could not.
    """
    query_bits = pack_bits(embedding)
    if query_bits is None:
        # Said out loud rather than returned as "no candidates": an arm that
        # quietly finds nothing is indistinguishable from an index that holds
        # nothing, and the deployment that turned this on would read the second.
        logger.warning("block arm: the query vector could not be quantised, skipping")
        return []
    rows = await _examined(db, iso, generation.block_keys())
    near = _hamming_order(rows, query_bits)
    stored = await _stored_vectors(db, [(row[0], row[1], row[2]) for row, _ in near])
    reranked = _rerank(near, stored, embedding)
    if reranked is not None:
        return reranked
    return _hamming(rows, query_bits)


# --------------------------------------------------------------------------------------
# quotation: a block, and the contiguous context that governs it
# --------------------------------------------------------------------------------------

#: The widest context a quoted block may carry, in characters.
#:
#: Twice the forced-boundary limit: two blocks of the largest size the divider
#: will produce is the widest the rule below can need before the window that
#: closes an embedding (1,787 characters on this corpus) is in sight, and it
#: keeps a quoted block plus its context smaller than the node it sits in —
#: which is the point of quoting a block at all.
#:
#: Server policy, and deliberately not a budget. The context a block carries is
#: decided before anything is cut, so raising the payload budget adds items and
#: excerpts and never replaces a quotation with a different one.
BLOCK_CONTEXT_CHARS = MAX_BLOCK_CHARS * 2

#: Openers that make a block depend on the block beside it. A block that begins
#: with one of these qualifies what came before; a block followed by one is
#: qualified by what comes after. Quoting either half alone can say the opposite
#: of the text it was taken from.
#:
#: A marked dependency is the case this can detect. An unmarked one — a
#: correction in the next sentence that announces itself only by its content —
#: is not, and this does not claim otherwise: the rule below is conservative
#: about what it can see, and section 9 of the design says what it does not
#: cover.
_QUALIFIERS: tuple[str, ...] = (
    "ただし", "但し", "ただ、", "しかし", "しかしながら", "だが", "ですが", "でも、",
    "なお", "とはいえ", "一方", "他方", "その代わり", "代わりに", "except",
    "however", "but ", "although", "though", "unless", "instead", "on the other hand",
    "that said", "caveat", "note that", "provided that", "in fact", "actually",
)

#: Sentence terminators, for deciding whether a block ends one. The full-width
#: marks end a sentence wherever they stand; the half-width ones only before
#: whitespace or at the end of the text, which is the divider's own rule.
_TERMINATORS = ("。", "！", "？", "．", ".", "!", "?", "…")


def text_revision(text: str) -> str:
    """A short digest of the text an offset was measured in.

    Not a version number: nothing increments, and two records holding the same
    text share it. What it is for is refusal — a span handed back later can be
    checked against the text as it stands, and a record rewritten since is an
    `unresolved` answer rather than a different passage served under the same
    offsets. A block or node range needs no such check, because the triggers
    delete a record's derived rows when its text changes; a raw span has nothing
    equivalent, which is why the one place that hands a span back attaches this.
    """
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _starts_with_qualifier(fragment: str) -> bool:
    stripped = fragment.lstrip()
    lowered = stripped.lower()
    return any(
        lowered.startswith(q) if q.isascii() else stripped.startswith(q) for q in _QUALIFIERS
    )


def _ends_a_sentence(fragment: str) -> bool:
    stripped = fragment.rstrip()
    return bool(stripped) and stripped.endswith(_TERMINATORS)


def context_range(
    text: str, spans: list[tuple[int, int]], index: int, max_chars: int = 0
) -> tuple[int, int, bool]:
    """The span to quote for block ``index``, and whether the rule was satisfied.

    ``spans`` are the record's block spans in order, which partition its text.
    Returns (start, end, complete). ``complete`` is False when the rule wanted
    more context than ``max_chars`` allowed — the caller reports that rather
    than presenting a severed quotation as whole evidence.

    Two conservative rules, both in whole blocks, applied until neither fires:

    1. **Finish the sentence.** A block that does not begin one is extended
       backwards, and a block that does not end one is extended forwards. The
       divider cuts at structure and sometimes inside a sentence, so a block can
       be a clause; a clause read without its sentence is the first way a
       quotation goes wrong.
    2. **Follow the qualifier.** A block beginning with a word that qualifies
       what came before is extended backwards, and a block followed by one is
       extended forwards. This is the case where the sentences are each complete
       and the second reverses the first.
    """
    limit = max_chars if max_chars > 0 else BLOCK_CONTEXT_CHARS
    first = last = index
    complete = True
    while True:
        start, end = spans[first][0], spans[last][1]
        wants_before = first > 0 and (
            not _ends_a_sentence(text[spans[first - 1][0] : spans[first - 1][1]])
            or _starts_with_qualifier(text[start:end])
        )
        wants_after = last + 1 < len(spans) and (
            not _ends_a_sentence(text[start:end])
            or _starts_with_qualifier(text[spans[last + 1][0] : spans[last + 1][1]])
        )
        if not wants_before and not wants_after:
            return start, end, complete
        # Backwards first, so a block that qualifies what came before never
        # returns the qualification without the thing qualified.
        if wants_before:
            candidate = spans[first - 1][0]
            if end - candidate > limit:
                return start, end, False
            first -= 1
            continue
        candidate = spans[last + 1][1]
        if candidate - start > limit:
            return start, end, False
        last += 1
