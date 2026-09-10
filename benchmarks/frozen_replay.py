"""Frozen-stage replay of the Track B pipeline: where does Track B - Track A arise?

Separates the Track B - Track A difference into the stages the recall path
actually has, on frozen embeddings and frozen lexical scores:

    S0  dense-only            full cosine ranking (== Track A when the corpus
                              and query populations are identical)
    S1  dense + admission     S0 cut at the calibrated admission floor
                              (threshold x RRF_THRESHOLD_FACTOR) -- exactly the
                              list _recall_rrf receives from _search_vector
    S2  RRF (pre-gate)        _recall_rrf's fusion of S1 with the memory FTS
                              list (episodes/profile arms are empty on LMEB)
    S2nf RRF without floor    same fusion over S0 (diagnostic only; not a
                              pipeline stage)
    S3  after heuristic gate  _apply_quality_gate on S2 with the pool-size
                              min_score (FUSED_GATE off, AUTOCUT off = the
                              Track B regime) == what do_recall returns

Every stage is scored with the harness's own compute_ndcg after the same
candidate-subset filter the harness applies, so S3 must reproduce the recorded
Track B number when the threshold is pinned to the recorded calibration, and
S0 must reproduce Track A. Both identities are checked, and on a sample of
queries S2 / S3 are compared row-for-row with the real _recall_rrf / do_recall.

Per query it also records the exact NDCG delta of each transition
(the exact per-query NDCG delta of docs/research/adaptive-fusion-derivation.md), classifies how
fusion moved the top-10 (lexical-only intruders / lexically boosted dense rows
/ gold rows without a lexical vote), and sweeps a global lexical weight on the
frozen lists as a first look at whether a task-constant weight exists.

Run from the repository root with the same environment the Track B harness uses
(LMEB_DIR points at the LMEB checkout, EMB_CACHE_DIR at a per-model embedding cache):

    LMEB_DIR=~/lmeb python benchmarks/frozen_replay.py \
        --model_path jinaai/jina-embeddings-v5-text-nano \
        --emb_cache_dir ~/lmeb/embcache_jinanano --trust_remote_code --default_task retrieval \
        --pin_from <a Track B output dir whose <task>.json carries calibration records> \
        --tasks LMEB_SciFact --out_dir replay_jinanano

Working set. Fusion is sorted over a bounded set: with a candidate subset, every
allowed row (their full-list ranks are kept, so this is exact); without one, the rows
with dense rank < D or lexical rank < D (D=3000). A row outside that set scores at most
(1+w)/(K+D+1) < 1/(K+10), the least any of the ten best in-set rows can score, so the
top-10 — and the identity check, which compares the first 100 rows — are unchanged.
S0/S1 NDCG are likewise exact (top-10 of the dense order).

Read-only with respect to everything except its own output directory and the
temp DB the harness creates.
"""
from __future__ import annotations

import argparse
import asyncio
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
logger = logging.getLogger("replay")

W_SWEEP = [0.0, 0.1, 0.25, 0.5, 0.75, 1.0]
#: --w_sweep overrides the grid above. The default is the coarse grid the
#: earlier runs used; a finer grid is for locating a default and its
#: flatness, which the coarse one cannot do near its lower edge.


def _dcg_w(rank0: int) -> float:
    return 1.0 / math.log2(rank0 + 2) if rank0 < 10 else 0.0


def ndcg10(ids: list[str], gold: set[str]) -> float:
    if not gold:
        return float("nan")
    dcg = sum(_dcg_w(i) for i, d in enumerate(ids[:10]) if d in gold)
    ideal = sum(_dcg_w(i) for i in range(min(len(gold), 10)))
    return dcg / ideal


def rrf_fuse(dense_ids: list[str], lex_ids: list[str], k: int, w_lex: float = 1.0) -> list[str]:
    """Exactly _recall_rrf's arithmetic and tie order (dict insertion: dense
    first, then FTS; Python sort is stable)."""
    scores: dict[str, float] = {}
    for r, d in enumerate(dense_ids):
        scores[d] = scores.get(d, 0.0) + 1.0 / (k + r + 1)
    for r, d in enumerate(lex_ids):
        scores[d] = scores.get(d, 0.0) + w_lex / (k + r + 1)
    return sorted(scores, key=scores.get, reverse=True)


