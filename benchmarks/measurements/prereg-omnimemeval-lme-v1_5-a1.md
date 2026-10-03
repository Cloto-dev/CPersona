# The evidence sequence at the cost of four items (2.6.5a1) on OmniMemEval's LongMemEval-S

Registered before any answer or judge call of this study. The commit that adds
this file is the evidence of the order.

## Question

[Evidence allocation](../../docs/EVIDENCE_ALLOCATION_DESIGN.md) section 7 sets
the rule this answers. `CPERSONA_RECONSTRUCT_SEQUENCE=evidence` (2.6.5a1) makes
the payload budget of `reconstruct` cut one order of the head records' passages
across records, instead of every head quote in item order and then the excerpts.
At a budget whose Context Tokens are at most 1,000, does it answer better than
the [count curve](results-omnimemeval-lme-count-curve.md)'s four items at the
same cost?

## Development (done before this registration)

- **The 100 development questions only**
  ([`v1_5_dev_questions.json`](../omnimemeval/v1_5_dev_questions.json), drawn
  once by seed 20261003). Search only, no model call.
- 2.6.5a1 (`v2.6.5a1`, `49a649b`) searched copies of the
  [published](results-omnimemeval-lme.md) store, each made with its calibration
  file so the server restores the calibration. Each search waited for the boot
  queue to drain. The harness's own search wrapper ran over the ingestion's
  user ids with two workers
  ([`search_driver.py`](../omnimemeval/search_driver.py) with
  `SEARCH_INDICES`). The budget was set with
  `CPERSONA_RECONSTRUCT_FORCED_BUDGET`. Every other setting is the published
  one, and no agent was calibrated during any search.
- **Reproduction check**: the default sequence at the default budget gave the
  published contexts for 100 of 100 questions.
- Measured with
  [`v1_5_dev_analyze.py`](../omnimemeval/v1_5_dev_analyze.py) on these 100
  questions. Mean retrieved context is in `cl100k_base` tokens (the harness's
  count). The evidence metrics are those of
  [`evidence_metrics.py`](../omnimemeval/evidence_metrics.py). Accuracy is the
  recorded answers of the controls on these questions.

| Point | Context tokens | Items | Answer sessions shown % | All shown % | Evidence turns touched % | Evidence-turn characters quoted % | Evidence share % | Accuracy (recorded) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Four items | 730.5 | 4.00 | 87.5 | 78.0 | 86.2 | 52.6 | 39.5 | 77.00 |
| Six items | 1,004.9 | 6.00 | 90.6 | 83.0 | 88.2 | 53.5 | 29.9 | 83.00 |
| Every item | 1,549.7 | 11.87 | 95.5 | 91.0 | 90.9 | 54.7 | 20.5 | 84.00 |
| Quotes 320/160 | 699.8 | 11.87 | 95.5 | 91.0 | 77.5 | 38.2 | 19.9 | 76.00 |
| Quotes 500/250 | 1,002.1 | 11.87 | 95.5 | 91.0 | 85.8 | 46.6 | 20.6 | 80.00 |
| Evidence, budget 2,400 | 623.0 | 5.04 | 93.2 | 86.0 | 90.1 | 58.5 | 61.2 | — |
| **Evidence, budget 2,800** | **720.7** | 5.62 | 93.2 | 86.0 | 90.4 | 59.8 | 58.7 | — |
| Evidence, budget 3,200 | 825.7 | 6.18 | 94.5 | 90.0 | 91.3 | 60.3 | 54.8 | — |
| Evidence, budget 3,600 | 928.4 | 6.65 | 94.5 | 90.0 | 91.3 | 60.8 | 52.3 | — |
| Evidence, budget 4,000 | 1,036.0 | 7.20 | 94.5 | 90.0 | 91.9 | 62.6 | 50.3 | — |

Every quoted character of every point was found in the store.

**The point is budget 2,800.** Its mean context on these questions is the one
closest to four items' (720.7 against 730.5, 1.3% below; budget 3,200 is 13.0%
above), and that is the whole rule of the choice. The evidence metrics above
were computed for every point before the choice and are reported, but they did
not choose it. Four items' answer-stage overhead over the 500 questions was
270.9 tokens (994.6 Context Tokens against 723.7 of context), so budget 2,800
is expected near 990 Context Tokens. That figure is an expectation, not the
measure. The measure is in the rules below.

## Test

- **The other 400 questions**, and only those.
- **Two searches, done the same way as development**: the defaults, and the
  evidence sequence at budget 2,800.
- **Reproduction check, before any call**: the defaults must give the
  published contexts for all 400 test questions
  ([`v1_5_build.py`](../omnimemeval/v1_5_build.py) stops otherwise). That
  script writes the point's answer inputs from its searched contexts.
- **Answer and judge exactly as in the published run and the count curve**:
  OmniMemEval `0b1ea8d` with the same adapter patch, answer model
  `gpt-4.1-mini-2025-04-14`, judge `gpt-4o-mini`, one judge run, temperature 0,
  and at most 3 concurrent model calls. The results go in one directory.
- **Controls are not answered again**: the count curve's answers for four
  items and every item, on the same 400 questions.
- [`v1_5_analyze.py`](../omnimemeval/v1_5_analyze.py) applies the rules
  below.

## What may be said

- **Primary**: the point's Context Tokens (the answer-stage prompt tokens the
  API reports, averaged over its 400 calls) are at most 1,000. The 95%
  bootstrap interval of the paired accuracy difference, point minus four items
  (10,000 resamples over the 400 questions, seed 20261003), has its lower bound
  above 0. When both hold, the evidence sequence answers better than four items
  at the same cost. Above 1,000 Context Tokens, the primary is not met whatever
  the accuracy. A mean retrieved context more than 5% from four items' on the
  same questions is reported as not at the same cost.
- **Secondary**: the lower bound of point minus every item is at or above −2.0
  points (the count curve's rule).
- **Reported beside the rules**: accuracy per question type, and the evidence
  metrics on the same questions for the point and both controls. A result that
  meets neither rule is reported the same way.
- The controls' answers come from an earlier pass. The count curve measured how
  much a second answer pass moves on the same contexts (96.0% per-question
  agreement), and the interval over questions carries that variation.
- Whether `evidence` becomes the default is decided after this result. If the
  primary rule is not met, it stays off.

## What it does not say

- Nothing about coverage (2.6.5a2), which this sequence does not use.
- Nothing about any other budget.
- Nothing about stores whose items hold several records. Here an item holds one
  session. The private real-use pack is not part of this claim.
- Nothing about the development questions, which chose the point.

One run. A rerun is made only for an infrastructure failure, and is reported.
Nothing in the construction or the settings changes after any answer is seen.
