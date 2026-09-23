"""End-to-end QA accuracy on LongMemEval: an isolated reader and judge over what recall returned.

Evaluation only. The memory server never calls a model; this script reads the
rankings a retrieval run dumped (`benchmark_trackb_lmeb.py --dump_rankings`),
rebuilds the context a caller would have read, and asks a reader to answer and a
judge to grade the answer. Both run through `reconstruct_count_codex.run_isolated`:
an isolated, subscription-authenticated Codex call with no tools, hooks, memories
or user configuration.

The reader prompt, the history formatting (sessions sorted by date, `nl` turns)
and the per-type judge prompts are copied verbatim from the LongMemEval
reference implementation (MIT), github.com/xiaowu0162/LongMemEval at 9e0b455:
`src/generation/run_generation.py` (no chain of thought, flat-session retriever)
and `src/evaluation/evaluate_qa.py`. The judge model is not the reference one,
so the numbers are for comparing arms of this harness, not for comparing with
published results.

Two arms, both over the same returned rows, both built from what the memory
stored and nothing else (the benchmark stores each session's title and user
turns, so an assistant turn is never available to a caller):
  expanded  every returned record in full, as `get_contents` would return it
            (the upper bound of what retrieval delivered; the preview cut is
            not part of it)
  preview   what the recall response shows by default: the stored text cut to
            the preview length
Each session's date is the time in its stored title, in the reference's format.
The oracle self-tests use the reference evidence sessions instead, both roles.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from benchmarks.reconstruct_count_codex import run_isolated
from benchmarks.reconstruct_count_qa import canonical

MODEL = "gpt-6-luna"
PREVIEW_CHARS = 500
TOP_K = 10
LMEB_TYPES = {
    "knowledge_update": "knowledge-update",
    "multi_session": "multi-session",
    "single_session_assistant": "single-session-assistant",
    "single_session_preference": "single-session-preference",
    "single_session_user": "single-session-user",
    "temporal_reasoning": "temporal-reasoning",
}

# --- prompts: verbatim from the reference implementation --------------------------

READER_TEMPLATE = (
    "I will give you several history chats between you and a user. Please answer the "
    "question based on the relevant chat history.\n\n\nHistory Chats:\n\n{}\n\n"
    "Current Date: {}\nQuestion: {}\nAnswer:"
)

_JUDGE_COMMON = (
    "I will give you a question, a correct answer, and a response from a model. Please "
    "answer yes if the response contains the correct answer. Otherwise, answer no. If the "
    "response is equivalent to the correct answer or contains all the intermediate steps "
    "to get the correct answer, you should also answer yes. If the response only contains "
    "a subset of the information required by the answer, answer no. "
)
_JUDGE_TAIL = "\n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
JUDGE_TEMPLATES = {
    "single-session-user": _JUDGE_COMMON + _JUDGE_TAIL,
    "single-session-assistant": _JUDGE_COMMON + _JUDGE_TAIL,
    "multi-session": _JUDGE_COMMON + _JUDGE_TAIL,
    "temporal-reasoning": _JUDGE_COMMON + (
        "In addition, do not penalize off-by-one errors for the number of days. If the "
        "question asks for the number of days/weeks/months, etc., and the model makes "
        "off-by-one errors (e.g., predicting 19 days when the answer is 18), the model's "
        "response is still correct. "
    ) + _JUDGE_TAIL,
    "knowledge-update": (
        "I will give you a question, a correct answer, and a response from a model. Please "
        "answer yes if the response contains the correct answer. Otherwise, answer no. If the "
        "response contains some previous information along with an updated answer, the "
        "response should be considered as correct as long as the updated answer is the "
        "required answer."
    ) + _JUDGE_TAIL,
    "single-session-preference": (
        "I will give you a question, a rubric for desired personalized response, and a "
        "response from a model. Please answer yes if the response satisfies the desired "
        "response. Otherwise, answer no. The model does not need to reflect all the points "
        "in the rubric. The response is correct as long as it recalls and utilizes the "
        "user's personal information correctly.\n\nQuestion: {}\n\nRubric: {}\n\n"
        "Model Response: {}\n\nIs the model response correct? Answer yes or no only."
    ),
}
JUDGE_ABSTENTION = (
    "I will give you an unanswerable question, an explanation, and a response from a "
    "model. Please answer yes if the model correctly identifies the question as "
    "unanswerable. The model could say that the information is incomplete, or some other "
    "information is given but the asked information is not.\n\nQuestion: {}\n\n"
    "Explanation: {}\n\nModel Response: {}\n\nDoes the model correctly identify the "
    "question as unanswerable? Answer yes or no only."
)

# The reference sends each prompt as the only user message with no system prompt.
# Codex requires instructions, so they say no more than the prompt already does.
READER_SYSTEM = "Follow the request. Answer only from the chat history it contains."
JUDGE_SYSTEM = "Follow the request. Grade only the response it contains."
READER_SCHEMA = {"type": "object", "additionalProperties": False,
                 "properties": {"answer": {"type": "string"}}, "required": ["answer"]}
JUDGE_SCHEMA = {"type": "object", "additionalProperties": False,
                "properties": {"verdict": {"type": "string", "enum": ["yes", "no"]}},
                "required": ["verdict"]}


def normalized(text):
    return re.sub(r"\s+", " ", text).strip().lower()


def judge_prompt(reference, response):
    """The reference implementation's prompt for this question, abstention first."""
    template = JUDGE_ABSTENTION if reference["abstention"] else JUDGE_TEMPLATES[reference["question_type"]]
    return template.format(reference["question"], reference["answer"], response)