async def main(args):
    # --- environment: the same block async_main sets before importing cpersona ---
    import tempfile
    tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False, prefix="replay_")
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
    # Track B regime (run_trackb.sh)
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
        "replay must run in the Track B regime (fused gate off, autocut off, confidence off)"

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

    # recorded calibrations to pin (one arm directory: <task>.json -> calibration[])
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
    k = cfg.RRF_K
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

        task_out = out_dir / f"{task_name}.json"
        if task_out.exists() and not args.force:
            logger.info("   exists, skip (use --force)")
            continue
        per_query_fh = open(out_dir / f"{task_name}.queries.jsonl", "w", encoding="utf-8")
        group_records = []
        subtask_stage: dict[str, dict] = {}
        t_task = time.time()

        for corpus_path, gsubs in groups.items():
            await server_mod.do_delete_agent_data(tb.AGENT_ID)
            corpus = tb.load_jsonl(corpus_path)
            n_lines = len(corpus)
            corpus_size = await tb.store_corpus(server_mod, emb_client, st_model, corpus, batch_size=args.batch_size)
            db = await get_db()
            stored = (await db.execute_fetchall(
                "SELECT COUNT(*) FROM memories WHERE agent_id = ?", (tb.AGENT_ID,)))[0][0]

            # --- threshold: pin to the recorded calibration, else calibrate live ---
            gnames = {s["name"] for s in gsubs}
            pinned = None
            for rec in pins.get(task_name, []):
                if set(rec.get("subtasks", [])) == gnames and rec.get("ok"):
                    pinned = rec
                    break
            cal_note: dict
            if pinned is not None:
                vector_mod._agent_thresholds[tb.AGENT_ID] = float(pinned["new_threshold"])
                cal_note = {"source": "pinned", "threshold": float(pinned["new_threshold"]),
                            "method": pinned.get("method"), "proxy": pinned.get("proxy_source"),
                            "youden_j": pinned.get("youden_j")}
            else:
                cal = await server_mod.do_calibrate_threshold(tb.AGENT_ID)
                cal_note = {"source": "live", "threshold": cal.get("new_threshold"), "ok": cal.get("ok"),
                            "method": cal.get("method"), "proxy": cal.get("proxy_source"),
                            "youden_j": cal.get("youden_j")}
            thr = vector_mod._get_vector_threshold(tb.AGENT_ID)
            floor = thr * factor
            await accel.preload(tb.AGENT_ID)

            async with connection() as cdb:
                pm, pe = await scope_stats.get_pool_counts(cdb, tb.AGENT_ID, project_id=None, channel="")
            memory_count = pm + pe
            min_score = mh._adaptive_min_score(memory_count)
            rrf_gate = min_score * cfg.RRF_MAX_SCALE
            logger.info("   corpus %s: lines=%d stored=%d thr=%.4f floor=%.4f pool=%d min_score=%.4f rrf_gate=%.5f (%s)",
                        os.path.relpath(corpus_path, task_dir), n_lines, stored, thr, floor, memory_count,
                        min_score, rrf_gate, cal_note["source"])
            group_records.append({"corpus": os.path.relpath(corpus_path, task_dir), "subtasks": sorted(gnames),
                                  "lines": n_lines, "stored": stored, "collapsed": n_lines - stored,
                                  "calibration": cal_note, "floor": floor, "pool": memory_count,
                                  "min_score": min_score, "rrf_gate": rrf_gate})

            for st in gsubs:
                queries = tb.load_jsonl(st["queries"])
                qrels = tb.load_qrels(st["qrels"])
                cands = tb.load_candidates(st["candidates"]) if st["candidates"] else {}
                qtexts = [q["text"] for q in queries]
                qemb = st_model.encode(qtexts, normalize_embeddings=True, show_progress_bar=False, **tb._QUERY_ENCODE_KW)
                emb_client.preload(qtexts, qemb)

                agg = {key: [] for key in ("s0", "s1", "s2", "s2nf", "s3")}
                wagg = {w: [] for w in W_SWEEP}
                cls = defaultdict(int)
                harm_pos = harm_neg = 0.0
                n_harm = n_help = n_same = 0
                identity = {"checked": 0, "s2_mismatch": 0, "s3_mismatch": 0}
                q_seen = 0
                # deterministic subsample: every k-th query so every scene/subtask is covered
                q_index = list(range(len(queries)))
                if args.max_queries_per_subtask and len(queries) > args.max_queries_per_subtask:
                    step = math.ceil(len(queries) / args.max_queries_per_subtask)
                    q_index = q_index[::step]
                # frozen dense side: the accel cache matrix (what the patched _search_vector scores)
                c = accel.cache
                n_mem = min(vector_mod.MAX_MEMORIES, len(c["mem_meta"]))
                meta_ids = [m_[1] for m_ in c["mem_meta"][:n_mem]]           # msg_id per matrix row
                id_to_idx = {mid: j for j, mid in enumerate(meta_ids)}
                D = args.work_depth

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

                        def flt(ids_: list[str]) -> list[str]:
                            return [d for d in ids_ if d in allowed] if allowed is not None else ids_

                        # --- dense arm: full ranking from the frozen matrix (same BLAS call as the pipeline) ---
                        qvec = np.array(emb_client._lookup[q["text"]], dtype=np.float32)
                        sims = accel._sims(c["mem_mat"], n_mem, qvec)
                        order = np.argsort(-sims, kind="stable")                # heapq.nlargest order (stable ties)
                        rank_of = np.empty(n_mem, dtype=np.int64)
                        rank_of[order] = np.arange(n_mem)
                        n_adm = int((sims >= floor).sum())                        # admitted = prefix of the order
                        top_cos = float(sims[order[0]]) if n_mem else None
                        # --- lexical arm: the real function on the real DB, full list ---
                        lex_rows = await mh._search_memories_keyword(qdb, tb.AGENT_ID, q["text"], corpus_size)
                        lex_ids = [r["msg_id"] for r in lex_rows]
                        lex_bm25_none = bool(lex_rows) and lex_rows[0].get("_bm25") is None
                        lex_rank = {d: r for r, d in enumerate(lex_ids)}
                        n_lex = len(lex_ids)

                        # --- working set: exact for the filtered top-10 (see module docstring) ---
                        if allowed is not None:
                            W_dense = [meta_ids[j] for j in order if meta_ids[j] in allowed]   # dense order
                        else:
                            W_dense = [meta_ids[j] for j in order[:D]]
                        W_lexonly_pool = [d for d in lex_ids[:D] if (allowed is None or d in allowed)] if allowed is None \
                            else [d for d in lex_ids if d in allowed]
                        W_set_dense = set(W_dense)

                        def fuse(admit_n: int, w: float) -> list[str]:
                            # Exactly _recall_rrf's score dict: every ADMITTED row in the working set
                            # gets its dense vote (including rows deeper than D that the lexical pool
                            # brought in -- they are admitted in the pipeline too), then lexical votes.
                            # Insertion order = dense rows in dense order, then lexical-only rows in
                            # lexical order; the stable sort keeps that order for exact ties.
                            score: dict[str, float] = {}
                            dense_rows: list[tuple[int, str]] = []
                            for d in W_dense:
                                rd = int(rank_of[id_to_idx[d]])
                                if rd < admit_n:
                                    dense_rows.append((rd, d))
                            for d in W_lexonly_pool:
                                if d in W_set_dense:
                                    continue
                                j = id_to_idx.get(d)
                                if j is not None:
                                    rd = int(rank_of[j])
                                    if rd < admit_n:
                                        dense_rows.append((rd, d))
                            dense_rows.sort()
                            rows_: list[str] = []
                            for rd, d in dense_rows:
                                score[d] = 1.0 / (k + rd + 1)
                                rows_.append(d)
                            for d in W_lexonly_pool:
                                if d in score:
                                    continue
                                score[d] = 0.0
                                rows_.append(d)
                            if w:
                                for d in rows_:
                                    rl = lex_rank.get(d)
                                    if rl is not None:
                                        score[d] += w / (k + rl + 1)
                            return sorted(rows_, key=score.get, reverse=True)

                        dense_ids_W = W_dense                                        # dense order, within W
                        dense_adm_W = [d for d in W_dense if int(rank_of[id_to_idx[d]]) < n_adm]
                        s2 = fuse(n_adm, 1.0)
                        s2nf = fuse(n_mem, 1.0)
                        dense_adm_set = {d for d in set(W_dense) | set(W_lexonly_pool) if d in id_to_idx and int(rank_of[id_to_idx[d]]) < n_adm}

                        def gate_keep(d: str) -> bool:
                            j = id_to_idx.get(d)
                            if j is not None and int(rank_of[j]) < n_adm:
                                return float(sims[j]) >= min_score
                            return (1.0 / (k + lex_rank[d] + 1)) >= rrf_gate
                        s3 = [d for d in s2 if gate_keep(d)]

                        f0, f1, f2, f2nf, f3 = (flt(dense_ids_W), flt(dense_adm_W), flt(s2), flt(s2nf), flt(s3))
                        n0, n1, n2, n2nf, n3 = (ndcg10(f0, gold), ndcg10(f1, gold), ndcg10(f2, gold),
                                                ndcg10(f2nf, gold), ndcg10(f3, gold))
                        for key, v in zip(("s0", "s1", "s2", "s2nf", "s3"), (n0, n1, n2, n2nf, n3)):
                            agg[key].append(v)
                        for w in W_SWEEP:
                            wagg[w].append(n2 if w == 1.0 else ndcg10(flt(fuse(n_adm, w)), gold))

                        d_fusion = n2 - n1
                        if d_fusion < -1e-12:
                            n_harm += 1
                            harm_neg += -d_fusion
                        elif d_fusion > 1e-12:
                            n_help += 1
                            harm_pos += d_fusion
                        else:
                            n_same += 1

                        top1 = set(f1[:10])
                        top2 = set(f2[:10])
                        gold_out = [d for d in top1 - top2 if d in gold]
                        intruders = [d for d in top2 - top1 if d not in gold]
                        for d in intruders:
                            cls["intruder_lexical_only" if d not in dense_adm_set else "intruder_dense_boosted"] += 1
                        for d in gold_out:
                            cls["gold_out_had_lex_vote" if d in lex_rank else "gold_out_no_lex_vote"] += 1
                        gold_in = [d for d in top2 - top1 if d in gold]
                        for d in gold_in:
                            cls["gold_in_lexical_only" if d not in dense_adm_set else "gold_in_dense_boosted"] += 1
                        # gold demoted inside the top-10 (no membership change) — the H the taxonomy above misses
                        if d_fusion < -1e-12 and not gold_out:
                            cls["harm_reorder_within_top10"] += 1
                        gold_dense_rank = sorted(int(rank_of[id_to_idx[d]]) if d in id_to_idx else -1 for d in gold)
                        gold_below_floor = sum(1 for d in gold if d in id_to_idx and float(sims[id_to_idx[d]]) < floor)
                        gold_missing = sum(1 for d in gold if d not in id_to_idx)
                        # eligible-universe view: how many admitted dense rows
                        # and how many rows at all exist inside this query's candidate subset
                        if allowed is not None:
                            n_eligible = len(W_dense)
                            n_adm_eligible = len(dense_adm_W)
                        else:
                            n_eligible = n_mem
                            n_adm_eligible = n_adm

                        if args.identity_every and (i % args.identity_every == 0):
                            identity["checked"] += 1
                            real2 = await mh._recall_rrf(qdb, tb.AGENT_ID, q["text"], corpus_size, False, "", set(),
                                                         project_id=None, source_id="")
                            real2_ids = flt([r.get("msg_id") for r in real2 if r.get("id") != -1])
                            if real2_ids[:100] != f2[:100]:
                                identity["s2_mismatch"] += 1
                                if identity["s2_mismatch"] <= 3:
                                    logger.warning("S2 mismatch qid=%s first diff at %s", qid,
                                                   next((j for j, (a, b) in enumerate(zip(real2_ids, f2)) if a != b), None))
                            real3 = await server_mod.do_recall(agent_id=tb.AGENT_ID, query=q["text"], limit=corpus_size)
                            real3_ids = flt([m_.get("id", "") for m_ in reversed(real3.get("messages", [])) if m_.get("id")])
                            if real3_ids[:100] != f3[:100]:
                                identity["s3_mismatch"] += 1
                                if identity["s3_mismatch"] <= 3:
                                    logger.warning("S3 mismatch qid=%s first diff at %s", qid,
                                                   next((j for j, (a, b) in enumerate(zip(real3_ids, f3)) if a != b), None))

                        per_query_fh.write(json.dumps({
                            "task": task_name, "subtask": st["name"], "qid": qid, "n_gold": len(gold),
                            "s0": round(n0, 4), "s1": round(n1, 4), "s2": round(n2, 4), "s2nf": round(n2nf, 4), "s3": round(n3, 4),
                            "n_dense_adm": n_adm, "n_lex": n_lex, "lex_like_fallback": lex_bm25_none,
                            "gold_below_floor": gold_below_floor, "gold_missing": gold_missing,
                            "n_eligible": n_eligible, "n_dense_adm_eligible": n_adm_eligible,
                            "gold_dense_rank": gold_dense_rank[:5],
                            "gold_lex_rank": sorted((lex_rank.get(d, -1) for d in gold))[:5],
                            "top_cos": round(top_cos, 4) if top_cos is not None else None,
                            "gold_out": len(gold_out), "gold_in": len(gold_in), "intruders": len(intruders),
                        }) + "\n")
                        q_seen += 1
                        if q_seen % 200 == 0:
                            logger.info("      %s: %d queries", st["name"], q_seen)

                def m(xs): return round(100.0 * float(np.mean(xs)), 3) if xs else float("nan")
                rec = {
                    "queries": q_seen,
                    "S0_dense": m(agg["s0"]), "S1_admitted": m(agg["s1"]), "S2_rrf": m(agg["s2"]),
                    "S2nf_rrf_nofloor": m(agg["s2nf"]), "S3_gated": m(agg["s3"]),
                    "d_admission": round(m(agg["s1"]) - m(agg["s0"]), 3),
                    "d_fusion": round(m(agg["s2"]) - m(agg["s1"]), 3),
                    "d_gate": round(m(agg["s3"]) - m(agg["s2"]), 3),
                    "d_fusion_nofloor_vs_dense": round(m(agg["s2nf"]) - m(agg["s0"]), 3),
                    "fusion_H": round(100.0 * harm_neg / max(q_seen, 1), 3),
                    "fusion_C": round(100.0 * harm_pos / max(q_seen, 1), 3),
                    "n_harmed": n_harm, "n_helped": n_help, "n_unchanged": n_same,
                    "top10_moves": dict(cls),
                    "w_sweep": {str(w): m(wagg[w]) for w in W_SWEEP},
                    "identity": identity,
                }
                subtask_stage[st["name"]] = rec
                logger.info("    %s: S0 %.2f | S1 %.2f | S2 %.2f | S3 %.2f  (adm %+.2f fus %+.2f gate %+.2f) H=%.2f C=%.2f harmed %d helped %d | id %s",
                            st["name"], rec["S0_dense"], rec["S1_admitted"], rec["S2_rrf"], rec["S3_gated"],
                            rec["d_admission"], rec["d_fusion"], rec["d_gate"], rec["fusion_H"], rec["fusion_C"],
                            n_harm, n_help, identity)

        await server_mod.do_delete_agent_data(tb.AGENT_ID)
        per_query_fh.close()

        def tmean(key):
            vals = [v[key] for v in subtask_stage.values() if not math.isnan(v[key])]
            return round(sum(vals) / len(vals), 3) if vals else float("nan")
        task_rec = {
            "task": task_name, "model": args.model_path, "cache_label": os.environ["EMB_CACHE_MODEL"],
            "cpersona_version": __import__("cpersona").__version__,
            "rrf_k": k, "threshold_factor": factor, "pin_from": args.pin_from,
            "time_s": round(time.time() - t_task, 1),
            "mean": {key: tmean(key) for key in ("S0_dense", "S1_admitted", "S2_rrf", "S2nf_rrf_nofloor", "S3_gated",
                                                 "d_admission", "d_fusion", "d_gate", "d_fusion_nofloor_vs_dense",
                                                 "fusion_H", "fusion_C")},
            "w_sweep_mean": {str(w): round(float(np.mean([v["w_sweep"][str(w)] for v in subtask_stage.values()])), 3)
                             for w in W_SWEEP},
            "identity": {kk: sum(v["identity"][kk] for v in subtask_stage.values()) for kk in ("checked", "s2_mismatch", "s3_mismatch")},
            "groups": group_records,
            "subtasks": subtask_stage,
        }
        with open(task_out, "w") as fh:
            json.dump(task_rec, fh, indent=2)
        logger.info("== %s done: %s  w_sweep=%s identity=%s", task_name, task_rec["mean"], task_rec["w_sweep_mean"], task_rec["identity"])

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
    ap.add_argument("--pin_from", default=None, help="arm dir whose <task>.json calibration records pin the threshold")
    ap.add_argument("--calibrate_method", default="separation")
    ap.add_argument("--device", default="mps")
    ap.add_argument("--batch_size", type=int, default=256)
    ap.add_argument("--max_memories", type=int, default=300000)
    ap.add_argument("--trust_remote_code", action="store_true")
    ap.add_argument("--default_task", default=None)
    ap.add_argument("--selfcheck_rate", type=float, default=0.0)
    ap.add_argument("--identity_every", type=int, default=20, help="check S2/S3 against the real pipeline every N queries (0=off)")
    ap.add_argument("--w_sweep", default=None,
                    help="comma-separated lexical weights to sweep (default: the coarse grid)")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--max_queries_per_subtask", type=int, default=0, help="every k-th query so that at most N per subtask are replayed (0=all)")
    ap.add_argument("--work_depth", type=int, default=3000, help="fusion working-set depth D per arm (exact for the top-10 when no candidate subset applies)")
    _args = ap.parse_args()
    if _args.w_sweep:
        W_SWEEP = [float(x) for x in _args.w_sweep.split(",") if x.strip()]
    asyncio.run(main(_args))
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)  # the harness stack has been seen to idle at interpreter exit
