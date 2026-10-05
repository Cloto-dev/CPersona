"""Recall latency with the query embedded by a real model, paired with the stand-in.

`perf_contiguous_index.py` and `profile_recall_path.py` hand the recall path a
stand-in query vector, so their `do_recall` figures leave out the time it takes
to embed the query. This driver builds the same corpus and index, then recalls
every query twice in alternating order: once with the stand-in vector and once
through a real `EmbeddingClient` pointed at an embedding server. The difference
is what embedding the query costs under the same machine state.

Registrations: prereg-recall-latency-with-embedding.md (the synthetic corpus) and
prereg-recall-latency-realistic-corpus.md (`--corpus-db`: a corpus of real text
built through the store path by build_realistic_corpus.py, with its blocks and
nodes, timed with real questions from `--queries-file`).

Every query text is used once per client, so the client's cache never answers a
timed query. The script refuses to run when the model's output width differs
from the corpus width, because the vector arm would then compare nothing.

Usage (the scan window must cover the corpus, as in the earlier records):
  CPERSONA_MAX_MEMORIES=100000 python benchmarks/measurements/perf_recall_with_embedding.py \\
      --rows 100000 --dim 768 --embed-url http://127.0.0.1:8401/embed --json out.json
  CPERSONA_MAX_MEMORIES=100000 python benchmarks/measurements/perf_recall_with_embedding.py \\
      --corpus-db corpus.db --queries-file queries.json --agent perf.real --dim 768 \\
      --settle-load 1.0 --json out.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import perf_contiguous_index as perf  # noqa: E402  (sets the scratch DB and env before cpersona loads)


def nearest_rank(samples: list[float], pct: float) -> float:
    ordered = sorted(samples)
    return ordered[max(0, math.ceil(pct / 100 * len(ordered)) - 1)]


def summary(samples: list[float]) -> dict:
    return {
        "median": round(statistics.median(samples), 2),
        "p95": round(nearest_rank(samples, 95), 2),
        "max": round(max(samples), 2),
        "min": round(min(samples), 2),
        "samples": [round(s, 2) for s in samples],
    }


def top_processes() -> list[str]:
    try:
        out = subprocess.run(
            ["ps", "-eo", "pcpu,comm", "--sort=-pcpu"], capture_output=True, text=True, timeout=10
        ).stdout.splitlines()
        return [line.strip() for line in out[1:6]]
    except Exception as exc:  # the record says it could not look rather than inventing a state
        return [f"unavailable: {exc!r}"]


async def run(args) -> dict:
    from cpersona import config, vector, vector_index
    from cpersona._vendored_mcp_common.embedding_client import EmbeddingClient
    from cpersona.database import close_db, connection, init_db
    import cpersona.server as server_mod

    if args.corpus_db:
        # The corpus is a finished database: copy it over the scratch path before
        # anything opens it, so the run never writes to the file it was given.
        shutil.copyfile(args.corpus_db, os.environ["CPERSONA_DB_PATH"])
    await init_db()
    stub = perf.LocalEmbeddingClient(args.dim)
    real = EmbeddingClient(
        mode="http",
        http_url=args.embed_url,
        cache_size=config.EMBEDDING_CACHE_SIZE,
        cache_ttl=config.EMBEDDING_CACHE_TTL,
        timeout=server_mod._resolve_embedding_timeout(),
    )
    await real.initialize()

    probe = await real.embed(["width probe"])
    if not probe or len(probe[0]) != args.dim:
        width = len(probe[0]) if probe else None
        raise SystemExit(f"model width {width} does not match corpus width {args.dim}")

    def use(client) -> None:
        vector._embedding_client = client
        server_mod._embedding_client = client

    agent = args.agent
    index_file = vector_index.index_path("memories")
    for path in (index_file, index_file + ".tmp"):
        if os.path.exists(path):
            os.unlink(path)

    result: dict = {
        "rows": args.rows,
        "dim": args.dim,
        "queries": args.queries,
        "warmup": args.warmup,
        "limit": args.limit,
        "embed_url": args.embed_url,
        "model_label": args.model_label,
        "numpy": perf.np.__version__,
        "python": sys.version.split()[0],
        "cpu_governor": open("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor").read().strip()
        if os.path.exists("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor") else None,
    }

    texts = None
    if args.queries_file:
        texts = json.load(open(args.queries_file))
        need = args.warmup + 2 * args.queries
        if len(texts) < need or len(set(texts[:need])) < need:
            raise SystemExit(f"--queries-file needs {need} distinct texts, has {len(set(texts))}")
    warm_texts = texts[: args.warmup] if texts else [f"topic {100000 + w} question" for w in range(args.warmup)]
    timed_texts = texts[args.warmup : args.warmup + args.queries] if texts else [f"topic {i} question" for i in range(args.queries)]
    embed_texts = (
        texts[args.warmup + args.queries : args.warmup + 2 * args.queries]
        if texts
        else [f"embed probe {i} about a topic question" for i in range(args.queries)]
    )
    result["corpus_db"] = os.path.basename(args.corpus_db) if args.corpus_db else None
    result["queries_file"] = os.path.basename(args.queries_file) if args.queries_file else None
    result["embed_server_note"] = args.embed_server_note

    async with connection() as db:
        if args.corpus_db:
            counts = {}
            for table in ("memories", "record_blocks", "record_nodes"):
                counts[table] = (await db.execute_fetchall(f"SELECT COUNT(*) FROM {table}"))[0][0]
            result["corpus_counts"] = counts
            result["rows"] = counts["memories"]
        else:
            await perf.build_corpus(db, args.rows, args.dim, agent)
        result["scan_window"] = vector.MAX_MEMORIES
        build = await vector_index.build_index(db, "memories")
        if not build.get("built"):
            raise SystemExit(f"index build declined: {build.get('reason')}")
        result["index"] = {k: build[k] for k in ("count", "dim", "watermark", "bytes")}

        # Warm-up texts are never reused below.
        for text in warm_texts:
            for client in (stub, real):
                use(client)
                await server_mod.do_recall(agent_id=agent, query=text, limit=args.limit)

        if args.settle_load is not None:
            # Wait for the machine to come to rest before the timed loop, rather
            # than reading a load average the driver's own preparation produced.
            waited, deadline = 0.0, time.monotonic() + args.settle_timeout
            while os.getloadavg()[0] >= args.settle_load and time.monotonic() < deadline:
                time.sleep(15)
                waited += 15
            result["settle"] = {
                "threshold": args.settle_load,
                "waited_s": waited,
                "settled": os.getloadavg()[0] < args.settle_load,
            }
        result["load_before"] = os.getloadavg()
        result["top_before"] = top_processes()

        stub_ms, real_ms, rows_stub, rows_real = [], [], [], []
        for i, text in enumerate(timed_texts):
            order = (stub, real) if i % 2 == 0 else (real, stub)
            for client in order:
                use(client)
                t0 = time.perf_counter()
                res = await server_mod.do_recall(agent_id=agent, query=text, limit=args.limit)
                elapsed = (time.perf_counter() - t0) * 1000
                returned = len(res.get("messages", [])) if isinstance(res, dict) else None
                if client is stub:
                    stub_ms.append(elapsed)
                    rows_stub.append(returned)
                else:
                    real_ms.append(elapsed)
                    rows_real.append(returned)

        embed_ms = []
        for i, text in enumerate(embed_texts):
            t0 = time.perf_counter()
            vec = await real.embed([text])
            embed_ms.append((time.perf_counter() - t0) * 1000)
            if not vec or len(vec[0]) != args.dim:
                raise SystemExit(f"embed probe {i} returned no vector of width {args.dim}")

        result["load_after"] = os.getloadavg()

    await close_db()

    result["do_recall_stub_ms"] = summary(stub_ms)
    result["do_recall_real_ms"] = summary(real_ms)
    result["paired_difference_ms"] = summary([r - s for r, s in zip(real_ms, stub_ms)])
    result["embed_only_ms"] = summary(embed_ms)
    result["rows_returned"] = {"stub": rows_stub, "real": rows_real}
    # Every timed text was new to the real client, so each should have left one entry.
    result["real_client_cache_entries"] = len(getattr(real, "_cache", {}))
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=100000)
    ap.add_argument("--dim", type=int, required=True, help="the model's output width")
    ap.add_argument("--queries", type=int, default=25)
    ap.add_argument("--warmup", type=int, default=3)
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--embed-url", default="http://127.0.0.1:8401/embed")
    ap.add_argument("--model-label", default="", help="recorded as given, e.g. onnx_jina_v5_nano")
    ap.add_argument("--json", default=None)
    ap.add_argument("--agent", default="perf.index", help="the corpus's agent (build_realistic_corpus.py: perf.real)")
    ap.add_argument("--corpus-db", default=None, help="a finished corpus database to time instead of the synthetic one")
    ap.add_argument("--queries-file", default=None, help="JSON list: warm-up, then timed, then embed-only texts")
    ap.add_argument("--settle-load", type=float, default=None, help="wait for the 1-minute load to fall below this")
    ap.add_argument("--settle-timeout", type=float, default=900.0)
    ap.add_argument("--embed-server-note", default="", help="recorded as given, e.g. ONNX_INTRA_OP_THREADS=1")
    args = ap.parse_args()

    result = asyncio.run(run(args))
    print(json.dumps(result, indent=2, sort_keys=True))
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(result, fh, indent=2, sort_keys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
