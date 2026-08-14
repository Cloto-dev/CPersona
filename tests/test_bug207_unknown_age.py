"""Regression tests for bug-207 (a row of unknown age was scored as written this instant).

Diagnosis (measured 2026-08-14 against the production deployment and a 1,545-document
benchmark corpus):

- ``utils._compute_confidence`` set ``age_hours = 0.0`` and only overwrote it when the
  timestamp parsed. An empty or malformed timestamp therefore produced ``age_hours = 0``,
  which is not "unknown" — it is the age of a row written this instant, and it yields the
  full ``time_decay`` of 1.0 that no dated row can reach.

- Under ``CONFIDENCE_ENABLED`` the confidence score is the ranking key AND the quality
  gate's signal, so those rows sorted above every dated row. On the benchmark corpus the
  undated group was the only group that GAINED reach when confidence was turned on
  (+19.0pt) while every dated quartile lost, the newest most of all (-39.3pt).

- The path is live in production: all 2,221 memories carry a timestamp, but 333 of 500
  episodes have no ``start_time``, and ``_search_episodes_fts`` passes it through as ""
  (``"timestamp": row[2] or ""``). Two thirds of the episodes outranked every dated
  memory on the time axis.

Fix: an unknown age is placed in the middle of the corpus's own AGE range — neutral rather
than newest — and the response carries ``age_unknown: true`` so a caller can tell an
imputed age from an observed one. No timestamp is written back to the row: the data stays
as it is.

The anchor is the load-bearing half, and the first implementation did not have it. Half of
``time_range_hours`` alone is a midpoint only while the newest row is roughly now, because
the span is the corpus's internal WIDTH (MAX - MIN) whereas every dated row's age is
measured from now. On a scope whose newest row is itself old the two diverge and the
original defect returns whole: with oldest 51d / newest 21d, half the 720h width imputes
360h, which outranks the 504h newest row. ``newest_age_hours`` closes that — the imputed
age is ``newest_age + width / 2``, i.e. ``((now - newest) + (now - oldest)) / 2`` — and
collapses to the newest row's own age on a scope holding one timestamp, which ties instead
of winning. ``_apply_recall_scoring`` computes it from the same MIN/MAX query as the span.

Tests come in two families and both matter:

- **anchored** — the production path (``newest_age_hours`` supplied). These are what pin
  the dormant-scope and single-timestamp cases.
- **unanchored** — the fallback for callers that never computed a span (gate calibration,
  an empty scope). It keeps the pre-anchor placement and still assumes a live corpus; the
  tests pin that documented shape so it cannot drift into looking correct.

Each test states what the UNFIXED code does, so that reverting the fix turns the
assertion red rather than merely lowering a number.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from cpersona._vendored_mcp_common import no_persist
from cpersona.config import MIN_TIME_RANGE_HOURS
from cpersona.database import get_db
from cpersona.utils import _compute_confidence


@pytest_asyncio.fixture
async def clean_db():
    """Same shape as the write-path suite's fixture: a real DB with the scored tables
    emptied, so the MIN/MAX the anchor comes from is exactly what this test stored."""
    no_persist.resume()
    db = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await db.execute(f"DELETE FROM {table}")
    await db.execute(
        "DELETE FROM sqlite_sequence WHERE name IN "
        "('memories','episodes','profiles','pending_memory_tasks')"
    )
    await db.commit()
    return db

CORPUS_SPAN_HOURS = 131 * 24.0  # the production corpus's real span, to scale the test
COSINE = 0.50                   # held equal everywhere: only the time axis may differ


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _score(
    timestamp: str,
    *,
    span: float = CORPUS_SPAN_HOURS,
    newest_age: float | None = None,
) -> float:
    return _compute_confidence(
        COSINE, timestamp, time_range_hours=span, newest_age_hours=newest_age
    )["score"]


@pytest.mark.parametrize("missing", ["", "not-a-timestamp", "2026-13-45T99:99:99Z"])
def test_unknown_age_does_not_outrank_a_fresh_row(missing: str) -> None:
    """UNFIXED: the undated row scores 0.7385 against a one-hour-old row's 0.7387-ish —
    it ties or wins because age_hours stayed 0.0. The assertion is strict `<`, so the
    old behaviour (equal-or-greater) fails rather than passing by a rounding accident."""
    fresh = _score(_iso(1 / 24))          # one hour old
    unknown = _score(missing)
    assert unknown < fresh, (
        f"a row whose age is unknown ({unknown}) must not outrank one known to be an "
        f"hour old ({fresh})"
    )


def test_unknown_age_is_not_treated_as_the_oldest_either() -> None:
    """The complement of the test above, and the reason the fix is 'middle' rather than
    'floor': over-correcting to maximum age would bury undated rows instead of placing
    them. UNFIXED this passes trivially; it exists so a later 'fix' that pessimises
    unknown rows cannot land unnoticed."""
    oldest = _score(_iso(131))
    unknown = _score("")
    assert unknown > oldest, (
        f"unknown age ({unknown}) must not be scored below the oldest dated row ({oldest})"
    )


def test_unknown_age_lands_between_the_newest_and_the_oldest() -> None:
    """States the invariant the two tests above bracket: neutral, not an extreme."""
    newest, oldest, unknown = _score(_iso(0.5)), _score(_iso(131)), _score("")
    assert oldest < unknown < newest


def test_the_imputed_age_follows_the_corpus_span() -> None:
    """The imputation is derived from the caller's own time_range_hours, so a corpus with
    a different span imputes a different age. UNFIXED both spans report age_hours 0.0 and
    this fails on the first assertion."""
    narrow = _compute_confidence(COSINE, "", time_range_hours=48.0)
    wide = _compute_confidence(COSINE, "", time_range_hours=CORPUS_SPAN_HOURS)
    assert narrow["age_hours"] == pytest.approx(24.0)
    assert wide["age_hours"] == pytest.approx(CORPUS_SPAN_HOURS / 2)
    assert wide["age_hours"] > narrow["age_hours"]


def test_no_range_information_falls_back_to_the_minimum_span() -> None:
    """time_range_hours=0 is what a caller passes when it could not compute a span (a
    single-row scope, or a corpus with no usable timestamps). There is no median to take,
    so the floor the rest of the function already uses (MIN_TIME_RANGE_HOURS) supplies
    one. UNFIXED this reports 0.0."""
    c = _compute_confidence(COSINE, "", time_range_hours=0.0)
    assert c["age_hours"] == pytest.approx(MIN_TIME_RANGE_HOURS / 2)
    assert c["score"] < _compute_confidence(COSINE, _iso(1 / 60), time_range_hours=0.0)["score"]


def test_age_unknown_is_reported_only_when_the_age_was_imputed() -> None:
    """The disclosure half of the fix. A caller must be able to tell an imputed age from
    an observed one; UNFIXED the key does not exist at all, so both assertions fail
    together and deleting the key later fails the first one alone."""
    assert _compute_confidence(COSINE, "", time_range_hours=CORPUS_SPAN_HOURS)["age_unknown"] is True
    dated = _compute_confidence(COSINE, _iso(3), time_range_hours=CORPUS_SPAN_HOURS)
    assert "age_unknown" not in dated


def test_deep_recall_still_reports_the_imputation() -> None:
    """deep=True flattens time_decay to 1.0, so the score is age-independent — but the
    reported age is still imputed and must still say so, or a deep caller reading
    age_hours would take an invented number for a measured one."""
    c = _compute_confidence(COSINE, "", deep=True, time_range_hours=CORPUS_SPAN_HOURS)
    assert c["age_unknown"] is True
    assert c["age_hours"] == pytest.approx(CORPUS_SPAN_HOURS / 2)


def test_a_dated_row_is_unaffected_by_the_change() -> None:
    """The fix must not move rows that carry a timestamp. Pinned against a recomputation
    of the same formula rather than a literal, so it survives a decay-constant change but
    still fails if the dated branch starts imputing."""
    ts = _iso(30)
    observed = _compute_confidence(COSINE, ts, time_range_hours=CORPUS_SPAN_HOURS)
    expected_age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds() / 3600
    assert observed["age_hours"] == pytest.approx(expected_age, abs=0.1)
    assert "age_unknown" not in observed


# --- anchored path: the corpus's AGE range, not its width ---------------------------
#
# A scope 30 days wide whose newest row is already three weeks old. Half the width (15d)
# is younger than every row in it; the midpoint of its age range (21d + 15d = 36d) is not.

DORMANT_SPAN = 30 * 24.0
DORMANT_NEWEST_AGE = 21 * 24.0


def test_a_dormant_scope_keeps_the_unknown_row_inside_the_dated_range() -> None:
    """The case the first implementation missed. WITHOUT the anchor the imputed age is
    15d — younger than the 21d newest row — and the first assertion fails, which is the
    original defect reproduced exactly on a scope that has simply gone quiet."""
    newest = _score(_iso(21), span=DORMANT_SPAN, newest_age=DORMANT_NEWEST_AGE)
    oldest = _score(_iso(51), span=DORMANT_SPAN, newest_age=DORMANT_NEWEST_AGE)
    unknown = _score("", span=DORMANT_SPAN, newest_age=DORMANT_NEWEST_AGE)
    assert unknown < newest, (
        f"on a dormant scope the unknown row ({unknown}) still outranks the newest dated "
        f"row ({newest}) — the imputation is anchored to the corpus width, not to now"
    )
    assert oldest < unknown, f"unknown ({unknown}) must not fall below the oldest ({oldest})"


def test_the_imputed_age_is_the_midpoint_of_the_age_range() -> None:
    """States the formula the placement above depends on, so a change to it fails here
    with the arithmetic visible rather than only as a reordering somewhere else."""
    c = _compute_confidence(
        COSINE, "", time_range_hours=DORMANT_SPAN, newest_age_hours=DORMANT_NEWEST_AGE
    )
    assert c["age_hours"] == pytest.approx(DORMANT_NEWEST_AGE + DORMANT_SPAN / 2)
    assert c["age_unknown"] is True


def test_a_single_timestamp_scope_ties_rather_than_wins() -> None:
    """Width 0 — one memory in the scope, or several sharing a timestamp. There is no
    range to take a midpoint of, so the imputed age collapses onto that one row's age and
    the two score the same. UNANCHORED this imputes MIN_TIME_RANGE_HOURS/2 = 12h and the
    unknown row beats a week-old one outright."""
    dated = _score(_iso(7), span=0.0, newest_age=7 * 24.0)
    unknown = _score("", span=0.0, newest_age=7 * 24.0)
    assert unknown == pytest.approx(dated, abs=1e-3), (
        f"a lone dated row ({dated}) and a row of unknown age ({unknown}) should tie"
    )


def test_dropping_the_anchor_reproduces_the_defect() -> None:
    """Pins the divergence itself: the same scope scored with and without the anchor puts
    the unknown row on opposite sides of the newest dated row. This is what fails if a
    call site stops passing newest_age_hours, and it also documents that the unanchored
    fallback is not a second correct answer — it is the pre-fix placement, kept only for
    callers that never computed a span."""
    newest = _score(_iso(21), span=DORMANT_SPAN)
    anchored = _score("", span=DORMANT_SPAN, newest_age=DORMANT_NEWEST_AGE)
    unanchored = _score("", span=DORMANT_SPAN)
    assert unanchored > newest, "the unanchored fallback still assumes a live corpus"
    assert anchored < newest, "the anchor is what places the row inside the dated range"


@pytest.mark.asyncio
async def test_the_scoring_pass_computes_and_hands_over_the_anchor(clean_db, monkeypatch) -> None:
    """Every test above scores through _compute_confidence directly, so none of them can
    see a call site that stops supplying newest_age_hours — the argument would simply
    default to None and the ranking would quietly revert to the width-only placement.
    This drives the real scoring pass over stored rows and asserts both halves: that the
    anchor is derived from the same MIN/MAX the span comes from, and that it reaches the
    scorer on every row."""
    from cpersona import memory_handlers

    for days, text in ((51, "the oldest row"), (21, "the newest row")):
        res = await memory_handlers.do_store(
            "anchor-agent", {"content": text, "timestamp": _iso(days)}
        )
        assert res["result"] == "stored", res

    seen: list[float | None] = []
    real = memory_handlers._compute_confidence

    def spy(*args, **kwargs):
        seen.append(kwargs.get("newest_age_hours"))
        return real(*args, **kwargs)

    monkeypatch.setattr(memory_handlers, "_compute_confidence", spy)
    monkeypatch.setattr(memory_handlers, "CONFIDENCE_ENABLED", True)

    rows = [{"id": 1, "content": "the oldest row", "timestamp": _iso(51), "_cosine": COSINE}]
    _, span, _, anchor = await memory_handlers._apply_recall_scoring(
        clean_db, "anchor-agent", rows, False
    )

    assert span == pytest.approx(30 * 24.0, abs=1.0), "span is still MAX - MIN"
    assert anchor is not None and anchor == pytest.approx(21 * 24.0, abs=1.0), (
        f"the anchor must be how old the newest row is right now, got {anchor}"
    )
    assert seen, "the scoring pass never reached _compute_confidence"
    assert all(a is not None for a in seen), (
        f"a call site scored a row without the anchor: {seen}"
    )
