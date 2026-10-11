# The keyword seat of `reconstruct`'s modes on LongMemEval-S: registration

Registered before any LongMemEval-S test question is searched at the
candidate commit, and before any answer or judge call.

## Question

2.6.9a1 (commit `dd14527`) has `reconstruct` under `mode: "lite"` or
`mode: "pro"` hold one keyword seat: a place beside the window for a row only
the keyword arms found that the answer does not hold, inside the mode's cap
([#455](https://github.com/Cloto-dev/CPersona/pull/455)). A call that names no
mode is unchanged.

Whether the seat brings more of a question's evidence within the cap is
judged on a sealed private pack, registered separately because its questions
cannot be published. This benchmark asks only:

- Do Lite and Pro answer no worse than they did in 2.6.8?
- Does every returned response stay within its cap?

## The arms

| | 2.6.8 (recorded) | 2.6.9a1 |
| --- | --- | --- |
| Commit | `893db3c` (tag `v2.6.8`) | `dd14527` (tag `v2.6.9a1`) |
| Lite | `mode: "lite"`, `count` 10, cap 3,000 | the same, with the seat |
| Pro | `mode: "pro"`, `count` 15, cap 5,000 | the same, with the seat |

The 2.6.8 contexts and answers are those of the Lite and Pro test
([results](results-omnimemeval-lme-v1_6.md)): Lite answered 323 of 400 and
Pro 353.

## Development (done before this registration)

The seat's rule, one seat holding the row with the most of the question's
parts, was chosen on a private pack of real agent memories and on
LongMemEval-S's 100 development questions. On the development questions,
Lite's context was unchanged and the share of evidence-turn characters Pro
quoted moved from 0.708 to 0.706.

## What was already seen of the test questions

The 400 test questions were used by the evidence-allocation tests and by the
Lite and Pro test, whose contexts, answers and grades are the 2.6.8 arm here.
None was searched at the candidate commit before this registration. This
benchmark is used only to check that neither mode got worse; no accuracy gain
is claimed from it.

## Test

- **The 400 test questions**, searched at `dd14527` through the harness's
  search wrapper as in the Lite and Pro test, once with `mode: "lite"` and
  `count` 10 and once with `mode: "pro"` and `count` 15, on Python 3.11 as
  there.
- **A question is changed** in a mode when its context differs from that
  mode's recorded 2.6.8 context. Only changed questions are answered; every
  other question sends the same prompt and keeps its recorded answer and grade.
- **Answer and judge exactly as before**: OmniMemEval `0b1ea8d` with the
  adapter patch, answer model `gpt-4.1-mini-2025-04-14`, judge `gpt-4o-mini`,
  one judge run, temperature 0, at most 3 concurrent model calls.
- The scripts that build the answer inputs and apply the rules are added by a
  later commit, before any test search, and named here then.

## What may be said

- **Lite** answers at least 320 of the 400 questions (at most three fewer than
  2.6.8's 323, 0.75 points), and **Pro** at least 350 (at most three fewer
  than 2.6.8's 353). Then neither mode answers worse with the seat here.
- **The cap**: every Lite and Pro response is within its cap, counted outside
  the server in `cl100k_base`, and equal to its `used_tokens`. One response
  over the cap means the release does not ship.
- **Reported beside the rules**: the changed questions of each mode and how
  their answers moved, accuracy per question type, the returned JSON's mean,
  95th percentile and maximum, and the evidence metrics of
  [`evidence_metrics.py`](../omnimemeval/evidence_metrics.py).

## What it does not say

- That the seat improves accuracy here. The development questions showed
  almost no change on this benchmark; the claim the seat is built for is
  judged on the private pack.
- Nothing about the default (no mode), other caps or other answer models.

One run. A rerun is made only for an infrastructure failure, and is reported.
Nothing in the construction or the settings changes after any answer is seen.
