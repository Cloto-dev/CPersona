# Results: attributing every wrong answer of the 2.6.11 LongMemEval-S run

Run: 2026-11-02. Driver: [failure_attribution_2611.py](failure_attribution_2611.py).
The procedure and what would be reported were written down before the search was
re-run. No model was called: the reader's answers and the judge's verdicts are the
published run's.

**Verdict.** All 52 failures of the run were coded. None was lost at retrieval:
the record holding the answer came back for every one. 48 are attributed to what
happened after that, and the 4 that stay unattributed are questions whose
benchmark data marks no turn as the answer, which no tool that reads the data can
attribute. Teaching the confirmation tool four more codes did not lower the
unattributed rate: it is 4 of 52 (7.7%) before and after.

## What was coded

- **The run**: CPersona 2.6.11, `reconstruct` in `mode: "pro"`, on the cleaned
  LongMemEval-S, all 500 questions, answered by `gpt-4.1-mini` and judged by
  `gpt-4o-mini` ([results](results-omnimemeval-2_6_11.md), record
  [cpersona-omnimemeval-2.6.11](https://github.com/Cloto-dev/cpersona-omnimemeval-2.6.11)).
  448 of 500 were judged correct.
- **The failures**: the 52 questions judged wrong.
- **The answer's records**: each question's answer sessions, matched to the record
  that stored them by the text of the session's longest turn. All 948 answer
  sessions of the 500 questions matched exactly one record; none matched two and
  none matched none. For the 479 questions whose data names the answer turns, every
  one of those turns is in the matched record's stored text.
- **The traces**: the run's Pro search, re-run with `trace: true` against a
  v2.6.11 server on a copy of the run's store, with the run's calibration file
  restored (the server reported `restored`, not a recalibration) and no embedding
  failure. For 51 of the 52 questions the re-run rendered text the run had shown;
  for the other one, the per-record shown text is not used.
- **The tool**: `benchmarks/recall_trace_confirm.py` as it was before it learned
  `CAPTURE_MISS`, `REPRESENTATION_MISS`, `SYSTEM_DEGRADED` and `EVIDENCE_NOISE`
  (`3cd1713`), and after (`e33e0e3`), given what each version takes.

## Codes

| Code | Before | After |
| --- | ---: | ---: |
| `CANDIDATE_MISS`, `FILTER_DROP`, `RANKING_MISS` (lost at retrieval) | 0 | 0 |
| `CAPTURE_MISS`, `REPRESENTATION_MISS`, `SYSTEM_DEGRADED` | — | 0 |
| `RECONSTRUCTION_LOSS` (returned, the answer's passage not shown) | 23 | 22 |
| `EVIDENCE_NOISE` (returned, nothing of it shown) | — | 1 |
| `AGENT_MISUSE` (the answer shown, the reader still wrong) | 25 | 25 |
| `UNATTRIBUTED` | 4 | 4 |
| **Unattributed rate** | **7.7%** | **7.7%** |

By question type, after: multi-session 10 misuse and 9 reconstruction losses;
temporal reasoning 10 misuse, 7 reconstruction losses and 1 evidence noise;
knowledge update 3 misuse, 3 reconstruction losses and 3 unattributed;
single-session-assistant 3 reconstruction losses; single-session-user 1 misuse
and 1 unattributed; single-session-preference 1 misuse.

## The four that stay unattributed

Questions 69, 437, 439 and 441 (one single-session-user, three knowledge-update).
Their answer sessions carry no `has_answer` turn in the benchmark data, so there
is no answer text to look for in what was shown, and the tool cannot tell a
passage that was not shown from a reader that misread one. Attributing them would
take a judgement of content, which this instrument does not make.

## What may be said

- On this run every failure came after retrieval: roughly half because the
  answer's passage was not in what was shown, half because the reader misused
  what was.
- The unattributed rate is 7.7%, and it is set by the benchmark data, not by the
  tool: the four codes the tool gained found nothing to name on this run.
- `REPRESENTATION_MISS` is 0 by construction on this store: every answer record's
  blocks carry bits to its end, so the whole of each is within semantic reach.

## What is not claimed

- Nothing about accuracy: no answer was produced or judged again.
- Nothing about the latest release: the run is 2.6.11's.
