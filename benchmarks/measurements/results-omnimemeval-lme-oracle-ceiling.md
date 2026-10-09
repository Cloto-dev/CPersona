# The answer model with the gold evidence: results

Registration: [prereg-omnimemeval-lme-oracle-ceiling.md](prereg-omnimemeval-lme-oracle-ceiling.md),
committed and pushed before any answer or judge call. Read with
[`oracle_ceiling_analyze.py`](../omnimemeval/oracle_ceiling_analyze.py).

## Result

- **Registered reading met.** Given at most 5,000 `cl100k_base` tokens of
  LongMemEval's gold evidence (`fit5000`), the answer model answered **365 of
  the 400 test questions (91.25%)**, 95% Wilson interval 88.07% to 93.64%. That
  is at least 360, so a 90% target is not ruled out for a retrieval that
  delivers such evidence.
- **The marked answer turns alone did as well or better.** With only the turns
  LongMemEval marks as holding the answer (`turns`, 211.6 tokens of context on
  average), it answered **374 of 400 (93.50%)**, Wilson 90.65% to 95.53%.
- **Both are well above today's lite** on the same 400 questions (323, 80.75%,
  [results](results-omnimemeval-lme-v1_5-whole.md)): `turns` +12.75 points
  (95% bootstrap +8.75 to +17.00), `fit5000` +10.50 (+6.50 to +14.50).

| Arm | Context (cl100k, mean) | Answer prompt (API, mean) | All 500 | Test 400 | 95% Wilson (test 400) |
| --- | --- | --- | --- | --- | --- |
| `turns` | 211.6 | 494.3 | 467 (93.40%) | 374 (93.50%) | 90.65 to 95.53 |
| `fit5000` | 4,092.9 | 4,321.4 | 460 (92.00%) | 365 (91.25%) | 88.07 to 93.64 |
| Today's lite | 722.2 | 993.8 | — | 323 (80.75%) | 76.60 to 84.31 |

Accuracy by question type on the test 400, % (questions):

| Arm | Knowledge update (64) | Multi-session (109) | Single-session assistant (46) | Single-session preference (22) | Single-session user (53) | Temporal reasoning (106) |
| --- | --- | --- | --- | --- | --- | --- |
| `turns` | 96.9 | 89.9 | 93.5 | 100.0 | 96.2 | 92.5 |
| `fit5000` | 87.5 | 88.1 | 97.8 | 95.5 | 100.0 | 88.7 |
| Today's lite | 85.9 | 69.7 | 82.6 | 86.4 | 94.3 | 80.2 |

## What it says

- **The answer model is not what holds today's lite at 80.75%.** With the right
  turns in front of it, about 212 tokens of them, it answers 93.5%. The gap is
  in which text reaches it, not in how much: today's lite already sends 722
  tokens of context, more than three times as much.
- **More gold text did not raise accuracy.** On the same 400 questions,
  `fit5000` minus `turns` is −2.25 points (95% bootstrap −5.25 to +0.75; 14
  questions right only with `fit5000`, 23 only with `turns`). This comparison
  was not registered and its interval includes zero, so it says that the extra
  surrounding turns bought nothing measurable, not that they hurt. The largest
  drop from `turns` to `fit5000` is knowledge update (96.9% to 87.5%, six
  questions), the type whose answer depends on telling a newer value from an
  older one.
- **Where today's lite loses the most questions** is multi-session (76 right,
  against 96 with `fit5000` and 98 with `turns`) and temporal reasoning (85,
  against 94 and 98).

## What it does not say

- **A retriever does not have the `has_answer` marks.** `turns` uses
  LongMemEval's own annotation of which turns hold the answer; it shows what a
  perfect choice of turns would give, not what any retrieval can reach.
- **A reference, not a bound**, as registered: another choice of gold text
  could do better or worse. The 24 abstention questions in the test 400 were
  answered right 21 times under `turns`, whose context is empty for most of
  them, and 23 times under `fit5000`.
- **Nothing about other answer models.**

## What was run

- Inputs built with
  [`oracle_ceiling_build.py`](../omnimemeval/oracle_ceiling_build.py) at the
  registration commit: 500 of 500 questions matched to the oracle file by
  `question_id`, with the question text equal; sizes as registered.
- OmniMemEval `0b1ea8d` with the same adapter patch, answer model
  `gpt-4.1-mini-2025-04-14`, judge `gpt-4o-mini`, temperature 0, at most 3
  concurrent model calls, from the answer step. One pass per arm, with no rerun:
  the harness reported 500 of 500 answers and 500 of 500 judgments successful
  for each arm.
- API usage, answer step: `turns` 500 calls, 247,148 prompt and 60,212
  completion tokens; `fit5000` 500 calls, 2,160,685 prompt and 55,109
  completion tokens.
