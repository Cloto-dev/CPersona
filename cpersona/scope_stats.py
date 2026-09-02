"""Process-local cache for the per-scope aggregates the recall path reads.

Every recall re-derives up to three full scans of the isolation scope it is about to
answer before it can answer it: ``COUNT(*)`` over ``memories`` and over ``episodes``
for the pool the quality gate governs (bug-216), and — under ``CONFIDENCE_ENABLED`` —
``MIN/MAX(timestamp)`` over ``memories`` for the confidence span (bug-107 / bug-237).
Their answers change only when a row is written, so a read-heavy agent pays for the
same scans on every call.

This module owns those statements and hands the raw results back. Nothing downstream
moves: the parsing, the ``_parse_timestamp_utc`` calls and the adaptive-threshold
arithmetic stay at their call sites and consume exactly the values the queries
returned, so a cached recall and an uncached one score identically.

The two halves are cached under one key but computed independently, so a
confidence-off install never issues the MIN/MAX at all — it is not merely cheap
there, it is absent.

An entry is served when BOTH hold:

- ``database.write_generation()`` is unchanged. The write seam bumps it after every
  commit, so a write THIS process makes invalidates exactly, with no per-write-site
  annotation to forget. The one exception is spelled out at its call site:
  ``transaction(scope_stats_neutral=True)`` withholds the bump for a statement that
  provably moves neither a row count nor a ``timestamp`` (the recall-count
  bookkeeping bump, which would otherwise invalidate the cache on the very recall
  that filled it — the confidence-on case this cache exists for).
- the entry is younger than ``SCOPE_STATS_TTL_SECONDS``. This is the honest bound on
  a writer OUTSIDE this process (a second server on the same file, an operator's
  sqlite3): such a write is invisible to the generation counter, so it is seen
  within the TTL rather than immediately. What that costs is bounded and worth
  stating plainly — these values scale a confidence curve and size the gate pool, so
  a stale one changes how rows are scored, never which rows exist or what a row
  says. ``PRAGMA data_version`` was the alternative and cannot be used: it moves on
  the read connection for every commit made on the write connection, including the
  neutral one above, so it cannot be made blind to a write the generation counter is
  deliberately blind to.

The clock is ``_clock`` (``time.monotonic`` by default) so tests can advance time
without sleeping. Freshly stored entries carry a real timestamp; ``None`` is the
"never stamped" value, because monotonic clocks legitimately read 0.0 shortly after
boot and a 0.0 sentinel would make a genuine reading at that moment mean its
opposite.
"""

from __future__ import annotations

import dataclasses
import time
from typing import Callable

from cpersona import database
from cpersona.config import SCOPE_STATS_CACHE_ENABLED, SCOPE_STATS_TTL_SECONDS
from cpersona.isolation import isolation_where

__all__ = ["clear", "get_pool_counts", "get_span"]

# Injectable clock (see the module docstring). Monotonic, not wall time: the TTL is a
# duration, and a wall clock can step backwards under NTP, which would freeze an
# entry for as long as the correction.
_clock: Callable[[], float] = time.monotonic


@dataclasses.dataclass
class _Entry:
    """One isolation scope's cached halves, each with its own stamp.

    Stamped independently because they are requested independently: a
    confidence-off recall asks only for the counts, and stamping the (never
    computed) span half alongside them would make a later confidence-on call
    believe it had a span of ``None`` in hand.
    """

    span: tuple[str | None, str | None] | None = None
    span_generation: int = -1
    span_at: float | None = None
    counts: tuple[int, int] | None = None
    counts_generation: int = -1
    counts_at: float | None = None


# (agent_id, project_id, channel) -> _Entry. The key is the isolation triple exactly
# as it reaches isolation_where(): the three axes have three different read
# contracts, and project_id=None (no filter) addresses a different row set than
# project_id='' (the global pool only), so the two must not collapse onto one entry.
_cache: dict[tuple[str | None, str | None, str | None], _Entry] = {}


def clear() -> None:
    """Drop every entry. For tests, and for anything that re-points the database."""
    _cache.clear()


def _fresh(generation: int, stamped_at: float | None, now: float) -> bool:
    """True if a half stamped at ``stamped_at`` under ``generation`` may still be served."""
    if stamped_at is None:
        return False
    if generation != database.write_generation():
        return False
    return (now - stamped_at) < SCOPE_STATS_TTL_SECONDS


