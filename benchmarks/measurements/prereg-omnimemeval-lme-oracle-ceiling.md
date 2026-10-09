# The answer model with the gold evidence: a reference for LongMemEval-S accuracy targets

Registered before any answer or judge call of this study. The commit that adds
this file is the evidence of the order.

## Question

The next reconstruct work sets two development targets on OmniMemEval's
LongMemEval-S: a small response (today's `lite=true`, 80.75% on the 400 test
questions, [results](results-omnimemeval-lme-v1_5-whole.md)) and a larger one
with a 90% target and at most 5,000 `cl100k_base` tokens. Before building toward
90%, this measures what the same answer model does when retrieval is perfect:
given LongMemEval's own gold evidence for each question, rendered the way the
CPersona adapter renders a recall, how many questions does it answer?

This is not a test of CPersona. No search runs and no CPersona code is involved.
It is a reference for the targets.

## Data

- **Questions**: the 500 questions of OmniMemEval's LongMemEval-S (the
  harness's `longmemeval_s_cleaned.json`), read with the harness's own loader.
- **Gold evidence**: `longmemeval_oracle.json` from the LongMemEval cleaned
  release (`xiaowu0162/longmemeval-cleaned` on Hugging Face, SHA-256
  `821a2034d219ab45846873dd14c14f12cfe7776e73527a483f9dac095d38620c`, the same
  as the file's published hash). For each question it holds only the answer
  sessions, and marks the turns that hold the answer (`has_answer`). Matched to
  the harness's questions by `question_id`, with the question text equal, for
  500 of 500.
- **The 400 test questions** are the ones every earlier test used: all but the
  100 development questions in
  [`v1_5_dev_questions.json`](../omnimemeval/v1_5_dev_questions.json).

## Arms

Built by [`oracle_ceiling_build.py`](../omnimemeval/oracle_ceiling_build.py).
Each answer session is one block, as the adapter renders a recalled record: its
time in brackets, its date line, then one `role: content` paragraph per kept
turn. Sessions are in date order, and the blocks are wrapped by the harness's
own context template.

- **`turns`**: only the turns marked `has_answer`. An answer session with no
  marked turn adds nothing; the 21 questions with no marked turn at all are all
  abstention questions, so their context is empty.
- **`fit5000`**: the marked turns, then the same sessions' other turns nearest
  to them by turn distance (ties: earlier session, then earlier turn), each kept
  only if the whole context stays within 5,000 `cl100k_base` tokens. An answer
  session with no marked turn grows from its first turn, since such sessions
  still carry the dates some questions need (45 such sessions outside the
  abstention questions, 32 of them in temporal-reasoning questions).

Sizes, measured before this registration with `--check-only` (context in
`cl100k_base`, the harness's count, over the 500 questions):

| Arm | Mean | Median | Max | Over 5,000 |
| --- | --- | --- | --- | --- |
| `turns` | 211.6 | 200 | 1,061 | 0 |
| `fit5000` | 4,092.9 | 4,852 | 5,000 | 0 |

The answer sessions whole would average 5,754 tokens, and 300 of the 500
questions would exceed 5,000, so whole sessions are not an arm.

## Run

- **Answer and judge as in every earlier test**: OmniMemEval `0b1ea8d` with the
  same adapter patch, answer model `gpt-4.1-mini-2025-04-14`, judge
  `gpt-4o-mini`, one answer pass and one judge run, temperature 0, at most 3
  concurrent model calls, from the harness's answer step (`--from-step 3`). One
  results directory per arm: `cpersona-lme1-oracle-turns` and
  `cpersona-lme1-oracle-fit5000`.
- **All 500 questions per arm**: 1,000 answer calls and 1,000 judge calls in
  total, about 2.4 million answer-stage input tokens.
- A pass that stops early is run again from the answer step, as in earlier
  tests, and the result reports how many passes each arm took.

## What may be said

Read with [`oracle_ceiling_analyze.py`](../omnimemeval/oracle_ceiling_analyze.py).

- **The registered reading is `fit5000` on the 400 test questions.** At 360 or
  more correct (90%), this answer model reaches 90% from at most 5,000 tokens of
  gold evidence chosen this way, so a 90% target is not ruled out for a
  retrieval that delivers such evidence. Below 360, it stays below 90% even with
  that evidence, and the result says whether the upper end of the 95% Wilson
  interval is below 90% too.
- **Reported, not judged**: both arms on all 500 and on the test 400, the
  Wilson interval, accuracy by question type, and each arm against today's lite
  on the same 400 questions (the paired difference with a 95% bootstrap
  interval, 10,000 resamples over questions, seed 20261009, and the questions
  each side answered alone).

## What this does not say

- **It is a reference, not a bound.** `fit5000` is one way to choose 5,000
  tokens of gold evidence; another choice could do better or worse. A result
  below 90% says that this choice, with this answer model, does not reach it.
- **The `has_answer` marks are turns, not facts.** A turn can be marked and
  still not state the answer alone, and some answer sessions carry no mark.
- **Nothing about other answer models.** A stronger answer model moves this
  reference, and a gain from changing the model is not a gain of the memory
  layer.
