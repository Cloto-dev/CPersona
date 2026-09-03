# Pre-registration: raising the shipped scan-window default

Registered BEFORE any A/B arm was executed or inspected. The comparison basis
below — instrument, arms, strata, metrics, controls and decision rule — is
fixed by this document; any later deviation must be called out as an
amendment, in this file, with what had already been seen when it was written.

## Question

`CPERSONA_MAX_MEMORIES` is the vector retriever's scan window: the number of
newest rows the vector arm ranks per recall (bug-085). It ships at 10,000.
On a corpus of ~150,000 rows that leaves roughly 93% of the corpus invisible
to the vector arm — silently, since the lexical arms still answer.

Raising the shipped default is **not** a pure relaxation.
`docs/CONTIGUOUS_INDEX_DESIGN.md` §2 states the reason plainly: a scan that
ranks more rows than today returns *different* answers, and a different answer
is a different feature that needs its own gate. A wider window enlarges the
candidate pool that reciprocal-rank fusion, the fused quality gate and autocut
all operate on. "Reaches further back" and "admits more contamination" are two
faces of the same change.

This measurement answers, on the real store/recall paths:

1. **Benefit** — for queries whose answer lies outside the 10,000-row window,
   how much does a wider window recover?
2. **Cost** — for queries whose answer lies *inside* the 10,000-row window,
   what does the larger pool do to the answer they already got?

It does not choose the new default value; it says whether raising it is safe
and worth it at the value tested.

## Why the dataset's own order cannot answer question 2

LongMemEval is 237,655 documents. Stored in dataset-file order, the shallowest
relevant document of a query sits inside the newest 10,000 rows for **21 of
500 queries** (measured before designing this instrument; 0 of 78, 21 of 133,
0 of 56, 0 of 30, 0 of 70 and 0 of 133 across the six subtasks).

So the natural order has essentially no "answer already inside the window"
stratum. An A/B on it would measure only the side that can improve, and would
report "no degradation" from a stratum that is empty — a detector with no
detection power, not evidence of safety.

## Instrument

The store order is therefore **designed**, and this is the instrument's main
assumption. A benchmark corpus has no true chronology in the database sense —
every document is stored in one run — so recency is an authored property here
either way. This design authors it explicitly rather than inheriting the file
order by default.

- The 500 queries are split 50/50 into two strata by a seeded shuffle
  (`--seed 20260903`), before any arm is run.
- **near** — every relevant document of these queries is placed inside the
  newest 10,000 rows. Both arms can see them.
- **far** — every relevant document of these queries is placed at depth
  20,000–150,000: outside the narrow window, inside the wide one.
- Every remaining document is filler, seeded-shuffled, filling the rest of the
  depth range including beyond 200,000.
- `created_at` is stamped strictly monotonic with store order. Without this the
  column's `datetime('now')` default has one-second resolution, a bulk store
  puts thousands of rows on the same value, and the window boundary falls
  inside a tie block — the cut would not be reproducible between arms.

Known distortion, stated up front: moving a query's relevant documents to a
different depth also separates them from the other documents of their scene,
which are their nearest distractors. The strata therefore differ from each
other in more than depth. This is why the primary readings are **within**
stratum (arm B vs arm A on the same queries), never near vs far.

Embeddings are read from the existing bge-m3 disk cache (verified 237,655 /
237,655 documents and 500 / 500 queries present), so no model is loaded and
nothing is re-encoded. The vector arm runs the shipped SQLite scan — no
accelerator patch, no contiguous index — which is what a default install runs.

## Arms

One database is built once. Each arm runs against its own byte-copy of it, so
the arms rank an identical corpus by construction rather than by a rebuild
that is trusted to be deterministic.

| Arm | `CPERSONA_MAX_MEMORIES` | Meaning |
|-----|------------------------:|---------|
| A | 10,000 | what ships today |
| B | 200,000 | the candidate default |

## Regime

Shipped defaults, not benchmark doctrine: `rrf`, fused quality gate **on**,
autocut **on**, confidence off, `VECTOR_MIN_SIMILARITY=0.3`, local vector
search. The truncation layers are the mechanisms a larger pool is suspected of
disturbing, so the usual "turn them off for a pure ranking metric" convention
would remove the effect being measured.

- **R1 (primary)**: `limit=10` — the MCP default, the shape a real recall has.
- **R2 (secondary)**: `limit=100` — the MCP cap, same gates. Shows rank
  movement that R1's cut hides.