def history_string(sessions):
    """Sessions as (date, turns or text), sorted by date, in the reference `nl` format."""
    out = ""
    for i, (date, content) in enumerate(sorted(sessions, key=lambda s: s[0])):
        if isinstance(content, list):
            body = "".join(f"\n\n{t['role']}: {t['content'].strip()}" for t in content)
        else:
            body = content
        out += f"\n### Session {i + 1}:\nSession Date: {date}\nSession Content:\n{body}\n"
    return out


def reader_prompt(reference, sessions):
    return READER_TEMPLATE.format(history_string(sessions), reference["question_date"], reference["question"])


# --- inputs --------------------------------------------------------------------------


def load_references(oracle_path):
    """Official answers, types, dates and abstention flags, keyed by normalized question."""
    out = {}
    for entry in json.load(open(oracle_path, encoding="utf-8")):
        key = normalized(entry["question"])
        if key in out:
            raise ValueError(f"Duplicate question text: {entry['question_id']}")
        out[key] = {
            "question_id": entry["question_id"], "question_type": entry["question_type"],
            "question": entry["question"], "answer": str(entry["answer"]),
            "question_date": entry["question_date"],
            "abstention": entry["question_id"].endswith("_abs"),
            # The oracle file holds only the evidence sessions: the oracle self-test's context.
            "evidence": list(zip(entry["haystack_dates"], entry["haystack_sessions"], strict=True)),
        }
    return out


def load_queries(lmeb_dir):
    """LMEB query id -> (LongMemEval type, text)."""
    out = {}
    for sub, qtype in LMEB_TYPES.items():
        for line in open(Path(lmeb_dir) / sub / "queries.jsonl", encoding="utf-8"):
            row = json.loads(line)
            out[row["id"]] = (qtype, row["text"])
    return out


def load_rankings(path):
    """query id -> returned ids, from a --dump_rankings file (the header line is skipped)."""
    out = {}
    for line in open(path, encoding="utf-8"):
        row = json.loads(line)
        if row.get("header"):
            continue
        out[row["query_id"]] = row["returned_ids"]
    return out


def scene_of(qid):
    return qid.split("_q_", 1)[0]


