# Lite and Pro modes for `reconstruct` on OmniMemEval's LongMemEval-S

Registered before the implementation of the two modes, before any test
question was searched with either of them, and before any answer or judge call
of this study. The commit that adds this file is the evidence of the order.
The implementation's commit, and the result of the checks in "Before the test",
are added by a later commit to this file before any test search is made;
nothing else in this file changes then.

## Question

The release adds `mode: "lite" | "pro"` to `reconstruct`, with a cap on the
`cl100k_base` tokens of the whole JSON the tool returns. On LongMemEval-S, this
registration asks three things:

- Does Lite answer no worse than `lite: true` of 2.6.7, which it replaces?
- Is Pro's accuracy, against Lite's on the same questions, at least not lower?
  The primary test of Pro against Lite is on a sealed private pack, registered
  separately because its questions cannot be published; this benchmark checks
  that the direction does not reverse.
- Does every returned response stay within its cap?

## The two modes

| | `mode: "lite"` | `mode: "pro"` | Neither |
| --- | --- | --- | --- |
| Sequence | `whole` (as `lite: true` in 2.6.7) | `whole`, with records shown whole only among the first 10 items; a short record of a later item competes as one passage in the evidence order | 2.6.7's default (`items`) |
| `count` | 10 | 15 | 2.6.7's default |
| Character budget | 2,800 (as `lite: true` in 2.6.7) | none, unless the caller passes `budget` | 2.6.7's default |
| Cap (`cl100k_base`, whole returned JSON) | 3,000 | 5,000 | none |

- **Counting**: `json.dumps(result, ensure_ascii=False)`, as the MCP layer
  sends it, counted with `tiktoken`'s `cl100k_base` and
  `disallowed_special=()`. The vocabulary file ships with the package, so the
  count needs no network access.
- **Meeting the cap**: the response is the longest prefix of the sequence whose
  serialized form is within the cap. The implementation first makes the token
  count non-decreasing in the length of that prefix. In 2.6.7 it is not: with
  the `whole` and evidence sequences, a larger budget can take the excerpt of a
  later item away (development question 75: from budget 5,623 to 5,700 item 2's
  quote grew by 151 characters and item 7's excerpts were replaced by
  `excerpts_omitted`). That
  breaks invariant 9 of [reliable recall](../../docs/RELIABLE_RECALL_2_6.md),
  and is fixed with a test that a mutation turns red.
- **Shape**: with a mode, the response has the shape of 2.6.7's `lite: true`,
  plus `cap` and `used_tokens`. The format of `as_of` and the `content_len`
  field stay as in 2.6.7, so the shape measured in development is the shape
  tested.
- **Neither**: the response is 2.6.7's, without the new fields. `lite: true`
  is a name for `mode: "lite"`; `lite` together with `mode` is a validation
  error.

## Development (done before this registration)

- **The 100 development questions only**
  ([`v1_5_dev_questions.json`](../omnimemeval/v1_5_dev_questions.json)), on
  copies of the [published](results-omnimemeval-lme.md) store, and the
  private real-use pack's 150 development questions. The candidates were the
  2.6.7 `whole` sequence at more items (`count` 15 and 20), longer passages
  (1,600 and 3,200 characters), the `items` sequence at 10 and 20 items, and
  quotes lengthened evenly, at caps from 1,200 to 5,000, and the leading
  points up to 24,000. The cap was met by
  a binary search over the character budget, at a prototype commit whose
  settings were read from the environment.
- **Evidence without a model**: more items helped here and hurt on the
  private pack. Holding records shown whole to the first 10 items removed most
  of that harm: at cap 3,000 it showed more evidence than 10 items on both, and
  at cap 5,000 it showed 4 fewer of the private pack's 193 evidence quotes (140
  against 144). Longer passages helped on neither.
- **Answered and judged** (as in "Test" below, development questions):

| | Cap 5,000 | Cap 12,000 |
| --- | --- | --- |
| `whole`, 10 items | 87 / 100 | 84 / 100 |
| **Pro: `whole`, 15 items, whole records among the first 10** | **92 / 100** | 88 / 100 |

  On the private pack's development questions, Pro was level at 5,000 (120 and
  120 of 150) and ahead at 12,000 (126 against 121). A cap of 12,000 was lower
  than 5,000 here and higher there, so Pro's cap is 5,000.
