"""The cue arm's remainder in the default mode (auto): through a usable coarse index, or not at all.

Design: `docs/BINARY_COARSE_SEARCH_DESIGN.md` §7. `CPERSONA_CUE_COARSE_ENABLED` has three
modes. "true" (on) searches a cue period's remainder through the coarse index or,
without one, the live store, at a cost that grows with the store. Unset (auto, the
default) asks the index only: without a usable one the remainder is not searched, the
recall is the one "false" (off) gives, and the response says the period was not
searched whole. The store and its cosines are those of `tests/test_cue_coarse.py`.

The coarse index suggestion (`cpersona/coarse_notice.py`), which tells an agent once
per session that its scope has outgrown the window with no index, is pinned here too:
it is the other half of "auto builds nothing".
"""

import os

import pytest
import pytest_asyncio

from cpersona import checks, coarse_index, coarse_notice, coarse_search, config, cue, vector
from cpersona import memory_handlers as M
from cpersona import reconstruct as R
from cpersona.database import get_db
from tests.test_cue_coarse import AGENT, CAP, ONE_HOT, PERIOD, QUERY, TOTAL, OneHotClient, _ids, _seed

TIME_CUE = {"after": "2026-03-10", "before": "2026-03-19", "confidence": "sure"}
REMAINDER = {"searched": False, "reason": "no_usable_index", "hint": M.CUE_REMAINDER_HINT}


def _clean_index():
    path = coarse_index.index_path("memories")
    for p in (path, path + ".tmp"):
        if os.path.exists(p):
            os.unlink(p)


@pytest_asyncio.fixture
async def db(monkeypatch):
    monkeypatch.setattr(vector, "_embedding_client", OneHotClient())
    monkeypatch.setattr(vector, "MAX_MEMORIES", CAP)
    monkeypatch.setattr(vector, "VECTOR_REACH", 0)
    monkeypatch.setattr(M, "MAX_MEMORIES", CAP)
    monkeypatch.setattr(M, "FTS_ENABLED", False)
    monkeypatch.setattr(config, "FAR_SEATS_ENABLED", False)
    conn = await get_db()
    _clean_index()
    await conn.execute("DELETE FROM memories")
    await conn.execute("DELETE FROM episodes")
    await conn.commit()
    yield conn
    await conn.execute("DELETE FROM memories")
    await conn.commit()
    _clean_index()


async def _arm(db, monkeypatch, mode: str):
    monkeypatch.setattr(config, "CUE_COARSE_MODE", mode)
    return await M._search_cue_arm(
        db, AGENT, QUERY, cue.DEPTH, PERIOD, channel="", project_id=None, source_id="",
        exclude_set=set(), query_vec=[ONE_HOT.tolist()],
    )


async def _recall(monkeypatch, mode: str, **kw):
    monkeypatch.setattr(config, "CUE_COARSE_MODE", mode)
    return await M.do_recall(AGENT, QUERY, 5, time_cue=TIME_CUE, **kw)


def _pairs(rows):
    return [(r["id"], r["_cosine"]) for r in rows]


async def _build(db):
    assert (await coarse_index.build_coarse_index(db, "memories"))["built"]


def _no_live_store(monkeypatch):
    async def detonate(*a, **kw):
        raise AssertionError("the live store was read")

    monkeypatch.setattr(coarse_search, "_from_live", detonate)


# ── the mode a value names ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "mode"),
    [
        (None, "auto"), ("", "auto"), ("auto", "auto"), (" AUTO ", "auto"),
        ("true", "on"), ("TRUE", "on"), (" true ", "on"),
        ("false", "off"), ("1", "off"), ("yes", "off"), ("on", "off"),
    ],
)
def test_the_setting_names_three_modes_and_unset_is_auto(raw, mode):
    """Only "true" turns the live store on, as before; unset is the new default."""
    assert config._cue_coarse_mode(raw) == mode


# ── the cue arm ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_without_an_index_is_off_and_never_reads_the_live_store(db, monkeypatch):
    ids = await _seed(db)
    off = await _arm(db, monkeypatch, "off")
    _no_live_store(monkeypatch)
    auto = await _arm(db, monkeypatch, "auto")
    assert _pairs(auto) == _pairs(off)
    assert ids[40] not in _ids(auto)


@pytest.mark.asyncio
async def test_auto_with_an_index_is_on(db, monkeypatch):
    ids = await _seed(db)
    on = await _arm(db, monkeypatch, "on")  # no index yet: the live store
    await _build(db)
    _no_live_store(monkeypatch)
    auto = await _arm(db, monkeypatch, "auto")
    assert _pairs(auto) == _pairs(on)
    assert _ids(auto)[:2] == [ids[40], ids[50]]


