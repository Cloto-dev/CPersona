"""Evidence metrics for reconstruct on OmniMemEval's LongMemEval-S: which answer evidence a context shows.

Accuracy alone cannot tell "shown the evidence and still wrong" from "never shown it and right
anyway". This reads a run's saved contexts against the stored records and the dataset's answer
labels, with no search and no model call.

How a quote is traced to its record. The adapter stores one haystack session as one record whose
message id ends in "_lme_exper_session_<j>", j the session's position in haystack_sessions, so
haystack_session_ids[j] names it. A context block is "[<as_of>]\\n<head quote>[\\n…\\n<excerpt>]";
a quote is verbatim record text, passages joined by " … " in text order (excerpts.SEPARATOR). Each
passage is found in the agent's records by exact substring search, in order within a part; the
head part is tried first on the records whose timestamp is the block's as_of. Passages that are
found nowhere are counted, never guessed.

Answer evidence. The dataset's answer_session_ids name the answer sessions; within them the turns
marked has_answer are the evidence turns (the same turns the harness lists as answer_evidences).
A turn's span is its content inside "<role>: <content>" in the stored record.

Per question:
  sessions_shown   answer sessions with at least one quoted character / answer sessions
  turns_touched    evidence turns overlapping a quoted span / evidence turns
  turn_chars       quoted characters inside evidence turns / evidence-turn characters
  evidence_share   quoted characters inside answer sessions / quoted characters (located)
  per_1k           answer sessions shown per 1,000 context tokens (the judge's cl100k count)
  dup_share        quoted characters quoting what another span already quoted / quoted characters
  first_item       1-based position of the first item quoting an answer session (None if none)

usage: OMNIMEMEVAL_DIR=<checkout> EVIDENCE_STORE=<store.db> \\
       <harness python> evidence_metrics.py [run ...]  (default: cpersona-lme1)
       EVIDENCE_OUT=<dir> also writes one JSON line per question per run.
"""
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

from count_curve_build import PREFIX, split

VERSION = "lme1"  # the published run's version; the count-curve points keep its user ids
PART_SEP, PASSAGE_SEP = "\n…\n", " … "
HEADER_LINE = re.compile(r"^\[([^\]\n]+)\]$")
SESSION_SUFFIX = re.compile(r"_lme_exper_session_(\d+)$")


def records(conn, agent):
    """j -> (content, timestamp) for one question's stored sessions."""
    out = {}
    for msg_id, content, ts in conn.execute(
            "SELECT msg_id, content, timestamp FROM memories WHERE agent_id = ?", (agent,)):
        m = SESSION_SUFFIX.search(msg_id)
        assert m, f"unexpected message id {msg_id!r}"
        out[int(m.group(1))] = (content, ts)
    return out


def evidence_spans(row, recs):
    """Answer-session positions, and the evidence turns' content spans [(j, start, end, role)]."""
    answer_ids = set(row["answer_session_ids"])
    ans_js = [j for j, sid in enumerate(row["haystack_session_ids"]) if sid in answer_ids]
    turns, missing = [], 0
    for j in ans_js:
        if j not in recs:
            continue  # an empty session is not stored
        text, cursor = recs[j][0], 0
        for t in row["haystack_sessions"][j]:
            content = str(t.get("content") or "").strip()
            if not content:
                continue
            needle = f"{t['role']}: {content}"
            at = text.find(needle, cursor)
            if at < 0:
                missing += t.get("has_answer", False)
                continue
            cursor = at + len(needle)
            if t.get("has_answer"):
                s = at + len(t["role"]) + 2
                turns.append((j, s, s + len(content), t["role"]))
    return ans_js, turns, missing


