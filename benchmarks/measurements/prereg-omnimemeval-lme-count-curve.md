# How many reconstruct items does an answer need? A count curve on OmniMemEval's LongMemEval-S

Registered before any answer or judge call of this study. The commit that adds
this file is the evidence of the order.

## Question

`reconstruct` returns one fixed sequence cut to its payload budget: every head
quote in item order, then each item's excerpts in turn
([section 7](../../docs/RELIABLE_RECALL_2_6.md#the-reconstruction-window),
invariant 9). Since v1.2 a head is sized by its item's place (800 characters
for the first five items, 400 after them), and the default budget is the sum
of the heads. On a store whose records are whole conversation sessions, the
heads are full, so a smaller budget removes items from the end rather than
shortening any quote.

How do accuracy and Context Tokens change as items are removed? This is an
instrument for the next step of the reconstruction design, not a claim that
any setting is better. It reads the results of
[the v1.2 run](results-omnimemeval-lme.md) and changes no code.

## Instrument

- The search results of that run's arm B (v1.2, `4c01ccc`), all 500 questions,
  as saved by the harness. **No search is run again.**
- Each question's context is the harness's `"Conversation memories:\n\n"`
  followed by one block per item, joined by one newline. A block starts at a
  line that is exactly an ISO-8601 time in brackets (other lines start with
  `[` in 18 contexts, so only that form delimits). Every context is checked to
  split and join back byte for byte before anything is written.
- The first `k` blocks are kept, with the same prefix and joining. A block
  keeps its excerpts, so `k` counts items, not characters. 55 contexts carry an
  excerpt inside an item.
- Items per question in the saved run: 12 in 475 questions (10 in the window
  and 2 held by the block reservation, which follow the window), fewer in 25.
- Answer and judge exactly as in that run: OmniMemEval `0b1ea8d` with the same
  adapter patch, answer model `gpt-4.1-mini-2025-04-14`, judge `gpt-4o-mini`,
  one judge run, temperature 0, at most 3 concurrent model calls. Each point
  answers into a results directory of its own, because the harness rewrites its
  token-usage file on every pass.

## Points

`k` = 1, 2, 3, 4, 5, 6, 8, 10, and **full** (every item of the saved run, the
same contexts as the published run, answered again in this study). Mean
retrieved context in `cl100k_base` tokens, computed from the saved contexts
before any call:

| k | 1 | 2 | 3 | 4 | 5 | 6 | 8 | 10 | full |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Context tokens (mean) | 184.9 | 365.1 | 544.1 | 723.7 | 903.5 | 995.7 | 1,177.3 | 1,355.4 | 1,531.4 |

The full point reproduces the published run's mean of 1,531.4.

## Measures

Accuracy overall and per question type as the harness judges it; Context
Tokens as the API reports them (answer prompt plus context); the mean
retrieved-context tokens above; Context Tokens per correct answer.

## What may be said

- **Noise first.** The full point is compared with the published arm B
  answers on the same contexts. If its accuracy differs from the published
  81.60% by more than 2.0 points, the study is reported as too noisy to read and
  nothing below is claimed.
- **A point holds** when the 95% bootstrap interval of the paired difference in
  accuracy, `k` minus full (10,000 resamples over questions, seed 20261003),
  has its lower bound at or above −2.0 points. The study reports the smallest
  `k` that holds and its Context Tokens. A point that does not hold is reported
  as the two numbers, without a claim either way.
- Every point is reported, including those that hold nothing, with the curve as
  measured.

## What it does not say

- Nothing about shorter quotes: a smaller head size at ten items is a separate
  study, and needs the search run again.
- Nothing about a different allocation. The curve is the baseline against which
  such a change would be measured.
- A cut by items is not a cut by characters where an item carries an excerpt
  (55 questions); the characters each point used are reported as measured.

One run per point. A rerun is made only for an infrastructure failure, and is
reported. Nothing in the construction or the settings changes after any answer
is seen.
