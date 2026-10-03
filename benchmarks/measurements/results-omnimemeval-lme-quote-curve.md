# Shorter quotes or fewer items? Results of the quote-length curve

Registration: [prereg-omnimemeval-lme-quote-curve.md](prereg-omnimemeval-lme-quote-curve.md),
committed and pushed before any answer or judge call of this study. Controls:
the [count curve](results-omnimemeval-lme-count-curve.md)'s answers for four
items, six items and every item.

## Result

- **Reproduction check: passed.** The published quote sizes (800/400), searched
  again, gave the published context for all 500 questions.
- **At the same cost, fewer items are better than shorter quotes, at both
  points.** Keeping every item and quoting each to 320/160 characters (694.5
  context tokens) scored 69.60%, 7.40 points below four items at 723.7 tokens
  (95% interval −11.20 to −3.60). At 500/250 (992.4 tokens) it scored 76.60%,
  4.20 points below six items at 995.7 (95% interval −7.20 to −1.20). Both
  intervals lie below zero, which is the registered rule for "fewer items
  better".
- **Neither point holds against every item**: −12.00 points (−15.60 to −8.40)
  and −5.00 points (−7.80 to −2.20).

| Point | Accuracy | Context tokens (cl100k) | Context Tokens (API) | Per correct answer | Knowledge update | Multi-session | SS-Assistant | SS-Preference | SS-User | Temporal |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Every item (800/400) | 81.60% | 1,531.4 | 1,786.7 | 2,190 | 84.6 | 71.4 | 80.4 | 80.0 | 90.0 | 86.5 |
| Six items | 80.80% | 995.7 | 1,261.2 | 1,561 | 83.3 | 69.9 | 83.9 | 90.0 | 88.6 | 82.7 |
| Every item at 500/250 | 76.60% | 992.4 | 1,260.5 | 1,646 | 85.9 | 63.2 | 66.1 | 83.3 | 88.6 | 81.2 |
| Four items | 77.00% | 723.7 | 994.6 | 1,292 | 84.6 | 61.7 | 82.1 | 86.7 | 85.7 | 78.9 |
| Every item at 320/160 | 69.60% | 694.5 | 970.1 | 1,394 | 80.8 | 54.9 | 50.0 | 76.7 | 82.9 | 77.4 |

Context Tokens (API) are the answer-stage prompt tokens the API reported,
averaged over 500 calls per point, every call usage-reported. "Per correct
answer" is a point's total prompt tokens divided by its correct answers.

## Why: the evidence metrics

The [evidence metrics](results-omnimemeval-lme-evidence.md) of each point
(exploratory, not part of the registered rule):

| Point | Answer sessions shown | Evidence turns touched | Evidence-turn characters quoted | Evidence share |
| --- | --- | --- | --- | --- |
| Every item (800/400) | 96.2% | 90.5% | 52.2% | 21.9% |
| Six items | 92.9% | 88.1% | 51.0% | 31.9% |
| Every item at 500/250 | 96.2% | 85.1% | 43.6% | 22.0% |
| Four items | 89.6% | 85.8% | 49.5% | 41.6% |
| Every item at 320/160 | 96.2% | 75.5% | 35.4% | 20.9% |

Wrong answers by what the context showed:

| Point | Wrong | No answer session | Answer session, no evidence turn | Some evidence turns | Every evidence turn | No turn labelled |
| --- | --- | --- | --- | --- | --- | --- |
| Six items | 96 | 12 | 8 | 39 | 33 | 4 |
| Every item at 500/250 | 117 | 4 | 18 | 46 | 46 | 3 |
| Four items | 115 | 14 | 8 | 56 | 33 | 4 |
| Every item at 320/160 | 152 | 5 | 44 | 55 | 47 | 1 |

## Reading it

Exploratory, beyond the registered rule:

- **Shortening every quote shrinks evidence and padding alike.** With every
  item kept, the answer sessions stay in the context (96.2%, as with full
  quotes), but the part of them that answers the question is cut: evidence
  turns touched fall from 90.5% to 75.5% at 320/160, and the share of the
  context that is evidence stays where it was (about 21%). Removing items from
  the end instead removes mostly padding, so the evidence share doubles (41.6%
  at four items).
- **The loss lands on long turns.** Single-session assistant questions, whose
  evidence sits in long assistant replies, fall from 80.4% to 50.0% at
  320/160; on wrong answers at that size, 44 had the answer session but none of
  its evidence turns in the quote, against 8 at four items.
- **Neither uniform cut is the allocation to keep.** Removing items loses
  sessions a multi-session question needs; shortening quotes loses the turn
  inside the session. Spending fewer tokens without losing accuracy needs the
  budget to follow the evidence: which passages, from which records, at what
  length.

## What it does not say

Nothing about splits other than a tail of half the head with five full-size
items, about allocations that choose passages across items, or about stores
whose items hold several records.

## Notes on the run

- Scripts: [`quote_curve_build.py`](../omnimemeval/quote_curve_build.py) checks the
  reproduction and writes the answer inputs; [`quote_curve_analyze.py`](../omnimemeval/quote_curve_analyze.py)
  applies the registered rules; [`evidence_metrics.py`](../omnimemeval/evidence_metrics.py)
  reads the evidence.
- **The judge stage ran twice.** The first answer inputs carried each
  question's search time as a string; the harness adds it to a number after the
  judge call, so the first run's judgements (three passes per point) were made
  and discarded, and no result was written. The answers of that run, 500 per
  point in one pass, were kept. The search time was converted to a number in
  the saved inputs and answers (nothing else in them changed; checked field by
  field), the build was fixed and pinned by a test, and the judge stage ran
  once more per point. Accuracy comes from that run only.
- While checking the fix with a stub judge, the text of two answers was
  printed. The points and the rules were already fixed in the registration.
- Ten quoted parts at 320/160 matched more than one record; in one of them the
  choice could change whether an answer session counts as shown. The
  registered accuracy does not depend on it.
- One answer pass and one judge pass per point, at most 3 concurrent model
  calls; 1,115,303 answer-prompt tokens in all.