def rows_for(qid, returned, k=TOP_K):
    """The first k returned rows that belong to the question's own history.

    A row from another scene is another user's history: showing it to the reader
    would be contamination, not retrieval. Such rows are dropped and counted, never
    backfilled, so a question can be answered from fewer than k rows.
    """
    own = [d for d in returned[:k] if d.startswith(scene_of(qid) + "_session_")]
    return own, len(returned[:k]) - len(own)


def load_stored(corpus_path, wanted):
    """LMEB doc id -> (title, text) for the wanted ids."""
    out = {}
    for line in open(corpus_path, encoding="utf-8"):
        row = json.loads(line)
        if row["id"] in wanted:
            out[row["id"]] = (row.get("title", ""), row["text"])
    return out


def session_date(title):
    """The stored title's time in the reference's date format.

    "Data time: 03:22 AM on Monday 29 May, 2023 - Session 390" becomes
    "2023/05/29 (Mon) 03:22". The format sorts in time order, which the history
    ordering relies on, so a title without a readable time is an error.
    """
    match = re.match(r"Data time: (.+?) - Session \d+$", title.strip())
    if not match:
        raise ValueError(f"No time in title: {title!r}")
    return datetime.strptime(match.group(1), "%I:%M %p on %A %d %B, %Y").strftime("%Y/%m/%d (%a) %H:%M")


def stored_content(title, text):
    """A record's content as the benchmark stores it."""
    return f"{title} {text}"


# --- calls ---------------------------------------------------------------------------


#: Attempts per call, and the pause before each retry. A failed turn here is the
#: transport (the backend refusing a connection under load), not an answer, so a
#: retry does not choose among answers; each failed attempt's artifacts are kept
#: beside the call and the result records how many attempts it took.
ATTEMPTS = 3
RETRY_PAUSE_S = (15, 60)


def call(prompt, system, schema, cache_dir, *, effort, rep=0):
    """One isolated call, cached by everything that determines it.

    ``rep`` > 0 names a repeat that must not reuse the cache: the A/A measurement.
    """
    key = hashlib.sha256(canonical({"model": MODEL, "effort": effort, "system": system,
                                    "schema": schema, "prompt": prompt, "rep": rep}).encode()).hexdigest()
    out = Path(cache_dir) / key[:2] / key
    if (out / "result.json").exists():
        return json.loads((out / "result.json").read_text())
    for attempt in range(1, ATTEMPTS + 1):
        if out.exists():
            # A call that failed earlier, in this run or a previous one: keep it as evidence.
            out.rename(out.with_name(f"{key}.failed-{time.time_ns()}"))
        try:
            raw, measured = run_isolated(prompt, system, schema, out, model=MODEL, effort=effort)
            break
        except (RuntimeError, ValueError):
            if attempt == ATTEMPTS:
                raise
            time.sleep(RETRY_PAUSE_S[min(attempt, len(RETRY_PAUSE_S)) - 1])
    result = {"key": key, "model": MODEL, "effort": effort, "attempts": attempt,
              "output": raw, "metrics": measured}
    (out / "result.json").write_text(canonical(result))
    return result


def answer_and_grade(item, cache_dir, reader_effort, judge_effort, rep=0):
    reader = call(item["prompt"], READER_SYSTEM, READER_SCHEMA, cache_dir, effort=reader_effort, rep=rep)
    answer = reader["output"]["answer"]
    judge = call(judge_prompt(item["reference"], answer), JUDGE_SYSTEM, JUDGE_SCHEMA,
                 cache_dir, effort=judge_effort, rep=rep)
    verdict = judge["output"]["verdict"]
    if verdict not in ("yes", "no"):
        raise ValueError(f"Invalid verdict: {verdict!r}")
    return {"qid": item["qid"], "arm": item["arm"], "question_type": item["reference"]["question_type"],
            "abstention": item["reference"]["abstention"], "rows": item.get("rows"),
            "answer": answer, "correct": verdict == "yes",
            "reader_key": reader["key"], "judge_key": judge["key"],
            "reader_metrics": reader["metrics"], "judge_metrics": judge["metrics"]}


