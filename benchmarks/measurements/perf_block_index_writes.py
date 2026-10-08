"""What the block change log costs a block write, with the log absent, off and on.

Registration: prereg-recall-latency-block-index-file.md. Schema 19 added four
triggers on `record_blocks` that keep the change log the block index file is
read through. They exist in every database at schema 19; with the file off their
`WHEN` finds the log off and they do nothing else, and with it on each written
row also moves the log's clock and adds a log row. This driver rewrites the same
records' blocks through the server's own write (`blocks.write_blocks`, inside
`transaction()`, as the queue's block build does) under three conditions:

- ``T0``: the four log triggers dropped (the schema before 19, for this table)
- ``T1``: the triggers present, the log off (a default deployment of 2.6.7)
- ``T2``: the triggers present, the log on (a deployment with the file on)

Each record is rewritten from its own stored rows — the same spans, bits and
vectors — so every run writes the same rows and no embedding is computed. The
timed span is one record's transaction, from taking the write lock to the
commit. Write-ahead log frames are counted per run with automatic checkpoints
off, so a run's figure is every page it wrote.

With ``--embed-url``, the first ``--embed-records`` records are also divided and
embedded again through a real embedding server (`blocks.prepare_blocks`, the part
of the queue's block build that runs outside the lock), so the write's share of a
whole block build can be stated.

Usage:
  python benchmarks/measurements/perf_block_index_writes.py --corpus-db corpus.db \\
      --records 300 --order T1,T2,T0,T0,T2,T1 --json out.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import shutil
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import perf_contiguous_index as perf  # noqa: E402,F401  (sets the scratch DB and env before cpersona loads)


def nearest_rank(samples: list[float], pct: float) -> float:
    ordered = sorted(samples)
    return ordered[max(0, math.ceil(pct / 100 * len(ordered)) - 1)]


def summary(samples: list[float]) -> dict:
    return {
        "n": len(samples),
        "median": round(statistics.median(samples), 4),
        "mean": round(statistics.fmean(samples), 4),
        "p95": round(nearest_rank(samples, 95), 4),
        "max": round(max(samples), 4),
        "min": round(min(samples), 4),
    }


async def load_records(n: int) -> list:
    """The newest ``n`` memories whose blocks can be rewritten exactly as stored."""
    from cpersona import blocks
    from cpersona.database import connection

    out = []
    async with connection() as db:
        ids = await db.execute_fetchall("SELECT id, content FROM memories ORDER BY id DESC")
        for mid, content in ids:
            rows = await db.execute_fetchall(
                "SELECT block_index, start_char, end_char, forced_boundary, embedding_bits, embedding_model"
                " FROM record_blocks WHERE parent_kind = 'mem' AND parent_id = ? ORDER BY block_index",
                (mid,),
            )
            vecs = await db.execute_fetchall(
                "SELECT block_index, embedding_i8 FROM record_block_vectors"
                " WHERE parent_kind = 'mem' AND parent_id = ? ORDER BY block_index",
                (mid,),
            )
            models = {r[5] for r in rows}
            # Only a complete, single-model set can be written back unchanged.
            if (
                not rows
                or len(models) != 1
                or [r[0] for r in rows] != list(range(len(rows)))
                or [v[0] for v in vecs] != list(range(len(rows)))
                or any(r[4] is None for r in rows)
            ):
                continue
            out.append(
                blocks.PreparedBlocks(
                    "mem",
                    mid,
                    content,
                    [blocks.BlockSpan(r[1], r[2], bool(r[3])) for r in rows],
                    [r[4] for r in rows],
                    [v[1] for v in vecs],
                    models.pop(),
                )
            )
            if len(out) == n:
                break
    return out


async def set_condition(name: str) -> dict:
    from cpersona import block_index
    from cpersona.database import BLOCK_LOG_SQL, get_db, transaction

    db = await get_db()
    if name == "T0":
        async with transaction() as tx:
            for trigger in block_index.LOG_TRIGGERS:
                await tx.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    else:
        await db.executescript(BLOCK_LOG_SQL)  # CREATE ... IF NOT EXISTS: puts back what T0 dropped
        async with transaction() as tx:
            await tx.execute(
                "UPDATE block_log_clock SET logging = ? WHERE id = 0", (1 if name == "T2" else 0,)
            )
    triggers = await db.execute_fetchall(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name IN (?, ?, ?, ?)", block_index.LOG_TRIGGERS
    )
    clock = await db.execute_fetchall("SELECT logging, generation, head FROM block_log_clock WHERE id = 0")
    return {"triggers": len(triggers), "logging": clock[0][0], "head": clock[0][2]}


async def time_prepare(records: list, url: str) -> dict:
    """Divide and embed each record again, as the queue's build does before it writes."""
    import cpersona.server as server_mod
    from cpersona import blocks, config, vector
    from cpersona._vendored_mcp_common.embedding_client import EmbeddingClient
    from cpersona.database import connection

    client = EmbeddingClient(
        mode="http",
        http_url=url,
        cache_size=config.EMBEDDING_CACHE_SIZE,
        cache_ttl=config.EMBEDDING_CACHE_TTL,
        timeout=server_mod._resolve_embedding_timeout(),
    )
    await client.initialize()
    vector._embedding_client = client
    ms, same = [], 0
    for prepared in records:
        async with connection() as db:
            bounds = await blocks._node_bounds(db, "mem", prepared.parent_id)
        t0 = time.perf_counter()
        again = await blocks.prepare_blocks("mem", prepared.parent_id, prepared.text, bounds)
        ms.append((time.perf_counter() - t0) * 1000)
        if again is not None and again.spans == prepared.spans:
            same += 1
    return {"records": len(records), "per_record_ms": summary(ms), "same_division": same}


