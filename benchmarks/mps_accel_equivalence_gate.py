"""Equivalence gate for mps_accel.py — proves behavior invariance.

Stores a real LMEB task corpus through the benchmark harness, calibrates
ONCE (calibration samples ORDER BY RANDOM(), so cross-run comparisons are
invalid — everything here shares one in-process threshold state), then for
EVERY query runs the original cpersona _search_vector and the accelerated
one side by side on the same DB and compares:

  1. _search_vector level: result _rid sequences must be identical;
     cosines must match (numpy backend: exactly / torch backend: <= tol).
  2. do_recall level (integration): full recall message-id lists with the
     patch bound vs unbound, on a per-subtask sample of queries.

Exit code 0 = gate passed for all requested backends.

This is a standalone script, not a pytest test — it mutates os.environ at
import time (the cpersona modules read their config on import), so it must
never carry a test_*.py name that pytest would collect: collecting it leaks
the env overrides into the rest of the suite.

Usage:
  LMEB_DIR=~/lmeb \
  python benchmarks/mps_accel_equivalence_gate.py --tasks LoCoMo --device mps \
      --model_path sentence-transformers/all-MiniLM-L6-v2 --backends numpy,torch

(CPERSONA_REPO defaults to the repo containing this script; set it to gate
another checkout, e.g. a bisect worktree.)
"""

import argparse
import asyncio
import logging
import os
import sys
import tempfile
from collections import defaultdict

# Env must be configured before cpersona import (mirrors benchmark_trackb_lmeb).
_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False, prefix="accelgate_")
_TMP_DB_PATH = _TMP_DB.name
_TMP_DB.close()
os.environ["CPERSONA_DB_PATH"] = _TMP_DB_PATH
os.environ["CPERSONA_EMBEDDING_MODE"] = "http"
os.environ["CPERSONA_EMBEDDING_URL"] = "http://localhost:0"
os.environ["CPERSONA_VECTOR_SEARCH_MODE"] = "local"
os.environ["CPERSONA_STORE_BLOB"] = "true"
os.environ["CPERSONA_FTS_ENABLED"] = "true"
os.environ["CPERSONA_TASK_QUEUE_ENABLED"] = "false"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from benchmark_trackb_lmeb import (  # noqa: E402
    AGENT_ID, EVAL_DATA, TASK_MAP, LookupEmbeddingClient,
    discover_task_structure, load_jsonl, store_corpus,
)
from mps_accel import FastVectorSearch  # noqa: E402

logging.basicConfig(format="%(levelname)s|%(asctime)s|%(name)s: %(message)s",
                    datefmt="%Y/%m/%d %H:%M:%S", level=logging.INFO)
logger = logging.getLogger("accel_gate")

TORCH_TOL = 1e-5


def compare_results(ref, fast, tol, strict=False):
    """Return (ok, detail) comparing two _search_vector result lists.

    ``strict`` refuses even a swap between exactly-tied rows. The torch backend
    needs the lenient reading — it computes on a GPU and two rows that tie in one
    arithmetic need not tie in the other — but the contiguous index makes a
    stronger claim: it reads the same float32 bytes and calls the same matmul, so
    the scores are identical and the ORDER of equal scores is a promise, not an
    accident. Tolerating a tie swap there would hide exactly the defect the order
    work exists to prevent.
    """
    if len(ref) != len(fast):
        return False, f"length {len(fast)} != {len(ref)}"
    max_diff = 0.0
    for i, (r, f) in enumerate(zip(ref, fast)):
        if r["_rid"] != f["_rid"]:
            # Tolerate order swaps between exact ties only (never under strict).
            tie = not strict and abs(r["_cosine"] - f["_cosine"]) <= tol
            if not tie:
                return False, (f"rank {i}: rid {f['_rid']}({f['_cosine']:.7f}) != "
                               f"{r['_rid']}({r['_cosine']:.7f})")
        d = abs(r["_cosine"] - f["_cosine"])
        max_diff = max(max_diff, d)
        if d > tol:
            return False, f"rank {i}: cosine diff {d:.2e} > {tol}"
    return True, f"max_cosine_diff={max_diff:.2e}"


