# Results — the time cue (`cued-v0.3`) on TMD (2026-09-27)

Pre-registration: `prereg-tmd-time-cue.md` (committed and pushed before this
run, `70c9b86`). Instrument: `benchmarks/longmemeval_time_cue.py --task TMD`
at the same commit; package code as of `ca1af85` (policy `cued-v0.3`). Raw
output: `tmd_time_cue/rows.jsonl.gz` and `run.json` beside this file;
`judge` reads the gzipped rows as they are.

## Verdict: pass

The run is valid and all three clauses of the rule hold. **On TMD's questions
that state a time, the cue the question states moves the evidence up in the
rows `recall` returns — against the same call without a cue, and against
asking for as many more rows without one.**

## Run

1,221 questions with a cue, 12 scenes, 7,463 turns stored, 7,254 calls,
250 s. Calibrated threshold 0.667 (`separation`). Every vector came from the
embedding cache.

## Preconditions

| precondition | required | observed |
| --- | --- | --- |
| target questions | ≥ 60 | 1,167 (1,221 cued, 54 ignored as pointing at the last day) |
| `none` = `none_again` | every question | every question |
| `extracted` keeps every `none` row, adds at most `L` | every target | every target |
| `extracted` differs from `none` | ≥ 1 | 1,149 |
| policy reported | `cued-v0.3` | `cued-v0.3` on every response |
| a `control` row per target | every target | every target |

## Decision rule

| clause | required | observed |
| --- | --- | --- |
| D1 = NDCG(`extracted`) − NDCG(`none`), sign-flip, one-sided | p < 0.05 | 1,029 up, **0 down**, Σ D1 = +108.00; p = 1.0 × 10⁻⁵ (the floor of 100,000 draws) |
| D2 = NDCG(`extracted`) − NDCG(`control`), sign-flip, one-sided | p < 0.05 | 1,003 up, 16 down, Σ D2 = +90.66; p = 1.0 × 10⁻⁵ |
| no subtask falls by more than max(2, ⌈0.05 n⌉) net | every subtask | no subtask has a question down on D1 |

## Improvement over no cue

Over the 1,167 target questions (`control` = no cue, `limit` raised by the
seats the cue filled):

| measure | no cue | cue (`extracted`) | change | as many rows, no cue (`control`) | change |
| --- | ---: | ---: | ---: | ---: | ---: |
| mean NDCG over returned rows | 0.1889 | 0.2814 | **+49.0%** | 0.2037 | +7.8% |
| mean reciprocal rank of the best evidence row | 0.4495 | 0.5870 | **+30.6%** | 0.4374 | −2.7% |
| questions with any evidence returned | 794 | 1,113 | **+40.2%** | 820 | +3.3% |
| questions with all evidence returned | 110 | 115 | +4.5% | 113 | +2.7% |
| evidence rows in the first five | 1,773 | 2,020 | +13.9% | 1,743 | −1.7% |
| mean rows returned | 10.00 | 12.77 | +27.7% | 12.77 | +27.7% |
| median latency per call | 13.0 ms | 28.3 ms | +117% | — | — |

TMD's evidence is often a whole session of turns, more than any ten rows can
hold, so "all evidence returned" stays rare in every arm; NDCG and the
reciprocal rank carry the effect.

By subtask (mean NDCG):

| subtask | n | no cue | cue | control |
| --- | ---: | ---: | ---: | ---: |
| `content_time_qs` | 141 | 0.5834 | 0.6511 | 0.5913 |
| `date_span_time_qs` | 180 | 0.1707 | 0.2378 | 0.1947 |
| `dates_time_qs` | 306 | 0.2795 | 0.4268 | 0.3112 |
| `day_span_time_qs` | 24 | 0.1934 | 0.2497 | 0.2090 |
| `last_named_day_time_qs` | 12 | 0.0771 | 0.1771 | 0.0863 |
| `month_time_qs` | 100 | 0.0911 | 0.1324 | 0.0993 |
| `rel_day_time_qs` | 304 | 0.0126 | 0.1135 | 0.0141 |
| `rel_month_time_qs` | 100 | 0.0341 | 0.0735 | 0.0385 |

## Reported, not part of the rule

| arm | mean NDCG | mean RR | all evidence | any evidence | evidence in first five |
| --- | ---: | ---: | ---: | ---: | ---: |
| `none` | 0.1889 | 0.4495 | 110 | 794 | 1,773 |
| `none_again` | 0.1889 | 0.4495 | 110 | 794 | 1,773 |
| `extracted` | 0.2814 | 0.5870 | 115 | 1,113 | 2,020 |
| `control` | 0.2037 | 0.4374 | 113 | 820 | 1,743 |
| `shifted` | 0.2384 | 0.5031 | 113 | 1,053 | 1,831 |
| `half` | 0.2789 | 0.5701 | 115 | 1,126 | 1,978 |

- Paired against `none` (reciprocal rank): `extracted` 684 up / 0 down,
  `control` 41 / 120, `shifted` 465 / 41, `half` 675 / 6.
- A seat carried evidence on 986 questions; the searched period contained an
  evidence turn on 1,146. Confidence: 1,009 sure, 158 likely.
- **The `shifted` arm is not a fully wrong cue here, and understates the harm
  of one.** It moves a period clear of the original after each is widened by
  the cue's *own* margin. When the moved period holds nothing, the server
  widens it one confidence step (`docs/RECALL_PROCESS_DESIGN.md` §2.5), which for a one-day `sure` cue reaches
  half a day into the neighbouring days — and on TMD, where a question's day
  holds a session and its neighbours usually do not, into the right one.
- D by the planning strata: where the period held at most 5% of the scene
  (577), D1 averaged +0.124 (506 up, 0 down) and D2 +0.107 (503 / 3); at most
  25% (458): +0.069 and +0.054; more (59): +0.020 and +0.014; no turn in the
  period (73): +0.053 and +0.052 — the one revision found the session on most
  of those. The narrower the period, the larger the effect.

## What this does and does not establish

- It establishes, at the retrieval stage, that a correct stated time moves
  evidence up beyond what the same number of extra rows gives, on a task
  whose questions depend on time. It does not measure answer accuracy.
- On LongMemEval, whose scenes span a median of ten days, the same mechanism
  (as `cued-v0.2`) was not distinguishable from no cue. The effect is carried
  by periods that select a small part of what a store holds.
- A wrong cue's cost is bounded by construction (`L` places, `L` seats), not by
  this run: the `shifted` arm above is too kind to measure it.
