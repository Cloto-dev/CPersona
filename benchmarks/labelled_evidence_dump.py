"""Row-keyed dump for the labelled conditional-evidence diagnostic.

The adaptive-fusion design factors the relevance likelihood ratio into the dense
arm's own evidence A(v) and the lexical arm's evidence *conditional on the dense
score*, J(v, l) = log f1(l|v) - log f0(l|v). If J is identically zero the fused
order is the dense order and the conditional-evidence fusion mode has nothing to
recover. Deciding that needs one row per (query, document) with both arms' scores
and the label on the same row; per-query aggregates cannot answer it.

This is a sibling of ``frozen_replay.py``, not a change to it: the replay's stage
identities are relied on elsewhere and are not disturbed here. It reuses the same
frozen dense matrix (the accel cache the patched _search_vector scores) and the
same real lexical arm on the real database, and writes rows instead of
aggregates.

What a row carries, over the *eligible universe* of its query -- the declared
candidate subset where the task has one, the stored corpus otherwise:

    y   1 if the row is gold for that query, else 0
    vr  rank in that query's dense order over the eligible universe (0-based)
    v   cosine similarity, the value the pipeline scores
    lr  rank in the lexical list, empty when the row carries no lexical vote
    l   lexical score, sign-normalised so that LARGER IS A BETTER MATCH
        (the index scores better matches more negative; the convention is
        checked against the returned order rather than assumed), empty when
        absent

Which rows are dumped, fixed by the pre-registration
(``measurements/prereg-conditional-lexical-evidence.md``):

  * every gold row, always;
  * every row with dense rank < 30 -- the region where the top-ten decision is
    made;
  * from each deeper dense-rank bin, a uniform sample of up to 20 non-gold rows,
    drawn from a seed derived from the query id and the bin and therefore
    INDEPENDENT OF y AND OF l. That independence is what keeps the analysis's
    permutation test valid: under the null the sampled non-gold scores and the
    gold scores are draws from the same within-stratum law.

The lexical side does not depend on the embedding model -- same corpus, same
query text, same index -- so across models only the conditioning variable v
changes, which is exactly the question being asked.

Run from the repository root with the environment the Track B harness uses:

    LMEB_DIR=~/lmeb python benchmarks/labelled_evidence_dump.py \
        --model_path jinaai/jina-embeddings-v5-text-nano \
        --emb_cache_dir ~/lmeb/embcache_jinanano --trust_remote_code \
        --default_task retrieval --tasks QASPER --out_dir ~/lmeb/evidence_jinanano \
        --max_queries_per_subtask 200

Read-only with respect to everything except its own output directory and the
temp DB the harness creates.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import gzip
import hashlib
import json
import logging
import math
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

CPERSONA_REPO = os.environ.get("CPERSONA_REPO", str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, CPERSONA_REPO)
sys.path.insert(0, os.path.join(CPERSONA_REPO, "benchmarks"))

logging.basicConfig(format="%(levelname)s|%(asctime)s|%(name)s: %(message)s",
                    datefmt="%Y/%m/%d %H:%M:%S", level=logging.INFO)
logger = logging.getLogger("evidence")

#: Dense-rank bin edges over the eligible universe (0-based rank, right-open).
#: Ranks below KEEP_ALL_BELOW are dumped whole; deeper bins are sampled.
BIN_EDGES = [0, 1, 3, 10, 30, 100, 300, 1000]
KEEP_ALL_BELOW = 30
DEEP_SAMPLE = 20


def code_state() -> dict:
    """The exact tree these rows were produced by.

    A dump is only reproducible against a commit; recording the version string
    alone would not distinguish a clean checkout from a working tree with
    uncommitted edits, and this measurement is meant to be re-runnable.
    """
    import subprocess
    def git(*a):
        try:
            return subprocess.run(["git", "-C", CPERSONA_REPO, *a], capture_output=True,
                                  text=True, timeout=10).stdout.strip()
        except Exception:
            return ""
    return {"commit": git("rev-parse", "HEAD"),
            "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": bool(git("status", "--porcelain"))}


def bin_of(rank: int) -> int:
    """Index of the dense-rank bin holding `rank` (the last bin is unbounded)."""
    for i in range(len(BIN_EDGES) - 1, -1, -1):
        if rank >= BIN_EDGES[i]:
            return i
    return 0


def sample_deep(qid: str, bin_idx: int, ranks: list[int], n: int) -> list[int]:
    """Up to `n` ranks drawn uniformly from `ranks`, seeded by (qid, bin).

    Deterministic, and a function of the query id and the bin only -- never of
    the label or of either arm's score. Sorting the candidate ranks first makes
    the draw independent of the order they were discovered in.
    """
    if len(ranks) <= n:
        return list(ranks)
    seed = int(hashlib.sha256(f"{qid}|{bin_idx}".encode()).hexdigest()[:16], 16)
    rng = np.random.default_rng(seed)
    return [int(r) for r in rng.choice(sorted(ranks), size=n, replace=False)]


async def main(args):
    # --- environment: the Track B regime, identical to the frozen replay's ---
    import tempfile
    tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False, prefix="evidence_")
    tmp_db.close()
    os.environ["CPERSONA_DB_PATH"] = tmp_db.name
    os.environ["CPERSONA_EMBEDDING_MODE"] = "http"
    os.environ["CPERSONA_EMBEDDING_URL"] = "http://localhost:0"
    os.environ["CPERSONA_VECTOR_SEARCH_MODE"] = "local"
    os.environ["CPERSONA_STORE_BLOB"] = "true"
    os.environ["CPERSONA_FTS_ENABLED"] = "true"
    os.environ["CPERSONA_TASK_QUEUE_ENABLED"] = "false"
    os.environ["CPERSONA_MAX_MEMORIES"] = str(args.max_memories)
    os.environ["CPERSONA_RECALL_LIBRARY_MAX_LIMIT"] = str(args.max_memories)
    os.environ["CPERSONA_VECTOR_MIN_SIMILARITY"] = "0.3"
    os.environ["CPERSONA_RECALL_MODE"] = "rrf"
    os.environ["CPERSONA_CALIBRATE_METHOD"] = args.calibrate_method
    os.environ["CPERSONA_AUTOCUT_ENABLED"] = "false"
    os.environ["CPERSONA_FUSED_GATE_ENABLED"] = "false"
    os.environ["EMB_CACHE_DIR"] = os.path.expanduser(args.emb_cache_dir)
    os.environ["EMB_CACHE_MODEL"] = args.emb_cache_model or args.model_path
    os.environ["PYTHONIOENCODING"] = "utf-8"

    import benchmark_trackb_lmeb as tb
    import cpersona.server as server_mod
    import cpersona.vector as vector_mod
    import cpersona.memory_handlers as mh
    import cpersona.config as cfg
    from cpersona.database import get_db, connection
    from cpersona import scope_stats
    from mps_accel import install_fast_accel

    assert not cfg.FUSED_GATE_ENABLED and not cfg.AUTOCUT_ENABLED and not mh.CONFIDENCE_ENABLED, \
        "the dump must run in the Track B regime (fused gate off, autocut off, confidence off)"

    emb_client = tb.LookupEmbeddingClient()
    vector_mod._embedding_client = emb_client
    server_mod._embedding_client = emb_client
    await get_db()
    accel = install_fast_accel(server_mod, vector_mod, mh, backend="numpy", device="cpu",
                               selfcheck_rate=args.selfcheck_rate)

    from sentence_transformers import SentenceTransformer
    import torch
    model_kwargs: dict = {"model_kwargs": {"torch_dtype": torch.float16}}
    if args.default_task:
        model_kwargs["model_kwargs"]["default_task"] = args.default_task
    if args.trust_remote_code:
        model_kwargs["trust_remote_code"] = True
    st_model = SentenceTransformer(args.model_path, device=args.device, **model_kwargs)
    from budget_batching import install_budget_batching
    install_budget_batching(st_model)
    st_prompts = getattr(st_model, "prompts", None) or {}
    tb._DOC_ENCODE_KW = {"prompt_name": "document"} if "document" in st_prompts else {}
    tb._QUERY_ENCODE_KW = {"prompt_name": "query"} if "query" in st_prompts else {}
    logger.info("model %s loaded (prompts=%s, cache=%s label=%s)", args.model_path, st_prompts,
                os.environ["EMB_CACHE_DIR"], os.environ["EMB_CACHE_MODEL"])

    pins: dict[str, list[dict]] = {}
    if args.pin_from:
        for p in Path(os.path.expanduser(args.pin_from)).glob("*.json"):
            if p.name == "summary.json":
                continue
            j = json.load(open(p))
            if j.get("calibration"):
                pins[j["task"]] = j["calibration"]

    out_dir = Path(os.path.expanduser(args.out_dir))
    out_dir.mkdir(parents=True, exist_ok=True)
    factor = cfg.RRF_THRESHOLD_FACTOR

    for task_name in [t.strip() for t in args.tasks.split(",") if t.strip()]:
        task_dir = os.path.join(tb.EVAL_DATA, tb.TASK_MAP[task_name])
        subtasks = tb.discover_task_structure(task_dir)
        if args.subtasks:
            keep = set(args.subtasks.split(","))
            subtasks = [s for s in subtasks if s["name"] in keep]
        groups: dict[str, list[dict]] = defaultdict(list)
        for s in subtasks:
            groups[s["corpus"]].append(s)
        logger.info("== %s: %d subtasks in %d corpus groups", task_name, len(subtasks), len(groups))

        meta_out = out_dir / f"{task_name}.meta.json"
        if meta_out.exists() and not args.force:
            logger.info("   exists, skip (use --force)")
            continue
        rows_fh = gzip.open(out_dir / f"{task_name}.rows.csv.gz", "wt", newline="", encoding="utf-8")
        writer = csv.writer(rows_fh)
        writer.writerow(["qid", "subtask", "doc", "y", "vr", "v", "lr", "l"])
        q_meta: list[dict] = []
        group_records = []
        # Instrument qualification: the sign convention is measured on every
        # query, not assumed once. A single disagreement stops the run.
        sign_checked = sign_bad = 0
        t_task = time.time()

        for corpus_path, gsubs in groups.items():
            await server_mod.do_delete_agent_data(tb.AGENT_ID)
            corpus = tb.load_jsonl(corpus_path)
            n_lines = len(corpus)
            corpus_size = await tb.store_corpus(server_mod, emb_client, st_model, corpus,
                                                batch_size=args.batch_size)
            db = await get_db()
            stored = (await db.execute_fetchall(
                "SELECT COUNT(*) FROM memories WHERE agent_id = ?", (tb.AGENT_ID,)))[0][0]

            gnames = {s["name"] for s in gsubs}
            pinned = None
            for rec in pins.get(task_name, []):
                if set(rec.get("subtasks", [])) == gnames and rec.get("ok"):
                    pinned = rec
                    break
            if pinned is not None:
                vector_mod._agent_thresholds[tb.AGENT_ID] = float(pinned["new_threshold"])
                cal_note = {"source": "pinned", "threshold": float(pinned["new_threshold"])}
            else:
                cal = await server_mod.do_calibrate_threshold(tb.AGENT_ID)
                cal_note = {"source": "live", "threshold": cal.get("new_threshold"), "ok": cal.get("ok")}
            thr = vector_mod._get_vector_threshold(tb.AGENT_ID)
            floor = thr * factor
            await accel.preload(tb.AGENT_ID)

            async with connection() as cdb:
                pm, pe = await scope_stats.get_pool_counts(cdb, tb.AGENT_ID, project_id=None, channel="")
            logger.info("   corpus %s: lines=%d stored=%d thr=%.4f floor=%.4f pool=%d (%s)",
                        os.path.relpath(corpus_path, task_dir), n_lines, stored, thr, floor,
                        pm + pe, cal_note["source"])
            group_records.append({"corpus": os.path.relpath(corpus_path, task_dir),
                                  "subtasks": sorted(gnames), "lines": n_lines, "stored": stored,
                                  "calibration": cal_note, "floor": floor})

            for st in gsubs:
                queries = tb.load_jsonl(st["queries"])
                qrels = tb.load_qrels(st["qrels"])
                cands = tb.load_candidates(st["candidates"]) if st["candidates"] else {}
                qtexts = [q["text"] for q in queries]
                qemb = st_model.encode(qtexts, normalize_embeddings=True, show_progress_bar=False,
                                       **tb._QUERY_ENCODE_KW)
                emb_client.preload(qtexts, qemb)

                q_index = list(range(len(queries)))
                if args.max_queries_per_subtask and len(queries) > args.max_queries_per_subtask:
                    step = math.ceil(len(queries) / args.max_queries_per_subtask)
                    q_index = q_index[::step]

                c = accel.cache
                n_mem = min(vector_mod.MAX_MEMORIES, len(c["mem_meta"]))
                meta_ids = [m_[1] for m_ in c["mem_meta"][:n_mem]]
                n_written = 0

                async with connection() as qdb:
                    for i in q_index:
                        q = queries[i]
                        qid = str(q["id"])
                        gold = {d for d, s in qrels.get(qid, {}).items() if s > 0}
                        if not gold:
                            continue
                        allowed = None
                        if cands:
                            sc = tb.get_scene_id(qid)
                            if sc in cands:
                                allowed = cands[sc]

                        # --- dense arm: the frozen matrix, same call the pipeline makes ---
                        qvec = np.array(emb_client._lookup[q["text"]], dtype=np.float32)
                        sims = accel._sims(c["mem_mat"], n_mem, qvec)
                        order = np.argsort(-sims, kind="stable")
                        # eligible universe, in dense order
                        if allowed is None:
                            elig = [int(j) for j in order]
                        else:
                            elig = [int(j) for j in order if meta_ids[j] in allowed]
                        n_elig = len(elig)
                        n_adm = int((sims >= floor).sum())

                        # --- lexical arm: the real function on the real DB, full list ---
                        lex_rows = await mh._search_memories_keyword(qdb, tb.AGENT_ID, q["text"], corpus_size)
                        raw = [r.get("_bm25") for r in lex_rows]
                        fallback = bool(lex_rows) and raw[0] is None
                        lex_l: dict[str, float] = {}
                        lex_rank: dict[str, int] = {}
                        if not fallback:
                            # The index returns better matches first and scores them
                            # MORE NEGATIVE; l = -raw makes larger mean better. The
                            # convention is checked against the returned order.
                            present = [x for x in raw if x is not None]
                            if len(present) >= 2:
                                sign_checked += 1
                                if not (present[0] <= present[-1] + 1e-9):
                                    sign_bad += 1
                            for r_, row_ in enumerate(lex_rows):
                                if row_.get("_bm25") is None:
                                    continue
                                lex_rank[row_["msg_id"]] = r_
                                lex_l[row_["msg_id"]] = -float(row_["_bm25"])

                        # --- which rows to dump ---
                        gold_ranks: dict[int, str] = {}
                        by_bin: dict[int, list[int]] = defaultdict(list)
                        for rk, j in enumerate(elig):
                            mid = meta_ids[j]
                            if mid in gold:
                                gold_ranks[rk] = mid
                            elif rk >= KEEP_ALL_BELOW:
                                by_bin[bin_of(rk)].append(rk)
                        chosen = set(range(min(KEEP_ALL_BELOW, n_elig))) | set(gold_ranks)
                        bin_pop = {}
                        for b, ranks_ in by_bin.items():
                            bin_pop[b] = len(ranks_)
                            chosen.update(sample_deep(qid, b, ranks_, DEEP_SAMPLE))

                        for rk in sorted(chosen):
                            j = elig[rk]
                            mid = meta_ids[j]
                            lr = lex_rank.get(mid)
                            writer.writerow([
                                qid, st["name"], mid, 1 if mid in gold else 0, rk,
                                f"{float(sims[j]):.6f}",
                                "" if lr is None else lr,
                                "" if lr is None else f"{lex_l[mid]:.6f}",
                            ])
                            n_written += 1

                        q_meta.append({
                            "qid": qid, "subtask": st["name"], "n_elig": n_elig,
                            "n_gold": len(gold),
                            "n_gold_eligible": len(gold_ranks),
                            "gold_ranks": sorted(gold_ranks),
                            "n_adm": n_adm, "n_lex": len(lex_l),
                            "lex_fallback": fallback,
                            "dumped": len(chosen),
                            "bin_pop": {str(k_): v_ for k_, v_ in sorted(bin_pop.items())},
                        })

                logger.info("     %s/%s: %d queries, %d rows", task_name, st["name"],
                            sum(1 for m in q_meta if m["subtask"] == st["name"]), n_written)

        rows_fh.close()
        if sign_bad:
            raise SystemExit(
                f"lexical sign convention disagrees with the returned order on {sign_bad}"
                f"/{sign_checked} queries -- the dump's `l` column cannot be trusted")
        meta = {
            "task": task_name, "model": args.model_path,
            "cache_label": os.environ["EMB_CACHE_MODEL"],
            "cpersona_version": __import__("cpersona").__version__,
            "code_state": code_state(),
            "bin_edges": BIN_EDGES, "keep_all_below": KEEP_ALL_BELOW, "deep_sample": DEEP_SAMPLE,
            "max_queries_per_subtask": args.max_queries_per_subtask,
            "pin_from": args.pin_from,
            "sign_check": {"checked": sign_checked, "disagreed": sign_bad},
            "time_s": round(time.time() - t_task, 1),
            "groups": group_records,
            "queries": q_meta,
        }
        with open(meta_out, "w") as fh:
            json.dump(meta, fh)
        logger.info("== %s done: %d queries, lex_fallback=%d, %.1fs", task_name, len(q_meta),
                    sum(1 for m in q_meta if m["lex_fallback"]), meta["time_s"])

    try:
        os.unlink(tmp_db.name)
    except OSError:
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--model_path", required=True)
    ap.add_argument("--emb_cache_dir", required=True)
    ap.add_argument("--emb_cache_model", default=None)
    ap.add_argument("--tasks", required=True)
    ap.add_argument("--subtasks", default=None)
    ap.add_argument("--out_dir", required=True)
    ap.add_argument("--pin_from", default=None,
                    help="arm dir whose <task>.json calibration records pin the threshold")
    ap.add_argument("--calibrate_method", default="separation")
    ap.add_argument("--device", default="mps")
    ap.add_argument("--batch_size", type=int, default=256)
    ap.add_argument("--max_memories", type=int, default=300000)
    ap.add_argument("--trust_remote_code", action="store_true")
    ap.add_argument("--default_task", default=None)
    ap.add_argument("--selfcheck_rate", type=float, default=0.0)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--max_queries_per_subtask", type=int, default=0,
                    help="every k-th query so that at most N per subtask are dumped (0=all)")
    code = 0
    try:
        asyncio.run(main(ap.parse_args()))
    except SystemExit as e:
        code = int(e.code or 0)
    except BaseException:
        import traceback
        traceback.print_exc()
        code = 1
    sys.stdout.flush()
    sys.stderr.flush()
    # os._exit on BOTH paths: this harness stack has been seen to idle at
    # interpreter exit, joining a non-daemon thread parked on a queue. Guarding
    # only the success path leaves a failed run hanging with no output.
    os._exit(code)
