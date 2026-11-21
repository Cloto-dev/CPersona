# Results: 2.5.12 against 2.6.16 on LongMemEval-S, with a reader

Run: 2026-11-20 (search) and 2026-11-21 (answers and judging). The conditions,
the decision rule and the token count were written down before any answer was
produced; two corrections made before the first search are listed under
[Deviations](#deviations).

**Verdict.** The frontier moved. Both registered rules hold. 2.6.16 in
`mode: "pro"` answered 88.4% of the 500 questions against 45.6% for 2.5.12
(one-sided exact paired binomial, p = 9.6e-47). 2.6.16 in `mode: "lite"`
answered 81.8%, non-inferior to 2.5.12 within the 2-point margin (Tango's
paired score test, z = 12.26 against 1.96), with about half the context tokens
(706 against 1,331 per question). The gap is far larger than the products'
difference alone, for a reason the instrument fixes: see
[What the comparison measures](#what-the-comparison-measures).

## The conditions

| Condition | Code | Call |
| --- | --- | --- |
| 2.5.12 (baseline) | `v2.5.12` | `recall`, `limit: 10`, at that version's default read tier |
| 2.6.16 Pro | `v2.6.16` | `reconstruct`, `mode: "pro"` (cap 5,000 tokens, 15 items) |
| 2.6.16 Lite | `v2.6.16` | `reconstruct`, `mode: "lite"` (cap 3,000 tokens, 10 items) |

- **Data**: the cleaned LongMemEval-S, all 500 questions. Each haystack session
  is stored as one record, at its session date, through each version's own
  `store` (a session can run to tens of thousands of characters, so the server
  ran with `CPERSONA_MAX_CONTENT_LENGTH=80000` and no session was truncated).
  Each version built its own store. 2.6.16 finished building its blocks and
  overflow nodes before the first search.
- **Harness**: OmniMemEval, the same harness and adapter as the
  [2.6.11 run](results-omnimemeval-2_6_11.md). The adapter gained a `recall`
  path for servers older than `reconstruct`; its `reconstruct` path is
  unchanged. Each 2.5.12 row is rendered as its time followed by its content as
  returned.
- **Reader and judge**: `gpt-4.1-mini` answers from the rendered context,
  `gpt-4o-mini` judges, temperature 0, one judge run, the harness's prompts.
  Every condition answered all 500 questions afresh; no earlier answer was
  reused.
- **Embedding**: `bge-m3`, the same server for both versions. Zero embedding
  failures in every run.
- **Calibration**: 2.6.16 restored the calibration file the earlier
  LongMemEval runs restored. 2.5.12 calibrated its global threshold on its own
  store (0.3956) and used the pool-size gate, as 2.6.16 does; see
  [Deviations](#deviations).

## The decision rule

- **A1 (accuracy)**: 2.6.16 Pro is more accurate than 2.5.12. Paired one-sided
  exact binomial on the questions the two answered differently, α = 0.025.
- **A2 (tokens)**: 2.6.16 Lite is non-inferior to 2.5.12 within 2 points
  (Tango's paired score test, one-sided α = 0.025) **and** hands the reader
  fewer context tokens on average (`o200k_base`, counted on the rendered
  context).
- The frontier moved if A1 or A2 holds; each test carries α = 0.025 so that
  either one suffices at 0.05 (Bonferroni).

## Results

| Condition | Accuracy | Against 2.5.12 | Relative | Wrong → right / right → wrong | Mean context tokens | Rule |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 2.5.12 | 45.6% | — | — | — | 1,331 | baseline |
| 2.6.16 Pro | 88.4% | +42.8 pt | +93.9% | 236 / 22 | 3,849 (2.89×) | A1 holds |
| 2.6.16 Lite | 81.8% | +36.2 pt | +79.4% | 210 / 29 | 706 (0.53×) | A2 holds |

At the same or lower cost: Lite hands the reader 0.53 times 2.5.12's context
and answers 36.2 points more. Pro spends 2.89 times the context and answers
6.6 points more than Lite.

### By question type (reported, not tested)

| Type | Questions | 2.5.12 | Pro | Lite | Pro: wrong → right / right → wrong | Lite: wrong → right / right → wrong |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| knowledge-update | 78 | 50.0% | 91.0% | 83.3% | 35 / 3 | 32 / 6 |
| multi-session | 133 | 43.6% | 80.5% | 72.9% | 58 / 9 | 50 / 11 |
| single-session-assistant | 56 | 12.5% | 91.1% | 83.9% | 44 / 0 | 40 / 0 |
| single-session-preference | 30 | 80.0% | 93.3% | 90.0% | 6 / 2 | 5 / 2 |
| single-session-user | 70 | 18.6% | 95.7% | 92.9% | 55 / 1 | 52 / 0 |
| temporal-reasoning | 133 | 65.4% | 88.7% | 81.2% | 38 / 7 | 31 / 10 |

No type fell. In every type some questions 2.5.12 answered were lost: 22 in
all for Pro and 29 for Lite, at most 11 in one type.

Search latency on the development machine that ran the benchmark, median /
p95: 2.5.12 197 / 687 ms, Pro 699 / 1,362 ms, Lite 405 / 648 ms. Latency and
peak memory on the reference machine are [below](#latency-and-peak-memory-on-the-reference-machine).

## Latency and peak memory on the reference machine

Measured on 2026-11-21, reported beside the rule and not part of it. The
machine is the reference Intel N150 (4 cores, 16 GB), with its virtual machine
shut down for the run, as in the earlier latency records.

- **Corpus**: the 100,000 real-length records of the
  [2.6.7 latency record](results-recall-latency-block-index-file.md) (2,691
  characters on average, 768-dimension vectors), not LongMemEval. 2.6.16 opened
  a copy as it was. 2.5.12 received the same text and the same vectors in a
  store of its own schema; 2.5.12 builds no blocks.
- **Calibration**: each version was started once on its store and calibrated
  by its own default procedure, global threshold and this user's gate; every
  timed run restored that calibration.
- **Timing**: each version ran as a server, and a client on the same machine
  called it over MCP once per question, timing the round trip, which includes
  embedding the question (the same embedding server for both). Peak memory is
  the server process's high-water mark. The 53 questions of the 2.6.7 record,
  each condition run twice on a fresh server, in the order 2.5.12, Pro, Lite,
  then again; one untimed call first, and the load average below 1.0 before
  timing.

| Condition | Median | p95 | Max | Peak memory | Median against 2.5.12 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 2.5.12 `recall`, `limit: 10` | 1,711 ms | 2,236 ms | 2,650 ms | 88 MB | — |
| 2.6.16 Pro | 2,232 ms | 2,597 ms | 2,930 ms | 267 MB | +30% |
| 2.6.16 Lite | 1,502 ms | 1,865 ms | 2,244 ms | 261 MB | −12% |

The two passes' medians agree within 3%. No call failed and no embedding
request failed. Lite answers faster than 2.5.12 and Pro slower; both 2.6.16
modes hold about three times 2.5.12's memory (its parts were not measured).
These figures time the whole tool call from outside the server, so they are not
comparable with the 2.6.7 record's, which timed the recall path inside the
process.

## What the comparison measures

This is a comparison of **what the first call shows**. The harness searches
once and answers from that context; it never calls again to read further.
2.5.12's `recall` returns the first 500 characters of each record by default and
leaves the rest to `get_contents`, and on this data a record is a whole
session. So on most questions 2.5.12's reader saw only the opening of each
session, which is why it scored lowest where the answer sits once in the middle
of one session (single-session-user 18.6%, single-session-assistant 12.5%).
Most of the gap is therefore the difference between quoting the part of a long
record that answers and showing its opening, not a difference 2.5.12 would show
if the caller read further. The registered rule was chosen with this limit
known.

## Deviations

- **2.5.12's calibration.** The plan said 2.5.12 would calibrate at startup as
  it does by default. It could not: after calibrating the global threshold,
  2.5.12 calibrates a gate for each user, one at a time, and redoes this on every
  start when no gate is stored. On a store of 500 users this would take days.
  2.5.12 was therefore run in the state 2.6.16 runs in: the global threshold
  calibrated on its own store, no per-user gate, the pool-size gate in its place.
  This was recorded before 2.5.12's search.
- **A wrong expectation corrected.** The plan expected A2's token half to fail,
  reading Lite's cap of 3,000 tokens as its size. Lite's context had measured
  705.5 tokens on 2.6.11's run. The expectation was corrected before the first
  search; the rule did not change.
- **The reference-machine timing** could not reuse the 2.6.7 latency driver,
  which calls 2.6's internals that 2.5.12 does not have. Both versions were
  timed as servers from a client instead, as described above.
- **The first answer pass failed** on an expired API key: every call was
  refused, none succeeded, and nothing was charged. The pass was set aside and
  rerun from the start with a new key. Every condition then answered and was
  judged on all 500 questions in one pass.

## Notes

- 2.6.16 Lite's rendered context was identical, question by question, to
  2.6.11 Lite's published run on all 500 questions, although the store was
  ingested again under new user ids and searched afresh: the changes from 2.6.11
  to 2.6.16 did not change what Lite shows on this data. Its accuracy (81.8%)
  matches that run's (80.75%) within the reader's run-to-run variation. Pro's
  context matched on 14 of 500.
- The harness's own token count (`context_tokens`) is 1,362.8 / 3,927.2 / 722.0,
  a slightly different tokenisation from the registered `o200k_base`; the order
  is the same.
- Answer-side cost: 3,374,710 input and 196,378 output tokens for the reader
  across the three conditions.

## What is not claimed

- Nothing about 2.5.12 used with `get_contents`, which this harness does not
  call.
- Nothing about other benchmarks.
