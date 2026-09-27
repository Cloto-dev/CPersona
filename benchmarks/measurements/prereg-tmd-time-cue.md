# Pre-registration: does the time cue (`cued-v0.3`) move TMD's evidence up in what recall returns?

Registered before the measured run. The instrument, arms, target questions,
metric, preconditions and decision rule below are fixed by this document.
What had been run and seen before it was written is listed under "Seen before
registration". A later change is written under "Amendments", with what had
been seen when it was written; the text above it is not edited.

## Question

On the questions of the LMEB TMD task whose text places what they ask about in
time, does passing `recall` the cue the question states move the evidence up
in the rows it returns — **against the same call without a cue, and against
asking for as many more rows without a cue?**

The second comparison is new. `cued-v0.3` (`docs/RECALL_PROCESS_DESIGN.md`
§2.11) holds up to three seats beyond `limit`, so a cued call returns more rows
than an uncued one. On LongMemEval, replayed after that measurement's verdict,
most of the revised policy's gain came from the extra places themselves
(`results-longmemeval-time-cue.md`, exploratory section). A claim that the cue
helps must therefore beat a caller who simply asks for more.

## What is claimed, and what is not

- The claim is at the **retrieval stage**: the evidence's places among the
  returned rows. No reader and no answer accuracy.
- The cue is the one the question **states**, extracted by a model from the
  question text and the question date only, never from the evidence.
- The policy is `cued-v0.3`, the package at `ca1af85` (the registration
  commit adds benchmark files only). The cue stays opt-in per call whatever
  the verdict.

## Instrument

`benchmarks/longmemeval_time_cue.py --task TMD` at the commit that adds this
file.

- **Data.** LMEB TMD: 12 scenes (dated two-person conversations), 7,463 turns
  stored one record per turn. The nine subtasks that address a time:
  `content_time_qs`, `date_span_time_qs`, `dates_time_qs`, `day_span_time_qs`,
  `earlier_today_time_qs`, `last_named_day_time_qs`, `month_time_qs`,
  `rel_day_time_qs`, `rel_month_time_qs` (1,252 questions). The three that
  address sessions by number are out of scope: a time cue names a time, not a
  session index.
- **Question keys.** TMD repeats query ids across its subtasks, so a question
  is keyed `<subtask>/<query id>` and its evidence is read from its own
  subtask's relevance file.
- **Question date.** The `[Current time: …]` a question states; a question
  without one takes the time its scene's other questions state (each scene
  states exactly one). The query text sent to `recall` is LMEB's, unchanged.
- **Haystack.** As in `prereg-longmemeval-time-cue.md`: each scene in its own
  channel, recall inside it, `limit=10`, `rrf`, autocut and the fused gate at
  the build's defaults, the threshold calibrated once after storing, every
  record at the time in its title (read as UTC), the clock the recall path
  reads stopped at the question date, the recall counters reset before every
  call, all arms in one process against one store.
- **Cues.** `tmd_time_cues.jsonl`, extracted with the LongMemEval extractor
  unchanged (`longmemeval_time_cue_extractor.json`: model `claude-sonnet-5`,
  effort `medium`, the same system prompt and schema, `extractor_sha`
  `551ef1e5812749c1`). The extractor saw the question date and the question
  text with the `[Current time: …]` bracket removed.

## Arms

| arm | call | role |
| --- | --- | --- |
| `none` | no cue, `limit=10` | reference |
| `none_again` | the same again | determinism control |
| `extracted` | the extracted cue, `limit=10` | **the treatment** |
| `control` | no cue, `limit = 10 + k`, where `k` is the number of seats `extracted` filled (no call when `k = 0`: the rows of `none`) | **as many rows, without the cue** |
| `shifted` | the period moved to where it does not overlap (as in the LongMemEval registration) | report only: a wrong cue |
| `half` | the period moved half its length into the past | report only: a partly right cue |

## Target questions

The questions with a cue, less those whose `extracted` response says the cue
was `ignored` (a cue for the last 24 hours only is not used, by design), less
any without evidence in the relevance file.

## Metric

As in the LongMemEval registration: per question and arm, NDCG over every row
returned, best first, with no cutoff, normalised by the ideal of all evidence
placed first.

- **D1** = NDCG(`extracted`) − NDCG(`none`)
- **D2** = NDCG(`extracted`) − NDCG(`control`)

## Preconditions — a run that fails any of them is void, not null

