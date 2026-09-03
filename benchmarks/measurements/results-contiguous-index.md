# Contiguous index on the production recall path — measurement record

`perf_contiguous_index.py` alongside this file re-derives the before/after
numbers. The segment profile and the two falsified hypotheses below were
produced by ad-hoc scripts against the same database, and are reproduced in full
so a later reader can re-run them.

## Regime

- Reference machine: Intel N150, 4 threads, `governor=performance`, idle
  (load 0.08). numpy 2.4.6, Python 3.11.2 — numpy matched to the machine the
  earlier breakdown used.
- The real path: `aiosqlite`, `do_recall`, FTS on. The earlier breakdown used
  sync `sqlite3` and said so; this is the measurement that settles the absolute
  numbers it declined to claim.
- 1024-dimensional embeddings (production width), synthetic corpus, response
  limit 10, median of 25 queries (15 at 100k), both arms warmed separately.

## The prediction, and the result

Predicted before running: ~7.0x on the vector arm, from the earlier finding that
SQLite row materialisation is 72.9% of the scan and `b"".join` another 24.4%.

| Corpus / window | vector arm | `do_recall` | non-vector part |
| --- | --- | --- | --- |
| 10,000 rows / window 10,000 | 70.2 → **33.2 ms** (2.11x) | 92.3 → 54.6 ms (1.69x) | 22.1 → 21.4 ms |
| 100,000 rows / window 100,000 | 611.0 → **469.1 ms** (1.30x) | 872.4 → 710.3 ms (1.23x) | 261.4 → 241.3 ms |

**2.11x where 7x was predicted, and worse at the larger corpus** — the opposite
of what the model says, since row materialisation should dominate more as the
corpus grows. The shape is what said the implementation was wrong rather than
the model.

## Where the time actually goes

Segment profile of the index path, 100,000 rows, median of 12 queries:

| Segment | Median | Share |
| --- | ---: | ---: |
| `merge_index_and_tail` (the embedding gather) | **440.2 ms** | **88%** |
| matmul | 24.1 ms | 5% |
| tail SQL read | 12.5 ms | 3% |
| axis selection | 0.4 ms | <1% |
| mapping the index file | 0.0 ms | — |
| `_search_vector` total | 497.7 ms | |

The index removed the SQLite row materialisation and replaced it with a
**scattered fancy-index copy of the same bytes** — 100,000 × 1024 × 4 B = 410 MB
pulled out of the memory-mapped file one row at a time. The 7.0x figure came
from `np.fromfile`, a *sequential* read into one array; this implementation
gathers even when the selection is the whole file in order.

Isolated on the same machine, on a resident array (no page faults):

| Operation, 100,000 × 1024 | Median |
| --- | ---: |
| `A[positions] @ q` (what the code does) | 123.7 ms |
| `A @ q` over a contiguous view | **26.0 ms** |
| `A[scattered_50%] @ q` | 65.6 ms |
| `(A @ q)[scattered_50%]` | 23.9 ms |

The gap between 123.7 ms resident and 440.2 ms in the real path is the mapped
file: a scattered gather faults pages in a scattered order, while a matmul over
a view streams them.

## What the fix can and cannot be

Bit-identity, measured on the same machine and numpy build:

| Selection | `A[pos] @ q` vs `(A @ q)[pos]` | vs a contiguous view |
| --- | --- | --- |
| All rows | **identical** | **identical** |
| Contiguous prefix (60%) | **identical** | **identical** |
| Scattered 50% | **differs** | — |
| Scattered 5% | **differs** | — |

So "multiply everything, then take the scores" — 23.9 ms against 65.6 — is
**not available** for a scattered selection: it changes the summation order and
would spend the exactness the whole design rests on. For a contiguous run it is
the same bytes in the same order with the same row count, and it is identical.

The actionable shape: when the selected positions form a contiguous run and the
tail is empty, hand the slice straight to the matmul with no copy. That is the
common case — one agent, no axis narrowing — and it is 4.8x on the term that is
88% of the time.

## Two hypotheses this record keeps because they were wrong

