# Results: the block arm reading its rows from the block index file

Registration: [prereg-recall-latency-block-index-file.md](prereg-recall-latency-block-index-file.md),
pushed in `760c221` at 05:54 UTC on 2026-10-08, before the run began at 05:56 UTC. Records:
[bi-A1.json](recall_latency_realistic/bi-A1.json),
[bi-B1.json](recall_latency_realistic/bi-B1.json),
[bi-A2.json](recall_latency_realistic/bi-A2.json),
[bi-B2.json](recall_latency_realistic/bi-B2.json),
[bi-W.json](recall_latency_realistic/bi-W.json), the machine state before and after each arm
and the write run (`bi-*-state-*.txt` beside them), and the server check
([bi-spin-check.txt](recall_latency_realistic/bi-spin-check.txt)).

**Verdict.**

- **Recall time: lowered.** With the block index file on, the median recall time fell in both
  registered pairs by more than the registered 5%: 960.16 → 797.95 ms (×0.831) and
  930.88 → 844.46 ms (×0.907). Recall over 100,000 memories of real length (about 56 million
  cl100k tokens in all), including embedding the query, took a median of **0.80 s and
  0.84 s**, at most **1.27 s and 1.37 s**. Both medians are below 1,000 ms and some samples are
  not, so the second row of the first registration's table applies: the median, stated as a
  median, with the maximum beside it.
- **The same answer.** Every stand-in recall — 25 in the registered loop and 25 in each of the
  two phases, per arm — returned the same refs in the same order in all four arms. The
  real-client recalls did too.
- **The block arm: lowered.** 582.96 → 64.71 ms (×0.111) and 568.13 → 64.23 ms (×0.113),
  medians over the 25 stand-in recalls of the registered loop.
- **How many changed rows the file may carry: the bound stays at 35,000.** With 33,948 block
  rows of 972 memories marked changed, the block arm took 175.53 and 174.41 ms with the file
  on against 471.69 and 489.47 ms off (×0.372 and ×0.356), within the registered 0.8. At
  9,982 rows of 288 memories: 99.40 and 99.48 ms against 449.10 and 451.70 ms (×0.221 and
  ×0.220).
- **What the change log costs a write: less than 1% of a block build, off and on.** Per
  record, over 2,400 samples each: M0 = 1.0575 ms with the triggers absent, M1 = 1.4302 ms
  with them present and the log off, M2 = 1.6675 ms with the log on. Dividing and embedding a
  record again took a median P of 3,245.21 ms (30 of 30 divisions matched the stored ones).
  With the file off, the change log's triggers add less than 1% to a record's block build on
  this machine: D1 = 0.3727 ms, 0.011% of P + M0. With the file on, the same: D2 = 0.6099 ms,
  0.019%. The database write alone is ×1.352 and ×1.577 of its cost without the triggers.
- **The default.** The rules recommend turning the block index file on by default in `2.6.7`:
  the recall-time rule says it lowered the median, no answer differed, and the log-on write
  statement is the less-than-1% one. As registered, experience with the setting on in a
  deployment is judged separately and can only keep it off.

## Run

The corpus was verified by SHA-256 at the start of the run. A clean shutdown of the 8 GB
virtual machine was asked for first; the guest had not stopped after more than two minutes,
and it was then stopped by force (05:59:09 UTC). It was found stopped at the start and the end of every arm
and of the write run, and was started again after the last (06:18:47 UTC). A 2 GB and a 3 GB
container ran throughout. `MemAvailable` was 11.5 to 11.7 GiB at the start of each arm.

The embedding server was CEmbedding `v0.9.1` (`f4c92e8`), `onnx_jina_v5_nano`, default
threads, `ONNX_ALLOW_SPINNING` unset; one `/embed` call was followed by 0.00, 0.00 and 0.00
CPU-seconds of the server's in the next second. Every arm ran with `PYTHONHASHSEED=0`.

