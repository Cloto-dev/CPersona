# The keyword seat of `reconstruct`'s modes on LongMemEval-S: results

Registration: [prereg-omnimemeval-lme-2_6_9.md](prereg-omnimemeval-lme-2_6_9.md),
committed and pushed before any test question was searched at the candidate
commit and before any answer or judge call. Read with
[`v2_6_9_analyze.py`](../omnimemeval/v2_6_9_analyze.py).

## Result

- **Lite: rule met.** With the seat, Lite answered 324 of the 400 test
  questions (81.00%), against the floor of 320; 2.6.8's Lite answered 323.
- **Pro: rule met.** With the seat, Pro answered 354 of 400 (88.50%), against
  the floor of 350; 2.6.8's Pro answered 353.
- **The cap: held.** Every Lite and Pro response of the 400 test questions was
  within its cap, counted outside the server in `cl100k_base`, and equal to its
  `used_tokens`: 0 over the cap and 0 unequal for each mode.

| Mode | Accuracy | Answer sessions shown % | All shown % | Evidence turns touched % | Evidence-turn characters quoted % | Evidence share % |
| --- | --- | --- | --- | --- | --- | --- |
| 2.6.9a1 Lite | 81.00 | 95.7 | 92.2 | 91.8 | 51.7 | 63.6 |
| 2.6.8 Lite (recorded) | 80.75 | 95.7 | 92.2 | 91.8 | 51.7 | 63.6 |
| 2.6.9a1 Pro | 88.50 | 98.2 | 97.2 | 97.0 | 71.3 | 28.2 |
| 2.6.8 Pro (recorded) | 88.25 | 98.2 | 97.2 | 97.1 | 72.3 | 28.9 |

Accuracy by question type, % (questions):

| Mode | Knowledge update (64) | Multi-session (109) | Single-session assistant (46) | Single-session preference (22) | Single-session user (53) | Temporal reasoning (106) |
| --- | --- | --- | --- | --- | --- | --- |
| 2.6.9a1 Lite | 85.9 | 69.7 | 82.6 | 90.9 | 94.3 | 80.2 |
| 2.6.8 Lite | 85.9 | 69.7 | 82.6 | 86.4 | 94.3 | 80.2 |
| 2.6.9a1 Pro | 87.5 | 82.6 | 95.7 | 100.0 | 96.2 | 85.8 |
| 2.6.8 Pro | 85.9 | 84.4 | 95.7 | 95.5 | 96.2 | 84.9 |

Size of what each mode returned with the seat:

| Mode | Returned JSON, mean (cl100k) | 95th percentile | Maximum | Cap |
| --- | --- | --- | --- | --- |
| Lite | 957.0 | 1,188 | 1,364 | 3,000 |
| Pro | 4,970.4 | 4,999 | 5,000 | 5,000 |

## What was run

- **30 of the 400 questions changed** under Lite against 2.6.8's Lite. One
  moved from wrong to right (question 149, single-session preference), and the
  other 29 kept their outcome: 25 right and 4 wrong in both.
- **349 of the 400 questions changed** under Pro. 10 moved from wrong to right
  and 9 from right to wrong; 301 stayed right and 29 stayed wrong. Pro's
  responses fill their cap, so a held item takes tokens from the rest of the
  response: of the 350 Pro responses that differ from 2.6.8's, 321 kept the
  same records in the same order in the window and added a held item, which
  shortened the window's quotations. On the development questions Pro's
  context had moved little (the share of evidence-turn characters quoted, 0.708
  to 0.706); on the test it moved from 72.3% to 71.3%.
- Every other question sends the same prompt as in 2.6.8 and keeps its
  recorded answer and grade.
- One answer pass and one judge run for each mode, with no rerun and no failed
  search, answer or judge call. The 30 Lite calls reported 30,884 prompt
  tokens and 3,649 completion tokens; the 349 Pro calls 1,507,729 and 46,399.
  The harness's usage record names the answer model `gpt-4.1-mini-2025-04-14`
  for both.
- The evidence metrics come from
  [`evidence_metrics.py`](../omnimemeval/evidence_metrics.py)'s own functions.

## The private pack

The seat's claim was judged on a sealed pack of real agent memories, under a
test registered separately before it ran, because its questions cannot be
published. None of its 365 test questions had been searched with either commit
before the test. Each question's weight follows the pack's population; one
stratum the test draw did not reach (about 3 × 10⁻⁹ of the population) is left
out of every figure.

- **Primary, the share of questions whose every evidence record Pro's response
  held within its cap.** 2.6.9a1 48.45% against 2.6.8's 44.93%: +3.51 points,
  z = 4.48 against the one-sided threshold of 1.645 (95% interval +1.97 to
  +5.05). Of the 463 evidence records, Pro held 296 against 269: 27 gained and
  none lost. The rule was met.
- **Lite, the same measure: direction not reversed.** 41.23% against 39.86%,
  +1.37 points (95% interval +0.15 to +2.59); 14 evidence records gained and 3
  lost.
- **The reader's accuracy, Pro: direction not reversed.** Each Pro arm was read
  twice, because a second reading of the same contexts had moved Pro's accuracy
  by about 2.6 points in an earlier exploration; a question scores the mean of
  its two readings. 2.6.9a1 67.08% against 2.6.8's 63.48%: +3.60 points (95%
  interval +0.60 to +6.59). The interval is reported and decides nothing.
- **Reported only:** Lite, read once, 54.55% against 54.63% (−0.07 points).
- **The cap** held on every response of all four arms.

## Speed, reported only

On the Intel N150 and the 100,000 memories of real length of
[the block index file's run](results-recall-latency-block-index-file.md),
with the same 25 questions and the query embedded by the model
(`onnx_jina_v5_nano`), [`perf_reconstruct_modes.py`](perf_reconstruct_modes.py)
timed the whole `reconstruct` call in both modes, in two pairs of runs. No rule
was registered for speed.

| Mode | 2.6.8, first pair | 2.6.9a1, first pair | 2.6.8, second pair | 2.6.9a1, second pair |
| --- | ---: | ---: | ---: | ---: |
| Lite, median | 1,118.2 ms | 1,108.0 ms | 1,112.1 ms | 1,144.7 ms |
| Pro, median | 1,216.7 ms | 1,193.0 ms | 1,212.9 ms | 1,244.2 ms |

The two pairs moved in opposite directions, each within 3%. The slowest
call was 1.57 s under Lite and 1.70 s under Pro. No call errored, and the load
fell below 1.0 before every timed run.

## What it does not say

- That the seat raises accuracy on LongMemEval-S. The changes on this
  benchmark are within one question of 2.6.8 in each mode; the claim the seat
  is built for was judged on the private pack.
- That the seat makes Lite more accurate. On the private pack Lite held more
  evidence, but its reader's accuracy did not move.
- Nothing about a call that names no mode, which is unchanged, or about other
  caps, counts or answer models.
