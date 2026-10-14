"""A recall's per-query cost is bounded by what the caller sends.

Four places let one argument of one call set how much work the server does,
with nothing to bound it:

1. The recall_count bump bound one SQL parameter per credited row in a single
   statement. A library recall deep enough to pass the variable ceiling raised
   inside the non-fatal guard, and no returned row was credited at all.
2. The keyword arm's rare-phrase shortcut issued one ``count(*) MATCH`` per
   distinct trigram of the query, so a pasted passage issued thousands of
   statements on the connection every recall's lexical arms share, and then ran
   the whole expression anyway.
3. The LIKE fallback used the whole query as one contains-pattern. SQLite
   refuses a pattern over 50,000 bytes ("LIKE or GLOB pattern too complex"), and
   the statement was outside any guard, so a long query with no FTS hit turned
   recall and reconstruct into a bare error.
4. ``exclude_contents`` (and ``external_context``, which recall_with_context
   turns into it) was walked entry by entry for every candidate row, so the
   cost of one recall was rows x entries.
"""

from __future__ import annotations

import random
import sqlite3
import sys

import pytest
import pytest_asyncio

from cpersona import memory_handlers as mh
from cpersona import reconstruct, session, utils
from cpersona.database import get_db

AGENT = "x06.agent"


@pytest.fixture(autouse=True)
def _clean_pauses():
    session.reset_pauses_for_tests()
    yield
    session.reset_pauses_for_tests()


@pytest_asyncio.fixture
async def clean_db():
    db = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()
    yield db
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()


async def _insert_rows(db, contents):
    await db.executemany(
        "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp)"
        " VALUES (?, '', '', ?, '{\"type\": \"User\", \"id\": \"u\"}', '2026-03-01T00:00:00+00:00')",
        [(AGENT, c) for c in contents],
    )
    await db.commit()