async def get_span(
    db,
    agent_id: str,
    project_id: str | None = None,
    channel: str = "",
) -> tuple[str | None, str | None]:
    """``(MIN(timestamp), MAX(timestamp))`` over the scope, cached (see module docstring).

    Returns the strings SQLite gave back, NOT a parsed span — the caller that needs a
    span parses them itself, so this cache cannot become a second place where scoring
    arithmetic lives. ``(None, None)`` when the scope holds no parseable timestamp.
    """
    key = (agent_id, project_id, channel)
    now = _clock()
    if SCOPE_STATS_CACHE_ENABLED:
        entry = _cache.get(key)
        if entry is not None and entry.span is not None:
            if _fresh(entry.span_generation, entry.span_at, now):
                return entry.span

    generation = database.write_generation()
    iso = isolation_where(agent_id=agent_id, project_id=project_id, channel=channel)
    # bug-107: the temporal span is computed over the SAME isolation scope as
    # the recall — an agent-wide MIN/MAX let timestamps from unrelated
    # projects/channels scale a tightly-scoped recall's confidence curve.
    # Callers that score corpus-wide (gate calibration) keep the defaults.
    #
    # bug-237: exclude non-timestamps in SQL rather than trusting MIN/MAX. `timestamp`
    # is TEXT NOT NULL but freely allows '' (import_memories defaults a missing field
    # to it, and an explicit "timestamp": "" is stored verbatim), and under BINARY
    # collation '' sorts first — so ONE such row collapsed MIN() to '', the falsy
    # guard at the call site skipped the whole block, and the scope lost both the range
    # scaling and the bug-207 age anchor. The undated rows then fell back to "half the
    # minimum range" (12 h) and outranked every dated row on the time axis: the
    # presence of exactly the row bug-207 exists to place correctly is what disabled
    # the anchor that would have placed it.
    row = await db.execute_fetchall(
        f"SELECT MIN(timestamp), MAX(timestamp) FROM memories "
        f"WHERE timestamp != '' AND datetime(timestamp) IS NOT NULL{iso.and_clause}",
        iso.params,
    )
    span = (row[0][0], row[0][1]) if row else (None, None)
    _store(key, "span", span, generation, now)
    return span


async def get_pool_counts(
    db,
    agent_id: str,
    project_id: str | None = None,
    channel: str = "",
) -> tuple[int, int]:
    """``(memories, episodes)`` row counts over the scope, cached (see module docstring).

    Returned as two numbers rather than their sum so the tables stay separately
    visible to anything else that reads the same scope; the caller that gates on the
    pool adds them.
    """
    key = (agent_id, project_id, channel)
    now = _clock()
    if SCOPE_STATS_CACHE_ENABLED:
        entry = _cache.get(key)
        if entry is not None and entry.counts is not None:
            if _fresh(entry.counts_generation, entry.counts_at, now):
                return entry.counts

    generation = database.write_generation()
    iso = isolation_where(agent_id=agent_id, project_id=project_id, channel=channel)
    # bug-216: both counts are scoped like the recall itself, for the same reason
    # bug-107 scoped the temporal span — a tightly-scoped recall must not be gated by
    # another project's volume.
    memory_count = (
        await db.execute_fetchall(f"SELECT COUNT(*) FROM memories{iso.where}", iso.params)
    )[0][0]
    episode_count = (
        await db.execute_fetchall(f"SELECT COUNT(*) FROM episodes{iso.where}", iso.params)
    )[0][0]
    counts = (memory_count, episode_count)
    _store(key, "counts", counts, generation, now)
    return counts


def _store(key, half: str, value, generation: int, now: float) -> None:
    """Record a computed half, unless a write landed while it was being computed.

    The queries above are awaits, so another coroutine's commit can land between the
    generation read and here; storing under the pre-read generation would stamp a
    torn read as current. Skipping the store costs one recomputation.
    """
    if not SCOPE_STATS_CACHE_ENABLED or generation != database.write_generation():
        return
    entry = _cache.setdefault(key, _Entry())
    setattr(entry, half, value)
    setattr(entry, f"{half}_generation", generation)
    setattr(entry, f"{half}_at", now)
