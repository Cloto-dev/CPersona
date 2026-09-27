"""LongMemEval with a time cue: does the cue move the evidence up in what recall returns?

A caller that half-remembers *when* something happened can pass `recall` a
`time_cue` (docs/RECALL_PROCESS_DESIGN.md §2). This instrument asks it of
LongMemEval's own questions: for each question whose text places what it asks
about in time, recall is called with and without the cue that text states,
and the evidence sessions' places in the returned rows are compared.

Fixed here, and recorded in run.json:

- The haystack of the `limit10` regime (`run_longmemeval_by_type.sh`): each
  scene's sessions stored in their own channel, recall inside the question's
  channel, limit 10, rrf, autocut and the fused gate at the build's defaults,
  the threshold calibrated once after storing. Only the scenes of questions
  that carry a cue are stored.
- Each session is stored at the time in its title ("Data time: 03:22 AM on
  Monday 29 May, 2023 - Session 390"), read as UTC. The stored text is the
  title and the user turns, exactly as the Track B runner stores it, so the
  embedding cache already holds every vector.
- The clock every cpersona module reads is stopped at the question's date for
  every call of that question, so "now", an `ago` cue and an open end mean what
  they meant when the question was asked.
- The recall counters are reset before every call, so no call is ranked by the
  calls before it.

Arms, all asked of one store in one process, in this order per question:

  none        no cue: the recall a caller gets without one
  none_again  no cue again: the determinism control
  extracted   the cue the cue file carries for the question
  shifted     the extracted period moved, length kept, to a place it does not
              overlap once each is widened by its confidence's margin: first
              into the past; if that starts before the scene's oldest session,
              into the future, ending no later than the question date; with no
              such place, the question is asked with no cue. Confidence kept.
  half        the extracted period moved half its own length into the past,
              confidence kept: a cue that is partly right.

`shifted` and `half` are built from the extracted cue, the scene's time span
and the question date only; nothing about the evidence enters them.

The cue file is JSONL, one row per LongMemEval question, with `question_id`,
`question_type`, `question_date`, `question` and `time_cue` (null when the
question carries none). Questions are matched to LMEB's query ids by text.

    PYTHONPATH=benchmarks EMB_CACHE_DIR=~/lmeb/embcache LMEB_DIR=~/lmeb \\
      python benchmarks/longmemeval_time_cue.py run --cues CUES.jsonl --out DIR
    python benchmarks/longmemeval_time_cue.py judge DIR

`run` writes DIR/rows.jsonl (one row per question and arm) and DIR/run.json.
`judge` reads them and prints the verdict of the pre-registered rule
(benchmarks/measurements/prereg-longmemeval-time-cue.md) with the tables it
reports. `judge` calls neither the server nor a model.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import hashlib
import itertools
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ARMS = ("none", "none_again", "extracted", "shifted", "half")
LIMIT = 10
TASK_SUBDIR = os.path.join("eval_data", "Dialogue", "LongMemEval")
TYPES = {
    "knowledge_update": "knowledge-update",
    "multi_session": "multi-session",
    "single_session_assistant": "single-session-assistant",
    "single_session_preference": "single-session-preference",
    "single_session_user": "single-session-user",
    "temporal_reasoning": "temporal-reasoning",
}
# The rule's constants (the pre-registration fixes them; see judge()).
MIN_TARGET = 60
ALPHA = 0.05
TYPE_GUARD = 2
PERMUTATIONS = 100_000
SEED = 20260927
EXACT_UP_TO = 20

_TITLE = re.compile(r"Data time: (.+?) - Session \d+$")


# --- time -------------------------------------------------------------------------

def title_instant(title: str) -> _dt.datetime:
    """The UTC instant in a session title; a title without one is an error."""
    match = _TITLE.match(title.strip())
    if not match:
        raise ValueError(f"No time in title: {title!r}")
    naive = _dt.datetime.strptime(match.group(1), "%I:%M %p on %A %d %B, %Y")
    return naive.replace(tzinfo=_dt.timezone.utc)


def question_instant(question_date: str) -> _dt.datetime:
    """LongMemEval's question date ("2023/04/10 (Mon) 23:07") as a UTC instant."""
    naive = _dt.datetime.strptime(question_date.strip(), "%Y/%m/%d (%a) %H:%M")
    return naive.replace(tzinfo=_dt.timezone.utc)


