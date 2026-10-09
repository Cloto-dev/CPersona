"""The keyword arms on Tantivy (CPERSONA_LEXICAL_ENGINE=tantivy), a measurement path.

SQLite stays the record. The index is derived from it: built in memory on the first keyword
search of a process, then kept current from a change log that triggers write while this engine
is selected (``LEXICAL_LOG_SQL``, created only then), which a search reads in one row. It answers
with ids and scores only; the rows are read back from SQLite under the isolation authority
(``isolation_where``, ``source_id_where``), which drops anything the index should not have
returned.

The index applies agent, project and channel as the authority does, and the rare source and
window restrictions as the set of ids SQLite selects for them, so it ranks inside the
authority's set -- never a looser set cut at the limit, which would lose rows silently.

Terms are made here, not by Tantivy's tokenizers (it ships none for Japanese), by one function
for a record and a query: CPERSONA_TANTIVY_CJK cuts Japanese and Chinese runs (``k3`` trigrams,
``k2`` bigrams, ``km`` morphemes), CPERSONA_TANTIVY_ASCII the rest (``w`` words plus any token
with inner punctuation whole, ``ws`` the same stemmed, ``t`` trigrams). A query's Japanese runs
are cut as CPERSONA_QUERY_SEGMENTER says. Tantivy's Python binding scores every matching row (no
top-k pruning); what it changes is the cost per row.

A score is returned as ``_bm25 = -score``, FTS5's sign, so the fusions read it as they read FTS5.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field

from cpersona import config, query_segment
from cpersona.isolation import isolation_where, source_id_where

logger = logging.getLogger(__name__)

_CJK_CLASS = r"぀-ヿ㐀-䶿一-鿿ｦ-ﾟ"
_CJK_RE = re.compile(f"[{_CJK_CLASS}]")
_TOKEN_RE = re.compile(f"[{_CJK_CLASS}]+|[^\\s{_CJK_CLASS}]+")
_WORD_RE = re.compile(r"[^\W_]+")
_EDGE = "\"'`.,;:!?()[]{}"

# Created only while this engine is selected, and inert (logging = 0) otherwise: a log nobody
# reads would charge every write a row. `epoch` names the database a cached index was built
# from, so an index is never reused for another file at the same path.
LEXICAL_LOG_SQL = """
CREATE TABLE IF NOT EXISTS lexical_log_clock (
    id      INTEGER PRIMARY KEY CHECK (id = 0),
    logging INTEGER NOT NULL DEFAULT 0,
    epoch   TEXT    NOT NULL DEFAULT '',
    head    INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO lexical_log_clock (id, epoch) VALUES (0, lower(hex(randomblob(8))));
CREATE TABLE IF NOT EXISTS lexical_changes (
    seq  INTEGER PRIMARY KEY,
    kind TEXT    NOT NULL,
    id   INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS lexical_log_mem_ai AFTER INSERT ON memories
WHEN (SELECT logging FROM lexical_log_clock WHERE id = 0) = 1 BEGIN
    UPDATE lexical_log_clock SET head = head + 1 WHERE id = 0;
    INSERT INTO lexical_changes VALUES ((SELECT head FROM lexical_log_clock WHERE id = 0), 'mem', new.id);
END;
CREATE TRIGGER IF NOT EXISTS lexical_log_mem_ad AFTER DELETE ON memories
WHEN (SELECT logging FROM lexical_log_clock WHERE id = 0) = 1 BEGIN
    UPDATE lexical_log_clock SET head = head + 1 WHERE id = 0;
    INSERT INTO lexical_changes VALUES ((SELECT head FROM lexical_log_clock WHERE id = 0), 'mem', old.id);
END;
CREATE TRIGGER IF NOT EXISTS lexical_log_mem_au AFTER UPDATE OF content, agent_id, project_id, channel ON memories
WHEN (SELECT logging FROM lexical_log_clock WHERE id = 0) = 1
 AND (new.content IS NOT old.content OR new.agent_id IS NOT old.agent_id
      OR new.project_id IS NOT old.project_id OR new.channel IS NOT old.channel) BEGIN
    UPDATE lexical_log_clock SET head = head + 1 WHERE id = 0;
    INSERT INTO lexical_changes VALUES ((SELECT head FROM lexical_log_clock WHERE id = 0), 'mem', old.id);
END;
CREATE TRIGGER IF NOT EXISTS lexical_log_ep_ai AFTER INSERT ON episodes
WHEN (SELECT logging FROM lexical_log_clock WHERE id = 0) = 1 BEGIN
    UPDATE lexical_log_clock SET head = head + 1 WHERE id = 0;
    INSERT INTO lexical_changes VALUES ((SELECT head FROM lexical_log_clock WHERE id = 0), 'ep', new.id);
END;
CREATE TRIGGER IF NOT EXISTS lexical_log_ep_ad AFTER DELETE ON episodes
WHEN (SELECT logging FROM lexical_log_clock WHERE id = 0) = 1 BEGIN
    UPDATE lexical_log_clock SET head = head + 1 WHERE id = 0;
    INSERT INTO lexical_changes VALUES ((SELECT head FROM lexical_log_clock WHERE id = 0), 'ep', old.id);
END;
CREATE TRIGGER IF NOT EXISTS lexical_log_ep_au AFTER UPDATE OF summary, keywords, agent_id, project_id, channel ON episodes
WHEN (SELECT logging FROM lexical_log_clock WHERE id = 0) = 1
 AND (new.summary IS NOT old.summary OR new.keywords IS NOT old.keywords OR new.agent_id IS NOT old.agent_id
      OR new.project_id IS NOT old.project_id OR new.channel IS NOT old.channel) BEGIN
    UPDATE lexical_log_clock SET head = head + 1 WHERE id = 0;
    INSERT INTO lexical_changes VALUES ((SELECT head FROM lexical_log_clock WHERE id = 0), 'ep', old.id);
END;
"""


async def install(db) -> None:
    """At boot: the log on and its objects present while this engine is selected, off otherwise."""
    if config.LEXICAL_ENGINE == "tantivy":
        await db.executescript(LEXICAL_LOG_SQL)
        await db.execute("UPDATE lexical_log_clock SET logging = 1 WHERE id = 0")
        return
    present = await db.execute_fetchall(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'lexical_log_clock'"
    )
    if present:
        await db.execute("UPDATE lexical_log_clock SET logging = 0 WHERE id = 0")
        await db.execute("DELETE FROM lexical_changes")


# --- terms ---------------------------------------------------------------------------------------

_STEMMER = None


def _stem(words: list[str]) -> list[str]:
    global _STEMMER
    if _STEMMER is None:
        from tantivy import Filter, TextAnalyzerBuilder, Tokenizer

        _STEMMER = TextAnalyzerBuilder(Tokenizer.raw()).filter(Filter.stemmer("english")).build()
    return [(_STEMMER.analyze(w) or [w])[0] for w in words]


def _ascii_terms(tok: str, mode: str) -> list[str]:
    tok = tok.strip(_EDGE).lower()
    if not tok:
        return []
    if mode == "t":
        return [tok[i : i + 3] for i in range(len(tok) - 2)]
    words = _WORD_RE.findall(tok)
    if mode == "ws":
        words = _stem(words)
    # A token with inner punctuation (bug-183, CVE-2024-3094) is also kept whole, so an
    # identifier is one rare term and not only its common parts.
    return words + ([tok] if len(words) > 1 or (words and words[0] != tok) else [])


def _ngrams(run: str, n: int) -> list[str]:
    return [run[i : i + n] for i in range(len(run) - n + 1)]


def _cjk_terms(run: str, mode: str, query: bool) -> list[str]:
    segmenter = config.QUERY_SEGMENTER if query else "trigram"
    if mode == "km":
        kept_only = query and segmenter != "trigram"
        return [
            surface for _b, _e, surface, pos in query_segment._morphemes(run)
            if not kept_only or query_segment._kept(pos, nouns_only=False)
        ]
    n = 3 if mode == "k3" else 2
    if segmenter == "trigram":
        return _ngrams(run, n)
    if segmenter == "morph":
        return [g for r in query_segment.content_runs(run) for g in _ngrams(r, n)]
    # morph_overlap keeps grams with two characters inside kept nouns, as for trigrams.
    inside = [False] * len(run)
    for b, e, _s, pos in query_segment._morphemes(run):
        if query_segment._kept(pos, nouns_only=True):
            inside[b:e] = [True] * (e - b)
    return [run[i : i + n] for i in range(len(run) - n + 1) if sum(inside[i : i + n]) >= 2]


def terms(text: str, *, query: bool = False) -> list[str]:
    """The index terms of ``text``: a record's, or a query's when ``query``."""
    cjk, ascii_ = config.TANTIVY_CJK, config.TANTIVY_ASCII
    out: list[str] = []
    for tok in _TOKEN_RE.findall(text):
        if _CJK_RE.match(tok):
            out.extend(_cjk_terms(tok, cjk, query))
        else:
            out.extend(_ascii_terms(tok, ascii_))
    return out


# --- the index -----------------------------------------------------------------------------------


@dataclass
class _Index:
    index: object
    schema: object
    applied: int
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_cache: dict[tuple, _Index] = {}
_build_lock = asyncio.Lock()


def _new_index():
    from tantivy import Index, SchemaBuilder, TextAnalyzerBuilder, Tokenizer

    sb = SchemaBuilder()
    sb.add_text_field("key", stored=False, tokenizer_name="raw")
    sb.add_text_field("kind", stored=False, tokenizer_name="raw")
    sb.add_integer_field("rid", stored=True, indexed=True, fast=True)
    sb.add_text_field("body", stored=False, tokenizer_name="ws")
    # Prefixed so that an empty project or channel is still a term.
    sb.add_text_field("agent", stored=False, tokenizer_name="raw")
    sb.add_text_field("project", stored=False, tokenizer_name="raw")
    sb.add_text_field("channel", stored=False, tokenizer_name="raw")
    schema = sb.build()
    index = Index(schema)
    index.register_tokenizer("ws", TextAnalyzerBuilder(Tokenizer.whitespace()).build())
    return index, schema


def _doc(kind: str, rid: int, text: str, agent: str, project: str, channel: str):
    from tantivy import Document

    return Document(
        key=f"{kind}:{rid}", kind=kind, rid=rid, body=" ".join(terms(text)),
        agent=f"a:{agent}", project=f"p:{project}", channel=f"c:{channel}",
    )


async def _rows(db, kind: str, ids: list[int] | None = None) -> list[tuple]:
    """Every row of ``kind`` (the index holds every agent's: a deliberate global read, the
    filters are applied at search), or the rows of ``ids``."""
    if ids is None:
        every = isolation_where(agent_id=None)
        if kind == "mem":
            return list(await db.execute_fetchall(
                f"SELECT id, content, agent_id, project_id, channel FROM memories{every.where}", every.params
            ))
        return list(await db.execute_fetchall(
            f"SELECT id, summary || ' ' || keywords, agent_id, project_id, channel FROM episodes{every.where}",
            every.params,
        ))
    out: list[tuple] = []
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        marks = ",".join("?" * len(chunk))
        if kind == "mem":
            out += await db.execute_fetchall(
                f"SELECT id, content, agent_id, project_id, channel FROM memories WHERE id IN ({marks})", chunk
            )
        else:
            out += await db.execute_fetchall(
                f"SELECT id, summary || ' ' || keywords, agent_id, project_id, channel FROM episodes WHERE id IN ({marks})",
                chunk,
            )
    return out


def _write(index, docs: list, deletes: list[str], heap: int) -> None:
    w = index.writer(heap_size=heap, num_threads=1)
    for key in deletes:
        w.delete_documents_by_term("key", key)
    for d in docs:
        w.add_document(d)
    w.commit()  # seam-waiver: a Tantivy index writer's commit, not a database transaction
    w.wait_merging_threads()
    index.reload()


async def _index(db) -> _Index | None:
    try:
        clock = await db.execute_fetchall("SELECT epoch, head FROM lexical_log_clock WHERE id = 0")
    except Exception:  # noqa: BLE001 -- no log table: the boot did not select this engine
        return None
    if not clock:
        return None
    epoch, head = clock[0]
    key = (config.DB_PATH, epoch)
    ix = _cache.get(key)
    if ix is None:
        async with _build_lock:
            ix = _cache.get(key)
            if ix is None:
                index, schema = _new_index()
                docs = [_doc(k, *r) for k in ("mem", "ep") for r in await _rows(db, k)]
                await asyncio.to_thread(_write, index, docs, [], 500_000_000)
                ix = _Index(index, schema, head)
                _cache.clear()
                _cache[key] = ix
    if head > ix.applied:
        async with ix.lock:
            if head > ix.applied:
                changed = await db.execute_fetchall(
                    "SELECT DISTINCT kind, id FROM lexical_changes WHERE seq > ? AND seq <= ?", (ix.applied, head)
                )
                deletes = [f"{k}:{i}" for k, i in changed]
                docs = []
                for k in ("mem", "ep"):
                    ids = [i for kk, i in changed if kk == k]
                    if ids:
                        docs += [_doc(k, *r) for r in await _rows(db, k, ids)]
                await asyncio.to_thread(_write, ix.index, docs, deletes, 50_000_000)
                ix.applied = head
    return ix


# --- search --------------------------------------------------------------------------------------


def _query(ix: _Index, kind: str, text_terms: list[str], phrases: list[list[str]], agent_id: str,
           project_id: str | None, channel: str, ids: list[int] | None):
    from tantivy import Occur, Query

    s = ix.schema
    should = [(Occur.Should, Query.term_query(s, "body", t)) for t in dict.fromkeys(text_terms)]
    for p in phrases:
        q = Query.term_query(s, "body", p[0]) if len(p) == 1 else Query.phrase_query(s, "body", p)
        should.append((Occur.Should, q))
    if not should:
        return None

    def only(q):
        return (Occur.Must, Query.boost_query(q, 0.0))

    def any_of(field_name, values):
        return Query.boolean_query([(Occur.Should, Query.term_query(s, field_name, v)) for v in values])

    musts = [(Occur.Must, Query.boolean_query(should)), only(Query.term_query(s, "kind", kind)),
             only(Query.term_query(s, "agent", f"a:{agent_id}"))]
    if project_id is not None:
        musts.append(only(any_of("project", ["p:"] if project_id == "" else [f"p:{project_id}", "p:"])))
    if channel:
        musts.append(only(any_of("channel", [f"c:{channel}", "c:"])))
    if ids is not None:
        musts.append(only(Query.term_set_query(s, "rid", ids)))
    return Query.boolean_query(musts)


def _hits(ix: _Index, query, limit: int) -> list[tuple[int, float]]:
    searcher = ix.index.searcher()
    return [(searcher.doc(addr)["rid"][0], score) for score, addr in searcher.search(query, limit, count=False).hits]


def _alias_phrases(extra_terms: list[str] | None) -> list[list[str]]:
    # A declared alias names one thing: its terms must appear together, as FTS5's phrase did.
    return [p for p in (terms(t, query=True) for t in dict.fromkeys(extra_terms or ()) if len(t) >= 3) if p]


async def search_memories(db, agent_id: str, query: str, limit: int, channel: str, project_id: str | None,
                          source_id: str, extra_terms: list[str] | None, window: tuple[str, str] | None):
    """The memory arm's rows, or None when the index is unavailable (the caller uses FTS5)."""
    ix = await _index(db)
    if ix is None:
        return None
    iso = isolation_where(agent_id=agent_id, project_id=project_id, channel=channel)
    src = source_id_where(source_id)
    win_clause = " AND datetime(timestamp) >= datetime(?) AND datetime(timestamp) < datetime(?)" if window else ""
    ids = None
    if source_id or window:
        ids = [r[0] for r in await db.execute_fetchall(
            f"SELECT id FROM memories WHERE {iso.clause}{src.and_clause}{win_clause}",
            (*iso.params, *src.params, *(window or ())),
        )]
        if not ids:
            return []
    q = _query(ix, "mem", terms(query, query=True), _alias_phrases(extra_terms), agent_id, project_id, channel, ids)
    if q is None:
        return []
    hits = await asyncio.to_thread(_hits, ix, q, limit)
    if not hits:
        return []
    rows = await db.execute_fetchall(
        f"SELECT id, msg_id, content, source, timestamp FROM memories "
        f"WHERE id IN ({','.join('?' * len(hits))}) AND {iso.clause}{src.and_clause}{win_clause}",
        (*[h[0] for h in hits], *iso.params, *src.params, *(window or ())),
    )
    by_id = {r[0]: r for r in rows}
    return [{"id": i, "msg_id": by_id[i][1], "content": by_id[i][2], "source": by_id[i][3],
             "timestamp": by_id[i][4], "_bm25": -score} for i, score in hits if i in by_id]


async def search_episodes(db, agent_id: str, query: str, limit: int, channel: str, project_id: str | None,
                          extra_terms: list[str] | None, window: tuple[str, str] | None, in_window_sql: str):
    """The episode arm's rows (id, summary, start_time, resolved, score, created_at), or None."""
    ix = await _index(db)
    if ix is None:
        return None
    iso = isolation_where(agent_id=agent_id, project_id=project_id, channel=channel, alias="e")
    ids = None
    if window:
        ids = [r[0] for r in await db.execute_fetchall(
            f"SELECT e.id FROM episodes e WHERE {iso.clause}{in_window_sql}", (*iso.params, *window)
        )]
        if not ids:
            return []
    q = _query(ix, "ep", terms(query, query=True), _alias_phrases(extra_terms), agent_id, project_id, channel, ids)
    if q is None:
        return []
    hits = await asyncio.to_thread(_hits, ix, q, limit)
    if not hits:
        return []
    rows = await db.execute_fetchall(
        f"SELECT e.id, e.summary, e.start_time, e.resolved, e.created_at FROM episodes e "
        f"WHERE e.id IN ({','.join('?' * len(hits))}) AND {iso.clause}{in_window_sql if window else ''}",
        (*[h[0] for h in hits], *iso.params, *(window or ())),
    )
    by_id = {r[0]: r for r in rows}
    return [(i, by_id[i][1], by_id[i][2], by_id[i][3], -score, by_id[i][4]) for i, score in hits if i in by_id]