def grade_only(item, cache_dir, judge_effort):
    """The judge alone, on a fixed response: the judge's own self-test."""
    judge = call(judge_prompt(item["reference"], item["response"]), JUDGE_SYSTEM, JUDGE_SCHEMA,
                 cache_dir, effort=judge_effort)
    verdict = judge["output"]["verdict"]
    if verdict not in ("yes", "no"):
        raise ValueError(f"Invalid verdict: {verdict!r}")
    return {"qid": item["qid"], "arm": item["arm"], "question_type": item["reference"]["question_type"],
            "abstention": item["reference"]["abstention"], "answer": item["response"],
            "correct": verdict == "yes", "judge_key": judge["key"], "judge_metrics": judge["metrics"]}


def summarize(results):
    """Per arm: accuracy by type (abstention questions apart), and overall."""
    out = {}
    for r in results:
        arm = out.setdefault(r["arm"], {})
        bucket = "abstention" if r["abstention"] else r["question_type"]
        cell = arm.setdefault(bucket, {"n": 0, "correct": 0})
        cell["n"] += 1
        cell["correct"] += r["correct"]
    for arm in out.values():
        total = {"n": sum(c["n"] for c in arm.values()), "correct": sum(c["correct"] for c in arm.values())}
        arm["overall"] = total
        for cell in arm.values():
            cell["accuracy"] = round(cell["correct"] / cell["n"], 4) if cell["n"] else None
    return out


def derangement(n, rng):
    while True:
        p = list(range(n))
        rng.shuffle(p)
        if all(i != v for i, v in enumerate(p)):
            return p


# --- building the items ----------------------------------------------------------------


def build_items(args, references, queries):
    """(items, stats) for the chosen mode. Items carry the prompt the reader will see."""
    rng = random.Random(args.seed)
    by_qid = {}
    for qid, (qtype, text) in queries.items():
        ref = references.get(normalized(text))
        if ref is None or ref["question_type"] != qtype:
            raise ValueError(f"No reference for {qid}")
        by_qid[qid] = ref
    qids = sorted(by_qid)
    if args.limit:
        qids = stratified(qids, by_qid, args.limit, rng)
    items, stats = [], {"questions": len(qids), "cross_scene_rows_dropped": 0}

    if args.mode in ("judge_gold", "judge_other"):
        # Abstention questions have no answer to hand over, only an explanation.
        qids = [q for q in qids if not by_qid[q]["abstention"]]
        responses = {q: by_qid[q]["answer"] for q in qids}
        if args.mode == "judge_other":
            perm = derangement(len(qids), rng)
            responses = {q: by_qid[qids[perm[i]]]["answer"] for i, q in enumerate(qids)}
        stats["questions"] = len(qids)
        return [{"qid": q, "arm": args.mode, "reference": by_qid[q], "response": responses[q]}
                for q in qids], stats

    if args.mode in ("oracle", "empty", "shuffled"):
        contexts = {q: by_qid[q]["evidence"] for q in qids}
        if args.mode == "shuffled":
            perm = derangement(len(qids), rng)
            contexts = {q: by_qid[qids[perm[i]]]["evidence"] for i, q in enumerate(qids)}
        for q in qids:
            sessions = [] if args.mode == "empty" else contexts[q]
            items.append({"qid": q, "arm": args.mode, "reference": by_qid[q],
                          "prompt": reader_prompt(by_qid[q], sessions)})
        return items, stats

    rankings = load_rankings(args.rankings)
    rows = {}
    for q in qids:
        rows[q], dropped = rows_for(q, rankings[q])
        stats["cross_scene_rows_dropped"] += dropped
    stored = load_stored(Path(args.lmeb_dir) / "corpus.jsonl", {d for r in rows.values() for d in r})
    for q in qids:
        ref = by_qid[q]
        expanded, preview = [], []
        for d in rows[q]:
            title, text = stored[d]
            content = stored_content(title, text)
            expanded.append((session_date(title), content))
            preview.append((session_date(title), content[:PREVIEW_CHARS]))
        for arm, sessions in (("expanded", expanded), ("preview", preview)):
            if arm in args.arms:
                items.append({"qid": q, "arm": arm, "reference": ref, "rows": rows[q],
                              "prompt": reader_prompt(ref, sessions)})
    return items, stats


