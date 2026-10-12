# The 2.6.10 line of `reconstruct`'s modes on LongMemEval-S: registration

Registered before any LongMemEval-S test question is searched at the
candidate commit, and before any answer or judge call.

## Question

2.6.10a4 (commit `e8d9e21`) changes what `reconstruct` returns under
`mode: "lite"` and `mode: "pro"`. A call that names no mode is unchanged. The
changes against 2.6.9:

- **2.6.10a1**: the evidence order breaks ties the same way on every Python version.
- **2.6.10a2**: Pro holds two more block places ([#461](https://github.com/Cloto-dev/CPersona/pull/461)).
- **2.6.10a3** ([#463](https://github.com/Cloto-dev/CPersona/pull/463)):
  - Lite reads 3,600 characters instead of 2,800.
  - Both modes hold extra keyword places for keyword-only rows the gate refused on their rrf score.
- **2.6.10a4**: Pro holds eight depth places, for rows a deeper ranking of the same recall admits ([#469](https://github.com/Cloto-dev/CPersona/pull/469)).

Whether these bring more of a question's evidence within the cap is judged on
a sealed private pack, registered separately because its questions cannot be
published. This benchmark asks only:

- Do Lite and Pro answer no worse than they did in 2.6.9?
- Does every returned response stay within its cap?

## The arms

| | 2.6.9 (recorded) | 2.6.10a4 |
| --- | --- | --- |
| Commit | `79a9ee8` (tag `v2.6.9`) | `e8d9e21` (tag `v2.6.10a4`) |
| Lite | `mode: "lite"`, `count` 10, cap 3,000 | the same |
| Pro | `mode: "pro"`, `count` 15, cap 5,000 | the same |

2.6.9's code is 2.6.9a1's (`dd14527`) with only the version changed. Its
contexts and answers are therefore those its own check recorded at 2.6.9a1
([results](results-omnimemeval-lme-2_6_9.md)): Lite answered 324 of 400 and
Pro 354.

## Development (done before this registration)

- **Lite's 3,600 characters** were chosen on LongMemEval-S's 100 development
  questions, searched only, with no model called. Against 2,800, every answer
  session was shown on 4 more questions and on none fewer, and the
  answer-stage prompt grew by about 20% (counted in the model's tokenizer, not
  sent).
- **The places** were chosen on a private pack of real agent memories.

## What was already seen of the test questions

- **Earlier use.** The 400 test questions were used by the evidence-allocation tests, the Lite and Pro test, and the 2.6.9 check. Their contexts, answers and grades are the 2.6.9 arm here.
- **Not yet searched.** None was searched at the candidate commit before this registration.
- **Scope of the claim.** This benchmark is used only to check that neither mode got worse; no accuracy gain is claimed from it.

## Test

- **The 400 test questions** are searched at `e8d9e21` through the harness's
  search wrapper, as in the Lite and Pro test: once with `mode: "lite"` and
  `count` 10, and once with `mode: "pro"` and `count` 15, on Python 3.11.
- **A question is changed** in a mode when its context differs from that
  mode's recorded 2.6.9 context. Only changed questions are answered; every
  other question sends the same prompt and keeps its recorded answer and grade.
  Lite's larger budget is expected to change nearly every Lite question.
- **Answer and judge exactly as before**:
  - OmniMemEval `0b1ea8d` with the adapter patch
  - answer model `gpt-4.1-mini-2025-04-14`, judge `gpt-4o-mini`
  - one judge run, temperature 0, at most 3 concurrent model calls
- The scripts that build the answer inputs and apply the rules are added by a
  later commit, before any test search, and named here then.

### Recorded before the test

Added by a later commit, before any test question was searched at the
candidate commit.

- **The scripts**:
  - [`v2_6_10_build.py`](../omnimemeval/v2_6_10_build.py) writes the answer inputs of each mode's changed questions.
  - [`v2_6_10_analyze.py`](../omnimemeval/v2_6_10_analyze.py) applies the rules.
  - 2.6.9's answers are read as its own check recorded them. Read that way, they total 324 for Lite and 354 for Pro, the published figures.
- **The interpreter**: Python 3.11.15, as in the Lite and Pro test.

## What may be said

- **Neither mode answers worse.** Among a mode's changed questions, count those 2.6.9 answered right and 2.6.10a4 wrong (L) and the reverse (W).
  - A mode fails only when L exceeds W beyond chance: an exact binomial test of L among L + W at one half, one-sided p < 0.05.
  - **Why not a fixed margin.** The 2.6.9 check used one (at most three fewer correct answers). That suited the few questions it re-answered, but with nearly every question re-answered, chance alone would cross it about one time in four.
- **The cap**: every Lite and Pro response is within its cap, counted outside
  the server in `cl100k_base`, and equal to its `used_tokens`. One response
  over the cap means the release does not ship.
- **Reported beside the rules**:
  - correct answers and their net change
  - L and W
  - accuracy per question type
  - the returned JSON's mean, 95th percentile and maximum
  - the evidence metrics of [`evidence_metrics.py`](../omnimemeval/evidence_metrics.py)

## What it does not say

- That 2.6.10 improves accuracy here. The claim the changes are built for is
  judged on the private pack.
- Nothing about the default (no mode), other caps or other answer models.

One run. A rerun is made only for an infrastructure failure, and is reported.
Nothing in the construction or the settings changes after any answer is seen.
