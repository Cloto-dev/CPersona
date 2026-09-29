# Pre-registration: rsf's gate on a fixed scale, its order unchanged (bug-247, second design)

Registered before the design below is implemented or run. The design, the
instruments, the controls and the decision rules are fixed by this document; a
later change is written under "Amendments" with what had been seen when it was
written.

## Why a second design

bug-247 is a defect in how `rsf`'s quality gate reads the fused score. `rsf`
normalises each channel by min-max over the rows a query retrieved, and the gate
compares the result with an absolute threshold. The weakest row of the only
channel it appears in therefore scores 0.0 and is always dropped, and a lone weak
hit scores 1.0 and always passes.

The first fix ([results](results-rsf-fixed-scale.md)) put every channel on a fixed
scale, and used that scale both to **order** the fused list and to **gate** it. It
passed its registered rule at ten rows on LongMemEval, but LMEB Track B scored it
lower on 19 of 22 tasks, and it was withdrawn before release. That regime could
not separate order from admission. The defect, though, is only about admission:
nothing in bug-247 says that the min-max order is wrong.

The second design changes only what the gate reads.

## The design

`_recall_rsf` gives every row two scores:

- `_rsf_score`, **unchanged**: the min-max fused sum divided by the number of
  active channels. It orders the list, exactly as in 2.6.0a8.
- `_rsf_gate_score`, **new**: the first fix's fixed scale, with its constants.
  A cosine counts as itself, clamped to [0, 1]. A keyword score `s` counts as
  `s / (s + 8)`. The far channel is weighted by `CPERSONA_PRIOR_FAR_WEIGHT`, and
  the sum is not divided. An all-None lexical channel (the LIKE fallback) casts
  a full vote, as in both scales.

The quality gate's `rsf` branch and `_gate_score()` read `_rsf_gate_score` when
a row has one. Calibration measures the value the gate compares through
`_gate_score()`, so it follows without a change of its own. The episode-boundary
penalty (off by default) scales both scores. The prior's age weight (identity
by default) scales only the order score; the rule that the prior never rewrites
the score the gate reads stays as it is.

`SCORING_VERSION` moves, because the value the fused gate is calibrated on
changes. A deployment recalibrates at startup (`CPERSONA_CALIBRATE_ON_MODEL_CHANGE`,
on by default). `rrf` is untouched.

The constants (divisor `none`, `H = 8`) are those the first fix chose on its dev
half. **They are not chosen again here.** No selection happens in this
measurement, so it has no dev half.

## What the design must satisfy before it is measured

These are tests in the implementing change, each proven by mutation, not
measurements:

1. **Order identity.** With the gate open (threshold 0, fused gate off,
   autocut off), the full `rsf` ranking is identical to 2.6.0a8's for every
   query in the unit fixtures.
2. **The two defects are gone.** The weakest row of a strong single-channel set
   passes a gate its cosine clears, and a lone weak hit is gated like any other
   row. These are the first fix's own tests, rewritten against `_rsf_gate_score`.
3. **The gate reads the gate score.** A row whose two scores fall on opposite
   sides of the threshold is admitted or dropped by `_rsf_gate_score`.

## Instruments

**T — LMEB Track B, deterministic.** The `run_trackb.sh` regime (22 tasks,
`--recall_mode rsf`, `--fast`, calibrated fused gate and autocut off,
pool-size gate on), except that `--auto_calibrate` is **not** passed: the
vector threshold stays at `--min_similarity 0.3` in both arms. Auto-calibration
samples at random and puts about ±1–2 NDCG@10 points of run-to-run noise on a
task mean ([README](../README.md), measurement regime, item 3). Holding the
threshold fixed makes each arm deterministic, so any difference between the
arms is the change. The arms are `legacy` (the parent commit of the
implementing change) and `gate` (that change), bge-m3, the existing embedding
cache.

