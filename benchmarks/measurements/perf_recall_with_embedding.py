"""Recall latency with the query embedded by a real model, paired with the stand-in.

`perf_contiguous_index.py` and `profile_recall_path.py` hand the recall path a
stand-in query vector, so their `do_recall` figures leave out the time it takes
to embed the query. This driver builds the same corpus and index, then recalls
every query twice in alternating order: once with the stand-in vector and once
through a real `EmbeddingClient` pointed at an embedding server. The difference
is what embedding the query costs under the same machine state.

Registrations: prereg-recall-latency-with-embedding.md (the synthetic corpus),
prereg-recall-latency-realistic-corpus.md (`--corpus-db`: a corpus of real text
built through the store path by build_realistic_corpus.py, with its blocks and
nodes, timed with real questions from `--queries-file`) and
prereg-recall-latency-block-index-file.md (`--block-index` / `--touch-rows`: the
block arm reading its rows from the block index file, and how it fares as rows
change after the file is built).

Every recall's returned refs and the time its block arm took are recorded, so
two runs that differ only in where the block arm reads its rows can be compared
row for row. The stand-in vector is derived from Python's string hash: runs that
are compared that way need the same PYTHONHASHSEED.

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
import collections
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


class BlockArmProbe:
    """Times the block arm and counts where it read its rows, per recall.

    Wraps `blocks.search` (the block arm's one entry, reached through the module
    attribute) and `block_index._note` (where every read says whether the file or
    SQLite served it). Both wrappers run in every arm, so they cost every arm alike.
    """

    def __init__(self) -> None:
        from cpersona import block_index, blocks

        self.calls: list[float] = []
        self.sources: collections.Counter = collections.Counter()
        search, note = blocks.search, block_index._note

        async def timed_search(*a, **k):
            t0 = time.perf_counter()
            try:
                return await search(*a, **k)
            finally:
                self.calls.append((time.perf_counter() - t0) * 1000)

        def counted_note(source, **fields):
            self.sources[source if source == "file" else f"{source}:{fields.get('reason')}"] += 1
            note(source, **fields)

        blocks.search = timed_search
        block_index._note = counted_note

    def take(self) -> tuple[float, int]:
        """The block arm's time in the recall just made, and how many calls it took."""
        total, n = sum(self.calls), len(self.calls)
        self.calls.clear()
        return total, n

    def take_sources(self) -> dict:
        out = dict(sorted(self.sources.items()))
        self.sources.clear()
        return out


def refs_of(res) -> list[str] | None:
    if not isinstance(res, dict):
        return None
    return [m.get("ref") for m in res.get("messages", [])]


async def touch_newest(target_rows: int, already: set) -> dict:
    """Mark the block rows of the newest memories as changed, up to ``target_rows`` in all.

    Rewrites one column of each row to its own value, which fires the change log's
    update trigger once per row when the log is on and leaves the rows as they were,
    so a recall's answer cannot move. Memories are taken newest first, skipping
    those an earlier call took, while the rows taken stay within the target.
    """
    from cpersona.block_index import _clock
    from cpersona.database import connection, transaction

    chosen, rows = [], sum(n for _, n in already)
    taken_ids = {i for i, _ in already}
    async with connection() as db:
        ids = await db.execute_fetchall("SELECT id FROM memories ORDER BY id DESC")
        for (mid,) in ids:
            if mid in taken_ids:
                continue
            n = (await db.execute_fetchall(
                "SELECT COUNT(*) FROM record_blocks WHERE parent_kind = 'mem' AND parent_id = ?", (mid,)
            ))[0][0]
            if not n:
                continue
            if rows + n > target_rows:
                break
            chosen.append((mid, n))
            rows += n
    async with transaction(scope_stats_neutral=True) as db:
        for start in range(0, len(chosen), 500):
            part = [mid for mid, _ in chosen[start : start + 500]]
            marks = ",".join("?" * len(part))
            await db.execute(
                "UPDATE record_blocks SET start_char = start_char"
                f" WHERE parent_kind = 'mem' AND parent_id IN ({marks})",
                part,
            )
    already.update(chosen)
    async with connection() as db:
        clock = await _clock(db)
    return {
        "target_rows": target_rows,
        "records_touched": len(already),
        "rows_touched": sum(n for _, n in already),
        "log": None if clock is None else dict(zip(("logging", "generation", "head", "pruned_through"), clock)),
    }


