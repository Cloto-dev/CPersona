# Can a different isolation bucket serve as the null population?

Measured 2026-09-10 on a live deployment, read-only. Buckets are reported by
size and shape only.

The conditional-evidence fusion mode needs a null it can estimate where it runs,
and the labelled route that showed the evidence exists is unavailable outside a
benchmark. The design named three candidates, and the second was the one a
deployment might actually have: **pairs whose irrelevance is established by
construction — a different isolation bucket**. The design also warned that such
a population is not automatically valid, because a different corpus moves the
score marginals through vocabulary and length for reasons that have nothing to
do with relevance, and said that must be measured before it is used.

It has now been measured. **A different isolation bucket is not a valid null,**
and the reason is structural rather than incidental to this deployment.

## What was run

A read-only probe against a deployment holding 3,614 embedded rows across eight
isolation buckets of 96 to 1,850 rows. All rows carry a stored embedding, and
every stored vector is unit norm to the precision printed (1.000000), which the
probe checks rather than assumes. The lexical arm is the deployment's own query
builder and its own trigram index — not a re-implementation — so the CJK
handling that a live index performs is the handling measured here.

For an ordered pair of buckets (A, B): sixty queries drawn from A, scored
against A's own rows and against B's rows, and the two laws compared on a shared
grid of cosine quantile × lexical-rank bin, with absence as its own bin:

\[
J_{\text{spurious}}(\text{cell})=\log p_{\text{own}}(\text{cell})-\log p_{\text{foreign}}(\text{cell})
\]

Cells below thirty observations on either side are reported and never
interpreted. Nothing was written; only aggregates left the host.

## The control, and the identification problem it exposed

**Control:** a *random half of A itself* is compositionally identical to A by
construction, so whatever the statistic reports against it is the floor of the
instrument — finite samples, smoothing, and A's own relevant tail.

| | max \|J\| | median \|J\| | \|median cosine shift\| |
|---|---:|---:|---:|
| random half of the same bucket (8 buckets) | 0.10 – 0.27 | 0.01 – 0.11 | ≤ 0.0068 |

The floor is about 0.18 nats. The instrument is calibrated: identical
composition produces essentially no evidence.

**But excess over that floor does not settle the question**, and this is worth
stating because the first reading of these numbers got it wrong. A *valid* null
would also raise \(J\) — raising it is what a null is for. Excess \(J\) alone
cannot separate "B is compositionally shifted" from "B is a correct null and
this is the real evidence".

What separates them is the **bulk**. Relevant rows are on the order of \(10^{-3}\)
of pairs, so they cannot move a median. **A non-zero median cosine shift is
compositional and nothing else**, and so are the cells at low cosine with no
lexical vote, which relevance does not reach.

## Result

| | median cosine shift | bulk \|J\| (relevance cannot reach) | max \|J\| |
|---|---:|---:|---:|
| control (same composition) | ≤ 0.0068 | — | 0.10 – 0.27 |
| foreign bucket, worst 8 pairs | 0.099 – 0.141 | 2.4 – 4.0 | 2.5 – 4.0 |
| foreign bucket, all 56 pairs | median 0.062, max 0.141 | 0.42 – 4.00 | 0.49 – 5.21 |

The benchmark measured the real conditional evidence at 1.2 to 5.3 nats. **The
distortion a foreign bucket injects is the same size as the signal it would be
used to detect.** In the units the estimator conditions on, the median cosine
shift is 6 caliper widths at the median pair and 14 at the worst — against a
benchmark caliper of 0.01, the width at which the dense score is genuinely
pinned.

**Six of fifty-six ordered pairs** have a bulk indistinguishable at one caliper
width. Every one of them takes the **broad, mixed-topic pool as the source** and
a narrow single-topic bucket as the null.

That is the wrong direction. An estimator needs the null for the bucket a user is
working *in*, which is the narrow one; the six clean pairs supply nulls for the
broad pool instead. The distortion is worst in exactly the direction a deployment
would need: a narrow bucket's queries find near neighbours in their own bucket
that do not exist in a foreign one, so the foreign law has no mass in the
high-cosine cells and the ratio diverges there — the cells where the decision is
made.

## Why this is structural, not a property of this deployment

Isolation buckets are separated **because their content differs**. That is what
they are for. A population that is separated by topic cannot also be a
topic-neutral null, and the sharper the isolation, the worse the null. A
deployment whose buckets were similar enough to serve as each other's nulls would
be a deployment that did not need isolation.

The control makes the same point from the other side: the one population that is
compositionally valid is **the bucket itself** — and the bucket itself is the
mixture, which contains the relevant rows. That is the identifiability problem
restated, not solved.

## Consequence

- Route (b), a different isolation bucket, is **closed** for this deployment and
  for the same reason in any deployment whose isolation means anything.
- Route (a), labels, remains benchmark-only.
- **Route (c) — a structural assumption strong enough to identify the mixture
  from one sample, stated so that it can be refuted — is the remaining route.**
  The control above is the natural place to start: it establishes that a random
  split of a single bucket is compositionally exact, so an assumption that
  separates mixture from null *within* one bucket is the shape the next attempt
  must take.

## Limits

- Sixty queries per bucket, and the queries are stored rows used as queries,
  because no query log exists. A real query distribution may differ from the
  document distribution.
- Buckets here run from 96 to 1,850 rows. The control shows the instrument is
  sound at those sizes; it does not show that a larger deployment behaves the
  same way.
- The comparison is of laws, not of retrieval quality. No score moved and no
  default changed because of this page.