**1. "The tail read walks the whole corpus."** The query plan says so:
`SEARCH memories USING INDEX idx_memories_agent (agent_id=?)` for a range that
matches nothing. It looked like the O(corpus) materialisation re-introduced
inside the code that removes it. Timed on the real database, the tail read is
**12.5 ms of 497.7** — SQLite evaluates `id > watermark` from the index entry's
rowid and never reads the rows. A plan that names a full index walk is not
evidence of a full row read.

**2. "`ORDER BY +created_at` fixes it."** On a 5,000-row corpus that spelling
produced exactly the wanted plan (`SEARCH ... USING INTEGER PRIMARY KEY
(rowid>?)`). On the real 100,000-row database it produces
`SEARCH ... USING INDEX idx_memories_isolation | USE TEMP B-TREE FOR ORDER BY`
instead — a different index and an added sort. **The plan is data-dependent, and
a plan read off a toy corpus does not describe production.** Measured in
isolation it is still an improvement (10.5 ms against 31.0), but the end-to-end
run showed it as a *regression*, because a 20 ms effect cannot be resolved under
a 440 ms term that varies with page-cache state. The change was reverted rather
than shipped with a rationale its own machine contradicts.

## What this says about the next tier

The question this measurement exists to answer is where the bottleneck moves
when the vector arm stops being it. It has not moved yet: **the vector arm is
still 55–74% of `do_recall`**, because the arm did not get the win the design
predicted. The non-vector part — the lexical arms, fusion, scoring and hydrate —
sits at 21 ms (10k) and 241 ms (100k) and was unchanged by the index, as
expected.

So the honest reading was that **the next tier could not be chosen from this
run.** The contiguous-view fix had to land first; the section below is that
measurement.

## The contiguous view, measured

Same machine, same script, same regime (1024 dims, response limit 10, median of
25 queries at 10k and 15 at 100k, both arms warmed). `_merge_index_and_tail`
now hands `index.embeddings[a:a+n]` to the matmul as a view — no copy — when the
selection is all-index and one contiguous run, and gathers exactly as before
otherwise. Before/after here is the code, not the index: each row is the
index-served arm, with the SQL scan of the same run beside it for scale.

| Corpus / window | scan | index, gather | index, view | view vs gather | view vs scan |
| --- | ---: | ---: | ---: | ---: | ---: |
| 10,000 / 10,000 | 72.4 ms | 44.1 ms | **23.1 ms** | 1.91x | 3.14x |
| 100,000 / 10,000 | 69.7 ms | 55.4 ms | **34.1 ms** | 1.62x | 2.04x |
| 100,000 / 100,000 | 609.1 ms | 541.3 ms | **211.8 ms** | 2.56x | 2.88x |

`do_recall` at 100,000 / 100,000: 788 → **446 ms** (1.77x on the whole path);
at 10,000 / 10,000: 66 → **45 ms**. The non-vector part is unchanged in every
row (22 ms / 220 ms / 235–247 ms), as it must be.

The scan column moved between the two runs (the earlier table has 611 → 469 for
the last row; today the same master code measured 611 → 541), so the gather
column is re-measured here rather than copied. The *shape* is the same either
way: the gather barely beat the scan at the large window, and the view is
where the index starts to pay.

### Still not 7x, and where the rest now goes

Segment profile of the index-served arm at 100,000 / 100,000 with the view in
place, median of 12 queries:

| Segment | Median | Share |
| --- | ---: | ---: |
| `_search_vector` total | 252.5 ms | |
| `_index_phase1` | 215.5 ms | 85% |
| ├ `_merge_index_and_tail` (the interleave loop; the view itself is free) | 79.0 ms | 31% |
| │ └ `_is_ascending_run` | 7.1 ms | 3% |
| ├ `_index_rows_lost_embedding` (the `IN (…)` probe over the selection) | 72.5 ms | 29% |
| ├ `_index_tail_rows` | 13.5 ms | 5% |
| ├ `select` | 0.2 ms | <1% |
| └ unattributed inside phase 1 (the `int(index.ids[p])` list, mapping) | ~50 ms | ~20% |
| `_cosine_matrix` (the matmul) | 24.0 ms | 10% |
| outside phase 1 (embed, top-k, hydrate) | ~37 ms | ~15% |

