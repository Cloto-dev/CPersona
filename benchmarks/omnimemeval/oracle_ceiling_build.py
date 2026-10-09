"""Build answer inputs from LongMemEval's gold evidence: how well the answer model does when retrieval is perfect.

Registration: benchmarks/measurements/prereg-omnimemeval-lme-oracle-ceiling.md.

No search runs and CPersona is not involved. Each question's context is made from its own answer
sessions in LongMemEval's oracle file (longmemeval_oracle.json, matched to the harness's questions by
question_id), rendered as the CPersona adapter renders an item (the record's time in brackets, then the
stored session text: its date line and one "role: content" paragraph per turn), and wrapped by the
harness's own context template. Two arms:

- turns:   only the turns marked has_answer, under their session's date line. An answer session with
           no marked turn contributes nothing (the 21 questions with no marked turn at all are all
           abstention questions).
- fit5000: the has_answer turns, then the same sessions' other turns nearest to them by turn distance
           (ties: earlier session, then earlier turn), each kept only if the whole context stays
           within 5,000 cl100k_base tokens. An answer session with no marked turn grows from its
           first turn, as if a mark sat just before it, since such sessions still carry dates.

Kept turns are shown in session order; a gap between kept turns is marked with the adapter's part
separator. Sessions are ordered by date. Every arm writes all 500 questions into its own results
directory, the published arm B entries with search_context replaced, so the harness answers and
judges them all (--from-step 3). --check-only prints sizes and writes nothing.

usage: OMNIMEMEVAL_DIR=<checkout> <harness python> oracle_ceiling_build.py <longmemeval_oracle.json> <turns|fit5000|all> [--check-only]
"""
import json
import os
import shutil
import statistics
import sys
from datetime import datetime
from pathlib import Path

import tiktoken

H = Path(os.environ["OMNIMEMEVAL_DIR"])  # the OmniMemEval checkout the run used
sys.path.insert(0, str(H / "scripts"))
from longmemeval.lme_data import load_lme_dataframe  # noqa: E402  the harness's own sanitising loader
from utils.search_helpers import format_search_context  # noqa: E402  the harness's own context template

enc = tiktoken.get_encoding("cl100k_base")
tok = lambda s: len(enc.encode(s, disallowed_special=()))  # noqa: E731  the harness's count
PUB = H / "results/lme/cpersona-lme1"
CAP = 5000
ARMS = ("turns", "fit5000")
PART_SEP = "\n…\n"  # the adapter's separator between a head quote and an excerpt


def iso(date: str) -> str:
    return datetime.strptime(date, "%Y/%m/%d (%a) %H:%M").strftime("%Y-%m-%dT%H:%M:00+00:00")


def block(date: str, turns: list[dict], keep: set[int]) -> str:
    """One item: the session's time, its date line, then the kept turns in order."""
    body, last = "", None
    for j in sorted(keep):
        sep = "" if last is None else ("\n\n" if j == last + 1 else PART_SEP)
        body += sep + f"{turns[j].get('role', 'user')}: {str(turns[j].get('content', '')).strip()}"
        last = j
    return f"[{iso(date)}]\nSession date: {date}\n\n{body}\n"


def context(sessions, keeps) -> str:
    return format_search_context([block(d, s, k) for (d, s), k in zip(sessions, keeps) if k])[0]


def build(entry: dict, arm: str) -> str:
    sessions = sorted(zip(entry["haystack_dates"], entry["haystack_sessions"]), key=lambda x: iso(x[0]))
    marked = [{j for j, t in enumerate(s) if t.get("has_answer")} for _, s in sessions]
    keeps = [set(m) for m in marked]
    if arm == "turns":
        return context(sessions, keeps)
    order = sorted(
        (min(abs(j - a) for a in marked[n]) if marked[n] else j + 1, n, j)
        for n, (_, s) in enumerate(sessions) for j in range(len(s)) if j not in marked[n]
    )
    for _, n, j in order:
        keeps[n].add(j)
        if tok(context(sessions, keeps)) > CAP:
            keeps[n].discard(j)
    return context(sessions, keeps)


def main():
    oracle = {e["question_id"]: e for e in json.load(open(sys.argv[1]))}
    arms = ARMS if sys.argv[2] == "all" else (sys.argv[2],)
    assert set(arms) <= set(ARMS), f"unknown arm: {sys.argv[2]}"
    df = load_lme_dataframe(H / "data/longmemeval/longmemeval_s_cleaned.json", verbose=False)
    pub = json.load(open(PUB / "cpersona_lme_search_results.json"))
    matched = {}
    for uid, conv in pub.items():
        row = df.loc[int(uid.rsplit("_", 1)[1])]
        entry = oracle[row["question_id"]]
        assert entry["question"] == row["question"] == conv[0]["question"], f"{uid}: the question differs"
        matched[uid] = entry
    print(f"matched {len(matched)}/{len(pub)} questions to the oracle by question_id")
    for arm in arms:
        ctx = {uid: build(e, arm) for uid, e in matched.items()}
        n = [tok(c) for c in ctx.values()]
        print(f"{arm}: context cl100k mean {statistics.mean(n):.1f} median {statistics.median(n):.0f} "
              f"max {max(n)} total {sum(n):,} | over {CAP}: {sum(x > CAP for x in n)}")
        if "--check-only" in sys.argv:
            continue
        dst = H / "results/lme" / f"cpersona-lme1-oracle-{arm}"
        if dst.exists():
            sys.exit(f"stop: {dst} exists")
        dst.mkdir(parents=True)
        # a number, as the harness wrote it: the judge stage adds it to the answer's duration
        out = {uid: [{**conv[0], "search_context": ctx[uid], "search_duration_ms": 0.0}] for uid, conv in pub.items()}
        json.dump(out, open(dst / "cpersona_lme_search_results.json", "w"), ensure_ascii=False, indent=4)
        shutil.copy2(PUB / "cpersona_lme_search_status.json", dst / "cpersona_lme_search_status.json")
        (dst / ".step_1_done").touch()
        (dst / ".step_2_done").touch()
        print(f"{arm}: wrote {dst.name} ({len(out)} questions)")


if __name__ == "__main__":
    main()
