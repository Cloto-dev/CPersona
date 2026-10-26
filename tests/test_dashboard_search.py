"""The dashboard's search panel: words, phrases, exclusions, operators and properties.

Each operator is pinned on memories that differ only in what that operator reads, so
an operator that read the wrong thing (or nothing) would return the wrong set.
"""

import pytest
import pytest_asyncio

from cpersona import database
from cpersona.dashboard import store
from cpersona.database import get_db


@pytest_asyncio.fixture
async def db():
    conn = await get_db()
    for table in ("memories", "entities", "entity_aliases", "entity_mentions"):
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()
    return conn


async def _add(conn, content, ts, agent="a", project="", channel="", **extra):
    cols = {"agent_id": agent, "project_id": project, "channel": channel, "content": content, "timestamp": ts, **extra}
    cur = await conn.execute(
        f"INSERT INTO memories ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", tuple(cols.values())
    )
    await conn.commit()
    return cur.lastrowid


def _ids(text, **kw):
    conn = store.open_store(database.DB_PATH)
    try:
        return [r["id"] for r in store.search(conn, text, **kw)["rows"]]
    finally:
        conn.close()


def test_a_search_is_parsed_into_its_parts():
    p = store.parse_search('alpha "two words" -gone path:a/p line:(x y) month:2026-10 [locked] [project:p] note:x')
    assert p["terms"] == ["alpha", "two words", "note:x"]
    assert p["exclude"] == ["gone"]
    assert p["ops"] == [("path", ["a/p"]), ("line", ["x", "y"]), ("month", ["2026-10"])]
    assert p["props"] == [("locked", ""), ("project", "p")]


@pytest.mark.asyncio
async def test_every_word_is_required_and_a_minus_word_excludes(db):
    both = await _add(db, "apple and pear", "2026-10-01T00:00:00+00:00")
    await _add(db, "apple only", "2026-10-02T00:00:00+00:00")
    await _add(db, "apple and pear and plum", "2026-10-03T00:00:00+00:00")
    assert both in _ids("apple pear") and len(_ids("apple pear")) == 2
    assert _ids("apple pear -plum") == [both]
    assert _ids('"and pear and"') != [] and both not in _ids('"and pear and"')


@pytest.mark.asyncio
async def test_case_is_matched_only_when_asked(db):
    upper = await _add(db, "Cloto said so", "2026-10-01T00:00:00+00:00")
    lower = await _add(db, "cloto said so", "2026-10-02T00:00:00+00:00")
    assert set(_ids("cloto")) == {upper, lower}
    assert _ids("cloto", case=True) == [lower]
    assert _ids("Cloto", case=True) == [upper]


@pytest.mark.asyncio
async def test_line_needs_the_words_on_one_line(db):
    same = await _add(db, "first line\nalpha beta here\nlast", "2026-10-01T00:00:00+00:00")
    await _add(db, "alpha here\nbeta there", "2026-10-02T00:00:00+00:00")
    assert _ids("line:(alpha beta)") == [same]
    assert len(_ids("alpha beta")) == 2


@pytest.mark.asyncio
async def test_path_agent_and_month_read_where_a_memory_lives(db):
    in_p = await _add(db, "note", "2026-10-01T00:00:00+00:00", agent="a", project="proj")
    by_b = await _add(db, "note", "2026-09-01T00:00:00+00:00", agent="b")
    assert _ids("path:proj") == [in_p]
    assert _ids("agent:b") == [by_b]
    assert _ids("month:2026-09") == [by_b]
    assert _ids("month:2026") == [in_p, by_b]


@pytest.mark.asyncio
async def test_tag_finds_memories_mentioning_an_entity_by_name_or_alias(db):
    tagged = await _add(db, "about the project", "2026-10-01T00:00:00+00:00")
    await _add(db, "about the project too", "2026-10-02T00:00:00+00:00")
    cur = await db.execute(
        "INSERT INTO entities (agent_id, name, normalized, declared_by, created_at) VALUES ('a', 'Orion', 'orion', 'a', 'x')"
    )
    eid = cur.lastrowid
    await db.execute("INSERT INTO entity_aliases (entity_id, alias, normalized) VALUES (?, 'Hunter', 'hunter')", (eid,))
    await db.execute("INSERT INTO entity_mentions (entity_id, ref, declared_by, created_at) VALUES (?, ?, 'a', 'x')", (eid, f"mem:{tagged}"))
    await db.commit()
    assert _ids("tag:orion") == [tagged]
    assert _ids("tag:hunter") == [tagged]
    assert _ids("tag:nobody") == []


@pytest.mark.asyncio
async def test_properties_and_sort(db):
    old = await _add(db, "x locked", "2026-01-01T00:00:00+00:00", locked=1, recall_count=0)
    mid = await _add(db, "x recalled", "2026-05-01T00:00:00+00:00", recall_count=5, channel="c1")
    new = await _add(db, "x plain", "2026-09-01T00:00:00+00:00", recall_count=1)
    assert _ids("[locked]") == [old]
    assert _ids("[recalled] x") == [new, mid]
    assert _ids("[channel:c1]") == [mid]
    assert _ids("x") == [new, mid, old]
    assert _ids("x", sort="old") == [old, mid, new]
    assert _ids("x", sort="recalled") == [mid, new, old]


@pytest.mark.asyncio
async def test_a_result_counts_its_matches_and_quotes_the_lines(db):
    await _add(db, "one apple\nnothing\napple and apple", "2026-10-01T00:00:00+00:00")
    conn = store.open_store(database.DB_PATH)
    row = store.search(conn, "apple")["rows"][0]
    conn.close()
    assert row["matches"] == 3
    assert row["lines"] == ["one apple", "apple and apple"]


def test_an_empty_search_finds_nothing_rather_than_everything():
    conn = store.open_store(database.DB_PATH)
    try:
        assert store.search(conn, "   ")["total"] == 0
    finally:
        conn.close()
