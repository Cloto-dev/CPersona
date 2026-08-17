"""Regression tests for bug-215: punctuated identifiers must survive _build_fts_query.

``_build_fts_query`` used to run ``re.sub(r"[^\\w\\s]", "", query)`` before tokenising,
so ``CVE-2024-3094`` reached FTS5 as ``CVE20243094``. Both FTS tables are trigram-
tokenised and a trigram phrase match is a SUBSTRING match, so the mangled form shares no
trigram with the stored text and matches nothing.

What makes that silent is the short-circuit in ``_search_memories_keyword``: the LIKE
fallback runs only when the FTS query returned ZERO rows. A realistic query
("what happened with CVE-2024-3094") also carries common words, those match the noise
rows, ``if rows:`` returns early — and the one row holding the exact identifier is
absent from the keyword channel (and therefore from RRF/RSF fusion, which only ever sees
what the retrievers hand it). This is exactly the identifier/hash lookup the bug-183
gate_fallback comment says recall must never make silently invisible.

The tests pin both halves: the query builder keeps the identifier intact, and the
retriever returns the identifier row on the FTS path (``_bm25`` present) while the noise
rows are returned too — i.e. with the short-circuit genuinely taken.
"""

import os
import tempfile

os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "test_bug215.db"))
os.environ.setdefault("CPERSONA_EMBEDDING_MODE", "none")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from cpersona import memory_handlers as M  # noqa: E402
from cpersona.database import get_db  # noqa: E402

AGENT = "agent.bug215"
IDENTIFIER_ROW = "CVE-2024-3094 xz backdoor: rotate the build key"
NOISE = [
    "what happened with the sourdough starter over the weekend",
    "notes on what happened with the office move",
    "what happened with the quarterly billing export",
]
QUERY = "what happened with CVE-2024-3094"


@pytest_asyncio.fixture(autouse=True)
async def _fresh_db():
    db = await get_db()
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM episodes")
    await db.commit()
    yield


async def _seed() -> None:
    for content in (IDENTIFIER_ROW, *NOISE):
        stored = await M.do_store(AGENT, {"content": content, "source": {"System": "t"}})
        assert stored["result"] == "stored", stored


def test_build_fts_query_keeps_punctuated_identifiers_whole():
    """bug-215: the term the trigram index actually holds is the punctuated one."""
    built = M._build_fts_query(QUERY)
    assert '"CVE-2024-3094"' in built, built
    assert "CVE20243094" not in built, "punctuation stripping is back — the term matches nothing"
    for other in ("bug-183", "v2.5.4", "api/v1/store", "user@example.com"):
        assert f'"{other}"' in M._build_fts_query(f"recall the {other} note")


def test_build_fts_query_escapes_the_only_hazardous_character():
    """bug-215: the FTS5 phrase quote is doubled; nothing else is removed."""
    assert M._build_fts_query('the "quoted" term') == '"the" OR """quoted""" OR "term"'


def test_cjk_decomposition_is_unchanged():
    """The CJK trigram path keeps working — punctuation is no longer stripped, and
    a run shorter than a trigram is still dropped for the LIKE fallback."""
    assert M._build_fts_query("同じ日本語") == '"同じ日" OR "じ日本" OR "日本語"'
    assert M._build_fts_query("パン") == ""
    assert M._build_fts_query("bug-183 の修正") == '"bug-183" OR "の修正"'


@pytest.mark.asyncio
async def test_keyword_channel_returns_the_identifier_row_behind_the_fts_short_circuit():
    """bug-215: FTS returning SOME rows must not lose the exact-match row.

    The noise rows are what makes this non-vacuous: they match the common words, so
    ``_search_memories_keyword``'s ``if rows: return`` fires and the LIKE fallback never
    runs. Before the fix the identifier row was simply missing from that early return.
    """
    await _seed()
    db = await get_db()
    rows = await M._search_memories_keyword(db, AGENT, QUERY, limit=10)
    contents = [r["content"] for r in rows]

    assert IDENTIFIER_ROW in contents, (
        f"the exact-match row is invisible to the keyword channel: {contents}"
    )
    assert any(n in contents for n in NOISE), (
        "no noise row came back, so the FTS short-circuit was not exercised"
    )
    assert all(r["_bm25"] is not None for r in rows), (
        "results came from the LIKE fallback; the FTS path is the one under test"
    )


@pytest.mark.asyncio
async def test_keyword_channel_survives_a_quoted_query():
    """bug-215: doubling the quote keeps the MATCH expression syntactically valid."""
    await _seed()
    db = await get_db()
    rows = await M._search_memories_keyword(db, AGENT, 'what happened with "billing"', limit=10)
    assert any("billing" in r["content"] for r in rows), [r["content"] for r in rows]
