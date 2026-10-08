"""CPERSONA_LEXICAL_ENGINE=off: both keyword arms are absent, not degraded.

The setting exists so a measurement can compare a store with its keyword arms
against the same store without them. `off` must therefore remove the FTS5 row
from both arms, must not hand the memory arm to its LIKE fallback (which is what
CPERSONA_FTS_ENABLED=false does), and must leave the empty-query path, which is
not a keyword search, as it was. Each case is checked under `fts5` too, so a
test that passes only because the row was never findable cannot pass here.
"""

import os
import tempfile

import pytest
import pytest_asyncio

# Override DB path BEFORE importing server modules
_tmpdir = tempfile.mkdtemp()
os.environ["CPERSONA_DB_PATH"] = os.path.join(_tmpdir, "test_lexical_engine.db")
os.environ["CPERSONA_EMBEDDING_MODE"] = "none"

from cpersona import memory_handlers as M  # noqa: E402
from cpersona.database import close_db, get_db  # noqa: E402

AGENT = "agent-lexical"
MEMORY = "The quarterly zephyrine audit closed with two findings."
EPISODE = "Reviewed the zephyrine audit plan with the team."
# Two CJK characters cannot match a trigram index, so the memory arm answers
# this one through its LIKE fallback.
SHORT_CJK = "パン"
SHORT_CJK_MEMORY = "駅前のパン屋が月曜に休む"


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    db = await get_db()
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM episodes")
    await db.commit()
    for content in (MEMORY, SHORT_CJK_MEMORY):
        stored = await M.do_store(AGENT, {"content": content, "source": {"System": "t"}})
        assert stored["result"] == "stored", stored
    await M.do_archive_episode(
        AGENT, [{"timestamp": "2026-06-14T00:00:00Z"}], summary=EPISODE, keywords="", resolved=False
    )
    yield
    await close_db()


async def _memories(query: str) -> list[str]:
    db = await get_db()
    return [r["content"] for r in await M._search_memories_keyword(db, AGENT, query, 10)]


async def _episodes(query: str) -> list[str]:
    db = await get_db()
    return [r["content"] for r in await M._search_episodes_fts(db, AGENT, query, 10)]


async def _arms(query: str) -> tuple[list[str], list[str]]:
    db = await get_db()
    episodes, memories = await M._lexical_arms(db, AGENT, query, 10, "", None, "", None)
    return [r["content"] for r in episodes], [r["content"] for r in memories]


@pytest.mark.asyncio
async def test_fts5_finds_the_memory_and_the_episode(monkeypatch):
    monkeypatch.setattr(M, "LEXICAL_ENGINE", "fts5")
    assert await _memories("zephyrine audit") == [MEMORY]
    assert await _episodes("zephyrine audit") == [f"[Episode] {EPISODE}"]
    assert await _arms("zephyrine audit") == ([f"[Episode] {EPISODE}"], [MEMORY])


@pytest.mark.asyncio
async def test_off_removes_both_arms(monkeypatch):
    monkeypatch.setattr(M, "LEXICAL_ENGINE", "off")
    assert await _memories("zephyrine audit") == []
    assert await _episodes("zephyrine audit") == []
    assert await _arms("zephyrine audit") == ([], [])


@pytest.mark.asyncio
async def test_off_does_not_fall_back_to_like(monkeypatch):
    monkeypatch.setattr(M, "LEXICAL_ENGINE", "fts5")
    assert await _memories(SHORT_CJK) == [SHORT_CJK_MEMORY]  # the LIKE fallback answers under fts5
    monkeypatch.setattr(M, "LEXICAL_ENGINE", "off")
    assert await _memories(SHORT_CJK) == []


@pytest.mark.asyncio
async def test_off_keeps_the_empty_query_path(monkeypatch):
    monkeypatch.setattr(M, "LEXICAL_ENGINE", "fts5")
    newest = await _memories("")
    assert sorted(newest) == sorted([MEMORY, SHORT_CJK_MEMORY])
    monkeypatch.setattr(M, "LEXICAL_ENGINE", "off")
    assert await _memories("") == newest


def test_unknown_value_falls_back_to_fts5(monkeypatch):
    from cpersona import config

    monkeypatch.setenv("CPERSONA_LEXICAL_ENGINE", "tantivyy")
    assert config._parse_choice("CPERSONA_LEXICAL_ENGINE", "fts5", ("fts5", "off")) == "fts5"
    monkeypatch.setenv("CPERSONA_LEXICAL_ENGINE", " OFF ")
    assert config._parse_choice("CPERSONA_LEXICAL_ENGINE", "fts5", ("fts5", "off")) == "off"
