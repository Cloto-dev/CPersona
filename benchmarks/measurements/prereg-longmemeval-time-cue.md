# Pre-registration: does a time cue move LongMemEval's evidence up in what recall returns?

Registered before the measured run. The instrument, arms, target questions,
metric, preconditions and decision rule below are fixed by this document.
What had been run and seen before it was written is listed under "Seen before
registration". A later change is written under "Amendments", with what had
been seen when it was written; the text above it is not edited.

## Question

A caller that half-remembers *when* something happened can pass `recall` a
`time_cue` (`docs/RECALL_PROCESS_DESIGN.md` §2). The server searches that
period with one more arm, moves a returned row that arm found up by at most
`L` places (3 / 2 / 1 for sure / likely / vague), and holds one extra place
for the best row only that arm found. The rows that pass the gate are the same
with and without the cue.

This measurement asks: **on LongMemEval questions whose text places what they
ask about in time, does passing the cue the question states move the
question's evidence sessions up in the rows `recall` returns, against the same
call without it?**

## What is claimed, and what is not

- The claim is at the **retrieval stage**: the evidence's places among the
  returned rows. No reader and no answer accuracy are part of it.
- The cue is the one the question **states**, extracted by a model from the
  question text and the question date only. It is never made from the answer
  or the evidence. A caller who passes a wrong cue is a different question;
  the `shifted` and `half` arms report it and are not part of the rule.
- The policy measured is `cued-v0.2`, the build at `20f75d9`
  (`cpersona/cue.py`). The measurement changes no default: the cue stays
  opt-in per call whatever the verdict.

## Instrument

`benchmarks/longmemeval_time_cue.py` at the commit that adds this file, run
against the package at `20f75d9` (the branch adds benchmark files only).

- **Data.** The LMEB LongMemEval task (500 scenes, one per question; the M
  haystack, a median of about 476 sessions per scene). Question text is
  LongMemEval's (MIT, github.com/xiaowu0162/LongMemEval).
- **Haystack.** The `limit10` regime of `run_longmemeval_by_type.sh`: each
  scene stored in its own channel, recall inside the question's channel,
  `limit=10`, `rrf`, autocut and the fused gate at the build's defaults, the
  threshold calibrated once after storing (`do_calibrate_threshold`). Only the
  scenes of questions that carry a cue are stored.
- **Time.** Each session is stored at the time in its title, read as UTC. The
  stored text is unchanged from the Track B runner (title and user turns), so
  every vector comes from the existing embedding cache. The clock the recall
  path reads is stopped at the question's date for every call of that
  question.
- **Independence of calls.** The recall counters are reset before every call.
  All arms run in one process against one store and one calibration.
- **Cues.** `longmemeval_time_cues.jsonl` (500 rows, 73 with a cue): model
  `claude-sonnet-5`, effort `medium`, system prompt and output schema in
  `longmemeval_time_cue_extractor.json`. Every row carries `extractor_sha` =
  the first 16 hex digits of
  `sha256(json.dumps([model, effort, system, schema], sort_keys=True))`,
  `551ef1e5812749c1`; `tests/test_longmemeval_time_cue.py` recomputes it from
  the published spec. Questions are matched to LMEB query ids by their text,
  and a question whose text matches no query, or more than one, stops the run.

## Arms

| arm | cue sent | role |
| --- | --- | --- |
| `none` | no cue | the reference: the recall a caller gets without a cue |
| `none_again` | no cue | determinism control |
| `extracted` | the cue in the file | **the treatment** |
| `shifted` | the period moved, length kept, to a place it does not overlap after both are widened by the confidence margin (first into the past; if that starts before the scene's oldest session, into the future ending by the question date; with no such place, no cue) | report only: a wrong cue |
| `half` | the period moved half its own length into the past | report only: a partly right cue |

`shifted` and `half` are built from the extracted cue, the scene's time span
and the question date only.

## Target questions

The questions with a cue, less those whose `extracted` response says the cue
was `ignored` (a cue that points only at the last 24 hours is not used, by
design), less any without evidence sessions in LMEB's relevance file.

## Metric

For each question and arm, over **every row returned, best first, with no
cutoff** (the held place can be an eleventh row, and it counts at its place):

    NDCG = sum over evidence rows at 0-based place i of 1 / log2(i + 2)
           divided by sum over i < |evidence| of 1 / log2(i + 2)

The per-question difference is **D = NDCG(extracted) − NDCG(none)**.

## Preconditions — a run that fails any of them is void, not null

1. At least **60** target questions.
2. `none` and `none_again` return identical lists on every question.
3. On every target question, every row `none` returns is also returned by
   `extracted`, and `extracted` returns at most one row more (the cue's
   contract: it reorders and adds at most its one held place).
4. `extracted` differs from `none` on at least one target question.
5. Every `extracted` response reports policy `cued-v0.2`.

## Decision rule (one run, judged once)

The time cue **moves the evidence up** if the run is valid and both hold:

1. **Sign-flip permutation test** on the sum of D over the target questions,
   one-sided, **p < 0.05**. Questions with D = 0 do not enter. Exact over all
   sign vectors when at most 20 are nonzero; otherwise 100,000 random sign
   vectors from seed 20260927, p = (1 + count(S* ≥ S)) / 100,001. The null is
   that, per question, the two arms' results are exchangeable.
2. **No question type falls**: in every type, the number of target questions
   with D < 0 minus the number with D > 0 is at most **2**.

Otherwise the verdict is **null**: the cue remains an opt-in capability with
no precision claim, its design is revisited, and a revised policy is measured
under a new name on questions other than these.

`python benchmarks/longmemeval_time_cue.py judge DIR` computes the verdict
exactly as written here; the tests pin each clause.

## Planning numbers (seen before registration; nothing from the evidence or an outcome)

From the cue file, the stored session times and the question dates only:

- A LongMemEval scene is short in time: over all 500 scenes, the sessions of
  one scene span a median of **10 days**.
- For the 73 questions with a cue, the fraction of the scene's sessions inside
  the period the server searches (the cue widened by its confidence margin):
  **28** cover the whole scene, **30** cover part of it (10 at most a quarter,
  14 at most three quarters, 6 more), **11** hold no session, and **4** are
  ignored as pointing at the last day. Expected target: 69.
- A period that covers the whole scene ranks what the ordinary arms already
  rank; one that holds no session finds nothing. Such questions are expected
  to give D = 0, which the test ignores. The effect, if any, is carried by
  about 30 questions, so the rule can pass only if most of those move up.

## Seen before registration

- A 3-question run on the first three target questions: only `run.json`
  (counts, calibration, timing) was read; the rows were deleted unread.
- Two 6-question runs on questions **without** a cue (not targets), one per
  question type, to check the wiring. With an artificial 60-day `likely` cue,
  no row moved (the widened period covered each scene). With a `sure` cue on
  the week of each question's first evidence session, rows moved on 3 of 6
  questions and one evidence session moved from 2nd to 1st. These runs are
  not part of any result.

## Reported, not part of the rule

For every arm over the target questions: mean NDCG, mean reciprocal rank of the
best evidence row, questions with all / any evidence returned, evidence rows in
the first five. Paired up/down counts against `none` for `extracted`,
`shifted` and `half`. Questions where the held place carried evidence, and
where the searched period contained an evidence session. The confidence mix,
the ignored count and the median latency per arm. D broken down by the
planning strata above (whole scene / part / none) — exploratory.

## Outputs

`results-longmemeval-time-cue.md` beside this file, and the run's `rows.jsonl`
and `run.json` under `longmemeval_time_cue/`.

## Amendments

(none)
