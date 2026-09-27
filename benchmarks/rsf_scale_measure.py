"""rsf's channel scale on LongMemEval: the pre-2.6.0b1 min-max against fixed-scale variants.

bug-247: rsf min-max normalised each channel against the rows a query retrieved, so a
fused score placed a row among its neighbours and an absolute gate could not read it.
The fix puts each channel on a fixed scale (`cpersona/memory_handlers.py`, `_fixed_norm`)
and has two constants to choose: the keyword score that counts half a vote
(`RSF_LEXICAL_HALF`) and what the fused sum is divided by (`RSF_DIVISOR`). This
instrument measures variants against the old scale in the production regime, all in one
process against one store:

- LMEB LongMemEval, every scene stored in its own channel at the times in its session
  titles (so calibration's same-session positives mean something), recall inside the
  question's channel, `limit=10`, `rsf`, autocut and the fused gate at the build's
  defaults.
- The vector threshold is calibrated once; the fused gate is recalibrated for every
  variant, because each variant puts the fused score on its own scale -- as a deployment
  recalibrates when the scoring version moves.
- Queries are split 50/50 per subtask with `trackb_instrument.split_queries` and a seed;
  `--split` chooses the half.

A variant is `legacy` (min-max, the divisor of active channels) or `<divisor>:<half>`
with a divisor of `none` or `present`, e.g. `none:2`. Writes <out>/rows.jsonl (question, variant, returned ids, NDCG@10) and
<out>/summary.json (per variant: mean NDCG@10 by question type and their macro mean, the
fused gate it calibrated to, rows returned).

    PYTHONPATH=benchmarks EMB_CACHE_DIR=~/lmeb/embcache LMEB_DIR=~/lmeb \\
      python benchmarks/rsf_scale_measure.py --split dev --variants legacy,none:2,present:4 --out DIR
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

LIMIT = 10
TYPES = ("knowledge_update", "multi_session", "single_session_assistant", "single_session_preference",
         "single_session_user", "temporal_reasoning")


def parse_variant(spec: str) -> tuple[str, float | None]:
    """`legacy` or `<divisor>:<half>` -> (divisor or 'legacy', half)."""
    if spec == "legacy":
        return "legacy", None
    divisor, half = spec.split(":")
    if divisor not in ("present", "none"):
        raise ValueError(f"unknown divisor {divisor!r}")
    return divisor, float(half)


def ndcg_at_10(ranked: list[str], relevant: set[str]) -> float:
    from longmemeval_by_type import ndcg_at_k

    return ndcg_at_k(ranked, relevant, 10)


def summarize(rows: list[dict]) -> dict:
    """Per variant: mean NDCG@10 by question type, the macro mean over types, and rows returned."""
    out: dict = {}
    for v in sorted({r["variant"] for r in rows}):
        mine = [r for r in rows if r["variant"] == v]
        by_type = {}
        for t in TYPES:
            vals = [r["ndcg10"] for r in mine if r["type"] == t]
            if vals:
                by_type[t] = round(100 * sum(vals) / len(vals), 2)
        out[v] = {"by_type": by_type, "macro": round(sum(by_type.values()) / len(by_type), 2),
                  "n": len(mine), "rows_mean": round(sum(r["n_returned"] for r in mine) / len(mine), 2)}
    return out


async def run(args) -> int:
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=False)
    lmeb = Path(args.lmeb_dir).expanduser()
    task = lmeb / "eval_data" / "Dialogue" / "LongMemEval"
    variants = [parse_variant(v) for v in args.variants]

    import benchmark_trackb_lmeb as tb
    import longmemeval_time_cue as tc
    from trackb_instrument import split_queries

    queries = tc._load_queries(task, TYPES)
    keep = {}
    for t in TYPES:
        ids = [q for q, v in queries.items() if v["type"] == t]
        assignment = split_queries(ids, args.split_seed, t)
        keep.update({q: queries[q] for q in ids if assignment[q] == args.split})
    corpus = tb.load_jsonl(str(task / "corpus.jsonl"))
    if args.limit_questions:
        # A smoke run: a few questions, and only their scenes stored.
        keep = dict(list(keep.items())[: args.limit_questions])
        scenes = {tb.get_scene_id(q) for q in keep}
        corpus = [d for d in corpus if tb.get_scene_id(str(d["id"])) in scenes]
    times = {str(d["id"]): tc.title_instant(d.get("title", "")) for d in corpus}

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False, prefix="rsfscale_")
    tmp.close()
    os.environ.update({
        "CPERSONA_DB_PATH": tmp.name, "CPERSONA_EMBEDDING_MODE": "http",
        "CPERSONA_EMBEDDING_URL": "http://localhost:0", "CPERSONA_VECTOR_SEARCH_MODE": "local",
        "CPERSONA_STORE_BLOB": "true", "CPERSONA_FTS_ENABLED": "true", "CPERSONA_TASK_QUEUE_ENABLED": "false",
        "CPERSONA_MAX_MEMORIES": "300000", "CPERSONA_RECALL_LIBRARY_MAX_LIMIT": "300000",
        "CPERSONA_VECTOR_MIN_SIMILARITY": "0.3", "CPERSONA_RECALL_MODE": "rsf",
    })
    import cpersona
    import cpersona.server as server_mod
    import cpersona.vector as vector_mod
    from cpersona import memory_handlers as mh
    from cpersona.database import get_db

    emb = tb.LookupEmbeddingClient()
    vector_mod._embedding_client = emb
    server_mod._embedding_client = emb
    db = await get_db()

    from sentence_transformers import SentenceTransformer

    kw = {}
    if args.dtype == "float16":
        import torch

        kw["model_kwargs"] = {"torch_dtype": torch.float16}
    st = SentenceTransformer(args.model_path, device=args.device, **kw)
    from budget_batching import install_budget_batching

    install_budget_batching(st)
    started = time.time()
    await tb.store_corpus(server_mod, emb, st, corpus, isolate_scenes=True,
                          timestamp_of=lambda d: times[str(d["id"])].isoformat())
    texts = sorted({q["text"] for q in keep.values()})
    emb.preload(texts, st.encode(texts, normalize_embeddings=True, show_progress_bar=False))

    fixed_norm = mh._fixed_norm
    first = await server_mod.do_calibrate_threshold(tb.AGENT_ID)
    vector_threshold = vector_mod._agent_thresholds.get(tb.AGENT_ID)
    calibrations = {}
    rows = []
    with (out / "rows.jsonl").open("w", encoding="utf-8") as fh:
        for divisor, half in variants:
            name = "legacy" if divisor == "legacy" else f"{divisor}:{half:g}"
            if divisor == "legacy":
                mh._fixed_norm = lambda raw, lexical: mh._minmax_norm(raw)
                mh.RSF_DIVISOR = "active"
            else:
                mh._fixed_norm = fixed_norm
                mh.RSF_DIVISOR, mh.RSF_LEXICAL_HALF = divisor, half
            cal = await server_mod.do_calibrate_threshold(tb.AGENT_ID)
            # One vector threshold for every variant: only the fused gate belongs to the scale.
            vector_mod._agent_thresholds[tb.AGENT_ID] = vector_threshold
            calibrations[name] = {"fused_gate": vector_mod._agent_fused_gates.get(tb.AGENT_ID),
                                  "signal": vector_mod._fused_gate_signal,
                                  "draws": (cal.get("fused_gate") or {}).get("threshold_draws")
                                  if isinstance(cal.get("fused_gate"), dict) else None}
            for qid, q in keep.items():
                resp = await mh.do_recall(agent_id=tb.AGENT_ID, query=q["text"], limit=LIMIT,
                                          channel=tb.get_scene_id(qid))
                ids = [m.get("id") for m in reversed(resp.get("messages", []))]
                row = {"qid": qid, "type": q["type"], "variant": name, "returned": ids, "n_returned": len(ids),
                       "ndcg10": ndcg_at_10(ids, q["relevant"])}
                rows.append(row)
                fh.write(json.dumps(row) + "\n")
    meta = {"cpersona_version": getattr(cpersona, "__version__", "?"), "split": args.split,
            "split_seed": args.split_seed, "questions": len(keep), "variants": [v for v in args.variants],
            "stored": (await db.execute_fetchall("SELECT COUNT(*) FROM memories WHERE agent_id = ?",
                                                 (tb.AGENT_ID,)))[0][0],
            "vector_threshold": vector_threshold, "first_calibration": {k: first.get(k) for k in
                                                                       ("new_threshold", "method")},
            "calibrations": calibrations, "seconds": round(time.time() - started, 1),
            "summary": summarize(rows)}
    (out / "summary.json").write_text(json.dumps(meta, indent=1))
    print(json.dumps(meta["summary"], indent=1))
    await server_mod.close_db()
    os.unlink(tmp.name)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--split", required=True, choices=["dev", "test"])
    ap.add_argument("--split_seed", type=int, default=20260928)
    ap.add_argument("--variants", required=True, type=lambda s: [v for v in s.split(",") if v])
    ap.add_argument("--lmeb_dir", default=os.environ.get("LMEB_DIR", "~/lmeb"))
    ap.add_argument("--model_path", default="BAAI/bge-m3")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--dtype", default="float16", choices=["float16", "float32"])
    ap.add_argument("--limit_questions", type=int, default=0)
    args = ap.parse_args()
    for v in args.variants:
        parse_variant(v)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
