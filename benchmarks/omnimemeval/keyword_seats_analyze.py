"""Read the keyword-seats test as registered (benchmarks/measurements/prereg-keyword-seats.md).

On the 400 test questions. The seats' answer to a question is the point's
(results/lme/cpersona-lme1-kwseats2) when its context changed, and the published answer otherwise:
the context, and so the prompt, is the same. The control is the published run's recorded answers.

- Harm rule: Tango's score interval (95%, two-sided) for the paired difference in accuracy, seats
  minus control. Harm is detected when its upper bound is below 0.
- Beside it: accuracy of each, the discordant pairs, mean retrieved context (cl100k, the harness's
  count), the changed questions, and the answer sessions shown (evidence_metrics.py's functions).

The interval is computed in the (h, d) form: h = the share of discordant pairs, d = gains - losses
over n. For a candidate d0 the restricted maximum-likelihood h has a closed form; the score statistic
is (d - d0) / sqrt((h~ - d0^2) / n), and the interval is every d0 with |statistic| <= z. A self-check
compares the closed form with a numeric maximisation, and the interval's lower bound with the boundary
the registration cites, before anything is read.

usage: OMNIMEMEVAL_DIR=<checkout> EVIDENCE_STORE=<store.db> <harness python> keyword_seats_analyze.py
"""

import json
import math
import os
import sqlite3
import sys
from pathlib import Path

from evidence_metrics import evidence_spans, locate, merged, records
from keyword_seats_build import PUBLISHED, RUN
from v1_5_build import test_questions

H = Path(os.environ["OMNIMEMEVAL_DIR"])
Z = 1.959963984540054  # the 0.975 normal quantile


# The three readers below are count_curve_analyze's and v1_5_analyze's, repeated so this study does
# not need those studies' usage directories in its environment.
def judged(d: Path) -> dict:
    j = json.load(open(d / "cpersona_lme_judged.json"))
    out = {}
    for uid, r in j.items():
        r = r[0] if isinstance(r, list) else r
        out[uid] = (bool(r["llm_judgments"]["judgment_1"]), r["category"], r["nlp_metrics"]["context_tokens"])
    return out


def acc(p: dict) -> float:
    return sum(v[0] for v in p.values()) / len(p) * 100


def ctx(p: dict) -> float:
    return sum(v[2] for v in p.values()) / len(p)


def shown(contexts: dict, uids: list, df, conn) -> tuple[float, float, int]:
    """Answer sessions shown (% per question, averaged) and all shown (%), as evidence_metrics counts them."""
    per, every, unlocated = [], [], 0
    for uid in uids:
        recs = records(conn, uid)
        ans_js, _turns, _missing = evidence_spans(df.iloc[int(uid.rsplit("_", 1)[1])], recs)
        spans, _items, c = locate(contexts[uid], recs, frozenset(ans_js))
        unlocated += c.get("quoted_chars_unlocated", 0)
        m = {j: merged(v) for j, v in spans.items()}
        if ans_js:
            per.append(sum(1 for j in ans_js if j in m) / len(ans_js))
            every.append(all(j in m for j in ans_js))
    return sum(per) / len(per) * 100, sum(every) / len(every) * 100, unlocated


def restricted_h(losses: int, gains: int, n: int, d0: float) -> float:
    hh, dh = (losses + gains) / n, (gains - losses) / n
    a = hh + dh * d0
    disc = a * a - 4 * (dh * d0 - (1 - hh) * d0 * d0)
    return min(1.0, max(abs(d0), (a + math.sqrt(max(disc, 0.0))) / 2))


def score(losses: int, gains: int, n: int, d0: float) -> float:
    dh = (gains - losses) / n
    var = (restricted_h(losses, gains, n, d0) - d0 * d0) / n
    if var <= 0:
        return math.inf if dh > d0 else (-math.inf if dh < d0 else 0.0)
    return (dh - d0) / math.sqrt(var)


