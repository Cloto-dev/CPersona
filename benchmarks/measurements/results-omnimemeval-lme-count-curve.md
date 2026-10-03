# How many reconstruct items does an answer need? Results of the count curve

Registration: [prereg-omnimemeval-lme-count-curve.md](prereg-omnimemeval-lme-count-curve.md),
committed and pushed before any answer or judge call of this study. Source run:
[results-omnimemeval-lme.md](results-omnimemeval-lme.md), arm B (v1.2, `4c01ccc`).

## Result

- **Noise check: passed.** The full point, answered again on the same contexts,
  scored 81.60%, the published figure, with 96.0% per-question agreement; its
  answer prompts totalled 893,373 tokens, as in the published run.
- **The smallest number of items that holds is 8**: accuracy 82.60%, a paired
  difference of +1.00 points against full (95% interval −1.00 to +3.20), at
  1,439.3 Context Tokens, 19% fewer than full's 1,786.7. "Holds" is the
  registered rule (the interval's lower bound at or above −2.0 points); it is
  not a claim of higher accuracy.
- **Below that, accuracy falls.** At four items, the first point under 1,000
  Context Tokens (994.6), accuracy is 77.00%, 4.60 points below full (95%
  interval −7.40 to −1.80). Multi-session questions fall most: 61.7% against
  71.4%.

| Items (k) | Accuracy | k − full (95% interval) | Holds | Context tokens (cl100k) | Context Tokens (API) | Per correct answer |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 42.20% | −39.40 (−44.20 to −34.60) | no | 184.9 | 468.2 | 1,109 |
| 2 | 69.20% | −12.40 (−16.00 to −8.80) | no | 365.1 | 643.8 | 930 |
| 3 | 75.40% | −6.20 (−9.40 to −3.20) | no | 544.1 | 818.8 | 1,086 |
| 4 | 77.00% | −4.60 (−7.40 to −1.80) | no | 723.7 | 994.6 | 1,292 |
| 5 | 77.80% | −3.80 (−6.40 to −1.20) | no | 903.5 | 1,170.6 | 1,505 |
| 6 | 80.80% | −0.80 (−3.20 to +1.60) | no | 995.7 | 1,261.2 | 1,561 |
| 8 | 82.60% | +1.00 (−1.00 to +3.20) | **yes** | 1,177.3 | 1,439.3 | 1,743 |
| 10 | 80.40% | −1.20 (−3.40 to +1.00) | no | 1,355.4 | 1,614.0 | 2,008 |
| full | 81.60% | — | — | 1,531.4 | 1,786.7 | 2,190 |

Context Tokens (API) are the answer-stage prompt tokens the API reported, averaged
over 500 calls per point, every call usage-reported, none estimated. "Per correct
answer" is a point's total prompt tokens divided by its correct answers. "Holds"
at 6 and 10 is "no" because their intervals reach below −2.0 points; the rule
makes no claim either way about a point that does not hold.

Per question type (accuracy, %):

| Items (k) | Knowledge update | Multi-session | SS-Assistant | SS-Preference | SS-User | Temporal |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 57.7 | 12.8 | 80.4 | 66.7 | 65.7 | 28.6 |
| 2 | 80.8 | 54.1 | 82.1 | 76.7 | 75.7 | 66.9 |
| 3 | 87.2 | 58.6 | 83.9 | 80.0 | 85.7 | 75.2 |
| 4 | 84.6 | 61.7 | 82.1 | 86.7 | 85.7 | 78.9 |
| 5 | 82.1 | 65.4 | 82.1 | 83.3 | 87.1 | 79.7 |
| 6 | 83.3 | 69.9 | 83.9 | 90.0 | 88.6 | 82.7 |
| 8 | 84.6 | 70.7 | 82.1 | 86.7 | 92.9 | 87.2 |
| 10 | 84.6 | 68.4 | 82.1 | 86.7 | 91.4 | 82.0 |
| full | 84.6 | 71.4 | 80.4 | 80.0 | 90.0 | 86.5 |

## Reading it

Exploratory, beyond the registered rule:

- Under the current allocation a smaller budget removes whole items. Reaching
  under 1,000 Context Tokens that way costs about 4.6 points of accuracy, most
  of it on questions whose evidence spans sessions. A tighter budget therefore
  needs a different allocation, not a smaller count.
- Single-session assistant questions barely move with the count (80.4% to
  83.9% from one item to full): the evidence is usually in the first item.
- The two items held by the block reservation (full against 10) differ by 1.2
  points in favour of full, inside the noise of this study.

## What it does not say

Nothing about shorter quotes at ten items (a separate study, which needs the
search run again), and nothing about any allocation other than the current one.

## Notes on the run

- Scripts: [`count_curve_build.py`](../omnimemeval/count_curve_build.py) cuts the contexts,
  [`count_curve_analyze.py`](../omnimemeval/count_curve_analyze.py) applies the registered rule.
- One pass per point, no retry, 500 answers and 500 judgements each, at most 3
  concurrent model calls. 5,098,658 answer-prompt tokens in all.
- While checking the published run's file format before this study's calls, the
  text of one published answer was printed. The construction and the rule were
  already fixed in the registration.
- The contexts were cut from the saved search results; no search ran. The
  construction checks that every context splits at its item headers and joins
  back byte for byte, and the full point reproduces the published mean of
  1,531.4 context tokens.