The gather term is gone — the matmul over the view is 24 ms, which is the
floor this arm can reach on this machine. What remains is **Python-level O(n)
work over the selection**: the interleave loop walks 100,000 positions one
tuple comparison at a time even when there is no tail to interleave, the
lost-embedding probe binds 100,000 parameters into one `IN` clause, and the
ids for that probe are built one numpy scalar at a time. Together they are
~200 ms of the 212. None of them touches the bytes the design is about, and
none was visible while the gather was 440 ms.

The 7x prediction was for the *bytes* — SQLite materialisation versus a
sequential read — and on that term the view delivers it: 440 ms of gather
became a 24 ms matmul (18x). The arm as a whole is 2.9x over the scan because
the arm was never only bytes. Each of the three remaining terms has an obvious
shape (an all-index selection needs no interleave; a probe over a contiguous
id run needs no parameter list), but each changes what a function promises,
not just how fast it runs, so they are recorded here and left for their own
change.

**One of them is also a correctness bound, not just a cost.** The probe's
parameter count equals the selection size, and SQLite's default
`SQLITE_MAX_VARIABLE_NUMBER` is 32,766. This machine's build allows 250,000,
which is why a 100,000-row window ran at all here; a build with the default
limit — the macOS Python that produced the earlier measurements is one — raises
`too many SQL variables` from that probe at any window above 32,766 once an
index exists. That is filed separately; it is not a property of the view.

### The probe, bounded by shape

Re-profiled at 100,000 / 100,000 on the same machine after the probe was
bounded by the selection's shape (a contiguous run of ids is asked with
`BETWEEN` and two parameters; a scattered selection in chunks of 500), median
of 12 queries:

| Segment | Before | After |
| --- | ---: | ---: |
| `_index_rows_lost_embedding` | 72.5 ms | 36.5 ms |

The other segments moved between runs by more than this change touches
(`_merge_index_and_tail` 69–93 ms across three runs on the same tree), so
only the probe's row is updated; the rest of the table above stands as the
shape of the arm, not as figures to subtract from.

What is left in the 36 ms is not the parameter list. Timed as a bare
statement on the corpus the profile built, the range form takes 25 ms and
its plan is `SEARCH memories USING INTEGER PRIMARY KEY (rowid>? AND
rowid<?)` — SQLite walks the 100,000 rows in the range to test
`embedding IS NULL` on each, and at 4 KB a row that is a 400 MB pass through
the page cache. `SELECT COUNT(*)` over the same range costs the same 18 ms
for the same reason. A partial index (`ON memories(id) WHERE embedding IS
NULL`) answers the same statement in 0.005 ms, because the rows it has to
visit are exactly the ones that lost their vector, normally none; it is a
schema change and is recorded here for the decision, not made.

Two measurements that shaped the statement rather than the design:

- The agent predicate the isolation gate asks every agent-scoped read to
  carry made the planner choose the isolation index and walk every row of
  the agent per statement: 43 ms for the range form, and 3.0 s for the
  chunked form because each of the 200 chunks repeated the walk. Written as
  `+agent_id = ?` the term no longer constrains an index, the id term stays
  the access path, and the chunked form over the whole corpus is 41 ms.
- The chunked form is not the slow path it looks like: 200 statements of 500
  rowid lookups each cost 41 ms end to end, against 25 ms for the range —
  the bytes SQLite reads are the same, and the parameter binding is not
  where the time goes.

### The walk, removed for the shape that never needed it

Same machine, same script, same regime. `_merge_index_and_tail` no longer
enters the interleave walk when the tail is empty — the selection is the
answer, in the order `select()` already returns it, taken as one numpy slice —
and `_is_ascending_run` tests the run element-wise in numpy instead of one
Python comparison per position. The walk itself is unchanged and still runs
for a non-empty tail. Before/after is the code again: the probe bound above is
in both columns.

