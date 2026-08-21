"""bug-249 perf harness — re-derives the numbers in results-bug249-two-phase-scan.md.

Builds a synthetic corpus at the scan window's size (MAX_MEMORIES = 10000 rows,
768-d embeddings) and times the pre-split single-query scan (a verbatim copy
kept below) against the two-phase scan, at two content sizes: the old write cap
(2000) and the current one (16000). Also reports the characters of text each
variant pulls across the SQLite boundary, which is the quantity the change is
actually about.

Usage:  uv run python benchmarks/measurements/perf_bug249_two_phase_scan.py [--rows N] [--repeats N]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import statistics
import sys
import time
import tracemalloc

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)
# Synthetic corpora live in a temp dir, never in the repository or any real DB path.
SCRATCH = os.environ.get("PERF_BUG249_DIR", "/tmp/perf-bug249")
os.makedirs(SCRATCH, exist_ok=True)

DIM = 768
LIMITS = tuple(int(x) for x in os.environ.get("PERF_LIMITS","10,100").split(","))
PLANTED = 10  # rows deliberately close to the query, so survivors are realistic


def _db_path(chars: int) -> str:
    return os.path.join(SCRATCH, f"perf_bug249_v2_{chars}.db")


async def build(chars: int, rows: int) -> str:
    path = _db_path(chars)
    if os.path.exists(path):
        return path
    from cpersona.database import get_db

    import numpy as np

    db = await get_db()
    rng = np.random.default_rng(20260822)
    query = rng.standard_normal(DIM).astype(np.float32)
    query /= np.linalg.norm(query)

    alphabet = "abcdefghijklmnopqrstuvwxyz "
    prng = random.Random(7)
    filler = "".join(prng.choice(alphabet) for _ in range(chars))

    batch = []
    for i in range(rows):
        if i < PLANTED:
            # Norm-scaled so the planted rows land ~0.89 cosine, above min_sim.
            noise = rng.standard_normal(DIM).astype(np.float32)
            vec = query + noise * (0.5 / float(np.sqrt(DIM)))
        else:
            vec = rng.standard_normal(DIM).astype(np.float32)
        vec /= np.linalg.norm(vec)
        content = f"row {i} " + filler[: chars - 12]
        batch.append(
            (
                "perf.agent",
                "",
                content,
                '{"type": "Agent"}',
                "2026-03-01 00:00:00",
                vec.astype(np.float32).tobytes(),
                f"2026-03-01 {i // 3600:02d}:{(i // 60) % 60:02d}:{i % 60:02d}",
                "",
                "",
            )
        )
        if len(batch) == 500:
            await db.executemany(
                "INSERT INTO memories (agent_id, msg_id, content, source, timestamp,"
                " embedding, created_at, project_id, channel)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                batch,
            )
            batch = []
    if batch:
        await db.executemany(
            "INSERT INTO memories (agent_id, msg_id, content, source, timestamp,"
            " embedding, created_at, project_id, channel)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            batch,
        )
    await db.commit()
    await db.close()
    import cpersona.database as database

    database._db = None
    database._read_db = None
    return path


async def legacy_scan(db, iso, scan_limit, query_vec, query_dim, min_sim, counter):
    from cpersona import vector

    rows = await db.execute_fetchall(
        f"""SELECT id, msg_id, content, source, timestamp, embedding
           FROM memories
           WHERE {iso.clause} AND embedding IS NOT NULL
           ORDER BY created_at DESC
           LIMIT ?""",
        (*iso.params, scan_limit),
    )
    counter["chars"] += sum(len(v) for r in rows for v in r if isinstance(v, str))
    counter["stmts"] += 1
    if not rows:
        return []
    valid_rows, blobs = [], []
    for row in rows:
        blob = row[5]
        if blob and len(blob) == query_dim * 4:
            valid_rows.append(row)
            blobs.append(blob)
    if not valid_rows:
        return []
    sims = vector._cosine_batch(query_vec, query_dim, blobs)
    out = []
    for i, sim_val in enumerate(sims):
        if sim_val >= min_sim:
            mem_id, msg_id, content, source, timestamp, _ = valid_rows[i]
            sim = float(sim_val)
            out.append((sim, {"id": mem_id, "_rid": ("mem", mem_id), "_cosine": sim,
                              "msg_id": msg_id, "content": content,
                              "source": source, "timestamp": timestamp}))
    return out


class Counting:
    """Wrap the connection so both variants report the text they materialise."""

    def __init__(self, db, counter):
        self._db = db
        self._counter = counter

    async def execute_fetchall(self, sql, params=()):
        rows = await self._db.execute_fetchall(sql, params)
        self._counter["chars"] += sum(len(v) for r in rows for v in r if isinstance(v, str))
        self._counter["stmts"] += 1
        return rows


async def measure(chars: int, rows: int, repeats: int, min_sims) -> None:
    await build(chars, rows)

    import numpy as np

    from cpersona import vector
    from cpersona.database import get_db
    from cpersona.isolation import isolation_where

    db = await get_db()
    iso = isolation_where(agent_id="perf.agent", project_id=None, channel="")
    rng = np.random.default_rng(20260822)
    query = rng.standard_normal(DIM).astype(np.float32)
    query /= np.linalg.norm(query)

    for min_sim in min_sims:
        for limit in LIMITS:
            await _one(db, vector, iso, query, chars, rows, repeats, min_sim, limit)

    await db.close()
    import cpersona.database as database

    database._db = None
    database._read_db = None


async def _one(db, vector, iso, query, chars, rows, repeats, min_sim, limit) -> None:
    old_t, new_t = [], []
    old_c = {"chars": 0, "stmts": 0}
    new_c = {"chars": 0, "stmts": 0}
    n_old = n_new = 0

    for r in range(repeats + 1):  # first pass is a warm-up, discarded
        c = {"chars": 0, "stmts": 0}
        t0 = time.perf_counter()
        got = await legacy_scan(db, iso, 10000, query, DIM, min_sim, c)
        dt = time.perf_counter() - t0
        if r:
            old_t.append(dt)
            old_c = c
            n_old = len(got)

        c = {"chars": 0, "stmts": 0}
        t0 = time.perf_counter()
        got = await vector._scan_memories_local(
            Counting(db, c), iso, "", (), 10000, limit, query, DIM, min_sim
        )
        dt = time.perf_counter() - t0
        if r:
            new_t.append(dt)
            new_c = c
            n_new = len(got)

    assert n_new <= n_old and n_new == min(n_old, limit), (n_old, n_new, limit)

    # Peak Python-heap allocation during one scan. Wall time is bounded below by
    # the 30 MB of embeddings both variants must read; the allocation peak is the
    # part the split actually removes, and it is what concurrent recalls multiply.
    tracemalloc.start()
    base = tracemalloc.get_traced_memory()[0]
    await legacy_scan(db, iso, 10000, query, DIM, min_sim, {"chars": 0, "stmts": 0})
    old_peak = tracemalloc.get_traced_memory()[1] - base
    tracemalloc.stop()

    tracemalloc.start()
    base = tracemalloc.get_traced_memory()[0]
    await vector._scan_memories_local(
        Counting(db, {"chars": 0, "stmts": 0}), iso, "", (), 10000, limit, query, DIM, min_sim
    )
    new_peak = tracemalloc.get_traced_memory()[1] - base
    tracemalloc.stop()

    med_old = statistics.median(old_t)
    med_new = statistics.median(new_t)
    print(f"\n--- content={chars} chars/row, {rows} rows, min_sim={min_sim}, limit={limit} ---")
    print(f"survivors / hydrated: {n_old} above threshold, {n_new} hydrated (of {rows})")
    print(f"single-query scan  : median {med_old * 1000:8.1f} ms   "
          f"(min {min(old_t) * 1000:.1f} / max {max(old_t) * 1000:.1f})")
    print(f"two-phase scan     : median {med_new * 1000:8.1f} ms   "
          f"(min {min(new_t) * 1000:.1f} / max {max(new_t) * 1000:.1f})")
    print(f"speedup            : {med_old / med_new:.2f}x  "
          f"({(med_old - med_new) * 1000:.1f} ms saved per scan)")
    print(f"text materialised  : {old_c['chars']:>12,} -> {new_c['chars']:>12,} chars "
          f"({old_c['chars'] / max(new_c['chars'], 1):.0f}x less)")
    print(f"statements issued  : {old_c['stmts']} -> {new_c['stmts']}")
    print(f"peak python heap   : {old_peak / 1e6:8.1f} MB -> {new_peak / 1e6:.1f} MB "
          f"({old_peak / max(new_peak, 1):.1f}x less)")
    print(f"db file            : {os.path.getsize(_db_path(chars)) / 1e6:.0f} MB")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chars", type=int, required=True)
    ap.add_argument("--rows", type=int, default=10000)
    ap.add_argument("--repeats", type=int, default=7)
    ap.add_argument("--min-sims", type=float, nargs="+", default=[0.5])
    args = ap.parse_args()
    await measure(args.chars, args.rows, args.repeats, args.min_sims)


if __name__ == "__main__":
    # Pinned before any cpersona import: config.DB_PATH is resolved at import time.
    _chars = int(sys.argv[sys.argv.index("--chars") + 1])
    os.environ["CPERSONA_DB_PATH"] = _db_path(_chars)
    os.environ["CPERSONA_EMBEDDING_MODE"] = "none"
    os.environ["CPERSONA_FTS_ENABLED"] = "false"  # not on the path under test
    asyncio.run(main())
