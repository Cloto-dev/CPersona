# Results — LongMemEval with a time cue (2026-09-27)

Pre-registration: `prereg-longmemeval-time-cue.md` (committed and pushed
before this run, `7087b6e`). Instrument: `benchmarks/longmemeval_time_cue.py`
at the same commit, package at `20f75d9` (policy `cued-v0.2`). Raw output:
`longmemeval_time_cue/rows.jsonl` and `run.json` beside this file.

## Verdict: null

The run is valid, and the rule is not met. **No precision claim is made for
the time cue.** It remains an opt-in capability; a revised policy is to be
measured under a new name on questions other than these.

## Run

73 questions with a cue, 73 scenes, 34,734 sessions stored, 365 calls,
205 s. Calibrated threshold 0.3943 (`separation`). Every vector came from the
embedding cache (no cache line reported a miss).

## Preconditions

| precondition | required | observed |
| --- | --- | --- |
| target questions | ≥ 60 | 69 (73 cued, 4 ignored as pointing at the last day) |
| `none` = `none_again` | every question | every question |
| `extracted` keeps every `none` row, adds at most one | every target | every target |
| `extracted` differs from `none` | ≥ 1 | 30 |
| policy reported | `cued-v0.2` | `cued-v0.2` on every response |

## Decision rule

| clause | required | observed |
| --- | --- | --- |
| sign-flip test on Σ D, one-sided | p < 0.05 | Σ D = +0.3242; 8 up, 5 down; **p = 0.2642** (exact over 13 nonzero) |
| no type falls by more than 2 net | every type | temporal reasoning 7 up / 3 down, multi-session 1 / 1, knowledge update 0 / 1, single-session user 0 / 0 — met |

## Reported, not part of the rule

Over the 69 target questions:

| arm | mean NDCG | mean RR | all evidence returned | any evidence returned | evidence rows in the first five |
| --- | ---: | ---: | ---: | ---: | ---: |
| `none` | 0.6633 | 0.7137 | 39 | 62 | 117 |
| `none_again` | 0.6633 | 0.7137 | 39 | 62 | 117 |
| `extracted` | 0.6680 | 0.7187 | 39 | 63 | 116 |
| `shifted` (wrong cue) | 0.6628 | 0.7062 | 39 | 62 | 118 |
| `half` (partly right) | 0.6650 | 0.7081 | 39 | 62 | 117 |

- Paired against `none`: `extracted` RR 6 up / 2 down, all-returned 0 / 0;
  `shifted` RR 0 up / 2 down; `half` RR 2 up / 4 down.
- The held place carried evidence on 1 question. The searched period
  contained an evidence session on **64 of 69** questions: the cues were right.
- Confidence: 26 sure, 42 likely, 1 vague. Median latency per call: `none`
  90 ms, `extracted` 185 ms.
- D by the planning strata: the period covered the whole scene on 28
  questions (0 up, 0 down), part of it on 30 (8 up, 5 down, Σ D = +0.3242),
  and no session on 11 (0 up, 0 down) — the whole effect sits where the
  planning numbers said it could.

## Exploratory (after the verdict; not a claim)

Everything below was looked for after the verdict, on these same questions, so
it can suggest a revision and cannot support one.

**Where the evidence is lost without a cue.** Of 186 evidence sessions over
the 69 questions, 139 are returned, 19 pass the gate but are cut by the count
(fused places 11 to 18), and 28 are reached by no retrieval arm. None is
removed by the gate. 34 of the 47 lost sessions lie inside the cue's period,
but the cue arm reached only 3 of them: it searches to the same depth as the
count (10), and a period that covers most of a scene ranks what the ordinary
arms already rank. The ones the count cut were reached by an ordinary arm, so
they are not eligible for the held place, and the move comes after the cut, so
it cannot bring them back either.

**What changes would do** (replayed from recall traces of the same store; the
replay of `cued-v0.2` reproduces the live result on every question):

| policy (replayed) | mean NDCG | all evidence returned | rows returned (mean) |
| --- | ---: | ---: | ---: |
| no cue | 0.6633 | 39 | 10.00 |
| `cued-v0.2` as shipped | 0.6680 (+0.7%) | 39 | 10.32 |
| cue arm searched to 50, independent of the count | 0.6712 (+1.2%) | 40 | 10.97 |
| … and held places for rows the count cut (not only rows no arm reached), three / two / one by confidence | 0.6842 (+3.1%) | 43 (+10.3%) | 12.17 |
| no cue, the same number of extra rows from the uncued order | 0.6745 (+1.7%) | 40 | 12.17 |
| the last policy with the wrong (shifted) cue | 0.6627 (−0.1%) | 39 | 10.90 |

More places buy most of that gain on their own: against the equal-rows
control the revised policy is 14 up / 9 down (p = 0.14). Part of it is the
cue's (all evidence returned 43 against 40), and a wrong cue costs almost
nothing, but on LongMemEval, where one scene spans a median of ten days and
most periods cover most of a scene, the cue has little to select. A benchmark
whose questions depend on time (the LMEB TMD task, dated conversations with
questions such as "what did we discuss on May 8th?") is the natural place to
measure a revised policy.