# --------------------------------------------------------------------------
# 1 — the recall_count bump is chunked like the read it pairs with
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_recall_deeper_than_the_variable_ceiling_still_credits_every_row(clean_db, monkeypatch):
    """The writer's variable ceiling is lowered below the result size, so a single
    ``IN (...)`` over every credited id cannot run; every returned row must still
    have its recall_count raised."""
    monkeypatch.setattr(mh, "CONFIDENCE_ENABLED", True)
    rows = 640
    await _insert_rows(clean_db, [f"row number {n}" for n in range(rows)])
    conn = clean_db._conn
    before = await clean_db._execute(conn.getlimit, sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
    ceiling = 600
    assert ceiling > mh.vector._ID_FETCH_CHUNK + 50, "the ceiling must admit one chunk and its filters"
    await clean_db._execute(conn.setlimit, sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, ceiling)
    try:
        out = await mh.do_recall(AGENT, "", rows)
    finally:
        await clean_db._execute(conn.setlimit, sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, before)
    returned = [int(m["ref"][4:]) for m in out["messages"] if str(m.get("ref", "")).startswith("mem:")]
    assert len(returned) > ceiling, f"the recall returned {len(returned)} rows; the test needs more than {ceiling}"
    marks = ",".join("?" * len(returned))
    credited = (
        await clean_db.execute_fetchall(
            f"SELECT count(*) FROM memories WHERE id IN ({marks}) AND recall_count > 0", returned
        )
    )[0][0]
    assert credited == len(returned), (
        f"{credited} of {len(returned)} returned rows were credited: the bump bound one "
        "parameter per row in one statement, and past the ceiling it failed as a whole"
    )


# --------------------------------------------------------------------------
# 2 — the rare-phrase shortcut issues a bounded number of count statements
# --------------------------------------------------------------------------
class _Index:
    """Answers the statements ``_common_phrases`` issues, counting them."""

    def __init__(self, n_rows, docs):
        self.n_rows, self.docs = n_rows, docs
        self.statements = 0

    async def execute_fetchall(self, sql, params=()):
        self.statements += 1
        if "memories_fts_docsize" in sql:
            return [(self.n_rows,)]
        assert sql == mh._PHRASE_ROWS_SQL, sql
        term = params[0][1:-1].replace('""', '"')
        return [(self.docs.get(term, 0),)]


def _distinct_cjk(n: int) -> str:
    """A run of n distinct CJK ideographs: n - 2 distinct trigrams."""
    return "".join(chr(0x4E00 + i) for i in range(n))


@pytest.mark.asyncio
async def test_a_long_query_issues_a_bounded_number_of_phrase_counts():
    counts = []
    for length in (2_000, 8_000):
        phrases = mh._fts_recall_phrases(_distinct_cjk(length))
        assert len(phrases) == length - 2
        index = _Index(100, {})
        await mh._common_phrases(index, phrases)
        counts.append(index.statements)
    assert max(counts) <= 128, (
        f"the shortcut issued {counts} statements for queries of 2,000 and 8,000 characters: "
        "one count per distinct trigram, without bound"
    )
    assert counts[0] == counts[1], f"the statement count still grows with the query: {counts}"


@pytest.mark.asyncio
async def test_an_ordinary_question_is_still_classified():
    """The bound leaves an ordinary question's shortcut as it was."""
    question = _distinct_cjk(40)
    phrases = mh._fts_recall_phrases(question)
    common_term = question[:3]
    index = _Index(100, {common_term: 80})
    assert await mh._common_phrases(index, phrases) == {f'"{common_term}"'}
    assert index.statements == 1 + len(phrases), "every trigram of the question is counted, once"


@pytest.mark.asyncio
async def test_a_long_query_returns_the_rows_of_the_whole_expression(clean_db):
    """Past the bound the arm ranks on the whole expression, which is what the
    shortcut is only allowed to reproduce."""
    await _insert_rows(clean_db, [f"{_distinct_cjk(5)}の話 {n}" for n in range(30)] + [f"別の話です {n}" for n in range(5)])
    query = _distinct_cjk(300)
    async with mh.connection() as db:
        ranked = await mh._search_memories_keyword(db, AGENT, query, 10, rank_only=True)
        whole = await mh._search_memories_keyword(db, AGENT, query, 10)
    assert [r["id"] for r in ranked] == [r["id"] for r in whole]
    assert ranked, "the query matches rows through FTS"


# --------------------------------------------------------------------------
# 3 — a long query with no FTS hit does not raise out of the LIKE fallback
# --------------------------------------------------------------------------
LONG_QUERIES = [
    pytest.param("z" * 50_001, id="ascii-50001"),
    pytest.param("杏" * 16_700, id="cjk-16700"),
    pytest.param("zzz " * 13_000, id="words-52000-bytes"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("query", LONG_QUERIES)
async def test_a_long_query_with_no_hit_returns_an_empty_recall(clean_db, query):
    await _insert_rows(clean_db, [f"an ordinary memory {n}" for n in range(20)])
    out = await mh.do_recall(AGENT, query, 10)
    assert "error" not in out, f"recall failed instead of finding nothing: {out.get('error')}"
    assert out["messages"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("query", LONG_QUERIES[:2])
async def test_a_long_query_with_no_hit_returns_an_empty_reconstruction(clean_db, query):
    await _insert_rows(clean_db, [f"an ordinary memory {n}" for n in range(20)])
    out = await reconstruct.do_reconstruct(AGENT, query)
    assert "error" not in out, f"reconstruct failed instead of finding nothing: {out.get('error')}"
    assert out["items"] == []


@pytest.mark.asyncio
async def test_a_short_extra_term_still_matches_beside_a_long_query(clean_db):
    """Leaving the oversized pattern out keeps the other patterns of the statement.

    The extra term is two characters, shorter than a trigram, so FTS cannot match
    it and the arm reaches the LIKE statement that carries both patterns."""
    await _insert_rows(clean_db, ["the kestrel nests on the tower", "an unrelated row"])
    async with mh.connection() as db:
        rows = await mh._search_memories_keyword(db, AGENT, "q" * 60_000, 10, extra_terms=["ke"])
    assert [r["content"] for r in rows] == ["the kestrel nests on the tower"]


@pytest.mark.asyncio
async def test_the_like_fallback_failing_is_an_empty_arm(clean_db):
    """A build whose pattern limit is lower than the default still gets an empty arm."""
    await _insert_rows(clean_db, ["an ordinary memory"])
    conn = clean_db._conn
    before = await clean_db._execute(conn.getlimit, sqlite3.SQLITE_LIMIT_LIKE_PATTERN_LENGTH)
    await clean_db._execute(conn.setlimit, sqlite3.SQLITE_LIMIT_LIKE_PATTERN_LENGTH, 10)
    try:
        rows = await mh._search_memories_keyword(clean_db, AGENT, "z" * 40, 10)
    finally:
        await clean_db._execute(conn.setlimit, sqlite3.SQLITE_LIMIT_LIKE_PATTERN_LENGTH, before)
    assert rows == []


# --------------------------------------------------------------------------
# 4 — exclusion is not a walk over every entry for every candidate row
# --------------------------------------------------------------------------
async def _calls_per_exclusion_check(awaitable):
    """Await ``awaitable`` and report how many rows were checked and the most
    builtin calls one ``_content_excluded`` made.

    A profile hook counts every C-level call made while a ``_content_excluded``
    frame is live, nested helpers included, and resets at each entry; building
    the exclusion set before the retrievers run is outside every such frame, so
    it is not counted.
    """
    target = utils._content_excluded.__code__
    depth = current = worst = checks = 0

    def hook(frame, event, arg):
        nonlocal depth, current, worst, checks
        if event == "call" and frame.f_code is target:
            if depth == 0:
                current = 0
                checks += 1
            depth += 1
        elif event == "return" and frame.f_code is target:
            depth -= 1
            if depth == 0:
                worst = max(worst, current)
        elif event == "c_call" and depth:
            current += 1

    # The hook is per thread; the recall's Python runs on this loop's thread.
    sys.setprofile(hook)
    try:
        result = await awaitable
    finally:
        sys.setprofile(None)
    return result, checks, worst


def _entries(n: int, length: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    return ["".join(rng.choice("abcdefghij") for _ in range(length)) for _ in range(n)]


@pytest.mark.asyncio
async def test_exclusion_cost_per_row_does_not_grow_with_the_list(clean_db):
    await _insert_rows(clean_db, [f"the pilot budget note number {n}" for n in range(40)])
    # Entries at and below the prefix floor: both halves of the rule.
    entries = _entries(10_000, 47, seed=1) + _entries(10_000, 12, seed=2)
    out, checks, worst = await _calls_per_exclusion_check(
        mh.do_recall(AGENT, "pilot budget", 20, exclude_contents=entries)
    )
    assert checks, "no candidate row reached the exclusion check"
    assert out["messages"], out
    assert worst <= 64, (
        f"one exclusion check made {worst} builtin calls against {len(entries)} entries: "
        "the list is walked entry by entry for every candidate row"
    )


_MIN = utils.EXCLUDE_PREFIX_MIN_CHARS


def _reference_excluded(content: str, exclude_set: set[str]) -> bool:
    """The rule as the linear walk applied it, kept here as the oracle."""
    if not exclude_set:
        return False
    normalized = content.strip().lower()
    if not normalized:
        return False
    for excl in exclude_set:
        if len(excl) < _MIN:
            if normalized == excl:
                return True
            continue
        if normalized.startswith(excl) or excl.startswith(normalized):
            return True
    return False


def test_the_indexed_rule_is_the_linear_rule():
    """Over a small alphabet, so that prefixes in both directions and near misses
    at every length are common, the answer is the walk's answer for every row."""
    rng = random.Random(7)
    alphabet = "ab"
    for _ in range(300):
        stem = "".join(rng.choice(alphabet) for _ in range(rng.randint(20, 50)))
        entries = set()
        for _ in range(rng.randint(0, 12)):
            cut = rng.randint(0, len(stem))
            tail = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12)))
            entries.add(stem[:cut] + tail)
        entries.discard("")
        for _ in range(12):
            cut = rng.randint(0, len(stem))
            row = stem[:cut] + "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12)))
            expected = _reference_excluded(row, entries)
            assert utils._content_excluded(row, entries) is expected, (row, sorted(entries))
            assert utils._content_excluded(row.upper() + "  ", entries) is expected
