# Pre-registration: does conditional lexical evidence exist, and how large is it

Registered BEFORE the row-keyed dump is written and before any number from it is
read. Any later deviation must be recorded as an amendment in this file,
together with what had already been seen when the amendment was written.

## Why this is asked now

The conditional-evidence fusion mode of
[the design](../../docs/ADAPTIVE_FUSION_DESIGN.md#d3-fusion-one-knob-now-one-mode-later-no-fitted-weight-ever)
scores a row by the joint density ratio

\[
S^\star(q,d)=\log\frac{h_q(v_d,l_d)}{f_{0q}(v_d,l_d)} .
\]

The sampling recipe first written for it was wrong: the null and the population
were the same distribution, so the ratio was identically one. Separating them
needs labels, an independently justified null population, or a structural
assumption. Only the first exists on this benchmark, and the design therefore
names a **labelled diagnostic** as the first experiment — one that can say
whether conditional lexical evidence exists at all, and how large it is, before
anyone builds a deployment estimator for it.

The quantity is the second term of the likelihood-ratio factorisation in
[the identifiability note](../../docs/research/adaptive-fusion-identifiability.md):

\[
\log\frac{f_1(v,l)}{f_0(v,l)}=\underbrace{\log\frac{f_{1v}(v)}{f_{0v}(v)}}_{A(v)}
+\underbrace{\log\frac{f_1(l\mid v)}{f_0(l\mid v)}}_{J(v,l)} .
\]

\(A\) is the dense arm's own evidence. \(J\) is what the lexical arm adds *given
the dense score*. If \(J\equiv0\) the fused order is the dense order and the mode
has nothing to recover; the constant lexical weight already in the design is then
the answer. So the question is exactly:

> **Is the relevance label \(Y\) dependent on the lexical score \(L\) given the
> dense score \(V\), and how large is that dependence — particularly on the tasks
> where fusion loses?**

## What the labels actually are, and which way that biases the answer

Route (1) of the design says "with relevance judgements, the judged negatives
define a null directly". **This benchmark has no judged negatives.** Every
relevance file across the eight tasks carries the label 1 and nothing else
(2,319 rows for one task, 76,287 for another; no zero-valued row anywhere).
Measured, not assumed — the label columns were counted before this file was
written.

So the null is not "judged irrelevant" but the weaker **unjudged-as-irrelevant**
assumption: every eligible row that is not gold is treated as null. This is the
standard pooling assumption and it is stated here because it has a direction:

- Unjudged rows that are in fact relevant enter \(f_0\), moving it toward
  \(f_1\) and **shrinking** \(|J|\).
- Therefore a positive result is conservative — the real \(J\) is at least as
  large as the measured one.
- A null result is **not** conclusive in the same way; it is bounded by this
  assumption, and the write-up must say so rather than reporting "no evidence".

## Instrument

A row-keyed dump produced by `benchmarks/labelled_evidence_dump.py`, a sibling of
the frozen-stage replay that reuses the same frozen dense matrix and the same
real lexical arm on the real database, and writes rows instead of per-query
aggregates. It does not touch the replay, whose stage identities are relied on
elsewhere.

Per row `(query, document)` it records:

| Field | What it is |
|---|---|
| `v` | cosine similarity from the frozen matrix (the value the pipeline scores) |
| `vr` | rank of the row in that query's dense order over the eligible universe |
| `l` | lexical score, sign-normalised so that larger is a better match; `absent` when the row carries no lexical vote |
| `lr` | rank in the lexical list, or absent |
| `y` | 1 if the row is gold for that query, else 0 |

The **eligible universe** is the candidate subset where the task declares one,
and the stored corpus otherwise — the same set the replay's working set is drawn
from.

Eight tasks, three embedding models: all-MiniLM-L6-v2 (weakest),
jina-embeddings-v5-text-nano (mid), BAAI/bge-m3 (strongest). Queries are the same
deterministic every-k-th subsample the replay uses (at most 200 per subtask), so
the diagnostic describes the same query population as every other number on this
line and the embedding caches stay warm.

**The lexical side does not depend on the model.** Same corpus, same query text,
same index: `l` is identical across the three runs and only the conditioning
variable `v` changes. That is the point — the question is whether lexical adds
evidence *given this model's dense score*.

### Which rows are dumped

Dumping every eligible row for every query is not affordable, so:

- every gold row, always;
- every row with dense rank < 30 (the region where the top-ten decision is made);
- from each deeper dense-rank bin, a uniform sample of up to 20 non-gold rows,
  drawn with a seed derived from the query id and the bin, **independently of
  `y` and of `l`**.

That independence is what keeps the test below valid: under the null hypothesis
the sampled non-gold scores and the gold scores are draws from the same
within-stratum law, so the pooled set is exchangeable.

## The statistic

Rows are stratified by **(query, dense-rank bin)**. Within a stratum the dense
score is held approximately fixed, which is the conditioning \(J\) requires, and
— because a stratum never spans two queries — every cross-query difference in
lexical scale is absorbed by construction. This matters here: the lexical scale
of this arm was previously measured to differ by about a factor of two between
languages, so any statistic that pooled raw lexical scores across queries would
be measuring vocabulary, not evidence.

Primary statistic, the **stratified AUC**:

\[
\widehat{A}=\frac{\sum_s\sum_{g\in G_s}\sum_{n\in N_s}\bigl[\mathbb 1(l_g>l_n)+\tfrac12\mathbb 1(l_g=l_n)\bigr]}{\sum_s |G_s|\,|N_s|},
\]

the probability that a gold row outscores a non-gold row of the same query at a
comparable dense rank. \(\widehat A=0.5\) is exactly \(J\equiv0\) in rank terms.

- **Test**: permute the labels within each stratum (2,000 permutations). This is
  an exact conditional test of \(Y\perp L\mid V\); because strata never span
  queries, the clustering of several gold rows inside one query is respected
  rather than assumed away.
- **Interval**: a cluster bootstrap that resamples **queries** with replacement,
  2,000 resamples, percentile interval.
- **Magnitude, in the units the mode would use**: \(\widehat J\) on a grid of
  dense-rank bin × lexical bin (`absent` is its own bin, never a p-value of one),
  with add-one smoothing, reported **with the count in every cell** — the tails
  are where the signal lives and where estimation noise competes with it.

Secondary arms, fixed now:

1. **Granularity sweep** — stratum widths of 2, 5, 10 and 30 consecutive dense
   ranks around each gold row.
2. **Informative-stratum restriction** — strata whose lexical values are not all
   tied.
3. **Equal-stratum weighting** instead of pair weighting.

## The rival hypothesis, and the computation that rejects it

**Rival: there is no conditional lexical evidence; the dense score simply varies
inside a stratum, and what the statistic sees is that residual dense signal.**

This is rejected by the granularity sweep, not by argument. At width 2 a gold row
is compared with its single nearest neighbour in the dense order, so the residual
variation in \(v\) inside a stratum is as small as the data allow. If the rival
holds, \(\widehat A-0.5\) must fall toward zero as the width narrows. **The rival
is rejected only if \(\widehat A-0.5\) at width 2 is at least half its value at
width 30, with a bootstrap lower bound above zero.** If the effect is present
only at wide strata, it is reported as leakage, not as evidence.

## What each task can resolve

Under the null the gold row's rank within its stratum is uniform, so for one gold
and \(c\) controls \(\operatorname{Var}(\widehat A_s)=(1+2/c)/12\) — free of the
score distribution. Checked against simulation at \(c\in\{5,9,19,29,99\}\) before
being used here; the closed form and 20,000-trial simulations agree to within
0.6 %.

The consequence decided the sampling rule above: **controls barely matter.**
Going from 5 controls per stratum to 99 moves the standard error from 0.0242 to
0.0206. Only the number of gold-bearing strata moves it, so nothing is spent on
deeper sampling.

Minimum effect detectable at 80 % power, two-sided \(\alpha=0.05\), 9 controls:

| Task | Gold-bearing queries | SE | Detectable \(\widehat A\) |
|---|---:|---:|---:|
| EPBench | 3,644 | 0.0053 | 0.515 |
| TMD | 2,134 | 0.0069 | 0.519 |
| ReMe | 1,217 | 0.0091 | 0.526 |
| Gorilla | 598 | 0.0131 | 0.537 |
| QASPER | 200 | 0.0226 | 0.563 |
| LMEB_SciFact | 188 | 0.0233 | 0.565 |
| MLDR | 100 | 0.0319 | 0.589 |
| ESGReports | 36 | 0.0532 | 0.649 |

**ESGReports and MLDR are declared under-powered now**, before any number is
read: they can only see effects far larger than the ones this line cares about,
and they are excluded from the decision rule below whatever they show. TMD's
figure is optimistic in the other direction — it averages 35.75 gold rows per
query, so its independent unit count is nearer its query count than its gold
count, and the cluster bootstrap rather than this table is what its interval
comes from.

## Decision rule

Fixed in advance, and each branch is a decision rather than a request for more
data.

**Conditional lexical evidence exists** if, on the adequately powered tasks
(every task above except ESGReports and MLDR), the bootstrap lower bound of
\(\widehat A\) exceeds 0.5 on a majority of tasks for **both** endpoint models,
**and** the rival hypothesis is rejected by the granularity criterion above.
Then the open question of a deployment-usable null is worth solving, and the
choice between an independently justified null population and a stated
structural assumption becomes the next decision.

**It does not** if the bootstrap interval covers 0.5 on a majority of the
adequately powered tasks for either endpoint model. Then the evidence mode is not
built: the measured constant already in the design is the answer, and this file's
completeness caveat is carried into that conclusion.

**Where fusion loses** is a separate reading, not a gate: \(\widehat A-0.5\) is
cross-tabulated against the per-task fusion delta already measured by the replay.
The design's expectation is that the losing tasks are the redundant ones
(\(\widehat A\approx0.5\) where the fusion delta is negative). *Refuted if* the
losing tasks show an effect as large as the winning ones — in which case fusion
is losing for a reason this factorisation does not name, and the mode would not
repair it.

## Instrument qualification — run before any real number is read

Each check names what refutes it. A failure stops the measurement.

| Check | Refuted when |
|---|---|
| **Redundancy returns nothing.** Synthetic rows where `l` depends on `v` alone and `y` is assigned from `v` alone: 200 seeds. | The rejection rate at \(\alpha=0.05\) exceeds 0.10 — the estimator invents evidence where the construction has none. |
| **Known evidence is recovered.** Synthetic rows with a true stratified AUC of 0.60. | The estimate misses 0.60 by more than 0.02. |
| **The lexical sign convention is measured, not assumed.** The best-ranked row of a real lexical list must carry the extreme raw score in the direction the dump normalises. | Any query where the ordering disagrees with the sign convention. |
| **Abstention is not scored.** A stratum with no gold, or no non-gold, or a query whose lexical arm fell back to the non-scoring path. | Any such row reaches the numerator or the denominator. |

## Abstention buckets — reported even when empty

Counted and listed separately, never folded into the statistic:

1. queries whose lexical arm used the fallback path and produced no score;
2. strata holding no gold row, or no non-gold row;
3. tasks where more than 95 % of strata are fully tied — reported as *no lexical
   signal available*, which is not the same finding as *no evidence*;
4. under-powered tasks, reported with their numbers and excluded from the
   decision.

## What this cannot decide

- **It does not produce a deployment estimator.** The null used here is
  unavailable outside a labelled benchmark. A positive result says the quantity
  exists and is worth estimating, not that it can be estimated in a deployment.
- **It does not fit or validate any weight.** No per-model or per-task constant
  is estimated here, and a positive result is not a licence to fit one.
- **It does not measure retrieval quality.** No NDCG number moves because of this
  file; the binding success condition of the design is a separate, later
  measurement against the dense-only order.

## Amendment 1 — a measured control for the rival hypothesis

Added before the confirmatory dump finished and before any number from it was
read. **What had already been seen when this was written:** one smoke-test
figure, from a single task (MLDR) on a single model, produced while checking
that the dump and the analysis run end to end. That dump was discarded and
regenerated; no other result existed.

The section above rejects the rival hypothesis — that the statistic is seeing
residual dense signal inside a coarse stratum — by narrowing the strata and
watching the effect. That is an argument about a trend. It is replaced by a
**measurement of the leakage itself**: at every stratum width the identical
statistic is computed with the lexical score swapped for the dense score.

\[
\widehat A_{\text{dense}}=\Pr\bigl(v_{\text{gold}}>v_{\text{non-gold}}\mid\text{same query, same block}\bigr)
\]

If a width genuinely fixes \(v\), a gold row is no more likely than chance to
hold the better dense score inside its own block, and \(\widehat A_{\text{dense}}\)
sits at 0.5. Whatever it sits above 0.5 is exactly the room the rival hypothesis
has to work in, in the same units as the effect it is competing with.

This strengthens the criterion rather than replacing it. The rival is rejected
when **both** hold at the narrowest width:

1. \(\widehat A-0.5\) is at least half its value at width 30, with a bootstrap
   lower bound above zero (as registered above); and
2. \(\widehat A_{\text{dense}}-0.5\) is smaller than
   \(\widehat A - 0.5\) — the leakage a width admits is smaller than the effect
   measured at that width.

Reported for every task, model and width, including where it fails.
