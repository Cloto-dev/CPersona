"""Segment profile of the index-served vector arm.

`perf_contiguous_index.py` answers "how long is the arm"; this answers "where
inside the arm the time goes". It builds the same corpus, wraps each stage of
the index path with a timer, runs N queries and reports per-stage medians.

The stage list is the set of functions the index path is made of. A name that
no longer exists is skipped rather than failing, so the script keeps running
across refactors — but a stage that silently disappears from the report is
worth noticing, because it may have been folded into another one.

Usage (from the repo root):
  uv run python benchmarks/measurements/profile_index_path.py --rows 100000 --queries 12
  CPERSONA_MAX_MEMORIES=100000 uv run python benchmarks/measurements/profile_index_path.py --rows 100000

The scan window is `CPERSONA_MAX_MEMORIES`, read by the server at import, so
it is set in the environment rather than by a flag. The scratch corpus is
removed at exit unless `PERF_INDEX_DIR` names where to keep it.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import perf_contiguous_index as perf  # noqa: E402  (sets the environment at import)

STAGES = (
    ("cpersona.vector", "_search_vector"),
    ("cpersona.vector", "_index_phase1"),
    ("cpersona.vector", "_index_rows_lost_embedding"),
    ("cpersona.vector", "_index_tail_rows"),
    ("cpersona.vector", "_merge_index_and_tail"),
    ("cpersona.vector", "_interleave_index_and_tail"),
    ("cpersona.vector", "_is_ascending_run"),
    ("cpersona.vector", "_cosine_matrix"),
    ("cpersona.vector_index", "select"),
    ("cpersona.vector_index", "load_index"),
)

timings: dict[str, list[float]] = {}


def _timed(mod, name: str) -> bool:
    orig = getattr(mod, name, None)
    if orig is None:
        return False
    if asyncio.iscoroutinefunction(orig):

        @functools.wraps(orig)
        async def wrapper(*args, **kwargs):
            t0 = time.perf_counter()
            try:
                return await orig(*args, **kwargs)
            finally:
                timings.setdefault(name, []).append((time.perf_counter() - t0) * 1000)

    else:

        @functools.wraps(orig)
        def wrapper(*args, **kwargs):
            t0 = time.perf_counter()
            try:
                return orig(*args, **kwargs)
            finally:
                timings.setdefault(name, []).append((time.perf_counter() - t0) * 1000)

    setattr(mod, name, wrapper)
    return True


async def run(rows: int, dim: int, queries: int) -> None:
    import importlib

    from cpersona import vector, vector_index
    from cpersona.database import close_db, connection, init_db
    import cpersona.server as server_mod

    await init_db()
    client = perf.LocalEmbeddingClient(dim)
    vector._embedding_client = client
    server_mod._embedding_client = client
    agent = "perf.index"
    index_file = vector_index.index_path("memories")
    for path in (index_file, index_file + ".tmp"):
        if os.path.exists(path):
            os.unlink(path)

    missing = [
        f"{module}.{name}"
        for module, name in STAGES
        if not _timed(importlib.import_module(module), name)
    ]

    texts = [f"topic {i} question" for i in range(queries)]
    try:
        async with connection() as db:
            await perf.build_corpus(db, rows, dim, agent)
            build = await vector_index.build_index(db, "memories")
            assert build.get("built"), build
            for q in texts[:2]:  # warm the page cache and the client
                await vector._search_vector(db, agent, q, 10)
            timings.clear()
            for q in texts:
                await vector._search_vector(db, agent, q, 10)
    finally:
        await close_db()

    print(f"rows={rows} dim={dim} window={vector.MAX_MEMORIES} queries={queries} numpy={perf.np.__version__}")
    total = statistics.median(timings.get("_search_vector", [0.0]))
    for name, xs in sorted(timings.items(), key=lambda kv: -statistics.median(kv[1])):
        med = statistics.median(xs)
        share = 100 * med / total if total else 0.0
        print(f"{name:28s} {med:9.2f} ms  {share:5.1f}%  (n={len(xs)})")
    if missing:
        print("not present in this tree (skipped): " + ", ".join(missing))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=10000)
    ap.add_argument("--dim", type=int, default=1024, help="production embedding width")
    ap.add_argument("--queries", type=int, default=12)
    args = ap.parse_args()
    asyncio.run(run(args.rows, args.dim, args.queries))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
