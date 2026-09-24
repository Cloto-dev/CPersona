# One Prior Function — design

**Status:** design, not shipped behaviour. Nothing described here is in a
release yet. It is the implementation form of
[the recall line's section 3](RELIABLE_RECALL_2_6.md#3-one-prior-function) and
of [the far-vote plan](REACH_AND_RECENCY_PLAN.md#6-the-plan-for-the-26-line-a-priced-far-vote-designed-with-recency),
and it settles the decision both of them say must come first: what happens to
the final re-sort.

## 0. What this changes

Every weight that makes a row rank higher or lower because of **where it sits**
— by scan position or by age — moves into one function, `p(row)`. That function
decides **order only**: it never decides which rows survive the quality gate.
The confidence score stops re-sorting the fused list and stops gating it; it is
still computed and returned beside each row as a separate value.

At the default settings, a deployment that leaves confidence off sees no change
at all. A deployment that has confidence on sees results in fusion order
instead of confidence order, and can restore the old behaviour with one
setting.

## 1. Where the weights are today

Four mechanisms weight a row by its position or age. Each has its own setting,
and none of them knows about the others.

| Mechanism | What it does | The weight it amounts to |
| --- | --- | --- |
| Vector scan window (`CPERSONA_MAX_MEMORIES`) | The vector retriever ranks only the newest rows by scan position | 1 inside the window, 0 beyond — an implicit recency preference |
| Far list (`CPERSONA_VECTOR_REACH`, `CPERSONA_VECTOR_FAR_LIMIT`) | Rows beyond the window join the fusion as a second vector list | 1 beyond the window: no price on a far vote |
| [Episode boundary penalty](behavior-contracts.md#3-episode-boundary-penalty) | Memories older than the newest episode are multiplied by a factor that reaches 0.5 | A step at the boundary, applied after fusion, and able to push rows under the quality gate. Off by default from 2.6.0a7 |
| Confidence (`CPERSONA_CONFIDENCE_ENABLED`) | Blends similarity, a time decay, resolved status and recall history into one score, re-sorts the whole list by it, and gates on it | A hyperbolic time decay with a floor of 0.3, hidden inside a score that also carries similarity |

Two measurements on a real long-term memory store, made with the in-house
benchmark under development, show why the scattered form is a problem rather
than an untidiness:

- The episode boundary penalty had no effect on order before 2.5.0 (bug-115).
  After the fix, it started re-ordering results under the default
  configuration, and nothing measured it. On a store where an agent archives an
  episode every session, it halved how often the record holding the answer
  ranked first. Public benchmarks never create episodes, so none of them could
  see it. A time weight that is not part of a measured mechanism can change
  ranking without anyone choosing that it should.
- With confidence enabled, the `rrf` and `rsf` fusion modes returned identical
  responses on every question of a 150-question set. The final re-sort
  discards the fusion order completely. Any prior applied inside the fusion
  would therefore do nothing in a deployment that has confidence on. On the same
  set, with the episode penalty off, an exact McNemar test found no difference
  in answer accuracy between confidence on and off in either fusion mode, so
  removing the re-sort is not expected to cost accuracy.

## 2. The prior

Under `rrf`, the score that orders the result list becomes

```
order_score(row) = p(row) × Σ over lists  w_list × 1 / (k + rank_on_list + 1)

p(row)  = p_age(row)                 (a p_cue(row) factor is reserved for Cued Recall)
w_list  = 1 for the near vector list, the full-text list and the keyword list
        = w_far for the far list
```

- **The far weight `w_far`** is the priced far vote of the
  [far-vote plan](REACH_AND_RECENCY_PLAN.md#62-the-shape-one-prior-its-special-cases).
  `w_far = 1` is today's reach, and `w_far = 0` is the reach turned off. Both
  ends are therefore identity controls: each must reproduce, to the digit, a
  measurement that already exists.
- **The age weight** is `p_age = max(floor, 1 / (1 + age_hours × rate))`. This
  is deliberately the same family as the time decay inside the confidence
  score. With the confidence score's own rate and floor, one arm of the
  measurement isolates exactly the time term confidence applies today, so its
  effect can be read on its own for the first time.
- Under `rsf`, `p(row)` multiplies the fused, normalised score, and the far
  channel is weighted by `w_far`. The channel divisor is unchanged, as the
  plan's note on `rsf` describes.

## 3. The prior orders; it never admits

The score multiplied by `p(row)` is used for the **final order only**. The
quality gate and autocut keep reading the unweighted fused score.

Half of the episode penalty's damage came through the gate. Lowering a row's
score pushed it under the calibrated threshold, and the row disappeared (the
penalty's own configuration comment describes this as its purpose). A weight
that can remove rows changes the candidate set as soon as it is applied. After
that, nothing can separate what the reordering did from what the removal did.
Keeping the prior out of the gate means **the prior decides which row comes
first, never which rows remain**.

Consequence: however extreme `p` is, the set of rows that pass the gate is
unchanged; only the order in which the count cuts them moves. A test pins this.

## 4. How age is measured

- **Age is measured from the newest record in the recall's scope**, not from
  the current time. Measured from now, every record would grow older together
  while the user is away, and how much the weight separates them would depend
  on how long the store sat idle. Measured from the newest record, a dormant
  store ranks exactly as it did when it was last used. The confidence score
  already anchors unknown ages on the newest row (bug-207) for a related
  reason. `CPERSONA_PRIOR_AGE_ANCHOR=now` is available for deployments that
  want the other reading.
- A row with no usable timestamp is placed at the middle of the scope's age
  range, as the confidence score does since bug-207. A row whose age is unknown
  must not win by default.
- Episodes are aged by their own timestamps under the same rule. The episode
  penalty's exemption for episodes does not carry over: a special case is what
  a single function exists to remove.
- Profile rows carry no score and are not weighted. Rows admitted by the block
  reservation are not weighted either: the reservation is a separate seat for
  evidence the window cannot reach, outside the competition for order.

## 5. Confidence

- The re-sort by confidence at the end of recall scoring is removed.
- Confidence leaves the quality gate's signal precedence. Removing only the
  re-sort would leave confidence deciding which rows survive, which is half of
  what the re-sort did.
- When `CPERSONA_CONFIDENCE_ENABLED` is on, the confidence value is still
  computed and returned with each row (`confidence: {age_hours, cosine,
  score}`), as a separate value.
- The time decay inside confidence stops affecting order. A deployment that
  wants time to affect order sets the age weight. **Time is weighted in one
  place.**
- `CPERSONA_CONFIDENCE_ORDERING=legacy` restores the re-sort and the confidence
  gate. A deployment that finds a problem can go back with a setting, without
  rolling back code.
- Deployments with confidence on must re-run `calibrate_threshold` after the
  upgrade: the gate's signal changes from confidence to the fused score.

## 6. The episode boundary penalty

It stays available as an opt-in in this line. Once the age weight is measured,
the penalty is a special case of it (a step at the newest episode rather than a
curve), and its retirement is proposed for a later line. It is not removed
here, so a deployment that depends on it keeps a path.

## 7. Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `CPERSONA_PRIOR_FAR_WEIGHT` | `1.0` | `w_far`. Only meaningful when the reach is set above the window |
| `CPERSONA_PRIOR_AGE_RATE` | `0` | Rate of the age weight. `0` means `p_age = 1` (off) |
| `CPERSONA_PRIOR_AGE_FLOOR` | `0.3` | Floor of the age weight |
| `CPERSONA_PRIOR_AGE_ANCHOR` | `newest` | Age measured from the scope's newest record (`newest`) or from the current time (`now`) |
| `CPERSONA_CONFIDENCE_ORDERING` | `fusion` | Confidence does not order or gate. `legacy` restores both |

At these defaults, a deployment with confidence off behaves bit-identically to
the release before this change. The behaviour golden and the existing suite pin
that. Only `CPERSONA_CONFIDENCE_ORDERING` changes behaviour at its default, and
only where confidence is enabled.

**The defaults of the prior move only after the measurements in section 8
pass**, and they move together in one change, as the
[far-vote plan's rollout](REACH_AND_RECENCY_PLAN.md#65-rollout) requires.

## 8. What is measured before any default moves

Each measurement is pre-registered before it runs.

| | Instrument | Arms | Rule, in outline |
| --- | --- | --- | --- |
| **M0** — dropping the confidence re-sort | Real-store benchmark, development questions, `recall`, episode penalty off | `legacy` against `fusion`, confidence enabled | Answer accuracy does not fall, within a margin fixed in advance |
| **M1** — the far weight | LongMemEval near and far strata, as in the [far-vote plan](REACH_AND_RECENCY_PLAN.md#64-what-is-pre-registered-before-any-arm-runs) | `w_far ∈ {0, 0.25, 0.5, 0.75, 1}` at a reach of 200,000 | Both ends reproduce arms A and S to the digit; then the near stratum within −1.0 of the shipped answer and the far stratum within a point of the unweighted far list |
| **M2** — the age weight | Real-store benchmark (every question type, with the current-value and temporal types in front) | `rate ∈ {0, small, medium, confidence's own}` | Accuracy rises overall, and no question type loses three or more answers. The types whose answers are old are the guard |

- The age weight is measured only on data with a real time structure. The
  LongMemEval harness (`benchmarks/benchmark_trackb_lmeb.py`) writes every
  record with the same fixed timestamp, so every record there has the same age
  and an age weight measured on it would measure nothing. This is the lesson of
  the episode penalty: a time weight has to be judged on a store where time
  actually varies.
- The rate is chosen on the development questions and confirmed once on the
  held-out questions. The held-out questions were already used once, to locate
  the episode penalty's effect, and the confirmation says so.

## 9. Not in this step

- Cued Recall, where the caller declares a time ("last month"). It changes the
  input contract and belongs to the recall process. `p(row)` keeps a factor
  free for it.
- The recall process and adaptive fusion.
- Removing the episode boundary penalty (section 6).

## 10. Risks

| Risk | What guards it |
| --- | --- |
| Order changes for deployments with confidence on | M0 before release; `CPERSONA_CONFIDENCE_ORDERING=legacy` restores the old behaviour by setting; recalibration after upgrade |
| An age weight buries old answers, as the episode penalty did | The prior never admits or removes rows (section 3); off by default; M2 guards the old-answer types |
| A default silently changes behaviour | Bit-identical at the defaults, pinned by the golden; identity controls at both ends of `w_far` |
| An instrument without time structure reports nothing | M2 runs on a real store; the LongMemEval limitation is stated rather than measured around |
