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
and writes what comes back as one bit per dimension.
"""

import logging
import re
import time
from dataclasses import dataclass

from cpersona import config, tasks, vector
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

    The model half is where the care is. ``current`` counts the rows produced by
    what ``config.reported_embedding_model()`` returns, which is empty when this
    process cannot learn the backend's model name. Empty compares equal to empty,
    so an unreported deployment stays consistent with itself — and never equal to
    a named model, which is the point: the resolved default must not stand in for
    an identity nobody reported (§6).
    """
    return bool(count) and first == 0 and last == text_len and current == count


async def _blocks_current(db, kind: str, parent_id: int, text_len: int, model: str) -> bool:
    """Whether this record's stored blocks were built from its current text by a
    known model. The one-record read behind :func:`_set_is_current`."""
    rows = await db.execute_fetchall(
        "SELECT COUNT(*), MIN(start_char), MAX(end_char), SUM(embedding_model = ?) "
        "FROM record_blocks WHERE parent_kind = ? AND parent_id = ?",
        (model, kind, parent_id),
    )
    count, first, last, current = rows[0]
    return _set_is_current(count, first, last, current, text_len)


@dataclass(frozen=True)
class PreparedBlocks:
    """One record's division and its quantised vectors, computed outside any
    write lock."""

    kind: str
    parent_id: int
    text: str
    spans: list[BlockSpan]
    bits: list[bytes]
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
    for start in range(0, len(spans), _EMBED_BATCH):
        batch = spans[start : start + _EMBED_BATCH]
        vectors = await client.embed([text[s.start : s.end] for s in batch])
        if not vectors or len(vectors) != len(batch):
            raise RuntimeError("embedding request returned no vectors for the block spans")
        for v in vectors:
            packed = pack_bits(v)
            if packed is None:
                raise RuntimeError("embedding for a block was refused for storage")
            bits.append(packed)
    return PreparedBlocks(kind, parent_id, text, spans, bits, config.reported_embedding_model())


async def write_blocks(db, prepared: PreparedBlocks) -> bool:
    """Replace a record's blocks inside the caller's open transaction. False if stale.

    The division was computed without a lock, so the text may have changed or
    the record may be gone since. The triggers only remove blocks that already
    existed at that moment, so a set written now from text that is no longer
    there would survive and be offered to searches as though it were true. This
    comparison is what closes the unlocked window; removing it is the mutation
    the tests are built to catch.

    The whole set is written in one statement inside the caller's transaction,
    so a crash between the delete and the inserts leaves the previous set intact
    rather than half of a new one.
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

    model = config.reported_embedding_model()
    async with connection() as db:
        text = await _parent_text(db, kind, parent_id)
        if text is None:
            return "parent gone"
        if not text:
            return "parent has no text"
        if await _blocks_current(db, kind, parent_id, len(text), model):
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


async def _sets_for(db, kind: str, ids: list[int], model: str) -> dict[int, tuple]:
    """The stored block sets of one page, keyed by parent id.

    One aggregate over a range of the primary key rather than a query per record.
    The page is bounded, so the range is, and the sweep's cost stays in the
    embedding calls, where it belongs.
    """
    if not ids:
        return {}
    rows = await db.execute_fetchall(
        "SELECT parent_id, COUNT(*), MIN(start_char), MAX(end_char), SUM(embedding_model = ?) "
        "FROM record_blocks WHERE parent_kind = ? AND parent_id BETWEEN ? AND ? "
        "GROUP BY parent_id",
        (model, kind, ids[0], ids[-1]),
    )
    return {row[0]: tuple(row[1:]) for row in rows}


async def coverage(db, model: str) -> tuple[int, int]:
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
        "WHERE embedding_model = ?)",
        (model,),
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
    model = config.reported_embedding_model()
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
                sets = await _sets_for(db, kind, [row_id for row_id, _ in page], model)
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
        total, held = await coverage(db, model)
    report = (
        f"built {built} records ({blocks_written} blocks) in {requests} embedding requests; "
        f"{needed_none} needed none, {refused} moved under the build, {failed} failed; "
        f"corpus {total} records, {held} hold blocks from this model"
    )
    if stopped_by is None:
        return f"backfill swept the corpus: {report}"
    await _enqueue_backfill({"kind": cursor[0], "after_id": cursor[1]})
    return f"backfill stopped at {stopped_by} after {cursor[0]}:{cursor[1]}: {report}"
