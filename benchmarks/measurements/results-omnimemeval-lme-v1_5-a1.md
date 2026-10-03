# The evidence sequence at the cost of four items (2.6.5a1): results

Registration: [prereg-omnimemeval-lme-v1_5-a1.md](prereg-omnimemeval-lme-v1_5-a1.md),
committed and pushed before any answer or judge call. Read with
[`v1_5_analyze.py`](../omnimemeval/v1_5_analyze.py).

## Result

- **Primary rule met.** At budget 2,800, `CPERSONA_RECONSTRUCT_SEQUENCE=evidence`
  averaged 992.9 Context Tokens over its 400 answer calls, which is under the
  1,000 cap. It answered 81.00% of the 400 test questions, against 77.00% for
  four items on the same questions. The paired difference is +4.00 points, with
  a 95% interval of +0.75 to +7.25. Its mean retrieved context was 721.4
  `cl100k_base` tokens against four items' 722.0 (−0.1%), so the two are at the
  same cost.
- **Secondary rule not met.** Against every item (also 81.00% on these
  questions), the difference is +0.00 points, with an interval of −3.50 to
  +3.25. The lower bound is below −2.0, so the rule does not let this study say
  the sequence holds every item's accuracy. Every item used 1,526.9 tokens of
  retrieved context, more than twice as many.

| Point | Accuracy | Context (cl100k) | Answer sessions shown % | All shown % | Evidence turns touched % | Evidence-turn characters quoted % | Evidence share % |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Evidence, budget 2,800 | 81.00 | 721.4 | 95.7 | 92.2 | 92.1 | 51.7 | 63.7 |
| Four items | 77.00 | 722.0 | 90.1 | 81.2 | 85.7 | 41.4 | 42.2 |
| Every item | 81.00 | 1,526.9 | 96.4 | 93.8 | 90.4 | 44.5 | 22.2 |

Accuracy by question type, % (questions):

| Point | Knowledge update (64) | Multi-session (109) | Single-session assistant (46) | Single-session preference (22) | Single-session user (53) | Temporal reasoning (106) |
| --- | --- | --- | --- | --- | --- | --- |
| Evidence, budget 2,800 | 85.9 | 69.7 | 82.6 | 86.4 | 94.3 | 81.1 |
| Four items | 87.5 | 60.6 | 80.4 | 90.9 | 86.8 | 78.3 |
| Every item | 84.4 | 71.6 | 78.3 | 81.8 | 88.7 | 85.8 |

## What was run

- The reproduction check passed before any call. On all 400 test questions,
  2.6.5a1 at its defaults gave the published contexts.
- One answer pass and one judge run, with no rerun. The 400 answer calls all
  reported usage: 397,177 prompt tokens and 54,840 completion tokens.
- That is 1,226 Context Tokens per correct answer. Four items took 1,292 and
  every item 2,190, both over all 500 questions in the count curve.
- The controls are the count curve's answers on the same 400 questions. The
  evidence metrics come from
  [`evidence_metrics.py`](../omnimemeval/evidence_metrics.py)'s own functions,
  and every quoted character of every point was found in the store.

## Reading

The evidence metrics come from the same contexts as the accuracy, but no rule
was registered on them; they are descriptive.

- **Fewer, fuller items.** At the cost of four items, the sequence returns a
  mean of 5.6 items on the development questions (registration table). It shows
  every answer session of a question more often than four items do (92.2%
  against 81.2%). It also touches more evidence turns than four items do, and
  more than every item does (92.1% against 85.7% and 90.4%).
- **Less padding.** 63.7% of the quoted text comes from answer sessions,
  against 42.2% for four items and 22.2% for every item.
- **Where the accuracy moved.** Multi-session questions moved most against four
  items (69.7% against 60.6%), the type the count curve lost most by removing
  items. So did single-session user questions (94.3% against 86.8%).
  Preference and knowledge-update questions are slightly lower than four items.
  Those groups hold 22 and 64 questions, and no per-type rule was registered.

## What it does not say

- Nothing about any budget but 2,800, and nothing about coverage (2.6.5a2).
- Nothing about stores whose items hold several records. The private real-use
  pack is not part of this result.
- It does not say that the sequence matches every item's accuracy: that rule was
  not met.
- Whether `evidence` becomes the default is a separate decision. This result is
  the measurement that decision requires.