class Clock:
    """The instant every patched `datetime.now()` returns; None = the real clock."""

    at: _dt.datetime | None = None


class _StoppedDatetime(_dt.datetime):
    @classmethod
    def now(cls, tz=None):
        at = Clock.at
        if at is None:
            real = _dt.datetime.now(tz)
            return cls(*real.timetuple()[:6], real.microsecond, real.tzinfo)
        t = at.astimezone(tz) if tz is not None else at.astimezone().replace(tzinfo=None)
        return cls(t.year, t.month, t.day, t.hour, t.minute, t.second, t.microsecond, t.tzinfo)

    @classmethod
    def utcnow(cls):
        at = Clock.at
        if at is None:
            real = _dt.datetime.now(_dt.timezone.utc)
            return cls(*real.timetuple()[:6], real.microsecond)
        return cls(*at.astimezone(_dt.timezone.utc).timetuple()[:6])


_RECALL_MODULES = ("utils", "memory_handlers", "vector", "cue", "scope_stats", "reconstruct", "blocks", "excerpts",
                   "associations", "session", "nodes", "propagation", "providers", "builtin_providers",
                   "recall_trace", "budget", "admin_handlers", "health")


def stop_clock() -> list[str]:
    """Swap `datetime` for the stopped one in every cpersona module the recall path loads; return their names.

    The modules are imported first, so one the recall imports lazily is not left on the real clock.
    """
    import importlib

    for name in _RECALL_MODULES:
        try:
            importlib.import_module(f"cpersona.{name}")
        except ImportError:
            pass
    patched = []
    for name, mod in list(sys.modules.items()):
        if (name == "cpersona" or name.startswith("cpersona.")) and getattr(mod, "datetime", None) is _dt.datetime:
            mod.datetime = _StoppedDatetime
            patched.append(name)
    return sorted(patched)


# --- the cue arms ----------------------------------------------------------------

def moved_cue(cue_mod, raw: dict | None, now, span, arm: str) -> dict | None:
    """The `shifted` or `half` cue built from an extracted one (module docstring), absolute form."""
    if not raw:
        return None
    parsed = cue_mod.parse(raw)
    conf = raw["confidence"]
    window = cue_mod.period(parsed, "sure", now, span)  # unwidened
    if window is None:
        return None
    start, end = window
    length = end - start
    if arm == "half":
        new = (start - length / 2, end - length / 2)
    elif arm == "shifted":
        margin = length * cue_mod.MARGIN[conf]
        distance = length + 2 * margin
        if span[0] is not None and start - distance >= span[0]:
            new = (start - distance, end - distance)
        elif end + distance + 2 * margin <= now:
            new = (start + distance, end + distance)
        else:
            return None
    else:
        raise ValueError(arm)
    out = {"after": new[0].isoformat(), "before": new[1].isoformat(), "confidence": conf}
    cue_mod.parse(out)  # the server must accept what the arm sends
    return out


# --- scoring ---------------------------------------------------------------------

def score(returned: list[str], relevant: set[str]) -> dict:
    """Per-question metrics over every row returned, best first, with no cutoff.

    `ndcg` normalises by the ideal of all evidence placed first; a row past the
    limit (a held seat) counts at its place. `rr` is the reciprocal rank of the
    best evidence row, 0 when none is returned.
    """
    dcg = sum(1 / math.log2(i + 2) for i, d in enumerate(returned) if d in relevant)
    ideal = sum(1 / math.log2(i + 2) for i in range(len(relevant)))
    ranks = [i + 1 for i, d in enumerate(returned) if d in relevant]
    return {
        "ndcg": dcg / ideal if ideal else 0.0,
        "rr": 1 / ranks[0] if ranks else 0.0,
        "all_returned": bool(relevant) and relevant <= set(returned),
        "any_returned": bool(ranks),
        "in_top5": sum(1 for d in returned[:5] if d in relevant),
    }


