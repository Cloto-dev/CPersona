"""reconstruct's latency under mode lite and mode pro, with the query embedded by a real model.

The corpus, queries, embedding server, settling and machine are those of
prereg-recall-latency-block-index-file.md; what is timed is the `reconstruct`
tool, called through the server's own handler with `mode: "lite"` (`count` 10)
and `mode: "pro"` (`count` 15), instead of `do_recall`. The block index file is
built before the warm-up, as the default does.

Each timed text is asked once in each mode, in alternating order (lite first on
even texts, pro first on odd ones), and the client's embedding cache is cleared
before every call, so no timed call is answered from it. Each response's
`used_tokens`, its items' head refs and the number of held items are recorded,
so two runs of different code can be compared item for item.

Usage (copy this file into each tree measured; the tree's cpersona is the one imported):
  CPERSONA_MAX_MEMORIES=100000 python benchmarks/measurements/perf_reconstruct_modes.py \\
      --corpus-db corpus.db --queries-file queries.json --agent perf.real --dim 768 \\
      --settle-load 1.0 --json out.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import perf_contiguous_index as perf  # noqa: E402  (sets the scratch DB and env before cpersona loads)
from perf_recall_with_embedding import summary, top_processes  # noqa: E402

MODES = {"lite": 10, "pro": 15}


async def run(args) -> dict:
    from cpersona import __version__, block_index, config, vector, vector_index
    from cpersona._vendored_mcp_common.embedding_client import EmbeddingClient
    from cpersona.database import close_db, connection, init_db
    import cpersona.server as server_mod

    shutil.copyfile(args.corpus_db, os.environ["CPERSONA_DB_PATH"])
    await init_db()
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
        raise SystemExit(f"model width {len(probe[0]) if probe else None} does not match corpus width {args.dim}")
    vector._embedding_client = real
    server_mod._embedding_client = real
    handler = server_mod.registry._handlers["reconstruct"]

    def clear_cache() -> None:
        cache = getattr(real, "_cache", None)
        if cache is not None:
            cache.clear()

    texts = json.load(open(args.queries_file))
    need = args.warmup + args.queries
    if len(texts) < need or len(set(texts[:need])) < need:
        raise SystemExit(f"--queries-file needs {need} distinct texts")
    warm_texts, timed_texts = texts[: args.warmup], texts[args.warmup : need]

    result: dict = {
        "cpersona": __version__,
        "queries": args.queries,
        "warmup": args.warmup,
        "modes": MODES,
        "embed_url": args.embed_url,
        "model_label": args.model_label,
        "embed_server_note": args.embed_server_note,
        "python": sys.version.split()[0],
        "numpy": perf.np.__version__,
        "corpus_db": os.path.basename(args.corpus_db),
        "queries_file": os.path.basename(args.queries_file),
        "cpu_governor": open("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor").read().strip()
        if os.path.exists("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor") else None,
    }

    index_file = vector_index.index_path("memories")
    for path in (index_file, index_file + ".tmp"):
        if os.path.exists(path):
            os.unlink(path)
    async with connection() as db:
        counts = {}
        for table in ("memories", "record_blocks", "record_nodes"):
            counts[table] = (await db.execute_fetchall(f"SELECT COUNT(*) FROM {table}"))[0][0]
        result["corpus_counts"] = counts
        result["scan_window"] = vector.MAX_MEMORIES
        build = await vector_index.build_index(db, "memories")
        if not build.get("built"):
            raise SystemExit(f"index build declined: {build.get('reason')}")
        result["index"] = {k: build[k] for k in ("count", "dim", "watermark", "bytes")}
    result["block_index"] = {"enabled": block_index.enabled()}
    if block_index.enabled():
        t0 = time.perf_counter()
        built = await block_index.build_block_index()
        result["block_index"]["build_seconds"] = round(time.perf_counter() - t0, 3)
        if not built.get("built"):
            raise SystemExit(f"block index build declined: {built.get('reason')}")

    async def ask(text: str, mode: str) -> tuple[float, dict]:
        clear_cache()
        t0 = time.perf_counter()
        res = await handler({"agent_id": args.agent, "query": text, "mode": mode, "count": MODES[mode]})
        return (time.perf_counter() - t0) * 1000, res

    for text in warm_texts:
        for mode in MODES:
            await ask(text, mode)

    if args.settle_load is not None:
        waited, deadline = 0.0, time.monotonic() + args.settle_timeout
        while os.getloadavg()[0] >= args.settle_load and time.monotonic() < deadline:
            time.sleep(15)
            waited += 15
        result["settle"] = {"threshold": args.settle_load, "waited_s": waited,
                            "settled": os.getloadavg()[0] < args.settle_load}
    result["load_before"] = os.getloadavg()
    result["top_before"] = top_processes()

    per = {m: {"ms": [], "used_tokens": [], "heads": [], "held": [], "errors": 0} for m in MODES}
    for i, text in enumerate(timed_texts):
        order = ("lite", "pro") if i % 2 == 0 else ("pro", "lite")
        for mode in order:
            ms, res = await ask(text, mode)
            p = per[mode]
            p["ms"].append(round(ms, 3))
            if not isinstance(res, dict) or "items" not in res:
                p["errors"] += 1
                for key in ("used_tokens", "heads", "held"):
                    p[key].append(None)
                continue
            items = res.get("items", [])
            p["used_tokens"].append(res.get("used_tokens"))
            p["heads"].append([it.get("head_ref") for it in items])
            p["held"].append(sum(1 for it in items if it.get("admission") == "reservation"))
    result["load_after"] = os.getloadavg()
    await close_db()

    for mode, p in per.items():
        result[mode] = {"reconstruct_ms": summary(p["ms"]), "samples_ms": p["ms"], "errors": p["errors"],
                        "used_tokens": p["used_tokens"], "heads": p["heads"], "held": p["held"]}
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dim", type=int, required=True, help="the model's output width")
    ap.add_argument("--queries", type=int, default=25)
    ap.add_argument("--warmup", type=int, default=3)
    ap.add_argument("--embed-url", default="http://127.0.0.1:8401/embed")
    ap.add_argument("--model-label", default="")
    ap.add_argument("--embed-server-note", default="")
    ap.add_argument("--agent", default="perf.real")
    ap.add_argument("--corpus-db", required=True)
    ap.add_argument("--queries-file", required=True)
    ap.add_argument("--settle-load", type=float, default=None)
    ap.add_argument("--settle-timeout", type=float, default=900.0)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()
    result = asyncio.run(run(args))
    print(json.dumps({m: result[m]["reconstruct_ms"] for m in MODES}, indent=2))
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(result, fh, indent=2, sort_keys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
