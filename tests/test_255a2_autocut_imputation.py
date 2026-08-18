"""Pin the bug-207 imputation x autocut interaction (an earlier decision).

Finding (measured 2026-08-15 against a production snapshot, in-process do_recall):
``_autocut`` decides its cut point from the RELATIVE gap ``gap / max_score`` against
``AUTOCUT_MIN_GAP_RATIO``. bug-207's unknown-age imputation compresses only the undated
rows' scores, so in a list that mixes dated and undated rows the ratios are not
preserved and the cut point moves — at limit=10 the imputation deactivated autocut
entirely (5 -> 7 rows returned), at limit=6-8 it cut earlier (4 -> 3). The direction is
not constant. Neither existing tooth catches this path: the SCORING_VERSION fingerprint
guards calibration staleness only, and the bug-207 golden grid pins
``_compute_confidence`` in isolation.

Why this is pinned rather than fixed: the bug-213 created_at fallback (2.5.5a1) starves
the interaction of fuel. On the production deployment every episode has ``created_at``
and every memory has ``timestamp`` (measured 2026-08-18: 0 rows can reach imputation),
so imputed rows no longer occur outside of data loss. The interaction itself is the
correct behaviour of a relative-gap autocut fed a changed score distribution; what
matters is that it stays VISIBLE. These tests state what each side of the fallback
does, so whoever changes the imputation, the fallback, or the autocut signal gets a
red assertion instead of a silent membership change.

Fixture geometry (five rows spanning a 131-day corpus, one old high-cosine row):
scored via the real ``_compute_confidence`` and cut via the real ``_autocut``, the
all-dated list cuts to 2 rows; turning ONE row's timestamp into the empty string —
exactly what every episode read path passed for two thirds of the production episodes
before bug-213 — re-scores that row at the imputed corpus midpoint and the same list
cuts to 3. The post-fallback invariant is the converse: as long as no row is imputed,
serving a row's time via ``episode_timestamp(None, created_at)`` cuts identically to an
explicit ``start_time``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from cpersona.memory_handlers import _autocut
from cpersona.utils import _compute_confidence, episode_timestamp

# The production corpus's shape at measurement time: 131 days of span, newest row ~2h old.
SPAN_HOURS = 131 * 24.0
NEWEST_AGE_HOURS = 2.0

# (days_ago, raw_cosine) — row 2 is the old, strongly-matching row (a historical episode
# in the production finding). Its dated score sits below the top pair, so the all-dated
# list shows its largest gap right after the pair; imputing row 2 to the corpus midpoint
# (~66 days, younger than its real 120) lifts it enough to move the largest gap.
ROWS = [
    (3, 0.62),
    (5, 0.60),
    (120, 0.66),
    (8, 0.40),
    (10, 0.38),
]


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _scored(timestamps: list[str]) -> list[dict]:
    rows = []
    for i, ((_, cosine), ts) in enumerate(zip(ROWS, timestamps)):
        conf = _compute_confidence(
            cosine,
            ts,
            time_range_hours=SPAN_HOURS,
            newest_age_hours=NEWEST_AGE_HOURS,
        )
        rows.append({"id": i, "_confidence_score": conf["score"], "_conf": conf})
    rows.sort(key=lambda r: r["_confidence_score"], reverse=True)
    return rows


def test_imputed_row_moves_the_autocut_cut_point():
    """One undated row among dated ones shifts the cut — the an earlier decision mechanism.

    This is the pre-bug-213 world: the row's real time exists but no read path
    delivered it, so ``_compute_confidence`` imputes. The imputation changes one
    score, the relative gaps change everywhere, and rows that did not change at all
    gain or lose visibility. If this assertion starts failing, the interaction the
    production measurement documented has been altered — deliberately or not.
    """
    dated = _scored([_iso(d) for d, _ in ROWS])
    mixed = _scored(["" if i == 2 else _iso(d) for i, (d, _) in enumerate(ROWS)])

    assert not any(r["_conf"].get("age_unknown") for r in dated)
    imputed = [r for r in mixed if r["_conf"].get("age_unknown")]
    assert [r["id"] for r in imputed] == [2]

    assert len(_autocut(dated)) == 2
    assert len(_autocut(mixed)) == 3


def test_fallback_served_rows_cut_identically_to_explicit_dates():
    """The post-bug-213 invariant: no imputation, no cut shift.

    A row whose time arrives via the created_at fallback scores exactly as if
    start_time carried the same value, so the autocut cut point is decided by the
    scores alone. Reverting the fallback (or rerouting a read path around it) turns
    the undated arm back into the imputed arm above and this diverges.
    """
    explicit = _scored([_iso(d) for d, _ in ROWS])
    fallback = _scored([episode_timestamp(None, _iso(d)) for d, _ in ROWS])

    assert not any(r["_conf"].get("age_unknown") for r in fallback)
    assert [r["_confidence_score"] for r in fallback] == [
        r["_confidence_score"] for r in explicit
    ]
    assert len(_autocut(fallback)) == len(_autocut(explicit)) == 2