No `--auto_calibrate`: its sampling is the harness's only run-to-run noise
source, and both arms must differ in the window alone.

## Metrics (fixed)

Per query, from the returned list, against the dataset's own qrels:

- **NDCG@10** — headline. Reported as a mean per stratum per arm, and Δ = B − A.
- **Recall@10** — share of a query's relevant documents that came back.
- **Returned count** — how many rows survived the gate and autocut.
- **Disturbance** (B vs A, same query): share of queries whose top-10 set
  changed; share whose order changed; mean Jaccard of the two sets.
- **Latency** p50/p95 of the recall call, per arm (secondary; informative for
  the documentation change that follows, not part of the decision rule).

No maximum statistics anywhere. Aggregation is the unweighted mean over
queries within a stratum.

## Controls

1. **Replicate (noise band)** — arm A is run twice on two separate copies of
   the database. With calibration off and the corpus fixed, the two runs are
   expected to agree exactly. Any spread found is reported and, if it exceeds
   0.2 NDCG points, the decision thresholds below are re-derived from it
   before either arm is read.
2. **Negative control (the window must not matter below itself)** — both
   window values are run over LoCoMo (5,882 documents, smaller than either
   window). The results must be identical. This is the claim that raising the
   default is free for small corpora, measured rather than asserted.
3. **Positive control (the window must matter above itself)** — if the far
   stratum shows no difference between the arms, the instrument is presumed
   broken and is repaired before any conclusion is drawn. A wider window that
   changes nothing where the answer is provably outside the narrow one is a
   dead detector, not a safe change.

## Decision rule (stated before the run)

Let Δ_far and Δ_near be the change in mean NDCG@10, arm B minus arm A.

- **Adopt** — raise the default — if Δ_far ≥ +5.0 **and** Δ_near ≥ −1.0.
- **Do not adopt as-is** if Δ_near < −1.0: the wider pool costs answers that
  today's window gets right. Report which mechanism moved them (gate, autocut
  or fusion rank) and settle that before the default changes.
- **Reconsider the value, not the direction**, if Δ_far < +5.0 while
  Δ_near ≥ −1.0: the window is not where this corpus's reach is decided, and
  the change is close to free but also close to pointless at this size.
- A change in top-10 membership without a change in NDCG is **not** counted as
  degradation. "The answer moved" and "the answer got worse" are different
  claims and only the second one is priced here.

## Outputs

`results-scan-window-default-ab.md` next to this file, quoting exactly the
metrics above in that order before any exploratory observation, plus the
per-arm JSON the driver writes. The driver, the seed and the stratum
assignment are committed with this document so the strata cannot be re-drawn
after seeing an arm.

## Amendment 1 (2026-09-03, after rotation 0, before the pooled result)

Two changes, and what had been seen when they were written.

**Seen at the time**: the smoke run on a small corpus, and rotation 0 of the
primary matrix (20 queries per stratum). Both showed the same direction —
the far stratum gains, the near stratum loses more than the −1.0 threshold —
and the replicate arm reproduced arm A exactly, so the noise band is zero.
The pooled twelve-rotation result had not been computed.

**1. The instrument became scene-blocked and rotated.** The design registered
above placed a query's relevant documents at a depth and left the rest of its
scene wherever the shuffle put it. Measured on the dataset, a query's own
scene is its hardest competition, so that layout would have compared "answer
plus its competitors" against "answer without them" and charged the
difference to the window. The layout now moves each scene as one block: near
scenes sit inside the narrow window, far scenes below it, and both arms see a
query's competitors either way. A 10,000-row window holds only 20 of the ~475
document scenes, so one build measures 20 near queries and the cohort rotates
over twelve disjoint builds (240 per stratum). Everything else — arms,
regime, metrics, controls, decision rule — is unchanged.

**2. Two exploratory readings are added.** Neither may move the decision rule
above; both exist to explain a result, not to grade it.

- **Window sweep** — a third window value between the two arms. If the near
  cost is graded in the window rather than a step, a smaller raise may buy
  most of the far gain; that is the question the next change (choosing the
  value) will ask, and it cannot be answered by two points.
- **`--limit 100`** — the same matrix at the MCP cap. At `limit=10` a
  displaced answer and a lost answer look identical. Rotation 0 showed the
  gate and autocut trimmed nothing (every call returned exactly ten rows, no
  gate fallback), so whatever moved the answers moved them by rank; this pass
  says how far they moved.
