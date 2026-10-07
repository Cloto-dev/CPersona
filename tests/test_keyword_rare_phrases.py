"""The keyword arm may rank without the query's common phrases, and returns the same rows.

FTS5's bm25 gives a phrase held by at least half the indexed rows an idf of 1e-6, so
such a phrase moves no row's score by as much as COMMON_PHRASE_BOUND. On 100,000
memories of real length a question's words matched 99.9% of the records, and ranking
every match was most of the keyword arm's time. A caller that reads only the order
(rrf, the cascade) may now be ranked on the rest of the phrases, and the rows are taken
only when no row could have moved: every gap among the first `limit` rows, and the gap
to the next, exceeds the bound, and so does the last row's score.

The unit tests drive the decision with a database double whose scores are chosen; the
integration tests run the real index and compare against the whole expression.
"""

import os
import re
import sqlite3
import tempfile

import pytest

from cpersona import config, database, memory_handlers as mh, session, tasks, vector

AGENT = "agent.rare"


# --------------------------------------------------------------------------
# the expression a recall searches is unchanged
# --------------------------------------------------------------------------

_CJK_CLASS = r"぀-ヿ㐀-䶿一-鿿ｦ-ﾟ"


def _expression_before(query, extra_terms=None):
    """The expression builders as they stood before the phrase lists were split out."""
    cjk = re.compile(f"[{_CJK_CLASS}]")
    token_re = re.compile(f"[{_CJK_CLASS}]+|[^\\s{_CJK_CLASS}]+")

    def build(q):
        terms = []
        for tok in token_re.findall(q):
            if cjk.match(tok):
                if len(tok) >= 3:
                    terms.extend(tok[i : i + 3] for i in range(len(tok) - 2))
            elif len(tok) >= 3:
                terms.append(tok)
        if not terms:
            return ""
        return " OR ".join('"' + t.replace('"', '""') + '"' for t in dict.fromkeys(terms))

    normalized = " ".join(token.strip("\"'`.,;:!?()[]{}") for token in query.split())
    expression = build(normalized)
    phrases = ['"' + t.replace('"', '""') + '"' for t in dict.fromkeys(extra_terms or ()) if len(t) >= 3]
    if not phrases:
        return expression
    return " OR ".join([expression, *phrases] if expression else phrases)


@pytest.mark.parametrize(
    ("query", "extra"),
    [
        ("What did we decide about the pilot budget?", None),
        ("the the the", None),
        ("CVE-2024-3094 and bug-183", None),
        ('a "quoted" word', None),
        ("パンを焼いたのは誰だった", None),
        ("mixed パン屋さん text", ["Miz Eye", "ab", "the"]),
        ("", ["only extra terms"]),
        ("ab cd", None),
    ],
)
def test_the_whole_expression_is_the_one_it_always_was(query, extra):
    assert mh._build_fts_recall_query(query, extra) == _expression_before(query, extra)
    assert " OR ".join(mh._fts_recall_phrases(query, extra)) == _expression_before(query, extra)


# --------------------------------------------------------------------------
# which phrases are common (a database double)
# --------------------------------------------------------------------------


class _Index:
    """Answers the statements the decision issues, with chosen numbers."""

    def __init__(self, n_rows, docs, ranked=()):
        self.n_rows, self.docs, self.ranked = n_rows, docs, list(ranked)
        self.looked_up: list[str] = []
        self.ranked_on = None

    async def execute(self, sql, params=()):
        raise AssertionError(f"the decision only reads, and creates no table: {sql}")

    async def execute_fetchall(self, sql, params=()):
        if "memories_fts_docsize" in sql:
            return [(self.n_rows,)]
        if sql == mh._PHRASE_ROWS_SQL:
            term = params[0][1:-1].replace('""', '"')
            self.looked_up.append(term)
            return [(self.docs.get(term, 0),)]
        self.ranked_on = params[0]
        return self.ranked[: params[-1]]


@pytest.mark.asyncio
async def test_a_phrase_in_half_the_rows_is_common_and_one_fewer_is_not():
    # idf = log((N - n + 0.5) / (n + 0.5)) is 0 at n = N / 2, the floor's edge.
    assert await mh._common_phrases(_Index(100, {"the": 50}), ['"the"']) == {'"the"'}
    assert await mh._common_phrases(_Index(100, {"the": 49}), ['"the"']) == set()


@pytest.mark.asyncio
async def test_an_ascii_phrase_is_looked_up_as_the_tokenizer_folds_it():
    index = _Index(10, {"the": 10})
    assert await mh._common_phrases(index, ['"The"']) == {'"The"'}
    assert index.looked_up == ["the"]


