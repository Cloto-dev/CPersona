"""One string, two identities: what Unicode normalisation would change (N-04).

Nothing in this package normalises Unicode -- `unicodedata` is imported nowhere
outside the check these tests cover. Identity is byte equality after
`strip().lower()`, so a Japanese string written precomposed and the same string
written with a combining dakuten are two different memories.

Measured on this project's deployment before any of this was written: all 3,234
memories and all 657 episodes are ALREADY NFC, and no dedup key would newly
collide under NFC, NFD, NFKC or NFKD. The defect is latent here, not live --
which is why 2.5.x ships a detector and not a repair. A canonical identity has
to live BESIDE the original (the original is never rewritten), and that is a
column, which is a schema change, which this line does not take.

So these tests do two different jobs, and the split is deliberate:

  characterisation   what the identity rule does TODAY, recorded so the release
                     that changes it has a baseline that was not written after
                     the fact
  the detector       that the check reports the condition, names the characters
                     involved, and does not report things that merely look
                     unusual

NFC and not NFKC throughout. NFKC is a compatibility fold: it equates a
fullwidth parenthesis with an ASCII one and a circled digit with a plain one,
which are different characters an author chose. On the corpus above NFC moves
0 rows and NFKC moves 478, and that gap is the difference between the two
questions rather than a matter of degree.
"""

import sqlite3
import unicodedata

import pytest
import pytest_asyncio

from cpersona import checks, config, memory_handlers, session
from cpersona.database import get_db
from cpersona.utils import EXCLUDE_PREFIX_MIN_CHARS, _content_excluded

AGENT = "agent.unicode-identity"

# The case the finding names: a voiced Japanese syllable. Composed, then the
# same string with the voicing written as a combining mark.
DAKUTEN = "だんごの記録"
DAKUTEN_NFD = unicodedata.normalize("NFD", DAKUTEN)
# The half-voiced sibling (U+309A rather than U+3099), which is a separate
# codepoint and would be missed by anything keyed on the voiced mark alone.
HANDAKUTEN = "ぱんの記録"
HANDAKUTEN_NFD = unicodedata.normalize("NFD", HANDAKUTEN)


def test_the_fixtures_are_equivalent_but_not_equal():
    """Guard on the fixtures themselves. A pair that was already byte-equal
    would make every test below pass no matter what the identity rule is."""
    for composed, decomposed in ((DAKUTEN, DAKUTEN_NFD), (HANDAKUTEN, HANDAKUTEN_NFD)):
        assert composed != decomposed, f"{composed!r} has no decomposed form here"
        assert unicodedata.normalize("NFC", decomposed) == composed
        assert len(decomposed) > len(composed), "no combining mark was introduced"
    # Escapes, not literals. A bare combining mark in this source would be
    # silently recomposed by any tool that normalises the file, turning the
    # assertion into one that cannot fail for the reason it was written.
    assert "\u3099" in DAKUTEN_NFD, "the voiced mark is the point of this fixture"
    assert "\u309a" in HANDAKUTEN_NFD, "the half-voiced mark is a different codepoint"


@pytest_asyncio.fixture
async def clean_db():
    session.reset_pauses_for_tests()
    db = await get_db()
    for table in ("memories", "episodes", "profiles"):
        await db.execute(f"DELETE FROM {table}")
    await db.execute("DELETE FROM sqlite_sequence WHERE name IN ('memories','episodes','profiles')")
    await db.commit()
    yield db
    for table in ("memories", "episodes", "profiles"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()


# ---------------------------------------------------------------------------
# Characterisation: the identity rule as it stands
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_spellings_are_two_memories_today(clean_db):
    """The finding's acceptance criterion, inverted and recorded.

    When a canonical identity exists, the second write becomes a dedup skip.
    Until then it is a second row, and that has to be an assertion rather than
    an assumption -- otherwise the release that changes it cannot show what it
    changed.
    """
    first = await memory_handlers.do_store(AGENT, {"content": DAKUTEN})
    second = await memory_handlers.do_store(AGENT, {"content": DAKUTEN_NFD})
    assert first["result"] == "stored", first
    assert second["result"] == "stored", second
    assert first["id"] != second["id"]

    rows = await clean_db.execute_fetchall(
        "SELECT content FROM memories WHERE agent_id = ? ORDER BY id", (AGENT,)
    )
    assert [r[0] for r in rows] == [DAKUTEN, DAKUTEN_NFD], (
        "both spellings are stored verbatim; the original is never rewritten"
    )


def test_the_exclude_comparison_splits_on_spelling_today():
    """`exclude_contents` is `strip().lower()` and nothing else, so an entry the
    caller already holds in one spelling does not exclude the other."""
    long_enough = DAKUTEN * 8
    assert len(long_enough) >= EXCLUDE_PREFIX_MIN_CHARS
    decomposed = unicodedata.normalize("NFD", long_enough)
    assert _content_excluded(long_enough, {long_enough.lower()}) is True
    assert _content_excluded(decomposed, {long_enough.lower()}) is False


def test_fts5_does_not_normalise_either():
    """Measured rather than assumed, and pinned because the whole search half of
    this finding rests on it: the tokenizer is `unicode61`, which case-folds and
    strips diacritics for Latin but performs no canonical composition. A query
    in one spelling returns nothing for a row stored in the other."""
    con = sqlite3.connect(":memory:")
    con.execute("CREATE VIRTUAL TABLE t USING fts5(content, tokenize='unicode61')")
    con.execute("INSERT INTO t(content) VALUES (?)", (DAKUTEN_NFD,))
    hits_same = con.execute("SELECT count(*) FROM t WHERE t MATCH ?", (DAKUTEN_NFD,)).fetchone()[0]
    hits_other = con.execute("SELECT count(*) FROM t WHERE t MATCH ?", (DAKUTEN,)).fetchone()[0]
    con.close()
    assert hits_same == 1, "sanity: the row is findable by its own spelling"
    assert hits_other == 0, "a decomposed row is invisible to a composed query"


# ---------------------------------------------------------------------------
# The detector
# ---------------------------------------------------------------------------


async def _mem(conn, content, agent=AGENT):
    await conn.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp, metadata, created_at)"
        " VALUES (?, ?, '{}', '2026-01-01T00:00:00+00:00', '{}', '2026-01-01 00:00:00')",
        (agent, content),
    )
    await conn.commit()


