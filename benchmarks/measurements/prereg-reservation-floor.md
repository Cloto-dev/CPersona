# Pre-registration — a floor for the reserved rows

Registered before any number below was read. What it costs to put a similarity
floor on the reservation is measured here; whether to add one is decided by the
rule fixed in this file, not after the table is seen.

## The question

The reservation holds the dense arm's top `k` rows from *before* the admission
floor, and appends them — marked `fallback: true`, never ahead of a qualified
row — when the qualified rows fall short of `k`. It has no floor of its own.

So on a small corpus the reservation returns whatever the corpus has. The
behaviour golden's no-hits scenario, four rows and a query related to none of
them, now returns all four; one of them sits at similarity **−0.038**.

Zero is not "unrelated". For normalised vectors zero is *orthogonal*, and a
negative value is *anti-correlated* — a row that is further from the query than
an average unrelated row is. Whether that makes zero a defensible line is a
question about the score distributions the shipping encoders actually produce,
which is measurable, so it is measured rather than argued.

## What is measured

Similarity here is exactly what the pipeline ranks by: the dot product of the
stored vector and the query vector, through the same call the retrieval path
makes. **M0 checks the vectors are unit-norm**, because every reading below
calls that dot product a cosine, and if the norms are not one the line at zero
means something else.

- **M1 — what zero means.** The distribution of that similarity over
  query × row pairs that are *not* answers, per model, over the benchmark
  corpora. Reported: `P(s <= 0)`, the percentile zero sits at, and the
  distribution's location and spread.
- **M2 — what a floor at zero would cost.** The same similarity over the
  *gold* pairs (`qrels > 0`). Reported per task and per model: the number of
  gold pairs at `s <= 0` (a count, not only a rate) and the minimum gold
  similarity.
- **M3 — how often the reservation fires.** From the frozen-replay dumps
  already on disk: the share of queries whose admitted set is smaller than the
  reservation target, and the share whose *eligible universe* is smaller than
  it. These are different regimes — with fewer rows in the corpus than the
  reservation holds, every row is reserved and no floor changes what comes
  back, it only shortens the answer.

Sampling, fixed here: up to 200 queries per subtask (the depth the current
sweep runs at); every gold pair of those queries for M2; 500 rows drawn
uniformly without replacement from the non-gold rows of the same corpus per
query for M1. Seed 1344, recorded in the result.

## The decision rule

**Adopt a floor at zero** if and only if both hold:

1. **It removes no answers.** Zero gold pairs at `s <= 0`, in *both* shipping
   models, across every task measured.
2. **Zero is a tail line, not a bulk cut.** `P(s <= 0)` among non-answer pairs
   is below 1 % in both shipping models. A floor there then excludes rows that
   are extreme even among unrelated ones, rather than cutting into the body of
   the distribution.

**Reject the floor** if either fails, and say which. A gold pair below zero
means the floor deletes an answer to buy tidiness. A `P(s <= 0)` in the percents
means zero is a large undeclared threshold for that model — the kind of constant
this line exists to remove, not to add.

The weak reference encoder has **no veto**: it is not a shipping slot, and its
numbers are reported as an endpoint, not as a condition. (Same convention as the
lexical-weight default, and for the same reason.)

**Tie-break, registered before the grid is seen:** if condition 1 holds and
condition 2 lands near its line (1–2 %), **no floor**. The default is to add no
constant; a constant has to earn its place, and a borderline reading is not
earning it.

## Limits, registered in advance

- These corpora are English. A model's similarity scale is not guaranteed to
  survive a change of language, so a floor adopted here carries a Japanese
  check with it before it becomes a default.
- The corpora are thousands of rows; the case that raised the question is four.
  M3 says how much of the traffic sits in each regime, and no floor helps the
  small one — there, the reservation is the whole corpus.
- Not measured, because it is not a measurement: whether a caller would rather
  see a short answer than an anti-correlated one. This file bounds what a floor
  would cost in answers; the preference is a separate judgement.
