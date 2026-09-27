# Pre-registration: rsf on a fixed channel scale (bug-247) — which constants, and does it hold ground?

Registered before any variant was run on LongMemEval. The instrument, variants,
selection rule and decision rule below are fixed by this document; a later
change is written under "Amendments" with what had been seen when it was
written.

## What is being fixed

`rsf` min-max normalised each channel against the rows a query retrieved, and
the quality gate compared the fused score with an absolute threshold. Two
consequences were deterministic: the weakest row of the only channel it
appeared in scored 0.0 and was dropped however similar it was, and a lone weak
hit scored 1.0 and passed however weak (issue registry, bug-247). The fix puts
each channel on a fixed scale (`cpersona/memory_handlers.py`, `_fixed_norm`): a
cosine as it is, a keyword score `s` (−bm25) as `s / (s + H)`. Tests in
`tests/test_bug247_rsf_scale.py` pin both defects and fail on the old scale for
the reasons named.

Two constants are chosen by measurement: `H` (`RSF_LEXICAL_HALF`, the keyword
score that counts half a vote) and the divisor of the fused sum
(`RSF_DIVISOR`): `none` (the plain sum) or `present` (the channels the row
appeared in); `active` (the channels that returned a row) is the old divisor.

The fix is a bug fix: **no improvement is claimed**. The measurement asks
whether it holds ground in the production regime.

## Instrument

`benchmarks/rsf_scale_measure.py` at the commit that adds this file.

- LMEB LongMemEval (500 questions, six types). Every scene stored in its own
  channel at the times in its session titles; recall inside the question's
  channel, `limit=10`, `CPERSONA_RECALL_MODE=rsf`, autocut and the fused gate at
  the build's defaults.
- The vector threshold is calibrated once and held for every variant; the fused
  gate is recalibrated for every variant (median of the default five draws),
  because each variant puts the fused score on its own scale, as a deployment
  recalibrates when the scoring version moves. Each variant's gate is recorded.
- Queries split 50/50 within each type by `trackb_instrument.split_queries`
  with seed **20260928** (not the seed of any earlier measurement).
- Metric: NDCG@10 per question over the rows returned, against LMEB's
  relevance file (`longmemeval_by_type.ndcg_at_k`).

## Variants

`legacy` (min-max, the `active` divisor — the scale before this fix) and the
eight fixed-scale variants `{present, none} × H ∈ {1, 2, 4, 8}`.

`active` and `all` are left out before any question is run. Under them a row
found by one channel is divided by the number of *other* channels that returned
something, so a strong vector-only row falls under a cosine-scale gate — the
heuristic gate a store uses before its fused gate is calibrated. That was
observed, not assumed: with `active`, `tests/test_recall_trace.py` recalls
nothing under the shipped gate for a seeded store whose best row has cosine
0.80. Under `present` and `none` a row in one channel keeps that channel's
score, so the cosine-scale gate still reads it.

## Selection on dev

On the dev half, the variant with the highest macro mean NDCG@10 over the six
types is chosen. Variants within 0.10 points of the best are ties, broken in
this order: divisor `none` before `present` (under `present` a keyword match can
lower a row's mean, so a row found by both channels can rank below one found by
the vector alone); then the smaller `H`.
The chosen constants become the code's defaults before the test half is read.

## Decision on test (one run, judged once)

On the test half, the chosen variant against `legacy`, paired per question:

1. **No type falls**: in every type, the questions whose NDCG@10 fell minus
   those whose NDCG@10 rose are at most max(2, ⌈0.05 n⌉).
2. **The mean holds**: the macro mean NDCG@10 falls by less than 1.0 point.

Both hold: the fix ships with the chosen constants. Either fails: this scale is
not shipped as it is, and the result goes back for a decision.

## Reported, not part of the rule

- Per type: mean NDCG@10 for `legacy` and the chosen variant, up/down counts,
  mean rows returned, and each variant's calibrated fused gate.
- The dev table of all nine variants.
- After the decision: LMEB Track B (22 tasks, `rsf`, `--fast`) for `legacy`
  (the checkout before the fix) and the fix, macro and per task.

## Seen before registration

The code of the fix and its unit tests (which show the two defects on the old
scale). Two runs of the instrument over the full store were stopped during
calibration, before any question was asked: the first calibration takes about
twelve minutes on the pooled store, and the Track B accelerator, tried to
shorten it, left the fused gate uncalibrated without a word, so it is not used.
A smoke run over the scenes of two dev questions checked the wiring. No variant
has been run on the dev or test questions.

## Amendments

(none)