1. At least **60** target questions.
2. `none` and `none_again` return identical lists on every question.
3. On every target question, every row `none` returns is also returned by
   `extracted`, which returns at most `L` rows more for the confidence it
   searched at (3 / 2 / 1 for sure / likely / vague).
4. `extracted` differs from `none` on at least one target question.
5. Every `extracted` response reports policy `cued-v0.3`.
6. Every target question has a `control` row.

## Decision rule (one run, judged once)

The time cue **moves the evidence up beyond what more rows alone give** if the
run is valid and all three hold:

1. Sign-flip permutation test on Σ D1, one-sided, **p < 0.05**.
2. Sign-flip permutation test on Σ D2, one-sided, **p < 0.05**.
3. **No subtask falls**: in every subtask, the target questions with D1 < 0
   minus those with D1 > 0 are at most **max(2, ⌈0.05 · n⌉)**, n the subtask's
   target questions.

Both tests use only the nonzero differences: exact over all sign vectors when
at most 20 are nonzero, otherwise 100,000 random sign vectors from seed
20260927, p = (1 + count(S* ≥ S)) / 100,001. Requiring both 1 and 2 needs no
correction for testing twice: the claim is their conjunction.

Otherwise the verdict is **null** and the cue remains an opt-in capability
with no precision claim. If 1 holds and 2 does not, the result is reported as
"the cue's gain is not distinguishable from asking for more rows", not as an
improvement.

`python benchmarks/longmemeval_time_cue.py judge DIR` computes this verdict;
the tests pin each clause, and re-judging the recorded LongMemEval run
reproduces that run's published verdict.

## Planning numbers (seen before registration; nothing from the evidence or an outcome)

From the cue file, the stored record times and the question dates only:

- 1,252 questions, 0 extraction errors, **1,221 with a cue** (1,051 `sure`,
  170 `likely`; 862 absolute, 359 relative). By subtask: content 146 of 177,
  every question of the other eight.
- The fraction of the scene's turns inside the period the server searches (the
  cue widened by its confidence margin): at most 5% on **577** questions, at
  most 25% on **458**, more on **59**, the whole scene on **0**. The period
  holds no turn on **73**. 69 of them are `rel_day_time_qs`: the server reads
  "n days ago" as the 24 hours centred on the same clock time n days back, and
  on those questions the nearest turn lies 0.1 to 19.6 hours outside that
  window (median 10.4). This is left as it is: changing how a cue is read
  after looking at this task's record times would fit the policy to the test.
  **54** point
  only at the last day and are expected to be ignored (all 12 of
  `earlier_today_time_qs`, 24 of `dates_time_qs`, 13 of `rel_day_time_qs`,
  5 of `content_time_qs`). Expected target: about 1,167.
- Unlike LongMemEval, where a scene spans a median of ten days and 28 of 69
  periods covered the whole scene, TMD's scenes span five to nine months, so
  nearly every period selects a small part of its scene.
- The published cue file carries the key, question date, how the date was
  found, the cue and the extractor's hash, not TMD's question text: the text
  is LMEB's, and the extractor's input is rebuilt from it by the rule above.

## Seen before registration

- The LongMemEval measurement and its exploratory replay, which motivated
  `cued-v0.3` and the control arm (`results-longmemeval-time-cue.md`).
- Wiring runs of this instrument with `cued-v0.3` on six LongMemEval questions
  **without** a cue (not TMD, not targets), given artificial cues on the week
  of their first evidence session: every arm, the control's raised count, and
  the determinism control behaved as specified. One of them first showed that
  a control built from the rows the count cut had none to add, and the control
  was changed to a raised count before this registration.
- The extracted TMD cues themselves (their forms, confidences and the planning
  numbers above). No TMD question has been asked of `recall`.

## Reported, not part of the rule

For every arm over the target questions: mean NDCG, mean reciprocal rank of the
best evidence row, questions with all / any evidence returned, evidence rows in
the first five, mean rows returned. Paired up/down counts against `none` for
`extracted`, `control`, `shifted` and `half`. Questions where a seat carried
evidence, and where the searched period contained an evidence record. The
confidence mix, the ignored count, the median latency per arm. D1 and D2 by
subtask and by the planning strata above — exploratory.

## Outputs

`results-tmd-time-cue.md` beside this file, and the run's `rows.jsonl` and
`run.json` under `tmd_time_cue/`.

## Amendments

(none)
