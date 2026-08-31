"""Order stability of the local vector scan — measurement harness.

Why this exists
---------------
The contiguous-sidecar work replaces the producer of phase 1 in
``_scan_memories_local``::

    SELECT id, embedding FROM memories
     WHERE <isolation> AND embedding IS NOT NULL [AND <source>]
     ORDER BY created_at DESC
     LIMIT ?

That statement's row order is not a presentation detail: it IS the tie-break.
Survivors keep scan order, ``heapq.nlargest`` is stable, and the ``limit`` cut
inside the scan re-sorts back into scan order — so two rows of equal similarity
are separated by nothing except where this query put them. A replacement that
reproduces the scores bit-for-bit but not this order still changes answers.

``created_at`` is ``TEXT NOT NULL DEFAULT (datetime('now'))`` — one-second
resolution — so equal keys are not an edge case in a corpus written faster than
one row per second.

Pre-registered expectations (written before the first run)
----------------------------------------------------------
H1  Rows sharing a ``created_at`` come back in ``id`` ASC order: every candidate
    index ends in ``created_at DESC``, and SQLite orders equal index keys by
    rowid ascending.
H2  That order does not depend on which index the planner picks, because all the
    candidates share the same trailing column. Falsified if any axis combination
    produces a plan that sorts instead of walking an index — a sorter is not
    required to be stable.
H3  ``ANALYZE`` does not change the answer to H1/H2, only possibly the plan.
H4  A ``LIMIT`` that cuts through a tie group keeps the ids H1 predicts (the
    consequence that matters: at the window boundary the tie order decides
    membership, not just presentation).
H5  Episodes behave identically — the two scans are structurally mirrored.

Usage:  uv run python benchmarks/measurements/order_stability_scan.py [--json OUT]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

# Hermetic: a scratch DB, no embedding backend, no operating-context sidecar.
# Pinned before any cpersona import (config is read at import time).
_SCRATCH = tempfile.mkdtemp(prefix="order-stability-")
os.environ["CPERSONA_DB_PATH"] = os.path.join(_SCRATCH, "order_stability.db")
os.environ["CPERSONA_EMBEDDING_MODE"] = "none"
os.environ["CPERSONA_OPERATING_CONTEXT"] = "off"

from cpersona.isolation import isolation_where  # noqa: E402

AGENTS = ("agent.one", "agent.two")
PROJECTS = ("", "proj.x", "proj.y")
CHANNELS = ("", "chan.a", "chan.b")
# Three timestamps, each shared by many rows: the tie groups under test.
STAMPS = ("2026-03-01 00:00:00", "2026-03-01 00:00:01", "2026-03-01 00:00:02")
ROWS_PER_CELL = 4
DUMMY_EMBEDDING = b"\x00" * 32  # width is irrelevant here; only NOT NULL matters

# The axis combinations the product actually issues. `None` = no filter on that
# axis (see isolation_where: agent None is a deliberate cross-agent scan, project
# None is no filter, project '' is global-pool-only, channel '' is no filter).
AXIS_CASES = (
    ("agent only", dict(agent_id=AGENTS[0])),
    ("agent + project global", dict(agent_id=AGENTS[0], project_id="")),
    ("agent + project X", dict(agent_id=AGENTS[0], project_id="proj.x")),
    ("agent + channel", dict(agent_id=AGENTS[0], channel="chan.a")),
    ("agent + project X + channel", dict(agent_id=AGENTS[0], project_id="proj.x", channel="chan.a")),
    ("cross-agent scan", dict(agent_id=None)),
)


async def build(db) -> None:
    """Insert the matrix, interleaved so a physical tie group spans every axis.

    Interleaving matters: if each (project, channel) cell were inserted as one
    contiguous block, a filtered read would see ids that are contiguous anyway
    and any ordering rule would look the same. Rotating the cells inside each
    timestamp makes the surviving ids of a filtered tie group non-adjacent, so a
    rule that only holds for dense id runs shows up as a difference.
    """
    rows = []
    for stamp in STAMPS:
        for rep in range(ROWS_PER_CELL):
            for agent in AGENTS:
                for project in PROJECTS:
                    for channel in CHANNELS:
                        rows.append(
                            (
                                agent,
                                project,
                                "",  # msg_id
                                f"{agent}|{project}|{channel}|{stamp}|{rep}",
                                json.dumps({"type": "Agent", "id": f"src.{rep % 2}"}),
                                stamp,  # timestamp
                                DUMMY_EMBEDDING,
                                channel,
                                stamp,  # created_at
                            )
                        )
    await db.executemany(
        "INSERT INTO memories (agent_id, project_id, msg_id, content, source, timestamp,"
        " embedding, channel, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    ep_rows = [
        (agent, project, f"summary {agent} {project} {channel} {stamp} {rep}", DUMMY_EMBEDDING, channel, stamp)
        for stamp in STAMPS
        for rep in range(ROWS_PER_CELL)
        for agent in AGENTS
        for project in PROJECTS
        for channel in CHANNELS
    ]
    await db.executemany(
        "INSERT INTO episodes (agent_id, project_id, summary, embedding, channel, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        ep_rows,
    )
    await db.commit()


def _phase1_sql(table: str, clause: str, src_clause: str, order: str = "created_at DESC") -> str:
    where = clause or "1=1"
    return (
        f"SELECT id, created_at FROM {table}"
        f" WHERE {where} AND embedding IS NOT NULL{src_clause}"
        f" ORDER BY {order} LIMIT ?"
    )


def _tie_groups(rows) -> list[tuple[str, list[int]]]:
    """Consecutive runs of equal created_at, in returned order."""
    groups: list[tuple[str, list[int]]] = []
    for row_id, stamp in rows:
        if groups and groups[-1][0] == stamp:
            groups[-1][1].append(row_id)
        else:
            groups.append((stamp, [row_id]))
    return groups


async def probe(
    db, table: str, label: str, axes: dict, src: bool, limit: int, order: str = "created_at DESC"
) -> dict:
    iso = isolation_where(**axes)
    src_clause = " AND json_extract(source, '$.id') LIKE ? ESCAPE '\\'" if src else ""
    sql = _phase1_sql(table, iso.clause, src_clause, order)
    params = (*iso.params, *(("src.0%",) if src else ()), limit)

    plan = await db.execute_fetchall("EXPLAIN QUERY PLAN " + sql, params)
    rows = await db.execute_fetchall(sql, params)
    rows = [(int(r[0]), str(r[1])) for r in rows]
    groups = _tie_groups(rows)

    return {
        "table": table,
        "case": label,
        "source_filter": src,
        "limit": limit,
        "order_by": order,
        "plan": " | ".join(str(p[-1]) for p in plan),
        "returned": len(rows),
        "order": [r[0] for r in rows],
        "stamps_descending": all(
            groups[i][0] > groups[i + 1][0] for i in range(len(groups) - 1)
        ),
        "ties_id_ascending": all(g[1] == sorted(g[1]) for g in groups),
        "tie_group_sizes": [len(g[1]) for g in groups],
    }


async def run_matrix(db, analyzed: bool) -> list[dict]:
    out = []
    for table in ("memories", "episodes"):
        for label, axes in AXIS_CASES:
            if table == "episodes" and "source" in label:
                continue
            out.append(await probe(db, table, label, axes, src=False, limit=10_000))
    # Source filter: memories only (episodes carry no per-user source tagging).
    out.append(await probe(db, "memories", "agent + source", dict(agent_id=AGENTS[0]), src=True, limit=10_000))
    for row in out:
        row["analyzed"] = analyzed
    return out


async def limit_cut(db, analyzed: bool) -> list[dict]:
    """H4: a LIMIT that lands inside a tie group — which ids survive the cut."""
    out = []
    for limit in (1, 5, 17, 36):
        probe_row = await probe(
            db, "memories", "agent + project X + channel", AXIS_CASES[4][1], src=False, limit=limit
        )
        probe_row["analyzed"] = analyzed
        probe_row["probe"] = "limit_cut"
        out.append(probe_row)
    return out


async def explicit_order(db) -> list[dict]:
    """Can the tie order be stated instead of inherited?

    The current statement gets `id ASC` inside a tie group as a by-product of how
    SQLite walks an index whose last column is `created_at DESC` — nothing in the
    SQL asks for it. Spelling it out (`ORDER BY created_at DESC, id ASC`) would
    make the contract explicit and independent of the planner. That is only worth
    proposing if it is free: if the extra term forces a sort, it costs the very
    scan this work is trying to speed up. Measured, not assumed.
    """
    out = []
    for label, axes in AXIS_CASES:
        implicit = await probe(db, "memories", label, axes, src=False, limit=10_000)
        explicit = await probe(
            db, "memories", label, axes, src=False, limit=10_000, order="created_at DESC, id ASC"
        )
        out.append(
            {
                "probe": "explicit_order",
                "case": label,
                "same_order": implicit["order"] == explicit["order"],
                "same_plan": implicit["plan"] == explicit["plan"],
                "plan_implicit": implicit["plan"],
                "plan_explicit": explicit["plan"],
            }
        )
    return out


async def detector_check(db) -> dict:
    """Does `ties_id_ascending` have teeth?

    Every probe above reports True, and a predicate that cannot report False
    proves nothing. Run the same probe against an order that is deliberately
    wrong (`id DESC` inside the tie group) and require the predicate to fail. If
    this comes back True, the whole record is decoration.
    """
    wrong = await probe(
        db, "memories", "detector", AXIS_CASES[0][1], src=False, limit=10_000,
        order="created_at DESC, id DESC",
    )
    return {
        "probe": "detector_check",
        "order_by": wrong["order_by"],
        "ties_id_ascending": wrong["ties_id_ascending"],
        "detector_has_teeth": wrong["ties_id_ascending"] is False,
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=None, help="write the full record here")
    args = ap.parse_args()

    from cpersona.database import get_db

    db = await get_db()
    await build(db)

    record: dict = {
        "sqlite_version": sqlite3.sqlite_version,
        "python": sys.version.split()[0],
        "rows_memories": len(AGENTS) * len(PROJECTS) * len(CHANNELS) * len(STAMPS) * ROWS_PER_CELL,
        "indexes": [],
        "probes": [],
    }
    idx = await db.execute_fetchall(
        "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name IN ('memories','episodes')"
        " ORDER BY name"
    )
    record["indexes"] = [{"name": r[0], "sql": r[1]} for r in idx]

    record["probes"] += await run_matrix(db, analyzed=False)
    record["probes"] += await limit_cut(db, analyzed=False)
    before = {(p["table"], p["case"], p["limit"], p["source_filter"]): p["order"] for p in record["probes"]}

    await db.execute("ANALYZE")
    await db.commit()
    after_probes = await run_matrix(db, analyzed=True) + await limit_cut(db, analyzed=True)
    record["probes"] += after_probes
    after = {(p["table"], p["case"], p["limit"], p["source_filter"]): p["order"] for p in after_probes}

    record["analyze_changed_order"] = sorted(str(k) for k in before if before[k] != after.get(k))
    record["plans_before"] = sorted({p["plan"] for p in record["probes"] if not p["analyzed"]})
    record["plans_after"] = sorted({p["plan"] for p in record["probes"] if p["analyzed"]})

    record["explicit_order"] = await explicit_order(db)
    record["detector_check"] = await detector_check(db)

    from cpersona.database import close_db

    await close_db()

    # Verdicts, keyed to the pre-registered hypotheses.
    ties_ok = all(p["ties_id_ascending"] for p in record["probes"])
    desc_ok = all(p["stamps_descending"] for p in record["probes"])
    sorters = [p for p in record["probes"] if "SCAN" in p["plan"] and "USING" not in p["plan"]]
    record["verdict"] = {
        "H1_ties_id_ascending": ties_ok,
        "H2_order_index_independent": len(record["plans_before"]) > 1 and ties_ok,
        "H3_analyze_order_stable": not record["analyze_changed_order"],
        "H5_episodes_match": all(
            p["ties_id_ascending"] for p in record["probes"] if p["table"] == "episodes"
        ),
        "created_at_descending": desc_ok,
        "plans_without_index": [p["plan"] for p in sorters],
    }

    print(f"sqlite {record['sqlite_version']}  rows={record['rows_memories']}")
    print(f"distinct plans (pre-ANALYZE):  {len(record['plans_before'])}")
    for plan in record["plans_before"]:
        print(f"  {plan}")
    print(f"distinct plans (post-ANALYZE): {len(record['plans_after'])}")
    for plan in record["plans_after"]:
        print(f"  {plan}")
    print(f"ANALYZE changed order for: {record['analyze_changed_order'] or 'nothing'}")
    for key, value in record["verdict"].items():
        print(f"{key}: {value}")
    print("\nexplicit `ORDER BY created_at DESC, id ASC` vs the current statement:")
    for row in record["explicit_order"]:
        print(
            f"  {row['case']:28} same_order={row['same_order']!s:5} same_plan={row['same_plan']!s:5}"
            f"  {row['plan_explicit']}"
        )

    print("\nlimit cuts (agent + project X + channel):")
    for p in record["probes"]:
        if p.get("probe") == "limit_cut":
            tag = "post" if p["analyzed"] else "pre "
            print(f"  {tag} limit={p['limit']:>3}  ids={p['order']}")

    teeth = record["detector_check"]["detector_has_teeth"]
    print(f"\ndetector_has_teeth (ties_id_ascending can report False): {teeth}")

    if args.json:
        with open(args.json, "w") as fh:
            json.dump(record, fh, indent=2)
        print(f"\nwrote {args.json}")
    return 0 if (ties_ok and desc_ok and teeth) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
