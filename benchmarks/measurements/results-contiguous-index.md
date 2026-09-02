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
