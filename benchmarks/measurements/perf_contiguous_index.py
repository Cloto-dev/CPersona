"""Contiguous index: before/after on the production recall path.

The question this answers is NOT "is it faster" — the arithmetic said that
already. It is **where the bottleneck moves once the vector arm stops being the
bottleneck**, because the next tier of this work is supposed to be decided by
that measurement rather than by a guess.

So the script times two things per query and reports both:

- `_search_vector`, the arm the index replaces the inside of;
- `do_recall`, the whole path — vector plus the lexical arms, the fusion, the
  scoring and the hydrate.

The difference between them is the part this change cannot touch, and it is the
part that decides what to look at next.

Two regimes, because they answer different questions: the default scan window
(`MAX_MEMORIES`, what a deployment actually runs) and a larger corpus (where the
row-materialisation cost the index removes is visible in the first place).

Deliberately on the real path — `aiosqlite`, `do_recall`, FTS on — rather than
the sync `sqlite3` harness that produced the original breakdown. That harness
was honest about its own limit ("the shape is settled, the absolute numbers are
not production's"), and this is the measurement that settles the numbers.

Usage:
  uv run python benchmarks/measurements/perf_contiguous_index.py --rows 10000
  uv run python benchmarks/measurements/perf_contiguous_index.py --rows 100000 --queries 20
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import json
import os
import shutil
import statistics
import struct
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

SCRATCH = os.environ.get("PERF_INDEX_DIR")
if SCRATCH:
    os.makedirs(SCRATCH, exist_ok=True)
else:
    # A 100,000-row corpus at production width is ~870 MB. Nothing about a
    # measurement is worth keeping in it, so an unnamed scratch directory goes
    # away with the process; name one with PERF_INDEX_DIR to keep it.
    SCRATCH = tempfile.mkdtemp(prefix="perf-index-")
    atexit.register(shutil.rmtree, SCRATCH, ignore_errors=True)
os.environ["CPERSONA_DB_PATH"] = os.path.join(SCRATCH, "perf_index.db")
os.environ["CPERSONA_EMBEDDING_MODE"] = "http"  # a client must exist; ours is local
os.environ["CPERSONA_OPERATING_CONTEXT"] = "off"
os.environ.setdefault("CPERSONA_FTS_ENABLED", "true")

import numpy as np  # noqa: E402


class LocalEmbeddingClient:
    """Deterministic, offline, and the same shape the server expects.

    The query vector has to come from somewhere, and an HTTP round trip would
    add a constant to both arms that has nothing to do with what is being
    measured.
    """

    mode = "http"
    _http_url = None  # empty → the remote index/search branches are skipped
    _client = None

    def __init__(self, dim: int, seed: int = 7):
        self.dim = dim
        self.rng = np.random.default_rng(seed)

    async def initialize(self):
        pass

    async def embed(self, texts):
        out = []
        for text in texts:
            vec = np.random.default_rng(abs(hash(text)) % (2**32)).standard_normal(self.dim)
            vec /= np.linalg.norm(vec)
            out.append(vec.astype(np.float32).tolist())
        return out

    async def embed_with_outcome(self, texts):
        """The entry point the recall path reads since the failure side learned to
        ask whether a call was attempted. A double that offers only ``embed()``
        sends the arm under measurement into an ``AttributeError`` instead of the
        matmul. Derived from ``embed()`` so the two cannot disagree."""
        from cpersona._vendored_mcp_common.embedding_client import EmbedOutcome

        result = await self.embed(texts)
        return result, EmbedOutcome(attempted=True, ok=bool(result), error=None)

    @staticmethod
    def pack_embedding(embedding):
        return struct.pack(f"<{len(embedding)}f", *embedding)


async def build_corpus(db, rows: int, dim: int, agent: str) -> None:
    rng = np.random.default_rng(20260901)
    batch, batch_size = [], 1000
    for n in range(rows):
        vec = rng.standard_normal(dim).astype(np.float32)
        vec /= np.linalg.norm(vec)
        batch.append(
            (
                agent, "", "", f"memory row {n} about topic {n % 997}",
                '{"type": "Agent", "id": "perf"}', "2026-03-01T00:00:00+00:00",
                f"2026-03-01 {n // 3600 % 24:02d}:{n // 60 % 60:02d}:{n % 60:02d}",
                vec.tobytes(),
            )
        )
        if len(batch) == batch_size:
            await db.executemany(
                "INSERT OR IGNORE INTO memories (agent_id, project_id, channel, content,"
                " source, timestamp, created_at, embedding) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                batch,
            )
            await db.commit()
            batch = []
    if batch:
        await db.executemany(
            "INSERT OR IGNORE INTO memories (agent_id, project_id, channel, content,"
            " source, timestamp, created_at, embedding) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            batch,
        )
        await db.commit()


async def time_queries(db, server_mod, vector_mod, agent: str, queries, limit: int):
    """Median ms for the vector arm and for the whole recall, over the same queries."""
    arm, whole = [], []
    for q in queries:
        t0 = time.perf_counter()
        await vector_mod._search_vector(db, agent, q, limit)
        arm.append((time.perf_counter() - t0) * 1000)

        t0 = time.perf_counter()
        await server_mod.do_recall(agent_id=agent, query=q, limit=limit)
        whole.append((time.perf_counter() - t0) * 1000)
    return statistics.median(arm), statistics.median(whole)


async def run(args) -> dict:
    from cpersona import vector, vector_index
    from cpersona.database import close_db, connection, init_db
    import cpersona.server as server_mod

    await init_db()
    client = LocalEmbeddingClient(args.dim)
    vector._embedding_client = client
    server_mod._embedding_client = client

    agent = "perf.index"
    index_file = vector_index.index_path("memories")
    for path in (index_file, index_file + ".tmp"):
        if os.path.exists(path):
            os.unlink(path)

    queries = [f"topic {i} question" for i in range(args.queries)]
    result: dict = {
        "rows": args.rows,
        "dim": args.dim,
        "queries": args.queries,
        "limit": args.limit,
        "numpy": np.__version__,
        "python": sys.version.split()[0],
        "scan_window": None,
    }

    async with connection() as db:
        await build_corpus(db, args.rows, args.dim, agent)
        result["scan_window"] = vector.MAX_MEMORIES

        # Warm-up: the first query pays for page cache and lazy imports on both
        # arms, and reporting that as the "before" number would flatter the after.
        await time_queries(db, server_mod, vector, agent, queries[:2], args.limit)

        before_arm, before_all = await time_queries(
            db, server_mod, vector, agent, queries, args.limit)

        build = await vector_index.build_index(db, "memories")
        if not build.get("built"):
            raise SystemExit(f"index build declined: {build.get('reason')}")
        result["index"] = {k: build[k] for k in ("count", "dim", "watermark", "bytes")}

        # Warm-up again: the first index query maps the file.
        await time_queries(db, server_mod, vector, agent, queries[:2], args.limit)
        after_arm, after_all = await time_queries(
            db, server_mod, vector, agent, queries, args.limit)

    await close_db()

    result["vector_arm_ms"] = {"before": round(before_arm, 2), "after": round(after_arm, 2)}
    result["do_recall_ms"] = {"before": round(before_all, 2), "after": round(after_all, 2)}
    result["speedup_vector_arm"] = round(before_arm / after_arm, 2) if after_arm else None
    result["speedup_do_recall"] = round(before_all / after_all, 2) if after_all else None
    # What the index cannot touch, and therefore what decides the next move.
    result["non_vector_ms"] = {
        "before": round(before_all - before_arm, 2),
        "after": round(after_all - after_arm, 2),
    }
    result["vector_share_of_recall"] = {
        "before": round(before_arm / before_all, 3) if before_all else None,
        "after": round(after_arm / after_all, 3) if after_all else None,
    }
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=10000)
    ap.add_argument("--dim", type=int, default=1024, help="production embedding width")
    ap.add_argument("--queries", type=int, default=25)
    ap.add_argument("--limit", type=int, default=10, help="the response limit a caller asks for")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    result = asyncio.run(run(args))
    print(json.dumps(result, indent=2, sort_keys=True))
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(result, fh, indent=2, sort_keys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
