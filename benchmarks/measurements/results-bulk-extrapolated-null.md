# Can a query's own bulk predict its own null tail?

Measured 2026-09-10. This is the third and last of the routes to a null the
conditional-evidence mode could estimate. The first needs labels and exists only
on a benchmark. The second — a different isolation bucket — was closed by
measurement: its composition moves the marginals as much as relevance does.

That closure also said where the remaining route has to live. The one population
whose composition is exact is **a single bucket**, because a random split of one
bucket showed a distortion of 0.10 to 0.27 nats against a signal of 1.2 to 5.3.
So the structural assumption has to identify the mixture *inside* one bucket
rather than across two.

## The assumption, stated so it can be refuted

> **C1.** For a given query, the score law of the irrelevant rows is a declared
> family whose parameters are fixed by that query's own central bulk. Relevant
> rows are of order \(10^{-3}\) of pairs, so they cannot move a robust centre or
> spread; the bulk is therefore estimable without labels, the tail becomes a
> prediction, and whatever the observed law holds in excess of that prediction is
> the relevant mass.

It is query-conditional by construction — which is what the identifiability note
requires and what the design page lost when it averaged over queries.

**It is also already half-shipped.** The calibration sidecar fixes its admission
threshold from the mean and standard deviation of sampled pairs. That is C1 with
a Gaussian family. So this measurement audits a shipped assumption as well as a
proposed one.

## The test

On the benchmark the true null is known. Fit the family on a query's bulk
**without looking at labels**, predict the null's survival where the decision is
made, and compare with the labelled truth:

\[
J_{\text{error}}=\log\frac{\text{predicted null survival}}{\text{true null survival}}
\]

Refutations fixed before the numbers were read: the family is refuted for a query
if it cannot predict a **held-out band of the bulk** that relevance cannot reach;
the assumption is refuted for the mode if the median \(|J_{\text{error}}|\)
reaches 1.2 nats, the smallest real signal measured; a query whose deep sample
cannot support a fit is abstained on and never scored as a success.

Every estimate is weighted by the dump's known inclusion probabilities. Those
weights are checked against a quantity the dump recorded independently — the size
of the eligible universe — and reconstruct it exactly (relative error 0.0000 on
every task and model). That check is what exposed the query-key defect corrected
the same day in the sister measurement.

## Result

Held-out bulk, every task and model, both families: **0.05 to 0.22 nats.** The
family fits the bulk; refutation one does not fire.

Median \(|J_{\text{error}}|\) in the decision region, bge-m3 / MiniLM:

| Task | family | r1 | r5 | r10 | r30 |
|---|---|---:|---:|---:|---:|
| EPBench | gaussian | 3.42 / 1.64 | 1.57 / 0.81 | 0.85 / 0.53 | 0.23 / 0.20 |
| EPBench | **logistic** | 1.11 / 0.84 | 0.75 / 0.56 | **0.56 / 0.40** | 0.20 / 0.19 |
| LMEB_SciFact | gaussian | 10.32 / 10.85 | 5.95 / 8.10 | 2.49 / 3.00 | 0.81 / 1.01 |
| LMEB_SciFact | **logistic** | 1.86 / 2.29 | 1.59 / 1.98 | **0.99 / 1.36** | 0.61 / 0.76 |
| Gorilla | gaussian | 2.81 / 2.03 | 1.61 / 1.44 | 1.20 / 1.18 | 0.76 / 0.82 |
| Gorilla | **logistic** | 1.17 / 1.36 | 0.83 / 1.05 | **0.65 / 0.86** | 0.44 / 0.37 |
| ReMe | **logistic** | 1.18 / 1.57 | 0.66 / 0.94 | **0.45 / 0.61** | 0.12 / 0.10 |
| TMD | **logistic** | 1.10 / 1.61 | 0.84 / 1.08 | **0.65 / 0.83** | 0.42 / 0.39 |

**The family matters more than anything else here.** The Gaussian — the shipped
calibration's implicit choice — misses the tail badly where the tail is heavy:
it is wrong by a factor of \(e^{10}\) at the top rank on one task. A logistic
with the same two parameters and the same robust fit brings that to \(e^{1.9}\)
and does not hurt the tasks the Gaussian already handled.

**The assumption is not refuted.** With the logistic family, the median error at
rank 10 runs 0.10 to 1.36 nats across tasks and models, and only one cell
(LMEB_SciFact on the weakest model, 1.36) reaches the 1.2-nat line. At rank 30
nothing does. This is the first of the three routes that survives its own
refutation test.

## What it costs: abstention

The fit needs a bulk, and a small eligible universe does not have one:

| Task | queries used | abstained |
|---|---:|---:|
| TMD | 1,370 | 0 |
| LMEB_SciFact | 188 | 0 |
| EPBench | 2,548 | 1,096 |
| ReMe | 615 | 390 |
| Gorilla | 167 | 98 |
| QASPER | 7 | 184 |
| MLDR | 0 | 100 |

MLDR abstains completely and QASPER on 96 % of queries: both declare small
candidate subsets, so there is no deep region to fit. This is the honest
behaviour rather than a failure — an assumption that cannot be checked is
declined — but it bounds where the mode could run. The deployment measured in the
sister page holds buckets of 96 to 1,850 rows, which clear the fit's threshold,
but the quality of a tail estimated from a hundred rows is not established here.

## What this does and does not settle

- **It keeps route (c) open**, which is the only remaining one, and names the
  family it needs: heavy-tailed, not Gaussian.
- **It is a finding about the shipped calibration independently of the mode.**
  The admission threshold is set from a Gaussian assumption that this measurement
  shows is wrong in the tail, on the same corpora the rest of this line uses.
  Whether that costs anything in retrieval is a separate question this page does
  not answer.
- **It does not build the estimator.** The joint law over both arms, the
  smoothing, the monotone regularisation and the abstention rule are all still to
  be designed, and the count-per-cell problem in the tails is untouched.
- **Rank 1 is one row.** The error there is dominated by discreteness rather than
  by the family; rank 10 and rank 30 are the columns to read.
