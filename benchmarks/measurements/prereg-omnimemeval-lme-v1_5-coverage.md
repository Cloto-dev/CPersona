# The coverage order (`coverage`) against the length floor on OmniMemEval's LongMemEval-S

Registered before any answer or judge call of this study, and before the test
questions were searched with `coverage`. The commit that adds this file is the
evidence of the order.

## Question

[Evidence allocation](../../docs/EVIDENCE_ALLOCATION_DESIGN.md#5-coverage-265a2)
section 5 adds `CPERSONA_RECONSTRUCT_SEQUENCE=coverage`: the order of `whole`,
rebuilt by a greedy pass in which a passage holding a part of the question that
no passage before it holds moves up five places, and one holding only parts
already held moves down five places, except a passage of the first two records.
It was chosen on the private real-use pack's development questions, where it
showed more evidence than `whole`. On LongMemEval-S a record is a session and a
passage is a part of one, so the question here is whether the coverage pass
costs the length floor anything: at the budget of the
[2.6.5a1 test](results-omnimemeval-lme-v1_5-a1.md) and the
[`whole` test](results-omnimemeval-lme-v1_5-whole.md) (2,800), does `coverage`
answer at least as well as `whole` at the same cost?

## Development (done before this registration)

- **The 100 development questions only**
  ([`v1_5_dev_questions.json`](../omnimemeval/v1_5_dev_questions.json)). Search
  only, no model call, done as for the earlier development: copies of the
  [published](results-omnimemeval-lme.md) store, each with its calibration
  file, the boot queue drained before each search, the harness's own search
  wrapper over the ingestion's user ids with two workers, and the budget set
  with `CPERSONA_RECONSTRUCT_FORCED_BUDGET`.
- The orders compared ran at an exploratory commit whose settings were read
  from the environment. The settings kept are this commit's constants: its
  `coverage` gave the exploratory point's contexts for 100 of 100 questions.
- **Reproduction check**: at this commit, `whole` at budget 2,800 gave the
  `whole` development contexts for 100 of 100 questions.
- Measured with [`v1_5_dev_analyze.py`](../omnimemeval/v1_5_dev_analyze.py).
  Context is in `cl100k_base` tokens (the harness's count).

| Point | Context tokens | Items | Answer sessions shown % | All shown % | Evidence turns touched % | Evidence-turn characters quoted % | Evidence share % |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Four items (count curve) | 730.5 | 4.00 | 87.5 | 78.0 | 86.2 | 52.6 | 39.5 |
| Evidence, budget 2,800 | 720.7 | 5.62 | 93.2 | 86.0 | 90.4 | 59.8 | 58.7 |
| **Whole, budget 2,800** | 722.5 | 5.64 | 94.2 | 87.0 | 91.4 | 60.9 | 58.8 |
| **Coverage, budget 2,800** | 721.4 | 5.50 | 94.5 | 88.0 | 91.7 | 61.0 | 60.1 |
| Set aside: every record filled to its head quote first | 721.5 | 3.97 | 87.5 | 78.0 | 87.3 | 53.9 | 45.9 |
| Set aside: the first two records filled first, then coverage | 726.5 | 5.05 | 94.2 | 87.0 | 91.8 | 60.0 | 58.4 |

Every quoted character of every point was found in the store. `coverage`'s
context differs from `whole`'s on 79 of the 100 questions: unlike the length
floor, the coverage pass moves passages on almost every question of this
store.

**The point is budget 2,800**, the budget of the two earlier tests, because the
question is `coverage` against `whole` where `whole` was judged. The
development table did not choose it.

## Test

- **The 2.6.5a1 test's 400 questions**, and only those.
- **Two searches at the commit that adds this file, done as in development**:
  `whole` at budget 2,800 and `coverage` at budget 2,800.
- **`whole`'s test context** for a question is the `whole` test's
  (`cpersona-lme1-v15whole-b2800`) where that test answered it, and the 2.6.5a1
  test's otherwise.
- **Reproduction check, before any call**: the `whole` search must give
  `whole`'s test contexts for all 400 questions
  ([`v1_5_coverage_build.py`](../omnimemeval/v1_5_coverage_build.py) stops
  otherwise).
- **Only the changed questions are answered.** A question is changed when its
  `coverage` context differs from `whole`'s test context. Every other question
  sends the same prompt, so it keeps `whole`'s recorded answer and grade. If no
  question changes, nothing is answered, and the result is that the two orders
  gave the same contexts on the test questions.
- **Answer and judge exactly as in the earlier tests**: OmniMemEval `0b1ea8d`
  with the same adapter patch, answer model `gpt-4.1-mini-2025-04-14`, judge
  `gpt-4o-mini`, one judge run, temperature 0, and at most 3 concurrent model
  calls. The results go in one directory, `cpersona-lme1-v15cov-b2800`.
- **Controls are not answered again**: `whole`'s recorded answers (the `whole`
  test's, and the 2.6.5a1 test's for the questions it did not change), and the
  count curve's for four items, on the same 400 questions.
- [`v1_5_coverage_analyze.py`](../omnimemeval/v1_5_coverage_analyze.py) applies
  the rules below.

## What may be said

- **Primary**: `coverage`'s mean retrieved context (`cl100k_base`, the
  harness's count) on the 400 questions is within 5% of `whole`'s, and the 95%
  bootstrap interval of the paired accuracy difference, `coverage` minus
  `whole` (10,000 resamples over the 400 questions, seed 20261003), has its
  lower bound at or above −2.0 points. When both hold, `coverage` answers no
  worse than `whole` at the same cost.
- **Secondary**: the 2.6.5a1 primary, for `coverage`: its Context Tokens are at
  most 1,000, and the lower bound of `coverage` minus four items is above 0.
  `coverage`'s Context Tokens are the 2.6.5a1 test's (the answer-stage prompt
  tokens the API reported, averaged over its 400 calls), plus, summed over the
  questions whose `coverage` context differs from the 2.6.5a1 context and
  divided by 400, the `o200k_base` tokens of `coverage`'s context minus those
  of the 2.6.5a1 context (`o200k_base` is the answer model's encoding in
  `tiktoken` 0.14.0). The prompt tokens the API reports for the changed calls
  are printed beside it.
- **Reported beside the rules**: the number and question types of the changed
  questions, how their answers moved, accuracy per question type, and the
  evidence metrics of [`evidence_metrics.py`](../omnimemeval/evidence_metrics.py)
  on the 400 questions for `coverage`, `whole` and four items. A result that
  meets neither rule is reported the same way.
- Whether `coverage` replaces `whole` in `lite=true` is decided after this
  result and the private real-use pack's test. The default stays `items`
  either way.

## What it does not say

- Nothing about any other budget, or about `coverage` against the default
  sequence on this benchmark.
- Nothing about the private real-use pack. Its test is registered separately,
  before it is run, because its questions cannot be published; its result is
  reported in counts.
- Nothing about the development questions, which chose the order and its
  constants.

One run. A rerun is made only for an infrastructure failure, and is reported.
Nothing in the construction or the settings changes after any answer is seen.