def sign_flip_p(diffs: list[float], permutations: int = PERMUTATIONS, seed: int = SEED) -> tuple[float, str]:
    """One-sided p for sum(diffs) > 0 under exchangeable arms (random signs).

    Exact over the nonzero differences when there are at most EXACT_UP_TO of
    them; otherwise `permutations` random sign vectors from `seed`, counted as
    (1 + #{S* >= S}) / (1 + permutations).
    """
    nz = [d for d in diffs if d != 0]
    observed = sum(nz)
    if not nz:
        return 1.0, "exact"
    eps = 1e-12
    if len(nz) <= EXACT_UP_TO:
        hits = sum(1 for signs in itertools.product((1, -1), repeat=len(nz))
                   if sum(s * d for s, d in zip(signs, nz)) >= observed - eps)
        return hits / 2 ** len(nz), "exact"
    import numpy as np

    rng = np.random.default_rng(seed)
    arr = np.array(nz)
    hits = 0
    for start in range(0, permutations, 10_000):
        n = min(10_000, permutations - start)
        signs = rng.choice((-1.0, 1.0), size=(n, len(arr)))
        hits += int(((signs * arr).sum(axis=1) >= observed - eps).sum())
    return (1 + hits) / (1 + permutations), f"monte_carlo({permutations}, seed={seed})"


# --- run -------------------------------------------------------------------------

def _load_queries(task_dir: Path) -> dict[str, dict]:
    """LMEB query id -> {type, text, relevant} over the six LongMemEval subtasks."""
    out = {}
    for sub in sorted(TYPES):
        d = task_dir / sub
        rel: dict[str, set[str]] = {}
        for line in (d / "qrels.tsv").read_text(encoding="utf-8").splitlines():
            parts = line.split("\t")
            if len(parts) >= 3 and parts[2].strip().lstrip("-").isdigit() and int(parts[2]) > 0:
                rel.setdefault(parts[0], set()).add(parts[1])
        for line in (d / "queries.jsonl").read_text(encoding="utf-8").splitlines():
            q = json.loads(line)
            out[str(q["id"])] = {"type": sub, "text": q["text"], "relevant": rel.get(str(q["id"]), set())}
    return out


def _norm(text: str) -> str:
    return " ".join(text.split())


def match_cues(queries: dict[str, dict], cue_rows: list[dict]) -> list[dict]:
    """Pair each cued question with its LMEB query by text; an unmatched or ambiguous one is an error."""
    by_text: dict[str, list[str]] = {}
    for qid, q in queries.items():
        by_text.setdefault(_norm(q["text"]), []).append(qid)
    out = []
    for row in cue_rows:
        if not row.get("time_cue"):
            continue
        hits = by_text.get(_norm(row["question"]), [])
        if len(hits) != 1:
            raise ValueError(f"{row['question_id']}: {len(hits)} LMEB queries have its text")
        qid = hits[0]
        if TYPES[queries[qid]["type"]] != row["question_type"]:
            raise ValueError(f"{row['question_id']}: type {row['question_type']} vs LMEB {queries[qid]['type']}")
        out.append({"qid": qid, "question_id": row["question_id"], **queries[qid],
                    "question_date": row["question_date"], "time_cue": row["time_cue"]})
    return out


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