def tango(losses: int, gains: int, n: int) -> tuple[float, float, float]:
    """(difference, lower, upper) of Tango's 95% score interval, by bisection on the monotone statistic."""
    dh = (gains - losses) / n

    def root(target: float, lo: float, hi: float) -> float:
        for _ in range(200):
            mid = (lo + hi) / 2
            if score(losses, gains, n, mid) > target:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2

    return dh, root(Z, -1 + 1e-12, dh), root(-Z, dh, 1 - 1e-12)


def self_check() -> None:
    # the closed form is the restricted maximum: compare with a fine grid over h
    for losses, gains, n, d0 in ((7, 3, 400, -0.02), (0, 0, 400, -0.02), (12, 30, 400, 0.01), (5, 1, 150, -0.03)):
        z_ = n - losses - gains
        def ll(h):
            pl, pg = (h - d0) / 2, (h + d0) / 2
            if pl <= 0 or pg < 0 or h >= 1:
                return -math.inf
            return losses * math.log(pl) + (gains * math.log(pg) if gains else 0.0) + z_ * math.log(1 - h)
        grid = max((abs(d0) + k * (1 - abs(d0)) / 200000 for k in range(1, 200000)), key=ll)
        assert abs(grid - restricted_h(losses, gains, n, d0)) < 1e-4, (losses, gains, n, d0)
    # the boundary the registration cites: with no gain, the lower bound clears -0.02 for at most 2 losses
    passes = [k for k in range(0, 10) if tango(k, 0, 400)[1] >= -0.02]
    assert passes == [0, 1, 2], passes


def main():
    self_check()
    sys.path.insert(0, str(H / "scripts"))
    from longmemeval.lme_data import load_lme_dataframe  # the harness's own sanitising loader

    test = set(test_questions())
    control_all = judged(PUBLISHED)
    control = {u: v for u, v in control_all.items() if int(u.rsplit("_", 1)[1]) in test}
    assert len(control) == len(test), "the published run does not hold every test question"
    uids = sorted(control)
    point_dir = H / "results/lme" / RUN
    answered = judged(point_dir)
    pub_ctx = {u: c[0]["search_context"] for u, c in json.load(open(PUBLISHED / "cpersona_lme_search_results.json")).items()}
    moved_ctx = {u: c[0]["search_context"] for u, c in json.load(open(point_dir / "cpersona_lme_search_results.json")).items()}
    assert set(answered) == set(moved_ctx), "the point did not answer exactly its changed questions"
    assert all(moved_ctx[u] != pub_ctx[u] for u in moved_ctx), "a question the point answered did not change"
    seats = {u: answered.get(u, control[u]) for u in uids}
    seats_ctx = {u: moved_ctx.get(u, pub_ctx[u]) for u in uids}

    losses = sum(1 for u in uids if control[u][0] and not seats[u][0])
    gains = sum(1 for u in uids if seats[u][0] and not control[u][0])
    d, lo, hi = tango(losses, gains, len(uids))
    print(f"test questions: {len(uids)}; changed: {len(moved_ctx)}")
    print(f"accuracy: seats {acc(seats):.2f}, control {acc(control):.2f}; "
          f"discordant pairs: {losses} right->wrong, {gains} wrong->right")
    print(f"context (cl100k): seats {ctx(seats):.1f}, control {ctx(control):.1f} ({(ctx(seats) / ctx(control) - 1) * 100:+.1f}%)")

    df = load_lme_dataframe(H / "data/longmemeval/longmemeval_s_cleaned.json", verbose=False)
    conn = sqlite3.connect(f"file:{os.environ['EVIDENCE_STORE']}?mode=ro", uri=True)
    for name, cs in (("seats", seats_ctx), ("control", {u: pub_ctx[u] for u in uids})):
        s_, a_, unlocated = shown(cs, uids, df, conn)
        assert unlocated == 0, f"{name}: {unlocated} quoted characters not found in the store"
        print(f"{name}: answer sessions shown {s_:.1f}%, all shown {a_:.1f}%")

    harm = hi < 0
    print(f"\nharm rule: seats - control {d * 100:+.2f} points, Tango 95% {lo * 100:+.2f} to {hi * 100:+.2f} "
          f"-> {'HARM DETECTED' if harm else 'no harm detected'}")


if __name__ == "__main__":
    main()