@pytest.mark.asyncio
async def test_on_without_an_index_still_reads_the_live_store(db, monkeypatch):
    """The 2.6.2 meaning of "true" is kept: the same records, whatever the index."""
    ids = await _seed(db)
    on = await _arm(db, monkeypatch, "on")
    assert _ids(on)[:2] == [ids[40], ids[50]]


# ── what the response says ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_without_an_index_returns_the_off_recall_and_says_so(db, monkeypatch):
    await _seed(db)
    off = await _recall(monkeypatch, "off")
    auto = await _recall(monkeypatch, "auto")
    assert auto["messages"] == off["messages"]
    assert "remainder" not in off["time_cue"]
    assert auto["time_cue"]["remainder"] == REMAINDER
    assert {k: v for k, v in auto["time_cue"].items() if k != "remainder"} == off["time_cue"]


@pytest.mark.asyncio
async def test_a_period_searched_whole_says_nothing_more(db, monkeypatch):
    """Through the index, the response is the one "on" gives: nothing was left out."""
    await _seed(db)
    await _build(db)
    on = await _recall(monkeypatch, "on")
    auto = await _recall(monkeypatch, "auto")
    assert auto["messages"] == on["messages"]
    assert auto["time_cue"] == on["time_cue"]
    assert "remainder" not in auto["time_cue"]


@pytest.mark.asyncio
async def test_a_period_within_the_cap_says_nothing_without_an_index(db, monkeypatch):
    """No remainder to leave out is not a remainder left out."""
    await _seed(db)
    monkeypatch.setattr(M, "MAX_MEMORIES", TOTAL)
    auto = await _recall(monkeypatch, "auto")
    assert "remainder" not in auto["time_cue"]


@pytest.mark.asyncio
async def test_on_never_reports_a_remainder_left_out(db, monkeypatch):
    await _seed(db)
    on = await _recall(monkeypatch, "on")
    assert "remainder" not in on["time_cue"]


@pytest.mark.asyncio
async def test_an_index_that_cannot_answer_is_skipped_and_reported(db, monkeypatch):
    """A candidate deleted since the build sends "on" to the live store; auto skips."""
    ids = await _seed(db)
    await _build(db)
    await db.execute("DELETE FROM memories WHERE id = ?", (ids[40],))
    await db.commit()
    _no_live_store(monkeypatch)
    rows = await _arm(db, monkeypatch, "auto")
    assert ids[50] not in _ids(rows)
    auto = await _recall(monkeypatch, "auto")
    assert auto["time_cue"]["remainder"] == REMAINDER


@pytest.mark.asyncio
async def test_the_trace_names_the_supplier_on_every_stage(db, monkeypatch):
    await _seed(db)
    auto = await _recall(monkeypatch, "auto", trace=True)
    assert auto["trace"]["stages"][0]["remainder"] == {"mode": "auto", "supplier": "skipped", "left_out": True}
    await _build(db)
    auto = await _recall(monkeypatch, "auto", trace=True)
    assert auto["trace"]["stages"][0]["remainder"] == {"mode": "auto", "supplier": "index"}
    off = await _recall(monkeypatch, "off", trace=True)
    assert "remainder" not in off["trace"]["stages"][0]


@pytest.mark.asyncio
async def test_a_skip_with_nothing_past_the_cap_leaves_the_stage_as_it_was(db, monkeypatch):
    """At the default, a store within the window traces exactly what the setting off does."""
    await _seed(db)
    monkeypatch.setattr(M, "MAX_MEMORIES", TOTAL)
    auto = await _recall(monkeypatch, "auto", trace=True)
    off = await _recall(monkeypatch, "off", trace=True)
    assert auto["trace"]["stages"] == off["trace"]["stages"]


# ── the health surface ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_reports_an_absent_index_under_auto_as_lost_reach(db, monkeypatch):
    await _seed(db)
    monkeypatch.setattr(checks, "INDEX_MATTERS_ROWS", 5)
    monkeypatch.setattr(config, "CUE_COARSE_MODE", "auto")
    [issue] = await checks.check_coarse_index(db, AGENT, False)
    assert issue["type"] == "coarse_index_absent"
    assert "severity" not in issue  # unstamped: the registry default, info
    assert "embedding_bytes_read_per_recall" not in issue
    assert issue["hint"].startswith(f"a time cue does not search the {TOTAL - CAP} records past the window")


