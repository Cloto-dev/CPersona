# The Lite and Pro modes of `reconstruct`: results

Registration: [prereg-omnimemeval-lme-v1_6.md](prereg-omnimemeval-lme-v1_6.md),
committed and pushed before the test questions were searched with a mode and
before any answer or judge call. Read with
[`v1_6_analyze.py`](../omnimemeval/v1_6_analyze.py).

## Result

- **Lite: rule met.** Lite answered 323 of the 400 test questions (80.75%),
  against the floor of 320. That is the recorded 323 of 2.6.7's `lite: true`:
  Lite answers no worse than the setting it replaces.
- **Pro's direction: not reversed.** Pro answered 353 of 400 (88.25%). The
  paired difference, Pro minus Lite, is +7.50 points, with a 95% bootstrap
  interval of +3.75 to +11.25. Pro alone answered 46 questions right and Lite
  alone 16; the exact binomial test of those 62 gives p < 0.001. The interval
  and the test are reported and decide nothing.
- **The cap: held.** Every Lite and Pro response of the 400 test questions was
  within its cap, counted outside the server, and equal to its `used_tokens`:
  0 over the cap and 0 unequal for each mode.

| Mode | Accuracy | Of gold-evidence 5,000 (91.25%) | Of marked turns (93.50%) | Context (cl100k) | Answer sessions shown % | All shown % | Evidence turns touched % | Evidence-turn characters quoted % | Evidence share % |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Lite | 80.75 | 88.5% | 86.4% | 722.0 | 95.7 | 92.2 | 91.8 | 51.7 | 63.6 |
| Pro | 88.25 | 96.7% | 94.4% | 4,152.8 | 98.2 | 97.2 | 97.1 | 72.3 | 28.9 |
| 2.6.7 `lite: true` (recorded) | 80.75 | 88.5% | 86.4% | 722.2 | 95.7 | 92.2 | 91.8 | 51.7 | 63.6 |

Accuracy by question type, % (questions):

| Mode | Knowledge update (64) | Multi-session (109) | Single-session assistant (46) | Single-session preference (22) | Single-session user (53) | Temporal reasoning (106) |
| --- | --- | --- | --- | --- | --- | --- |
| Lite | 85.9 | 69.7 | 82.6 | 86.4 | 94.3 | 80.2 |
| Pro | 85.9 | 84.4 | 95.7 | 95.5 | 96.2 | 84.9 |

Size of what each mode returns, and what the answer stage was sent:

| Mode | Returned JSON, mean (cl100k) | 95th percentile | Maximum | Cap | Context Tokens |
| --- | --- | --- | --- | --- | --- |
| Lite | 955.6 | 1,175 | 1,296 | 3,000 | 993.5 |
| Pro | 4,970.8 | 4,999 | 5,000 | 5,000 | 4,355.4 |

Context Tokens are the answer-stage prompt tokens the API reported, averaged
over the calls (for Lite, as registered: the recorded calls, adjusted by the
token difference of the changed contexts).

## What was run

- Checks 2 to 4 of the registration passed before the test, as recorded there.
- **6 of the 400 questions changed** under Lite against 2.6.7's `lite: true`:
  questions 100 and 126 (multi-session), 303, 306 and 352 (temporal reasoning),
  and 375 (knowledge update). Each kept its outcome: four right and two wrong
  in both. Every other question sends the same prompt and keeps its recorded
  answer and grade.
- Pro answered all 400 questions.
- One answer pass and one judge run for each, with no rerun and no failed
  call. The 6 Lite calls reported 6,153 prompt tokens and 1,005 completion
  tokens; the 400 Pro calls 1,742,153 and 54,738. The harness's usage record
  names the answer model `gpt-4.1-mini-2025-04-14` for both.
- The evidence metrics come from
  [`evidence_metrics.py`](../omnimemeval/evidence_metrics.py)'s own functions.

## The private packs

Their tests were registered separately, before they ran, because their
questions cannot be published.

- **Primary, the sealed pack (624 questions).** Pro answered 66.14% and Lite
  57.07%: a difference of +9.07 points, z = 4.69 against the one-sided
  threshold of 1.645. Pro was ahead on all seven question types. The rule was
  met.
- **The real-use pack (150 questions).** Lite answered 109, against its floor
  of 108; two questions changed against 2.6.7's `lite: true`, both because the
  cap applied, and both kept their outcome. Pro answered 121: +12 over Lite,
  with a 95% interval of +1 to +23. The direction is not reversed.
- **The cap** held on every response of both modes on both packs.

## What it does not say

- Nothing about Pro against the response without a mode. No rule compared
  them. On the sealed pack, an exploration after the test found them level
  within the variation of a second reading of the same contexts.
- That Pro answers better at the same cost. Pro sends about four times
  Lite's context; the claim is only that, with its cap guaranteed, it is not
  worse than Lite, and here and on the sealed pack it was better.
- Nothing about caps other than 3,000 and 5,000, or counts other than 10 and
  15.