@pytest.mark.asyncio
async def test_only_a_phrase_of_one_trigram_is_classified():
    index = _Index(10, {"with": 10, "ている": 10, "çaé": 10})
    common = await mh._common_phrases(index, ['"with"', '"ている"', '"çaé"', '"ab"'])
    assert common == {'"ている"'}
    assert index.looked_up == ["ている"]


@pytest.mark.asyncio
async def test_an_empty_index_classifies_nothing():
    assert await mh._common_phrases(_Index(0, {"the": 0}), ['"the"']) == set()


# --------------------------------------------------------------------------
# when the rows ranked on the rest are taken (chosen scores)
# --------------------------------------------------------------------------

BOUND = mh.COMMON_PHRASE_BOUND  # one common phrase left out


def _rows(scores):
    return [(i, "", f"row {i}", "{}", "", -s) for i, s in enumerate(scores)]


async def _rank(scores, limit=3):
    index = _Index(100, {"the": 100}, _rows(scores))
    rows, report = await mh._rank_on_rare_phrases(index, ['"zebra"', '"the"'], "SQL", (), limit)
    return rows, report, index


@pytest.mark.asyncio
async def test_well_separated_rows_are_taken_from_the_rest():
    rows, report, index = await _rank([9.0, 8.0, 7.0, 6.0])
    assert [r[0] for r in rows] == [0, 1, 2]
    assert index.ranked_on == '"zebra"'
    assert report == {"phrases": 2, "left_out": 1, "bound": report["bound"], "ranked_on": "rare"}
    assert BOUND < report["bound"] < 2 * BOUND


@pytest.mark.asyncio
async def test_a_gap_inside_the_bound_among_the_rows_sends_it_to_the_whole():
    rows, report, _ = await _rank([9.0, 9.0 - BOUND / 2, 7.0, 6.0])
    assert rows is None and report["ranked_on"] == "whole"


@pytest.mark.asyncio
async def test_a_gap_inside_the_bound_at_the_cut_sends_it_to_the_whole():
    rows, report, _ = await _rank([9.0, 8.0, 7.0, 7.0 - BOUND / 2])
    assert rows is None and report["ranked_on"] == "whole"


@pytest.mark.asyncio
async def test_a_last_row_within_reach_of_the_common_phrases_sends_it_to_the_whole():
    # A row holding only common phrases scores below the bound; it could overtake.
    rows, report, _ = await _rank([3.0, 2.0, BOUND / 2, BOUND / 8], limit=3)
    assert rows is None and report["ranked_on"] == "whole"


@pytest.mark.asyncio
async def test_too_few_rows_to_see_the_cut_sends_it_to_the_whole():
    rows, report, _ = await _rank([9.0, 8.0, 7.0])
    assert rows is None and report["ranked_on"] == "whole"


@pytest.mark.asyncio
async def test_nothing_to_leave_out_asks_nothing():
    index = _Index(100, {"the": 10}, _rows([9.0, 8.0, 7.0, 6.0]))
    rows, report = await mh._rank_on_rare_phrases(index, ['"zebra"', '"the"'], "SQL", (), 3)
    assert rows is None and report is None and index.ranked_on is None


@pytest.mark.asyncio
async def test_a_query_of_common_phrases_only_asks_nothing():
    index = _Index(100, {"the": 100}, _rows([9.0, 8.0, 7.0, 6.0]))
    rows, report = await mh._rank_on_rare_phrases(index, ['"the"'], "SQL", (), 3)
    assert rows is None and report is None and index.ranked_on is None


# --------------------------------------------------------------------------
# the real index
# --------------------------------------------------------------------------


class _TempDB:
    async def __aenter__(self):
        session.reset_pauses_for_tests()
        self._dir = tempfile.mkdtemp()
        self._saved = (database._db, database.DB_PATH, tasks._task_queue)
        database._db = None
        database.DB_PATH = os.path.join(self._dir, "rare_phrases.db")
        tasks._task_queue = None
        await database.get_db()
        return self

    async def __aexit__(self, *exc):
        await database.close_db()
        database._db, database.DB_PATH, tasks._task_queue = self._saved
        session.reset_pauses_for_tests()


#: Every record holds "the" (a common phrase of one trigram). "zebra" and "quokka" are each
#: in about a third of the records, in varying counts, and the records differ in length,
#: so the rankings on them separate.
RECORDS = [
    f"the note {i} "
    + "zebra " * ((i % 6) if i < 25 else 0)
    + "quokka " * ((i % 4) + 1 if i % 3 == 0 else 0)
    + "the " * (i % 3)
    + "x" * i
    for i in range(60)
]
QUERIES = ["the zebra quokka", "the quokka", "zebra the note", "the the zebra"]


async def _store_records():
    async with database.transaction() as db:
        for i, text in enumerate(RECORDS):
            await db.execute(
                "INSERT INTO memories (agent_id, content, timestamp) VALUES (?, ?, ?)",
                (AGENT, text, f"2026-10-06T00:{i // 60:02d}:{i % 60:02d}+00:00"),
            )


