"""Binary coarse search: how many candidates the Hamming pass keeps, and what it costs.

Pre-registration: `prereg-binary-coarse-search.md` next to this file. Read it
first; this module only executes what that document fixed. The design it
measures is `docs/BINARY_COARSE_SEARCH_DESIGN.md` (sections 5 and 9).

**What is approximated, and where.** The far seats rank the records past the
scan window by the cosine of their stored float32 vectors. The only
approximation is which records reach that ranking: the coarse supplier keeps
the `K'` records whose one-bit codes are nearest the query's. With `K'` equal
to the number of far records, the seats are filled exactly as an exact scan of
the same positions fills them. This harness measures, for a grid of `K'`, how
often the seats an exact scan would fill are filled the same way.

**How the measurement isolates that one approximation.**

1. Every cosine is computed once per (query, record), by one matmul over the
   contiguous index's float32 rows, and both the exact list and every `K'` list
   are ranked with those same values. A record's cosine therefore cannot differ
   between the two lists, and the lists differ only in which records the
   Hamming pass let through. The production ranking (`far_seats.ranked`, which
   reads the vectors from SQLite by id) is compared with this one separately,
   as a bridge check, rather than assumed equal.
2. The candidates come from the shipped supplier (`coarse_search.coarse_candidates`)
   reading the shipped coarse index. They are asked for once per query at the
   largest `K'` of the grid; a smaller `K'` is its prefix, because the
   supplier's order is total (distance, then scan position). The prefix
   property is checked, not assumed.
3. Which records a recall may seat is decided by recall itself: a record the
   answer already holds, or one an ordinary arm reached and the gate refused, is
   not eligible. The harness runs the real `do_recall` once per query with the
   far seats on, hands it the union of every list it will score as the far
   ranking, and records which of those records recall found eligible. Seating
   is then replayed for each list from that one record — the eligibility of a
   record does not depend on the far list (the far seats displace nothing).

Stores are nested by recency. The newest 237,654 rows are LongMemEval in the
scene layout of `scan_window_ab.py` (seed 20260903, rotation 0) — the store the
scan-window measurements used. The 100,000 store is its newest 100,000 rows; the
1,000,000 store puts 762,346 further documents from the other LMEB corpora
below it. Every vector is a bge-m3 vector from the Track A/B disk cache, so the
geometry the one-bit code is judged on is a real model's, not a Gaussian's.

Usage (from the repo root; each step is resumable, and `run` does them all):

    uv run python benchmarks/measurements/coarse_search_k.py run --workdir /path/to/dir
    uv run python benchmarks/measurements/coarse_search_k.py score --workdir /path/to/dir \\
        --json benchmarks/measurements/results-binary-coarse-search.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sqlite3
import statistics
import subprocess
import sys
import time
import tracemalloc
from datetime import timedelta
from pathlib import Path

import numpy as np

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[1]
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_REPO / "benchmarks"))
sys.path.insert(0, str(_REPO))

import scan_window_ab as swab

AGENT_ID = "coarse-k"
SEED = 20261001
#: The scan-window instrument's layout (`scan_window_ab.py`), unchanged.
LME_SEED = 20260903
LME_ROTATION = 0
WINDOW = 10_000
LIMIT = 10

#: Fixed before the run (prereg, "Grid").
K_GRID = (32, 64, 128, 256, 512, 1000, 2048, 4096, 8192, 16384, 32768, 65536)
K_MAX = K_GRID[-1]
#: Entries kept per ranked list. Seating skips ineligible records, so a seat can
#: sit below rank 2; check V5 fails the run if a list ran out inside this depth.
TOP_M = 128
BOUND = 0.95

STORE_ROWS = {"s100k": 100_000, "s237": None, "s1m": 1_000_000}
COST_ROWS = {"c12k": 12_000, "c15k": 15_000, "c20k": 20_000, "c30k": 30_000, "c50k": 50_000}
QSETS = ("q1", "q2")
REGIMES = ("shipped", "production")
#: The cells the decision reads (prereg, "Rule").
DECISION_CELLS = [(s, q, r) for (s, q) in (("s100k", "q1"), ("s237", "q1"), ("s1m", "q1"), ("s1m", "q2"))
                  for r in REGIMES]
N_QUERIES = 500
COST_QUERIES = 25
COST_WARMUP = 3


# ---------------------------------------------------------------------------
# plan: which documents, in which order, and which queries
# ---------------------------------------------------------------------------

def _corpus_files(lmeb: Path) -> list[Path]:
    root = lmeb / "eval_data"
    return sorted(p for p in root.rglob("corpus.jsonl") if "LongMemEval" not in p.parts)


def _query_files(lmeb: Path) -> list[Path]:
    root = lmeb / "eval_data"
    return sorted(p for p in root.rglob("queries.jsonl") if "LongMemEval" not in p.parts)


def _cached(cache: swab.CacheVectors, texts: list[str]) -> list[bool]:
    """Whether each text has a vector in the cache, without reading the vectors."""
    keys = [cache._key(t) for t in texts]
    have: set[bytes] = set()
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        ph = ",".join("?" * len(chunk))
        have.update(bytes(r[0]) for r in cache._db.execute(f"SELECT k FROM emb WHERE k IN ({ph})", chunk))
    return [k in have for k in keys]


def plan(args) -> None:
    work = Path(args.workdir)
    work.mkdir(parents=True, exist_ok=True)
    lmeb = Path(args.lmeb)
    cache = swab.CacheVectors(Path(args.cache), swab.DEFAULT_CACHE_LABEL)

    corpus, queries = swab.load_task(lmeb / "eval_data" / swab.DEFAULT_TASK)
    layout = swab.plan_layout(corpus, queries, LME_SEED, LME_ROTATION, swab.NEAR_MAX_DEPTH, swab.FAR_LO_DEPTH)
    text_of = {str(d["id"]): swab._doc_text(d) for d in corpus}
    # The shipped UNIQUE index keeps the first of two identical contents, and the
    # store is written oldest first, so the older copy is the one that stays.
    seen: set[str] = set()
    lme: list[str] = []
    for doc_id in layout["order"]:
        t = text_of[doc_id]
        if t in seen:
            continue
        seen.add(t)
        lme.append(doc_id)

    need = STORE_ROWS["s1m"] - len(lme)
    pool: list[tuple[str, str]] = []
    for path in _corpus_files(lmeb):
        rel = str(path.parent.relative_to(lmeb / "eval_data"))
        for doc in swab._load_jsonl(path):
            t = swab._doc_text(doc)
            if not t or t in seen:
                continue
            seen.add(t)
            pool.append((f"{rel}/{doc['id']}", t))
    pool_size = len(pool)
    random.Random(SEED).shuffle(pool)
    filler: list[tuple[str, str]] = []
    misses = 0
    for i in range(0, len(pool), 5000):
        chunk = pool[i:i + 5000]
        for item, ok in zip(chunk, _cached(cache, [t for _, t in chunk])):
            if ok:
                filler.append(item)
            else:
                misses += 1
        if len(filler) >= need:
            break
    if len(filler) < need:
        raise SystemExit(f"only {len(filler)} cached filler documents; the 1,000,000 store needs {need}")
    filler = filler[:need]
    with open(work / "filler.jsonl", "w", encoding="utf-8") as f:
        for m, t in filler:
            f.write(json.dumps({"m": m, "t": t}, ensure_ascii=False) + "\n")

    q1 = [{"id": q["id"], "text": q["text"]} for q in queries]
    random.Random(SEED).shuffle(q1)
    q1 = q1[:N_QUERIES]
    if not all(_cached(cache, [q["text"] for q in q1])):
        raise SystemExit("a LongMemEval query is not cached")
    q2_pool = []
    for path in _query_files(lmeb):
        rel = str(path.parent.relative_to(lmeb / "eval_data"))
        for q in swab._load_jsonl(path):
            if q.get("text"):
                q2_pool.append({"id": f"{rel}/{q['id']}", "text": q["text"]})
    q2_pool.sort(key=lambda q: q["id"])
    random.Random(SEED + 1).shuffle(q2_pool)
    q2, q2_seen = [], set()
    for i in range(0, len(q2_pool), 2000):
        chunk = q2_pool[i:i + 2000]
        for q, ok in zip(chunk, _cached(cache, [q["text"] for q in chunk])):
            if ok and q["text"] not in q2_seen:
                q2_seen.add(q["text"])
                q2.append(q)
        if len(q2) >= N_QUERIES:
            break
    q2 = q2[:N_QUERIES]
    if len(q2) < N_QUERIES:
        raise SystemExit(f"only {len(q2)} cached queries outside LongMemEval")

    (work / "plan.json").write_text(json.dumps({
        "seed": SEED, "lme_seed": LME_SEED, "lme_rotation": LME_ROTATION,
        "lme_order": lme, "filler_count": len(filler),
        "filler_pool_unique": pool_size, "filler_cache_misses_seen": misses,
        "q1": q1, "q2": q2,
    }, ensure_ascii=False), encoding="utf-8")
    print(f"plan: {len(lme)} LongMemEval rows, {len(filler)} filler rows "
          f"(pool {pool_size} unique, {misses} uncached passed over), q1 {len(q1)}, q2 {len(q2)}")


# ---------------------------------------------------------------------------
# build-store: one database per store
# ---------------------------------------------------------------------------

def _store_rows(work: Path, lmeb: Path, store: str) -> list[tuple[str, str, int]]:
    """(msg_id, text, second offset) oldest first. The offset fixes created_at,
    so a row has the same created_at in every store that holds it."""
    p = json.loads((work / "plan.json").read_text(encoding="utf-8"))
    corpus, _ = swab.load_task(lmeb / "eval_data" / swab.DEFAULT_TASK)
    text_of = {str(d["id"]): swab._doc_text(d) for d in corpus}
    lme = [(d, text_of[d], j) for j, d in enumerate(p["lme_order"])]
    if store == "s1m":
        filler = []
        with open(work / "filler.jsonl", encoding="utf-8") as f:
            for i, line in enumerate(f):
                r = json.loads(line)
                filler.append((r["m"], r["t"], i - p["filler_count"]))
        return filler + lme
    if store == "s237":
        return lme
    n = STORE_ROWS.get(store) or COST_ROWS[store]
    return lme[-n:]


async def _build_store(args) -> None:
    work, lmeb = Path(args.workdir), Path(args.lmeb)
    db_path = work / f"{args.store}.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    swab._set_env(str(db_path), window=WINDOW)
    os.environ.pop("CPERSONA_FAR_SEATS_ENABLED", None)
    from cpersona.database import close_db, get_db

    rows = _store_rows(work, lmeb, args.store)
    cache = swab.CacheVectors(Path(args.cache), swab.DEFAULT_CACHE_LABEL)
    db = await get_db()
    t0 = time.time()
    for start in range(0, len(rows), 1000):
        chunk = rows[start:start + 1000]
        vecs = cache.get_many([t for _, t, _ in chunk])
        batch = []
        for (msg_id, text, offset), vec in zip(chunk, vecs):
            created = (swab.STAMP_BASE + timedelta(seconds=offset)).strftime("%Y-%m-%d %H:%M:%S")
            batch.append((AGENT_ID, msg_id, text, "{}", "2026-01-01T00:00:00Z", "{}",
                          np.asarray(vec, dtype="<f4").tobytes(), created))
        await db.executemany(
            "INSERT OR IGNORE INTO memories "
            "(agent_id, msg_id, content, source, timestamp, metadata, embedding, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", batch)
        await db.commit()
    stored = (await db.execute_fetchall("SELECT COUNT(*) FROM memories WHERE agent_id = ?", (AGENT_ID,)))[0][0]
    await close_db()
    if stored != len(rows):
        raise SystemExit(f"{args.store}: stored {stored} of {len(rows)} rows")
    (work / f"{args.store}.build.json").write_text(json.dumps(
        {"store": args.store, "rows": stored, "seconds": round(time.time() - t0, 1)}), encoding="utf-8")
    print(f"{args.store}: {stored} rows in {time.time() - t0:.0f}s")


# ---------------------------------------------------------------------------
# index: the contiguous and the coarse index, each timed
# ---------------------------------------------------------------------------

async def _index(args) -> None:
    work = Path(args.workdir)
    db_path = work / f"{args.store}.db"
    swab._set_env(str(db_path), window=WINDOW)
    from cpersona import coarse_index, database, vector_index
    from cpersona.database import close_db, connection

    database.SKIP_BOOT_MIGRATIONS = True
    out = {"store": args.store}
    async with connection() as db:
        t = time.perf_counter()
        r = await vector_index.build_index(db, "memories")
        out["contiguous_seconds"] = round(time.perf_counter() - t, 2)
        out["contiguous_built"] = bool(r.get("built"))
        t = time.perf_counter()
        r = await coarse_index.build_coarse_index(db, "memories")
        out["coarse_seconds"] = round(time.perf_counter() - t, 2)
        out["coarse_built"] = bool(r.get("built"))
    await close_db()
    out["contiguous_bytes"] = os.path.getsize(vector_index.index_path("memories"))
    out["coarse_bytes"] = os.path.getsize(coarse_index.index_path("memories"))
    out["db_bytes"] = os.path.getsize(db_path)
    if not (out["contiguous_built"] and out["coarse_built"]):
        raise SystemExit(f"{args.store}: an index did not build: {out}")
    (work / f"{args.store}.index.json").write_text(json.dumps(out), encoding="utf-8")
    print(f"{args.store}: indexes built {out}")


# ---------------------------------------------------------------------------
# prepare: the exact list, the supplier's candidates, every K' list, checks V1-V4
# ---------------------------------------------------------------------------

def _queries(work: Path, qset: str) -> list[dict]:
    return json.loads((work / "plan.json").read_text(encoding="utf-8"))[qset]


def _ranked_top(positions: np.ndarray, cos: np.ndarray, floor: float, m: int):
    """At most `m` of the given rows at or above `floor`, by (-cosine, position)."""
    keep = cos >= floor
    positions, cos = positions[keep], cos[keep]
    if len(cos) > m:
        kth = np.partition(cos, len(cos) - m)[len(cos) - m]
        sel = cos >= kth  # every row tied with the m-th is kept, then cut by position
        positions, cos = positions[sel], cos[sel]
    order = np.lexsort((positions, -cos))[:m]
    return positions[order], cos[order]


async def _prepare(args) -> None:
    work = Path(args.workdir)
    db_path = work / f"{args.store}.db"
    swab._set_env(str(db_path), window=WINDOW)
    os.environ.pop("CPERSONA_FAR_SEATS_ENABLED", None)
    import cpersona.memory_handlers as mh
    from cpersona import coarse_index, coarse_search, far_seats, vector_index
    from cpersona.database import close_db, get_db

    vidx = vector_index.load_index("memories")
    cidx = coarse_index.load_coarse_index("memories")
    if vidx is None or cidx is None:
        raise SystemExit(f"{args.store}: an index is missing")
    if not np.array_equal(np.asarray(vidx.ids), np.asarray(cidx.ids)):
        raise SystemExit(f"{args.store}: the two indexes hold different rows")
    w0, n = far_seats.scan_start(), vidx.count
    if w0 != WINDOW:
        raise SystemExit(f"far scan starts at {w0}, expected {WINDOW}")
    n_far = n - w0
    floor = mh._vector_arm_floor(AGENT_ID)
    dim = vidx.dim

    qs = _queries(work, args.qset)
    cache = swab.CacheVectors(Path(args.cache), swab.DEFAULT_CACHE_LABEL)
    qmat = np.stack(cache.get_many([q["text"] for q in qs])).astype(np.float32)

    # One cosine per (record, query): the far rows of the contiguous index times
    # every query, in fixed chunks. Both kinds of list below read these values.
    t = time.perf_counter()
    cos = np.empty((n_far, len(qs)), dtype=np.float32)
    for lo in range(w0, n, 65536):
        hi = min(n, lo + 65536)
        cos[lo - w0:hi - w0] = np.asarray(vidx.embeddings[lo:hi], dtype=np.float32) @ qmat.T
    cos_seconds = time.perf_counter() - t

    lists = {name: {"ids": np.full((len(qs), TOP_M), -1, dtype=np.int64),
                    "pos": np.full((len(qs), TOP_M), -1, dtype=np.int64),
                    "cos": np.zeros((len(qs), TOP_M), dtype=np.float32),
                    "len": np.zeros(len(qs), dtype=np.int32)}
             for name in ["exact"] + [f"k{k}" for k in K_GRID]}

    def put(name, qi, positions, values):
        L = len(positions)
        lists[name]["pos"][qi, :L] = positions
        lists[name]["ids"][qi, :L] = np.asarray(vidx.ids)[positions]
        lists[name]["cos"][qi, :L] = values
        lists[name]["len"][qi] = L

    all_pos = np.arange(w0, n, dtype=np.int64)
    db = await get_db()
    checks = {"store": args.store, "qset": args.qset, "rows": n, "far_rows": n_far, "floor": floor,
              "dim": dim, "cosine_seconds": round(cos_seconds, 1)}
    supplier_ms, sources = [], set()
    cand_cache: dict[int, np.ndarray] = {}
    for qi, q in enumerate(qs):
        col = cos[:, qi]
        p, c = _ranked_top(all_pos, col, floor, TOP_M)
        put("exact", qi, p, c)
        t = time.perf_counter()
        found = await coarse_search.coarse_candidates(db, qmat[qi], agent_id=AGENT_ID, start=w0, k=K_MAX)
        supplier_ms.append((time.perf_counter() - t) * 1000)
        sources.add(found.source)
        cand = np.asarray(found.positions, dtype=np.int64)
        if len(cand) != min(K_MAX, n_far):
            raise SystemExit(f"supplier returned {len(cand)} candidates, expected {min(K_MAX, n_far)}")
        if qi < 20:
            cand_cache[qi] = cand
        for k in K_GRID:
            pk = cand[:k]
            p, c = _ranked_top(pk, col[pk - w0], floor, TOP_M)
            put(f"k{k}", qi, p, c)
    checks["supplier_sources"] = sorted(sources)
    checks["supplier_ms_median"] = round(statistics.median(supplier_ms), 1)
    if sources != {"index"}:
        raise SystemExit(f"supplier answered from {sources}, expected the index only")

    # V1 identity: K' = every far record returns every far position, once.
    v1 = []
    for qi in range(3):
        found = await coarse_search.coarse_candidates(db, qmat[qi], agent_id=AGENT_ID, start=w0, k=n_far)
        got = np.sort(np.asarray(found.positions, dtype=np.int64))
        v1.append(bool(len(got) == n_far and np.array_equal(got, all_pos)))
    checks["V1_identity"] = {"queries": len(v1), "pass": all(v1)}

    # V2 prefix: asking for k directly returns the first k of the K_MAX answer.
    v2 = []
    for qi in range(10):
        for k in (32, 1000, 8192):
            found = await coarse_search.coarse_candidates(db, qmat[qi], agent_id=AGENT_ID, start=w0, k=k)
            v2.append(np.array_equal(np.asarray(found.positions, dtype=np.int64), cand_cache[qi][:k]))
    checks["V2_prefix"] = {"calls": len(v2), "pass": all(v2)}

    # V3 bridge: the production ranking at the provisional K' against this one.
    v3 = {"queries": 0, "top_seats_equal": 0, "near_tie_explained": 0, "max_abs_cos_diff": 0.0}
    k_prov = coarse_search.K_PROVISIONAL
    if k_prov in K_GRID:
        for qi in range(20):
            hits = await far_seats.ranked(db, qmat[qi], agent_id=AGENT_ID, project_id=None, channel="",
                                          source_id="", floor=floor)
            prod_ids = [h.id for h in hits[:TOP_M]]
            prod_cos = {h.id: h.cosine for h in hits}
            L = int(lists[f"k{k_prov}"]["len"][qi])
            mine = lists[f"k{k_prov}"]["ids"][qi, :L].tolist()
            mine_cos = lists[f"k{k_prov}"]["cos"][qi, :L]
            for rid, cv in zip(mine, mine_cos):
                if rid in prod_cos:
                    v3["max_abs_cos_diff"] = max(v3["max_abs_cos_diff"], abs(prod_cos[rid] - float(cv)))
            v3["queries"] += 1
            s = far_seats.SEATS
            if prod_ids[:s] == mine[:s]:
                v3["top_seats_equal"] += 1
            elif L > s and abs(float(mine_cos[s - 1]) - float(mine_cos[s])) < 1e-6:
                v3["near_tie_explained"] += 1
    v3["pass"] = v3["queries"] > 0 and v3["top_seats_equal"] + v3["near_tie_explained"] == v3["queries"]
    checks["V3_bridge"] = v3

    # V4 the live supplier returns what the index returns (the stores small enough to scan live).
    if args.store in ("s100k", "s237"):
        v4 = []
        absent = str(work / "absent.coarseindex")
        for qi in range(3):
            a = await coarse_search.coarse_candidates(db, qmat[qi], agent_id=AGENT_ID, start=w0, k=1000)
            b = await coarse_search.coarse_candidates(db, qmat[qi], agent_id=AGENT_ID, start=w0, k=1000,
                                                      path=absent)
            v4.append(b.source == "live" and a.rows() == b.rows())
        checks["V4_live_equals_index"] = {"queries": len(v4), "pass": all(v4)}
    await close_db()

    np.savez(work / f"{args.store}.{args.qset}.lists.npz",
             **{f"{name}__{field}": arr for name, d in lists.items() for field, arr in d.items()})
    (work / f"{args.store}.{args.qset}.checks.json").write_text(json.dumps(checks), encoding="utf-8")
    failed = [k for k, v in checks.items() if isinstance(v, dict) and v.get("pass") is False]
    print(f"{args.store}/{args.qset}: prepared; checks {'FAILED ' + str(failed) if failed else 'pass'}")
    if failed:
        raise SystemExit(1)


def _load_lists(work: Path, store: str, qset: str) -> dict:
    z = np.load(work / f"{store}.{qset}.lists.npz")
    out: dict = {}
    for key in z.files:
        name, field = key.split("__")
        out.setdefault(name, {})[field] = z[key]
    return out


# ---------------------------------------------------------------------------
# eligibility: which far records one real recall would let take a seat
# ---------------------------------------------------------------------------

async def _eligibility(args) -> None:
    work = Path(args.workdir)
    db_path = str(work / f"{args.store}.db")
    if args.regime == "production":
        # The confidence scorer writes on recall; this regime reads its own copy.
        copy = f"{db_path}.production.tmp"
        with sqlite3.connect(db_path) as src, sqlite3.connect(copy) as dst:
            src.backup(dst)
        for suffix in ("vecindex", "coarseindex"):
            os.symlink(f"{db_path}.memories.{suffix}", f"{copy}.memories.{suffix}")
        db_path = copy
    swab._set_env(db_path, window=WINDOW, regime=args.regime)
    os.environ["CPERSONA_FAR_SEATS_ENABLED"] = "true"

    from benchmark_trackb_lmeb import LookupEmbeddingClient

    import cpersona.memory_handlers as mh
    import cpersona.server as server_mod
    import cpersona.vector as vector_mod
    from cpersona import config, far_seats
    from cpersona.database import close_db, get_db

    if not config.FAR_SEATS_ENABLED or config.MAX_MEMORIES != WINDOW or config.VECTOR_REACH != 0:
        raise SystemExit("the far seats, the window or the reach did not take")
    if args.regime == "production" and not (mh.RECALL_MODE == "rsf" and config.CONFIDENCE_ENABLED):
        raise SystemExit("production regime did not take")
    if args.regime == "shipped" and (mh.RECALL_MODE != "rrf" or config.CONFIDENCE_ENABLED):
        raise SystemExit("shipped regime is not rrf with the confidence scorer off")

    qs = _queries(work, args.qset)
    lists = _load_lists(work, args.store, args.qset)
    emb = LookupEmbeddingClient()
    vector_mod._embedding_client = emb
    server_mod._embedding_client = emb
    cache = swab.CacheVectors(Path(args.cache), swab.DEFAULT_CACHE_LABEL)
    emb.preload([q["text"] for q in qs], np.stack(cache.get_many([q["text"] for q in qs])))

    state: dict = {}

    async def fake_ranked(db, query_vec, **kw):
        state["ranked_calls"] += 1
        state["floor_seen"] = kw.get("floor")
        return state["union"]

    async def capture_seat_rows(db, hits, **kw):
        state["eligible"] = [h.id for h in hits]
        return []

    far_seats.ranked = fake_ranked
    far_seats.seat_rows = capture_seat_rows

    db = await get_db()
    out = {"store": args.store, "qset": args.qset, "regime": args.regime,
           "recall_mode": mh.RECALL_MODE, "confidence": config.CONFIDENCE_ENABLED,
           "floor": mh._vector_arm_floor(AGENT_ID), "excluded": {}, "union_size": {}, "ms": []}
    names = list(lists)
    for qi, q in enumerate(qs):
        union: dict[int, tuple[float, int]] = {}
        for name in names:
            L = int(lists[name]["len"][qi])
            for rid, pos, c in zip(lists[name]["ids"][qi, :L].tolist(), lists[name]["pos"][qi, :L].tolist(),
                                   lists[name]["cos"][qi, :L].tolist()):
                union[rid] = (c, pos)
        hits = sorted((far_seats.FarHit(rid, c, pos) for rid, (c, pos) in union.items()),
                      key=lambda h: (-h.cosine, h.position))
        state.update(union=hits, eligible=None, ranked_calls=0)
        t = time.perf_counter()
        await mh.do_recall(agent_id=AGENT_ID, query=q["text"], limit=LIMIT)
        out["ms"].append(round((time.perf_counter() - t) * 1000, 1))
        if state["ranked_calls"] != 1:
            raise SystemExit(f"query {q['id']}: the far scan ran {state['ranked_calls']} times")
        # seat_rows is not called when no record is eligible.
        eligible = set(state["eligible"] or [])
        out["excluded"][q["id"]] = sorted(rid for rid in union if rid not in eligible)
        out["union_size"][q["id"]] = len(union)
    out["floor_seen"] = state.get("floor_seen")
    out["recall_count_sum"] = (await db.execute_fetchall(
        "SELECT COALESCE(SUM(recall_count), 0) FROM memories WHERE agent_id = ?", (AGENT_ID,)))[0][0]
    await close_db()
    if args.regime == "shipped" and out["recall_count_sum"]:
        # Every later step reads this database: the shipped regime must not write.
        raise SystemExit(f"the shipped regime bumped recall_count on {out['recall_count_sum']} rows")
    if args.regime == "production":
        for suffix in ("", "-wal", "-shm", ".memories.vecindex", ".memories.coarseindex"):
            Path(db_path + suffix).unlink(missing_ok=True)
    (work / f"{args.store}.{args.qset}.{args.regime}.eligibility.json").write_text(json.dumps(out),
                                                                                  encoding="utf-8")
    print(f"{args.store}/{args.qset}/{args.regime}: {len(qs)} recalls, "
          f"median {statistics.median(out['ms']):.0f} ms")


# ---------------------------------------------------------------------------
# score: per-seat agreement, the controls, and the decision
# ---------------------------------------------------------------------------

def _seats(ids: np.ndarray, length: int, excluded: set, seats: int) -> tuple[list[int], bool]:
    """The first `seats` eligible records of a list, and whether the list's kept
    depth ran out before they were found (a truncation the instrument forbids)."""
    out = []
    for rid in ids[:length].tolist():
        if rid not in excluded:
            out.append(rid)
            if len(out) == seats:
                return out, False
    return out, length == TOP_M


def _bootstrap(values: list[float], reps: int = 10_000) -> tuple[float, float]:
    if not values:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(SEED)
    a = np.asarray(values)
    means = a[rng.integers(0, len(a), size=(reps, len(a)))].mean(axis=1)
    return float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))


def _agreement(lists: dict, qs: list[dict], excluded: dict | None, seats: int) -> dict:
    out = {"truncated": 0}
    exact_n = lists["exact"]
    for k in K_GRID:
        per_q, misses = [], []
        for qi, q in enumerate(qs):
            ex = set(excluded[q["id"]]) if excluded is not None else set()
            e, t1 = _seats(exact_n["ids"][qi], int(exact_n["len"][qi]), ex, seats)
            c, t2 = _seats(lists[f"k{k}"]["ids"][qi], int(lists[f"k{k}"]["len"][qi]), ex, seats)
            out["truncated"] += int(t1) + int(t2)
            if e:
                hit = len(set(e) & set(c))
                per_q.append(hit / len(e))
                misses.append(len(e) - hit)
        lo, hi = _bootstrap(per_q)
        out[f"k{k}"] = {"agreement": float(np.mean(per_q)) if per_q else float("nan"),
                        "ci95": [lo, hi], "queries": len(per_q),
                        "misses_per_recall": float(np.mean(misses)) if misses else float("nan"),
                        "all_seats_kept": float(np.mean([m == 0 for m in misses])) if misses else float("nan")}
    fill = [len(_seats(exact_n["ids"][qi], int(exact_n["len"][qi]),
                       set(excluded[q["id"]]) if excluded is not None else set(), seats)[0])
            for qi, q in enumerate(qs)]
    out["exact_seats_filled"] = {str(s): fill.count(s) for s in range(seats + 1)}
    return out


def score(args) -> None:
    work = Path(args.workdir)
    from cpersona import far_seats
    seats = far_seats.SEATS
    report: dict = {"bound": BOUND, "grid": list(K_GRID), "seats": seats, "cells": {}, "checks": {},
                    "raw_top_seats": {}}
    for store in STORE_ROWS:
        for qset in QSETS:
            ck = work / f"{store}.{qset}.checks.json"
            if not ck.exists():
                continue
            report["checks"][f"{store}/{qset}"] = json.loads(ck.read_text(encoding="utf-8"))
            lists = _load_lists(work, store, qset)
            qs = _queries(work, qset)
            report["raw_top_seats"][f"{store}/{qset}"] = _agreement(lists, qs, None, seats)
            for regime in REGIMES:
                ef = work / f"{store}.{qset}.{regime}.eligibility.json"
                if not ef.exists():
                    continue
                el = json.loads(ef.read_text(encoding="utf-8"))
                cell = _agreement(lists, qs, el["excluded"], seats)
                cell["excluded_per_query_median"] = statistics.median(len(v) for v in el["excluded"].values())
                cell["recall_ms_median"] = statistics.median(el["ms"])
                report["cells"][f"{store}/{qset}/{regime}"] = cell

    # Validity: every check passed, and no list ran out before its seats were found.
    # Not named "invalid": that key is reserved for runs kept under an .INVALID- name
    # (tests/test_structural_gates.py), and this report is not one.
    invalid = [name for name, c in report["checks"].items()
               for key, v in c.items() if isinstance(v, dict) and v.get("pass") is False]
    invalid += [f"{name}: truncated {c['truncated']}" for name, c in report["cells"].items() if c["truncated"]]
    missing = [f"{s}/{q}/{r}" for s, q, r in DECISION_CELLS if f"{s}/{q}/{r}" not in report["cells"]]
    report["control_failures"] = invalid
    report["missing_cells"] = missing
    decision = None
    if not invalid and not missing:
        for k in K_GRID:
            if all(report["cells"][f"{s}/{q}/{r}"][f"k{k}"]["agreement"] >= BOUND for s, q, r in DECISION_CELLS):
                decision = k
                break
    report["decision"] = {"k": decision,
                          "status": ("invalid" if invalid else "incomplete" if missing
                                     else "decided" if decision else "not met within the grid")}
    text = json.dumps(report, indent=1)
    if args.json:
        Path(args.json).write_text(text + "\n", encoding="utf-8")
    print(_table(report))


def _table(report: dict) -> str:
    lines = ["cell | " + " | ".join(f"K'={k}" for k in K_GRID)]
    for name, cell in sorted(report["cells"].items()):
        lines.append(name + " | " + " | ".join(f"{cell[f'k{k}']['agreement']:.4f}" for k in K_GRID))
    lines.append(f"decision: {report['decision']}")
    if report["control_failures"]:
        lines.append(f"INVALID: {report['invalid']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# cost: latency, bytes, peak memory — reported, not decided on
# ---------------------------------------------------------------------------

async def _cost(args) -> None:
    work = Path(args.workdir)
    db_path = work / f"{args.store}.db"
    swab._set_env(str(db_path), window=WINDOW)
    os.environ.pop("CPERSONA_FAR_SEATS_ENABLED", None)
    import cpersona.memory_handlers as mh
    from cpersona import coarse_search, far_seats, vector, vector_index
    from cpersona.database import close_db, get_db
    from cpersona.isolation import isolation_where

    vidx = vector_index.load_index("memories")
    w0, n, dim = far_seats.scan_start(), vidx.count, vidx.dim
    floor = mh._vector_arm_floor(AGENT_ID)
    qs = _queries(work, "q1")[:COST_WARMUP + COST_QUERIES]
    cache = swab.CacheVectors(Path(args.cache), swab.DEFAULT_CACHE_LABEL)
    qmat = np.stack(cache.get_many([q["text"] for q in qs])).astype(np.float32)
    ks = sorted({int(k) for k in args.k.split(",")})
    db = await get_db()

    async def timed(fn, queries) -> float:
        samples = []
        for qi in queries:
            t = time.perf_counter()
            await fn(qmat[qi])
            samples.append((time.perf_counter() - t) * 1000)
        return statistics.median(samples[COST_WARMUP:]) if len(samples) > COST_WARMUP else statistics.median(samples)

    every = range(len(qs))
    out = {"store": args.store, "rows": n, "far_rows": max(0, n - w0), "dim": dim, "ks": {},
           "machine": os.uname().machine, "queries": COST_QUERIES}

    async def exact_sqlite(q):
        iso = isolation_where(agent_id=AGENT_ID, project_id=None, channel="")
        best: list = []
        async with db.execute(
            f"SELECT id, embedding FROM memories WHERE {iso.clause} AND embedding IS NOT NULL "
            "ORDER BY created_at DESC, id ASC LIMIT -1 OFFSET ?", (*iso.params, w0)) as cur:
            while True:
                rows = await cur.fetchmany(512)
                if not rows:
                    break
                c = vector._cosine_batch(q, dim, [r[1] for r in rows if len(r[1]) == dim * 4])
                best.extend(float(x) for x in c[c >= floor])
                best = sorted(best, reverse=True)[:far_seats.SEATS]
        return best

    async def exact_index(q):
        c = vector._cosine_matrix(q, vidx.embeddings[w0:n])
        if len(c) > far_seats.SEATS:
            return np.partition(c, len(c) - far_seats.SEATS)[-far_seats.SEATS:]
        return c

    out["exact_sqlite_ms"] = await timed(exact_sqlite, every)
    out["exact_index_ms"] = await timed(exact_index, every)
    absent = str(work / "absent.coarseindex")
    live_queries = every if out["far_rows"] <= 250_000 else range(COST_WARMUP + 3)
    for k in ks:
        coarse_search.K_PROVISIONAL = k

        async def ranked(q):
            return await far_seats.ranked(db, q, agent_id=AGENT_ID, project_id=None, channel="",
                                          source_id="", floor=floor)

        async def supplier(q, k=k):
            return await coarse_search.coarse_candidates(db, q, agent_id=AGENT_ID, start=w0, k=k)

        async def live(q, k=k):
            return await coarse_search.coarse_candidates(db, q, agent_id=AGENT_ID, start=w0, k=k, path=absent)

        row = {"ranked_ms": await timed(ranked, every), "supplier_index_ms": await timed(supplier, every),
               "supplier_live_ms": await timed(live, live_queries),
               "supplier_live_queries": max(1, len(live_queries) - COST_WARMUP),
               "hamming_bytes": out["far_rows"] * ((dim + 7) // 8),
               "rerank_bytes": min(k, out["far_rows"]) * dim * 4}
        tracemalloc.start()
        await ranked(qmat[0])
        row["ranked_peak_bytes"] = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        out["ks"][str(k)] = row
    await close_db()
    idx = work / f"{args.store}.index.json"
    if idx.exists():
        out["index"] = json.loads(idx.read_text(encoding="utf-8"))
    (work / f"{args.store}.cost.json").write_text(json.dumps(out), encoding="utf-8")
    print(f"{args.store}: cost {json.dumps(out)}")


# ---------------------------------------------------------------------------
# run: every step in order, each skipped when its output exists
# ---------------------------------------------------------------------------

def _step(args, *argv: str) -> None:
    cmd = [sys.executable, str(Path(__file__).resolve()), *argv, "--workdir", args.workdir,
           "--lmeb", args.lmeb, "--cache", args.cache]
    print("$", " ".join(argv), flush=True)
    subprocess.run(cmd, check=True)


def run(args) -> None:
    work = Path(args.workdir)
    work.mkdir(parents=True, exist_ok=True)
    if not (work / "plan.json").exists():
        _step(args, "plan")
    for store in list(STORE_ROWS) + list(COST_ROWS):
        if not (work / f"{store}.build.json").exists():
            _step(args, "build-store", "--store", store)
        if not (work / f"{store}.index.json").exists():
            _step(args, "index", "--store", store)
    for store in STORE_ROWS:
        for qset in QSETS:
            if qset == "q2" and store != "s1m":
                continue
            if not (work / f"{store}.{qset}.checks.json").exists():
                _step(args, "prepare", "--store", store, "--qset", qset)
            for regime in REGIMES:
                if not (work / f"{store}.{qset}.{regime}.eligibility.json").exists():
                    _step(args, "eligibility", "--store", store, "--qset", qset, "--regime", regime)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "build-store", "index", "prepare", "eligibility", "score", "cost", "run"):
        p = sub.add_parser(name)
        p.add_argument("--workdir", required=True)
        p.add_argument("--lmeb", default=str(swab.DEFAULT_LMEB))
        p.add_argument("--cache", default=str(swab.DEFAULT_CACHE))
        if name in ("build-store", "index", "prepare", "eligibility", "cost"):
            p.add_argument("--store", required=True, choices=list(STORE_ROWS) + list(COST_ROWS))
        if name in ("prepare", "eligibility"):
            p.add_argument("--qset", required=True, choices=QSETS)
        if name == "eligibility":
            p.add_argument("--regime", required=True, choices=REGIMES)
        if name == "score":
            p.add_argument("--json")
        if name == "cost":
            p.add_argument("--k", default="1000")
    args = ap.parse_args()
    if args.cmd == "plan":
        plan(args)
    elif args.cmd == "build-store":
        asyncio.run(_build_store(args))
    elif args.cmd == "index":
        asyncio.run(_index(args))
    elif args.cmd == "prepare":
        asyncio.run(_prepare(args))
    elif args.cmd == "eligibility":
        asyncio.run(_eligibility(args))
    elif args.cmd == "score":
        score(args)
    elif args.cmd == "cost":
        asyncio.run(_cost(args))
    elif args.cmd == "run":
        run(args)


if __name__ == "__main__":
    main()
