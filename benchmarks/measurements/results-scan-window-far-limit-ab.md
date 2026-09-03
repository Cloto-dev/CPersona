# Results: a shorter far list

Pre-registration: `prereg-scan-window-far-limit-ab.md`. Harness:
`scan_window_ab.py`. Run 2026-09-04.

**Verdict, by the pre-registered rule: no length passes.** With the reach
held at 200,000, cutting the far list from ten rows to one reduces the near
stratum's loss from 6.67 to 2.73 NDCG@10 points, and 2.73 is still below the
−1.0 the rule asked for. The rule's second branch applies: **the vote is the
problem, not the count.** Even one far row at full weight displaces more
than a point, so the candidate-count knob is exhausted, and the remaining
shape — a lighter far vote — is a scoring change that this measurement does
not decide.

## What was measured

Six arms on the instrument of the two previous measurements (LongMemEval,
237,654 documents, scene-blocked order, seed `20260903`, twelve rotations,
240 paired queries per stratum, shipped regime, `limit=10`): the shipped
setting (A), the full far list at a reach of 200,000 (S), and the same reach
with the far list cut to one, two, three and five rows (F1–F5) by
`CPERSONA_VECTOR_FAR_LIMIT`. Each arm asserted in-process that the window,
the reach and the far-list limit took effect.

## Controls

| Control | Expectation | Measured |
|---|---|---|
| Replicate (S on these builds vs the previous run) | to the digit | **to the digit** — near 27.51 / far 9.56, Δ −6.67 ± 0.85 / +3.87 ± 0.88, 0 better / 84 worse and 38 / 11 |
| Near-list identity (A vs every F) | 0 differ | **0 / 240** on every arm, both strata |
| Monotone cost (loss must not grow as the list shortens) | F1 ≤ F2 ≤ F3 ≤ F5 ≤ S | **holds** — 2.73 ≤ 4.35 ≤ 5.06 ≤ 5.61 ≤ 6.67 |
| Positive (the far stratum must move for every F) | moves | **moves** — +1.32 to +3.25 |

## Primary result (`limit=10`)

| Arm | Far rows | near NDCG@10 | Δ near | far NDCG@10 | Δ far | p50 |
|---|---:|---:|---:|---:|---:|---:|
| A | — | 34.18 | — | 5.69 | — | 310 ms |
| F1 | 1 | 31.45 | **−2.73 ± 0.48** | 7.01 | +1.32 ± 0.70 | 590 ms |
| F2 | 2 | 29.82 | −4.35 ± 0.63 | 7.56 | +1.87 ± 0.79 | 601 ms |
| F3 | 3 | 29.12 | −5.06 ± 0.70 | 7.93 | +2.24 ± 0.85 | 600 ms |
| F5 | 5 | 28.56 | −5.61 ± 0.75 | 8.94 | +3.25 ± 0.91 | 609 ms |
| S | 10 | 27.51 | −6.67 ± 0.85 | 9.56 | +3.87 ± 0.88 | 621 ms |

Near-stratum queries made worse: 76 / 79 / 81 / 82 / 84 of 240 for F1
through S; none made better in any arm. Every call returned ten rows and no
gate fallback fired.

Against the rule — Δ_near ≥ −1.0 and Δ_far ≥ Δ_far(S) − 1.0 = +2.87 — the
near side is failed by every length, and the far side is met only by F5,
whose near loss is 5.61. The two sides do not cross: no length keeps the
far gain and spares the near stratum.

## Mechanism: the first far row always carries the maximum single vote

The far-only reading, per arm:

| Arm | near losers | displacing rows | far-only |
|---|---:|---:|---:|
| F1 | 76 | 62 | 56 |
| F2 | 79 | 128 | 113 |
| F3 | 81 | 192 | 168 |
| F5 | 82 | 220 | 187 |
| S | 84 | 251 | 187 |

The displacing rows fall with the length, as a count should make them, and
the far-only share stays near nine tenths, so the mechanism is confirmed as
a count of far votes — but the number of *queries* that lose barely moves:
76 with one far row against 84 with ten. F1 has fewer displacing rows than
losing queries. A single far row is enough to make most of the losers lose.

The reason is arithmetic. Reciprocal-rank fusion gives the first row of any
list `1 / (k + 1)`, the largest vote a single list can cast. The far list's
first row therefore ties the near list's first row and beats every near row
of rank two or below that holds no second vote. A recent answer at near
rank three with no lexical vote is pushed to fourth by a far list of one row,
and the discount at that step is what −2.73 measures. Shortening the list
removes the second through tenth far votes, which is the difference between
−6.67 and −2.73; it cannot remove the first, because a list of one row is
still a list whose first row votes at full strength.

That is what "the vote is the problem, not the count" means concretely. The
count decides how many near answers are pushed how far; the weight of the
first far vote decides whether any are pushed at all. No candidate count
reaches the first vote.

## Cost

Unchanged from the reach measurement: the far region is read whatever the
cut, so p50 is about double the shipped setting's at this reach (310 → 590–
620 ms). A shorter list saves a hydrate of a few rows and nothing else.

## Limitations

As before: one dataset, one embedding model, one fusion mode (`rrf`), an
authored near/far mix, scene blocks as an authored recency structure. The
far-only reading counts rows, not points.

## Exploratory (not part of the decision rule)

Six rotations, 120 paired queries per stratum, reach 50,000 — where the reach
sweep put the largest far-stratum gain — with the far list at two, three and
ten rows. Arm A re-run on these builds.

| Arm | Reach | Far rows | Δ near | Δ far | far-only / displacing |
|---|---:|---:|---:|---:|---:|
| F2-50 | 50,000 | 2 | −3.84 ± 0.80 | +7.03 ± 1.57 | 59 / 60 |
| F3-50 | 50,000 | 3 | −4.59 ± 0.89 | +8.67 ± 1.65 | 96 / 99 |
| S50 | 50,000 | 10 | −5.97 ± 1.02 | +10.20 ± 1.81 | 114 / 125 |

The same shape at the better reach: shortening the list trims both sides,
the near stratum never comes within −1.0, and the displacing rows are
far-only almost to the last one (59 of 60 at two rows). Pooled over the
authored 50/50 mix every reach-50,000 arm is ahead of the shipped setting
(+1.6 to +2.1 points), which is the reading that makes a reach worth
pricing — and the near column is the price, unchanged in kind by the count.

## What this measurement does not settle

It does not choose a weight; it establishes that one is needed. The far
list can be made to reach, and made short, and still its first vote is a
full vote, and a full vote from below the window displaces the recent
answers the window was quietly protecting. The remaining design — a far vote
worth less than a near vote — is a change to what the fusion computes, and
`docs/REACH_AND_RECENCY_PLAN.md` says how the next line makes it, with this
instrument, between two arms this record already contains: a weight of zero
is arm A, and a weight of one is arm S.