| Corpus / window | scan | index, before | index, after | after vs before | after vs scan |
| --- | ---: | ---: | ---: | ---: | ---: |
| 10,000 / 10,000 | 79.2 ms | 15.5 ms | **8.8 ms** | 1.76x | 9.03x |
| 100,000 / 100,000 | 603.7 ms | 145.6 ms | **77.4 ms** | 1.88x | **7.80x** |

`do_recall` at 100,000 / 100,000: 367 → **299 ms**; at 10,000 / 10,000:
37.6 → **30.5 ms**. The non-vector part is unchanged (22 ms / 221 ms).

The 7x that was predicted for the arm before any of this was measured is now
the arm's number, one change later than the prediction expected: the
prediction was for the bytes, and the bytes were 18x by the view; what the
walk and the probe added on top was Python-level O(n) that the sync harness
never had to pay. The segment profile at 100,000 / 100,000, median of 12:

| Segment | Median | Share |
| --- | ---: | ---: |
| `_search_vector` total | 76.6 ms | |
| `_index_phase1` | 43.5 ms | 57% |
| ├ `_index_rows_lost_embedding` | 29.0 ms | 38% |
| ├ `_index_tail_rows` | 12.7 ms | 17% |
| ├ `_merge_index_and_tail` (was 69–93 ms) | 1.4 ms | 2% |
| │ └ `_is_ascending_run` (was 7 ms) | 0.2 ms | <1% |
| └ `select` | 0.2 ms | <1% |
| `_cosine_matrix` (the matmul) | 23.7 ms | 31% |
| outside phase 1 (embed, top-k, hydrate) | ~9 ms | ~12% |

The matmul is not yet the dominant term: the probe is, at 29 ms, for the
reason recorded above — SQLite walking the rows in the range — and the tail
query is 13 ms for a tail that is empty (it still runs the isolation query
with `LIMIT scan_limit` to learn that). Both are now larger than everything
else in the arm combined, and neither is where the design's bytes are. The
tail query is worth a look of its own.

### The probe, answered from an index

The partial index recorded above as a decision has been made. Both arms below
are the same script, the same machine and the same regime, differing only in
whether boot creates `idx_memories_lost_embedding` — the before arm is the
code with that block removed, run rather than remembered:

| Segment | Before | After |
| --- | ---: | ---: |
| `_search_vector` total | 77.92 ms | **49.92 ms** |
| ├ `_index_rows_lost_embedding` | 29.19 ms | **0.27 ms** |
| ├ `_index_phase1` | 21.47 ms | 7.00 ms |
| ├ `_index_tail_rows` | 12.55 ms | 12.53 ms |
| ├ `_merge_index_and_tail` | 1.40 ms | 1.40 ms |
| `_cosine_matrix` (the matmul) | 24.27 ms | 23.99 ms |

**1.56x on the arm**, and every segment the change does not touch is
unmoved to the second decimal — which is also what says the two runs are
comparable at all. The before arm reproduces the 29.0 ms in the table above
on a tree several changes later, so that figure was the probe and not the
day it was measured.

**The matmul is now the dominant term** (48% of the arm), which is the state
the design was aiming at: what remains is the arithmetic, not the bookkeeping
around it. The probe's 0.27 ms is not the 0.005 ms a bare statement costs —
the difference is building the id array and crossing into SQLite twelve times
per query, not the lookup.

It needs no schema version. An index is neither a column, a row, nor a read
contract: a copy of a real database carrying it was opened by the shipped
build, which has no code for it, and that build answered four recalls
bit-identically, left the index in place, and kept its recorded schema
version unchanged. So it is created where the isolation index already is —
idempotent, non-fatal, outside the migration ladder — and an existing
database picks it up on its next boot. One-time cost at 100,000 rows with
3 KB rows: `CREATE INDEX` 53 ms, +0.8 MB on disk, and a per-write cost that
does not separate from the blob write it rides along with.

`profile_index_path.py` alongside this file is the segment profile that
produced both tables, in-tree from this change on; the perf script removes
its scratch corpus at exit unless `PERF_INDEX_DIR` names where to keep it
(eight runs had left 4.6 GB in `/tmp` on the reference machine).