async def run(args) -> dict:
    from cpersona import block_index, config, vector, vector_index
    from cpersona._vendored_mcp_common.embedding_client import EmbeddingClient
    from cpersona.database import close_db, connection, init_db
    import cpersona.server as server_mod

    if args.corpus_db:
        # The corpus is a finished database: copy it over the scratch path before
        # anything opens it, so the run never writes to the file it was given.
        shutil.copyfile(args.corpus_db, os.environ["CPERSONA_DB_PATH"])
    await init_db()
    if args.block_index != block_index.enabled():
        # An arm must be what its name says: the reader follows the setting, not the flag.
        raise SystemExit(
            f"--block-index is {args.block_index} but the block index is "
            f"{'on' if block_index.enabled() else 'off'} (CPERSONA_BLOCK_INDEX)"
        )
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

    result["pythonhashseed"] = os.environ.get("PYTHONHASHSEED")
    result["block_index"] = {"enabled": block_index.enabled()}
    if args.block_index:
        t0 = time.perf_counter()
        built = await block_index.build_block_index()
        result["block_index"]["build"] = built
        result["block_index"]["build_seconds"] = round(time.perf_counter() - t0, 3)
        if not built.get("built"):
            raise SystemExit(f"block index build declined: {built.get('reason')}")
        result["block_index"]["status_before"] = await block_index.status()
    probe_arm = BlockArmProbe()

    async with connection() as db:

        # Warm-up texts are never reused below.
        for text in warm_texts:
            for client in (stub, real):
                use(client)
                await server_mod.do_recall(agent_id=agent, query=text, limit=args.limit)
        probe_arm.take()
        result["block_sources_warmup"] = probe_arm.take_sources()

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
        block_stub, block_real, refs_stub, refs_real, block_calls = [], [], [], [], []
        for i, text in enumerate(timed_texts):
            order = (stub, real) if i % 2 == 0 else (real, stub)
            for client in order:
                use(client)
                t0 = time.perf_counter()
                res = await server_mod.do_recall(agent_id=agent, query=text, limit=args.limit)
                elapsed = (time.perf_counter() - t0) * 1000
                block_ms, calls = probe_arm.take()
                block_calls.append(calls)
                returned = len(res.get("messages", [])) if isinstance(res, dict) else None
                if client is stub:
                    stub_ms.append(elapsed)
                    rows_stub.append(returned)
                    block_stub.append(block_ms)
                    refs_stub.append(refs_of(res))
                else:
                    real_ms.append(elapsed)
                    rows_real.append(returned)
                    block_real.append(block_ms)
                    refs_real.append(refs_of(res))
        result["block_sources"] = probe_arm.take_sources()

        embed_ms = []
        for i, text in enumerate(embed_texts):
            t0 = time.perf_counter()
            vec = await real.embed([text])
            embed_ms.append((time.perf_counter() - t0) * 1000)
            if not vec or len(vec[0]) != args.dim:
                raise SystemExit(f"embed probe {i} returned no vector of width {args.dim}")

        result["load_after"] = os.getloadavg()

    # After the registered loop, so nothing here can touch its figures: the same
    # timed texts again with the stand-in vector, each time more rows have changed
    # since the block index file was built. Run in every arm, so an arm with the
    # file off rewrites the same rows.
    phases = []
    touched: set = set()
    for target in args.touch_rows:
        phase = await touch_newest(target, touched)
        if args.settle_load is not None:
            waited, deadline = 0.0, time.monotonic() + args.settle_timeout
            while os.getloadavg()[0] >= args.settle_load and time.monotonic() < deadline:
                time.sleep(15)
                waited += 15
            phase["settle_waited_s"] = waited
        use(stub)
        ms, block_ms, refs = [], [], []
        for text in timed_texts:
            t0 = time.perf_counter()
            res = await server_mod.do_recall(agent_id=agent, query=text, limit=args.limit)
            ms.append((time.perf_counter() - t0) * 1000)
            block_ms.append(probe_arm.take()[0])
            refs.append(refs_of(res))
        phase["do_recall_stub_ms"] = summary(ms)
        phase["block_arm_stub_ms"] = summary(block_ms)
        phase["refs_stub"] = refs
        phase["block_sources"] = probe_arm.take_sources()
        phases.append(phase)
    result["touch_phases"] = phases
    if args.block_index:
        result["block_index"]["status_after"] = await block_index.status()

    await close_db()

    result["do_recall_stub_ms"] = summary(stub_ms)
    result["do_recall_real_ms"] = summary(real_ms)
    result["paired_difference_ms"] = summary([r - s for r, s in zip(real_ms, stub_ms)])
    result["block_arm_ms"] = {"stub": summary(block_stub), "real": summary(block_real)}
    result["block_arm_calls"] = block_calls
    result["refs"] = {"stub": refs_stub, "real": refs_real}
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
    ap.add_argument(
        "--block-index", action="store_true",
        help="build the block index file before the warm-up (needs CPERSONA_BLOCK_INDEX=true; refused otherwise)",
    )
    ap.add_argument(
        "--touch-rows", default="",
        help="comma-separated row totals: after the timed loop, mark that many block rows changed, "
        "newest memories first, and recall the timed texts again with the stand-in vector",
    )
    args = ap.parse_args()
    args.touch_rows = [int(x) for x in args.touch_rows.split(",") if x.strip()]
    if args.touch_rows != sorted(args.touch_rows):
        ap.error("--touch-rows totals are cumulative and must rise")

    result = asyncio.run(run(args))
    print(json.dumps(result, indent=2, sort_keys=True))
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(result, fh, indent=2, sort_keys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
