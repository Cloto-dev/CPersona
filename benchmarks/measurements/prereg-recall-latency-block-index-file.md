# Registration: the block arm reading its rows from the block index file

Registered 2026-10-08, before any run it governs. The commit that adds this file is pushed
before the first measurement on the reference machine.

## Why this exists

`2.6.7a2` added a block index file (`CPERSONA_BLOCK_INDEX`, off by default;
[BLOCK_CANDIDATES_CONTRACT.md](../../docs/BLOCK_CANDIDATES_CONTRACT.md) §3.4). With it on, the
block arm reads the rows it examines from a file beside the database instead of from SQLite,
and reads only the records changed since the file was built from SQLite, through a change log
that four triggers on `record_blocks` keep. The file is meant to return the rows the SQLite
read returns, in the same order. Whether it should be on by default in `2.6.7` depends on
three things nobody has measured on the reference machine: what it does to recall time, how
the reads of changed rows grow before the server gives the file up, and what the change log
costs a block write.

What is known, none of it registered:

- On the reference machine, `v2.6.6`, stand-in query vectors, 25 questions: the block arm took
  654 ms of a 1,034 ms recall, 468 ms of it reading the examined rows. The keyword arm, which
  runs beside it, took 1,032 ms and decided the total.
- On the reference machine, a prototype read the 250,000 examined rows from SQLite in 375 and
  360 ms (1,384 ms the first, cold time) and in a median of 6.1 ms from a contiguous file.
- On an Apple laptop, this branch, 5 questions with stand-in vectors: the block arm took a
  median of 153 ms with the file off and 43 ms with it on; with 9,982 and 33,948 block rows
  marked changed since the file was built, 36 and 69 ms against 138 and 136 ms off. Every
  recall returned the same rows in the same order in both arms. The file for this corpus was
  431,528,748 bytes and took 16.9 s to build.
- On the same laptop, rewriting the newest 100 memories' blocks through the server's write
  (eight runs per condition, interleaved): a median of 0.39 ms per record with the change log's triggers
  absent, 1.09 ms with them present and the log off, and 1.35 ms with the log on. The
  triggers exist in every database at schema 19, so the second figure is what a default
  deployment of `2.6.7` pays, and is the reason this registration measures it.

## What is measured

Everything not named here is as in
[the first registration](prereg-recall-latency-realistic-corpus.md): the machine, the
configuration (`rrf`), the queries, the settling and the driver.

- **Corpus**: the same database, verified by SHA-256
  (`b5536681b139c60157a80447e386a05c794c59e8a85092a6a6cd980e9eae2cc2`) on the reference
  machine before the run. Each arm works on its own copy and migrates it to schema 19.
- **CPersona**: one tree for every arm, this branch at the commit that adds this file (the
  code of `master` at `3c28370`, the driver changes on this branch and this file). The arms
  differ only in `CPERSONA_BLOCK_INDEX` and the driver's `--block-index`, which builds the
  file before the warm-up; the driver refuses to run when the two disagree.
- **Embedding server**: CEmbedding `v0.9.1` (`f4c92e8`), `onnx_jina_v5_nano`, default
  threads, `ONNX_ALLOW_SPINNING` unset. One `/embed` call before the first arm is checked to
  leave the server at no more than 0.1 CPU-seconds in the second after it.
- **Machine state**: the 8 GB virtual machine is shut down before the first arm and started
  again after the last run; the containers stay running as found. `MemAvailable` is recorded
  immediately before each arm.
- **Stand-in vectors** are derived from Python's string hash, so every arm runs with
  `PYTHONHASHSEED=0`, which makes an arm's stand-in recalls comparable with another's.
- **What the driver records besides time**: each recall's returned refs in order, the time
  of its block arm (the one call to `blocks.search` a recall makes, timed around the call),
  and where each block-arm read was served from and why (`file`, or `sqlite` with the reason
  the server gives). The wrappers that record these run in every arm.

### Four recall arms, in this order

| Arm | Block index file |
| --- | --- |
| A1 | off |
| B1 | on |
| A2 | off |
| B2 | on |

Each arm is one run of the driver with `--touch-rows 10000,34000`. After the registered
loop, the driver marks block rows changed, newest memories first, until 10,000 rows are
marked without exceeding that total, and recalls the 25 timed questions again with the
stand-in vector; then marks more, to 34,000 in all, and recalls them once more. A row is
marked by rewriting one of its columns to its own value, which leaves the rows as they were
and, with the file on, adds one change-log row per row. The arms with the file off rewrite
the same rows. The server gives the file up when more than 35,000 rows of changed records
would be read (`LIVE_ROW_BOUND`), so 34,000 is the last point before it does.

### One write run, after the recall arms

