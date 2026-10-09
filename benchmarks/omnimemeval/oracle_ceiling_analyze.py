"""Read the gold-evidence reference as registered (benchmarks/measurements/prereg-omnimemeval-lme-oracle-ceiling.md).

For each arm (turns, fit5000): accuracy on all 500 questions and on the 400 test questions, a 95%
Wilson interval on the test 400, accuracy by question type on the test 400, and the mean context in
cl100k_base tokens (the harness's count). Against today's lite on the same 400 questions (the V1.5
`whole` test: the 2.6.5a1 test's answers with the whole run's changed questions laid over them),
the paired difference with a 95% bootstrap interval (10,000 resamples over questions, seed 20261009)
and the questions each side answered alone. The registered reading is printed last.

usage: OMNIMEMEVAL_DIR=<checkout> <harness python> oracle_ceiling_analyze.py
"""
import json
import math
import os
import random
import sys
from collections import defaultdict
from pathlib import Path

import tiktoken

sys.path.insert(0, str(Path(__file__).parent))
from v1_5_build import test_questions  # noqa: E402  the 400 test questions

H = Path(os.environ["OMNIMEMEVAL_DIR"])  # the OmniMemEval checkout the run used
R = H / "results/lme"
ARMS = ("turns", "fit5000")
SEED, B, Z = 20261009, 10_000, 1.959963984540054
TARGET = 360  # 90% of the 400 test questions
enc = tiktoken.get_encoding("cl100k_base")


def judged(d: Path) -> dict:
    out = {}
    for uid, r in json.load(open(d / "cpersona_lme_judged.json")).items():
        r = r[0] if isinstance(r, list) else r
        out[int(uid.rsplit("_", 1)[1])] = (bool(r["llm_judgments"]["judgment_1"]), r["category"])
    return out


def wilson(k: int, n: int) -> tuple[float, float]:
    p, z2 = k / n, Z * Z
    centre, half = (p + z2 / (2 * n)) / (1 + z2 / n), Z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / (1 + z2 / n)
    return 100 * (centre - half), 100 * (centre + half)


def boot(a: dict, b: dict, ids: list[int]) -> tuple[float, float, float]:
    diff = [a[i][0] - b[i][0] for i in ids]
    rng, n = random.Random(SEED), len(ids)
    stats = sorted(sum(diff[rng.randrange(n)] for _ in range(n)) / n * 100 for _ in range(B))
    return sum(diff) / n * 100, stats[int(0.025 * B)], stats[int(0.975 * B) - 1]


def main():
    test = test_questions()
    lite = judged(R / "cpersona-lme1-v15a1-b2800")
    assert sorted(lite) == test, "the 2.6.5a1 test did not answer exactly the test questions"
    lite.update(judged(R / "cpersona-lme1-v15whole-b2800"))
    print(f"today's lite on the test 400: {sum(lite[i][0] for i in test)}/400")
    readings = {}
    for arm in ARMS:
        d = R / f"cpersona-lme1-oracle-{arm}"
        res = judged(d)
        assert sorted(res) == list(range(500)), f"{arm}: not all 500 questions were judged"
        ctx = json.load(open(d / "cpersona_lme_search_results.json"))
        mean_ctx = sum(len(enc.encode(v[0]["search_context"], disallowed_special=())) for v in ctx.values()) / len(ctx)
        k_all, k_test = sum(v[0] for v in res.values()), sum(res[i][0] for i in test)
        lo, hi = wilson(k_test, len(test))
        diff, dlo, dhi = boot(res, lite, test)
        only_arm = sum(res[i][0] and not lite[i][0] for i in test)
        only_lite = sum(lite[i][0] and not res[i][0] for i in test)
        print(f"\n## {arm}\ncontext (cl100k) mean {mean_ctx:.1f}")
        print(f"all 500: {k_all}/500 = {k_all / 5:.2f}%")
        print(f"test 400: {k_test}/400 = {k_test / 4:.2f}% (95% Wilson {lo:.2f} to {hi:.2f})")
        print(f"against today's lite (test 400): {diff:+.2f} points (95% bootstrap {dlo:+.2f} to {dhi:+.2f}); "
              f"right only here {only_arm}, right only in lite {only_lite}")
        by = defaultdict(lambda: [0, 0])
        for i in test:
            by[res[i][1]][0] += res[i][0]
            by[res[i][1]][1] += 1
        print("by type (test 400): " + ", ".join(f"{c} {k}/{n} = {100 * k / n:.1f}%" for c, (k, n) in sorted(by.items())))
        readings[arm] = (k_test, lo, hi)
    k, lo, hi = readings["fit5000"]
    print("\n## Registered reading (fit5000, test 400)")
    if k >= TARGET:
        print(f"{k}/400 >= {TARGET}: this answer model reaches 90% from at most 5,000 tokens of gold evidence "
              f"chosen this way, so 90% is not ruled out for a retrieval that delivers such evidence.")
    else:
        bound = " The interval's upper end is below 90% as well." if hi < 90 else ""
        print(f"{k}/400 < {TARGET}: this answer model stays below 90% even with at most 5,000 tokens of gold "
              f"evidence chosen this way.{bound}")


if __name__ == "__main__":
    main()
