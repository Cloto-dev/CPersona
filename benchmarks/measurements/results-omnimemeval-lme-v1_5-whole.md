# The length floor (`whole`) against the evidence sequence: results

Registration: [prereg-omnimemeval-lme-v1_5-whole.md](prereg-omnimemeval-lme-v1_5-whole.md),
committed and pushed before the test questions were searched with `whole` and
before any answer or judge call. Read with
[`v1_5_whole_analyze.py`](../omnimemeval/v1_5_whole_analyze.py).

## Result

- **Primary rule met.** At budget 2,800, `CPERSONA_RECONSTRUCT_SEQUENCE=whole`
  answered 80.75% of the 400 test questions, against 81.00% for the evidence
  sequence on the same questions. The paired difference is −0.25 points, with a
  95% interval of −0.75 to +0.00. The lower bound is above −2.0. Its mean
  retrieved context was 722.2 `cl100k_base` tokens against the evidence
  sequence's 721.4 (+0.1%), so the two are at the same cost.
- **Secondary rule met.** `whole`'s Context Tokens are 993.8, under the 1,000
  cap: the 2.6.5a1 test's 992.9, plus 327 `o200k_base` tokens over the changed
  questions, divided by 400. Against four items (77.00%) the difference is
  +3.75 points, with an interval of +0.50 to +7.00, so `whole` keeps the
  evidence sequence's lead over four items at the same cost.

| Point | Accuracy | Context (cl100k) | Answer sessions shown % | All shown % | Evidence turns touched % | Evidence-turn characters quoted % | Evidence share % |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Whole, budget 2,800 | 80.75 | 722.2 | 95.7 | 92.2 | 91.8 | 51.7 | 63.6 |
| Evidence, budget 2,800 | 81.00 | 721.4 | 95.7 | 92.2 | 92.1 | 51.7 | 63.7 |
| Four items | 77.00 | 722.0 | 90.1 | 81.2 | 85.7 | 41.4 | 42.2 |

Accuracy by question type, % (questions):

| Point | Knowledge update (64) | Multi-session (109) | Single-session assistant (46) | Single-session preference (22) | Single-session user (53) | Temporal reasoning (106) |
| --- | --- | --- | --- | --- | --- | --- |
| Whole, budget 2,800 | 85.9 | 69.7 | 82.6 | 86.4 | 94.3 | 80.2 |
| Evidence, budget 2,800 | 85.9 | 69.7 | 82.6 | 86.4 | 94.3 | 81.1 |
| Four items | 87.5 | 60.6 | 80.4 | 90.9 | 86.8 | 78.3 |

## What was run

- The reproduction check passed before any call. On all 400 test questions,
  the evidence sequence at budget 2,800 gave the 2.6.5a1 test's contexts.
- **9 of the 400 questions changed**: questions 36, 44, 55, 58, 70, 257, 352,
  478 and 488 (four single-session user, two temporal reasoning, two
  single-session assistant, one multi-session). Every other question sends the
  same prompt as in the 2.6.5a1 test and keeps that test's recorded answer.
- One answer pass and one judge run for the 9, with no rerun. The 9 answer
  calls all reported usage: 9,288 prompt tokens (1,032.0 each) and 747
  completion tokens.
- Eight of the nine kept their outcome (five right, three wrong in both). One,
  question 257 (temporal reasoning), was right under the evidence sequence and
  wrong under `whole`. A single changed answer cannot be told apart from the
  variation of a second answer pass, which the count curve measured at 96.0%
  per-question agreement on the same contexts.
- The evidence metrics come from
  [`evidence_metrics.py`](../omnimemeval/evidence_metrics.py)'s own functions,
  and every quoted character of every point was found in the store.

## The private real-use pack

Its test was registered separately, before it ran, because its questions
cannot be published. On its 150 test questions, at budget 2,800, `whole`
showed 120 of the 196 evidence quotes and the evidence sequence 81: 39 more,
0.260 per question, with a 95% interval of +0.127 to +0.400, at shown text
1.5% shorter. That met its registered rule. Every quote `whole` gained over the
evidence sequence there came from a record no longer than one quote. The
default sequence showed 121 at the same budget; that comparison was not
registered and is not a claim.

## What it does not say

- Nothing about any budget but 2,800 on LongMemEval-S.
- It does not say `whole` answers better than the evidence sequence here: on
  this pack the floor changes 9 questions, and the rule was that it costs no
  more than 2 points.
- Nothing about `whole` against the default sequence. On the private pack the
  two were close at this budget, but no rule was registered for it.
- Whether `whole` replaces `evidence` as the evidence sequence that is
  recommended, and whether either becomes the default, are decisions this result
  informs; it does not make them.
