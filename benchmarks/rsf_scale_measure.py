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
- The vector threshold is calibrated once and held for every variant. The fused gate is
  unset for every variant, so each runs under the heuristic gate a store uses before its
  fused gate is calibrated. The fused calibration cannot be used here: its positives are
  rows stored within 30 minutes of a pseudo-query, and with one record per session they
  arise only when two scenes' times happen to coincide, so it succeeds on some draws and
  not others -- and a calibration that fails leaves the previous variant's gate, measured
  on another scale, in place.
- Queries are split 50/50 per subtask with `trackb_instrument.split_queries` and a seed;
  `--split` chooses the half.

A variant is `legacy` (min-max, the divisor of active channels) or `<divisor>:<half>`
with a divisor of `none` or `present`, e.g. `none:2`. Writes <out>/rows.jsonl (question, variant, returned ids, NDCG@10) and
<out>/summary.json (per variant: mean NDCG@10 by question type and their macro mean, the
fused gate it calibrated to, rows returned).

    PYTHONPATH=benchmarks EMB_CACHE_DIR=~/lmeb/embcache LMEB_DIR=~/lmeb \\
      python benchmarks/rsf_scale_measure.py --split dev --variants legacy,none:2,present:4 --out DIR

The first fix was withdrawn. The `<divisor>:<half>` variants switch constants only its
checkouts have, so they refuse to run elsewhere.

The second design (benchmarks/measurements/prereg-rsf-gate-scale.md) keeps the min-max
order and gives the gate its own fixed-scale score, `_rsf_gate_score`. Its variants:

- `gate`: the checkout as it is.
- `legacy`: the gate reads the order score, as in 2.6.0a8. On a checkout whose rsf rows
  carry `_rsf_gate_score`, those scores are removed after fusion, and a row without one
  is gated on `_rsf_score` exactly as before; on a checkout without them nothing is
  patched.

