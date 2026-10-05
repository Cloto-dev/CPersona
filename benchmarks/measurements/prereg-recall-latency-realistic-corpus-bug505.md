# Registration: recall over the same 100,000 memories with bug-504 and bug-505 fixed, the host's memory free

Registered 2026-10-05, before any run it governs. The commit that adds this file is pushed
before the first measurement on the reference machine.

## Why this exists

With bug-504 fixed, recall over the corpus of
[prereg-recall-latency-realistic-corpus.md](prereg-recall-latency-realistic-corpus.md)
took a median of 7.20 s on the reference machine
([prereg-recall-latency-realistic-corpus-bug504.md](prereg-recall-latency-realistic-corpus-bug504.md)).
An unregistered profile there found two things:

- **bug-505.** The block arm's two hundred re-rank vectors were read with a row-value
  `IN (VALUES ...)`, which SQLite 3.40.1 — the version the machine's Python links — answers
  by scanning every stored vector: 4.6 s of a 6.7 s recall. The library the earlier
  profile used searches the key for the same statement, which is why it was not seen
  there.
- **Memory.** The machine also runs an 8 GB virtual machine, which left about 5 GB for the
  page cache against a 5.3 GB database. The kernel reported that tasks were stalled on IO
  about half of the time while recall ran.

This run measures recall with both defects fixed, with that virtual machine shut down,
so the figure describes an N100-class PC that runs CPersona without another 8 GB guest on
it.

## What is measured

Everything not named here is as in the first registration: the machine, the embedding
server (CEmbedding 0.8.0, `onnx_jina_v5_nano`, default threads), the configuration, the
queries, the settling and the driver.

- **Corpus**: the same database, verified by SHA-256
  (`b5536681b139c60157a80447e386a05c794c59e8a85092a6a6cd980e9eae2cc2`) on the reference
  machine before the run.
- **Code**: `master` at `adb422e` with the fix branch at `dc20a2c` merged (bug-504 in
  `de5597e`, bug-505 in `dc20a2c`), plus the driver of this branch. The tree is built with
  `git merge` and its commit is recorded in the results.
- **Machine state**: the 8 GB virtual machine is shut down before the run and started
  again after it; a 2 GB container stays running. `MemAvailable` is recorded immediately
  before the driver starts.
- **One arm.** No control arm: the earlier registration measured the tree without the
  fixes on the same corpus, and this run asks only what recall takes with them.

## The rules, fixed now

With the 25 timed real-client `do_recall` samples, the table of the first registration
applies unchanged:

| Outcome | What may be written |
| --- | --- |
| median < 1,000 ms **and** every sample < 1,000 ms | "Recall over 100,000 memories of real length (about 56 million tokens in all) completes in under one second on an N100-class PC, including embedding the query." The median may be given in seconds to two decimals |
| median < 1,000 ms, some sample ≥ 1,000 ms | The median, stated as a median, with the maximum beside it |
| median ≥ 1,000 ms | No sub-second statement. The measured median and maximum are stated as they are |

Any statement made from this run names the machine state: the reference machine with
nothing else of size running on it.

The run is invalid, and is repeated once rather than read, under the conditions of the
first registration (the load not falling below 1.0 within 15 minutes, a real-client sample
erroring or returning no vector, an output width other than 768, a recall returning no
rows, a corpus not holding exactly 100,000 memories), and also if the virtual machine is
found running at the start or the end of the run.

## What this does not measure

As in the first registration: recall quality, Japanese text, episodes, concurrent
requests, a cold start of the embedding server, and any other machine. It does not measure
a released version: the tree is `master` with an unmerged change. It does not measure the
machine with the virtual machine running; that figure is the 7.20 s of the earlier run,
without the bug-505 fix.
