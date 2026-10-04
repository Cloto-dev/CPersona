# The length floor (`whole`) against the evidence sequence on OmniMemEval's LongMemEval-S

Registered before any answer or judge call of this study, and before the test
questions were searched with `whole`. The commit that adds this file is the
evidence of the order.

## Question

[Evidence allocation](../../docs/EVIDENCE_ALLOCATION_DESIGN.md#a-floor-by-record-length)
section 4 adds `CPERSONA_RECONSTRUCT_SEQUENCE=whole`: every head record no
longer than a quote comes first, whole, in item order, and the passages of the
longer records follow in the evidence order of 2.6.5a1. It was built for stores
of short records. On LongMemEval-S a record is a session, and almost every
record is far longer than a quote, so the question here is what the floor costs
the evidence order: at the budget of the
[2.6.5a1 test](results-omnimemeval-lme-v1_5-a1.md) (2,800), does `whole`
answer at least as well as `evidence` at the same cost?

## Development (done before this registration)

- **The 100 development questions only**
  ([`v1_5_dev_questions.json`](../omnimemeval/v1_5_dev_questions.json)). Search
  only, no model call, done as for the 2.6.5a1 development: copies of the
  [published](results-omnimemeval-lme.md) store, each with its calibration
  file, the boot queue drained before each search, the harness's own search
  wrapper over the ingestion's user ids with two workers, and the budget set
  with `CPERSONA_RECONSTRUCT_FORCED_BUDGET`.
- The `whole` searches ran at an exploratory commit that also carried a second
  floor, since removed. Its `whole` is the same code as this commit's.
- **Reproduction check**: at that commit, the evidence sequence at budget 2,800
  gave the 2.6.5a1 development contexts for 100 of 100 questions (every field
  but the two timing fields).
- Measured with [`v1_5_dev_analyze.py`](../omnimemeval/v1_5_dev_analyze.py).
  Context is in `cl100k_base` tokens (the harness's count).

| Point | Context tokens | Items | Answer sessions shown % | All shown % | Evidence turns touched % | Evidence-turn characters quoted % | Evidence share % |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Evidence, budget 2,400 | 623.0 | 5.04 | 93.2 | 86.0 | 90.1 | 58.5 | 61.2 |
| Whole, budget 2,400 | 621.9 | 5.02 | 94.2 | 87.0 | 91.2 | 59.6 | 61.5 |
| **Evidence, budget 2,800** | 720.7 | 5.62 | 93.2 | 86.0 | 90.4 | 59.8 | 58.7 |
| **Whole, budget 2,800** | 722.5 | 5.64 | 94.2 | 87.0 | 91.4 | 60.9 | 58.8 |
| Evidence, budget 3,200 | 825.7 | 6.18 | 94.5 | 90.0 | 91.3 | 60.3 | 54.8 |
| Whole, budget 3,200 | 822.3 | 6.19 | 95.5 | 91.0 | 92.4 | 61.3 | 55.1 |
| Evidence, budget 4,000 | 1,036.0 | 7.20 | 94.5 | 90.0 | 91.9 | 62.6 | 50.3 |
| Whole, budget 4,000 | 1,035.4 | 7.19 | 95.5 | 91.0 | 92.9 | 63.7 | 50.5 |

Every quoted character of every point was found in the store. Of the store's
23,867 records, 379 (1.6%) are no longer than a quote (800 characters), so
`whole` differs from `evidence` only on a question whose items include such a
session.

**The point is budget 2,800**, the budget of the 2.6.5a1 test, because the
question is `whole` against `evidence` where `evidence` was judged. The
development table did not choose it.

## Test

- **The 2.6.5a1 test's 400 questions**, and only those.
- **Two searches at the commit that adds this file, done as in development**:
  the evidence sequence at budget 2,800 and `whole` at budget 2,800.
- **Reproduction check, before any call**: the evidence sequence must give the
  2.6.5a1 test's contexts (`cpersona-lme1-v15a1-b2800`) for all 400 questions
  ([`v1_5_whole_build.py`](../omnimemeval/v1_5_whole_build.py) stops
  otherwise).
- **Only the changed questions are answered.** A question is changed when its
  `whole` context differs from its 2.6.5a1 test context. Every other question
  sends the same prompt, so it keeps the 2.6.5a1 test's recorded answer and
  grade; answering it again would only add the variation of a second answer
  pass to a difference that is zero. If no question changes, nothing is
  answered, and the result is that the two sequences gave the same contexts on
  the test questions.
- **Answer and judge exactly as in the 2.6.5a1 test**: OmniMemEval `0b1ea8d`
  with the same adapter patch, answer model `gpt-4.1-mini-2025-04-14`, judge
  `gpt-4o-mini`, one judge run, temperature 0, and at most 3 concurrent model
  calls. The results go in one directory, `cpersona-lme1-v15whole-b2800`.
- **Controls are not answered again**: the 2.6.5a1 test's answers for the
  evidence sequence, and the count curve's for four items, on the same 400
  questions.
- [`v1_5_whole_analyze.py`](../omnimemeval/v1_5_whole_analyze.py) applies the
  rules below.

## What may be said

- **Primary**: `whole`'s mean retrieved context (`cl100k_base`, the harness's
  count) on the 400 questions is within 5% of the evidence sequence's, and the
  95% bootstrap interval of the paired accuracy difference, `whole` minus
  `evidence` (10,000 resamples over the 400 questions, seed 20261003), has its
  lower bound at or above −2.0 points. When both hold, `whole` answers no worse
  than `evidence` at the same cost.
- **Secondary**: the 2.6.5a1 primary, for `whole`: its Context Tokens are at
  most 1,000, and the lower bound of `whole` minus four items is above 0.
  `whole`'s Context Tokens are the 2.6.5a1 test's (the answer-stage prompt
  tokens the API reported, averaged over its 400 calls), plus, summed over the
  changed questions and divided by 400, the `o200k_base` tokens of `whole`'s
  context minus those of the 2.6.5a1 context (`o200k_base` is the answer
  model's encoding in `tiktoken` 0.14.0). The prompt tokens the API reports for
  the changed calls are printed beside it.
- **Reported beside the rules**: the number and question types of the changed
  questions, accuracy per question type, and the evidence metrics of
  [`evidence_metrics.py`](../omnimemeval/evidence_metrics.py) on the 400
  questions for `whole`, `evidence` and four items. A result that meets neither
  rule is reported the same way.
- Whether `whole` replaces `evidence` as the evidence sequence that is
  recommended is decided after this result and the private real-use pack's
  test. The default stays `items` either way.

## What it does not say

- Nothing about any other budget, or about `whole` against the default
  sequence.
- Nothing about the private real-use pack. Its test is registered separately,
  before it is run, because its questions cannot be published; its result is
  reported in counts.
- Nothing about the development questions, which shaped the floor.

One run. A rerun is made only for an infrastructure failure, and is reported.
Nothing in the construction or the settings changes after any answer is seen.
