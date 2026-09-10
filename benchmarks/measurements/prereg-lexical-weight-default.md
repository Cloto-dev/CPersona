# Pre-registration: what the lexical weight's default should be

Registered before the fine sweep finished and before any of its numbers were
read. This decides a **shipping default**, not a research question.

## What had already been seen when this was written

Full disclosure, because it is not zero:

- The **coarse** grid {0, 0.1, 0.25, 0.5, 0.75, 1.0} on 22 tasks × 3 models,
  from runs made for other questions. Mean NDCG@10 against the dense-only arm:
  w=0.1 gives +1.73 / +0.56 / +0.61 (MiniLM / jina-v5-nano / bge-m3) and the
  shipped w=1.0 gives +4.99 / −1.32 / −0.87. Paired over tasks, w=0.1 has
  t = 6.38 / 2.57 / 2.86 and w=1.0 has t = 5.40 / −1.70 / −1.27.
- The per-model argmax on that grid moves 1.0 → 0.1 → 0.25, and an oracle that
  picks the best constant per *task* beats the best constant per *model* by only
  +0.49 / +0.84 / +0.55.
- An earlier reading of the same question on an 8-task subset, which was
  **selected for a different experiment** and gave a different answer (jina's
  argmax appeared to be 0, i.e. the lexical arm off). That reading is withdrawn;
  the 22-task set is the basis here.

What has **not** been seen: any value between 0 and 0.1, or between 0.1 and
0.25, on any model.

## Question

Two, in order.

1. **Is the shipped default wrong?** The lexical arm's reciprocal-rank votes
   currently enter fusion at weight 1.0, and that value is not a measured
   choice — it is what reciprocal rank fusion does when nobody picks a weight.
2. **If so, what value ships?** One global constant, not a per-model table: on
   the coarse grid a per-model table buys +0.00 (jina) and +0.18 (bge-m3) over a
   single 0.1, and the whole of its advantage sits on a weak reference encoder
   that is not a shipping slot.

## Instrument

`benchmarks/frozen_replay.py`, whose lexical-weight sweep re-fuses frozen lists,
so a sweep costs no re-encoding and no re-retrieval. A `--w_sweep` flag was added
to set the grid; nothing else about the replay changed, and its stage identities
are unaffected. Grid: **0, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 1.0** — dense
and 1.0 anchor each end, and the resolution is where the coarse grid had none.

22 tasks, three models, at most 200 queries per subtask: the same population as
every other number on this line.

**The knob does not exist in the server.** A grep of the package for the weight
returns nothing: the design page describes `CPERSONA_RRF_LEXICAL_WEIGHT` as
though it were implemented, and it is not. So this measurement decides the value
that an unwritten knob would default to, and the implementation is downstream of
it.

## Statistics, fixed now

The unit is the task; the comparison is paired across tasks within a model.

- **Against the status quo** (the decision that actually ships):
  mean and paired *t* of `NDCG(w) − NDCG(1.0)` per model.
- **Against the dense-only arm** (does the lexical arm earn its place at all):
  mean and paired *t* of `NDCG(w) − NDCG(0)` per model.
- **Dispersion is reported with every mean**: standard deviation and the number
  of tasks with a positive difference. A mean that rests on a handful of tasks
  moving is reported as that, not as a mean.

## Decision rule

A value ships only if **all three** hold:

1. `NDCG(w) − NDCG(1.0) > 0` with a paired *t* clearing 2 on **both** shipping
   slots — jina-v5-nano and bge-m3. MiniLM is a reference endpoint, not a
   shipping slot, and does not get a veto; its numbers are reported.
2. `NDCG(w) − NDCG(0) > 0` on both shipping slots. A weight that only beats the
   status quo by silencing the arm is a case for removing the arm, not for
   re-weighting it, and would be a different decision.
3. It does not sit on a cliff: the neighbouring grid points on both sides are
   within the dispersion of the difference.

### The tie-break, registered before the grid is seen

Where several values satisfy all three and their pairwise differences are inside
the dispersion, **the largest of them ships.**

The reason is asymmetric ignorance rather than taste. Lowering the weight reduces
the influence of exactly the arm that is known to matter for languages without
whitespace, and this benchmark is English-centric — it cannot see that case at
all. The measured cost of choosing the upper end of a flat region is bounded by
the flatness; the unmeasured cost of choosing the lower end is not bounded by
anything here.

## What blocks adoption even if the rule is met

**The Japanese instrument.** The lexical arm's known value is in no-whitespace
languages: the contamination work made a magnitude-based fusion the recommended
setting there, and this arm's raw scale was measured to differ by about a factor
of two between English and Japanese. Every task in this benchmark is
English-centric. So a measurement that says "less lexical weight scores better"
is a measurement about English, and shipping a lower default on it alone would
hand a worse default to the users the arm was helping.

Therefore: this file can decide **what the value should be** and can conclude
that 1.0 is not it. It cannot authorise the default change on its own. The
missing instrument is a Japanese and mixed-language query set, and it is
unbuilt.

Reported honestly either way: if the rule is met, the outcome is "the shipped
default is wrong, the replacement is X, and the change waits on a
Japanese measurement" — not "ship X".

## Abstention buckets, reported even when empty

1. tasks whose sweep did not complete for a model;
2. tasks where every grid point ties (the sweep is uninformative there);
3. MiniLM's numbers, reported in full but excluded from the decision by rule 1.
