# Block Candidate Generation — contract

Status: the contract, its conformance data, and a Go implementation checked
against that data are on `master`. Nothing in the server calls the Go
implementation yet, and no release ships it. Recall's answers do not change.

## 0. What this is

The block arm ([Block reach](BLOCK_REACH_DESIGN.md)) searches clause-sized
blocks of every record by Hamming distance on one bit per dimension. Its search
has two halves:

1. **Candidate generation.** Read the block rows a call may look at, measure
   each one's Hamming distance to the query, and keep the nearest few in a
   written-down order.
2. **Re-rank.** Read the stored vectors of those few rows and order them by
   cosine.

The first half returns integers and identifiers and nothing else, so an
implementation in another language can be held to returning exactly the same
rows in exactly the same order. This page states that half as a contract,
records how an implementation of it is checked, and records where such an
implementation runs and how it is shipped.

**Why the block population comes first.** Over 100,000 memories of real length
the store holds 3,480,069 blocks, 34.8 per record
([the corpus](https://github.com/Cloto-dev/CPersona/blob/master/benchmarks/measurements/results-recall-latency-realistic-corpus.md)).
Blocks therefore reach a million rows at about 29,000 records, long before
records themselves do. Candidate generation is not what decides recall's time
today; the keyword arm is (section 4). The reason for this step is scale.

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

**What stays in the server.** The re-rank by stored vectors stays in the Python
server, and so does what follows it. A cosine summed in another order is a
different float, and recall's order depends on those floats, so the re-rank
cannot be held to the server's answer exactly in another language. When a
returned row has no stored vector yet (a deployment part-way through building
them), the server answers that call by its own path, as it does today.

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

**Two tests on the Python side.** `tests/test_block_candidates_golden.py` fails
when the file no longer says what the code returns, and when a case stops
exercising a rule this page names (for example, when no case's cut falls inside
a tie any more). Breaking the server's code (the tie order, the per-record cap
by one, admitting rows without bits, loosening the width check) makes the
first test fail.

**The Go side.** `go/blockcand` reads the same file:

- its implementation returns every case's rows in order;
- an implementation that returns nothing fails exactly the cases that expect
  rows, so the verifier reports a missing answer;
- each of a set of deliberately broken implementations (each cap off by one in
  either direction, either tie order changed, rows without bits admitted, rows
  of another width refused, labels or agents ignored, each axis read too
  widely or too narrowly, the share counted over every row) fails at least one
  case.

A golden regenerated from a broken server makes the Go test fail, so a change
to the server's candidate generation cannot reach the golden without every
other implementation having to follow it.

## 3. Decisions

### 3.1 A separate process, not a library in the server

The Go implementation will run as a sidecar process the server asks over a
local socket, not as a shared library loaded into the Python process.

- The PyPI package stays pure Python, with no platform-specific wheels to build
  and keep building.
- The server already works this way with the embedding server, and an absent
  derived index already has a path: the server answers from its own store. An
  absent or failing sidecar takes the same path.
- If the server itself later moves to Go, the boundary merges into one process
  and nothing written for it is thrown away.

The cost is one local round trip per call.

### 3.2 It reads a contiguous file of block bits, not SQLite

The sidecar will read the block bits from a contiguous file the server writes,
not from the SQLite database.

On an Intel N150 with 100,000 memories of real length, an unregistered
breakdown with stand-in query vectors put the block arm at 654 ms: 468 ms
reading the examined rows out of SQLite, 128 ms measuring them, 23 ms reading
the re-rank vectors. An implementation that reads the same rows from SQLite in
another language keeps most of that cost. The file follows the coarse index of
records ([Binary coarse search §3](BINARY_COARSE_SEARCH_DESIGN.md#3-the-coarse-index)):
written by the server, validated when it is opened, with a watermark past which
rows are read from the store.

One difference from records has to be designed for. The key starts with the
kind, so a block written after the file was built does not always sort after
every row in it: an episode's block sorts before every memory's. The rows past
the watermark have to be merged into key order before either cap applies,
because the caps truncate in key order.

**What it buys, stated plainly.** Read through numpy, the same file removes
most of the SQLite cost without Go. At today's caps, the Go reader's own gain is
small; it is a gain at scale. The examined cap (250,000 rows) already binds
over 100,000 memories, where one call looks at about 7% of the blocks, the first
ones in key order. Lifting that cap would change which records the arm can
reach, and is a separate decision measured on answer quality, not part of this
contract.

### 3.3 No cgo

The module builds with `CGO_ENABLED=0`, so one machine can build every target:
linux/amd64 (with `GOAMD64=v2`), linux/arm64, darwin/arm64 and windows/amd64.
This is also why the sidecar does not read SQLite through a C driver.

### 3.4 Shipping

- The Go module lives in this repository, under `go/`. A change to the
  contract, the golden and both implementations lands in one pull request,
  rather than being kept in step across repositories.
- Binaries will be attached to GitHub Releases.
- The PyPI package stays pure Python and never requires the binary. Without it,
  recall answers as it does today.
- A separate package that carries the binary through `pip` may follow if
  deployments ask for one.

### 3.5 Off by default, answers unchanged

When the sidecar ships it is opted into. An absent sidecar, a damaged file or a
different dimension sends the call to the server's own path. It is held to
returning the same rows in the same order as that path, by this contract and
its golden, rather than to a bound on how much worse its answers may be.

## 4. Not in this step

- **The file format, the sidecar's protocol and its fallback, and the release
  binaries.** The next step.
- **Lifting the examined cap.** A change to answers; measured on its own.
- **The coarse search of records.** The same mechanism over another
  population, when the record count makes it worth it.
- **The re-rank.** Not planned, for the reason in section 1.
- **The keyword arm.** In the same breakdown, recall's slowest arm is the
  keyword arm: the full-text ranking inside SQLite, which this step does not
  touch.
