# Results — what the lexical weight's default should be

Pre-registration: `prereg-lexical-weight-default.md`, fixed before the fine
sweep finished and before any of its numbers were read. Summariser:
`lexical_weight_summary.py`, which computes the registered statistics and
applies the registered rule in code rather than by eye.

Grid 0, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 1.0 · 22 tasks · 3 models ·
≤ 200 queries per subtask · `frozen_replay.py --w_sweep` on cpersona 2.5.12b3,
`rrf_k = 60`, threshold factor 0.5. Wall clock 3 h 48 m + 3 h 34 m + 3 h 26 m.

**Two findings, and they are not the same finding.**

1. **The shipped default is wrong.** Every weight from 0.05 to 0.4 beats it on
   both shipping slots, paired over tasks, with t from 2.17 to 4.67. 1.0 is not
   a measured choice; it is what reciprocal rank fusion does when nobody picks
   a weight.
2. **The replacement the rule selects is 0.3** — and it cannot ship on this
   evidence, for a reason registered in advance: every task here is
   English-centric, and this arm's known value is in languages without
   whitespace.

## Against the status quo, and against a silent arm

Unit = task; every comparison paired inside a model. `+/-` counts the tasks
that moved each way, because a mean over 22 tasks that rests on three of them
is not a mean.

### jina-v5-nano (shipping slot)

| w | vs 1.0: mean | sd | t | +/− | vs 0: mean | sd | t | +/− |
|---:|---:|---:|---:|:--:|---:|---:|---:|:--:|
| 0.0 | +1.318 | 3.637 | 1.70 | 12/10 | — | — | — | — |
| 0.05 | +1.672 | 3.245 | 2.42 | 13/9 | +0.354 | 0.602 | 2.76 | 17/5 |
| 0.1 | +1.881 | 2.812 | 3.14 | 16/6 | +0.563 | 1.026 | 2.58 | 17/5 |
| 0.15 | +1.907 | 2.404 | 3.72 | 16/6 | +0.589 | 1.521 | 1.82 | 14/8 |
| 0.2 | +1.866 | 2.205 | 3.97 | 18/4 | +0.548 | 1.818 | 1.41 | 13/9 |
| 0.25 | +1.846 | 2.034 | 4.26 | 18/4 | +0.529 | 1.976 | 1.25 | 13/9 |
| 0.3 | +1.795 | 1.977 | 4.26 | 17/5 | +0.477 | 2.036 | 1.10 | 13/9 |
| 0.4 | +1.581 | 1.587 | 4.67 | 17/5 | +0.264 | 2.433 | 0.51 | 11/11 |

### bge-m3 (shipping slot)

| w | vs 1.0: mean | sd | t | +/− | vs 0: mean | sd | t | +/− |
|---:|---:|---:|---:|:--:|---:|---:|---:|:--:|
| 0.0 | +0.875 | 3.212 | 1.28 | 11/11 | — | — | — | — |
| 0.05 | +1.328 | 2.864 | 2.17 | 12/10 | +0.453 | 0.574 | 3.70 | 18/4 |
| 0.1 | +1.488 | 2.577 | 2.71 | 12/10 | +0.612 | 1.005 | 2.86 | 14/8 |
| 0.15 | +1.598 | 2.340 | 3.20 | 17/5 | +0.722 | 1.284 | 2.64 | 14/8 |
| 0.2 | +1.654 | 2.161 | 3.59 | 16/6 | +0.779 | 1.385 | 2.64 | 14/8 |
| 0.25 | +1.666 | 1.928 | 4.05 | 19/3 | +0.790 | 1.563 | 2.37 | 15/7 |
| 0.3 | +1.564 | 1.781 | 4.12 | 18/4 | +0.689 | 1.779 | 1.82 | 13/9 |
| 0.4 | +1.431 | 1.585 | 4.23 | 19/3 | +0.555 | 2.018 | 1.29 | 11/11 |

### MiniLM-L6-v2 — reference endpoint, excluded from the decision

| w | vs 1.0: mean | sd | t | +/− | vs 0: mean | sd | t | +/− |
|---:|---:|---:|---:|:--:|---:|---:|---:|:--:|
| 0.0 | −4.994 | 4.325 | −5.42 | 4/18 | — | — | — | — |
| 0.05 | −4.032 | 3.838 | −4.93 | 4/18 | +0.963 | 0.759 | 5.95 | 22/0 |
| 0.1 | −3.261 | 3.389 | −4.51 | 5/17 | +1.733 | 1.267 | 6.42 | 21/1 |
| 0.15 | −2.743 | 3.090 | −4.16 | 5/17 | +2.251 | 1.662 | 6.35 | 22/0 |
| 0.2 | −2.229 | 2.725 | −3.84 | 5/17 | +2.765 | 2.131 | 6.09 | 22/0 |
| 0.25 | −1.892 | 2.440 | −3.64 | 5/17 | +3.102 | 2.448 | 5.94 | 21/1 |
| 0.3 | −1.597 | 2.291 | −3.27 | 5/17 | +3.397 | 2.558 | 6.23 | 19/3 |
| 0.4 | −1.178 | 1.896 | −2.91 | 6/16 | +3.817 | 2.924 | 6.12 | 18/4 |

