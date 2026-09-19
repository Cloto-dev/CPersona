# Reconstruction v1.1 reader study: re-measurement after the compact envelope

Registered before any reader call of this run. The commit that adds this file is
the evidence of the order.

## Why a second run

The [first study](prereg-reconstruct-v1_1-reader.md) found no reduction: the median
ratio of payload characters, reconstruct over recall, was 1.002 (95% interval 0.687
to 1.571) on 18 questions, with 14 against 15 correct answers and a noise floor of
24%. Its tool log located the cost: a 500-character envelope on every search
response, and whole-record expansion where a range would have done (5 ranged of 22
records expanded). Two changes answer that, and this run measures them: the
envelope is compact by default, and an item whose node quote was cut carries
`expand`, the argument that reads the rest of that node.

The changes were designed from the first study's log. That is the point of an
instrument, and it is also why the first study cannot score them: this run is new
data under the same rule.

## What is the same

The store, the 18 questions, the reader and judge, the tool layer, every fixed
parameter (count 10, top_k 10, budget 5,000), the measures, the validity checks and
the decision rule. The store is not rebuilt.

## Arms

| Arm | Code | Source |
| --- | --- | --- |
| A | recall + whole-record expansion, at the first study's commit | the first study's 18 runs, reused |
| B | reconstruct v1.1 as first measured | the first study's 18 runs, reused |
| B2 | reconstruct with the compact envelope and `expand` (the commit under review) | 18 new runs |

B2's `expand` tool description carries the product's new guidance -- the node first,
its neighbours next, the whole record last -- because that guidance is part of the
change. Nothing else in the tool layer differs from arm B.

Without a model, B2's first-search responses for the 18 question texts total 53,023
characters (arm B 58,236, arm A 50,057), and 65 of 66 items carry `expand`. So B2
still starts 6% above recall; a reduction has to come from what the reader reads
next.

**Reusing A and B is a limit, stated here rather than discovered later.** They ran
hours earlier on the same day, so B2 is not interleaved with them, and anything that
drifted in between -- the model behind the CLI, its caching -- is confounded with the
arm. The noise floor below is measured inside this run for that reason. If the
result is close, the honest reading is "not shown", not a re-run of the arm that
looks better.

## Run

18 reader calls for B2, then B2 a second time on the six noise-floor questions (the
first selected of each type, as before): 24 reader calls and 24 judge calls, order
shuffled with seed 20260919, one at a time, no wrapper retries, 300-second timeout.
A reader run without a logged `search` call is a failed run and stops the run. It
pauses before a new call once this run's reported input tokens pass 4,000,000.

## Decision rule

The first study's rule, applied to r = B2 / A per question:

- **Claim a reduction** when the median of r is at most 0.70, the 95% bootstrap
  interval of the median (10,000 resamples over questions, seed 20260919) lies
  below 1.0, the median reduction exceeds the noise floor (the larger of the two
  floors: arm A's from the first study, B2's from this run), and B2 has at most two
  fewer correct answers than A.
- **Do not release** when B2 has three or more fewer correct answers than A.
- **Otherwise** release as experimental with no claim about reading cost.

**Old against new.** B2 / B is reported the same way (median, interval, correct
answers, cost per correct answer as a ratio of sums, searches, expansions and how
many were ranged). It answers whether the two changes did what they were for. It
decides nothing about the release on its own: a B2 that beats B but not A has
repaired a regression, not delivered a reduction.