[perf_block_index_writes.py](perf_block_index_writes.py) on its own copy of the corpus,
`--records 300`, the conditions in the order `T0 T1 T2 T2 T1 T0` four times (24 runs, eight
per condition):

| Condition | The change log's four triggers | The log |
| --- | --- | --- |
| T0 | absent (the schema before 19, for this table) | — |
| T1 | present | off (a default deployment) |
| T2 | present | on (a deployment with the file on) |

Each run rewrites the blocks of the newest 300 memories that can be rewritten as stored,
through `blocks.write_blocks` inside one `transaction()` per record, as the queue's block
build does; the timed span is that transaction. Write-ahead log frames are counted per run
with automatic checkpoints off. Then the first 30 of those records are divided and embedded
again through the embedding server (`blocks.prepare_blocks`, the part of a block build that
runs outside the write lock), timed per record.

## The rules, fixed now

**Recall time.** The figure read is the median of an arm's 25 real-client `do_recall`
samples. If B1 ≤ 0.95 × A1 **and** B2 ≤ 0.95 × A2, the record may say that turning the block
index file on lowered the median recall time on this machine, giving both pairs. If
B1 > 1.05 × A1 **or** B2 > 1.05 × A2, the record says it made recall slower here. Otherwise
the record states the medians and claims no change. Arms B1 and B2 are also read with the
table of the first registration (the sub-second statement); a row's statement is made only if
both arms meet it.

**The same answer.** Every stand-in recall — the 25 of the registered loop and the 25 of each
phase — must return the same refs in the same order in all four arms. If any differs, the
record says that the file changed an answer, no figure from this run is stated as a gain, and
the file stays off by default. The real-client recalls are compared the same way and any
difference is reported; it is not judged, because it can come from the embedding server.

**The block arm.** The figure read is the median block-arm time over an arm's 25 stand-in
recalls of the registered loop. If B ≤ 0.95 × A in both pairs, the record may say that the
file lowered the block arm's time, giving both pairs.

**How many changed rows the file may carry** (`LIVE_ROW_BOUND`). The figure read is the
median block-arm time over a phase's 25 stand-in recalls, compared between the arms of a pair
at the same phase. If B ≤ 0.8 × A at 34,000 rows in both pairs, the bound stays at 35,000.
Otherwise, if B ≤ 0.8 × A at 10,000 rows in both pairs, the bound is lowered to 10,000 before
`2.6.7` is final. Otherwise the record says that reading the changed rows had taken the file's
margin by 10,000 rows, and the bound is decided separately; no untested value is chosen from
this run.

**What the change log costs a write.** The figures read are the medians of the 2,400
per-record samples of each condition (300 records × 8 runs), M0, M1 and M2, and P, the
median of the 30 timed divisions and embeddings. For the log off, D1 = M1 − M0: if
D1 ≤ 0.01 × (P + M0), the record may say that with the file off, the change log's triggers
add less than 1% to a record's block build on this machine; otherwise it states the share
D1 / (P + M0) and recommends creating the triggers only while the file is on, before `2.6.7`
is final. For the log on, D2 = M2 − M0, read the same way with "with the file on". Both
statements give M0, M1, M2 and P. The write-ahead log frames per run, by condition, and the
change log's size per row are stated as measured, without a threshold.

**The default.** The record recommends turning the file on by default in `2.6.7` only if the
recall-time rule says it lowered the median, the same-answer rule found no difference, and the
log-on write statement is the less-than-1% one. Otherwise it recommends keeping the file off
by default, as an opt-in for large stores. Experience with the setting on in a deployment is
judged separately and can only keep it off.

**Invalid runs.** A recall arm is invalid, and is repeated once rather than read, under the
conditions of the first registration (the load not falling below 1.0 within 15 minutes, a
real-client sample erroring or returning no vector, an output width other than 768, a recall
returning no rows, a corpus not holding exactly 100,000 memories), if the virtual machine is
found running at the start or the end of the arm, and also if, in an arm with the file on, the
build is declined, the file is not current before the warm-up, or any block-arm read of the
registered loop or the phases is served from SQLite; or if, in an arm with the file off, any
read is served from anywhere but SQLite with the reason `off`. In the write run, a run whose
truncating checkpoint was busy is not read for its frames, and if fewer than 30 of the 30
divisions match the stored ones, P is stated with that count and the 1% statements are not
made.

## What this does not measure

As in the first registration: recall quality, Japanese text, episodes, concurrent requests, a
cold start of the embedding server, and any other machine. The corpus holds 34.8 blocks per
memory, which is heavier on the block arm than a store of Japanese memories of the same
length. It does not measure the `rsf` fusion, a build running while recalls are served, writes
arriving during a recall, the queue deciding when to rebuild, or how long a deployment can run
before the file falls behind. The time each build took is recorded in the arm's record and
stated, without a rule.
