# Shorter quotes or fewer items? A quote-length curve on OmniMemEval's LongMemEval-S

Registered before any answer or judge call of this study. The commit that adds
this file is the evidence of the order.

## Question

The [count curve](results-omnimemeval-lme-count-curve.md) cut `reconstruct`'s
contexts by whole items: under 1,000 Context Tokens, accuracy fell 4.6 points
at four items. The [evidence metrics](results-omnimemeval-lme-evidence.md)
showed why: removing items removes answer sessions, most of all from
multi-session questions, while with every item only 21.9% of the quoted text
comes from answer sessions.

The other way to spend fewer tokens keeps every item and shortens every
quote. At the same retrieved-context tokens, does that hold accuracy better
than removing items? This is an instrument for the next allocation design, not
a claim that any setting is better.

## Instrument

- **The search is run again**, only the quote sizes changed: v1.2 (`4c01ccc`,
  arm B of [the published run](results-omnimemeval-lme.md)) with
  `CPERSONA_RECONSTRUCT_QUOTE_CHARS` = Q and
  `CPERSONA_RECONSTRUCT_TAIL_QUOTE_CHARS` = Q/2. The first five items are
  quoted to Q, later ones to Q/2, as the defaults do with 800 and 400. Every
  other setting is the published one; the default payload budget follows the
  quote sizes.
- Each size searches its own copy of the published store, made with its
  calibration file so the server restores the calibration instead of running
  it again, and waits for the startup queue to drain before the first query.
  The harness's own search wrapper runs over the ingestion's user ids with two
  workers, as the harness did. Nothing is ingested again.
- **Reproduction check, before any call**: the published sizes (800/400),
  searched the same way, must give the published contexts for all 500
  questions. If they do not, the study stops here.
- Answer and judge exactly as in the published run and the count curve:
  OmniMemEval `0b1ea8d` with the same adapter patch, answer model
  `gpt-4.1-mini-2025-04-14`, judge `gpt-4o-mini`, one judge run, temperature 0,
  at most 3 concurrent model calls, one results directory per point.
- **Controls are not answered again**: the count curve's answers for four
  items, six items and every item are the comparisons.
- Scripts: [`quote_curve_build.py`](../omnimemeval/quote_curve_build.py)
  checks the reproduction and writes each point's answer inputs from its
  searched contexts; [`quote_curve_analyze.py`](../omnimemeval/quote_curve_analyze.py)
  applies the rules below.

## Points

Sizes searched before any call, with the mean retrieved context in
`cl100k_base` tokens (the harness's count):

| Quote / tail (characters) | 800/400 | 560/280 | 500/250 | 480/240 | 400/200 | 360/180 | 320/160 | 240/120 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Context tokens (mean) | 1,531.4 | 1,097.1 | 992.4 | 955.7 | 820.8 | 755.0 | 694.5 | 576.7 |

800/400 passed the reproduction check: all 500 contexts are the published
ones, and their mean is the published 1,531.4. The first six sizes were
searched as a sweep; 500/250 and 360/180 were added after it to bracket the two
controls more closely. No size's contexts were read for anything but their
length, and no answer or judge call had been made. Search time was 0.55 to
0.60 seconds per question on average for every size, and no agent was
calibrated during any search.

The two points are the sizes whose mean context is closest to the count
curve's four items (723.7 tokens) and six items (995.7): **320/160** (694.5,
4.0% below four items; 360/180 is 4.3% above) and **500/250** (992.4, 0.3%
below six items). A point whose mean differs from its control's by more than
5% is compared as "not at the same cost", with both numbers reported; neither
does.

## Measures

Accuracy overall and per question type as the harness judges it; Context
Tokens as the API reports them; the mean retrieved-context tokens; Context
Tokens per correct answer; and the evidence metrics of
[`evidence_metrics.py`](../omnimemeval/evidence_metrics.py) (answer sessions
shown, evidence turns touched, evidence share) for each point.

## What may be said

- **Each point against its item-count control at the same cost**: the 95%
  bootstrap interval of the paired difference in accuracy, point minus control
  (10,000 resamples over questions, seed 20261003). Shorter quotes are better
  at that cost when the lower bound is above 0; fewer items are better when
  the upper bound is below 0; otherwise neither is claimed.
- **Each point against every item**: the point holds, as in the count curve,
  when the lower bound of the interval of point minus every item is at or
  above −2.0 points.
- The controls' answers come from an earlier pass. The count curve measured
  how much a second answer pass moves on the same contexts (96.0% per-question
  agreement); the interval over questions carries that variation.
- Every point is reported, including one that shows nothing, with the evidence
  metrics beside it.

## What it does not say

- Nothing about another split of the quote sizes (a tail other than half the
  head, or a different number of full-size items), nor about an allocation that
  chooses quotes across items: this measures the current allocation with
  shorter quotes.
- Nothing about stores whose items hold several records; here an item holds
  one session.

One run per point. A rerun is made only for an infrastructure failure, and is
reported. Nothing in the construction or the settings changes after any answer
is seen.
