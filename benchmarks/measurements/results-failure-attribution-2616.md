# Results: attributing every wrong answer of the 2.6.16 LongMemEval-S run

Run: 2026-11-21. The procedure is the one the
[2.6.11 attribution](results-failure-attribution-2611.md) followed, pointed at
the 2.6.16 run of the [version comparison](results-version-comparison-2512-to-2616-lme.md).
No model was called: the reader's answers and the judge's verdicts are that
run's.

**Verdict.** All 58 failures of the run were coded. None was lost at retrieval.
3 stay unattributed (5.2%), and all three are questions whose benchmark data
marks no turn as the answer. Each failure is also kept as a record of the
auditor profile, with one difference from the published format, described
[below](#the-records).

## What was coded

- **The run**: CPersona 2.6.16, `reconstruct` in `mode: "pro"`, on the cleaned
  LongMemEval-S, all 500 questions, answered by `gpt-4.1-mini` and judged by
  `gpt-4o-mini`. 442 of 500 were judged correct.
- **The failures**: the 58 questions judged wrong.
- **The answer's records**: matched as for 2.6.11. All 948 answer sessions
  matched exactly one record.
- **The traces**: the run's Pro search, re-run with `trace: true` against a
  v2.6.16 server on a copy of the run's store, with the run's calibration file
  restored and no embedding failure. For all 58 questions the re-run rendered
  text the run had shown.
- **The tool**: `benchmarks/recall_trace_confirm.py` at `e843800`, the version
  that writes `attribution_record`. Its `confirm` is the one the 2.6.11
  attribution used after the tool learned four more codes.

## Codes

| Code | 2.6.11 (52 failures) | 2.6.16 (58 failures) |
| --- | ---: | ---: |
| `CANDIDATE_MISS`, `FILTER_DROP`, `RANKING_MISS` (lost at retrieval) | 0 | 0 |
| `CAPTURE_MISS`, `REPRESENTATION_MISS`, `SYSTEM_DEGRADED` | 0 | 0 |
| `RECONSTRUCTION_LOSS` (returned, the answer's passage not shown) | 22 | 29 |
| `EVIDENCE_NOISE` (returned, nothing of it shown) | 1 | 1 |
| `AGENT_MISUSE` (the answer shown, the reader still wrong) | 25 | 25 |
| `UNATTRIBUTED` | 4 | 3 |
| **Unattributed rate** | **7.7%** | **5.2%** |

By question type: multi-session 13 misuse, 12 reconstruction losses and 1
unattributed; temporal reasoning 7 misuse, 7 reconstruction losses and 1
evidence noise; knowledge update 3 misuse, 3 reconstruction losses and 1
unattributed; single-session-assistant 5 reconstruction losses;
single-session-user 1 misuse, 1 reconstruction loss and 1 unattributed;
single-session-preference 1 misuse and 1 reconstruction loss.

The 39 questions both runs answered wrong received the same code in both. 13
questions were wrong only on 2.6.11 and 19 only on 2.6.16; the difference
between the runs' accuracy (89.6% and 88.4%) is not distinguishable from
chance (two-sided paired binomial, p = 0.38). The extra failures show up as
reconstruction losses.

## The three that stay unattributed

Questions 69 (single-session-user), 230 (multi-session) and 439
(knowledge-update). Their answer sessions carry no `has_answer` turn in the
benchmark data, so there is no answer text to look for in what was shown. Each
record states this as its `unattributed_reason`. Questions 69 and 439 were
also unattributed on 2.6.11; the other two of that run (437 and 441) were
answered correctly on 2.6.16.

## The records

The [published format](../../docs/schemas/recall-failure-attribution-v1.schema.json)
reads `trace_version` 2, and 2.6.16 returns version 1. Version 2 (from
2.6.17a1) rounds numbers to four significant digits and holds each gate
threshold once; it changes no reading of the trace. The 58 records were
therefore written with every field of the format and `trace_version` 1, and
checked against the schema with that one constant changed: all 58 pass, and
records broken in four ways (the reason dropped, an unknown code, a malformed
digest, a reason on an attributed failure) are each rejected. Records in the
published format can be written from 2.6.17's traces on.

## What is not claimed

- Nothing about accuracy beyond the run's own: no answer was produced or judged
  again.
