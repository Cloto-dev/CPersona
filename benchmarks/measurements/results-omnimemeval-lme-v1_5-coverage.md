# The coverage order (`coverage`) against the length floor: results

Registered in [`prereg-omnimemeval-lme-v1_5-coverage.md`](prereg-omnimemeval-lme-v1_5-coverage.md),
committed and pushed before the test questions were searched with `coverage`.
The private real-use pack's test was registered separately, also before its
runs, and was run first.

## Summary

- **The private real-use pack's primary rule was not met.** On its 150 test
  questions (196 evidence quotes) at budget 2,800, `coverage` showed 121
  evidence quotes and `whole` 120, at 2,657 and 2,673 characters shown. The 95%
  bootstrap interval of the per-question difference is −0.033 to +0.047: the
  rule, a lower bound above zero, does not hold.
- **Its secondary rule was not met either.** Today's sequence (`items`) at the
  budget whose characters shown were nearest, chosen by characters alone
  (3,000, 2,702 characters), showed 123; `coverage` showed 2 fewer, with an
  interval of −0.093 to +0.073 per question.
- **The LongMemEval-S answer stage was not run.** The searches and the
  reproduction check ran as registered. The private pack's result had already
  settled the decision this test fed, whether `coverage` replaces `whole` in
  `lite=true`, which needed both, so the answer and judge calls were not made.
  This is a departure from the registration, and no accuracy is claimed.
- **The coverage order is set aside and not shipped.** The development gain
  (113 against 104 at 2,800 on the private pack's development questions) did
  not carry to the test questions.

## The private real-use pack (test, 150 questions)

The pack's questions cannot be published; its result is given in counts.
Reproduction checks first, both met: today's sequence at the default budget
gave the recorded test responses of the pack's earlier study for 150 of 150
questions, and `whole` at 2,800 gave the `whole` test's responses for 150 of
150.

| Budget | Sequence | Evidence quotes shown (of 196) | Records reached | Characters shown | Items returned |
| --- | --- | --- | --- | --- | --- |
| 2,000 | items | 108 | 119 | 1,563 | 2.31 |
| 2,000 | whole | 98 | 127 | 1,811 | 5.57 |
| 2,000 | coverage | 105 | 135 | 1,790 | 5.59 |
| **2,800** | items | 121 | 135 | 2,397 | 3.53 |
| **2,800** | **whole** | **120** | 148 | 2,673 | 8.38 |
| **2,800** | **coverage** | **121** | 155 | 2,657 | 8.33 |
| 3,000 | items | 123 | 137 | 2,702 | 3.97 |
| 3,200 | items | 126 | 140 | 2,896 | 4.27 |
| 3,400 | items | 127 | 141 | 2,997 | 4.49 |
| 4,000 | items | 131 | 145 | 3,826 | 6.18 |
| 4,000 | whole | 134 | 162 | 3,891 | 10.85 |
| 4,000 | coverage | 136 | 163 | 3,888 | 10.82 |

Reported beside the rules, with no claim: against `whole`, `coverage` showed 7
more at 2,000 (interval 0.000 to +0.100 per question) and 2 more at 4,000
(−0.013 to +0.040), at the same characters within 1.2%.

## LongMemEval-S (test, 400 questions, searches only)

- **Reproduction check met**: `whole` at budget 2,800, searched at the
  registered commit, gave `whole`'s test contexts for 400 of 400 questions.
- `coverage`'s context differs from `whole`'s on 319 of the 400 questions,
  which is how many would have been answered.
- The evidence metrics of [`evidence_metrics.py`](../omnimemeval/evidence_metrics.py),
  which need no model call (context in `cl100k_base` tokens):

| Point | Context tokens | Items | Answer sessions shown % | All shown % | Evidence turns touched % | Evidence-turn characters quoted % | Evidence share % |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Four items (count curve) | 722.0 | 4.00 | 90.1 | 81.2 | 85.7 | 48.8 | 42.2 |
| Whole, budget 2,800 | 722.2 | 5.42 | 95.7 | 92.2 | 91.8 | 56.6 | 63.6 |
| Coverage, budget 2,800 | 723.2 | 5.37 | 95.5 | 91.5 | 91.3 | 56.8 | 64.7 |

On these questions `coverage` showed the answer sessions, all of a question's
answer sessions and the evidence turns slightly less often than `whole`, and a
slightly larger share of its quotes came from answer sessions. Every quoted
character was found in the store.

## What this means

On the development questions the coverage order was the best of the orders
tried, and those questions chose it and its constants; the test questions show
how much of its lead was the questions. At the same characters, today's
sequence, `whole` and `coverage` showed evidence within a few quotes of each
other on the real-use pack's test, and `coverage` and `whole` showed the
answer sessions about equally often on LongMemEval-S. Ordering the passages of
the window by the question's words does not move the evidence shown much once
short records are protected.

`whole` stays the sequence of `lite=true`, and the default stays `items`.

## Development (before registration, for reference)

On the private pack's 150 development questions (193 evidence quotes),
`coverage` showed 92, 113, 114 and 123 at budgets 2,000, 2,800, 3,000 and
4,000, against `whole`'s 84, 104, 108 and 117; on LongMemEval-S's 100
development questions at 2,800, the table in the registration. The design
note's [section 5](../../docs/EVIDENCE_ALLOCATION_DESIGN.md#5-coverage-265a2)
records the orders set aside on those questions.

If the answer stage is wanted later, it can be run from the recorded searches
with [`v1_5_coverage_build.py`](../omnimemeval/v1_5_coverage_build.py) and
[`v1_5_coverage_analyze.py`](../omnimemeval/v1_5_coverage_analyze.py) at the
registered commit (`3ec58cf`), and would be reported as run after this result.