@pytest.fixture
def no_vectors(monkeypatch):
    monkeypatch.setattr(vector, "_embedding_client", None)
    monkeypatch.setattr(config, "BLOCK_RETRIEVAL_ENABLED", False)


def _spy(monkeypatch):
    calls = []
    real = mh._rank_on_rare_phrases

    async def spy(*args, **kwargs):
        rows, report = await real(*args, **kwargs)
        calls.append(report["ranked_on"] if report else None)
        return rows, report

    monkeypatch.setattr(mh, "_rank_on_rare_phrases", spy)
    return calls


@pytest.mark.asyncio
async def test_the_counts_classify_each_phrase_as_the_index_vocabulary_does():
    # The rows holding a one-trigram phrase, counted with the phrase, must be the rows the
    # index's vocabulary names for that trigram, case folded and with a quote inside, and
    # the floor's edge must fall where FTS5 puts it: 10 of 20 rows is common, 9 is not.
    records = []
    for i in range(20):
        text = f"rec{i:02d} "
        if i < 10:
            text += ("THE " if i % 2 else "the ") + "パン屋 " + 'a"b '
        if i < 9:
            text += "zeb "
        records.append(text)
    phrases = mh._fts_recall_phrases('The zeb パン屋 a"b')
    assert phrases == ['"The"', '"zeb"', '"パン屋"', '"a""b"']
    async with _TempDB():
        async with database.transaction() as db:
            for i, text in enumerate(records):
                await db.execute(
                    "INSERT INTO memories (agent_id, content, timestamp) VALUES (?, ?, ?)",
                    (AGENT, text, f"2026-10-07T00:00:{i:02d}+00:00"),
                )
        async with database.connection() as db:
            common = await mh._common_phrases(db, phrases)
            await db.execute("CREATE VIRTUAL TABLE temp.vocab USING fts5vocab(main, memories_fts, row)")
            docs = dict(await db.execute_fetchall("SELECT term, doc FROM temp.vocab"))
    assert {t: docs[t] for t in ("the", "zeb", "パン屋", 'a"b')} == {"the": 10, "zeb": 9, "パン屋": 10, 'a"b': 10}
    assert common == {'"The"', '"パン屋"', '"a""b"'}


@pytest.mark.asyncio
async def test_the_rows_and_order_are_the_whole_expressions(monkeypatch, no_vectors):
    calls = _spy(monkeypatch)
    async with _TempDB():
        await _store_records()
        async with database.connection() as db:
            for query in QUERIES:
                for depth in (1, 3, 5, 10, 20, 59):
                    whole = await mh._search_memories_keyword(db, AGENT, query, depth)
                    rank_only = await mh._search_memories_keyword(db, AGENT, query, depth, rank_only=True)
                    assert [r["id"] for r in rank_only] == [r["id"] for r in whole], (query, depth)
    assert "rare" in calls, calls  # the shortcut was taken, not only declined


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "asks"), [("rrf", True), ("cascade", True), ("rsf", False)])
async def test_only_a_fusion_that_reads_the_order_asks(monkeypatch, no_vectors, mode, asks):
    calls = _spy(monkeypatch)
    monkeypatch.setattr(mh, "RECALL_MODE", mode)
    async with _TempDB():
        await _store_records()
        await mh.do_recall(agent_id=AGENT, query="the zebra quokka", limit=5)
    assert bool(calls) is asks, calls


@pytest.mark.asyncio
async def test_a_failing_shortcut_ranks_on_the_whole_expression_not_like(monkeypatch, no_vectors):
    async def broken(*args, **kwargs):
        raise sqlite3.OperationalError("no vocabulary here")

    async with _TempDB():
        await _store_records()
        async with database.connection() as db:
            whole = await mh._search_memories_keyword(db, AGENT, "the zebra quokka", 5)
            monkeypatch.setattr(mh, "_common_phrases", broken)
            rank_only = await mh._search_memories_keyword(db, AGENT, "the zebra quokka", 5, rank_only=True)
    assert [r["id"] for r in rank_only] == [r["id"] for r in whole]
    assert all(r["_bm25"] is not None for r in rank_only), "fell back to LIKE, which has no bm25"


@pytest.mark.asyncio
async def test_the_trace_says_how_the_keyword_arm_ranked(monkeypatch, no_vectors):
    monkeypatch.setattr(mh, "RECALL_MODE", "rrf")
    async with _TempDB():
        await _store_records()
        result = await mh.do_recall(agent_id=AGENT, query="the zebra quokka", limit=5, trace=True)
    ranking = result["trace"]["keyword_ranking"]
    assert ranking and ranking[0]["ranked_on"] in {"rare", "whole"}
    assert ranking[0]["left_out"] == 1 and ranking[0]["phrases"] == 3
