"""The coverage ledger (2.6, docs/RECALL_PROCESS_DESIGN.md §1.5).

On a traced call, which parts of the question the returned records hold, and which
records hold each part. It is recorded and nothing reads it: the recall returns
what it returned before. It exists so that a question part no returned record
holds can be counted from real traffic, which is the gap a later stage of the
recall process would go and fetch.

A part is a word of the question, cut deterministically by script with no
dependency (the server has no word segmenter): a run of two or more kanji, a run
of two or more katakana, or an ASCII identifier of two or more characters
(names, version numbers such as 2.5.5a1, bug-218, #354, paths). Hiragana runs are
particles and endings and are not parts. The question is normalized first (NFKC,
lower case); a part repeated in the question is counted once.

Rare character trigrams were tried first and rejected: in Japanese a rare
trigram is usually a fragment across a word boundary. On questions whose
evidence was returned, 8% of such parts were found in the returned records on
average, against 82% for words taken this way.

Like the rest of the trace, the ledger carries no text. A part is its span in the
normalized question and its kind; a record is its ref. A caller that holds the
question can recover each part as normalize(question)[start:end].
"""
from __future__ import annotations

import re
import unicodedata

from .database import connection

NORMALIZATION = "nfkc-lower"

# A question longer than this many distinct parts is rare, and the check is a
# substring test per part and record, so the bound keeps the cost fixed.
MAX_PARTS = 32

_PART = re.compile(
    r"(?P<identifier>#\d+|[a-z0-9](?:[a-z0-9._/#-]*[a-z0-9])?)"
    r"|(?P<katakana>[\u30a0-\u30ff]{2,})"
    r"|(?P<kanji>[\u4e00-\u9fff\u3005]{2,})"
)


def normalize(text: str | None) -> str:
    return unicodedata.normalize("NFKC", text or "").lower()


def parts(normalized_query: str) -> list[tuple[int, int, str]]:
    """(start, end, kind) of each distinct part, in order of first appearance."""
    seen: set[str] = set()
    out: list[tuple[int, int, str]] = []
    for m in _PART.finditer(normalized_query):
        word = m.group(0)
        if len(word) < 2 or word in seen:
            continue
        seen.add(word)
        out.append((m.start(), m.end(), m.lastgroup))
    return out


def ledger(query: str, records: list[tuple[str, str]]) -> dict:
    """The ledger for `query` over `records`, a list of (ref, stored text).

    `covered_by[i]` lists, in the order `records` gives them, the refs whose text
    holds part i; `uncovered` lists the parts no record holds.
    """
    q = normalize(query)
    found = parts(q)
    kept = found[:MAX_PARTS]
    texts = [(ref, normalize(text)) for ref, text in records]
    covered_by = [[ref for ref, text in texts if q[s:e] in text] for s, e, _ in kept]
    out = {
        "normalization": NORMALIZATION,
        "parts": [{"span": [s, e], "kind": kind} for s, e, kind in kept],
        "covered_by": covered_by,
        "uncovered": [i for i, refs in enumerate(covered_by) if not refs],
        "records": len(texts),
    }
    if len(found) > len(kept):
        out["parts_omitted"] = len(found) - len(kept)
    return out


async def stored_texts(agent_id: str, refs: list[str]) -> dict[str, str]:
    """The full stored text of each `mem:` / `ep:` ref this agent owns.

    The refs come from a recall this agent just made; the ownership predicate is
    applied anyway, as every id-keyed read does. A ref that is not a memory or an
    episode (the profile) is not loaded.
    """
    ids: dict[str, list[int]] = {"mem": [], "ep": []}
    for ref in refs:
        kind, _, raw = ref.partition(":")
        if kind in ids and raw.isdigit():
            ids[kind].append(int(raw))
    out: dict[str, str] = {}
    async with connection() as db:
        for kind, table, column in (("mem", "memories", "content"), ("ep", "episodes", "summary")):
            if not ids[kind]:
                continue
            marks = ",".join("?" * len(ids[kind]))
            rows = await db.execute_fetchall(
                f"SELECT id, {column} FROM {table} WHERE agent_id = ? AND id IN ({marks})",
                (agent_id, *ids[kind]),
            )
            for row_id, text in rows:
                out[f"{kind}:{row_id}"] = text or ""
    return out


async def for_refs(agent_id: str, query: str, refs: list[str]) -> dict:
    """The ledger over the records `refs` names, each loaded in full, in `refs` order."""
    ordered = list(dict.fromkeys(r for r in refs if isinstance(r, str)))
    texts = await stored_texts(agent_id, ordered)
    return ledger(query, [(ref, texts[ref]) for ref in ordered if ref in texts])