async def gate(args):
    # config reads env at import time — set before importing cpersona.
    os.environ["CPERSONA_MAX_MEMORIES"] = str(args.max_memories)

    import cpersona.memory_handlers as mh_mod
    import cpersona.server as server_mod
    import cpersona.vector as vector_mod

    for mod in (vector_mod, mh_mod):
        if hasattr(mod, "MAX_MEMORIES"):
            mod.MAX_MEMORIES = args.max_memories

    emb_client = LookupEmbeddingClient()
    vector_mod._embedding_client = emb_client
    server_mod._embedding_client = emb_client
    # v2.4.20+ package layout: get_db lives in cpersona.database and is NOT
    # re-exported by cpersona.server (close_db is). Same import-at-call-site
    # note as benchmark_trackb_lmeb.py.
    from cpersona.database import get_db

    db = await get_db()

    from sentence_transformers import SentenceTransformer
    st_model = SentenceTransformer(args.model_path, device=args.device)

    original = vector_mod._search_vector  # unpatched reference
    backends = [b.strip() for b in args.backends.split(",")]
    failures = []
    checked_queries = 0

    for task_name in [t.strip() for t in args.tasks.split(",")]:
        task_dir = os.path.join(EVAL_DATA, TASK_MAP[task_name])
        subtasks = discover_task_structure(task_dir)
        corpus_groups = defaultdict(list)
        for st in subtasks:
            corpus_groups[st["corpus"]].append(st)

        for corpus_path, group_subtasks in corpus_groups.items():
            await server_mod.do_delete_agent_data(AGENT_ID)
            corpus = load_jsonl(corpus_path)
            corpus_size = await store_corpus(server_mod, emb_client, st_model, corpus,
                                             batch_size=args.batch_size)
            cal = await server_mod.do_calibrate_threshold(AGENT_ID)
            logger.info("calibrated: %s -> %s", cal.get("old_threshold"), cal.get("new_threshold"))

            accels = {}
            for b in backends:
                accels[b] = FastVectorSearch(
                    server_mod, vector_mod, mh_mod, backend=b,
                    device=args.device if b == "torch" else "cpu",
                )
                await accels[b].preload(AGENT_ID)

            for st in group_subtasks:
                queries_data = load_jsonl(st["queries"])
                query_texts = [q["text"] for q in queries_data]
                q_embs = st_model.encode(query_texts, normalize_embeddings=True,
                                         show_progress_bar=False)
                emb_client.preload(query_texts, q_embs)

                for qi, q in enumerate(queries_data):
                    ref = await original(db, AGENT_ID, q["text"], corpus_size)
                    for b in backends:
                        fast = await accels[b].search_vector(db, AGENT_ID, q["text"], corpus_size)
                        tol = 0.0 if b == "numpy" else TORCH_TOL
                        ok, detail = compare_results(ref, fast, tol)
                        if not ok:
                            failures.append(f"{task_name}/{st['name']} q#{qi} [{b}]: {detail}")
                            logger.error("MISMATCH %s", failures[-1])
                    checked_queries += 1

                # Integration check: full do_recall with patch bound vs unbound.
                sample = queries_data[:args.recall_sample]
                for q in sample:
                    ref_recall = await server_mod.do_recall(
                        agent_id=AGENT_ID, query=q["text"], limit=corpus_size)
                    ref_ids = [m.get("id") for m in ref_recall.get("messages", [])]
                    for b in backends:
                        vector_mod._search_vector = accels[b].search_vector
                        mh_mod._search_vector = accels[b].search_vector
                        try:
                            fast_recall = await server_mod.do_recall(
                                agent_id=AGENT_ID, query=q["text"], limit=corpus_size)
                        finally:
                            vector_mod._search_vector = original
                            mh_mod._search_vector = original
                        fast_ids = [m.get("id") for m in fast_recall.get("messages", [])]
                        if fast_ids != ref_ids:
                            n_diff = sum(1 for a, c in zip(fast_ids, ref_ids) if a != c)
                            failures.append(
                                f"{task_name}/{st['name']} do_recall [{b}]: id list diverges "
                                f"({n_diff}/{len(ref_ids)} positions, len {len(fast_ids)} vs {len(ref_ids)})")
                            logger.error("MISMATCH %s", failures[-1])
                logger.info("  %s/%s: %d queries compared, do_recall sample=%d — "
                            "%s", task_name, st["name"], len(queries_data), len(sample),
                            "OK" if not failures else f"{len(failures)} failures so far")

    await server_mod.do_delete_agent_data(AGENT_ID)
    await server_mod.close_db()
    try:
        os.unlink(_TMP_DB_PATH)
    except OSError:
        pass

    print("\n" + "=" * 60)
    print(f"EQUIVALENCE GATE: {checked_queries} queries × backends {backends}")
    if failures:
        print(f"FAILED — {len(failures)} mismatches:")
        for f in failures[:20]:
            print("  " + f)
        return 1
    print("PASSED — accelerated _search_vector is behavior-identical "
          "(numpy: exact; torch: <= %.0e)" % TORCH_TOL)
    return 0


def _index_paths():
    from cpersona import vector_index

    base = vector_index.index_path("memories")
    return (base, base + ".tmp")


def _remove_index():
    for path in _index_paths():
        if os.path.exists(path):
            os.unlink(path)