def stratified(qids, by_qid, limit, rng):
    """About ``limit`` questions, each type in proportion, drawn with the seed."""
    groups = {}
    for q in qids:
        ref = by_qid[q]
        groups.setdefault("abstention" if ref["abstention"] else ref["question_type"], []).append(q)
    out = []
    for name in sorted(groups):
        members = sorted(groups[name])
        take = max(1, round(limit * len(members) / len(qids)))
        out += rng.sample(members, min(take, len(members)))
    return sorted(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", default="rankings",
                        choices=["rankings", "oracle", "empty", "shuffled", "judge_gold", "judge_other"])
    parser.add_argument("--rankings", help="--dump_rankings output of a retrieval run (mode rankings)")
    parser.add_argument("--arms", default="expanded,preview")
    parser.add_argument("--lmeb_dir", required=True, help="LMEB LongMemEval directory")
    parser.add_argument("--oracle", required=True, help="longmemeval_oracle.json (answers, types, dates)")
    parser.add_argument("--cache_dir", required=True)
    parser.add_argument("--out", required=True, help="per-question JSONL; the summary goes beside it")
    # high: at low and medium this model spends no reasoning tokens, and on the
    # oracle self-test high answered 93 of 99 against 82 at low, the gain in the
    # multi-session and temporal types beyond the reader's own run-to-run noise.
    # A reader that is the bottleneck hides what retrieval changed.
    parser.add_argument("--reader_effort", default="high")
    parser.add_argument("--judge_effort", default="low")
    parser.add_argument("--limit", type=int, default=0, help="stratified subset size (0 = all)")
    parser.add_argument("--rep", type=int, default=0, help="> 0 repeats every call past the cache (A/A)")
    parser.add_argument("--seed", type=int, default=1374)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    args.arms = set(args.arms.split(","))

    references = load_references(args.oracle)
    queries = load_queries(args.lmeb_dir)
    items, stats = build_items(args, references, queries)
    def one(item):
        """A question that still fails after the retries is counted, not fatal to the run."""
        try:
            if args.mode.startswith("judge_"):
                return grade_only(item, args.cache_dir, args.judge_effort)
            return answer_and_grade(item, args.cache_dir, args.reader_effort, args.judge_effort, args.rep)
        except Exception as exc:  # recorded per question; the summary says how many
            return {"qid": item["qid"], "arm": item["arm"], "error": f"{type(exc).__name__}: {exc}"}

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        outcomes = list(pool.map(one, items))
    failed = [o for o in outcomes if "error" in o]
    results = [o for o in outcomes if "error" not in o]
    with open(args.out, "w", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    summary = {"failed": len(failed), "failed_questions": [(f["qid"], f["arm"]) for f in failed],
               "mode": args.mode, "model": MODEL, "reader_effort": args.reader_effort,
               "judge_effort": args.judge_effort, "rep": args.rep, "limit": args.limit,
               "seed": args.seed, "rankings": args.rankings, **stats,
               "accuracy": summarize(results)}
    Path(args.out).with_suffix(".summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False))
    print(json.dumps(summary["accuracy"], indent=1, ensure_ascii=False))
    if failed:
        # Re-running resumes: every completed call is read from the cache.
        print(f"{len(failed)} question(s) failed; the accuracy above excludes them", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
