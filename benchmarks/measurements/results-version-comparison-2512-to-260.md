# Results — 2.6.0 against 2.5.12 on LMEB, each in its best configuration

Pre-registration: [prereg-version-comparison-2512-to-260.md](prereg-version-comparison-2512-to-260.md)
(with Amendment 1). The best arm of each version was chosen on the dev half and
committed before the test half was read
([selection-version-comparison-2512-to-260.json](selection-version-comparison-2512-to-260.json)):
2.5.12 `rrf` `recall` (`A_rrf`, dev macro 56.72) and 2.6.0 `rsf` `reconstruct`
with block reach (`C_rsf_on`, dev macro 59.04). This file records the test half,
run on 2026-10-01. Every number below is in
[results-version-comparison-2512-to-260.json](results-version-comparison-2512-to-260.json).

## The invalidation conditions, checked before the table was read

All five test arms — the two best arms, the two shipped 2.6.0 configurations
(`B_rrf_on`, `C_rrf_on`) and the 2.4.41 reference arm (`R_rrf`) — pass every
condition the pre-registration fixed:

- all 15 tasks of every arm completed, and the harness's last exit for each was 0;
- every block-reach arm recorded block rows in each corpus group whose records
  divide, and no block-off arm recorded any;
- every dump header carries `recall_limit` 10, autocut and the fused gate on,
  `scene_isolated` true for exactly the seven per-conversation tasks, and the
  test split with seed 20260930;
- every store batch of every task logged a full cache hit, and every task log
  carries at least one such line, so the check did not pass by finding nothing.

No arm is invalid. The run stands.

## The registered comparison: best against best

| task | category | 2.5.12 `A_rrf` | 2.6.0 `C_rsf_on` | Δ |
| --- | --- | ---: | ---: | ---: |
| LongMemEval | Dialogue | 80.69 | 84.75 | +4.06 |
| KnowMeBench | Episodic | 48.61 | 52.03 | +3.42 |
| LoCoMo | Dialogue | 44.29 | 47.51 | +3.22 |
| REALTALK | Dialogue | 42.12 | 45.22 | +3.10 |
| Gorilla | Procedural | 33.34 | 36.05 | +2.71 |
| DeepPlanning | Procedural | 52.21 | 54.29 | +2.08 |
| ToolBench | Procedural | 49.47 | 51.32 | +1.85 |
| MemBench | Dialogue | 60.34 | 61.90 | +1.56 |
| MemGovern | Procedural | 89.13 | 90.49 | +1.36 |
| EPBench | Episodic | 88.68 | 89.98 | +1.30 |
| ReMe | Episodic | 59.87 | 60.89 | +1.02 |
| TMD | Dialogue | 23.60 | 24.56 | +0.96 |
| ConvoMem | Dialogue | 60.22 | 60.87 | +0.65 |
| LMEB_SciFact | Semantic | 72.49 | 73.02 | +0.53 |
| Proced_mem_bench | Procedural | 55.28 | 53.88 | −1.40 |
| **macro (15 tasks)** | | **57.36** | **59.12** | **+1.76** |

**Verdict, by the rule fixed before the run: 2.6.0 is better on these 15
tasks.** The macro NDCG@10 rose from 57.36 to 59.12, and one task of fifteen
fell, against the at most seven the rule allows. Fourteen rose; none stayed
within 0.01. The sign count, for orientation only, is 14 to 1.

No task moved by more than 5 points, so the pre-registration asks for no
hypothesis. One is offered for the task that fell, labelled as a hypothesis and
read off the reported arms rather than tested: on Proced_mem_bench the three
`rrf` `recall` arms score the same 55.28 (2.4.41, 2.5.12, and 2.6.0 with block
reach), `rrf` `reconstruct` scores 54.99 and `rsf` `reconstruct` 53.88. The fall
sits in the choice of tool and fusion that made `C_rsf_on` the best arm overall,
not in anything 2.6.0's `recall` does differently on that task.

**What the verdict covers.** The 15 tasks whose haystack can be one caller's
memory, at `limit=10`, scored on the order the response gives. Not the seven
candidate-pool tasks, not answer quality, not latency or memory, and not stores
with overflow-tree nodes or vectors from a deployment's embedding server — the
pre-registration's section *What this cannot answer* applies unchanged.