- **Lite's cap**: 2.6.7's `lite: true` returns at most 1,296 tokens on all 500
  LongMemEval-S questions, and at most 2,849 on the private pack's
  development questions, where the records are mostly Japanese. A cap of
  3,000 changes neither.

## What was already seen of the test questions

The test questions were used before: by the evidence-allocation tests (where
`whole` at budget 2,800 answered 323 of 400), by the count and quote-length
curves, and by the
[gold-evidence reference](results-omnimemeval-lme-oracle-ceiling.md). The
returned JSON of 2.6.7 was counted on all 500 questions, without answering.
A cap of 1,000 or 1,200 for Lite was examined with the recorded answers of
`whole` and of the count curve on the test questions; neither value was
chosen. Pro was chosen on the development questions only.

## Before the test

Added to this file by a later commit, before any test search:

1. **The implementation's commit**, and the build and analysis scripts.
2. **Neither mode is unchanged**: the implementation's tests hold the default
   response to 2.6.7's, and at that commit the default search gives the
   published arm B's context for all 400 test questions. If it does not,
   nothing is run.
3. **Development check**: Lite and Pro at that commit, on the development
   questions, against the prototype points (Lite against 2.6.7's
   `lite: true`, Pro against the prototype at cap 5,000). The number of equal
   contexts is reported. If the questions with every answer session shown
   fall by 3 or more, or the share of evidence-turn characters quoted falls by
   0.02 or more, the test is not run and the cause is found first. Equal
   contexts are not required: the excerpt fix and the two new fields can move
   the last passage under the cap.
4. **The cap holds** on every development response, counted outside the
   server.

## Test

- **The 400 test questions** of the evidence-allocation tests, and only those.
- **Three searches at the named commit**, through the harness's search wrapper
  as for the published run: no mode, `mode: "lite"` with `count` 10, and
  `mode: "pro"` with `count` 15. The adapter passes the mode; its call without
  a mode is unchanged, which check 2 confirms.
- **Lite**: a question is changed when its Lite context differs from 2.6.7's
  `lite: true` context, which is `whole`'s test context
  (`cpersona-lme1-v15whole-b2800` where that test answered it, and
  `cpersona-lme1-v15a1-b2800` otherwise). Only changed questions are answered;
  every other question sends the same prompt and keeps its recorded answer and
  grade.
- **Pro**: all 400 questions are answered.
- **Answer and judge exactly as in the earlier tests**: OmniMemEval `0b1ea8d`
  with the adapter patch, answer model `gpt-4.1-mini-2025-04-14`, judge
  `gpt-4o-mini`, one judge run, temperature 0, and at most 3 concurrent model
  calls. The results go in `cpersona-lme1-v16lite` and `cpersona-lme1-v16pro`.

## What may be said

- **Lite**: Lite answers at least 320 of the 400 questions, at most three
  fewer than the recorded 323 (0.75 points). Then Lite answers no worse than
  `lite: true` of 2.6.7.
- **Pro's direction**: the paired accuracy difference, Pro minus Lite, has a
  point estimate of 0 or more. Its 95% bootstrap interval (10,000 resamples
  over the 400 questions, seed 20261009) and the exact binomial test of the
  discordant questions are reported beside it, and decide nothing.
- **The cap**: every Lite and Pro response is within its cap, counted outside
  the server, and equal to its `used_tokens`. One response over the cap means
  the cap is not guaranteed, and the release does not ship.
- Pro ships as a separate mode only if it wins the primary test on the
  private pack, its direction here and on the private real-use pack is not
  reversed, and the cap holds. Otherwise Pro does not ship.
- **Reported beside the rules**: the changed Lite questions and how their
  answers moved; accuracy per question type; Context Tokens (the answer-stage
  prompt tokens the API reported, averaged over the calls) and the retrieved
  context in `cl100k_base`; the returned JSON's mean, 95th percentile and
  maximum; the evidence metrics of
  [`evidence_metrics.py`](../omnimemeval/evidence_metrics.py); and each mode's
  accuracy against the gold-evidence reference (91.25% within 5,000 tokens,
  93.50% from the marked turns alone). A result that meets no rule is reported
  the same way.

## What it does not say

- Nothing about the accuracy of the default (no mode), or about any cap above
  5,000.
- Nothing about other answer models.
- Nothing about the development questions, which chose the modes.

One run. A rerun is made only for an infrastructure failure, and is reported.
Nothing in the construction or the settings changes after any answer is seen.