`--split all` keeps every question (the second design selects nothing, so it has no dev
half). With both variants in one run, summary.json also carries the registered controls
and rules: `order_agreement` (control 2: the rows both variants return appear in the same
relative order), `rule_l` (the first fix's rule, gate against legacy) and `added_rows`
(questions where gate returns more rows, and whether the added rows hold an answer).

    PYTHONPATH=benchmarks EMB_CACHE_DIR=~/lmeb/embcache LMEB_DIR=~/lmeb \\
      python benchmarks/rsf_scale_measure.py --split all --variants legacy,gate --out DIR
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import tempfile
import time
from pathlib import Path

LIMIT = 10
TYPES = ("knowledge_update", "multi_session", "single_session_assistant", "single_session_preference",
         "single_session_user", "temporal_reasoning")


def parse_variant(spec: str) -> tuple[str, float | None]:
    """`legacy`, `gate` or `<divisor>:<half>` -> ('legacy' or 'gate' or the divisor, half or None)."""
    if spec in ("legacy", "gate"):
        return spec, None
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


def _by_question(rows: list[dict], variant: str) -> dict:
    return {r["qid"]: r for r in rows if r["variant"] == variant}


def order_agreement(rows: list[dict], a: str = "legacy", b: str = "gate") -> dict:
    """Control 2: the rows both variants return appear in the same relative order.

    The gate may admit a row the other variant dropped or drop one it admitted, which
    shifts every later position, so positions are not compared; the order of the rows
    both returned is.
    """
    left, right = _by_question(rows, a), _by_question(rows, b)
    disagree = []
    for qid in sorted(set(left) & set(right)):
        common = set(left[qid]["returned"]) & set(right[qid]["returned"])
        la = [i for i in left[qid]["returned"] if i in common]
        rb = [i for i in right[qid]["returned"] if i in common]
        if la != rb:
            disagree.append(qid)
    return {"questions": len(set(left) & set(right)), "disagree": len(disagree), "examples": disagree[:10]}


def rule_l(rows: list[dict], base: str = "legacy", arm: str = "gate") -> dict:
    """The first fix's rule at ten rows, `arm` against `base` (prereg-rsf-fixed-scale.md).

    1. In each question type, questions whose NDCG@10 fell minus those whose NDCG@10 rose
       are at most max(2, ceil(0.05 n)).
    2. The macro mean NDCG@10 over types falls by less than 1.0 point.
    """
    left, right = _by_question(rows, base), _by_question(rows, arm)
    per_type, ok_types = {}, True
    for t in TYPES:
        qids = [q for q in left if left[q]["type"] == t and q in right]
        if not qids:
            continue
        fell = sum(1 for q in qids if right[q]["ndcg10"] < left[q]["ndcg10"] - 1e-12)
        rose = sum(1 for q in qids if right[q]["ndcg10"] > left[q]["ndcg10"] + 1e-12)
        allowed = max(2, math.ceil(0.05 * len(qids)))
        per_type[t] = {"n": len(qids), "fell": fell, "rose": rose, "net_fell": fell - rose,
                       "allowed": allowed, "ok": fell - rose <= allowed}
        ok_types = ok_types and per_type[t]["ok"]
    s = summarize([r for r in rows if r["variant"] in (base, arm)])
    delta = s[arm]["macro"] - s[base]["macro"]
    # The macro is compared unrounded as well, so a rounding step cannot move the verdict.
    raw = {}
    for v in (base, arm):
        mine = [r for r in rows if r["variant"] == v]
        means = [sum(r["ndcg10"] for r in mine if r["type"] == t) / max(1, sum(1 for r in mine if r["type"] == t))
                 for t in TYPES if any(r["type"] == t for r in mine)]
        raw[v] = 100 * sum(means) / len(means)
    raw_delta = raw[arm] - raw[base]
    return {"per_type": per_type, "rule1_no_type_falls": ok_types,
            "macro": {base: s[base]["macro"], arm: s[arm]["macro"], "delta": round(delta, 2),
                      "delta_unrounded": raw_delta},
            "rule2_mean_holds": raw_delta > -1.0, "pass": ok_types and raw_delta > -1.0}


def added_rows(rows: list[dict], relevant: dict, base: str = "legacy", arm: str = "gate") -> dict:
    """Questions where `arm` returns more rows than `base`, and whether the added rows hold an answer."""
    left, right = _by_question(rows, base), _by_question(rows, arm)
    more = [q for q in left if q in right and right[q]["n_returned"] > left[q]["n_returned"]]
    fewer = [q for q in left if q in right and right[q]["n_returned"] < left[q]["n_returned"]]
    with_answer = [q for q in more
                   if (set(right[q]["returned"]) - set(left[q]["returned"])) & relevant.get(q, set())]
    return {"more": len(more), "more_with_answer_added": len(with_answer), "fewer": len(fewer)}


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
        if args.split == "all":
            keep.update({q: queries[q] for q in ids})
            continue
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

    fixed_norm = getattr(mh, "_fixed_norm", None)
    recall_rsf = mh._recall_rsf
    first_fix = hasattr(mh, "RSF_DIVISOR")
    if any(d not in ("legacy", "gate") for d, _ in variants) and not first_fix:
        raise SystemExit("the <divisor>:<half> variants need a checkout of the withdrawn first fix")
    if any(d == "gate" for d, _ in variants) and first_fix:
        # There the fixed scale orders the list too, and a legacy variant run before it would
        # leave its patches in place, so `gate` would measure something else under its name.
        raise SystemExit("the gate variant needs a checkout of the second design, not the first fix")

    async def recall_rsf_without_gate_score(*a, **k):
        # The second design's legacy: drop the gate's score, so the gate reads _rsf_score
        # as 2.6.0a8 did. Order and _rsf_score are untouched by construction.
        rows = await recall_rsf(*a, **k)
        for r in rows:
            r.pop("_rsf_gate_score", None)
        return rows
    first = await server_mod.do_calibrate_threshold(tb.AGENT_ID)
    vector_threshold = vector_mod._agent_thresholds.get(tb.AGENT_ID)
    calibrations = {"first_fused_gate": vector_mod._agent_fused_gates.get(tb.AGENT_ID)}
    rows = []
    with (out / "rows.jsonl").open("w", encoding="utf-8") as fh:
        for divisor, half in variants:
            name = divisor if divisor in ("legacy", "gate") else f"{divisor}:{half:g}"
            mh._recall_rsf = recall_rsf
            if divisor == "legacy" and first_fix:
                mh._fixed_norm = lambda raw, lexical: mh._minmax_norm(raw)
                mh.RSF_DIVISOR = "active"
            elif divisor == "legacy":
                mh._recall_rsf = recall_rsf_without_gate_score
            elif divisor == "gate":
                pass
            else:
                mh._fixed_norm = fixed_norm
                mh.RSF_DIVISOR, mh.RSF_LEXICAL_HALF = divisor, half
            # Every variant under the heuristic gate, with the one vector threshold.
            vector_mod._agent_fused_gates.pop(tb.AGENT_ID, None)
            vector_mod._fused_gate_signal = None
            vector_mod._agent_thresholds[tb.AGENT_ID] = vector_threshold
            calibrations[name] = {"fused_gate": vector_mod._get_fused_gate(tb.AGENT_ID)}
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
    names = {r["variant"] for r in rows}
    if {"legacy", "gate"} <= names:
        meta["order_agreement"] = order_agreement(rows)
        meta["rule_l"] = rule_l(rows)
        meta["added_rows"] = added_rows(rows, {q: set(v["relevant"]) for q, v in keep.items()})
    (out / "summary.json").write_text(json.dumps(meta, indent=1))
    print(json.dumps(meta["summary"], indent=1))
    await server_mod.close_db()
    os.unlink(tmp.name)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--split", required=True, choices=["dev", "test", "all"])
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
