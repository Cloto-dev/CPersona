# Results: recall over the same corpus with bug-504 fixed

Registration: [prereg-recall-latency-realistic-corpus-bug504.md](prereg-recall-latency-realistic-corpus-bug504.md),
pushed in `ca184f3` before the first run. Records:
[bug504-fixed.json](recall_latency_realistic/bug504-fixed.json),
[bug504-control.json](recall_latency_realistic/bug504-control.json).

**Verdict.** With bug-504 fixed, recall took a median of **7.20 s** (maximum 7.83 s); the
same tree without the fix took a median of 20.64 s (maximum 23.87 s). The registered rule
allows no sub-second statement for the fixed arm. The control arm's median is 2.87 times
the fixed arm's, not the registered five, so under the rule these results say that the
fix does not account for the difference, and the registered reading stops there.

## Runs

Trees: fixed = `7528e0c` (`master` `adb422e`, the fix `de5597e`, the driver `ca184f3`);
control = `07a719f` (`master` `adb422e`, the driver `ca184f3`). The corpus was verified
by SHA-256 before the runs. Everything else as in the first registration.

| | fixed: median | p95 | max | control: median | p95 | max |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `do_recall`, query embedded by the model | **7,196.68 ms** | 7,720.03 | 7,828.74 | **20,639.17 ms** | 23,313.17 | 23,870.01 |
| `do_recall`, stand-in vector | 6,766.88 ms | 7,209.76 | 7,253.99 | 20,063.54 ms | 21,836.11 | 22,521.02 |
| embedding one query alone | 81.32 ms | 123.65 | 181.54 | 82.59 ms | 124.36 | 126.89 |

The fixed arm's median is 0.349 of the control arm's. The control arm's median, 20.64 s,
sits beside the 20.73 s of v2.6.4.

**Validity.** Both arms: the load fell below 1.0 after 30 s; every recall returned 12 rows;
no sample errored; width 768; exactly 100,000 memories.

## About the five-times threshold (not registered)

The two trees differ only by the fix, and the control arm reproduces v2.6.4, so the 13.4 s
between the arms is the fix's. The threshold of five was the speed-up the laptop showed,
written into the rule as if it were the question; what it actually asked was whether the
reference machine would speed up by as much as the laptop did, and it did not. The rule
stands as registered and is reported as such; the next registration does not reuse it.

Why the reference machine did not: an unregistered profile there put 4.6 s of a 6.7 s
recall in the read of the re-rank vectors, which SQLite 3.40.1 answers with a full scan
(bug-505), and the machine was running an 8 GB virtual machine that left the page cache
smaller than the database. Both are taken up by
[prereg-recall-latency-realistic-corpus-bug505.md](prereg-recall-latency-realistic-corpus-bug505.md).
