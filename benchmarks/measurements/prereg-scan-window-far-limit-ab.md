# Pre-registration: a shorter far list

Registered BEFORE any arm with a bounded far list was executed or inspected.
The comparison basis below — instrument, arms, strata, metrics, controls and
decision rule — is fixed by this document; any later deviation must be called
out as an amendment, in this file, with what had already been seen when it was
written.

## Question

`results-scan-window-reach-ab.md` found that a far list of ten rows at equal
vote weight costs the near stratum 6.67 NDCG@10 points at a reach of 200,000,
that three quarters of the displacing rows carried a far-list vote and nothing
else, and — in its exploratory sweep — that the near cost is nearly the same
at a reach of 50,000 (−5.97) as at 300,000 (−8.49). The rows that displace are
priced by their number, not by the depth they were drawn from.

`CPERSONA_VECTOR_FAR_LIMIT` bounds that number: the far list is cut to
`min(limit, FAR_LIMIT)` rows before it reaches the fusion. It is a
candidate-count setting. It changes nothing about how a row is scored or how
lists are weighed, which is what keeps it inside the reach design's scope and
out of the recency-weighted scoring line.

This measurement asks whether a shorter far list removes the near cost
without giving back the far gain:

1. **Cost** — does the near stratum come back within the rule with fewer far
   rows in the fusion?
2. **Benefit** — does the far stratum keep what the full far list bought at
   the same reach?

It does not choose the reach value. Every primary arm holds the reach at
200,000, the value the previous two measurements were judged at, so that the
far-list length is the only thing that moves.

## Instrument

Identical to the previous two measurements: LongMemEval, 237,654 documents,
bge-m3 vectors from the disk cache, scene-blocked store order, seed
`20260903`, twelve disjoint rotations, 240 paired queries per stratum, arms
sharing one database per rotation, the shipped SQLite scan (no contiguous
index), per-query retriever lists recorded.

The harness pins `CPERSONA_VECTOR_FAR_LIMIT` per arm and asserts in-process
that it took effect, as it does for the window and the reach.

## Arms

| Arm | Window | Reach | Far limit | Meaning |
| --- | ---: | ---: | ---: | --- |
| A | 10,000 | off | — | what ships today |
| S | 10,000 | 200,000 | 10 (= `limit`) | the full far list, re-run on these builds so every pair is paired |
| F1 | 10,000 | 200,000 | 1 | one far row |
| F2 | 10,000 | 200,000 | 2 | |
| F3 | 10,000 | 200,000 | 3 | |
| F5 | 10,000 | 200,000 | 5 | |

Regime as before: `rrf`, fused gate on, autocut on, confidence off,
threshold 0.3, local vector search, `limit=10`, no auto-calibration.

## Metrics (fixed)

As before: NDCG@10 (headline, Δ = arm − A per stratum), Recall@10, MRR,
returned count, disturbance, latency (secondary), near-list identity, and
the far-only-votes reading for every arm with a far list.

## Controls

1. **Replicate** — S on these builds must reproduce S of the previous
   measurement to the last digit (same seed, same builds, same code path):
   near 27.51 / far 9.56 NDCG@10, Δ −6.67 / +3.87. Any difference is a change
   in the instrument, not a result, and is diagnosed first.
2. **Near-list identity** — 0 queries whose near list differs between A and
   any F arm. One difference stops the measurement.
3. **Monotone cost** — the near loss must not grow as the far list shortens
   (F1 ≤ F2 ≤ F3 ≤ F5 ≤ S in loss). A shorter list that costs more is a
   defect in the cut, not a finding.
4. **Positive** — the far stratum must move between A and each F arm. A far
   list of one row that changes nothing on the far stratum is a dead arm.

## Decision rule (stated before the run)

Let Δ_near(F) and Δ_far(F) be the change in mean NDCG@10, arm F minus arm A,
and Δ_far(S) the full far list's gain on the same builds (expected +3.87).

- **A shorter far list works** at length *k* if **Δ_near(F_k) ≥ −1.0** and
  **Δ_far(F_k) ≥ Δ_far(S) − 1.0** — the near stratum is within the rule the
  wide window and the full far list both failed, and the far stratum keeps
  the reach's gain to within a point. Where several *k* pass, the largest
  passing *k* is the one carried forward (it keeps the most of the far gain;
  a smaller *k* is not "safer" once the near rule is met).
- **The vote is the problem, not the count** if no *k* passes on the near
  side: even one far row at full weight displaces more than a point. The
  candidate-count knob is then exhausted, and the remaining shape — a
  lighter far vote — is a scoring change and is not decided here.
- **The count trades the gain away** if the near rule is met only at a *k*
  whose far gain has fallen more than a point below the full list's: the far
  answers were not at the top of their own list, and shortening removed them
  with the displacers. Report the *k* at which each side crosses.
- The absolute far bar of the earlier rules (+5.0) is **not** applied here:
  at a reach of 200,000 neither the wide window (+4.93) nor the full far list
  (+3.87) reached it on this instrument, whose far answers sit at depth
  20,000–29,500. That bar belongs to the choice of a reach value, which is a
  later, separate measurement; this one holds the reach fixed and asks only
  whether the length of the far list can be set so the reach stops costing.

## Exploratory (not part of the decision rule)

Registered now so they cannot be added after a result is seen. Six rotations,
separate work directory.

- **F2-50 / F3-50** — reach 50,000 with far limit 2 and 3, against A and S50.
  The previous sweep found 50,000 the better reach for this instrument's far
  answers; this asks whether a short far list at that reach comes out ahead
  on both strata — the reading that would inform a default, without being
  the measurement that chooses one.
- **Far-only votes per arm** — how the displacing rows' composition changes
  with the length. If the far-only share stays near three quarters while the
  count falls, the mechanism is confirmed as a count; if it shifts toward rows
  with two votes, the remaining loss is in the lexical arms' overlap and a
  count cannot reach it.

## Outputs

`results-scan-window-far-limit-ab.md` next to this file, quoting exactly the
metrics above in that order before any exploratory observation, plus the
per-arm JSON with the per-query lists.
