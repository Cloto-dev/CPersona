"""Read the count curve as registered (benchmarks/measurements/prereg-omnimemeval-lme-count-curve.md).

Noise check: the full point against the published arm B (cpersona-lme1). A point holds when
the 95% bootstrap interval of the paired accuracy difference k - full (10,000 resamples over
questions, seed 20261003) has its lower bound at or above -2.0 points.
Context Tokens = API-reported answer-stage prompt tokens averaged over usage-reported calls,
summed over every pass's saved token-usage file.
"""
import json
import os
import random
from pathlib import Path

H = Path(os.environ["OMNIMEMEVAL_DIR"])  # the OmniMemEval checkout the run used
BK = Path(os.environ["COUNT_CURVE_USAGE_DIR"])  # each pass's token_usage_answer.json, copied aside as k<k>-pass<n>.json
POINTS = ["1", "2", "3", "4", "5", "6", "8", "10", "full"]
SEED, B, MARGIN = 20261003, 10_000, -2.0


def judged(d: Path) -> dict:
    j = json.load(open(d / "cpersona_lme_judged.json"))
    out = {}
    for uid, r in j.items():
        r = r[0] if isinstance(r, list) else r
        out[uid] = (bool(r["llm_judgments"]["judgment_1"]), r["category"], r["nlp_metrics"]["context_tokens"])
    return out


def api_tokens(k: str):
    calls = prompt = 0
    for p in sorted(BK.glob(f"k{k}-pass*.json")):
        m = json.loads(p.read_text())["modules"]["ANSWER"]
        calls += m["usage_reported_call_count"]
        prompt += m["prompt_tokens"]
    return calls, prompt


def boot(a: dict, b: dict) -> tuple[float, float, float]:
    ids = sorted(a)
    assert ids == sorted(b), "points answer different question sets"
    diff = [a[i][0] - b[i][0] for i in ids]
    rng = random.Random(SEED)
    n = len(ids)
    stats = sorted(sum(diff[rng.randrange(n)] for _ in range(n)) / n * 100 for _ in range(B))
    return sum(diff) / n * 100, stats[int(0.025 * B)], stats[int(0.975 * B) - 1]


def acc(p: dict) -> float:
    return sum(v[0] for v in p.values()) / len(p) * 100


def main():
    pub = judged(H / "results/lme/cpersona-lme1")
    pts = {k: judged(H / f"results/lme/cpersona-lme1-k{k}") for k in POINTS}
    full = pts["full"]
    agree = sum(pub[i][0] == full[i][0] for i in pub) / len(pub) * 100
    noisy = abs(acc(full) - acc(pub)) > 2.0
    print(f"noise check: published {acc(pub):.2f} / full re-answered {acc(full):.2f} / "
          f"per-question agreement {agree:.1f}% -> {'TOO NOISY' if noisy else 'ok'}")
    cats = sorted({v[1] for v in full.values()})
    print("| k | accuracy | k - full (95% interval) | holds | context (cl100k) | Context Tokens (API) | per correct | "
          + " | ".join(cats) + " |")
    smallest = None
    for k in POINTS:
        p = pts[k]
        calls, prompt = api_tokens(k)
        ctx = sum(v[2] for v in p.values()) / len(p)
        correct = sum(v[0] for v in p.values())
        api = prompt / calls if calls else float("nan")
        per = prompt / correct if correct and calls == len(p) else float("nan")
        d, lo, hi = boot(p, full)
        holds = k != "full" and lo >= MARGIN
        if holds and smallest is None:
            smallest = k
        by = []
        for c in cats:
            vs = [v[0] for v in p.values() if v[1] == c]
            by.append(f"{sum(vs) / len(vs) * 100:.1f}")
        print(f"| {k} | {acc(p):.2f} | {d:+.2f} ({lo:+.2f} to {hi:+.2f}) | {'yes' if holds else ('—' if k == 'full' else 'no')} | "
              f"{ctx:.1f} | {api:.1f} ({calls} calls) | {per:.0f} | " + " | ".join(by) + " |")
    print(f"smallest k that holds: {smallest}" + (" (study too noisy: no claim)" if noisy else ""))


if __name__ == "__main__":
    main()