**L — LongMemEval at ten rows.** The instrument of
[`prereg-rsf-fixed-scale.md`](prereg-rsf-fixed-scale.md), as amended there: all
scenes stored with their session times, recall inside each question's channel,
`limit = 10`, `rsf`, the vector threshold calibrated once and held, the fused
gate unset in both arms. The harness `benchmarks/rsf_scale_measure.py` gains a
`gate` variant in the implementing change. All 500 questions are used, because
nothing is selected. The first fix's test half was read once, for a different
change (order and gate together). That is disclosed here, not hidden.

## Controls, read first

1. **Determinism of T.** `legacy` is run twice on three tasks (LoCoMo, EPBench,
   REALTALK). Identical NDCG@10 is expected. A difference stops the
   measurement: the fixed threshold did not remove the randomness.
2. **Order identity on real data.** On L, `gate` and `legacy` rank the same
   rows in the same order wherever both return a row at a rank. The design
   changes admission only, so this holds for every question, or the
   implementation is wrong.

## Decision rules

**Rule T (the regression gate).** Let Δ be `gate` minus `legacy` in mean
NDCG@10.

1. The macro mean over the 22 tasks falls by less than 0.20 points.
2. No task falls by 1.00 point or more.

The thresholds are narrow because T is deterministic. They are not the ±1–2
points of the calibrated regime.

**Rule L (holding ground at ten rows),** the first fix's rule, unchanged:

1. **No type falls:** in each of the six question types, the questions whose
   NDCG@10 fell minus those whose NDCG@10 rose are at most max(2, ⌈0.05 n⌉).
2. **The mean holds:** the macro mean NDCG@10 over types falls by less than
   1.0 point.

**Outcome.**

- If both rules hold, the design is the bug-247 fix. Which release carries it
  is decided separately. It is a scoring change and moves `SCORING_VERSION`,
  so it is not a beta-line fix.
- If either rule fails, the design is not shipped, the result is reported, and
  bug-247 stays open.

No improvement is claimed either way. The measurement asks whether the fix
holds ground.

## Reported, not part of the rule

- T in the standard regime, with `--auto_calibrate`, one run per arm, for
  comparison with the first fix's Track B. Its noise is stated beside it.
- Per task and per type: rows the gate admitted and dropped in each arm, and
  rows returned.
- On L: the questions where `gate` returns more rows than `legacy`, and whether
  the added rows contain an answer.

## Limits known in advance

- The calibrated fused gate, which a deployment runs, is not measured. On L the
  first fix's calibration succeeded or failed by chance
  ([amendment 1](prereg-rsf-fixed-scale.md#amendments)). Both arms run under the
  heuristic gate, as the first fix's did.
- Production runs `rsf` with the confidence scorer on. Since 2.6.0a7 that
  scorer neither orders nor gates, so the `rsf` branch of the gate is the one
  that decides. The measurement runs with confidence off.

## Seen before registration

The first fix's code, its tests, its registered measurement on L and its Track B
table, which reads order and admission together. The gate and order scores have
not been separated in any run. No arm of this design has been implemented or
run.

## Amendments

**Amendment 1** (2026-09-29). Seen when written: the implementation and its
tests; control 1's LoCoMo and EPBench, identical in both replicas (46.51 and
90.34). No `gate` arm of either instrument had run.

1. **Control 2 is read as relative order.** The rows both arms return must
   appear in the same relative order. The gate may drop a row the legacy arm
   admitted, or admit one it dropped, and either shifts every later position,
   so a position-by-position comparison would flag questions whose order is
   unchanged.
2. **How T is run.** One harness process per task, because a long-lived
   process slows its searches without changing NDCG. `--fast` on the numpy
   backend on the CPU, which is exact, as the first fix's Track B ran.
   `--dump_rankings` is on, for the rows reported per task.
3. **How L's `legacy` is built.** It is the implementing checkout with
   `_rsf_gate_score` removed after fusion, not the parent commit, so both
   variants run in one process against one store and one calibration, as the
   instrument requires. A row without a gate score is gated on `_rsf_score`
   as in 2.6.0a8, and a unit test pins that.
