# Registration: recall over the same 100,000 memories of real length, with bug-504 fixed

Registered 2026-10-05, before any run it governs. The commit that adds this file is pushed
before the first measurement on the reference machine.

## Why this exists

[results-recall-latency-realistic-corpus.md](results-recall-latency-realistic-corpus.md)
measured a recall median of 20.73 s on the default configuration of v2.6.4 over 100,000
memories of real length, and an unregistered profile put nearly all of it in the block
arm's examined read (bug-504). The fix reads those rows in primary key order and stops at
the cap. This run measures recall with the fix, on the same machine and corpus, and
measures the same tree without the fix beside it, because the tree also carries the other
changes made since v2.6.4.

## What is measured

Everything not named here is as in
[prereg-recall-latency-realistic-corpus.md](prereg-recall-latency-realistic-corpus.md):
the machine, the embedding server and model, the configuration, the queries, the
settling and the driver.

- **Corpus**: the same database, verified by SHA-256
  (`b5536681b139c60157a80447e386a05c794c59e8a85092a6a6cd980e9eae2cc2`) on the reference
  machine before the first run.
- **Embedding server**: default threads only. The thread question was answered by the
  earlier registration and is not asked again.
- **Code**, two arms on one tree each:

| Arm | Tree | Purpose |
| --- | --- | --- |
| Fixed | `master` at `adb422e` with `de5597e` (the fix) merged, plus the driver of this branch | the statement below |
| Control | `master` at `adb422e`, plus the driver of this branch | how much of any change is the fix |

The fixed arm runs first. The trees are built with `git merge` and their commits are
recorded in the results.

## The rules, fixed now

For the fixed arm, with the 25 timed real-client `do_recall` samples, the table of the
earlier registration applies unchanged:

| Outcome | What may be written |
| --- | --- |
| median < 1,000 ms **and** every sample < 1,000 ms | "Recall over 100,000 memories of real length (about 56 million tokens in all) completes in under one second on an N100-class PC, including embedding the query." The median may be given in seconds to two decimals |
| median < 1,000 ms, some sample ≥ 1,000 ms | The median, stated as a median, with the maximum beside it |
| median ≥ 1,000 ms | No sub-second statement. The measured median and maximum are stated as they are |

The control arm supports no statement about speed of its own. The results state the
fixed arm's median as a fraction of the control arm's, and the control arm's median
beside the 20.73 s of v2.6.4; if the control arm's median is not at least five times the
fixed arm's, the results say that the fix does not account for the difference and stop
there.

An arm is invalid, and is repeated once rather than read, under the conditions of the
earlier registration: the load not falling below 1.0 within 15 minutes, a real-client
sample erroring or returning no vector, an output width other than 768, a recall returning
no rows, or a corpus not holding exactly 100,000 memories.

## What this does not measure

As in the earlier registration: recall quality, Japanese text, episodes, concurrent
requests, a cold start, and any other machine. It also does not measure a released
version: the trees are `master` with and without an unmerged change.