async def _plant_ties(db, count):
    """Give the corpus rows that score EXACTLY equal, and return how many.

    Measured on EPBench: flipping the index's tie-break produced one mismatch in
    3,644 queries. Not because the order does not matter — because real text
    embedded by a real model almost never scores two rows identically, so the
    tie-break is barely reached. A gate that prints "ties included" on that
    evidence is describing its corpus, not its subject.

    These rows carry a duplicate embedding under distinct content (the UNIQUE
    index is on content, not on the vector), so every query scores them equal and
    their relative order is decided by nothing except the scan order the index
    has to reproduce.
    """
    if count <= 0:
        return 0
    rows = await db.execute_fetchall(
        "SELECT embedding FROM memories WHERE agent_id = ? AND embedding IS NOT NULL LIMIT 1",
        (AGENT_ID,),
    )
    if not rows:
        return 0
    blob = rows[0][0]
    await db.executemany(
        "INSERT OR IGNORE INTO memories (agent_id, msg_id, content, source, timestamp,"
        " metadata, embedding) VALUES (?, ?, ?, '{}', '2026-01-01T00:00:00Z', '{}', ?)",
        [(AGENT_ID, f"planted-tie-{i}", f"planted tie row {i}", blob) for i in range(count)],
    )
    await db.commit()
    return count

