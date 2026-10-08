"""CPERSONA_QUERY_SEGMENTER: the morpheme-aware variants leave a request's wording out.

`trigram` must stay the builder it always was. `morph` and `morph_overlap` must drop the
pieces made only of function words, keep an ASCII identifier as one phrase, and
(`morph_overlap`) keep the trigrams that bridge a particle between two short nouns. The
store case is built so that, under `trigram`, rows holding the request's wording outscore
the one row that names the identifier: a variant that kept the wording cannot pass it.
"""

import os
import tempfile

import pytest
import pytest_asyncio

pytest.importorskip("sudachipy")

# Override DB path BEFORE importing server modules
_tmpdir = tempfile.mkdtemp()
os.environ["CPERSONA_DB_PATH"] = os.path.join(_tmpdir, "test_query_segmenter.db")
os.environ["CPERSONA_EMBEDDING_MODE"] = "none"

from cpersona import config  # noqa: E402
from cpersona import memory_handlers as M  # noqa: E402
from cpersona.database import close_db, get_db  # noqa: E402

AGENT = "agent-segmenter"
REQUEST = "bug-191 について教えて"
TARGET = "bug-191 was fixed by pinning the parser to the old grammar."
# Three rows that hold the request's wording verbatim, and enough rows that hold none of
# it for FTS5's idf of that wording to stay positive (it is floored at half the rows).
WORDING = [f"手順{i}について教えてもらった記録" for i in range(3)]
NEUTRAL = [f"Neutral record number {i} about the weekly backup rotation." for i in range(45)]


def _phrases(query: str, segmenter: str, monkeypatch) -> list[str]:
    monkeypatch.setattr(config, "QUERY_SEGMENTER", segmenter)
    return M._fts_recall_phrases(query)


def test_trigram_is_the_builder_it_always_was(monkeypatch):
    assert _phrases(REQUEST, "trigram", monkeypatch) == [
        '"bug-191"', '"につい"', '"ついて"', '"いて教"', '"て教え"', '"教えて"',
    ]


def test_the_default_is_trigram(monkeypatch):
    monkeypatch.delenv("CPERSONA_QUERY_SEGMENTER", raising=False)
    assert config._parse_choice("CPERSONA_QUERY_SEGMENTER", "trigram", ("trigram", "morph", "morph_overlap")) == "trigram"


@pytest.mark.parametrize("segmenter", ["morph", "morph_overlap"])
def test_the_request_wording_is_left_out(segmenter, monkeypatch):
    assert _phrases(REQUEST, segmenter, monkeypatch) == ['"bug-191"']


def test_morph_keeps_content_words_and_identifiers(monkeypatch):
    assert _phrases("CVE-2024-3094 の影響範囲は？", "morph", monkeypatch) == [
        '"CVE-2024-3094"', '"影響範"', '"響範囲"',
    ]
    # し and ある are verbs marked not independent: they do not extend the run.
    assert _phrases("設定変更したことがある", "morph", monkeypatch) == ['"設定変"', '"定変更"']


def test_morph_overlap_bridges_a_particle_between_short_nouns(monkeypatch):
    # Two two-character nouns joined by の: morph has no run long enough for a trigram,
    # morph_overlap keeps the trigrams with two characters inside the nouns.
    assert _phrases("共通の契約について", "morph", monkeypatch) == []
    assert _phrases("共通の契約について", "morph_overlap", monkeypatch) == [
        '"共通の"', '"通の契"', '"の契約"', '"契約に"',
    ]
    assert _phrases("共通の契約について", "trigram", monkeypatch) == [
        '"共通の"', '"通の契"', '"の契約"', '"契約に"', '"約につ"', '"につい"', '"ついて"',
    ]


def test_unknown_segmenter_raises():
    from cpersona import query_segment

    with pytest.raises(ValueError):
        query_segment.cjk_terms("について教えて", "bigram")


@pytest_asyncio.fixture
async def store():
    db = await get_db()
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM episodes")
    await db.commit()
    for content in [TARGET, *WORDING, *NEUTRAL]:
        stored = await M.do_store(AGENT, {"content": content, "source": {"System": "t"}})
        assert stored["result"] == "stored", stored
    yield
    await close_db()


async def _first(segmenter: str, monkeypatch) -> str:
    monkeypatch.setattr(config, "QUERY_SEGMENTER", segmenter)
    db = await get_db()
    rows = await M._search_memories_keyword(db, AGENT, REQUEST, 1)
    return rows[0]["content"]


@pytest.mark.asyncio
async def test_the_wording_outranks_the_identifier_under_trigram(store, monkeypatch):
    assert await _first("trigram", monkeypatch) in WORDING


@pytest.mark.asyncio
@pytest.mark.parametrize("segmenter", ["morph", "morph_overlap"])
async def test_the_identifier_ranks_first_without_the_wording(segmenter, store, monkeypatch):
    assert await _first(segmenter, monkeypatch) == TARGET