@pytest.mark.asyncio
async def test_health_stamps_the_absence_warn_when_a_reader_pays_the_live_store(db, monkeypatch):
    await _seed(db)
    monkeypatch.setattr(checks, "INDEX_MATTERS_ROWS", 5)
    monkeypatch.setattr(config, "CUE_COARSE_MODE", "on")
    [issue] = await checks.check_coarse_index(db, AGENT, False)
    assert issue["severity"] == "warn"
    assert issue["embedding_bytes_read_per_recall"] > 0
    monkeypatch.setattr(config, "CUE_COARSE_MODE", "auto")
    monkeypatch.setattr(config, "FAR_SEATS_ENABLED", True)
    [issue] = await checks.check_coarse_index(db, AGENT, False)
    assert issue["severity"] == "warn"


@pytest.mark.asyncio
async def test_health_under_auto_reports_an_unusable_file_unstamped(db, monkeypatch):
    await _seed(db)
    monkeypatch.setattr(checks, "INDEX_MATTERS_ROWS", 5)
    monkeypatch.setattr(config, "CUE_COARSE_MODE", "auto")
    with open(coarse_index.index_path("memories"), "wb") as fh:
        fh.write(b"not an index")
    [issue] = await checks.check_coarse_index(db, AGENT, False)
    assert issue["type"] == "coarse_index_unusable"
    assert "severity" not in issue


# ── the suggestion ───────────────────────────────────────────────────────────


@pytest.fixture
def window(monkeypatch):
    monkeypatch.setattr(checks, "INDEX_MATTERS_ROWS", 5)
    monkeypatch.setattr(config, "CUE_COARSE_MODE", "auto")
    _clean_index()
    yield 10
    _clean_index()


def test_the_suggestion_starts_where_the_health_check_does(window):
    assert coarse_notice.notice(window + 4, window, "s", True) is None
    said = coarse_notice.notice(window + 5, window, "s", True)
    assert said["kind"] == "coarse_index"
    assert (said["records"], said["past_window"]) == (window + 5, 5)
    assert said["fix"] == {"tool": "check_health", "arguments": {"checks": ["coarse_index"], "fix": True}}
    assert "only if they agree" in said["message"]


def test_the_suggestion_is_said_once_per_session(window):
    assert coarse_notice.notice(100, window, "s1", True) is not None
    assert coarse_notice.notice(100, window, "s1", True) is None
    assert coarse_notice.notice(100, window, "s2", True) is not None


def test_without_a_session_key_the_suggestion_is_said_once_per_process(window):
    assert coarse_notice.notice(100, window, "", False) is not None
    assert coarse_notice.notice(100, window, "", False) is None


def test_the_suggestion_is_for_auto_only(window, monkeypatch):
    """An operator who set the mode chose; the suggestion is for the default."""
    for mode in ("on", "off"):
        monkeypatch.setattr(config, "CUE_COARSE_MODE", mode)
        assert coarse_notice.notice(100, window, f"s-{mode}", True) is None


def test_an_unusable_index_file_does_not_silence_the_suggestion(window):
    with open(coarse_index.index_path("memories"), "wb") as fh:
        fh.write(b"not an index")
    assert coarse_notice.notice(100, window, "s", True) is not None


@pytest.mark.asyncio
async def test_a_recall_carries_the_suggestion_once_and_reconstruct_forwards_it(db, monkeypatch):
    await _seed(db)
    monkeypatch.setattr(checks, "INDEX_MATTERS_ROWS", TOTAL - CAP)
    first = await _recall(monkeypatch, "auto", session_key="sess-a")
    assert (first["suggestion"]["records"], first["suggestion"]["past_window"]) == (TOTAL, TOTAL - CAP)
    again = await _recall(monkeypatch, "auto", session_key="sess-a")
    assert "suggestion" not in again
    rebuilt = await R.do_reconstruct(AGENT, QUERY, session_key="sess-b")
    assert rebuilt["suggestion"]["records"] == TOTAL


@pytest.mark.asyncio
async def test_a_recall_below_the_line_or_with_an_index_carries_no_suggestion(db, monkeypatch):
    await _seed(db)
    monkeypatch.setattr(checks, "INDEX_MATTERS_ROWS", TOTAL - CAP + 1)
    assert "suggestion" not in await _recall(monkeypatch, "auto", session_key="sess-c")
    monkeypatch.setattr(checks, "INDEX_MATTERS_ROWS", TOTAL - CAP)
    await _build(db)
    assert "suggestion" not in await _recall(monkeypatch, "auto", session_key="sess-d")
