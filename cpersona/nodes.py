"""Overflow tree nodes: dividing a long record, and building its nodes on the queue.

docs/OVERFLOW_TREE_DESIGN.md §2 (division), §3 (when nodes are built) and §5
(invariants). The table and the triggers that keep nodes true to their parent are
in database.py (RECORD_NODES_SQL).

Three layers, each testable without the next:

- ``choose_cut`` is pure: where to end one node, given the text and the position
  at which the embedding window closes.
- ``divide`` repeats it over a whole text against an async measure (the embedding
  server's token report), and re-measures each span it cuts, so a node's
  ``token_count`` is counted on the node itself rather than inferred.
- ``build_nodes`` is the queue task: read the parent, divide, embed every span,
  and write the nodes only if the parent still holds the text that was divided.

Nodes are never read by any retrieval path. Nothing in this module writes to
``memories`` or ``episodes``.
"""

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from cpersona import config, tasks, vector
from cpersona.database import connection, transaction

logger = logging.getLogger(__name__)

#: The two parent kinds and the column that holds each one's text.
PARENT_TEXT = {"mem": ("memories", "content"), "ep": ("episodes", "summary")}

#: The queue task type that builds one record's nodes.
TASK_TYPE = "build_nodes"

#: Spans per embedding request while building one record's nodes. The longest
#: stored record (16,000 characters) divides into about fourteen nodes, so this is
#: one request for every record the store limit admits today.
_EMBED_BATCH = 32

# Boundary classes in the order §2 takes them. Each pattern's match END is the cut:
# the separator stays with the node before it, so the nodes still partition the text.
#
# Sentence ends: the full-width marks end a sentence wherever they stand, because
# Japanese and Chinese prose puts the next sentence directly after them. The
# half-width marks count only before whitespace or the end of the text, so a decimal
# point, a version number or a file extension is not read as a sentence end.
_BOUNDARY_CLASSES: tuple[re.Pattern, ...] = (
    re.compile(r"\n[ \t]*\n"),  # blank line (paragraph)
    re.compile(r"\n"),  # line break (line or list item)
    re.compile(r"[。．！？]|[.!?](?=\s|$)"),  # sentence end
    re.compile(r"\s"),  # whitespace
)


def choose_cut(text: str, window_end: int) -> int:
    """Where the node that starts at ``text[0]`` ends, as an exclusive offset.

    ``window_end`` is the position at which the embedding window closes on
    ``text`` (``text[:window_end]`` is what the model would embed). The cut is the
    last boundary at or before it, taking the classes in order, and a class whose
    last boundary would leave the node shorter than half of ``window_end`` is
    passed over for the next one. With no acceptable boundary the cut is
    ``window_end`` itself.

    Always in ``[1, window_end]``: every node has at least one character.
    """
    if window_end < 1:
        raise ValueError(f"window_end must be at least 1, got {window_end}")
    window_end = min(window_end, len(text))
    for pattern in _BOUNDARY_CLASSES:
        best = 0
        # Searched over the whole text, not text[:window_end]: bounding the search
        # would let `$` match at the window and read "3." in "3.14" as a sentence end.
        for match in pattern.finditer(text):
            if match.end() > window_end:
                break
            best = match.end()
        if best and best * 2 >= window_end:
            return best
    return window_end


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    token_count: int
    window: int


#: Measures one text: returns the embedding server's report for it, or None when
#: the report is unavailable. Shaped like ``EmbeddingClient.count_tokens`` for one text.
Measure = Callable[[str], Awaitable[object]]


class TokensUnknown(Exception):
    """The token report was unavailable part-way through a division."""


async def divide(text: str, measure: Measure) -> list[Span]:
    """Divide ``text`` into spans that each fit the embedding window (§2).

    Returns one span covering the whole text when the text already fits. Raises
    ``TokensUnknown`` when any measurement comes back empty, because a division
    built on a guess about the tokens is exactly what §2 rules out.

    Each span is measured on its own text before it is accepted. Cutting a text
    can change how its last characters tokenize, so the position the report gave
    for the longer text is where the search starts, not a promise about the span.
    When a span still runs past the window, the search continues from the span's
    own report, which strictly shrinks the candidate, so the loop ends.
    """
    spans: list[Span] = []
    pos = 0
    while pos < len(text):
        rest = text[pos:]
        info = await measure(rest)
        if info is None:
            raise TokensUnknown(f"no token report at offset {pos}")
        if not info.truncated:
            spans.append(Span(pos, len(text), info.count, info.window))
            break
        limit = info.window_end_char
        while True:
            if limit < 1:
                raise TokensUnknown(f"the window closes before the first character at offset {pos}")
            cut = choose_cut(rest, limit)
            span_info = await measure(rest[:cut])
            if span_info is None:
                raise TokensUnknown(f"no token report for the span at offset {pos}")
            if not span_info.truncated:
                spans.append(Span(pos, pos + cut, span_info.count, span_info.window))
                pos += cut
                break
            limit = min(span_info.window_end_char, cut - 1)
    return spans


def _client_measure(client) -> Measure:
    async def measure(text: str):
        infos = await client.count_tokens([text])
        return infos[0] if infos else None

    return measure