async def gate_sidecar(args):
    """Compare `_search_vector` against itself, with and without the index file.

    The contiguous index lives inside `_search_vector` rather than beside it, so
    the comparison is not "two functions" but "one function, two states of the
    disk". Two passes over the same queries, in that order, because building
    between them is what production does: an index is built once and reused, and
    deleting it per query would exercise a cache-thrash path nothing real takes.

    The claim is stronger than the accel backends': the index reads the same
    float32 bytes and reaches the same matmul, so scores are identical and the
    order of equal scores is a promise. Compared with `strict=True`.

    Every fallback in that path is silent by construction, so this counts the
    calls the index actually answered and fails if the number is zero. Without
    it, a gate that quietly fell back would compare the live scan against itself
    and report agreement — the most expensive way to learn nothing.
    """
    os.environ["CPERSONA_MAX_MEMORIES"] = str(args.max_memories)

    import cpersona.memory_handlers as mh_mod
    import cpersona.server as server_mod
    import cpersona.vector as vector_mod
    from cpersona import vector_index

    for mod in (vector_mod, mh_mod):
        if hasattr(mod, "MAX_MEMORIES"):
            mod.MAX_MEMORIES = args.max_memories

    emb_client = LookupEmbeddingClient()
    vector_mod._embedding_client = emb_client
    server_mod._embedding_client = emb_client
    from cpersona.database import get_db

    db = await get_db()

    from sentence_transformers import SentenceTransformer
    st_model = SentenceTransformer(args.model_path, device=args.device)

    taken = {"index": 0, "fallback": 0}
    real_phase1 = vector_mod._index_phase1

    async def counting_phase1(*a, **kw):
        result = await real_phase1(*a, **kw)
        taken["index" if result is not None else "fallback"] += 1
        return result

    vector_mod._index_phase1 = counting_phase1

    failures = []
    checked_queries = 0
    built_indexes = 0

    for task_name in [t.strip() for t in args.tasks.split(",")]:
        task_dir = os.path.join(EVAL_DATA, TASK_MAP[task_name])
        corpus_groups = defaultdict(list)
        for st in discover_task_structure(task_dir):
            corpus_groups[st["corpus"]].append(st)

        for corpus_path, group_subtasks in corpus_groups.items():
            await server_mod.do_delete_agent_data(AGENT_ID)
            _remove_index()
            corpus = load_jsonl(corpus_path)
            corpus_size = await store_corpus(server_mod, emb_client, st_model, corpus,
                                             batch_size=args.batch_size)
            planted = await _plant_ties(db, args.plant_ties)
            corpus_size += planted
            cal = await server_mod.do_calibrate_threshold(AGENT_ID)
            logger.info("calibrated: %s -> %s (planted %d tie rows)",
                        cal.get("old_threshold"), cal.get("new_threshold"), planted)

            prepared = []
            for st in group_subtasks:
                queries_data = load_jsonl(st["queries"])
                query_texts = [q["text"] for q in queries_data]
                q_embs = st_model.encode(query_texts, normalize_embeddings=True,
                                         show_progress_bar=False)
                emb_client.preload(query_texts, q_embs)
                prepared.append((st, queries_data))

            # Pass 1 — the live scan. No index on disk, so nothing to fall back from.
            # The counter is cumulative across corpus groups, so the check below
            # has to be against a baseline taken here rather than against zero.
            index_calls_before_pass1 = taken["index"]
            refs, ref_recalls = {}, {}
            for st, queries_data in prepared:
                refs[st["name"]] = [
                    await vector_mod._search_vector(db, AGENT_ID, q["text"], corpus_size)
                    for q in queries_data
                ]
                ref_recalls[st["name"]] = [
                    [m.get("id") for m in (await server_mod.do_recall(
                        agent_id=AGENT_ID, query=q["text"], limit=corpus_size)).get("messages", [])]
                    for q in queries_data[:args.recall_sample]
                ]
            if taken["index"] != index_calls_before_pass1:
                failures.append("pass 1 used an index that should not have existed")
                logger.error("MISMATCH %s", failures[-1])

            build = await vector_index.build_index(db, "memories")
            if not build.get("built"):
                failures.append(f"{task_name}: index build declined ({build.get('reason')})")
                logger.error("MISMATCH %s", failures[-1])
                continue
            built_indexes += 1
            logger.info("index built: %d rows x %dd, watermark %d",
                        build["count"], build["dim"], build["watermark"])

            # Pass 2 — the same queries, the same corpus, the index in place.
            before = taken["index"]
            for st, queries_data in prepared:
                for qi, q in enumerate(queries_data):
                    fast = await vector_mod._search_vector(db, AGENT_ID, q["text"], corpus_size)
                    ok, detail = compare_results(refs[st["name"]][qi], fast, 0.0, strict=True)
                    if not ok:
                        failures.append(f"{task_name}/{st['name']} q#{qi} [sidecar]: {detail}")
                        logger.error("MISMATCH %s", failures[-1])
                    checked_queries += 1

                for qi, q in enumerate(queries_data[:args.recall_sample]):
                    ids = [m.get("id") for m in (await server_mod.do_recall(
                        agent_id=AGENT_ID, query=q["text"], limit=corpus_size)).get("messages", [])]
                    if ids != ref_recalls[st["name"]][qi]:
                        n_diff = sum(1 for a, c in zip(ids, ref_recalls[st["name"]][qi]) if a != c)
                        failures.append(
                            f"{task_name}/{st['name']} do_recall q#{qi} [sidecar]: id list diverges "
                            f"({n_diff} positions, len {len(ids)} vs {len(ref_recalls[st['name']][qi])})")
                        logger.error("MISMATCH %s", failures[-1])

                logger.info("  %s/%s: %d queries compared, do_recall sample=%d — %s",
                            task_name, st["name"], len(queries_data),
                            min(args.recall_sample, len(queries_data)),
                            "OK" if not failures else f"{len(failures)} failures so far")

            if taken["index"] == before:
                failures.append(
                    f"{task_name}: pass 2 never took the index path — the comparison is vacuous")
                logger.error("MISMATCH %s", failures[-1])

    vector_mod._index_phase1 = real_phase1
    await server_mod.do_delete_agent_data(AGENT_ID)
    _remove_index()
    await server_mod.close_db()
    try:
        os.unlink(_TMP_DB_PATH)
    except OSError:
        pass

    print("\n" + "=" * 60)
    print(f"EQUIVALENCE GATE (sidecar): {checked_queries} queries over {built_indexes} index build(s)")
    print(f"  index-answered phase-1 calls: {taken['index']}, fell back: {taken['fallback']}")
    if failures:
        print(f"FAILED — {len(failures)} mismatches:")
        for f in failures[:20]:
            print("  " + f)
        return 1
    if not checked_queries:
        print("FAILED — nothing was compared")
        return 1
    print("PASSED — the contiguous index is answer-identical to the live scan, "
          "ties included")
    return 0

def main():
    p = argparse.ArgumentParser(description="mps_accel equivalence gate")
    p.add_argument("--tasks", default="LoCoMo")
    p.add_argument("--model_path", default="sentence-transformers/all-MiniLM-L6-v2")
    p.add_argument("--device", default="mps")
    p.add_argument("--backends", default="numpy,torch")
    p.add_argument("--batch_size", type=int, default=256)
    p.add_argument("--max_memories", type=int, default=300000)
    p.add_argument("--recall_sample", type=int, default=25,
                   help="queries per subtask for the full do_recall integration check")
    p.add_argument("--plant_ties", type=int, default=24,
                   help="sidecar mode: rows sharing one embedding, so the tie-break is "
                        "actually reached (real text almost never scores two rows equal)")
    p.add_argument("--mode", default="accel", choices=("accel", "sidecar"),
                   help="accel: the external preloaded-matrix backends. "
                        "sidecar: _search_vector against itself, with and without "
                        "the contiguous index on disk")
    args = p.parse_args()
    runner = gate_sidecar if args.mode == "sidecar" else gate
    sys.exit(asyncio.run(runner(args)))


if __name__ == "__main__":
    main()