async def _run(conn, fix=False):
    return await checks.deep_unnormalized_content(conn, AGENT, fix)


@pytest.mark.asyncio
async def test_the_detector_finds_the_decomposed_row_only(clean_db):
    await _mem(clean_db, DAKUTEN)
    await _mem(clean_db, DAKUTEN_NFD)
    await _mem(clean_db, "an ordinary ascii row")

    result = await _run(clean_db)
    assert result["count"] == 1, result
    assert result["by_table"] == {"memories": 1, "episodes": 0}
    assert result["rows_scanned"]["memories"] == 3
    assert result["complete"] is True


@pytest.mark.asyncio
async def test_the_sample_names_the_characters_that_compose(clean_db):
    """A known positive for the sample, not only for the count.

    The first version of this computed the sample per character -- and
    normalisation composes a SEQUENCE, so U+305F and U+3099 each normalise to
    themselves and the list came back EMPTY on every row it reported. A finding
    that is entirely about which characters are involved, naming none of them,
    is indistinguishable from a finding that is not looking.
    """
    await _mem(clean_db, DAKUTEN_NFD)
    sample = (await _run(clean_db))["samples"][0]
    assert sample["table"] == "memories"
    assert sample["decomposed_codepoints"], "the sample named no characters"
    assert "U+3099" in sample["decomposed_codepoints"], sample
    assert sample["excerpt"].startswith(DAKUTEN_NFD[:4])


@pytest.mark.asyncio
async def test_the_half_voiced_mark_is_found_too(clean_db):
    """U+309A is a different codepoint from U+3099. A detector keyed on the
    voiced mark alone would report `ぱ` as clean."""
    await _mem(clean_db, HANDAKUTEN_NFD)
    result = await _run(clean_db)
    assert result["count"] == 1, result
    assert "U+309A" in result["samples"][0]["decomposed_codepoints"], result


@pytest.mark.asyncio
async def test_an_emoji_with_a_variation_selector_is_not_a_finding(clean_db):
    """VS16 is not decomposable, so an emoji carrying one is already NFC.

    This is the false positive that would matter most: variation selectors are
    everywhere in modern text, and a check that flagged them would report most
    corpora as defective and be switched off.
    """
    emoji = "heart ❤️ and a family \U0001F468‍\U0001F469‍\U0001F466"
    assert unicodedata.normalize("NFC", emoji) == emoji, "fixture assumption"
    await _mem(clean_db, emoji)
    assert (await _run(clean_db))["count"] == 0


@pytest.mark.asyncio
async def test_compatibility_characters_are_not_a_finding(clean_db):
    """The NFC / NFKC line, pinned.

    A fullwidth parenthesis and a circled digit are what NFKC would fold and NFC
    leaves alone. They are 478 rows on this project's deployment, and reporting
    them would turn a canonical-identity check into a house-style check.
    """
    compat = "全角（かっこ）と丸数字 ① と…"
    assert unicodedata.normalize("NFC", compat) == compat
    assert unicodedata.normalize("NFKC", compat) != compat, "fixture assumption"
    await _mem(clean_db, compat)
    assert (await _run(clean_db))["count"] == 0


@pytest.mark.asyncio
async def test_episodes_are_scanned_as_well(clean_db):
    await clean_db.execute(
        "INSERT INTO episodes (agent_id, summary) VALUES (?, ?)", (AGENT, DAKUTEN_NFD)
    )
    await clean_db.commit()
    result = await _run(clean_db)
    assert result["by_table"] == {"memories": 0, "episodes": 1}, result
    assert result["samples"][0]["table"] == "episodes"


@pytest.mark.asyncio
async def test_the_check_writes_nothing_even_with_fix(clean_db):
    """Report-only, and that is the finding rather than an omission: a repair
    would rewrite stored text, and the canonical form belongs beside the
    original rather than on top of it."""
    await _mem(clean_db, DAKUTEN_NFD)
    await _run(clean_db, fix=True)
    rows = await clean_db.execute_fetchall(
        "SELECT content FROM memories WHERE agent_id = ?", (AGENT,)
    )
    assert rows[0][0] == DAKUTEN_NFD, "the original must survive the check"
    assert "unnormalized_content" not in checks.DEEP_FIX_CAPABLE


@pytest.mark.asyncio
async def test_past_the_cap_the_answer_says_it_is_a_sample(clean_db, monkeypatch):
    """`complete` is the difference between a total and a floor. Without it a
    capped scan reports a number an operator would read as the whole corpus."""
    # Unique on the ASCII suffix, never on the Japanese: the decomposed marks are
    # the axis being measured, and varying them would make the rows differ in the
    # one dimension the check reads.
    for i in range(3):
        await _mem(clean_db, f"{DAKUTEN_NFD} row {i}")
    monkeypatch.setattr(checks, "NORMALIZATION_SCAN_CAP", 2)
    result = await _run(clean_db)
    assert result["rows_scanned"]["memories"] == 2
    assert result["complete"] is False, result
    assert result["count"] == 2, "it reports what it scanned, not what it guessed"


def test_the_cap_default_is_the_documented_one():
    assert config.NORMALIZATION_SCAN_CAP == 10000
