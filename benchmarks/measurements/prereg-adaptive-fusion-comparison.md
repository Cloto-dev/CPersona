# Pre-registration: what the adaptive-fusion comparison can resolve

Registered BEFORE any adaptive rule exists to evaluate. The planning numbers
below come from arms that are already measured — the shipped fusion and a
constant lexical weight — and they fix what the comparison in
`docs/ADAPTIVE_FUSION_DESIGN.md` §2 is able to decide. Any later deviation
must be called out as an amendment, in this file, with what had already been
seen when it was written.

## Question

The design compares four rules against the dense-only order: (a) the shipped
reciprocal rank fusion, (b) dense-first with a constant lexical weight of 0.1,
(c) the square-root mixture of the first derivation, and (d) a conditional
evidence rule. The success condition is fixed by the 2.6 design page: the mean
over the twenty-two tasks of NDCG@10 against the dense-only order must exceed
zero on **both** endpoint models.

Before building (d), this asks a cheaper question: **given the spread of
per-task differences we already measure, how large must an improvement be for
this comparison to see it?** A comparison that cannot resolve the effect being
sought is not worth running, and an effect size discovered after the fact is
not a pre-registration.

## Instrument

The frozen-stage replay (`benchmarks/frozen_replay.py`, described in
[the research note](../../docs/research/frozen-stage-replay-2026-09.md)), whose
per-task aggregates carry `mean.S0_dense` and a lexical-weight sweep
`w_sweep_mean` over {0, 0.1, 0.25, 0.5, 0.75, 1}. Twenty-two tasks, three
embedding models: all-MiniLM-L6-v2 (weakest), jina-embeddings-v5-text-nano
(mid), BAAI/bge-m3 (strong). Tasks with more than 200 queries per subtask are
200-query-per-subtask subsamples. The replay reproduces Track A at its first
stage and the recorded Track B at its last, and matched the live pipeline
row-for-row on every sampled query.

Reproduce with:

```bash
REPLAY_ROOT=~/lmeb python benchmarks/measurements/replay_planning_spreads.py
```

## Assumption, and what refutes it

**The paired spread of (shipped fusion − dense) against (weight 0.1 − dense) is
the planning proxy for the spread of an adaptive rule's difference from a
constant.** It is an assumption, not a measurement of rule (d), which does not
exist. *Refuted if* a development sample of the adaptive rule shows a
materially different paired spread; the numbers below must then be recomputed
before the confirmatory run.

## Planning numbers

Per-task differences in NDCG@10 points against the dense-only order. `sd(a−b)`
is the paired spread that the test has to work against. MDE is the smallest
mean difference a one-sided paired t-test over twenty-two tasks detects at the
stated power; `pow@1pt` is that test's power against a one-point improvement.
Two levels are shown: α = 0.005, the strictest single test in the
pre-registered Holm family of ten directional claims, and α = 0.05, one
unadjusted test — a floor that the real design cannot beat.

| Model | n | mean a−S0 | sd | mean b−S0 | sd | sd(a−b) | MDE 80 % @.005 | MDE 90 % @.005 | pow@1pt @.005 | MDE 80 % @.05 | pow@1pt @.05 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| MiniLM | 22 | +6.6505 | 6.3858 | +3.3867 | 4.2774 | 3.3914 | 2.686 | 3.034 | 9.7 % | 1.859 | 38.0 % |
| jina-v5-nano | 22 | −1.3703 | 3.7900 | +0.5111 | 1.3241 | 2.8120 | 2.227 | 2.516 | 15.0 % | 1.541 | 48.8 % |
| bge-m3 | 22 | −0.8781 | 3.2149 | +0.6060 | 1.0127 | 2.5793 | 2.043 | 2.308 | 18.5 % | 1.414 | 54.6 % |

Two readings, both binding on the design:

1. **A one-point improvement is not detectable.** Even at the most favourable
   level the test has 38–55 % power against it, and at the level the family
   actually uses, 10–19 %. The comparison resolves improvements of roughly two
   to three points.
2. **That is not hopeless, because the gap being chased is larger.** The
   per-query oracle — choosing, per query, the better of the dense-only and
   fused orders — sits three to six points above either constant policy on
   every task family. A rule that captures a large part of it is visible here;
   a rule that captures a little of it is not, and this comparison will not
   tell those apart from no effect at all.

The endpoint conjunction makes it stricter still: both models must clear zero,
so the effective power for a shipping decision is below the per-model figures
above, not equal to them.

## Decision rules fixed here

- **An inconclusive result is reported as unresolved.** "No significant
  advantage" is not "the constant is equivalent"; the latter needs a declared
  equivalence margin and its own test, and neither exists.
- **Nothing is added after the numbers are seen** — no fifth arm, no larger
  panel, no repeated evaluation. A new estimator is a new pre-registration.
- **The comparator is the dense-only order**, never the admitted list or the
  gated list. An improvement over a stage we already know is defective is not
  evidence for a fusion rule.

## By-product worth recording

The same table shows the model dependence the line exists to remove, in the
arms that already exist: the shipped fusion is +6.65 against the dense order on
the weakest model and −1.37 and −0.88 on the other two, while the constant 0.1
is positive on all three but small on the two stronger models. No constant
serves all three, which is the fixed-weight problem stated as a measurement
rather than as a claim.
