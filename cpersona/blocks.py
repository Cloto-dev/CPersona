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
corpus that is 75 blocks out of 105,975.
"""

import re
from dataclasses import dataclass

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
