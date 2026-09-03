# Pre-registration: separating reach from the recency prior

Registered BEFORE any arm with the far list enabled was executed or inspected.
The comparison basis below — instrument, arms, strata, metrics, controls and
decision rule — is fixed by this document; any later deviation must be called
out as an amendment, in this file, with what had already been seen when it was
written.

## Question

`results-scan-window-default-ab.md` found that widening the vector scan window
from 10,000 to 200,000 rows costs 20.19 NDCG@10 points on queries whose answer
is recent and buys 4.93 on queries whose answer is old, and traced the cost to
rank displacement inside reciprocal-rank fusion: the window is also an
unpriced recency prior, and widening it removes the prior.

`docs/SCAN_WINDOW_REACH_DESIGN.md` gives the two jobs two settings: the
window stays the **near list** (the newest N rows, ranked exactly as today) and
a new `CPERSONA_VECTOR_REACH` adds a **far list** — the top rows by cosine
among scan positions `[N, REACH)` — as one more ranked list for the fusion.
Every existing list is untouched.

This measurement asks whether that separation does what it claims:

1. **Benefit** — does the far list recover, for queries whose answer lies
   below the window, what the wide window recovered?
2. **Cost** — does the near stratum keep what it has today, now that its own
   list is unchanged and the far rows can only be added?

It does not choose the reach value. It says whether the far list is a
mechanism worth pricing.

## Instrument

Identical to the amended instrument of `prereg-scan-window-default-ab.md`:
LongMemEval, 237,654 documents, bge-m3 vectors from the existing disk cache;
scene-blocked store order with `created_at` strictly monotonic; near scenes
inside the newest 10,000 rows, far scenes at depth 20,000–150,000; twelve
disjoint rotations of the near cohort (240 queries per stratum); each arm on
its own byte-copy of the one database built per rotation. The seed
(`20260903`) and stratum assignment are unchanged, so the arms below rank the
same corpus, in the same order, as the arms already measured.

The vector arm runs the shipped SQLite scan — no contiguous index — as before.
The far list is therefore read through the chunked `LIMIT ? OFFSET ?` path.
That is the slower of the two suppliers; the measurement is about answers, and
the index reproduces the scan's answers by contract, so it need not be present
to measure them.

The harness sets `CPERSONA_VECTOR_REACH` per arm and asserts, in-process,
that the value in effect is the one requested, as it does for the window. An
arm whose setting did not take is a failed arm, not a data point.

## Arms

| Arm | `CPERSONA_MAX_MEMORIES` | `CPERSONA_VECTOR_REACH` | Meaning |
| --- | ---: | ---: | --- |
| A | 10,000 | 0 (off) | what ships today; identical to arm A of the prior measurement |
| S | 10,000 | 200,000 | the separation: today's near list plus a far list reaching where arm B reached |

Arm B of the prior measurement (window 200,000, no far list) is the reference
for the benefit question. It is not re-run: the replicate control of that
measurement showed the instrument reproduces exactly (Δ = ±0.00 on 480 paired
queries), so its figures stand for the same builds.

## Regime

Shipped defaults, unchanged from the prior measurement: `rrf`, fused quality
gate on, autocut on, confidence off, `VECTOR_MIN_SIMILARITY=0.3`, local vector
search, no auto-calibration.

- **R1 (primary)**: `limit=10`.
- **R2 (secondary)**: `limit=100`.

## Metrics (fixed)

As before: NDCG@10 (headline, mean per stratum per arm, Δ = S − A),
Recall@10, returned count, disturbance (share of queries whose top-10 set
changed, share whose order changed, mean Jaccard), latency p50/p95
(secondary). Unweighted mean over queries within a stratum, no maximum
statistics.

One metric is added, because the design makes a claim the others cannot
check: **near-list identity**. For every query in every arm the harness
records the vector retriever's near list (ids and order) and, in arm S, the
far list separately. The near list in arm S must equal the vector list in arm
A for every query. This is the design's "every existing list is untouched"
claim, measured per query rather than argued.

## Controls

1. **Replicate (noise band)** — arm A is run twice on two copies. Expected
   identical; any spread above 0.2 NDCG points re-derives the thresholds
   before either arm is read.
2. **Off-is-identical** — arm A with `CPERSONA_VECTOR_REACH=10000`
   (reach equal to the window) must equal arm A with the setting unset, on
   every metric and every returned list. A reach that equals the window is
   the setting's off state expressed as a number, and it must not run a far
   scan that returns nothing and changes a tie somewhere.
3. **Positive (the far list must matter above the window)** — the far stratum
   must move between A and S. If it does not, the far list is not reaching the
   far rows and the instrument is repaired before any conclusion is drawn.
4. **Near-list identity** — as defined under metrics: zero queries whose near
   list differs between A and S. A single difference is a bug in the
   separation, not a data point, and stops the measurement.

## Decision rule (stated before the run)

Let Δ_far and Δ_near be the change in mean NDCG@10, arm S minus arm A. The
thresholds are the ones the wide window was held to, so that the separation
is judged by the rule it was built to pass.

- **The separation works** if Δ_far ≥ +5.0 **and** Δ_near ≥ −1.0. Raising the
  reach then becomes a change that can be priced; the value is chosen in a
  later, separate measurement.
- **The far list's vote is too strong** if Δ_near < −1.0 while Δ_far ≥ +5.0:
  the near list is unchanged by construction (control 4), so any near loss is
  far rows outranking near rows in the fusion at equal per-list weight. The
  design as written does not pass; the next candidate is a per-list weight,
  which is a scoring change and is not decided here.
- **The far list does not reach** if Δ_far < +5.0: compare against arm B's
  far-stratum figure. If S recovers substantially less than B did with the
  same reach, the far list's `limit` cut or its threshold is losing rows the
  wide window kept, and that is diagnosed before the design is judged.
- Membership change without NDCG change is not degradation, as before.

## Exploratory (not part of the decision rule)

Registered now so they cannot be added after a result is seen:

- **S50** — window 10,000, reach 50,000. The prior sweep found 50,000 the
  better window value; this asks whether the same holds for the reach when
  the near list is preserved, or whether preserving it makes the reach value
  matter less.
- **`limit=100`** — the same arms at the MCP cap, to see whether the deeper
  fusion list changes how far rows and near rows share the top of it.
- **Far-only votes** — among the near-stratum queries that lost NDCG in S,
  how many of the rows that displaced the answer carried only a far-list vote
  (no lexical vote). This is the reading that would justify, or rule out, a
  per-list weight.

## Outputs

`results-scan-window-reach-ab.md` next to this file, quoting exactly the
metrics above in that order before any exploratory observation, plus the
per-arm JSON the driver writes, including the per-query near and far lists.
