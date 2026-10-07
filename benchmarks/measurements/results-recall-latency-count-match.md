# Results: recall with common phrases' rows counted by the phrase

Registration: [prereg-recall-latency-count-match.md](prereg-recall-latency-count-match.md),
pushed in `7f70eb7` at 11:50 UTC on 2026-10-07, before the run at 11:51 UTC. Records:
[cm-A1.json](recall_latency_realistic/cm-A1.json),
[cm-B1.json](recall_latency_realistic/cm-B1.json),
[cm-A2.json](recall_latency_realistic/cm-A2.json),
[cm-B2.json](recall_latency_realistic/cm-B2.json), the machine state before and after each
arm (`cm-*-state-*.txt` beside them), and the server check
([cm-spin-check.txt](recall_latency_realistic/cm-spin-check.txt)).

**Verdict.** No change is claimed. The branch lowered the median recall time in both
registered pairs, but by less than the registered 5%: 1,017.70 → 991.45 ms (×0.974) and
1,044.55 → 1,006.51 ms (×0.964). Neither pair made recall slower by more than 5%, so the rules
allow the branch to be merged, since its responses are unchanged, with no speed claimed for it.

With the change, recall over 100,000 memories of real length (about 56 million cl100k tokens
in all), including embedding the query, took a median of **0.99 s and 1.01 s**, at most
**1.45 s and 1.51 s**. One of the two medians is above 1,000 ms, so the third row of the table
applies: no sub-second statement.

## Run

The corpus was verified by SHA-256 at the start of the run. The 8 GB virtual machine was shut
down before the first arm, was found shut down at the start and the end of every arm, and was
started again after the last. A 2 GB and a 3 GB container ran throughout. `MemAvailable` was
11.5 to 11.6 GB at the start of each arm.

The embedding server was CEmbedding `v0.9.1` (`f4c92e8`), `onnx_jina_v5_nano`, default
threads, `ONNX_ALLOW_SPINNING` unset; one `/embed` call was followed by 0.00, 0.01 and 0.00
CPU-seconds of the server's in the next second.

| Arm | CPersona | median | p95 | max | min |
| --- | --- | ---: | ---: | ---: | ---: |
| A1 | `v2.6.6` | 1,017.70 ms | 1,296.58 | 1,482.92 | 620.85 |
| B1 | this branch | 991.45 ms | 1,310.73 | 1,451.57 | 600.74 |
| A2 | `v2.6.6` | 1,044.55 ms | 1,368.43 | 1,569.52 | 606.15 |
| B2 | this branch | 1,006.51 ms | 1,346.54 | 1,507.67 | 590.93 |

The figures are `do_recall` with the query embedded by the model, 25 questions per arm.

**Validity.** The load fell below 1.0 after 45, 75, 60 and 60 s; every recall returned 12
rows; no sample errored; width 768; exactly 100,000 memories; the virtual machine was shut
down at the start and the end of each arm. No arm was repeated.

## What else the records show (not registered)

- **The stand-in recalls moved alike**: 891.30 and 951.16 ms on `v2.6.6`, 852.96 and
  893.88 ms on this branch. The embed call's median was 129 to 131 ms in every arm, so the
  change is in recall, not in the embed call.
- **The saving is smaller than the lookup it removes.** An unregistered breakdown before this
  run, with stand-in query vectors, put the replaced lookup at 139 ms inside a recall and the
  counts that replace it at about a third of the lookup's cost on its own (19.6 against
  66.4 ms). The medians moved by 26 and 38 ms. Where the rest went is not measured here: the
  keyword arm runs beside the vector arm, and with the model embedding the query on the same
  four cores, the arm that decides the total need not be the same one.
- **`v2.6.6` was close to the last registration's figures**: 1,017.70 and 1,044.55 ms here,
  against 1,055.09 and 1,048.28 ms for the tree it was cut from
  ([results-recall-latency-rare-phrases.md](results-recall-latency-rare-phrases.md), arms B1
  and B2), whose recall path it does not change.

## Limits

As in the first registration: one machine, one corpus, one configuration (`rrf`), English text,
recall quality not checked here (the branch's classification of common phrases was compared
with the index vocabulary in the test suite, and its rows with the whole expression's), no
concurrent requests. The `rsf` fusion does not classify common phrases and is not measured.
The trees are `v2.6.6` and an unmerged branch.
