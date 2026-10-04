"""Build a corpus of real conversation turns through the store path, for a latency run.

`perf_contiguous_index.build_corpus` writes 100,000 rows of nine tokens each
(`memory row {n} about topic {n % 997}`) straight into the table, with random
vectors and no derived records. That measures the recall path over a corpus no
deployment has: real memories are tens to hundreds of tokens long, the lexical
arms match far more of them, and the default configuration builds and reads
blocks (and, for text past the embedding window, nodes) that a direct insert
never creates.

This script stores conversation text from LongMemEval through `do_store`, the
path a client call takes, with a real embedding client, then builds every node
and block the stores queued and drains the task queue. The result is a database
file the latency driver opens instead of building its synthetic corpus
(`perf_recall_with_embedding.py --corpus-db`).

Two ways to cut records. By default one turn is one record. With
`--length-percentiles`, record lengths are drawn from a measured distribution
(`recall_latency_realistic/length_percentiles.json`: the memory rows of one
existing deployment, in cl100k tokens) and cut from consecutive turns, so the
corpus has the lengths a deployment actually stores. Text is taken in file
order: each distinct haystack session once, each turn in order. A store the
server declines (a duplicate) is counted and skipped, not retried.

It also writes the query texts the driver times: the questions of the same file,
in order, each text once.

Registration: prereg-recall-latency-realistic-corpus.md.

Usage:
  python benchmarks/measurements/build_realistic_corpus.py \\
      --longmemeval longmemeval_m.json --rows 100000 --out corpus/ \\
      --length-percentiles benchmarks/measurements/recall_latency_realistic/length_percentiles.json \\
      --embed-url http://127.0.0.1:8401/embed
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

AGENT = "perf.real"


def parse_date(text: str) -> datetime:
    """LongMemEval writes session dates as '2023/05/20 (Sat) 02:21'."""
    day, _, clock = text.partition(") ")
    return datetime.strptime(day.split(" (")[0] + " " + clock, "%Y/%m/%d %H:%M").replace(tzinfo=timezone.utc)


def distinct_sessions(data: list) -> list[tuple[str, str, list]]:
    """Each haystack session once, in file order (questions share sessions)."""
    seen, out = set(), []
    for q in data:
        for sid, date, sess in zip(q["haystack_session_ids"], q["haystack_dates"], q["haystack_sessions"]):
            if sid not in seen:
                seen.add(sid)
                out.append((sid, date, sess))
    return out


def turns(sessions: list):
    for sid, date, sess in sessions:
        start = parse_date(date)
        for k, m in enumerate(sess):
            text = (m.get("content") or "").strip()
            if text:
                yield f"lme:{sid}:{k}", m.get("role", ""), text, start + timedelta(seconds=k)


def matched(sessions: list, enc, percentiles: list[int], seed: int):
    """Records whose lengths follow a measured distribution, cut from the same turns.

    Each record's length in cl100k tokens is drawn from `percentiles` (the 0th to
    the 100th percentile of the lengths being matched, linearly interpolated) with
    a seeded generator. Consecutive turns, each written as "role: text" on its own
    line, are joined until the draw is reached and the tokens past it are cut. A
    record that reaches the end of a session continues into the next one, so the
    realised lengths follow the draws; the builder reports them.
    """
    import numpy as np

    rng = np.random.default_rng(seed)
    grid = np.linspace(0.0, 1.0, len(percentiles))

    def lines():
        for sid, date, sess in sessions:
            start = parse_date(date)
            for k, m in enumerate(sess):
                text = (m.get("content") or "").strip()
                if text:
                    yield f"lmm:{sid}:{k}", start + timedelta(seconds=k), f"{m.get('role', '')}: {text}"

    stream = lines()
    for msg_id, ts, line in stream:
        target = max(1, int(round(float(np.interp(rng.random(), grid, percentiles)))))
        toks = enc.encode(line, disallowed_special=())
        while len(toks) < target:
            nxt = next(stream, None)
            if nxt is None:
                break
            toks += enc.encode("\n" + nxt[2], disallowed_special=())
        text = enc.decode(toks[:target]).strip()
        if text:
            yield msg_id, "user", text, ts


def questions(data: list, n: int) -> list[str]:
    out, seen = [], set()
    for q in data:
        t = q["question"].strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
        if len(out) == n:
            break
    return out


async def run(args) -> dict:
    from cpersona import admin_handlers, memory_handlers, nodes, tasks, vector
    from cpersona._vendored_mcp_common.embedding_client import EmbeddingClient
    from cpersona.database import close_db, connection, init_db
    import cpersona.server as server_mod
    import tiktoken

    enc = tiktoken.get_encoding("cl100k_base")
    data = json.load(open(args.longmemeval))
    sessions = distinct_sessions(data)
    qs = questions(data, args.queries)
    del data  # the -M file is 2.7 GB parsed; only its distinct sessions are kept
    gc.collect()

    await init_db()
    client = EmbeddingClient(mode="http", http_url=args.embed_url, timeout=60.0)
    await client.initialize()
    vector._embedding_client = client
    server_mod._embedding_client = client
    queue = tasks.MemoryTaskQueue()
    queue._running = True  # drained by hand below; no background loop
    tasks._task_queue = queue

    # Phase 1: the stores, in file order, `--concurrency` at a time. Writes are
    # serialised by the server's own write lock; what overlaps is the waiting on
    # the embedding server. The last group asks only for the rows still missing,
    # so the corpus ends at exactly --rows stored records.
    stored = skipped = tokens = chars = 0
    t0 = time.perf_counter()
    if args.length_percentiles:
        pct = json.load(open(args.length_percentiles))["percentiles_0_to_100"]
        source = matched(sessions, enc, pct, args.seed)
    else:
        source = turns(sessions)
    lengths: list[int] = []

    async def store(item):
        msg_id, role, text, ts = item
        res = await memory_handlers.do_store(
            agent_id=AGENT,
            message={
                "id": msg_id,
                "content": text,
                "timestamp": ts.isoformat(),
                "source": {"type": "User" if role == "user" else "Agent", "id": "longmemeval", "name": role},
            },
        )
        return res.get("result") == "stored", text

    last_report = 0
    while stored < args.rows:
        group = [item for _, item in zip(range(min(args.concurrency, args.rows - stored)), source)]
        if not group:
            break
        for ok, text in await asyncio.gather(*(store(item) for item in group)):
            if ok:
                stored += 1
                lengths.append(len(enc.encode(text, disallowed_special=())))
                tokens += lengths[-1]
                chars += len(text)
            else:
                skipped += 1
        if stored - last_report >= 5000:
            last_report = stored
            print(f"stored {stored}, skipped {skipped}, {time.perf_counter() - t0:.0f} s", flush=True)
    store_seconds = time.perf_counter() - t0

    # Phase 2: the derived records the stores queued, built by the server's own
    # functions — every node first, then every block, as the queue's FIFO order
    # has it for each record (do_store queues nodes before blocks). Both builds
    # are idempotent, so the queue's own drain that follows finds them current
    # and only clears its table; it stays the authority on what was pending.
    from cpersona import blocks

    async with connection() as db:
        pending = await db.execute_fetchall("SELECT task_type, payload FROM pending_memory_tasks ORDER BY id")
    sem = asyncio.Semaphore(args.concurrency)
    built = {}

    async def build(fn, payload):
        async with sem:
            outcome = await fn(payload)
        key = outcome.split(" ")[0] if isinstance(outcome, str) else repr(outcome)
        built[key] = built.get(key, 0) + 1

    for kind, fn in ((nodes.TASK_TYPE, nodes.build_nodes), (blocks.TASK_TYPE, blocks.build_blocks)):
        payloads = [json.loads(p) for t, p in pending if t == kind]
        for start in range(0, len(payloads), 2000):
            await asyncio.gather(*(build(fn, pl) for pl in payloads[start : start + 2000]))
            print(f"{kind}: {min(start + 2000, len(payloads))}/{len(payloads)}, {time.perf_counter() - t0:.0f} s", flush=True)
    await queue._drain(admin_handlers, memory_handlers, nodes)

    async with connection() as db:
        counts = {}
        for table in ("memories", "record_nodes", "record_blocks", "record_block_vectors", "pending_memory_tasks"):
            try:
                counts[table] = (await db.execute_fetchall(f"SELECT COUNT(*) FROM {table}"))[0][0]
            except Exception as exc:  # a table this version does not have is recorded, not invented
                counts[table] = f"unavailable: {exc!r}"
        await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    await close_db()

    with open(os.path.join(args.out, "queries.json"), "w") as fh:
        json.dump(qs, fh, ensure_ascii=False, indent=1)

    from cpersona import __version__

    return {
        "agent": AGENT,
        "rows_requested": args.rows,
        "stored": stored,
        "skipped": skipped,
        "tokens_cl100k": tokens,
        "chars": chars,
        "table_counts": counts,
        "queries_written": len(qs),
        "embed_url": args.embed_url,
        "cpersona": __version__,
        "build_seconds": round(time.perf_counter() - t0, 1),
        "store_seconds": round(store_seconds, 1),
        "source_file": os.path.basename(args.longmemeval),
        "length_percentiles": args.length_percentiles and os.path.basename(args.length_percentiles),
        "seed": args.seed if args.length_percentiles else None,
        "tokens_per_record": {
            "mean": round(tokens / max(stored, 1), 1),
            "median": sorted(lengths)[len(lengths) // 2] if lengths else None,
            "p10": sorted(lengths)[int(0.1 * (len(lengths) - 1))] if lengths else None,
            "p90": sorted(lengths)[int(0.9 * (len(lengths) - 1))] if lengths else None,
            "max": max(lengths) if lengths else None,
        },
        "derived_build_outcomes": built,
        "concurrency": args.concurrency,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--longmemeval", required=True, help="longmemeval_s_cleaned.json")
    ap.add_argument("--rows", type=int, default=100000)
    ap.add_argument("--queries", type=int, default=53, help="3 warm-up + 25 timed + 25 embed-only")
    ap.add_argument("--out", required=True)
    ap.add_argument("--embed-url", default="http://127.0.0.1:8401/embed")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--length-percentiles", default=None,
                    help="match record lengths to this distribution (JSON with percentiles_0_to_100); "
                         "without it, one turn is one record")
    ap.add_argument("--seed", type=int, default=20261004)
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    os.environ["CPERSONA_DB_PATH"] = os.path.join(os.path.abspath(args.out), "corpus.db")
    os.environ["CPERSONA_EMBEDDING_MODE"] = "http"
    os.environ["CPERSONA_OPERATING_CONTEXT"] = "off"
    os.environ.setdefault("CPERSONA_FTS_ENABLED", "true")

    result = asyncio.run(run(args))
    print(json.dumps(result, indent=2, sort_keys=True))
    with open(os.path.join(args.out, "corpus_meta.json"), "w") as fh:
        json.dump(result, fh, indent=2, sort_keys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