def locate(ctx, recs, answer_js=frozenset()):
    """Quoted spans per record and the records each item quotes.

    Returns ({j: [(s, e)]}, [set of j per item], checks). A part found in more than one record is
    counted in checks["parts_ambiguous"], and in checks["parts_ambiguous_evidence"] when those
    records disagree on being an answer session (the choice could change a metric).
    """
    spans, items = defaultdict(list), []
    checks = defaultdict(int)
    for block in split(ctx):
        header, _, rest = block.partition("\n")
        m = HEADER_LINE.match(header)
        assert m, f"block without a header: {header[:60]!r}"
        as_of = m.group(1)
        body = rest[:-1] if rest.endswith("\n") else rest
        seen = set()
        for pi, part in enumerate(body.split(PART_SEP)):
            passages = [p for p in part.split(PASSAGE_SEP) if p]
            order = sorted(recs, key=lambda j: (pi > 0 or recs[j][1] != as_of, j))
            hits = []
            for j in order:
                text, cursor, found = recs[j][0], 0, []
                for p in passages:
                    at = text.find(p, cursor)
                    if at < 0:
                        break
                    found.append((at, at + len(p)))
                    cursor = at + len(p)
                else:
                    hits.append((j, found))
            n = sum(map(len, passages))
            if not hits:
                checks["quoted_chars_unlocated"] += n
                continue
            checks["quoted_chars_located"] += n
            if len(hits) > 1:
                checks["parts_ambiguous"] += 1
                if len({j in answer_js for j, _ in hits}) > 1:
                    checks["parts_ambiguous_evidence"] += 1
            j, found = hits[0]
            spans[j].extend(found)
            seen.add(j)
        items.append(seen)
    return spans, items, checks


def merged(spans):
    out = []
    for s, e in sorted(spans):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def overlap(a, s, e):
    return sum(max(0, min(e, y) - max(s, x)) for x, y in a)


def judged(H, run):
    j = json.load(open(H / "results/lme" / run / "cpersona_lme_judged.json"))
    out = {}
    for uid, r in j.items():
        r = r[0] if isinstance(r, list) else r
        out[uid] = (bool(r["llm_judgments"]["judgment_1"]), r["category"], r["nlp_metrics"]["context_tokens"])
    return out


def per_question(H, run, df, conn):
    ctxs = json.load(open(H / "results/lme" / run / "cpersona_lme_search_results.json"))
    grades = judged(H, run)
    rows, checks = [], defaultdict(int)
    for i, row in df.iterrows():
        agent = f"lme_exper_user_{VERSION}_{i}"
        recs = records(conn, agent)
        checks["records"] += len(recs)
        checks["sessions"] += sum(1 for s in row["haystack_sessions"]
                                  if any(str(t.get("content") or "").strip() for t in s))
        ans_js, turns, missing = evidence_spans(row, recs)
        checks["evidence_turns_unlocated"] += missing
        ctx = ctxs[agent][0]["search_context"]
        assert ctx.startswith(PREFIX)
        spans, items, c = locate(ctx, recs, frozenset(ans_js))
        for k, v in c.items():
            checks[k] += v
        quoted = sum(e - s for v in spans.values() for s, e in v)
        dup = quoted - sum(e - s for v in spans.values() for s, e in merged(v))
        m = {j: merged(v) for j, v in spans.items()}
        shown = [j for j in ans_js if j in m]
        touched = [t for t in turns if overlap(m.get(t[0], []), t[1], t[2]) > 0]
        t_chars = sum(e - s for _, s, e, _ in turns)
        t_cov = sum(overlap(m.get(j, []), s, e) for j, s, e, _ in turns)
        in_ans = sum(e - s for j in ans_js for s, e in m.get(j, []))
        first = next((k + 1 for k, seen in enumerate(items) if seen & set(ans_js)), None)
        correct, cat, ctx_tokens = grades[agent]
        rows.append({
            "agent": agent, "question_id": row["question_id"], "category": cat, "correct": correct,
            "abstention": str(row["question_id"]).endswith("_abs"),
            "answer_sessions": len(ans_js), "sessions_shown": len(shown),
            "evidence_turns": len(turns), "turns_touched": len(touched),
            "evidence_turn_roles": sorted({t[3] for t in turns}),
            "touched_roles": sorted({t[3] for t in touched}),
            "turn_chars": t_chars, "turn_chars_quoted": t_cov,
            "quoted_chars": quoted, "answer_session_chars_quoted": in_ans, "dup_chars": dup,
            "context_tokens": ctx_tokens, "items": len(items), "first_item": first,
        })
    return rows, checks


def mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else float("nan")


def summarise(rows):
    q = [r for r in rows if r["answer_sessions"]]
    t = [r for r in rows if r["evidence_turns"]]
    quoted = sum(r["quoted_chars"] for r in rows)
    return {
        "n": len(rows),
        "accuracy": mean(r["correct"] for r in rows) * 100,
        "sessions_shown": mean(r["sessions_shown"] / r["answer_sessions"] for r in q) * 100,
        "all_sessions_shown": mean(r["sessions_shown"] == r["answer_sessions"] for r in q) * 100,
        "turns_touched": mean(r["turns_touched"] / r["evidence_turns"] for r in t) * 100,
        "turn_chars": mean(r["turn_chars_quoted"] / r["turn_chars"] for r in t if r["turn_chars"]) * 100,
        "evidence_share": sum(r["answer_session_chars_quoted"] for r in rows) / quoted * 100 if quoted else float("nan"),
        "per_1k": mean(r["sessions_shown"] / r["context_tokens"] * 1000 for r in q if r["context_tokens"]),
        "dup_share": sum(r["dup_chars"] for r in rows) / quoted * 100 if quoted else float("nan"),
        "context_tokens": mean(r["context_tokens"] for r in rows),
        "per_correct": (sum(r["context_tokens"] for r in rows) / sum(r["correct"] for r in rows)
                        if any(r["correct"] for r in rows) else float("nan")),
        "no_evidence_turns": len(rows) - len(t),
    }


COLS = [("n", "{:.0f}"), ("accuracy", "{:.2f}"), ("sessions_shown", "{:.1f}"), ("all_sessions_shown", "{:.1f}"),
        ("turns_touched", "{:.1f}"), ("turn_chars", "{:.1f}"), ("evidence_share", "{:.1f}"),
        ("per_1k", "{:.2f}"), ("dup_share", "{:.2f}"), ("context_tokens", "{:.1f}"), ("per_correct", "{:.0f}")]


def table(title, groups):
    print(f"\n{title}\n| group | " + " | ".join(c for c, _ in COLS) + " |")
    print("| --- |" + " --- |" * len(COLS))
    for name, rows in groups:
        s = summarise(rows)
        print(f"| {name} | " + " | ".join(f.format(s[c]) for c, f in COLS) + " |")


def main():
    H = Path(os.environ["OMNIMEMEVAL_DIR"])
    store = os.environ["EVIDENCE_STORE"]  # opened read-only
    out = os.environ.get("EVIDENCE_OUT")
    sys.path.insert(0, str(H / "scripts"))
    from longmemeval.lme_data import load_lme_dataframe  # the harness's own sanitising loader

    runs = sys.argv[1:] or ["cpersona-lme1"]
    df = load_lme_dataframe(H / "data/longmemeval/longmemeval_s_cleaned.json", verbose=False)
    conn = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
    for run in runs:
        rows, checks = per_question(H, run, df, conn)
        print(f"\n## {run}\nchecks: {dict(checks)}")
        cats = sorted({r["category"] for r in rows})
        table("overall / by correctness", [("all", rows), ("correct", [r for r in rows if r["correct"]]),
                                           ("wrong", [r for r in rows if not r["correct"]])])
        table("by question type", [(c, [r for r in rows if r["category"] == c]) for c in cats])
        q = [r for r in rows if r["evidence_turns"]]
        cell = defaultdict(int)
        for r in q:
            cell[(r["turns_touched"] == r["evidence_turns"], r["correct"])] += 1
        print("\nevidence turns all touched x correct (questions with evidence turns):")
        for shown in (True, False):
            print(f"  all touched={shown}: correct {cell[(shown, True)]}, wrong {cell[(shown, False)]}")
        firsts = [r["first_item"] for r in rows if r["answer_sessions"]]
        hist = defaultdict(int)
        for f in firsts:
            hist[f] += 1
        print("first item quoting an answer session:", dict(sorted(hist.items(), key=lambda kv: (kv[0] is None, kv[0] or 0))))
        if out:
            Path(out).mkdir(parents=True, exist_ok=True)
            with open(Path(out) / f"{run}.jsonl", "w") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
