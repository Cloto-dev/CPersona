"""bug-209: the SCORING_VERSION bump discipline gets a detector.

``cpersona/utils.py`` pins the calibration fingerprint contract in prose — "BUMP THIS
whenever a change shifts the confidence/score distribution" — and three real shifts
(bug-155, bug-207, bug-213) show the rule is load-bearing: ``ensure_calibrated_on_startup``
and ``deep_calibration_staleness`` can only see a stale gate through that string. Yet
PR #88 shifted the distribution, left the string alone, and sailed through the entire CI
matrix green; a human diff review was the only thing that caught it.

This test is the machine for that rule. It drives the two scoring surfaces the bump
comment names — ``_compute_confidence`` (branch structure and constants) and the episode
boundary penalty (``_episode_boundary_factor``) — across a fixed input grid under a frozen
clock, serialises every cell deterministically, and pins the SHA-256 of the whole table
against the SCORING_VERSION it was recorded for.

Decision table on mismatch:

- fingerprint unchanged                          -> pass (a bump alone never moves the
  distribution, so bumping without a behaviour change stays green by design)
- fingerprint moved, SCORING_VERSION unchanged   -> RED: the exact failure this test
  exists for — the distribution shifted and no bump recorded it
- fingerprint moved, SCORING_VERSION bumped      -> RED with re-pin instructions: the
  shift is presumably intentional; re-pinning here is the act that arms the detector
  for the next unbumped shift

The fingerprint input deliberately excludes SCORING_VERSION itself: folding the version
into the hash would make every bump self-consistent and void the check (a generator
authoring its own conformance record). ``test_fingerprint_input_excludes_scoring_version``
guards that structurally.
"""

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

import cpersona.utils as utils
from cpersona.memory_handlers import _episode_boundary_factor
from cpersona.utils import SCORING_VERSION

# The golden pair. Re-pin BOTH together, and only alongside a SCORING_VERSION bump
# (or when extending the grid itself — say so in the commit message if you do).
# 255a3 (bug-257): the episode-penalty exemption is a CALL-SITE membership change
# (_apply_recall_scoring skips episode rows); the two functions this grid drives are
# untouched, so the fingerprint deliberately stays the same while the version moves.
# The exemption itself is pinned behaviourally in test_255a3_episode_penalty_exemption.py.
GOLDEN_SCORING_VERSION = "255a3-episode-penalty-exempt"
GOLDEN_FINGERPRINT = "75fb2c84c901c1b6dfa82b18d202824299b6c9807f304d6688315647a080d027"

FIXED_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


class _FrozenDatetime(datetime):
    """datetime subclass whose now() is pinned to FIXED_NOW.

    A subclass (not a stub) so fromisoformat / arithmetic inside
    ``_parse_timestamp_utc`` keep working unchanged.
    """

    @classmethod
    def now(cls, tz=None):
        return FIXED_NOW if tz else FIXED_NOW.replace(tzinfo=None)


def _iso(hours_before_now: float) -> str:
    return (FIXED_NOW - timedelta(hours=hours_before_now)).isoformat()


