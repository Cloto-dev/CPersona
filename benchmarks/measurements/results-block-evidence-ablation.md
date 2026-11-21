# Results: what blocks and evidence allocation contribute to `reconstruct`

Run: 2026-11-20. The conditions, the metric and the decision rule were written
down before the questions were generated, after a development run on a
separate question set; nothing was changed after the development run. No model
was called in the measurement: the metric is counted from what was returned.

**Verdict.** Both components contribute. Turning the block arm off lowers the
share of questions whose evidence was all quoted from 51.81% to 35.80%
(z = 6.85); replacing Pro's evidence allocation with the default sequence lowers
it to 42.54% (z = 4.18). Both clear the registered thresholds under Holm's
procedure at α = 0.05. Turning off only the block arm's reach, while quoting
from the blocks it built, was exploratory and did not show a contribution
(50.29%, z = 1.36).

## The question set

275 questions written against 4,478 records of one real deployment's memory.
The records are private, so the set is not published. The questions were
generated and checked by language models: 275 of 890 candidates passed the
check. They are drawn by strata over seven question types, among them
questions that combine two or three records and questions the memory cannot
answer. No question was used before this run.

## The conditions

All four run `reconstruct` in `mode: "pro"` (cap 5,000 tokens, 15 items) on
2.6.16's code, with reciprocal-rank fusion.

| Condition | What changes | Role |
| --- | --- | --- |
| Full | nothing (2.6.16 as released) | baseline |
| No blocks | `CPERSONA_BLOCK_RETRIEVAL_ENABLED=false`: the block index is built but not read, so blocks neither reach records nor supply quotes | tested |
| No evidence allocation | Pro follows `CPERSONA_RECONSTRUCT_SEQUENCE` (default `items`) instead of the `whole` sequence of the [evidence allocation](../../docs/EVIDENCE_ALLOCATION_DESIGN.md) | tested |
| No block reach | the block arm is off, but quotes still read the blocks that were built | exploratory |

The last two need a code change and were run from measurement-only commits
that were never merged. Before the decision, a check run on every question
confirmed that each commit changes only what it should: the evidence
allocation commit returned the same candidates and clusters as Full, and the
reach commit the same as No blocks, on all 275 questions; No blocks differed
from Full on all 275, so the check could see a difference. Every run restored
the calibration file and had no embedding failure.

## Metric and rule

- **y**: the share of questions for which every record holding the evidence was
  quoted in what `reconstruct` returned. Averaged over the seven types with
  equal weight; within a type, strata carry their natural weight.
- **d** for a component = y(Full) − y(without it), with a Wald z.
- Holm's procedure over the two tested components at α = 0.05: the larger z
  must exceed 1.960, and then the other must exceed 1.645.

## Results

Improvement = (Full − without) ÷ without. The same-cost control: all four
conditions run under the same cap and item count and returned 4,873 to 4,947
tokens on average.

| Component removed | y without | y Full | d | Improvement | z | Evidence records gained / lost | Status |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Blocks | 35.80% | 51.81% | +16.01 pt | +44.7% | 6.85 | 72 / 1 | confirmed |
| Evidence allocation | 42.54% | 51.81% | +9.26 pt | +21.8% | 4.18 | 44 / 4 | confirmed |
| Block reach only | 50.29% | 51.81% | +1.52 pt | +3.0% | 1.36 | 8 / 3 | exploratory, not shown (95% interval −0.67 to +3.71 pt) |

Latency per question (median / p95, the development machine): Full 127 / 143 ms,
no blocks 69 / 85 ms, no evidence allocation 113 / 124 ms, no block reach
76 / 92 ms. The block arm's reach accounts for about 50 ms of Full's latency.

### Reported, not tested

- With relative-score fusion instead, all three differences point the same way:
  +7.34 pt (z 4.03), +6.58 pt (z 3.72) and +2.14 pt (z 1.98).
- On the development set (989 questions), the differences were +18.53 and
  +13.70 points; on this set they are 0.86 and 0.68 times as large.
- By type (Full): 96.2%, 97.6%, 68.1% and 88.8% on four types, and 12.0% and
  0.0% on the two that combine two or three records. The type the memory cannot
  answer has no evidence records, so its y is 0 in every condition by the
  metric's definition.

## Deviations

- The plan expected about 366 questions; the model check passed 275. Five
  strata had no question left; they hold 7.3e-8 of the frame's weight and were
  left out of the estimate, with each type's weights renormalised over its
  remaining strata. The rule did not change.

## What is not claimed

- Nothing about what a reader answers: no reader was run.
- Nothing about the other two parts of reconstruction — bundling rows into
  items, and quoting from overflow nodes. There is no switch that turns either
  off, so their contribution is not tested.
