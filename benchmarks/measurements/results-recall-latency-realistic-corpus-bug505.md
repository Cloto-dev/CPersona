# Results: recall over the same corpus with bug-504 and bug-505 fixed, the host's memory free

Registration: [prereg-recall-latency-realistic-corpus-bug505.md](prereg-recall-latency-realistic-corpus-bug505.md),
pushed in `d77c05e` at 02:44 UTC on 2026-10-05, before the run at 02:52 UTC. Records:
[bug505-both-fixes.json](recall_latency_realistic/bug505-both-fixes.json),
[bug505-state-start.txt](recall_latency_realistic/bug505-state-start.txt),
[bug505-state-end.txt](recall_latency_realistic/bug505-state-end.txt).

**Verdict.** On the Intel N150 with nothing else of size running, recall over 100,000
memories of real length (about 56 million cl100k tokens in all) took a median of
**1.94 s** including embedding the query, with a maximum of **2.70 s**. The median is above
1,000 ms, so the third row of the rule applies: no sub-second statement; the median and
the maximum, as they are.

## Run

Tree `0eb5f03`: `master` at `adb422e` with the fixes (`de5597e`, `dc20a2c`) and the driver
(`d77c05e`). The corpus was verified by SHA-256 before the run. The 8 GB virtual machine
was shut down before the run and was still shut down at its end; a 2 GB container ran
throughout. `MemAvailable` was 13,699,532 kB at the start.

| | median | p95 | max | min |
| --- | ---: | ---: | ---: | ---: |
| `do_recall`, query embedded by the model | **1,935.30 ms** | 2,354.90 | **2,699.19** | 1,492.74 |
| `do_recall`, stand-in vector (same texts, same run) | 1,480.83 ms | 1,848.75 | 2,243.87 | 1,104.45 |
| paired difference (real − stand-in) | 450.83 ms | 506.15 | 507.90 | 331.27 |
| embedding one query, client call alone | 112.43 ms | 231.19 | 296.79 | 70.69 |

**Validity.** The load fell below 1.0 after 45 s; every recall returned 12 rows; no sample
errored; width 768; exactly 100,000 memories; the virtual machine was found shut down at
the start and at the end.

## The three runs on this corpus

| tree | machine | median | max |
| --- | --- | ---: | ---: |
| v2.6.4 | 8 GB virtual machine running | 20.73 s | 23.14 s |
| `master` with the bug-504 fix | 8 GB virtual machine running | 7.20 s | 7.83 s |
| `master` with the bug-504 and bug-505 fixes | 8 GB virtual machine shut down | 1.94 s | 2.70 s |

The last two rows change two things at once, the code and the machine, and this run does
not separate them; it was registered to say what recall takes on a machine with its
memory free, and that is what it says.

## Limits

As in the first registration: one machine, one corpus, one configuration, English text,
recall quality not checked. The tree is `master` with an unmerged change, not a release.
