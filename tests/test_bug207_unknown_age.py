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

Fix: an unknown age is placed at the middle of the corpus's own span
(``max(MIN_TIME_RANGE_HOURS, time_range_hours) / 2``) — neutral rather than newest — and
the response carries ``age_unknown: true`` so a caller can tell an imputed age from an
observed one. No timestamp is written back to the row: the data stays as it is.

Each test states what the UNFIXED code does, so that reverting the fix turns the
assertion red rather than merely lowering a number.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cpersona.config import MIN_TIME_RANGE_HOURS
from cpersona.utils import _compute_confidence

CORPUS_SPAN_HOURS = 131 * 24.0  # the production corpus's real span, to scale the test
COSINE = 0.50                   # held equal everywhere: only the time axis may differ


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _score(timestamp: str, *, span: float = CORPUS_SPAN_HOURS) -> float:
    return _compute_confidence(COSINE, timestamp, time_range_hours=span)["score"]


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
