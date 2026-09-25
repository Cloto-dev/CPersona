"""Instrument pieces for the Track B runner that must not trust the package under test.

Two things live here, apart from ``benchmark_trackb_lmeb.py`` so they can be
tested without loading an embedding model:

- ``DepthCheck`` recomputes, from the harness's own reading of the environment,
  the Recall Depth every recall call should have used, and compares it with the
  ``depth`` the response reports. The package reads ``CPERSONA_RECALL_DEPTH_FLOOR``
  once, at import; a run that set the variable too late would silently rank at
  the default and look like a null result. The check does not call the
  package's own depth function -- that would compare the package with itself.
- ``split_queries`` assigns each query of a subtask to ``dev`` or ``test`` by a
  seeded shuffle, so a value can be chosen on one half and confirmed on the
  other, and the assignment can be written out and never redrawn.

See benchmarks/measurements/prereg-recall-depth-floor-sweep.md.
"""

from __future__ import annotations

import os
import random

FUSING_MODES = frozenset({"rrf", "rsf"})
SPLIT_PARTS = ("dev", "test")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    try:
        return int(raw) if raw.strip() else default
    except ValueError:
        return default


class DepthCheck:
    """Compare each recall's reported depth with the depth this run intended.

    The package first clamps the requested limit to ``[0, ceiling]``, then reports
    ``depth`` only when it differs from that clamped limit, so an absent key means
    "depth == clamped limit". The expected depth follows the rule the 2.6 line
    documents: ``max(limit, floor)`` clamped to the ceiling, and only in a fusing
    mode with a non-empty query; everywhere else the depth is the clamped limit.
    """

    def __init__(self, floor: int, ceiling: int, mode: str, max_examples: int = 5):
        self.floor = max(0, int(floor))
        self.ceiling = int(ceiling)
        self.mode = mode
        self.max_examples = max_examples
        self.checked = 0
        self.mismatches = 0
        self.examples: list[dict] = []

    @classmethod
    def from_env(cls) -> "DepthCheck":
        # Read the same variables the package reads, independently of it. The
        # defaults are the package's own (cpersona/config.py).
        floor = _env_int("CPERSONA_RECALL_DEPTH_FLOOR", 0)
        ceiling = _env_int("CPERSONA_RECALL_LIBRARY_MAX_LIMIT", 10000)
        mode = os.environ.get("CPERSONA_RECALL_MODE", "rrf")
        return cls(floor=floor, ceiling=ceiling, mode=mode)

    def _clamped(self, limit: int) -> int:
        # The package clamps the requested limit to the library ceiling first,
        # and decides whether to report `depth` against the clamped value.
        return min(max(0, limit), self.ceiling)

    def expected(self, limit: int, query: str) -> int:
        lim = self._clamped(limit)
        if self.mode in FUSING_MODES and query.strip():
            return min(max(lim, self.floor), self.ceiling)
        return lim

    def observe(self, limit: int, query: str, response: dict, query_id: str = "") -> None:
        self.checked += 1
        want = self.expected(limit, query)
        got = response.get("depth", self._clamped(limit))
        if got != want:
            self.mismatches += 1
            if len(self.examples) < self.max_examples:
                self.examples.append(
                    {"query_id": query_id, "limit": limit, "expected": want, "reported": got}
                )

    def summary(self) -> dict:
        return {
            "floor": self.floor,
            "ceiling": self.ceiling,
            "mode": self.mode,
            "checked": self.checked,
            "mismatches": self.mismatches,
            "examples": list(self.examples),
        }


def record_depth_check(result: dict, check: DepthCheck) -> None:
    """Write the check into a task result, and mark the result invalid on any mismatch.

    Uses the ``invalid`` / ``invalid_reason`` fields the measurement records
    already carry (benchmarks/measurements/README.md), so a run whose depth
    nobody chose is filtered out by the same test as every other invalid run.
    """
    summary = check.summary()
    result["depth_check"] = summary
    if summary["mismatches"]:
        result["invalid"] = True
        result["invalid_reason"] = (
            f"{summary['mismatches']} of {summary['checked']} recalls reported a depth "
            f"other than max(limit, floor={summary['floor']}) clamped to "
            f"{summary['ceiling']}; see depth_check.examples"
        )


def split_queries(query_ids: list[str], seed: int, key: str) -> dict[str, str]:
    """Assign each query id to ``dev`` or ``test``, 50/50, reproducibly.

    The order is sorted before the shuffle so the assignment does not depend on
    the file's row order, and the shuffle is seeded by ``seed`` and ``key`` (the
    subtask name) so each subtask is split on its own. With an odd count, dev
    gets the smaller half.
    """
    ids = sorted({str(q) for q in query_ids})
    rng = random.Random(f"{seed}:{key}")
    rng.shuffle(ids)
    half = len(ids) // 2
    return {qid: ("dev" if i < half else "test") for i, qid in enumerate(ids)}
