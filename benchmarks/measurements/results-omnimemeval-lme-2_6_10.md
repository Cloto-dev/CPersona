# The 2.6.10 line of `reconstruct`'s modes on LongMemEval-S: results

Registration: [prereg-omnimemeval-lme-2_6_10.md](prereg-omnimemeval-lme-2_6_10.md),
committed and pushed before any test question was searched at the candidate
commit and before any answer or judge call. Read with
[`v2_6_10_analyze.py`](../omnimemeval/v2_6_10_analyze.py).

## Result

- **Pro: rule failed.** 2.6.10a4 answered 343 of the 400 test questions against 2.6.9's 354. Among the 384 questions whose context changed, 20 went from right to wrong and 9 from wrong to right: one-sided p = 0.031, below 0.05.
- **Lite: not shown worse.** Lite answered 317 against 324. Among its 400 changed questions, 16 went from right to wrong and 9 from wrong to right (p = 0.115). The direction is negative.
- **The cap: held.** Every Lite and Pro response of the 400 test questions was within its cap, counted outside the server in `cl100k_base`, and equal to its `used_tokens`: 0 over the cap and 0 unequal in each mode.

The failure means 2.6.10a4 does not ship as the 2.6.10 release. The places it
added were removed ([#477](https://github.com/Cloto-dev/CPersona/pull/477)).

| Mode | Accuracy | Answer sessions shown % | All shown % | Evidence turns touched % | Evidence-turn characters quoted % | Evidence share % |
| --- | --- | --- | --- | --- | --- | --- |
| 2.6.10a4 Lite | 79.25 | 95.8 | 92.2 | 92.7 | 54.6 | 57.4 |
| 2.6.9 Lite | 81.00 | 95.7 | 92.2 | 91.8 | 51.7 | 63.6 |
| 2.6.10a4 Pro | 85.75 | 98.8 | 97.8 | 97.1 | 68.2 | 26.6 |
| 2.6.9 Pro | 88.50 | 98.2 | 97.2 | 97.0 | 71.3 | 28.2 |

Accuracy by question type, %:

| Mode | Knowledge update | Multi-session | Single-session assistant | Single-session preference | Single-session user | Temporal reasoning |
| --- | --- | --- | --- | --- | --- | --- |
| 2.6.10a4 Lite | 81.2 | 68.8 | 82.6 | 90.9 | 90.6 | 79.2 |
| 2.6.9 Lite | 85.9 | 69.7 | 82.6 | 90.9 | 94.3 | 80.2 |
| 2.6.10a4 Pro | 85.9 | 76.1 | 93.5 | 90.9 | 96.2 | 85.8 |
| 2.6.9 Pro | 87.5 | 82.6 | 95.7 | 100.0 | 96.2 | 85.8 |

Size of what each mode returned:

| Mode | Returned JSON, mean (cl100k) | 95th percentile | Maximum | Cap |
| --- | --- | --- | --- | --- |
| Lite | 1,208.6 | 1,445 | 1,568 | 3,000 |
| Pro | 4,962.6 | 4,999 | 5,000 | 5,000 |

## What was run

- **Changed questions.** Every Lite question's context changed against 2.6.9, as the registration expected from Lite's larger budget. So did 384 of Pro's.
- **Unchanged questions.** Every other question sends the same prompt as in 2.6.9 and keeps its recorded answer and grade.
- **Model calls.** One answer pass and one judge run for each mode, with no rerun and no failed search, answer or judge call.
  - The 400 Lite calls reported 476,644 prompt tokens and 56,782 completion tokens.
  - The 384 Pro calls reported 1,602,560 and 52,665.
- **Reading 2.6.9's answers.** They are read as its own check recorded them; read that way, they total the published 324 and 354.

## Where the loss came from (exploration, after the check)

The questions were searched again at each pre-release, and Pro's changed questions answered as above. None of this was registered.

| Pro at | Correct | Right to wrong / wrong to right |
| --- | --- | --- |
| 2.6.9 | 354 | — |
| 2.6.10a2: two block places | 354 | 8 / 8 |
| 2.6.10a3: + extra keyword places | 348 | 17 / 11 |
| 2.6.10a4: + depth places | 343 | 20 / 9 |
| 2.6.10a4, with every held item's passages after the window's | 350 | 16 / 12 |

**Pro's responses fill their cap.** An item held beside the window therefore takes tokens from the window's own quotations:

- 2.6.10a4's Pro responses held 6.9 items on average, against 2.6.9's 2.7, in the same 4,960 tokens.
- The answer sessions shown rose, but the share of evidence-turn characters quoted fell.
- The multi-session questions, which need more than one session's text, fell the most.

**Putting the held items' passages after the window's recovered part of the loss here.** On a private pack of short agent memories, though, it lowered Pro's measure well below 2.6.9. There the block reservation and the keyword seat, which 2.6.9 already had, carry evidence the window does not.

**The two benchmarks disagree** on how a cap should be split between more records and more of each record. That split is left to be designed with larger caps.

## The private pack

The claim the places were built for was judged on a sealed pack of real agent memories, under a test registered separately before it ran, on 370 questions none of the arms had searched. The measure is the share of questions whose every evidence record the response held within its cap, weighted by the pack's population.

- **Pro under rrf: met.** 2.6.10a4 54.85% against 2.6.9's 50.58%: +4.26 points, z = 3.75 against the one-sided threshold of 1.645.
- **Lite under rsf with confidence: met.** 43.92% against 38.02%: +5.91 points, z = 4.02.
- **The other two pairs: direction not reversed.** rrf Lite was +6.75 points and rsf with confidence Pro +3.78.
- **The cap** held on every response.

That measure counts whether each evidence record appears, not how much of it,
and it rose with every held place; this benchmark's answers fell with them.
Both results stand as registered.

## What it does not say

- That the places lower accuracy everywhere. On the private pack they held more of each question's evidence; no reader was asked there.
- That Lite got worse. Its loss is within chance.
- Nothing about a call that names no mode, which is unchanged, or about other caps, counts or answer models.
