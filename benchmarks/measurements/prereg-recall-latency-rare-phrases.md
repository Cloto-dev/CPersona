# Registration: recall over the same 100,000 memories with the keyword arm ranked without the query's common phrases

Registered 2026-10-06, before any run it governs. The commit that adds this file is pushed
before the first measurement on the reference machine.

## Why this exists

With the arms run beside each other and an embedding server that does not spin, recall over
the corpus of [prereg-recall-latency-realistic-corpus.md](prereg-recall-latency-realistic-corpus.md)
took a median of 1.25 s on the reference machine
([results-recall-latency-concurrent-arms.md](results-recall-latency-concurrent-arms.md),
arm D). The keyword arm ranks every record the question's words match, and a question's
words match 99.9% of these records, mostly through words such as "the" and "was" that
FTS5's bm25 weighs at its idf floor. The change on this branch ranks the keyword arm without
those phrases when that provably cannot change its rows or their order, for the fusions that
read only the order (rrf, the default, and the cascade).

Unregistered, on an Apple laptop: the keyword arm's median went from 444.6 to 275.5 ms on this
corpus, every one of 212 question-depth pairs returned the whole expression's rows in its
order, and whole recalls with the same pre-computed query vectors went from 512 / 456 ms to
305 / 298 ms (two runs each, alternating) with identical responses for all 50 questions.

## What is measured

Everything not named here is as in the first registration: the machine, the configuration
(`rrf`), the queries, the settling and the driver.

- **Corpus**: the same database, verified by SHA-256
  (`b5536681b139c60157a80447e386a05c794c59e8a85092a6a6cd980e9eae2cc2`) on the reference
  machine before the run.
- **CPersona**, two trees: `master` at `302dbcd` (the arms beside each other), and this branch
  at the commit that adds this file (the same code as `4291b63`, plus this file). The driver
  is the same file in both.
- **Embedding server**: CEmbedding `main` at `b8b075d`, `onnx_jina_v5_nano`, default threads,
  `ONNX_ALLOW_SPINNING` unset (off, the default there). One `/embed` call before the first arm
  is checked to leave the server at no more than 0.1 CPU-seconds in the second after it.
- **Machine state**: the 8 GB virtual machine is shut down before the first arm and started
  again after the last; the containers stay running as found. `MemAvailable` is recorded
  immediately before each arm.

### Four arms, in this order

| Arm | CPersona |
| --- | --- |
| A1 | `master` |
| B1 | this branch |
| A2 | `master` |
| B2 | this branch |

Each arm is one run of the driver, as in the first registration.

## The rules, fixed now

The figure read is the median of an arm's 25 real-client `do_recall` samples.

If B1 ≤ 0.9 × A1 **and** B2 ≤ 0.9 × A2, the record may say that ranking the keyword arm without
the query's common phrases lowered the median recall time on this machine, giving both pairs.
If B1 > 1.05 × A1 **or** B2 > 1.05 × A2, the branch is not merged on this evidence: it made
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
fusion, which keeps the whole expression, nor released versions.