async def run(args) -> int:
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=False)
    lmeb = Path(args.lmeb_dir).expanduser()
    task_dir = lmeb / TASK_SUBDIR
    cues_path = Path(args.cues).expanduser()
    cue_rows = [json.loads(line) for line in cues_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    questions = match_cues(_load_queries(task_dir), cue_rows)
    if args.limit_questions:
        questions = questions[: args.limit_questions]

    import benchmark_trackb_lmeb as tb

    scenes = {tb.get_scene_id(q["qid"]) for q in questions}
    corpus = [d for d in tb.load_jsonl(str(task_dir / "corpus.jsonl")) if tb.get_scene_id(str(d["id"])) in scenes]
    times = {str(d["id"]): title_instant(d.get("title", "")) for d in corpus}

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False, prefix="timecue_")
    tmp.close()
    os.environ.update({
        "CPERSONA_DB_PATH": tmp.name,
        "CPERSONA_EMBEDDING_MODE": "http",
        "CPERSONA_EMBEDDING_URL": "http://localhost:0",
        "CPERSONA_VECTOR_SEARCH_MODE": "local",
        "CPERSONA_STORE_BLOB": "true",
        "CPERSONA_FTS_ENABLED": "true",
        "CPERSONA_TASK_QUEUE_ENABLED": "false",
        "CPERSONA_MAX_MEMORIES": str(args.max_memories),
        "CPERSONA_RECALL_LIBRARY_MAX_LIMIT": str(args.max_memories),
        "CPERSONA_VECTOR_MIN_SIMILARITY": "0.3",
        "CPERSONA_RECALL_MODE": "rrf",
    })

    import cpersona
    import cpersona.server as server_mod
    import cpersona.vector as vector_mod
    from cpersona import config as cfg
    from cpersona import cue as cue_mod
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
    stored = (await db.execute_fetchall("SELECT COUNT(*) FROM memories WHERE agent_id = ?", (tb.AGENT_ID,)))[0][0]
    cal = await server_mod.do_calibrate_threshold(tb.AGENT_ID)
    texts = [q["text"] for q in questions]
    emb.preload(texts, st.encode(texts, normalize_embeddings=True, show_progress_bar=False))

    patched = stop_clock()
    Clock.at = _dt.datetime(2000, 1, 2, 3, 4, 5, tzinfo=_dt.timezone.utc)
    assert mh.datetime.now(_dt.timezone.utc) == Clock.at, "clock not stopped in memory_handlers"
    Clock.at = None

    commit = subprocess.run(["git", "-C", str(Path(cpersona.__file__).resolve().parents[1]), "rev-parse", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    rows_path = out / "rows.jsonl"
    n_calls = 0
    with rows_path.open("w", encoding="utf-8") as fh:
        for q in questions:
            scene = tb.get_scene_id(q["qid"])
            now = question_instant(q["question_date"])
            own = [t for d, t in times.items() if tb.get_scene_id(d) == scene]
            span = (min(own), max(own))
            sent = {
                "none": None, "none_again": None, "extracted": q["time_cue"],
                "shifted": moved_cue(cue_mod, q["time_cue"], now, span, "shifted"),
                "half": moved_cue(cue_mod, q["time_cue"], now, span, "half"),
            }
            for arm in args.arms:
                await db.execute("UPDATE memories SET recall_count = 0, last_recalled_at = NULL WHERE agent_id = ?",
                                 (tb.AGENT_ID,))
                await db.commit()
                Clock.at = now
                t0 = time.perf_counter()
                resp = await mh.do_recall(agent_id=tb.AGENT_ID, query=q["text"], limit=LIMIT, channel=scene,
                                          **({"time_cue": sent[arm]} if sent[arm] else {}))
                ms = (time.perf_counter() - t0) * 1000
                Clock.at = None
                n_calls += 1
                if "error" in resp:
                    raise SystemExit(f"{q['qid']} {arm}: {resp['error']}")
                msgs = list(reversed(resp.get("messages", [])))  # best first
                seat = [m.get("id") for m in msgs
                        if (m.get("match_reason") or {}).get("signal") == "cue"]
                fh.write(json.dumps({
                    "qid": q["qid"], "question_id": q["question_id"], "type": q["type"], "arm": arm,
                    "question_date": q["question_date"], "time_cue": sent[arm],
                    "returned": [m.get("id") for m in msgs], "seat": seat,
                    "response_time_cue": resp.get("time_cue"),
                    "relevant": sorted(q["relevant"]),
                    "relevant_times": {d: times[d].isoformat() for d in sorted(q["relevant"]) if d in times},
                    "latency_ms": round(ms, 2),
                }, ensure_ascii=False) + "\n")

    run_meta = {
        "cpersona_version": getattr(cpersona, "__version__", "?"), "cpersona_commit": commit,
        "cue_policy": cue_mod.POLICY, "arms": list(args.arms), "limit": LIMIT,
        "questions": len(questions), "scenes": len(scenes), "sessions": len(corpus), "stored": stored,
        "cues_file": str(cues_path), "cues_sha256_16": _sha256(cues_path),
        "extractor_sha": sorted({r.get("extractor_sha") for r in cue_rows if r.get("extractor_sha")}),
        "calibration": {k: cal.get(k) for k in ("ok", "old_threshold", "new_threshold", "method", "z_factor")},
        "autocut_enabled": bool(cfg.AUTOCUT_ENABLED), "fused_gate_enabled": bool(cfg.FUSED_GATE_ENABLED),
        "recall_mode": os.environ["CPERSONA_RECALL_MODE"], "confidence_enabled": bool(mh.CONFIDENCE_ENABLED),
        "env": {k: v for k, v in sorted(os.environ.items()) if k.startswith("CPERSONA_") and k != "CPERSONA_DB_PATH"},
        "model": args.model_path, "dtype": args.dtype, "device": args.device,
        "clock_patched": patched, "calls": n_calls, "seconds": round(time.time() - started, 1),
    }
    (out / "run.json").write_text(json.dumps(run_meta, indent=1, ensure_ascii=False))
    print(json.dumps({k: run_meta[k] for k in ("questions", "scenes", "stored", "calibration", "calls", "seconds")}))
    await server_mod.close_db()
    os.unlink(tmp.name)
    return 0


# --- judge -----------------------------------------------------------------------

def judge(rows: list[dict], policy: str) -> dict:
    """The pre-registered verdict and the numbers it reports, from rows.jsonl."""
    by = {(r["qid"], r["arm"]): r for r in rows}
    qids = sorted({r["qid"] for r in rows})
    pre: dict[str, object] = {}

    def sc(qid, arm):
        r = by[(qid, arm)]
        return score(r["returned"], set(r["relevant"]))

    ignored = [q for q in qids if (by[(q, "extracted")].get("response_time_cue") or {}).get("ignored")]
    target = [q for q in qids if q not in ignored and by[(q, "extracted")]["relevant"]]
    pre["target"] = len(target)
    pre["target_at_least"] = len(target) >= MIN_TARGET
    pre["replicate_identical"] = all(by[(q, "none")]["returned"] == by[(q, "none_again")]["returned"] for q in qids)
    pre["returned_preserved"] = all(
        set(by[(q, "none")]["returned"]) <= set(by[(q, "extracted")]["returned"])
        and len(by[(q, "extracted")]["returned"]) - len(by[(q, "none")]["returned"]) <= 1
        for q in target)
    pre["positive_control"] = sum(by[(q, "none")]["returned"] != by[(q, "extracted")]["returned"] for q in target)
    pre["policy_reported"] = all((by[(q, "extracted")].get("response_time_cue") or {}).get("policy") == policy
                                 for q in qids)
    valid = (pre["target_at_least"] and pre["replicate_identical"] and pre["returned_preserved"]
             and pre["positive_control"] >= 1 and pre["policy_reported"])

    diffs = {q: sc(q, "extracted")["ndcg"] - sc(q, "none")["ndcg"] for q in target}
    p, method = sign_flip_p([diffs[q] for q in target])
    per_type = {}
    for q in target:
        t = per_type.setdefault(by[(q, "none")]["type"], {"n": 0, "up": 0, "down": 0})
        t["n"] += 1
        t["up"] += diffs[q] > 0
        t["down"] += diffs[q] < 0
    guard = all(t["down"] - t["up"] <= TYPE_GUARD for t in per_type.values())
    passed = valid and p < ALPHA and guard

    def arm_table(arm):
        s = [sc(q, arm) for q in target]
        n = len(s) or 1
        return {"ndcg": round(sum(x["ndcg"] for x in s) / n, 4), "rr": round(sum(x["rr"] for x in s) / n, 4),
                "all_returned": sum(x["all_returned"] for x in s), "any_returned": sum(x["any_returned"] for x in s),
                "evidence_in_top5": sum(x["in_top5"] for x in s)}

    def paired(arm, key):
        a = [sc(q, arm)[key] for q in target]
        b = [sc(q, "none")[key] for q in target]
        return {"up": sum(x > y for x, y in zip(a, b)), "down": sum(x < y for x, y in zip(a, b))}

    seat_evidence = sum(1 for q in target if set(by[(q, "extracted")]["seat"]) & set(by[(q, "extracted")]["relevant"]))
    period_holds = 0
    for q in target:
        per = (by[(q, "extracted")].get("response_time_cue") or {}).get("period")
        if per:
            lo, hi = (_dt.datetime.fromisoformat(x) for x in per)
            times = [_dt.datetime.fromisoformat(t) for t in by[(q, "extracted")]["relevant_times"].values()]
            period_holds += any(lo <= t < hi for t in times)
    return {
        "verdict": "pass" if passed else ("void" if not valid else "null"),
        "preconditions": pre, "valid": valid,
        "primary": {"metric": "ndcg over the returned rows, extracted minus none", "sum": round(sum(diffs.values()), 4),
                    "up": sum(d > 0 for d in diffs.values()), "down": sum(d < 0 for d in diffs.values()),
                    "p_one_sided": p, "method": method, "alpha": ALPHA},
        "type_guard": {"max_net_down": TYPE_GUARD, "ok": guard, "by_type": per_type},
        "report": {
            "ignored": len(ignored),
            "arms": {arm: arm_table(arm) for arm in ARMS if all((q, arm) in by for q in target)},
            "paired_vs_none": {arm: {k: paired(arm, k) for k in ("rr", "all_returned", "in_top5")}
                               for arm in ("extracted", "shifted", "half") if all((q, arm) in by for q in target)},
            "seat_carried_evidence": seat_evidence,
            "period_holds_evidence": period_holds,
            "confidence": {c: sum(1 for q in target if (by[(q, "extracted")]["time_cue"] or {}).get("confidence") == c)
                           for c in ("sure", "likely", "vague")},
            "latency_ms_median": {arm: sorted(by[(q, arm)]["latency_ms"] for q in qids)[len(qids) // 2]
                                  for arm in ARMS if all((q, arm) in by for q in qids)},
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--cues", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--lmeb_dir", default=os.environ.get("LMEB_DIR", "~/lmeb"))
    r.add_argument("--model_path", default="BAAI/bge-m3")
    r.add_argument("--device", default="cpu")
    r.add_argument("--dtype", default="float16", choices=["float16", "float32"])
    r.add_argument("--max_memories", type=int, default=300000)
    r.add_argument("--arms", default=",".join(ARMS), type=lambda s: [a for a in s.split(",") if a])
    r.add_argument("--limit_questions", type=int, default=0, help="first N cued questions only (a smoke run)")
    j = sub.add_parser("judge")
    j.add_argument("dir")
    j.add_argument("--policy", default=None, help="the policy the extracted arm must report (default: run.json's)")
    args = ap.parse_args()
    if args.cmd == "run":
        bad = [a for a in args.arms if a not in ARMS]
        if bad:
            ap.error(f"unknown arms {bad}")
        return asyncio.run(run(args))
    d = Path(args.dir).expanduser()
    meta = json.loads((d / "run.json").read_text())
    rows = [json.loads(line) for line in (d / "rows.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    print(json.dumps(judge(rows, args.policy or meta["cue_policy"]), indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
