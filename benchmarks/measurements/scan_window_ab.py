"""Scan-window default A/B — does a wider vector window pay for itself?

Pre-registrations: `prereg-scan-window-default-ab.md` and, on the same
instrument, `prereg-scan-window-reach-ab.md`, both next to this file. Read them
first; this module only executes what those documents fixed. The reach
measurement changes no part of the instrument — same seed, same scene layout,
same strata, same metrics — it adds arms that also set `CPERSONA_VECTOR_REACH`,
two controls and one exploratory reading.

`CPERSONA_MAX_MEMORIES` is how many of the newest rows the vector arm ranks per
recall. It ships at 10,000, which on a six-figure corpus leaves most of the
corpus invisible to that arm. Raising it is not a pure relaxation: a scan that
ranks more rows hands a larger candidate pool to fusion, to the quality gate
and to autocut, so the same change that reaches further back also admits more
of everything else.

Four things this harness does differently from `benchmark_trackb_lmeb.py`,
each for a reason:

1. **The store order is authored, in scene blocks.** LongMemEval is 500 scenes
   of ~475 sessions with exactly one query each, and a query's answer always
   lies in its own scene. Stored in dataset-file order, the shallowest answer
   sits inside a 10,000-row window for 21 of the 500 queries, so a plain A/B
   would have no power on the one stratum where a wider window can *hurt*.
   Here each scene is placed as a contiguous block at a chosen depth: `near`
   scenes inside the narrow window, `far` scenes below it. A query's hardest
   competitors are its own scene-mates, and keeping the scene whole is what
   makes the arms differ in the rest of the corpus rather than in whether the
   competitors were there at all.

2. **The narrow window only holds 20 scenes, so the cohort rotates.** 10,000
   rows is 4% of this corpus. One build can measure 20 near queries; the run
   repeats the build with disjoint cohorts and pools the paired results.

3. **`created_at` is stamped monotonically at insert.** The column's default
   has one-second resolution; a bulk store puts thousands of rows on one value
   and the window boundary lands inside a tie block, which is not reproducible
   between arms.

4. **No model and no accelerator.** Vectors come from the Track A/B embedding
   disk cache, so nothing is encoded; the vector arm is the shipped SQLite
   scan, which is what a default install actually runs.

All arms of a rotation query one database file. In this regime the recall path
writes nothing (the `recall_count` bookkeeping is gated on the confidence
scorer, which ships off — measured, and re-checked at the end of every arm),
so "identical corpus" is a property of the file rather than of a rebuild
trusted to be deterministic.

Usage (from the repo root):

    uv run python benchmarks/measurements/scan_window_ab.py run \\
        --workdir /tmp/window-ab --json results.json

The exploratory matrix (`EXPLORATORY_SPEC`) is a second `run` with its own
`--spec`, fewer rotations and its own `--workdir` — the builds and the arm
files belong to the matrix that produced them, and a shared directory would
let one matrix answer for the other:

    uv run python benchmarks/measurements/scan_window_ab.py run \\
        --workdir /tmp/window-ab-exploratory --rotations 6 \\
        --spec <EXPLORATORY_SPEC — the constant of that name below> \\
        --json results-exploratory.json

Sub-commands (`build` / `arm` / `score`) exist so a long run can be resumed
without re-storing 237,655 rows.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import sqlite3
import struct
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[1]
sys.path.insert(0, str(_REPO / "benchmarks"))
sys.path.insert(0, str(_REPO))

AGENT_ID = "window-ab"

# The dataset the pre-registration names, and the cache that covers it. Both
# were verified before the prereg was written: 237,655/237,655 documents and
# 500/500 queries are present in the cache under this label.
DEFAULT_TASK = "Dialogue/LongMemEval"
DEFAULT_LMEB = Path(os.environ.get("LMEB_DIR", str(Path.home() / "lmeb")))
DEFAULT_CACHE = DEFAULT_LMEB / "embcache_bgem3_p1" / "embcache.sqlite3"
DEFAULT_CACHE_LABEL = "BAAI/bge-m3"

# Depth = distance from the newest row (0 = newest).
NEAR_MAX_DEPTH = 10_000       # the shipped window: near scenes live inside it
FAR_LO_DEPTH = 20_000         # far scenes start here — outside the narrow window
WIDE_WINDOW = 200_000         # the candidate default
NARROW_WINDOW = 10_000        # what ships today

# Timestamps are authored, not observed: one second per row from a fixed base.
STAMP_BASE = datetime(2020, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Embedding cache (read-only)
# ---------------------------------------------------------------------------

class CacheVectors:
    """sha256(label + '\\0' + text) -> float32 vector, from the Track A/B cache.

    Same key derivation as ``budget_batching._EmbeddingDiskCache``. A miss is
    fatal rather than an on-the-fly encode: this harness exists to compare two
    windows over one corpus, and a partially-encoded corpus would differ
    between arms for a reason that is not the window.
    """

    def __init__(self, path: Path, label: str):
        if not path.exists():
            raise SystemExit(f"embedding cache not found: {path}")
        self._db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self._label = label

    def _key(self, text: str) -> bytes:
        return hashlib.sha256((self._label + "\x00" + text).encode("utf-8")).digest()

    def get_many(self, texts: list[str]) -> list[np.ndarray]:
        keys = [self._key(t) for t in texts]
        found: dict[bytes, np.ndarray] = {}
        CHUNK = 500
        for i in range(0, len(keys), CHUNK):
            chunk = keys[i:i + CHUNK]
            ph = ",".join("?" * len(chunk))
            for k, dim, v in self._db.execute(
                f"SELECT k, dim, v FROM emb WHERE k IN ({ph})", chunk
            ):
                found[bytes(k)] = np.frombuffer(v, dtype=np.float32, count=dim)
        out = []
        for t, k in zip(texts, keys):
            v = found.get(k)
            if v is None:
                raise SystemExit(
                    "embedding cache miss — the corpus is not fully cached, so the "
                    f"arms would not rank the same rows. First miss: {t[:80]!r}"
                )
            out.append(v)
        return out


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------

def _load_jsonl(path: Path) -> list[dict]:
    items = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def _load_qrels(path: Path) -> dict[str, dict[str, int]]:
    qrels: dict[str, dict[str, int]] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 3:
                continue
            qrels.setdefault(parts[0], {})[parts[1]] = int(parts[2])
    return qrels


def _doc_text(doc: dict) -> str:
    """The text the store harness indexes — title and body, exactly as
    `benchmark_trackb_lmeb.store_corpus` joins them, so the cache keys match."""
    return (doc.get("title", "") + " " + doc.get("text", "")).strip()


def _scene_of(ident: str) -> str:
    """`scene_12_session_7` / `scene_12_q_0` -> `scene_12`."""
    parts = ident.split("_")
    if len(parts) < 2:
        raise SystemExit(f"identifier {ident!r} has no scene prefix")
    return "_".join(parts[:2])


def load_task(task_dir: Path) -> tuple[list[dict], list[dict]]:
    """Return (corpus docs in file order, queries with their qrels)."""
    corpus = _load_jsonl(task_dir / "corpus.jsonl")
    queries = []
    for sub in sorted(p for p in task_dir.iterdir() if p.is_dir()):
        qrels = _load_qrels(sub / "qrels.tsv")
        for q in _load_jsonl(sub / "queries.jsonl"):
            qid = str(q["id"])
            queries.append({
                "id": qid,
                "subtask": sub.name,
                "text": q["text"],
                "qrels": qrels.get(qid, {}),
                "scene": _scene_of(qid),
            })
    return corpus, queries


# ---------------------------------------------------------------------------
# build: scene layout, cohorts, database
# ---------------------------------------------------------------------------

def plan_layout(corpus: list[dict], queries: list[dict], seed: int,
                rotation: int, near_max_depth: int, far_lo_depth: int) -> dict:
    """Scene blocks, a near cohort and a far cohort for this rotation.

    Scenes are shuffled once by the seed, so a rotation is a slice of a fixed
    order: the cohorts of different rotations are disjoint by construction, and
    which scene lands in which cohort cannot be redrawn after seeing an arm.
    """
    scene_docs: dict[str, list[str]] = {}
    for doc in corpus:
        scene_docs.setdefault(_scene_of(str(doc["id"])), []).append(str(doc["id"]))
    for docs in scene_docs.values():
        docs.sort()

    by_scene_query: dict[str, list[dict]] = {}
    for q in queries:
        gold = {d for d, s in q["qrels"].items() if s > 0}
        outside = gold - set(scene_docs.get(q["scene"], []))
        if outside:
            raise SystemExit(
                f"query {q['id']} has {len(outside)} relevant documents outside its "
                "own scene; the scene-block layout cannot keep them together"
            )
        by_scene_query.setdefault(q["scene"], []).append(q)

    scenes = sorted(scene_docs)
    rng = random.Random(seed)
    rng.shuffle(scenes)

    biggest = max(len(v) for v in scene_docs.values())
    cohort = near_max_depth // biggest          # whole scenes that fit in the window
    if cohort < 2:
        raise SystemExit("the narrow window does not hold two scenes")
    lo = rotation * 2 * cohort
    if lo + 2 * cohort > len(scenes):
        raise SystemExit(
            f"rotation {rotation} runs past the {len(scenes)} scenes "
            f"({len(scenes) // (2 * cohort)} rotations available)"
        )
    near_scenes = scenes[lo:lo + cohort]
    far_scenes = scenes[lo + cohort:lo + 2 * cohort]
    rest = [s for s in scenes if s not in set(near_scenes) | set(far_scenes)]

    # Oldest first. The newest slots go to the near cohort, the far cohort sits
    # just below `far_lo_depth`, and everything else fills what is left in a
    # seeded order.
    n = sum(len(v) for v in scene_docs.values())
    order: list[str] = []
    for scene in rest:
        order.extend(scene_docs[scene])
    far_block: list[str] = []
    for scene in far_scenes:
        far_block.extend(scene_docs[scene])
    near_block: list[str] = []
    for scene in near_scenes:
        near_block.extend(scene_docs[scene])

    # Insert the far block so that its shallowest row lands exactly at
    # `far_lo_depth`. With the near block occupying the newest positions, the
    # filler that must sit between them is `far_lo_depth - len(near_block)`
    # rows, so the far block goes that far from the end of the filler.
    cut = len(order) + len(near_block) - far_lo_depth
    if not 0 <= cut <= len(order):
        raise SystemExit(
            f"the far band at depth {far_lo_depth} does not fit this corpus "
            f"({len(order)} filler rows, {len(near_block)} in the near block)"
        )
    order = order[:cut] + far_block + order[cut:] + near_block
    if len(order) != n:
        raise SystemExit(f"layout produced {len(order)} rows for {n} documents")

    return {
        "order": order,
        "cohort_size": cohort,
        "near_scenes": near_scenes,
        "far_scenes": far_scenes,
        "queries": (
            [dict(q, stratum="near") for s in near_scenes for q in by_scene_query.get(s, [])]
            + [dict(q, stratum="far") for s in far_scenes for q in by_scene_query.get(s, [])]
        ),
    }


async def build(args: argparse.Namespace) -> None:
    task_dir = Path(args.lmeb) / "eval_data" / args.task
    corpus, queries = load_task(task_dir)
    by_doc = {str(d["id"]): d for d in corpus}

    layout = plan_layout(corpus, queries, args.seed, args.rotation,
                         args.near_max_depth, args.far_lo_depth)
    order = layout["order"]
    print(f"rotation {args.rotation}: {len(corpus)} docs, cohort {layout['cohort_size']} scenes, "
          f"{len(layout['queries'])} evaluated queries")

    db_path = Path(args.db)
    for p in (db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")):
        if p.exists():
            p.unlink()

    _set_env(str(db_path), window=NARROW_WINDOW)
    from cpersona.database import get_db, close_db

    db = await get_db()
    cache = CacheVectors(Path(args.cache), args.cache_label)

    t0 = time.time()
    BATCH = 500
    for start in range(0, len(order), BATCH):
        chunk = order[start:start + BATCH]
        texts = [_doc_text(by_doc[d]) for d in chunk]
        vecs = cache.get_many(texts)
        rows = []
        for k, (doc_id, text, vec) in enumerate(zip(chunk, texts, vecs)):
            created = (STAMP_BASE + timedelta(seconds=start + k)).strftime("%Y-%m-%d %H:%M:%S")
            blob = struct.pack(f"<{len(vec)}f", *vec.tolist())
            rows.append((AGENT_ID, doc_id, text, "{}", "2026-01-01T00:00:00Z", "{}", blob, created))
        await db.executemany(
            "INSERT OR IGNORE INTO memories "
            "(agent_id, msg_id, content, source, timestamp, metadata, embedding, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        await db.commit()

    # Depths are read back in the order the scan itself uses, not assumed from
    # the order that was written. Exact-duplicate content collapses under the
    # shipped UNIQUE index (one pair does, in this corpus), which shifts every
    # later row by one — small, but cheaper to measure than to argue about.
    rows = await db.execute_fetchall(
        "SELECT msg_id FROM memories WHERE agent_id = ? "
        "ORDER BY created_at DESC, id ASC", (AGENT_ID,))
    stored = [r[0] for r in rows]
    depth_of = {doc: i for i, doc in enumerate(stored)}
    collapsed = [d for d in order if d not in depth_of]
    print(f"stored {len(stored)}/{len(order)} rows in {time.time() - t0:.0f}s"
          + (f", {len(collapsed)} collapsed on the UNIQUE index" if collapsed else ""))
    await close_db()

    # The instrument's own claim, checked rather than assumed: every near query
    # has ALL of its answers inside the narrow window, and every far query has
    # none of them there. A stratum that does not hold is not a weaker reading,
    # it is a different experiment.
    out_queries = []
    for q in layout["queries"]:
        gold = [d for d, s in q["qrels"].items() if s > 0 and d in depth_of]
        if not gold:
            continue
        depths = [depth_of[d] for d in gold]
        if q["stratum"] == "near" and max(depths) >= NARROW_WINDOW:
            raise SystemExit(f"near query {q['id']} has an answer at depth {max(depths)}")
        if q["stratum"] == "far" and min(depths) < NARROW_WINDOW:
            raise SystemExit(f"far query {q['id']} has an answer at depth {min(depths)}")
        if q["stratum"] == "far" and max(depths) >= WIDE_WINDOW:
            raise SystemExit(f"far query {q['id']} answer at depth {max(depths)} is "
                             "outside the wide window too")
        out_queries.append({**q, "gold_depths": sorted(depths)})

    Path(args.plan).write_text(json.dumps({
        "task": args.task,
        "seed": args.seed,
        "rotation": args.rotation,
        "corpus_size": len(stored),
        "collapsed": len(collapsed),
        "bands": {"near_max_depth": args.near_max_depth, "far_lo_depth": args.far_lo_depth},
        "cohort_size": layout["cohort_size"],
        "counts": {s: sum(1 for q in out_queries if q["stratum"] == s) for s in ("near", "far")},
        "queries": out_queries,
    }, ensure_ascii=False), encoding="utf-8")
    print(f"plan written: {args.plan}")


# ---------------------------------------------------------------------------
# arm: one window, one recall pass
# ---------------------------------------------------------------------------

def _set_env(db_path: str, window: int, reach: int = 0) -> None:
    """Shipped defaults except for what a benchmark cannot have (no network).

    The truncation layers stay ON: they are the mechanisms a larger candidate
    pool is suspected of disturbing, so the usual "turn them off for a pure
    ranking number" convention would remove the effect being measured.
    """
    os.environ["CPERSONA_DB_PATH"] = db_path
    os.environ["CPERSONA_EMBEDDING_MODE"] = "http"
    os.environ["CPERSONA_EMBEDDING_URL"] = "http://localhost:0"
    os.environ["CPERSONA_VECTOR_SEARCH_MODE"] = "local"
    os.environ["CPERSONA_STORE_BLOB"] = "true"
    os.environ["CPERSONA_FTS_ENABLED"] = "true"
    os.environ["CPERSONA_TASK_QUEUE_ENABLED"] = "false"
    os.environ["CPERSONA_MAX_MEMORIES"] = str(window)
    # Pinned rather than defaulted-by-absence: the setting's off state is a
    # number (a reach at or below the window is an empty region), so an
    # inherited `CPERSONA_VECTOR_REACH` must be overwritten, not left to decide
    # an arm. 0 is the store path's value — `build` runs no recall.
    os.environ["CPERSONA_VECTOR_REACH"] = str(reach)
    # The recall regime is whatever ships, so it is *removed* from the
    # environment rather than pinned here: an inherited override (the Track B
    # launcher exports two of these) would otherwise ride into the run, and a
    # hardcoded "true" would keep claiming "shipped defaults" after the default
    # moved. What actually took effect is asserted and recorded in `arm`.
    for key in ("CPERSONA_AUTOCUT_ENABLED", "CPERSONA_FUSED_GATE_ENABLED",
                "CPERSONA_RECALL_MODE", "CPERSONA_CONFIDENCE_ENABLED",
                "CPERSONA_VECTOR_MIN_SIMILARITY"):
        os.environ.pop(key, None)


async def arm(args: argparse.Namespace) -> None:
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    _set_env(args.db, args.window, args.reach)

    import cpersona.config as config
    import cpersona.memory_handlers as mh
    import cpersona.server as server_mod
    import cpersona.vector as vector_mod
    from benchmark_trackb_lmeb import LookupEmbeddingClient

    if config.MAX_MEMORIES != args.window:
        raise SystemExit(
            f"scan window did not take: config says {config.MAX_MEMORIES}, "
            f"asked for {args.window}"
        )
    # Asked of the config the process actually imported, for the same reason the
    # window is: an arm whose setting did not take is a failed arm, not a data
    # point (`prereg-scan-window-reach-ab.md`, Instrument).
    if config.VECTOR_REACH != args.reach:
        raise SystemExit(
            f"vector reach did not take: config says {config.VECTOR_REACH}, "
            f"asked for {args.reach}"
        )
    regime = {
        "recall_mode": config.RECALL_MODE,
        "fused_gate": config.FUSED_GATE_ENABLED,
        "autocut": config.AUTOCUT_ENABLED,
        "confidence": config.CONFIDENCE_ENABLED,
        "min_similarity": config.VECTOR_MIN_SIMILARITY,
        "scan_window": config.MAX_MEMORIES,
        "reach": config.VECTOR_REACH,
    }

    emb = LookupEmbeddingClient()
    vector_mod._embedding_client = emb
    server_mod._embedding_client = emb

    cache = CacheVectors(Path(args.cache), args.cache_label)
    texts = [q["text"] for q in plan["queries"]]
    emb.preload(texts, np.stack(cache.get_many(texts)))

    # What each retriever handed the fusion, per query. The fused answer cannot
    # show this: near-list identity is a claim about the vector retriever's own
    # list, and "which list did this row come from" is a claim about the four
    # inputs. Wrapped in `memory_handlers`, where the fusion looks the names up,
    # rather than in `vector` — `_search_vector` is bound into that namespace at
    # import, so patching the source module would not be seen by the caller.
    tapped: dict[str, list[str]] = {}

    def _ids(rows: list[dict] | None) -> list[str]:
        """Retriever rows as the dataset document ids `ids` is written in.

        `do_recall` reports a row as its `msg_id`; episodes have none and are
        left out of `ids` for that reason, so they are left out here too.
        """
        return [r["msg_id"] for r in (rows or []) if r.get("msg_id")]

    def _tap_vector(fn):
        async def call(*a, **kw):
            near = await fn(*a, **kw)
            tapped["near"] = _ids(near)
            # The far list is an out-parameter: it is the caller's list, and it
            # is what the caller fuses, so it is read where the caller left it.
            tapped["far"] = _ids(kw.get("far_out"))
            return near
        return call

    def _tap(name: str, fn):
        async def call(*a, **kw):
            rows = await fn(*a, **kw)
            tapped[name] = _ids(rows)
            return rows
        return call

    mh._search_vector = _tap_vector(mh._search_vector)
    mh._search_episodes_fts = _tap("ep_fts", mh._search_episodes_fts)
    mh._search_memories_keyword = _tap("mem_kw", mh._search_memories_keyword)

    from cpersona.database import get_db, close_db
    db = await get_db()

    out = {"window": args.window, "reach": args.reach, "limit": args.limit,
           "label": args.label,
           "rotation": plan["rotation"], "corpus_size": plan["corpus_size"],
           "regime": regime, "results": {}}
    t0 = time.time()
    for q in plan["queries"]:
        tapped.clear()
        t = time.perf_counter()
        res = await mh.do_recall(agent_id=AGENT_ID, query=q["text"], limit=args.limit)
        ms = (time.perf_counter() - t) * 1000
        # do_recall returns most-relevant-last for context assembly; reverse for
        # a relevance-descending ranking.
        messages = list(reversed(res.get("messages", [])))
        out["results"][q["id"]] = {
            # Kept to 100 (the MCP cap) rather than to the response size: at
            # limit=10 a displaced answer and a lost answer look the same, and
            # the deeper pass is what tells them apart.
            "ids": [m["id"] for m in messages if m.get("id")][:100],
            "returned": len(messages),
            "ms": round(ms, 2),
            "gate_fallback": bool(res.get("gate_fallback")),
            # The four lists the fusion weighed, whole: each is at most `limit`
            # long, and a truncated one would answer "was this row a far-only
            # vote?" with a guess.
            "near": tapped.get("near", []),
            "far": tapped.get("far", []),
            "ep_fts": tapped.get("ep_fts", []),
            "mem_kw": tapped.get("mem_kw", []),
        }
    # The claim that arms may share one database: this regime writes nothing.
    # Checked after every arm rather than once, so a future version that starts
    # writing shows up as a number here instead of as an unexplained difference.
    bumped = (await db.execute_fetchall(
        "SELECT COALESCE(SUM(recall_count), 0) FROM memories WHERE agent_id = ?",
        (AGENT_ID,)))[0][0]
    out["recall_count_sum"] = bumped
    await close_db()
    if bumped:
        raise SystemExit(
            f"recall bumped recall_count on {bumped} rows: the arms no longer share "
            "an identical corpus. Give each arm its own copy of the database."
        )

    Path(args.out).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"  arm {args.label} (window {args.window}, reach {args.reach}): "
          f"{len(plan['queries'])} queries in {time.time() - t0:.0f}s")


# ---------------------------------------------------------------------------
# score
# ---------------------------------------------------------------------------

def _ndcg_at_k(rels: dict[str, int], ranked: list[str], k: int = 10) -> float:
    """One query. Computed by `benchmark_trackb_lmeb.compute_ndcg` itself, so
    this harness cannot drift away from the published Track B formula."""
    from benchmark_trackb_lmeb import compute_ndcg
    return compute_ndcg({"q": rels}, {"q": ranked}, k=k)


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(int(len(s) * p), len(s) - 1)]


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def score(groups: list[tuple[dict, list[dict]]]) -> dict:
    """`groups` is one (plan, arms) pair per rotation; results are pooled over
    rotations because the cohorts are disjoint by construction.

    Arms are compared within one `limit`: the first arm carrying a given limit
    is that limit's baseline. A delta across two response sizes would not be a
    window effect.
    """
    labels: list[str] = []
    for _, arms in groups:
        for a in arms:
            if a["label"] not in labels:
                labels.append(a["label"])
    limit_of = {a["label"]: a["limit"] for _, arms in groups for a in arms}
    base_of: dict[str, str] = {}
    for label in labels:
        base_of.setdefault(limit_of[label], label)

    reach_of = {a["label"]: a.get("reach", 0) for _, arms in groups for a in arms}

    acc: dict[tuple[str, str], dict[str, list]] = {}
    paired: dict[tuple[str, str], list[tuple[float, float]]] = {}
    disturb: dict[tuple[str, str], dict[str, list]] = {}
    control: dict[tuple[str, str], dict[str, int]] = {}
    far_only: dict[str, dict[str, int]] = {}

    for plan, arms in groups:
        by_label = {a["label"]: a for a in arms}
        for q in plan["queries"]:
            rels = {d: s for d, s in q["qrels"].items() if s > 0}
            if not rels:
                continue
            for stratum in (q["stratum"], "all"):
                for label in labels:
                    a = by_label.get(label)
                    r = a and a["results"].get(q["id"])
                    if not r:
                        continue
                    cell = acc.setdefault((label, stratum), {
                        "ndcg": [], "recall": [], "returned": [], "ms": [], "mrr": []})
                    cell["ndcg"].append(_ndcg_at_k(rels, r["ids"], 10))
                    top10 = set(r["ids"][:10])
                    cell["recall"].append(len(top10 & set(rels)) / len(rels))
                    # Reciprocal rank of the first relevant row anywhere in what
                    # was returned: "the answer fell out of the top ten" and
                    # "the answer is gone" are different failures.
                    rank = next((i + 1 for i, d in enumerate(r["ids"]) if d in rels), None)
                    cell["mrr"].append(1.0 / rank if rank else 0.0)
                    cell["returned"].append(r["returned"])
                    cell["ms"].append(r["ms"])
                for label in labels:
                    if label == base_of[limit_of[label]]:
                        continue
                    a = by_label.get(label)
                    base = by_label.get(base_of[limit_of[label]])
                    x = base and base["results"].get(q["id"])
                    y = a and a["results"].get(q["id"])
                    if not x or not y:
                        continue
                    nx, ny = _ndcg_at_k(rels, x["ids"], 10), _ndcg_at_k(rels, y["ids"], 10)
                    paired.setdefault((label, stratum), []).append((nx, ny))

                    # The two controls the reach pre-registration reads before
                    # anything else. Both are counted, not asserted: this
                    # function scores, and what a violated control means for the
                    # measurement is the report's decision, not the scorer's.
                    #
                    # near_list_identity — the design's "every existing list is
                    # untouched" claim. It is a claim about the vector arm's own
                    # list, so the fused answer cannot check it; arms that
                    # recorded no lists (an older arm file) are not counted.
                    c = control.setdefault((label, stratum), {
                        "near_n": 0, "near_differs": 0, "ids_identical": 0})
                    if "near" in x and "near" in y:
                        c["near_n"] += 1
                        c["near_differs"] += int(x["near"] != y["near"])
                    # ids_identical — how the off-is-identical and the replicate
                    # controls are read: both must be every query.
                    c["ids_identical"] += int(x["ids"] == y["ids"])

                    # Exploratory: where did the rows that displaced the answer
                    # come from? Only for a label that runs a far list, only on
                    # the near stratum, and only on the queries that LOST NDCG
                    # against the base — the reading exists to say whether a
                    # per-list weight is the next candidate, and a row that
                    # displaced nothing says nothing about that.
                    if reach_of.get(label, 0) > 0 and stratum == "near" and ny < nx - 1e-9:
                        f = far_only.setdefault(label, {
                            "near_losers": 0, "displacing_rows": 0, "far_only_rows": 0})
                        f["near_losers"] += 1
                        base_top = set(x["ids"][:10])
                        displacing = [d for d in y["ids"][:10] if d not in base_top]
                        f["displacing_rows"] += len(displacing)
                        far_ids = set(y.get("far", []))
                        voted_elsewhere = (set(y.get("near", [])) | set(y.get("ep_fts", []))
                                           | set(y.get("mem_kw", [])))
                        f["far_only_rows"] += sum(
                            1 for d in displacing if d in far_ids and d not in voted_elsewhere)

                    sx, sy = x["ids"][:10], y["ids"][:10]
                    d = disturb.setdefault((label, stratum), {
                        "set": [], "order": [], "jaccard": []})
                    d["set"].append(set(sx) != set(sy))
                    d["order"].append(sx != sy)
                    union = set(sx) | set(sy)
                    d["jaccard"].append(len(set(sx) & set(sy)) / len(union) if union else 1.0)

    windows = {a["label"]: a["window"] for _, arms in groups for a in arms}
    limits = {a["label"]: a["limit"] for _, arms in groups for a in arms}
    report = {
        "rotations": [p["rotation"] for p, _ in groups],
        "corpus_size": groups[0][0]["corpus_size"],
        "seed": groups[0][0]["seed"],
        "regime": groups[0][1][0]["regime"],
        "arms": [], "delta": [], "disturbance": [], "controls": [],
        "exploratory": {"far_only_votes": []},
    }
    for label in labels:
        for stratum in ("near", "far", "all"):
            cell = acc.get((label, stratum))
            if not cell:
                continue
            report["arms"].append({
                "label": label, "window": windows[label],
                "reach": reach_of.get(label, 0), "limit": limits[label],
                "stratum": stratum, "n": len(cell["ndcg"]),
                "ndcg@10": round(_mean(cell["ndcg"]), 2),
                "recall@10": round(_mean(cell["recall"]) * 100, 2),
                "mrr": round(_mean(cell["mrr"]), 4),
                "returned_mean": round(_mean(cell["returned"]), 2),
                "latency_p50_ms": round(_pct(cell["ms"], 0.5), 1),
                "latency_p95_ms": round(_pct(cell["ms"], 0.95), 1),
            })
    for (label, stratum), pairs in paired.items():
        deltas = [y - x for x, y in pairs]
        n = len(deltas)
        mean = _mean(deltas)
        sd = (sum((d - mean) ** 2 for d in deltas) / (n - 1)) ** 0.5 if n > 1 else 0.0
        report["delta"].append({
            "pair": f"{base_of[limit_of[label]]} -> {label}", "stratum": stratum, "n": n,
            "mean_delta_ndcg": round(mean, 2),
            "sem": round(sd / (n ** 0.5), 2) if n else 0.0,
            "worse": sum(1 for d in deltas if d < -1e-9),
            "better": sum(1 for d in deltas if d > 1e-9),
        })
    for (label, stratum), d in disturb.items():
        n = max(len(d["jaccard"]), 1)
        report["disturbance"].append({
            "pair": f"{base_of[limit_of[label]]} -> {label}", "stratum": stratum,
            "n": len(d["jaccard"]),
            "top10_set_changed_pct": round(sum(d["set"]) / n * 100, 1),
            "top10_order_changed_pct": round(sum(d["order"]) / n * 100, 1),
            "mean_jaccard": round(_mean(d["jaccard"]), 3),
        })
    for (label, stratum), c in control.items():
        n = len(paired.get((label, stratum), []))
        report["controls"].append({
            "pair": f"{base_of[limit_of[label]]} -> {label}", "stratum": stratum,
            "n": n,
            "near_list_compared": c["near_n"],
            "near_list_differs": c["near_differs"],
            "ids_identical": c["ids_identical"],
            "ids_identical_pct": round(c["ids_identical"] / n * 100, 1) if n else 0.0,
        })
    for label, f in far_only.items():
        report["exploratory"]["far_only_votes"].append({
            "label": label, "reach": reach_of.get(label, 0), "stratum": "near",
            **f,
        })
    return report


def print_report(report: dict) -> None:
    print(f"\ncorpus {report['corpus_size']} rows, seed {report['seed']}, "
          f"rotations {report['rotations']}")
    print(f"regime {report['regime']}\n")
    hdr = (f"{'arm':<8}{'window':>9}{'reach':>9}{'limit':>6}{'stratum':>9}{'n':>5}"
           f"{'ndcg@10':>9}{'rec@10':>8}{'mrr':>7}{'ret':>7}{'p50ms':>9}{'p95ms':>9}")
    print(hdr)
    print("-" * len(hdr))
    for a in report["arms"]:
        print(f"{a['label']:<8}{a['window']:>9}{a.get('reach', 0):>9}{a['limit']:>6}"
              f"{a['stratum']:>9}{a['n']:>5}"
              f"{a['ndcg@10']:>9.2f}{a['recall@10']:>8.2f}{a['mrr']:>7.3f}"
              f"{a['returned_mean']:>7.2f}"
              f"{a['latency_p50_ms']:>9.1f}{a['latency_p95_ms']:>9.1f}")
    print()
    for d in report["delta"]:
        print(f"{d['pair']:<14}{d['stratum']:>6}  n={d['n']:<5} "
              f"Δndcg@10={d['mean_delta_ndcg']:+7.2f} ± {d['sem']:.2f} (sem)   "
              f"better={d['better']:<4} worse={d['worse']}")
    print()
    for d in report["disturbance"]:
        print(f"{d['pair']:<14}{d['stratum']:>6}  set±{d['top10_set_changed_pct']:>5.1f}%  "
              f"order±{d['top10_order_changed_pct']:>5.1f}%  jaccard={d['mean_jaccard']:.3f}")
    if report.get("controls"):
        print("\ncontrols  (near lists that differ: 0 expected where the two arms share a "
              "window; identical fused ids: 100% expected for a replicate and for a reach "
              "equal to the window)")
        for c in report["controls"]:
            print(f"{c['pair']:<14}{c['stratum']:>6}  n={c['n']:<5} "
                  f"near_list_differs={c['near_list_differs']}/{c['near_list_compared']}   "
                  f"ids_identical={c['ids_identical']}/{c['n']} "
                  f"({c['ids_identical_pct']:.1f}%)")
    votes = report.get("exploratory", {}).get("far_only_votes") or []
    if votes:
        print("\nEXPLORATORY — not part of the decision rule")
        print("far-only votes: among the near-stratum queries that lost NDCG@10 against "
              "their base arm,\nthe top-10 rows that were not in the base's top ten, and "
              "how many of those carried\na far-list vote and no other.")
        for v in votes:
            print(f"  {v['label']:<8} reach={v['reach']:<8} near_losers={v['near_losers']:<5} "
                  f"displacing_rows={v['displacing_rows']:<6} "
                  f"far_only_rows={v['far_only_rows']}")


# ---------------------------------------------------------------------------
# run: the pre-registered matrix
# ---------------------------------------------------------------------------

# The pre-registered matrix of `prereg-scan-window-reach-ab.md`, as an arm
# spec: label:window:reach:limit.
#
# "A-rep" repeats the shipped setting as the noise band — with calibration off
# and one database, the two A runs must agree exactly. "A0" is the
# off-is-identical control: a reach EQUAL to the window is the setting's off
# state written as a number, and it must not run a far scan that returns
# nothing and moves a tie somewhere. "S" is the separation itself, reaching
# where the wide-window arm of the previous measurement reached.
DEFAULT_SPEC = (
    f"A:{NARROW_WINDOW}:0:10,"
    f"A-rep:{NARROW_WINDOW}:0:10,"
    f"A0:{NARROW_WINDOW}:{NARROW_WINDOW}:10,"
    f"S:{NARROW_WINDOW}:{WIDE_WINDOW}:10"
)

# The exploratory matrix of the same pre-registration — a smaller reach, two
# reaches past the end of the corpus, and the pair at the MCP cap. Run as a
# separate `run` with its own `--workdir` and fewer rotations; none of it may
# move the decision rule.
EXPLORATORY_SPEC = (
    f"A:{NARROW_WINDOW}:0:10,"
    f"S50:{NARROW_WINDOW}:50000:10,"
    f"D:{NARROW_WINDOW}:300000:10,"
    f"E:{NARROW_WINDOW}:500000:10,"
    f"A100:{NARROW_WINDOW}:0:100,"
    f"S100:{NARROW_WINDOW}:{WIDE_WINDOW}:100"
)


def parse_spec(spec: str) -> list[tuple[str, int, int, int]]:
    """`label:window:reach:limit`, or `label:window:limit` with the reach off.

    The three-field form is the spec the window measurement was run with, and
    it still means what it meant: reach 0, no far list. Keeping it parseable is
    what lets that measurement's spec be replayed against this harness.
    """
    arms = []
    for part in spec.split(","):
        fields = part.split(":")
        if len(fields) == 3:
            label, window, limit = fields
            reach = "0"
        elif len(fields) == 4:
            label, window, reach, limit = fields
        else:
            raise SystemExit(
                f"arm spec {part!r} is not label:window:reach:limit "
                "(nor the older label:window:limit)"
            )
        arms.append((label, int(window), int(reach), int(limit)))
    return arms


def _arm_path(work: Path, rot: int, label: str, reach: int, limit: int) -> Path:
    """The reach is in the filename, not only in the label.

    Two arms of one matrix can share a label and a limit and differ in the
    reach — and a resumed run reuses whatever file is already there, so a
    filename that cannot tell them apart would silently answer one arm with the
    other's results.
    """
    return work / f"arm-r{rot}-{label}-reach{reach}-limit{limit}.json"


def run(args: argparse.Namespace) -> None:
    work = Path(args.workdir)
    work.mkdir(parents=True, exist_ok=True)
    spec = parse_spec(args.spec)
    (work / "manifest.json").write_text(json.dumps(
        {"spec": args.spec, "rotations": args.rotations, "seed": args.seed}),
        encoding="utf-8")
    groups = []
    for rot in range(args.rotations):
        plan = work / f"plan-r{rot}.json"
        db = work / "corpus.db"
        outs = [_arm_path(work, rot, label, reach, limit)
                for label, _, reach, limit in spec]
        if args.rerun or not all(o.exists() for o in outs):
            _self(["build", "--db", str(db), "--plan", str(plan), "--seed", str(args.seed),
                   "--rotation", str(rot), "--task", args.task, "--lmeb", args.lmeb,
                   "--cache", args.cache, "--cache-label", args.cache_label])
            for (label, window, reach, limit), out in zip(spec, outs):
                if out.exists() and not args.rerun:
                    continue
                _self(["arm", "--db", str(db), "--plan", str(plan), "--window", str(window),
                       "--reach", str(reach),
                       "--limit", str(limit), "--label", label, "--out", str(out),
                       "--cache", args.cache, "--cache-label", args.cache_label])
        groups.append((json.loads(plan.read_text(encoding="utf-8")),
                       [json.loads(o.read_text(encoding="utf-8")) for o in outs]))

    report = score(groups)
    print_report(report)
    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        print(f"\nreport written: {args.json}")


def _self(argv: list[str]) -> None:
    """Run one sub-command in a fresh process.

    `cpersona.config` reads its environment at import, so a window change needs
    a new interpreter — patching the module afterwards would leave the value
    the rest of the package already captured.
    """
    cmd = [sys.executable, str(Path(__file__).resolve())] + argv
    subprocess.run(cmd, check=True, cwd=str(_REPO))


# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--cache", default=str(DEFAULT_CACHE))
        p.add_argument("--cache-label", default=DEFAULT_CACHE_LABEL)
        p.add_argument("--task", default=DEFAULT_TASK)
        p.add_argument("--lmeb", default=str(DEFAULT_LMEB))
        p.add_argument("--seed", type=int, default=20260903)

    b = sub.add_parser("build")
    b.add_argument("--db", required=True)
    b.add_argument("--plan", required=True)
    b.add_argument("--rotation", type=int, default=0)
    b.add_argument("--near-max-depth", type=int, default=NEAR_MAX_DEPTH)
    b.add_argument("--far-lo-depth", type=int, default=FAR_LO_DEPTH)
    common(b)

    a = sub.add_parser("arm")
    a.add_argument("--db", required=True)
    a.add_argument("--plan", required=True)
    a.add_argument("--window", type=int, required=True)
    a.add_argument("--reach", type=int, default=0)
    a.add_argument("--limit", type=int, default=10)
    a.add_argument("--label", default="arm")
    a.add_argument("--out", required=True)
    common(a)

    s = sub.add_parser("score")
    s.add_argument("--workdir", required=True)
    s.add_argument("--rotations", type=int)
    s.add_argument("--spec")
    s.add_argument("--json")

    r = sub.add_parser("run")
    r.add_argument("--workdir", required=True)
    r.add_argument("--rotations", type=int, default=12)
    r.add_argument("--spec", default=DEFAULT_SPEC)
    r.add_argument("--json")
    r.add_argument("--rerun", action="store_true")
    common(r)

    args = ap.parse_args()
    if args.cmd == "build":
        asyncio.run(build(args))
    elif args.cmd == "arm":
        asyncio.run(arm(args))
    elif args.cmd == "score":
        work = Path(args.workdir)
        manifest = json.loads((work / "manifest.json").read_text(encoding="utf-8")) \
            if (work / "manifest.json").exists() else {}
        spec = parse_spec(args.spec or manifest.get("spec", DEFAULT_SPEC))
        rotations = args.rotations or manifest.get("rotations", 1)
        groups = []
        for rot in range(rotations):
            plan = json.loads((work / f"plan-r{rot}.json").read_text(encoding="utf-8"))
            arms = [json.loads(
                        _arm_path(work, rot, label, reach, limit).read_text(encoding="utf-8"))
                    for label, _, reach, limit in spec]
            groups.append((plan, arms))
        report = score(groups)
        print_report(report)
        if args.json:
            Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    else:
        run(args)


if __name__ == "__main__":
    main()
