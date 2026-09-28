# Pre-registration: the price of a far vote

Registered before any arm with a far weight was run on the instrument below,
apart from the mechanical smoke run described under "Seen before
registration". The instrument, arms, split, selection rule and decision rule
are fixed by this document; a later change is written under "Amendments" with
what had been seen when it was written.

## Question

The reach separation ([results](results-scan-window-reach-ab.md)) kept the
vector arm's near list exactly as it ships, and added a far list reaching
200,000 rows. At equal weight the far list cost 6.67 NDCG@10 points on queries
whose answer is recent and bought 3.87 on queries whose answer is old. Three
quarters of the rows that displaced a recent answer carried a far-list vote
and no other.

`CPERSONA_PRIOR_FAR_WEIGHT` (2.6.0a7) prices a far vote at `w` of a near one,
in both fusions ([design](../../docs/PRIOR_FUNCTION_DESIGN.md#2-the-prior)).
This measurement asks whether some `w` keeps the recent answers and keeps what
the far list buys, as the [far-vote plan](../../docs/REACH_AND_RECENCY_PLAN.md#64-what-is-pre-registered-before-any-arm-runs)
registered in outline. It chooses the weight's default. It does not choose a
reach: the reach stays off by default, and the plan moves the reach, the far
list's length and the weight together, later, in one change.

What a caller gains: with the reach set above the window, the default price of
a far vote. That default has no effect while the reach is off.

## Instrument

The instrument of `prereg-scan-window-reach-ab.md`, unchanged: LongMemEval,
237,654 stored documents, bge-m3 vectors from the existing disk cache, the
scene-blocked store order with `created_at` strictly monotonic, near scenes
inside the newest 10,000 rows, far scenes at depth 20,000–150,000, twelve
disjoint rotations of 20 near and 20 far queries each, seed `20260903`. The
vector arm is the shipped SQLite scan with no contiguous index.

Harness: `scan_window_ab.py` at the commit that adds this file, arms
`M1_SPEC`, identity pairs `M1_IDENTITY`. The build under test is `master` at
that commit. It includes bug-442: a far weight of 0 no longer asks for the far
list, so `w = 0` is the reach turned off by construction.

The record's figures for arms A and S came from an older build, and recall has
changed since then. This run re-measures A and S and reads every weight
against them. The record's figures are printed beside for orientation only.
The plan's "both ends reproduce arms A and S to the digit" is read as the
identity controls below: each end must equal, on this build, the arm it is
defined to equal.

## Arms

All at `CPERSONA_MAX_MEMORIES = 10,000`, `limit = 10`, the far list at full
length (`CPERSONA_VECTOR_FAR_LIMIT = 0`). Each arm asserts in-process that
every setting it asked for took effect, and records it.

| Arm | Reach | `w` | Regime | Role |
| --- | ---: | ---: | --- | --- |
| A | off | unset | shipped | the reach off; baseline |
| A-rep | off | unset | shipped | replicate |
| W0 | 200,000 | 0 | shipped | must equal A |
| W25 | 200,000 | 0.25 | shipped | candidate |
| W50 | 200,000 | 0.5 | shipped | candidate |
| W75 | 200,000 | 0.75 | shipped | candidate |
| S | 200,000 | unset (1) | shipped | the unpriced far list |
| W100 | 200,000 | 1.0 | shipped | must equal S |
| W875, W90, W95 | 200,000 | 0.875, 0.9, 0.95 | shipped | exploratory |
| A-p | off | unset | production | baseline of its regime |
| W0-p … W75-p | 200,000 | 0 … 0.75 | production | as above |
| S-p | 200,000 | unset (1) | production | as above |

**Regimes.** *Shipped*: the defaults — `rrf`, fused gate on, autocut on,
confidence off, threshold 0.3 — which is what the record was measured under.
*Production*: `rsf` with the confidence scorer on, as the plan requires once
the final re-sort was settled (§6.3; since 2.6.0a7 the confidence score neither
orders nor gates). The fused gate is uncalibrated in every arm, as in every
earlier run of this harness. The confidence scorer writes on recall, so each
production arm reads its own copy of the rotation's database.

## Split

Rotations 0–5 are **dev** (120 near and 120 far queries): the weight is chosen
there. Rotations 6–11 are **test**: the chosen weight is judged there, once.
All twelve rotations run in one pass. The dev selection is committed before
the test half is scored. Pooled figures are reported and do not decide.

The split exists because the rule selects among five weights. Selecting and
judging on the same questions favours passing.

## Rule

For an arm `W` under one regime, with that regime's A and S:

- `Δnear(W)` = mean NDCG@10 of W minus A, near stratum
- `Δfar(W)` = mean NDCG@10 of W minus A, far stratum

A weight **passes** when

1. `Δnear(W) ≥ −1.0` — the recent answers keep what the reach off gives them,
   and
2. `Δfar(W) ≥ Δfar(S) − 1.0` — the far stratum keeps, within a point, what the
   unpriced far list buys.

The absolute far bar of the reach measurement (+5.0) is not applied here. It
belongs to the measurement that chooses a reach.

**Selection on dev (shipped regime).** The candidates are `w ∈ {0, 0.25, 0.5,
0.75, 1}`, read from W0, W25, W50, W75 and S. Among those that pass, the one
with the highest `Δfar` is chosen. Candidates within 0.10 of it are ties,
broken by the higher `Δnear`, then by the larger `w` (the smaller change from
today's default). If none passes, the default stays at 1 and the test half
decides nothing.

**Decision on test, once.** The chosen `w` must pass on the test half under
the shipped regime **and** under the production regime. If it passes both, the
default of `CPERSONA_PRIOR_FAR_WEIGHT` becomes `w`. If it passes only one, the
default stays at 1 and the split is reported: one setting governs both
fusions, and production runs `rsf`.

## Controls, read before any outcome

1. **Replicate**: A and A-rep return identical rows for every query. Any
   difference stops the measurement.
2. **A weight of 0 is the reach off**: A = W0 and A-p = W0-p, every query. A
   difference is a bug, not a data point.
3. **The default written as a number**: S = W100, every query.
4. **Near-list identity**: the vector arm's near list is the same in A and in
   every reach arm, every query. The weight acts in the fusion, after both
   lists exist.
5. **Positive**: the far stratum moves between A and S. If it does not, the
   far list is not reaching the far rows, and nothing here can be priced.
6. **The weight acts**: at least one query's rows differ between S and W75. If
   none does, the setting did not reach the fusion, and the run is repaired
   before anything is read.

## Prediction, stated before the run

Under `rrf`, `k = 60` and the per-list depth at `limit = 10` is 10, so a vote
is worth between `1/61` (rank 0) and `1/70` (rank 9). A far vote at rank 0 is
worth `w/61`. It outranks a single vote at rank 9 only if `w > 61/70 ≈ 0.871`.
So for every candidate from 0.25 to 0.75, a row whose only vote is a far vote
cannot outrank any row that holds one vote from another list, and such rows
fill the ten places only when fewer than ten rows hold one. The contest that
cost the recent answers in the record (a far-only row against a near row with
one vote) is therefore priced only between 0.871 and 1.

Expected under the shipped regime: W25, W50 and W75 lose little on the near
stratum and differ from each other only in rows holding two or more votes.
Their far gain comes only from far answers that also hold a lexical vote. If
most of what S bought came from far-only answers, no candidate passes the
second condition, and the price that would is in the exploratory interval.

Under `rsf` each channel is on a fixed scale and the far channel adds `w`
times a row's cosine, so the same contest moves continuously with `w`.

## Reported, not part of the rule

- The full table for every arm, stratum and half: NDCG@10, Recall@10, MRR,
  rows returned, and the paired Δ with better/worse counts.
- **Exploratory weights** W875, W90 and W95, which sit where the arithmetic
  above puts the change under `rrf`. They cannot move the rule. If one of them
  would pass where no candidate does, that is a reason to register a follow-up
  measurement, not a result.
- **The far-only reading**, per arm: among near-stratum queries that lost
  NDCG@10 against A, the displacing rows and how many carried only a far vote.
  A weight is supposed to shrink that count, and a count cannot show it.
- Latency p50/p95, recorded but not compared with the record: this run shares
  the machine with other jobs.
- The reach-50,000 companion of the plan is not part of this run. It can be
  run afterwards as its own matrix, with fewer rotations, and it cannot move
  this decision.

## Outputs

`results-far-weight-sweep.md` next to this file. It quotes the controls first,
then the dev table and the selection, then the test decision, then the pooled
and exploratory readings, plus the per-arm JSON the harness writes.

## Seen before registration

- The bug-442 fix and its tests. They show that `w = 0` used to leave far rows
  in the fusion at a score of 0, which `rrf`'s cosine-reading gate admitted.
- The arithmetic under "Prediction".
- One mechanical smoke run of rotation 0: the build and all seventeen arms.
  It checked only that each arm finished with the settings it asked for, that
  the production copies were removed, and whether the identity pairs returned
  the same rows. No NDCG, recall, rank or answer position was computed. The
  registered run rebuilds rotation 0 from scratch in its own directory.

No weight has been compared with another on this instrument.

## Amendments

### 1 — the finer weights become candidates (2026-09-28, before any outcome was read)

Seen: no outcome. The registered run had started and had written one arm file
(rotation 0, arm A). No file had been scored or read. The run was stopped,
this amendment was written, and the run resumed with that file kept.

Changed:

- **The candidates are `w ∈ {0, 0.25, 0.5, 0.75, 0.875, 0.9, 0.95, 1}`**
  (W0, W25, W50, W75, W875, W90, W95, S). The three finer weights were
  registered as exploratory. Under `rrf`, the arithmetic under "Prediction"
  places the contest the record's loss came from between 0.871 and 1, and
  none of the plan's candidates lies there. The rule would then choose among
  weights that the prediction says behave alike. Selecting on dev and judging
  once on test protects a larger candidate set just as it protects a smaller
  one. This run uses 480 of the instrument's 500 questions, so a later
  confirmatory run of the finer weights would have no fresh questions.
- **Production arms W875-p, W90-p and W95-p are added**, so that every
  candidate can be judged under both fusions, as the rule requires.

The rule, the tie-breaking, the split and the controls are unchanged. The
finer weights listed as exploratory under "Reported, not part of the rule"
are candidates now.
