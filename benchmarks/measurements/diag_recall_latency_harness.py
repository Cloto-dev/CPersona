"""Which harness condition moves the stand-in do_recall figure.

perf_contiguous_index.py reports do_recall at 100,000 rows near 250-300 ms on the
N150; perf_recall_with_embedding.py, on the same tree and machine, reports its
stand-in arm near 415 ms. The two differ in three ways, and this script changes
them one at a time on one corpus, stand-in vector throughout:

  A  distinct texts, do_recall alone                 (the newer driver's shape)
  B  distinct texts, _search_vector first, then do_recall on the same text
                                                     (the older harness's shape)
  C  texts already recalled in A, do_recall alone    (the older harness reuses
                                                      its warm-up texts)
  S  a full scan (_search_vector before the index exists) precedes everything,
     as in the older harness — run with --scan-first

Diagnostic, not registered: it explains a difference, it decides nothing.

  CPERSONA_MAX_MEMORIES=100000 python benchmarks/measurements/diag_recall_latency_harness.py \\
      --rows 100000 --dim 1024 --json out.json [--scan-first]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import perf_contiguous_index as perf  # noqa: E402  (sets the scratch DB and env before cpersona loads)


async def timed_recall(server_mod, agent, text, limit):
    t0 = time.perf_counter()
    await server_mod.do_recall(agent_id=agent, query=text, limit=limit)
    return (time.perf_counter() - t0) * 1000


async def run(args) -> dict:
    from cpersona import vector, vector_index
    from cpersona.database import close_db, connection, init_db
    import cpersona.server as server_mod

    await init_db()
    stub = perf.LocalEmbeddingClient(args.dim)
    vector._embedding_client = stub
    server_mod._embedding_client = stub
    agent = "perf.index"
    index_file = vector_index.index_path("memories")
    for path in (index_file, index_file + ".tmp"):
        if os.path.exists(path):
            os.unlink(path)

    n = args.queries
    out: dict = {"rows": args.rows, "dim": args.dim, "queries": n, "scan_first": args.scan_first}
    async with connection() as db:
        await perf.build_corpus(db, args.rows, args.dim, agent)
        if args.scan_first:
            for i in range(n):
                await vector._search_vector(db, agent, f"topic {50000 + i} question", args.limit)
        build = await vector_index.build_index(db, "memories")
        if not build.get("built"):
            raise SystemExit(f"index build declined: {build.get('reason')}")
        for w in range(3):
            await timed_recall(server_mod, agent, f"topic {100000 + w} question", args.limit)

        a = [await timed_recall(server_mod, agent, f"topic {i} question", args.limit) for i in range(n)]
        b = []
        for i in range(n):
            text = f"topic {1000 + i} question"
            await vector._search_vector(db, agent, text, args.limit)
            b.append(await timed_recall(server_mod, agent, text, args.limit))
        c = [await timed_recall(server_mod, agent, f"topic {i} question", args.limit) for i in range(n)]
        a2 = [await timed_recall(server_mod, agent, f"topic {2000 + i} question", args.limit) for i in range(n)]
    await close_db()

    for key, samples in (("A_distinct_alone", a), ("B_search_vector_first", b),
                         ("C_repeated_texts", c), ("A2_distinct_alone_again", a2)):
        out[key] = {"median": round(statistics.median(samples), 2),
                    "min": round(min(samples), 2), "max": round(max(samples), 2)}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=100000)
    ap.add_argument("--dim", type=int, default=1024)
    ap.add_argument("--queries", type=int, default=15)
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--scan-first", action="store_true")
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