def _round_floats(value):
    """Round floats to 10 significant digits recursively.

    The confidence dict is already rounded by the function itself; this pass exists so
    the raw ``_episode_boundary_factor`` cells (and any future unrounded output) cannot
    flap on last-ulp libm differences between platforms.
    """
    if isinstance(value, float):
        return float(f"{value:.10g}")
    if isinstance(value, dict):
        return {k: _round_floats(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_round_floats(v) for v in value]
    return value


# Grid axes. Values are deliberately literal (not derived from the config constants they
# exercise — deriving them would move the inputs together with the constant under test)
# and deliberately ragged (no value sits on a rounding boundary of the outputs).
_TIMESTAMPS = [
    ("future", _iso(-24.3)),
    ("written-now", _iso(0.0)),
    ("fresh", _iso(1.37)),
    ("mid", _iso(239.61)),
    ("old", _iso(2404.7)),
    ("missing", ""),  # bug-207 imputation branch
    ("garbage", "not-a-timestamp"),  # same branch via parse failure
]
_COSINES = [None, 0.0, 0.19, 0.47, 0.83, 1.0]
_TIME_RANGES = [0.0, 719.3]  # 0.0 exercises the flat DECAY_RATE branch
_NEWEST_AGES = [None, 503.9]  # None exercises the anchorless imputation fallback
_RECALL_COUNTS = [0, 3]
_LAST_RECALLED = [
    ("never", ""),
    ("recent", _iso(2.1 / 60)),  # inside RECENT_RECALL_WINDOW_MIN -> penalty branch
    ("long-ago", _iso(97.3)),  # boost decay branch, outside the penalty window
]

_BOUNDARY = FIXED_NOW - timedelta(hours=100.0)
_BOUNDARY_CELLS = [
    ("no-boundary", _iso(1.0), None),
    ("no-memory-ts", "", _BOUNDARY),
    ("garbage-memory-ts", "not-a-timestamp", _BOUNDARY),
    ("after-boundary", _iso(1.0), _BOUNDARY),
    ("at-boundary", _iso(100.0), _BOUNDARY),
    ("just-before", _iso(100.7), _BOUNDARY),
    ("well-before", _iso(153.3), _BOUNDARY),
    ("floor", _iso(9971.0), _BOUNDARY),
]


def _fingerprint(monkeypatch) -> str:
    monkeypatch.setattr(utils, "datetime", _FrozenDatetime)

    cells = []
    for ts_label, ts in _TIMESTAMPS:
        for cosine in _COSINES:
            for deep in (False, True):
                for resolved in (False, True):
                    for time_range in _TIME_RANGES:
                        for newest_age in _NEWEST_AGES:
                            for recall_count in _RECALL_COUNTS:
                                for lr_label, lr in _LAST_RECALLED:
                                    out = utils._compute_confidence(
                                        cosine,
                                        ts,
                                        resolved=resolved,
                                        deep=deep,
                                        time_range_hours=time_range,
                                        newest_age_hours=newest_age,
                                        recall_count=recall_count,
                                        last_recalled_at_str=lr,
                                    )
                                    cells.append(
                                        {
                                            "fn": "confidence",
                                            "ts": ts_label,
                                            "cos": cosine,
                                            "deep": deep,
                                            "resolved": resolved,
                                            "range": time_range,
                                            "newest": newest_age,
                                            "rc": recall_count,
                                            "lr": lr_label,
                                            "out": _round_floats(out),
                                        }
                                    )
    for label, mem_ts, boundary in _BOUNDARY_CELLS:
        cells.append(
            {
                "fn": "episode_boundary",
                "cell": label,
                "out": _round_floats(_episode_boundary_factor(mem_ts, boundary)),
            }
        )

    blob = json.dumps(cells, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def test_scoring_distribution_fingerprint(monkeypatch):
    current = _fingerprint(monkeypatch)
    if current == GOLDEN_FINGERPRINT:
        # bug-244: the PAIR is pinned on the passing path too. A SCORING_VERSION
        # bump for a change outside this grid (bug-213 bumped it for episode
        # timestamp plumbing) leaves the fingerprint identical, so the golden
        # version string silently goes stale — and the NEXT genuine unbumped
        # shift then takes the branch below and tells the developer the shift is
        # "presumably intentional, re-pin", which is exactly the blessing this
        # detector exists to withhold.
        assert SCORING_VERSION == GOLDEN_SCORING_VERSION, (
            "SCORING_VERSION moved but the scoring distribution did not, so the golden\n"
            "pair in this test is now out of step:\n"
            f"  SCORING_VERSION:        {SCORING_VERSION!r}\n"
            f"  GOLDEN_SCORING_VERSION: {GOLDEN_SCORING_VERSION!r}\n"
            "Re-pin GOLDEN_SCORING_VERSION on ANY bump, not only on one that moves the\n"
            "grid — a stale pair inverts this test's verdict on the next unbumped shift."
        )
        return
    if SCORING_VERSION == GOLDEN_SCORING_VERSION:
        pytest.fail(
            "The confidence/score distribution moved but SCORING_VERSION did not.\n"
            f"  SCORING_VERSION:     {SCORING_VERSION!r} (unchanged)\n"
            f"  golden fingerprint:  {GOLDEN_FINGERPRINT}\n"
            f"  current fingerprint: {current}\n"
            "Every stored calibration is trusted or discarded by this string alone\n"
            "(ensure_calibrated_on_startup / deep_calibration_staleness), so an unbumped\n"
            "shift makes every deployed gate silently stale — the bug-184 failure mode.\n"
            "If this shift is intentional: bump SCORING_VERSION in cpersona/utils.py (see\n"
            "the BUMP THIS comment) and re-pin GOLDEN_SCORING_VERSION + GOLDEN_FINGERPRINT\n"
            "in this test. If it is not intentional, fix the scoring change instead of\n"
            "re-pinning."
        )
    pytest.fail(
        "SCORING_VERSION was bumped and the distribution moved with it — presumably\n"
        "intentional, but the golden pair in this test is now stale and must be re-pinned\n"
        "so the NEXT unbumped shift is caught:\n"
        f'  GOLDEN_SCORING_VERSION = "{SCORING_VERSION}"\n'
        f'  GOLDEN_FINGERPRINT = "{current}"'
    )


def test_fingerprint_input_excludes_scoring_version(monkeypatch):
    """The fingerprint must never incorporate SCORING_VERSION itself.

    If the version string fed the hash, bumping it would re-pin the fingerprint in the
    same motion and the golden could never catch an unbumped shift — the check would be
    a generator authoring its own record. The serialized blob is rebuilt here the same
    way _fingerprint builds it, and the version string must be absent from it.
    """
    monkeypatch.setattr(utils, "datetime", _FrozenDatetime)
    sample = json.dumps(
        _round_floats(
            utils._compute_confidence(0.47, _iso(1.37), time_range_hours=719.3)
        ),
        sort_keys=True,
    )
    assert SCORING_VERSION not in sample
    # And the module-level golden must be pinned to a real hash, not left dangling.
    assert len(GOLDEN_FINGERPRINT) == 64, (
        "GOLDEN_FINGERPRINT is not a SHA-256 hex digest — pin it by running "
        "test_scoring_distribution_fingerprint and copying the printed fingerprint."
    )
