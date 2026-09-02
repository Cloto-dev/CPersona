"""Segment profile of the whole recall path — where the non-vector time goes.

`perf_contiguous_index.py` reports `do_recall` minus the vector arm as one
number ("the part the index cannot touch"). `profile_index_path.py` breaks the
vector arm down. This breaks the rest down: the two lexical arms (FTS5 over
memories and over episodes), the fusion, the scoring, the gate, and the SQL
statements `do_recall` issues directly (the pool-size counts, the temporal
span, the recall-count bump), which no function boundary wraps.

Two attributions are reported for every run, because they answer different
questions and neither is complete on its own:

- **stages**: each function on the path, wrapped with a timer. Nested stages
  are listed under their parent, so the numbers are not meant to sum.
- **sql**: every statement that went through `aiosqlite`, keyed by a normalised
  prefix. This is the only place the bare statements in `do_recall` show up,
  and it is what says whether "FTS5" means the MATCH itself or the JOIN and
  ORDER BY around it.

Three query sets, because FTS5 cost depends on how many rows match, and the
synthetic corpus can be made to match everything or nothing:

- `broad`: every row shares a term with the query (the worst case for bm25
  ranking — the postings list is the corpus).
- `narrow`: a digit run that appears in a handful of rows.
- `none`: a term that appears nowhere (the MATCH returns empty; the arm's
  fixed cost is what remains).

Two configurations, chosen by `--config`: `default` is what an install runs
out of the box (rrf, confidence off); `production` is rsf with confidence on,
which adds the cosine backfill, the temporal span and the recall-count
bookkeeping — a different set of statements, not just a different order.

Usage (from the repo root; the scan window is read from the environment):
  CPERSONA_MAX_MEMORIES=100000 uv run python benchmarks/measurements/profile_recall_path.py \\
      --rows 100000 --config production --json out.json
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import os
import re
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import perf_contiguous_index as perf  # noqa: E402  (sets the environment at import)

# (module, name, parent) — parent is display-only.
STAGES = (
    ("cpersona.memory_handlers", "do_recall", None),
    ("cpersona.memory_handlers", "_recall_rrf", "do_recall"),
    ("cpersona.memory_handlers", "_recall_rsf", "do_recall"),
    ("cpersona.memory_handlers", "_recall_cascade", "do_recall"),
    ("cpersona.memory_handlers", "_search_vector", "fusion"),
    ("cpersona.memory_handlers", "_search_episodes_fts", "fusion"),
    ("cpersona.memory_handlers", "_search_memories_keyword", "fusion"),
    ("cpersona.memory_handlers", "_append_profile_rows", "fusion"),
    ("cpersona.memory_handlers", "_minmax_norm", "fusion"),
    ("cpersona.memory_handlers", "_apply_recall_scoring", "do_recall"),
    ("cpersona.memory_handlers", "_backfill_cosines", "_apply_recall_scoring"),
    ("cpersona.memory_handlers", "_get_episode_boundary_ts", "_apply_recall_scoring"),
    ("cpersona.memory_handlers", "_compute_confidence", "_apply_recall_scoring"),
    ("cpersona.memory_handlers", "_apply_quality_gate", "do_recall"),
    ("cpersona.memory_handlers", "_autocut", "do_recall"),
    ("cpersona.memory_handlers", "_build_fts_query", "fusion"),
    ("cpersona.health", "maybe_advisory", "do_recall"),
    ("cpersona.health", "observe_config", "do_recall"),
)

timings: dict[str, list[float]] = {}
sql_timings: dict[str, list[float]] = {}
sql_rows: dict[str, list[int]] = {}


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


_WS = re.compile(r"\s+")


def _sql_key(sql: str) -> str:
    """A stable label for a statement: FTS and the bare do_recall statements get
    names; everything else is its normalised first 72 characters."""
    s = _WS.sub(" ", sql).strip()
    if "memories_fts MATCH" in s:
        return "fts:memories MATCH (join + bm25 + ORDER BY rank)"
    if "episodes_fts MATCH" in s:
        return "fts:episodes MATCH (join + bm25 + ORDER BY rank)"
    if s.startswith("SELECT COUNT(*) FROM memories"):
        return "count:memories (gate pool size)"
    if s.startswith("SELECT COUNT(*) FROM episodes"):
        return "count:episodes (gate pool size)"
    if "MIN(timestamp), MAX(timestamp)" in s:
        return "span:MIN/MAX(timestamp) with datetime() (confidence)"
    if s.startswith("SELECT created_at FROM episodes"):
        return "boundary:latest episode created_at (episode penalty)"
    if s.startswith("SELECT id, recall_count, last_recalled_at"):
        return "recall_counts:SELECT by id (confidence)"
    if s.startswith("UPDATE memories SET recall_count"):
        return "bump:UPDATE recall_count (confidence)"
    if s.startswith("SELECT content FROM profiles"):
        return "profile:SELECT profiles"
    if "content LIKE" in s:
        return "keyword:LIKE fallback"
    return s[:72]


def _wrap_sql() -> None:
    import aiosqlite

    # `execute` is left alone: aiosqlite returns an object that is both awaitable
    # and an async context manager, and a coroutine wrapper breaks the latter.
    # The path's `execute` calls are the recall_count bump (an UPDATE inside a
    # transaction) and the index builder's stream; the bump shows up in the
    # gap between do_recall and the sum of its stages, not in the sql table.
    for meth in ("execute_fetchall", "executemany"):
        orig = getattr(aiosqlite.Connection, meth)

        @functools.wraps(orig)
        async def wrapper(self, sql, *args, _orig=orig, **kwargs):
            t0 = time.perf_counter()
            try:
                out = await _orig(self, sql, *args, **kwargs)
                if isinstance(out, list):
                    sql_rows.setdefault(_sql_key(sql), []).append(len(out))
                return out
            finally:
                sql_timings.setdefault(_sql_key(sql), []).append(
                    (time.perf_counter() - t0) * 1000
                )

        setattr(aiosqlite.Connection, meth, wrapper)


async def build_episodes(db, rows: int, dim: int, agent: str) -> None:
    """Episodes share the memories' vocabulary so the same query hits both FTS
    tables; one per five memories, which is the shape of a real corpus (a
    session summary per few dozen stores is typical, but the ratio is not what
    is measured here — that both arms have a non-empty postings list is)."""
    if rows <= 0:
        return
    rng = perf.np.random.default_rng(20260902)
    batch, batch_size = [], 1000
    for n in range(rows):
        vec = rng.standard_normal(dim).astype(perf.np.float32)
        vec /= perf.np.linalg.norm(vec)
        batch.append(
            (
                agent, "", "",
                f"session {n} covered topic {n % 997} and topic {(n * 7) % 997}",
                f"topic{n % 997}", vec.tobytes(),
                f"2026-03-02T{n // 3600 % 24:02d}:{n // 60 % 60:02d}:{n % 60:02d}+00:00",
                0, f"2026-03-02 {n // 3600 % 24:02d}:{n // 60 % 60:02d}:{n % 60:02d}",
            )
        )
        if len(batch) == batch_size:
            await db.executemany(
                "INSERT INTO episodes (agent_id, project_id, channel, summary, keywords,"
                " embedding, start_time, resolved, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                batch,
            )
            await db.commit()
            batch = []
    if batch:
        await db.executemany(
            "INSERT INTO episodes (agent_id, project_id, channel, summary, keywords,"
            " embedding, start_time, resolved, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            batch,
        )
        await db.commit()


QUERY_SETS = {
    # "topic" is in every memory and every episode; "question" is in none.
    "broad": lambda i: f"topic {i} question",
    # A 4-digit run: matches the rows whose ordinal or topic contains it.
    "narrow": lambda i: f"{1000 + i * 37}",
    # Six letters that appear nowhere in the corpus.
    "none": lambda i: f"zqxjvk{i}",
}


def _median(xs):
    return round(statistics.median(xs), 3) if xs else None


async def run(args) -> dict:
    import importlib

    from cpersona import config, vector, vector_index
    from cpersona import memory_handlers as mh
    from cpersona.database import close_db, connection, init_db
    import cpersona.server as server_mod

    await init_db()
    client = perf.LocalEmbeddingClient(args.dim)
    vector._embedding_client = client
    server_mod._embedding_client = client
    agent = "perf.index"
    for table in ("memories", "episodes"):
        index_file = vector_index.index_path(table)
        for path in (index_file, index_file + ".tmp"):
            if os.path.exists(path):
                os.unlink(path)

    missing = [
        f"{module}.{name}"
        for module, name, _ in STAGES
        if not _timed(importlib.import_module(module), name)
    ]
    _wrap_sql()

    n_episodes = args.episodes if args.episodes is not None else args.rows // 5
    report: dict = {
        "rows": args.rows,
        "episodes": n_episodes,
        "dim": args.dim,
        "queries_per_set": args.queries,
        "limit": args.limit,
        "scan_window": vector.MAX_MEMORIES,
        "config": {
            "name": args.config,
            "recall_mode": config.RECALL_MODE,
            "confidence": config.CONFIDENCE_ENABLED,
            "fts": config.FTS_ENABLED,
            "episode_penalty": config.EPISODE_PENALTY_ENABLED,
            "autocut": config.AUTOCUT_ENABLED,
            "fused_gate": config.FUSED_GATE_ENABLED,
        },
        "index_built": None,
        "numpy": perf.np.__version__,
        "python": sys.version.split()[0],
        "sets": {},
    }
    try:
        async with connection() as db:
            await perf.build_corpus(db, args.rows, args.dim, agent)
            await build_episodes(db, n_episodes, args.dim, agent)
            if args.index:
                build = await vector_index.build_index(db, "memories")
                assert build.get("built"), build
                report["index_built"] = {k: build[k] for k in ("count", "watermark", "bytes")}
                if n_episodes:
                    ep_build = await vector_index.build_index(db, "episodes")
                    assert ep_build.get("built"), ep_build
                    report["episode_index_built"] = {
                        k: ep_build[k] for k in ("count", "watermark", "bytes")
                    }
            # aiosqlite's connection is per `connection()`; do_recall opens its own.
        for set_name in args.sets:
            make = QUERY_SETS[set_name]
            texts = [make(i) for i in range(args.queries)]
            for q in texts[:2]:  # warm
                await mh.do_recall(agent_id=agent, query=q, limit=args.limit)
            timings.clear()
            sql_timings.clear()
            sql_rows.clear()
            hits: list[int] = []
            for q in texts:
                out = await mh.do_recall(agent_id=agent, query=q, limit=args.limit)
                hits.append(len(out.get("messages", [])))
            total = statistics.median(timings.get("do_recall", [0.0]))
            stages = {}
            for _, name, parent in STAGES:
                xs = timings.get(name)
                if not xs:
                    continue
                per_call = _median(xs)
                calls = len(xs) / len(texts)
                stages[name] = {
                    "median_ms": per_call,
                    "calls_per_query": round(calls, 2),
                    "ms_per_query": round(per_call * calls, 3) if per_call is not None else None,
                    "share": round(100 * per_call * calls / total, 1) if total else None,
                    "parent": parent,
                }
            sql = {}
            for key, xs in sorted(sql_timings.items(), key=lambda kv: -sum(kv[1])):
                calls = len(xs) / len(texts)
                med = _median(xs)
                rows = sql_rows.get(key)
                sql[key] = {
                    "median_ms": med,
                    "calls_per_query": round(calls, 2),
                    "ms_per_query": round(med * calls, 3),
                    "share": round(100 * med * calls / total, 1) if total else None,
                    "median_rows": _median(rows) if rows else None,
                }
            report["sets"][set_name] = {
                "do_recall_median_ms": round(total, 3),
                "results_median": _median(hits),
                "stages": stages,
                "sql": sql,
                "sql_total_ms_per_query": round(sum(v["ms_per_query"] for v in sql.values()), 3),
            }
    finally:
        await close_db()
    if missing:
        report["stages_not_present"] = missing
    return report


def _print(report: dict) -> None:
    c = report["config"]
    print(
        f"rows={report['rows']} episodes={report['episodes']} dim={report['dim']} "
        f"window={report['scan_window']} index={'yes' if report['index_built'] else 'no'} "
        f"config={c['name']} (mode={c['recall_mode']} confidence={c['confidence']}) "
        f"numpy={report['numpy']} python={report['python']}"
    )
    for set_name, s in report["sets"].items():
        print(f"\n== query set: {set_name}  do_recall median {s['do_recall_median_ms']:.1f} ms  "
              f"results/query {s['results_median']}")
        print("-- stages (ms/query = median per call x calls per query)")
        for name, st in sorted(s["stages"].items(), key=lambda kv: -(kv[1]["ms_per_query"] or 0)):
            print(f"  {name:28s} {st['ms_per_query']:9.2f} ms  {st['share']:5.1f}%  "
                  f"(x{st['calls_per_query']:.1f}, under {st['parent'] or '-'})")
        print(f"-- sql (total {s['sql_total_ms_per_query']:.1f} ms/query)")
        for key, st in s["sql"].items():
            rows = "" if st["median_rows"] is None else f"  rows={st['median_rows']:.0f}"
            print(f"  {st['ms_per_query']:9.2f} ms  {st['share']:5.1f}%  x{st['calls_per_query']:.1f}"
                  f"{rows}  {key}")
    if report.get("stages_not_present"):
        print("\nnot present in this tree (skipped): " + ", ".join(report["stages_not_present"]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=10000)
    ap.add_argument("--episodes", type=int, default=None, help="default rows/5")
    ap.add_argument("--dim", type=int, default=1024, help="production embedding width")
    ap.add_argument("--queries", type=int, default=12, help="per query set")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--sets", nargs="+", default=["broad", "narrow", "none"], choices=list(QUERY_SETS))
    ap.add_argument("--config", choices=("default", "production"), default="default")
    ap.add_argument("--no-index", dest="index", action="store_false",
                    help="measure the scan path instead of the contiguous index")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    # The config module reads these at import; nothing from `cpersona` has been
    # imported yet (perf only imports numpy at module level).
    if args.config == "production":
        os.environ["CPERSONA_RECALL_MODE"] = "rsf"
        os.environ["CPERSONA_CONFIDENCE_ENABLED"] = "true"
    else:
        os.environ.pop("CPERSONA_RECALL_MODE", None)
        os.environ.pop("CPERSONA_CONFIDENCE_ENABLED", None)

    report = asyncio.run(run(args))
    _print(report)
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(report, fh, indent=2, sort_keys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
