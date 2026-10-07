# Registration: recall over the same 100,000 memories with common phrases' rows counted by the phrase

Registered 2026-10-07, before any run it governs. The commit that adds this file is pushed
before the first measurement on the reference machine.

## Why this exists

With the keyword arm ranked without the query's common phrases, recall over the corpus of
[prereg-recall-latency-realistic-corpus.md](prereg-recall-latency-realistic-corpus.md) took a
median of 1.05 s on the reference machine
([results-recall-latency-rare-phrases.md](results-recall-latency-rare-phrases.md)).

Unregistered, on the reference machine with stand-in query vectors and 25 questions, tree
`v2.6.6`: recall took a median of 1,034 ms. The keyword arm, which runs beside the other
arms, took 1,032 ms of it and decided the total; 139 ms of the keyword arm was the lookup that
finds which phrases are common, and the other arms together took about 706 ms. That lookup
read each phrase's row count from an `fts5vocab` table. SQLite 3.40.1, the version on the
reference machine, walks every position of a term to report how many rows hold it, whether
or not the occurrence count is asked for. Alone, over the same 25 questions, the lookup took
66.4 ms there; counting the rows with one `count(*) ... MATCH` per phrase took 19.6 ms, and
the counts were equal in 25 of 25 questions. The change on this branch counts them that way.
Which phrases are classified, the test for the floor, what is left out and the check on the
ranked rows are unchanged, so the rows returned are the ones `v2.6.6` returns.

## What is measured

Everything not named here is as in the first registration: the machine, the configuration
(`rrf`), the queries, the settling and the driver.

- **Corpus**: the same database, verified by SHA-256
  (`b5536681b139c60157a80447e386a05c794c59e8a85092a6a6cd980e9eae2cc2`) on the reference
  machine before the run.
- **CPersona**, two trees: `v2.6.6` (`36b053f`), and this branch at the commit that adds this
  file (the same code as `6b76cdd`, plus this file). The driver is the same file in both.
- **Embedding server**: CEmbedding `v0.9.1` (`f4c92e8`), `onnx_jina_v5_nano`, default
  threads, `ONNX_ALLOW_SPINNING` unset (off, the default there). One `/embed` call before the
  first arm is checked to leave the server at no more than 0.1 CPU-seconds in the second
  after it.
- **Machine state**: the 8 GB virtual machine is shut down before the first arm and started
  again after the last; the containers stay running as found. `MemAvailable` is recorded
  immediately before each arm.

### Four arms, in this order

| Arm | CPersona |
| --- | --- |
| A1 | `v2.6.6` |
| B1 | this branch |
| A2 | `v2.6.6` |
| B2 | this branch |

Each arm is one run of the driver, as in the first registration.

## The rules, fixed now

The figure read is the median of an arm's 25 real-client `do_recall` samples.

The threshold is 5% rather than the 10% of the earlier registrations, because the change
removes one step of about a tenth of the keyword arm, and the same tree measured twice in the
last registration differed by 2.6% (A1 1,274.59 ms, A2 1,308.21 ms), so 5% sits above that.

If B1 ≤ 0.95 × A1 **and** B2 ≤ 0.95 × A2, the record may say that counting common phrases'
rows by the phrase lowered the median recall time on this machine, giving both pairs. If
B1 > 1.05 × A1 **or** B2 > 1.05 × A2, the branch is not merged on this evidence: it made
recall slower here, and the record says so. Otherwise the record states the medians and
claims no change; the branch may still be merged, since its responses are unchanged, but no
speed is claimed for it.

**What recall takes** with the change: arms B1 and B2 read with the table of the first
registration. A row's statement is made only if both arms meet it.

| Outcome | What may be written |
| --- | --- |
| median < 1,000 ms **and** every sample < 1,000 ms | "Recall over 100,000 memories of real length (about 56 million tokens in all) completes in under one second on an N100-class PC, including embedding the query." The median may be given in seconds to two decimals |
| median < 1,000 ms, some sample ≥ 1,000 ms | The median, stated as a median, with the maximum beside it |
| median ≥ 1,000 ms | No sub-second statement. The measured median and maximum are stated as they are |

An arm is invalid, and is repeated once rather than read, under the conditions of the first
registration (the load not falling below 1.0 within 15 minutes, a real-client sample erroring
or returning no vector, an output width other than 768, a recall returning no rows, a corpus
not holding exactly 100,000 memories), and also if the virtual machine is found running at
the start or the end of the arm.

## What this does not measure

As in the first registration: recall quality, Japanese text, episodes, concurrent requests,
a cold start of the embedding server, and any other machine. It does not measure the `rsf`
fusion, which does not classify common phrases, nor released versions other than `v2.6.6`.
