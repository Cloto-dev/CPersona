"""Regression tests for bug-213 (an episode's age was synthesised while the row carried one).

Diagnosis (measured 2026-08-14 against the production deployment, 505 episodes):

- ``episodes.start_time`` is nullable and 338 of 505 rows have NULL there, while
  ``episodes.created_at`` is ``TEXT NOT NULL DEFAULT (datetime('now'))`` and every row has
  a value. All four paths that turn an episode row into a recall result passed
  ``start_time or ""``, so two thirds of the episodes reached ``_compute_confidence``
  with no timestamp at all.

- Under bug-207 that means an IMPUTED age — the midpoint of the corpus's age range, a
  single flat number (1713h on that deployment) for rows whose real ages span months.
  Measured against the true ages: median error 950h, and a 7-day-old episode was scored
  as ``time_decay`` 0.7042 where its own recorded time gives 0.9822. On the gate
  (0.4470) that is a 0.8392 factor on the score, i.e. 0.046 of extra raw cosine demanded
  from exactly the recent episodes session-start grounding exists to surface.

- ``created_at`` is a measured stand-in, not an assumed one: over the 167 episodes
  holding both values it trails ``start_time`` by under 24h in 93.4% of cases
  (mean +8.8h, max +209h).

Fix: one helper, ``utils.episode_timestamp``, read by every episode read path — the two
vector branches (local scan and remote by-id fetch), the FTS retriever, and
``get_contents``. Nothing is written back to the row, and a row with no usable time in
either column still takes bug-207's imputation and still reports ``age_unknown``.

Why a read-side fallback rather than backfilling ``start_time`` from ``created_at``
(the other obvious fix): a backfill writes a claim the data does not support. ``created_at``
is when the episode was RECORDED, not when what it describes happened, and once written
into ``start_time`` that distinction is unrecoverable. Reading it keeps the approximation
where it belongs — in the ranking — and leaves the row honest.

Each test states what the UNFIXED code returns, so reverting the fallback turns an
assertion red rather than merely moving a number.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from cpersona import session
from cpersona.database import get_db
from cpersona.isolation import isolation_where
from cpersona.utils import _compute_confidence, episode_timestamp

AGENT = "created-at-agent"
CORPUS_SPAN_HOURS = 131 * 24.0  # the production corpus's real span
COSINE = 0.50


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


@pytest_asyncio.fixture
async def clean_db():
    session.reset_pauses_for_tests()
    db = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await db.execute(f"DELETE FROM {table}")
    await db.execute(
        "DELETE FROM sqlite_sequence WHERE name IN "
        "('memories','episodes','profiles','pending_memory_tasks')"
    )
    await db.commit()
    return db


async def _insert_episode(
    db,
    *,
    summary: str = "the raspberry pi episode",
    start_time: str | None,
    created_at: str,
    embedding: bytes | None = None,
    agent_id: str = AGENT,
) -> int:
    """Insert an episode row directly, because the shape under test is one no writer
    produces today: NULL start_time beside a populated created_at. That is what two
    thirds of the production rows look like, whatever wrote them."""
    cur = await db.execute(
        "INSERT INTO episodes (agent_id, project_id, channel, summary, keywords, "
        "start_time, end_time, resolved, embedding, created_at) "
        "VALUES (?, '', '', ?, '', ?, NULL, 0, ?, ?)",
        (agent_id, summary, start_time, embedding, created_at),
    )
    await db.commit()
    return cur.lastrowid


# --- the helper itself ---------------------------------------------------------------


def test_the_helper_prefers_the_episode_s_own_time() -> None:
    """created_at is the fallback, never the override: a row that knows when it happened
    is scored on that, so the fix cannot move the 167 rows that were already correct."""
    assert episode_timestamp("2026-01-01T00:00:00+00:00", "2026-06-01T00:00:00+00:00") == (
        "2026-01-01T00:00:00+00:00"
    )


def test_the_helper_falls_back_only_when_there_is_nothing_to_prefer() -> None:
    """UNFIXED (``start_time or ""``) both of these are "". NULL and empty string both
    occur: the column is nullable, and the read paths coerced NULL to "" for years."""
    assert episode_timestamp(None, "2026-06-01T00:00:00+00:00") == "2026-06-01T00:00:00+00:00"
    assert episode_timestamp("", "2026-06-01T00:00:00+00:00") == "2026-06-01T00:00:00+00:00"


def test_a_row_with_no_time_at_all_still_reports_nothing() -> None:
    """The handover to bug-207. The helper must not invent a value of its own — an
    episode with neither column usable stays timeless, and the imputation (which says
    age_unknown) is what places it."""
    assert episode_timestamp(None, None) == ""
    assert episode_timestamp(None, "") == ""


# --- every path that turns an episode row into a recall result ------------------------
#
# One test per read path, because the fallback is only as good as its least-updated call
# site: a retriever left on `start_time or ""` would score the same row differently from
# the one that found it, and the disagreement would be invisible in any single path's test.


@pytest.mark.asyncio
async def test_the_fts_retriever_reads_the_recorded_time(clean_db) -> None:
    """The path bug-207 measured — ``_search_episodes_fts`` is what passed "" for two
    thirds of the episodes. UNFIXED this returns "" and the assertion fails outright."""
    from cpersona.memory_handlers import _search_episodes_fts

    recorded = _iso(3)
    await _insert_episode(clean_db, start_time=None, created_at=recorded)

    rows = await _search_episodes_fts(clean_db, AGENT, "raspberry", 10)

    assert rows, "the episode was not found at all — the test's own query is broken"
    assert rows[0]["timestamp"] == recorded


@pytest.mark.asyncio
async def test_the_local_vector_scan_reads_the_recorded_time(clean_db) -> None:
    """The cosine branch, reached whenever CPERSONA_VECTOR_SEARCH_MODE is local (this
    deployment). UNFIXED: "". Uses the suite's deterministic fake embedding so the row
    ranks without a network call."""
    import numpy as np

    from cpersona.vector import _scan_episodes_local
    from tests.conftest import FakeEmbeddingClient, fake_embed_one

    recorded = _iso(3)
    blob = FakeEmbeddingClient.pack_embedding(fake_embed_one("the raspberry pi episode"))
    await _insert_episode(clean_db, start_time=None, created_at=recorded, embedding=blob)

    query_vec = np.array(fake_embed_one("the raspberry pi episode"), dtype=np.float32)
    candidates = await _scan_episodes_local(
        clean_db,
        isolation_where(agent_id=AGENT, project_id=None, channel=""),
        50,
        query_vec,
        len(query_vec),
        0.0,
        "",
        "",
    )

    assert candidates, "the episode did not clear the similarity gate"
    assert candidates[0][1]["timestamp"] == recorded


@pytest.mark.asyncio
async def test_the_remote_vector_fetch_reads_the_recorded_time(clean_db, monkeypatch) -> None:
    """The by-id hydration behind VECTOR_SEARCH_MODE=remote. It is a separate SELECT from
    the local scan's, so it can regress on its own; no test drove it before this one.
    UNFIXED: ""."""
    from cpersona import vector

    recorded = _iso(3)
    ep_id = await _insert_episode(clean_db, start_time=None, created_at=recorded)

    class _Resp:
        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> dict:
            return {"results": [{"id": f"ep:{ep_id}", "score": 0.91}]}

    class _Client:
        @staticmethod
        async def post(*_args, **_kwargs) -> _Resp:
            return _Resp()

    class _RemoteClient:
        """Only the surface _search_vector_remote touches: the URL it derives the
        /search endpoint from, and the client it posts with."""

        _http_url = "http://localhost:9999/embed"
        _client = _Client()

    monkeypatch.setattr(vector, "VECTOR_SEARCH_MODE", "remote")
    monkeypatch.setattr(vector, "_embedding_client", _RemoteClient())

    iso = isolation_where(agent_id=AGENT, project_id=None, channel="")
    results = await vector._search_vector_remote(
        clean_db,
        AGENT,
        "raspberry",
        10,
        0.0,
        iso_fetch=iso,
        iso_ep_fetch=iso,
        src_clause="",
        src_params=(),
        src_like="",
        channel="",
    )

    assert results, "the remote branch declined (None) or matched nothing — see sv-remote-empty"
    assert results[0]["timestamp"] == recorded


@pytest.mark.asyncio
async def test_get_contents_agrees_with_what_recall_showed(clean_db) -> None:
    """get_contents expands a ref recall already returned. A timestamp that disagreed
    with the retriever's would make one row read as two. UNFIXED: "" here while the
    retrievers (fixed) report the recorded time — the disagreement this pins."""
    from cpersona.memory_handlers import do_get_contents

    recorded = _iso(3)
    ep_id = await _insert_episode(clean_db, start_time=None, created_at=recorded)

    res = await do_get_contents(AGENT, [f"ep:{ep_id}"])

    assert res["items"], f"the ref did not resolve: {res}"
    assert res["items"][0]["timestamp"] == recorded


# --- what the fallback is worth, and what it does not take over -----------------------


@pytest.mark.asyncio
async def test_the_scored_age_is_the_real_one_not_the_corpus_midpoint(
    clean_db, monkeypatch
) -> None:
    """The consequence, driven through the real scoring pass rather than asserted on the
    formula. A 3-day-old episode scored on its recorded time reports age_hours ~72 and no
    age_unknown; UNFIXED it reports the imputed midpoint of the corpus age range (~1572h
    for this fixture) and age_unknown: true — the 950h median error measured in
    production, reproduced at fixture scale."""
    from cpersona import memory_handlers

    recorded = _iso(3)
    await _insert_episode(clean_db, start_time=None, created_at=recorded)
    for days in (131, 0.5):  # give the corpus a span for the imputation to have used
        res = await memory_handlers.do_store(AGENT, {"content": f"row {days}", "timestamp": _iso(days)})
        assert res["result"] == "stored", res

    # _apply_recall_scoring keeps only the score float on the row, so the age it was
    # computed from is observable exactly where it is used: at the scorer's own boundary.
    # Spying there also catches the regression a return-value assertion cannot — a call
    # site that reads created_at and then hands the scorer something else.
    seen: list[dict] = []
    real = memory_handlers._compute_confidence

    def spy(raw_cosine, timestamp_str, **kwargs):
        out = real(raw_cosine, timestamp_str, **kwargs)
        seen.append({"ts": timestamp_str, **out})
        return out

    monkeypatch.setattr(memory_handlers, "_compute_confidence", spy)
    monkeypatch.setattr(memory_handlers, "CONFIDENCE_ENABLED", True)
    rows = await memory_handlers._search_episodes_fts(clean_db, AGENT, "raspberry", 10)
    await memory_handlers._apply_recall_scoring(clean_db, AGENT, rows, False)

    assert seen, "the scoring pass never reached _compute_confidence"
    confidence = seen[0]
    assert confidence["ts"] == recorded, (
        f"the scorer was handed {confidence['ts']!r} for a row recorded at {recorded!r}"
    )
    assert "age_unknown" not in confidence, (
        f"the episode was scored as undated though it carries a recorded time: {confidence}"
    )
    assert confidence["age_hours"] == pytest.approx(72.0, abs=2.0)


@pytest.mark.asyncio
async def test_the_value_the_column_default_writes_is_one_the_scorer_can_read(clean_db) -> None:
    """The fallback is worth nothing unless the value actually stored parses. Every test
    above supplies an ISO-8601 string with an offset; SQLite's ``datetime('now')`` — what
    the DEFAULT writes, and what all 505 production rows carry — emits
    "YYYY-MM-DD HH:MM:SS": a space separator and no offset at all. If that failed to parse
    the row would land back on the imputation and no other test here would notice, so this
    one stores through the DEFAULT and reads the age back out.

    The naive half matters as much as the separator: a naive value is UTC by invariant
    (bug-114), and a host in a non-UTC zone that assumed local time would put a row written
    this second nine hours into the past (or future). The age assertion below is what
    catches that, on the JST host this is developed on."""
    from cpersona.memory_handlers import _search_episodes_fts

    await clean_db.execute(
        "INSERT INTO episodes (agent_id, project_id, channel, summary, keywords, "
        "start_time, end_time, resolved) VALUES (?, '', '', ?, '', NULL, NULL, 0)",
        (AGENT, "the raspberry pi episode"),
    )
    await clean_db.commit()

    rows = await _search_episodes_fts(clean_db, AGENT, "raspberry", 10)
    stored = rows[0]["timestamp"]

    assert stored and "T" not in stored and "+" not in stored, (
        f"the shape under test is SQLite's datetime('now'), got {stored!r} — if a writer "
        "started storing ISO strings here this test is no longer covering production"
    )
    scored = _compute_confidence(
        COSINE, stored, time_range_hours=CORPUS_SPAN_HOURS, newest_age_hours=1.0
    )
    assert "age_unknown" not in scored, f"the recorded time did not parse: {stored!r}"
    assert scored["age_hours"] == pytest.approx(0.0, abs=1.0)


@pytest.mark.asyncio
async def test_an_episode_with_no_usable_time_still_reaches_the_imputation(clean_db) -> None:
    """The fallback narrows bug-207's branch; it must not close it. created_at is NOT NULL
    but an empty string satisfies that, and imported rows can carry unparseable text — such
    a row must still arrive at the retriever with "" so the imputation places it and says
    so, rather than being handed a value that parses to nonsense."""
    from cpersona.memory_handlers import _search_episodes_fts

    await _insert_episode(clean_db, start_time=None, created_at="")

    rows = await _search_episodes_fts(clean_db, AGENT, "raspberry", 10)

    assert rows and rows[0]["timestamp"] == ""
    imputed = _compute_confidence(
        COSINE, rows[0]["timestamp"], time_range_hours=CORPUS_SPAN_HOURS, newest_age_hours=1.0
    )
    assert imputed["age_unknown"] is True


def test_the_recent_episode_the_fallback_rescues_is_worth_rescuing() -> None:
    """States the gate arithmetic the fix exists for, so a change to the decay constants
    that quietly erases the difference fails here with the numbers visible. A 3-day-old
    episode against the imputed midpoint of a 131-day corpus: the imputation costs it a
    factor on the score, which the gate converts into extra raw cosine demanded of the
    row for no reason the data supports."""
    dated = _compute_confidence(
        COSINE, _iso(3), time_range_hours=CORPUS_SPAN_HOURS, newest_age_hours=1.0
    )
    imputed = _compute_confidence(
        COSINE, "", time_range_hours=CORPUS_SPAN_HOURS, newest_age_hours=1.0
    )
    assert imputed["score"] < dated["score"], (
        f"imputed {imputed} did not score below the row's own recorded age {dated} — the "
        "fallback would then be pointless, and this test is how that is noticed"
    )