# --------------------------------------------------------------------------------------
# write side: deciding at store time, and queueing
# --------------------------------------------------------------------------------------


def building_enabled() -> bool:
    """Whether a write may queue node construction at all.

    Needs a running task queue (§3: with the queue disabled nothing is built at
    write time) and an embedding client that can report tokens.
    """
    client = vector._embedding_client
    return (
        tasks._task_queue is not None
        and client is not None
        and callable(getattr(client, "count_tokens", None))
    )


async def runs_past_window(text: str) -> bool:
    """Whether ``text`` runs past the embedding window. False when not known.

    Unknown is never read as "fits" for the purpose of building (that is the
    task's own check); here it only means no build is queued, so the record is
    quoted from its start, as it is without this feature. Never raises: a write
    must not fail because the token report did.
    """
    if not building_enabled():
        return False
    try:
        infos = await vector._embedding_client.count_tokens([text])
    except Exception as e:  # the report is advisory to the write path
        logger.warning("token report failed at write time, no nodes queued: %s", e)
        return False
    return bool(infos) and bool(infos[0].truncated)


async def queue_build(kind: str, parent_id: int, agent_id: str, session_key: str) -> dict | None:
    """Queue one record's node construction. Returns the response field, or None.

    None when the enqueue itself failed: the record is stored, it is quoted from
    its start, and the health check finds it without nodes. A write that
    succeeded does not report a failure it cannot act on.
    """
    queue = tasks._task_queue
    if queue is None:
        return None
    try:
        await queue.enqueue(TASK_TYPE, agent_id, {"kind": kind, "id": parent_id}, session_key=session_key)
    except Exception as e:
        logger.warning("could not queue nodes for %s:%s: %s", kind, parent_id, e)
        return None
    return {"status": "queued"}


# --------------------------------------------------------------------------------------
# the queue task
# --------------------------------------------------------------------------------------


async def _parent_text(db, kind: str, parent_id: int) -> str | None:
    table, column = PARENT_TEXT[kind]
    rows = await db.execute_fetchall(f"SELECT {column} FROM {table} WHERE id = ?", (parent_id,))
    return rows[0][0] if rows else None


async def _nodes_current(db, kind: str, parent_id: int, text_len: int, model: str) -> bool:
    rows = await db.execute_fetchall(
        "SELECT MIN(start_char), MAX(end_char), COUNT(*), SUM(embedding_model = ?) "
        "FROM record_nodes WHERE parent_kind = ? AND parent_id = ?",
        (model, kind, parent_id),
    )
    first, last, count, current = rows[0]
    return bool(count) and first == 0 and last == text_len and current == count


async def build_nodes(payload: dict) -> str:
    """Build the nodes of one record. Returns what happened, for the queue's log.

    Raises on a failure worth retrying (the token report or the embedding request
    failed); the queue's retry path handles it. Returns without writing when there
    is nothing to do: the record is gone, it fits the window, or its nodes are
    already current.
    """
    kind = payload.get("kind") if isinstance(payload, dict) else None
    parent_id = payload.get("id") if isinstance(payload, dict) else None
    if kind not in PARENT_TEXT or not isinstance(parent_id, int) or isinstance(parent_id, bool):
        return "malformed payload, discarded"
    client = vector._embedding_client
    if client is None or not callable(getattr(client, "count_tokens", None)):
        raise TokensUnknown("no embedding client with a token report")
    model = config.EMBEDDING_MODEL

    async with connection() as db:
        text = await _parent_text(db, kind, parent_id)
        if text is None:
            return "parent gone"
        if await _nodes_current(db, kind, parent_id, len(text), model):
            return "nodes already current"

    spans = await divide(text, _client_measure(client))
    if len(spans) == 1:
        # The record fits: it is its own only span and has no nodes (§2).
        return "fits the window"

    blobs: list[bytes] = []
    for start in range(0, len(spans), _EMBED_BATCH):
        batch = spans[start : start + _EMBED_BATCH]
        vectors = await client.embed([text[s.start : s.end] for s in batch])
        if not vectors or len(vectors) != len(batch):
            raise RuntimeError("embedding request returned no vectors for the node spans")
        for v in vectors:
            blob = vector.pack_for_storage(v)
            if blob is None:
                raise RuntimeError("embedding for a node span was refused for storage")
            blobs.append(blob)

    async with transaction() as db:
        # Divided and embedded outside the lock; the text may have changed or the
        # record may be gone since. The triggers only remove nodes that already
        # existed at that moment, so a stale set written now would survive. This
        # comparison is what keeps invariant 6 across the unlocked window.
        if await _parent_text(db, kind, parent_id) != text:
            return "parent changed during the build, discarded"
        await db.execute(
            "DELETE FROM record_nodes WHERE parent_kind = ? AND parent_id = ?", (kind, parent_id)
        )
        await db.executemany(
            "INSERT INTO record_nodes (parent_kind, parent_id, node_index, start_char, end_char, "
            "token_count, window, embedding, embedding_model) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (kind, parent_id, i, s.start, s.end, s.token_count, s.window, blob, model)
                for i, (s, blob) in enumerate(zip(spans, blobs))
            ],
        )
    return f"built {len(spans)} nodes"