The weak encoder wants the arm at full strength and every step down costs it,
monotonically. The two shipping encoders want it at a fraction and every step
up towards 1.0 costs them. That is the model-dependence this line has been
measuring, now visible in the fusion constant itself: **no single weight is
best for all three**, and the fine grid does not change that — it only says
where the two that ship agree.

## The rule, applied

- **Rule 1** (beats 1.0 on both slots, paired t > 2): 0.05 … 0.4 pass; 0.0
  fails (t = 1.70 / 1.28). *Turning the arm off is not supported by this data.*
- **Rule 2** (beats a silent arm on both slots): 0.05 … 0.4 pass. Note the
  margin thins as w rises — at 0.4 the mean is +0.264 (jina) and +0.555
  (bge-m3), with t below 2 on both.
- **Rule 3** (not on a cliff — both grid neighbours inside the dispersion of
  the paired difference, on both slots):
  - **0.05 fails.** Its lower neighbour is measurably worse (t = −2.76 /
    −3.70): a step of 0.05 down falls off the plateau.
  - **0.4 fails**, but for a different reason worth writing down: its upper
    neighbour on this grid is 1.0, **0.6 away**, and of course distinguishable.
    Nothing was measured between 0.4 and 1.0, so 0.4 is not shown to sit on a
    cliff — it is shown to be un-checked above. The rule is literal and 0.4 is
    out; a reader should not mistake that for evidence against 0.4.
  - **0.1, 0.15, 0.2, 0.25, 0.3 pass.**
- **Flatness across the survivors**: all 20 pairwise comparisons (5 values × 2
  slots) are inside the dispersion — the largest is |t| = 1.46. One flat
  region.
- **Tie-break, registered before the grid was seen** — where several values
  satisfy all three and cannot be told apart, the largest ships:
  **w = 0.3**.

The tie-break is doing real work here, and it was registered precisely so that
it would: on the means alone a reader would pick 0.15 for jina (+1.907) and
0.25 for bge-m3 (+1.666), and neither is distinguishable from 0.3. The reason
to take the top of the plateau is asymmetric ignorance rather than taste —
lowering the weight weakens exactly the arm known to matter for languages this
benchmark cannot see, the cost of being too high is bounded by the flatness,
and the cost of being too low is not bounded by anything measured here.

## What this does not authorise

**Not a default change.** Registered in advance and unchanged by the result:
these 22 tasks are English-centric, the lexical arm's raw scale was measured to
differ by about a factor of two between English and Japanese, and the
contamination work made a magnitude-based fusion the recommended setting for
no-whitespace text. A measurement saying "less lexical weight scores better" is
a measurement about English. Shipping a lower default on it alone would hand a
worse default to the users the arm was helping.

So the outcome is: **1.0 is not a measured choice and the evidence against it
is strong; the value that the registered rule selects is 0.3; the change waits
on a Japanese and mixed-language query set, which is unbuilt.**

**The knob does not exist yet.** A grep of the package returns nothing for the
lexical weight: the design page describes an environment variable as though it
were implemented, and it is not. This file decides what that unwritten knob
should default to; implementing it is downstream, and implementing it with the
default left at 1.0 is a coherent first step that changes no behaviour.

## Abstention buckets, reported as registered

1. **Sweeps that did not complete**: none. 22/22 tasks in all three models.
2. **Tasks where every grid point ties**: none, in any model.
3. **MiniLM**: reported in full above, excluded from the decision by rule 1.

## Provenance and one asymmetry

All three runs: cpersona 2.5.12b3, `rrf_k = 60`, threshold factor 0.5, ≤ 200
queries per subtask, the same 22 tasks.

- **The stage-identity check was off** (`--identity_every 0`, 0 rows checked in
  all three runs). The sweep compares arms against each other inside one frozen
  replay, which is what the identity check is not needed for; identity against
  the live pipeline was verified in the earlier run this grid extends. Do not
  read these absolute NDCG values as pipeline-identical — read the differences.
- **jina's calibration was pinned** from a prior calibration run; bge-m3 and
  MiniLM calibrated live. This does not touch any comparison here, because
  every w within a model re-fuses the *same* frozen lists under the *same*
  calibration — but it does mean the three models' absolute numbers are not on
  one footing.