## Reported, not judged

### The shipped configurations

The defaults a caller gets, against 2.5.12's default (`rrf` `recall`). The
thresholds are the verdict's, applied descriptively.

| | macro | Δ against 2.5.12 | rose | fell |
| --- | ---: | ---: | ---: | ---: |
| 2.5.12 `rrf` `recall` | 57.36 | | | |
| 2.6.0 `rrf` `recall`, block reach (`B_rrf_on`) | 58.87 | +1.51 | 13 | 0 |
| 2.6.0 `rrf` `reconstruct`, block reach (`C_rrf_on`) | 58.53 | +1.17 | 13 | 2 (Proced_mem_bench, MemBench) |

### Every arm

| arm | macro | Dialogue | Episodic | Procedural | Semantic | any relevant document carried | documents carried, mean |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2.4.41 `R_rrf` | 58.43 | 53.54 | 66.73 | 56.63 | 71.90 | 93.28% | 9.97 |
| 2.5.12 `A_rrf` | 57.36 | 51.88 | 65.72 | 55.89 | 72.49 | 93.15% | 9.97 |
| 2.6.0 `B_rrf_on` | 58.87 | 54.04 | 66.81 | 57.10 | 72.80 | 93.90% | 11.96 |
| 2.6.0 `C_rrf_on` | 58.53 | 53.03 | 67.15 | 56.96 | 73.45 | 93.48% | 11.91 |
| 2.6.0 `C_rsf_on` | 59.12 | 54.14 | 67.63 | 57.21 | 73.02 | 93.65% | 9.58 |

The category means are over the tasks of each category (six Dialogue, three
Episodic, five Procedural, one Semantic). The pre-registration named three
categories; LMEB_SciFact is the one task in a fourth, and it is shown rather
than folded into another. "Any relevant document carried" is the share of all
test queries, pooled over the 15 tasks, whose response carried a relevant
document anywhere in it — every row and every `reconstruct` claim, reserved ones
included. "Documents carried, mean" is the mean over tasks of each task's
query-weighted mean.

Two readings, both descriptive:

- **The best arm's gain is not bought with more rows.** `C_rsf_on` carries 9.58
  documents a response on average, fewer than 2.5.12's 9.97. The two `rrf` arms
  with block reach carry about two more (the block reservation), and score
  below it.
- **The share of responses that hold any relevant document barely moves**
  (93.15% to 93.90% across the arms). What changed is where in the response the
  relevant document sits, which is what NDCG@10 measures.

### Against 2.4.41 (Amendment 1)

The reference arm `R_rrf` ran on the test half only and took no part in any
choice. Described with the verdict's thresholds and wording, as a description
and not as a registered claim:

| 2.6.0 arm | macro | Δ against 2.4.41 (58.43) | rose | fell | wording |
| --- | ---: | ---: | ---: | ---: | --- |
| best, `C_rsf_on` | 59.12 | +0.69 | 12 | 3 (Proced_mem_bench, TMD, ConvoMem) | above |
| shipped `recall`, `B_rrf_on` | 58.87 | +0.44 | 11 | 3 (ReMe, REALTALK, TMD) | above |
| shipped `reconstruct`, `C_rrf_on` | 58.53 | +0.09 | 8 | 7 | above, by the letter |

The last row meets the wording rule by its letter — the macro is higher and
seven tasks fell — but a difference of 0.09 with eight tasks up and seven down
is the same level, and it is stated that way here. 2.5.12 itself scores 1.07
below 2.4.41 on this half (57.36 against 58.43), which places 2.6.0's gain over
2.5.12 as recovering that fall and going past it, by 0.69 for the best arm.

## Disclosure: what was seen while the test half ran

The runner writes a line per finished task, with its score, to a status file.
While the last tasks were running, the tail of that file showed some finished
tasks' scores before every arm had completed.
No choice remained open at that point: the arms, the best of each version and
the verdict rule were fixed in the commits named above, and the reference arm's
amendment was committed before any of its results were read. The smoke runs the
pre-registration discloses are unchanged.