async def run(args) -> dict:
    from cpersona import blocks
    from cpersona.database import close_db, get_db, init_db, transaction

    shutil.copyfile(args.corpus_db, os.environ["CPERSONA_DB_PATH"])
    await init_db()
    db = await get_db()
    await db.execute("PRAGMA wal_autocheckpoint = 0")
    records = await load_records(args.records)
    if len(records) < args.records:
        raise SystemExit(f"only {len(records)} records can be rewritten as stored, asked for {args.records}")
    rows_per_run = sum(len(r.spans) for r in records)

    result: dict = {
        "corpus_db": os.path.basename(args.corpus_db),
        "records": len(records),
        "rows_per_run": rows_per_run,
        "record_ids": [records[-1].parent_id, records[0].parent_id],
        "order": args.order,
        "python": sys.version.split()[0],
        "sqlite": (await db.execute_fetchall("SELECT sqlite_version()"))[0][0],
        "page_size": (await db.execute_fetchall("PRAGMA page_size"))[0][0],
        "runs": [],
    }

    for name in args.order:
        state = await set_condition(name)
        busy, *_ = (await db.execute_fetchall("PRAGMA wal_checkpoint(TRUNCATE)"))[0]
        per_record = []
        for prepared in records:
            t0 = time.perf_counter()
            async with transaction() as tx:
                ok = await blocks.write_blocks(tx, prepared)
            per_record.append((time.perf_counter() - t0) * 1000)
            if not ok:
                raise SystemExit(f"record {prepared.parent_id} was refused as stale; the copy changed under the run")
        frames = (await db.execute_fetchall("PRAGMA wal_checkpoint(PASSIVE)"))[0]
        clock = await db.execute_fetchall("SELECT head FROM block_log_clock WHERE id = 0")
        result["runs"].append({
            "condition": name,
            "state": state,
            "truncate_busy": busy,
            "per_record_ms": summary(per_record),
            "samples_ms": [round(x, 4) for x in per_record],
            "wal_frames": frames[1],
            "wal_bytes": frames[1] * (result["page_size"] + 24),
            "log_rows_added": clock[0][0] - state["head"],
        })

    if args.embed_url:
        result["prepare"] = await time_prepare(records[: args.embed_records], args.embed_url)

    try:
        size = await db.execute_fetchall(
            "SELECT SUM(pgsize), COUNT(*) FROM dbstat WHERE name = 'record_block_changes'"
        )
        log_rows = (await db.execute_fetchall("SELECT COUNT(*) FROM record_block_changes"))[0][0]
        result["change_log_table"] = {"bytes": size[0][0], "pages": size[0][1], "rows": log_rows}
    except Exception as exc:  # the record says it could not look rather than inventing a size
        result["change_log_table"] = {"unavailable": repr(exc)}

    await close_db()
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus-db", required=True, help="a finished corpus database; the run writes to a copy")
    ap.add_argument("--records", type=int, default=300)
    ap.add_argument("--order", default="T1,T2,T0,T0,T2,T1")
    ap.add_argument("--json", default=None)
    ap.add_argument("--embed-url", default=None, help="time the division and embedding of a block build too")
    ap.add_argument("--embed-records", type=int, default=30)
    args = ap.parse_args()
    args.order = [x.strip() for x in args.order.split(",") if x.strip()]
    if not set(args.order) <= {"T0", "T1", "T2"}:
        ap.error("--order names T0, T1 and T2 only")

    result = asyncio.run(run(args))
    brief = {k: v for k, v in result.items() if k != "runs"}
    brief["runs"] = [{k: v for k, v in r.items() if k != "samples_ms"} for r in result["runs"]]
    print(json.dumps(brief, indent=2, sort_keys=True))
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(result, fh, indent=2, sort_keys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
