# Block Candidate Generation — contract

Status: the contract and its conformance data are on `master`, and so is a
second implementation of the read: from a file beside the database, when
`CPERSONA_BLOCK_INDEX` is on (the default from 2.6.7). Recall's answers do not change.

## 0. What this is

The block arm ([Block reach](BLOCK_REACH_DESIGN.md)) searches clause-sized
blocks of every record by Hamming distance on one bit per dimension. Its search
has two halves:

1. **Candidate generation.** Read the block rows a call may look at, measure
   each one's Hamming distance to the query, and keep the nearest few in a
   written-down order.
2. **Re-rank.** Read the stored vectors of those few rows and order them by
   cosine.

The first half returns integers and identifiers and nothing else, so a faster
implementation of it can be held to returning exactly the same rows in exactly
the same order as the one recall runs today. This page states that half as a
contract, records how an implementation is checked against it, and records the
decision on what the faster implementation is.

**Why it needs a faster implementation.** Today the first half reads its rows
out of SQLite on every call. Over 100,000 memories of real length the store
holds 3,480,069 blocks, 34.8 per record
([the corpus](https://github.com/Cloto-dev/CPersona/blob/master/benchmarks/measurements/results-recall-latency-realistic-corpus.md)),
and on an Intel N150 that read alone takes 360 to 375 ms (section 3.1).

## 1. The contract

**Input.**

- **Rows**, in key order: kind (byte order), then parent id, then block index.
  Each row carries its kind, parent id and block index; its record's agent id,
  project id and channel; the label of the model that produced its bits; and
  the bits, or nothing when the row has none yet.
- **The query**: its bits; an agent id; a project id, which is either absent,
  empty, or a name; a channel, which is either empty or a name; and two model
  labels.
- **Three caps**: how many rows may be examined, how many of them one record
  may take, and how many rows are returned (the depth). None of the three is
  derived from how many results the caller asked to receive.

**Procedure.** This is normative; an implementation that differs in any step
returns different rows.

1. **Admit.** A row is admitted when it has bits, its label equals one of the
   two model labels, its agent id equals the query's (an empty agent id is the
   bucket of rows owned by no named agent, not a wildcard), and the two axes
   below allow it. A row that is not admitted spends neither cap.
   - **Project.** Absent reads every project. Empty reads the global pool alone:
     rows whose project id is empty. A name reads that project and the global
     pool.
   - **Channel.** Empty reads every channel. A name reads that channel and the
     channel-global rows: rows whose channel is empty.
2. **Examine.** Walk the admitted rows in key order. A record's share is its
   first rows in block order, up to the per-record cap; its later rows are
   skipped. Stop when the examined cap is full. The share is counted over
   admitted rows, so a row without bits or under another label does not use
   up a record's share.
3. **Measure.** An examined row whose bits are a different length from the
   query's has no distance: it spent its share and a place under the examined
   cap, and it is not ranked. A different width is a different dimension, and
   a distance between the two would be a number without meaning. Every other
   examined row gets its Hamming distance to the query.
4. **Order.** Sort the measured rows by distance, then kind, then parent id,
   then block index, ascending. The key is unique, so no two rows tie. Return
   the first rows up to the depth.

**Output.** For each row returned: kind, parent id, block index and distance.
No text, no metadata, no floating-point score.

**What stays as it is.** The re-rank by stored vectors, and what follows it, are
not part of this contract. When a returned row has no stored vector yet (a
deployment part-way through building them), the arm answers by its own path, as
it does today.

## 2. Conformance

**The golden.** `tests/golden/block_candidates.json` holds cases: rows in key
order, a query, the caps, and the rows expected back. It is written by
`scripts/capture-block-candidates.py`, which runs the functions recall runs
(`blocks._examined`, `blocks._measured` and `blocks._order`) over a real
database. Nobody writes an expected row by hand. The rows are inserted out of
order, so the answer depends on the table's key and not on arrival order.

**What the cases cover.** The width of a 768-dimension model with recall's own
caps; a depth cut that falls inside a run of equal distances, so only the
written-down order decides; each cap binding, and both at once; rows the filter
refuses inside a record (no bits, another model's label) beside rows of other
widths, narrower and wider; every reading of the three axes; fewer measured
rows than the depth; and empty answers.

**What holds it.** `tests/test_block_candidates_golden.py` fails when the file
no longer says what the code returns, and when a case stops exercising a rule
this page names (for example, when no case's cut falls inside a tie any more).
Breaking the server's code (the tie order, the per-record cap by one, admitting
rows without bits, loosening the width check) makes the first test fail. An
implementation that reads the rows another way is held to the same file: it
passes when it returns every case's rows in order.

## 3. Decisions

### 3.1 The rows are read from a contiguous file, not SQLite

The faster implementation reads the block bits from a contiguous file the
server writes, not from the SQLite database.

Measured on the 100,000-memory corpus (exploratory, not registered): the
250,000 rows recall examines, read by the server's own examined read, against
the same rows read from a file. The Intel N150 had an 8 GB virtual machine
running beside the measurement.

| Read of the 250,000 examined rows | Apple M-series laptop | Intel N150 |
| --- | ---: | ---: |
| SQLite, as recall reads them today | 127–140 ms (762 ms cold) | 360–375 ms (1,384 ms cold) |
| A contiguous file | 1.2 ms | 6.1 ms |

The file follows the coarse index of records
([Binary coarse search §3](BINARY_COARSE_SEARCH_DESIGN.md#3-the-coarse-index)):
written by the server, validated when it is opened, never repaired, safe to
delete. Unlike that index it is not read up to a watermark. The key starts with
the kind, so a block written after the build does not sort after every row in
the file, and block rows are deleted and moved as well as added, which a
watermark does not see. The file is read through a log of the records whose
blocks changed since the build instead (section 3.4).

### 3.2 The distances are taken in the server's process, with a SIMD library

The Hamming distances and the nearest rows are computed in the Python process,
by a library that uses the CPU's vector instructions, not by a separate process
in another language.

Measured on the same rows (exploratory, not registered), the median over 25
queries whose bits are blocks of the corpus. Every implementation returned the
same rows in the same order as the server's code for all 25 queries.

| Hamming distance and the nearest 200 rows | Apple M-series laptop | Intel N150 |
| --- | ---: | ---: |
| The server's code today, over row tuples | 34.0 ms | 169.7 ms |
| The same lookup table, over an array | 23.1 ms | 96.3 ms |
| NumPy `bitwise_count` | 4.0 ms | 13.1 ms |
| FAISS `IndexBinaryFlat`, one thread | 1.1 ms | 6.6 ms |
| SimSIMD | 1.9 ms | 4.9 ms |
| Go, as a separate program over the same file | 1.3 ms | 3.6 ms |

A separate process in Go was the plan before this measurement. It saves about
1 ms over a SIMD library on the N150 at today's caps, and it would have brought
a second kind of release artifact, a binary per platform beside the PyPI
package. Almost all of the gain comes from not reading SQLite (section 3.1).
Which library is used, and whether it is required or optional, is decided with
the implementation.

### 3.3 On by default, answers unchanged

The file reader is on unless `CPERSONA_BLOCK_INDEX=false`. It was opt-in when
2.6.7a2 introduced it, and was turned on by default after a registered run over
100,000 memories on the reference machine found recall faster, every answer
unchanged and the change log adding under 1% to a block build
([results](https://github.com/Cloto-dev/CPersona/blob/master/benchmarks/measurements/results-recall-latency-block-index-file.md)). Any state in
which the file cannot answer exactly sends the call to the SQLite read (section
3.4). It is held to returning the same rows in the same order as that read,
rather than to a bound on how much worse its answers may be: by this contract's
golden, read through the file; by random writes after a build (new records,
records rebuilt, deleted, retagged, moved to another key, and rows of another
width), compared with the SQLite read row for row under caps that bind and do
not, with the file read in pieces small enough that records run across their
edges; and by hand-made mutations of the rules in section 3.4, each of which
turns those tests red. Two rules are not mutated, because a test in one process
cannot watch them fail: that the read is one statement, and that pruning is one
transaction.

The distances follow section 3.2 without a new dependency: NumPy's
`bitwise_count` where NumPy is 2.0 or later, and the existing lookup table
otherwise. Both give the same numbers, and a test holds them together.

### 3.4 How the file stays exact

The file holds every block row with bits, in key order, as it was at the build.
A recall reads the rows of the records whose blocks changed since then from
SQLite, takes every other record's rows from the file, merges the two in key
order, and only then applies both caps. A record either changed or did not, so
the two parts never hold the same record, and the merged sequence is the one
the SQLite read produces as long as the set of changed records is complete. The
rest of this section is what keeps it complete, and what the reader refuses.

- **The log.** Triggers on the block table write one row per row event: the
  record a row was inserted into, deleted from or updated in, and on an update
  that moves a row to another record, that record too. Triggers rather than
  calls in the writers, so a write from an older build or the SQLite shell is
  logged as well.
- **Its numbers.** Each log row takes the next number from a clock of the
  server's own (`block_log_clock.head`), not from `AUTOINCREMENT`, whose
  bookkeeping table may be edited or cleared and would then hand out a number
  the file already claims to include.
- **Only while a file is in use.** The triggers write nothing unless the log is
  on. A build turns it on, and a server started with the file off turns it off.
  With the log off the triggers still check, for each block row written, whether
  it is on: on the reference machine that added 0.37 ms to rewriting a record's
  blocks, against 0.61 ms with the log on, each under 0.02% of a block build
  ([results](https://github.com/Cloto-dev/CPersona/blob/master/benchmarks/measurements/results-recall-latency-block-index-file.md)). Each time the log is turned on its generation rises, and a
  file from before a gap is refused.
- **The build.** It turns the log on, then reads the clock, the schema version
  and every row in one snapshot, and refuses to write a file unless every
  logging trigger exists as the schema defines it: a check made later only
  notices a change after the build, not a build that began without complete
  logging. A value the file cannot hold (a kind other than `ep` or `mem`, a key
  that is not an integer, a block index outside 32 bits, bits that are not a
  blob) declines the build.
- **Pruning.** After a build, the log rows the file now holds are deleted and a
  mark (`pruned_through`) is raised past them, in one transaction, and the mark
  only rises: a slower build that finishes second does not lower it.
- **The read.** One statement returns the changed records' current rows, the
  clock and the schema version, so they come from one state of the database.
  The admission filter sits in the join, so a changed record with no rows left
  that the filter admits still comes back, and its rows in the file are still
  dropped.
- **What is refused.** No file, a damaged one, or another width; the log off,
  or a generation other than the file's; a file newer than the snapshot (its
  `built_seq` above the clock); changes it would need already pruned; a schema
  change since the build (a trigger dropped and recreated could have missed
  writes); more than 35,000 rows of changed records, a bound set well below the
  250,000 rows the SQLite read examines; and a changed row the file could not
  hold. Each sends the call to the SQLite read.
- **Rows of another width.** A row whose bits are not the file's width is in
  the file with its bits zeroed and a mark that it cannot be measured. It keeps
  its place in both caps, as it does in the SQLite read, and is never measured.

The file is rebuilt on the server's queue: at startup, and after the queue
writes blocks, when the file is absent, unusable or too far behind the log. A
recall never queues it. `python -m cpersona.block_index build` builds it by
hand, and `status` reports whether a recall can read it now.

Not defended, and documented here instead: an `INSERT OR REPLACE` from another
program that replaces a row of a different record by its rowid while SQLite's
recursive triggers are off (SQLite runs no delete trigger for it), a temporary
trigger in another connection that stops the logging triggers, and editing the
clock or the log by hand.

## 4. Not in this step

- **The time a recall saves.** The read above is measured on the 100,000-memory
  corpus before a figure is claimed for recall as a whole; the keyword arm
  below stays the slowest part either way.
- **Lifting the examined cap.** Over 100,000 memories the cap (250,000 rows)
  already binds, and one call looks at about 7% of the blocks, the first ones in
  key order. Lifting it would change which records the arm can reach, so it is
  a change to answers, measured on its own.
- **The coarse search of records.** The same mechanism over another
  population.
- **The re-rank.** Not part of this contract.
- **The keyword arm.** On the N150, recall's slowest arm is the keyword arm:
  the full-text ranking inside SQLite, which this step does not touch.
