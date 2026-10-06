# Results: recall with the keyword arm ranked without the query's common phrases

Registration: [prereg-recall-latency-rare-phrases.md](prereg-recall-latency-rare-phrases.md),
pushed in `fb21727` at 07:28 UTC on 2026-10-06, before the run at 11:11 UTC. Records:
[rare-A1.json](recall_latency_realistic/rare-A1.json),
[rare-B1.json](recall_latency_realistic/rare-B1.json),
[rare-A2.json](recall_latency_realistic/rare-A2.json),
[rare-B2.json](recall_latency_realistic/rare-B2.json), the machine state before and after each
arm (`rare-*-state-*.txt` beside them), and the server check
([rare-spin-check.txt](recall_latency_realistic/rare-spin-check.txt)).

**Verdict.** Ranking the keyword arm without the query's common phrases lowered the median
recall time on the Intel N150 in both registered pairs: 1,274.59 → 1,055.09 ms (×0.828) and
1,308.21 → 1,048.28 ms (×0.801).

With it, recall over 100,000 memories of real length (about 56 million cl100k tokens in all),
including embedding the query, took a median of **1.06 s and 1.05 s**, at most **1.57 s and
1.53 s**. Both medians are above 1,000 ms, so the third row of the table applies: no
sub-second statement.

## Run

The corpus was verified by SHA-256 at the start of the run. The 8 GB virtual machine was shut
down before the first arm, was found shut down at the start and the end of every arm, and was
started again after the last. A 2 GB and a 3 GB container ran throughout. `MemAvailable` was
12.6 to 12.8 GB at the start of each arm.

The embedding server was CEmbedding `main` at `b8b075d`, `onnx_jina_v5_nano`, default threads,
`ONNX_ALLOW_SPINNING` unset; one `/embed` call was followed by 0.00, 0.01 and 0.00 CPU-seconds
of the server's in the next second.

| Arm | CPersona | median | p95 | max | min |
| --- | --- | ---: | ---: | ---: | ---: |
| A1 | `master` `302dbcd` | 1,274.59 ms | 1,580.81 | 2,057.37 | 797.73 |
| B1 | this branch | 1,055.09 ms | 1,389.09 | 1,574.34 | 600.92 |
| A2 | `master` `302dbcd` | 1,308.21 ms | 1,726.42 | 2,249.32 | 864.86 |
| B2 | this branch | 1,048.28 ms | 1,377.09 | 1,529.68 | 674.80 |

The figures are `do_recall` with the query embedded by the model, 25 questions per arm.

**Validity.** The load fell below 1.0 after 45, 45, 60 and 45 s; every recall returned 12
rows; no sample errored; width 768; exactly 100,000 memories; the virtual machine was shut
down at the start and the end of each arm. No arm was repeated.

## What else the records show (not registered)

- **The stand-in recalls moved alike**: 1,164.97 and 1,221.94 ms on `master`, 963.11 and
  932.19 ms on this branch. The change is in recall, not in the embed call, whose median was
  126.03, 127.43, 125.59 and 126.06 ms in the four arms.
- **Arm A1 repeats an earlier run**: the same tree and server setting as arm D of
  [results-recall-latency-concurrent-arms.md](results-recall-latency-concurrent-arms.md)
  took 1,249.12 ms there and 1,274.59 ms here.

## Limits

As in the first registration: one machine, one corpus, one configuration (`rrf`), English text,
recall quality not checked here (the responses of the two trees were compared on another
machine and were identical, and the keyword arm alone was compared on this corpus and on a
pack of Japanese questions). The `rsf` fusion keeps the whole expression and is not measured.
The trees are `master` and an unmerged branch, not releases.