| Arm | Block index file | median | p95 | max | min |
| --- | --- | ---: | ---: | ---: | ---: |
| A1 | off | 960.16 ms | 1,335.17 | 1,362.30 | 582.78 |
| B1 | on | 797.95 ms | 1,089.19 | 1,269.93 | 323.12 |
| A2 | off | 930.88 ms | 1,337.24 | 1,477.87 | 635.51 |
| B2 | on | 844.46 ms | 1,140.19 | 1,374.88 | 409.57 |

The figures are `do_recall` with the query embedded by the model, 25 questions per arm.

| Arm | block arm, loop | 9,982 rows changed | 33,948 rows changed |
| --- | ---: | ---: | ---: |
| A1 | 582.96 ms | 449.10 | 471.69 |
| B1 | 64.71 ms | 99.40 | 175.53 |
| A2 | 568.13 ms | 451.70 | 489.47 |
| B2 | 64.23 ms | 99.48 | 174.41 |

The block arm's time is the median over 25 stand-in recalls.

| Condition | per record, median | mean | WAL frames per run (mean of 8) |
| --- | ---: | ---: | ---: |
| T0, triggers absent | 1.0575 ms | 1.1047 | 11,862 |
| T1, triggers present, log off | 1.4302 ms | 1.5047 | 11,914 |
| T2, triggers present, log on | 1.6675 ms | 1.7604 | 12,798 |

Each run rewrote the blocks of the newest 300 memories, 10,428 block rows in all.

**Validity.** The load fell below 1.0 after 15, 45, 45 and 30 s; every recall returned 12
rows; no sample errored; width 768; exactly 100,000 memories; the virtual machine was shut
down at the start and the end of each arm. In both arms with the file on, the build
succeeded (13.6 and 11.5 s), the file was current before the warm-up, and all 100 block-arm
reads of the registered loop and the phases were served from the file; in both arms with it
off, all 100 were served from SQLite with the reason `off`. Each recall made exactly one
block-arm call. No arm was repeated. In the write run, no truncating checkpoint was busy
(24 of 24), and all 30 divisions matched the stored ones.

## What else the records show (not registered)

- **Most of what the block arm saves does not reach the total.** The block arm fell by about
  500 ms and the median recall by 162 and 86 ms. The block arm runs beside the keyword arm,
  which an earlier unregistered breakdown on this machine found to decide the total; this run
  does not measure which arm decided it. The stand-in recalls moved the same way: 827.47 and
  884.04 ms off, 764.72 and 794.16 ms on. The embed call's median was 127.8 to 132.9 ms in
  every arm.
- **Changed rows cost the file path about 3 ms per thousand.** With the file on, the block
  arm took 64 ms with nothing changed, 99 ms with 9,982 rows changed and 175 ms with 33,948:
  about 3.3 ms more per thousand changed rows, read from SQLite and merged.
- **The arms with the file off were faster in the phases than in the registered loop**
  (449 to 489 ms against 568 and 583 ms). This run does not explain it; the phases follow the
  rewrite of the newest memories' rows and use only stand-in vectors.
- **The file**: 3,480,069 rows of 96 bytes of bits, 431,528,748 bytes, built in 13.6 and 11.5 s.
- **The change log**: 166,848 rows (two per rewritten row, one for its delete and one for its
  insert) took 2,678,784 bytes in 654 pages, about 16 bytes per row. The write-ahead log grew
  by 0.4% per run with the log off and 7.9% with it on.
- **Per-run medians were steady**: 1.049 to 1.081 ms for T0, 1.378 to 1.459 ms for T1 and
  1.628 to 1.719 ms for T2, so the order of the runs did not decide the comparison. P ranged
  from 503 to 7,741 ms per record.

## Limits

As registered: one machine, one corpus (English text, 34.8 blocks per memory), one
configuration (`rrf`), recall quality not checked beyond the rows being the same, no
concurrent requests, no writes arriving during a recall, no build running while recalls are
served. The tree is an unmerged branch whose code is `master` at `3c28370` plus the drivers.
