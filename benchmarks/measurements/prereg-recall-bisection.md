# Recall regression bisection

Registered 2026-09-15 before new endpoint measurements.

> First committed on 2026-09-19. Unlike the
> [version-comparison registration](prereg-version-comparison-2440-to-2512.md),
> no commit predates the measurements, so the line above is the only record of
> when this was written.

Value: instrumentation that separates a code-caused retrieval regression from
random calibration drift. This is diagnostic work, not a shipped accuracy claim.

Primary instrument: REALTALK, all three subtasks, macro NDCG@10 in percentage
points, full pooled corpus ranking followed by the existing scene filtering.
Use the existing BAAI/bge-m3 float32 embedding cache read-only. No inference,
network embedding requests, or calibration resampling. Fix the threshold to
0.6602 (the recorded development endpoint), RRF, confidence/autocut/fused gate
off, scan and library limit 300000. Retain each fresh experimental database.

First reproduce v2.4.40 and e43ad34 using the same driver. Classify <40.5 as
regressed, >=42.0 as baseline-like, otherwise indeterminate. Proceed with these
cutoffs only if the endpoints bracket them and their difference is >=3 points.
Otherwise investigate the instrument/threshold sensitivity before bisection.
Never label a 1-2 point step the cause of the historical 5.03 point loss.

Measure chronological checkpoints 68653f3, 5499613, a5206f6, 5fcc1fc, eafd3af
until a transition is found; narrow the affected interval to a runtime change.
5499613 is a version-only checkpoint and eafd3af adds benchmark validation,
not runtime indexing. Do not attribute causality to their commit messages.

Confirm a suspected mechanism on the same development endpoint by reversing
only that mechanism. Require >=3 point restoration, baseline-like score, and
improvement in all three subtasks. Report the residual against the controlled
old endpoint. This proves the measured mechanism only, not all 22-task loss.

Then confirm LongMemEval, full regime, six types, fixed threshold 0.4493.
Compare unchanged development and intervention with identical corpus/embeddings.
Predeclared historical fingerprint: user, temporal, multi-session improve;
report all six types even if that fingerprint fails. No significance claim
from a single controlled pair. No 22-task rerun unless attribution needs it.

The existing NumPy accelerator may be used with seeded native self-checks.
Require nonzero checked calls and zero mismatches; otherwise results are invalid
until resolved. It bypasses the native vector scan/index, so cannot by itself
exonerate those paths. A native confirmation is required for any implicated
scan/index mechanism. Record source hashes, effective configuration, input
hashes, coverage, per-query ranking evidence, and acceleration diagnostics.

The pre-2.5.0 literal `_clamp_limit(limit, 100)` is bypassed only for the
full-ranking measurement, matching the existing historical benchmark adapter.
These results are not production limit=10 measurements or latency claims.
