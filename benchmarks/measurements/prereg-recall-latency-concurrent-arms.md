# Registration: recall over the same 100,000 memories with its arms run beside each other, and an embedding server that does not spin

Registered 2026-10-06, before any run it governs. The commit that adds this file is pushed
before the first measurement on the reference machine.

## Why this exists

With bug-504 and bug-505 fixed, recall over the corpus of
[prereg-recall-latency-realistic-corpus.md](prereg-recall-latency-realistic-corpus.md)
took a median of 1.94 s on the reference machine, with the query embedded by the model,
and 1.48 s with a stand-in vector in the same run
([results-recall-latency-realistic-corpus-bug505.md](results-recall-latency-realistic-corpus-bug505.md)).
Unregistered work since found two things:

- **The arms ran in line.** Every arm of a recall ran on one read connection, whose
  statements aiosqlite runs one after another on one thread. On an Apple laptop the keyword
  arm was 504 of 677 ms of a recall: a question's words match 99.9% of the records and FTS5
  ranks every match. The change on this branch runs the keyword arms on a side connection
  while the query is embedded and scanned, and starts the block arm as soon as the query
  vector exists. On the laptop, the same 50 questions returned identical responses and the
  median recall went from 737 / 665 ms to 471 / 455 ms (two runs each, alternating).
- **The embedding server spins.** After each `/embed` call, the three worker threads of
  ONNX Runtime 1.30.0 on the reference machine kept 3 cores busy for about 1.4 s, the time in
  which recall runs. With `session.intra_op.allow_spinning = 0` that went to 0 and inference
  took no longer (53 ms against 54 ms for a short query). The 1.94 s and 1.48 s above differ by 451 ms, of which embedding the
  query alone is 112 ms. The change is in CEmbedding, behind `ONNX_ALLOW_SPINNING`.

This run measures the two changes on the reference machine, separately and together.

## What is measured

Everything not named here is as in the first registration: the machine, the configuration,
the queries, the settling and the driver.

- **Corpus**: the same database, verified by SHA-256
  (`b5536681b139c60157a80447e386a05c794c59e8a85092a6a6cd980e9eae2cc2`) on the reference
  machine before the run.
- **CPersona**, two trees: `v2.6.6a1` (the release with bug-504 and bug-505 fixed), and this
  branch at the commit that adds this file (the same code as `143a4e6`, which changes the
  arms, plus this file). The driver of each tree is the one at `v2.6.6a1`.
- **Embedding server**: CEmbedding at `632088d` (0.9.0 plus the spinning change),
  `onnx_jina_v5_nano`, default threads, the model files of the first registration, run in
  two settings: `ONNX_ALLOW_SPINNING=1` (what ONNX Runtime does by default) and
  `ONNX_ALLOW_SPINNING=0`. The server is restarted between the settings.
- **Machine state**: the 8 GB virtual machine is shut down before the first arm and started
  again after the last; the containers stay running as found, and are listed. `MemAvailable`
  is recorded immediately before each arm.

### Four arms, in this order

| Arm | CPersona | `ONNX_ALLOW_SPINNING` |
| --- | --- | --- |
| A | `v2.6.6a1` | 1 |
| B | this branch | 1 |
| C | `v2.6.6a1` | 0 |
| D | this branch | 0 |

Each arm is one run of the driver: 3 warm-up, 25 timed questions recalled once with the
stand-in vector and once through the real client in alternating order, and 25 for the
model's embed time alone, as in the first registration.

## The rules, fixed now

The figure read is the median of an arm's 25 real-client `do_recall` samples.

**The arms change** (this branch). If B ≤ 0.9 × A **and** D ≤ 0.9 × C, the record may say
that running a recall's arms beside each other lowered the median recall time on this
machine, giving both pairs of medians. If B > 1.05 × A **or** D > 1.05 × C, the branch is not
merged on this evidence: it made recall slower here, and the record says so. Otherwise the
record states the medians and claims no change.

**The spinning change** (CEmbedding). If C ≤ 0.9 × A **and** D ≤ 0.9 × B, the record may say
that the embedding server's spinning after each call cost recall time on this machine,
giving both pairs. The same 1.05 bound applies the other way, and otherwise no change is
claimed. Whether CEmbedding ships the setting off by default is decided on this evidence.

**What recall takes** with both changes: arm D read with the table of the first
registration, unchanged:

| Outcome | What may be written |
| --- | --- |
| median < 1,000 ms **and** every sample < 1,000 ms | "Recall over 100,000 memories of real length (about 56 million tokens in all) completes in under one second on an N100-class PC, including embedding the query." The median may be given in seconds to two decimals |
| median < 1,000 ms, some sample ≥ 1,000 ms | The median, stated as a median, with the maximum beside it |
| median ≥ 1,000 ms | No sub-second statement. The measured median and maximum are stated as they are |

Any statement made from this run names the machine state, the trees and the setting.

An arm is invalid, and is repeated once rather than read, under the conditions of the first
registration (the load not falling below 1.0 within 15 minutes, a real-client sample
erroring or returning no vector, an output width other than 768, a recall returning no
rows, a corpus not holding exactly 100,000 memories), and also if the virtual machine is
found running at the start or the end of the arm.

## What this does not measure

As in the first registration: recall quality, Japanese text, episodes, concurrent requests,
a cold start of the embedding server, and any other machine. The responses of the two trees
are compared on the laptop (identical for the 50 questions); this run times them and does
not compare them again. It does not measure released versions of either change.
