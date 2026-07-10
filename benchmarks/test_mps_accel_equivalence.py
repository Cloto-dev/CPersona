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

Usage:
  LMEB_DIR=~/lmeb \
  python benchmarks/test_mps_accel_equivalence.py --tasks LoCoMo --device mps \
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


def compare_results(ref, fast, tol):
    """Return (ok, detail) comparing two _search_vector result lists."""
    if len(ref) != len(fast):
        return False, f"length {len(fast)} != {len(ref)}"
    max_diff = 0.0
    for i, (r, f) in enumerate(zip(ref, fast)):
        if r["_rid"] != f["_rid"]:
            # Tolerate order swaps between exact ties only.
            tie = abs(r["_cosine"] - f["_cosine"]) <= tol
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
    db = await server_mod.get_db()

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

                worst = {b: 0.0 for b in backends}
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
    args = p.parse_args()
    sys.exit(asyncio.run(gate(args)))


if __name__ == "__main__":
    main()
