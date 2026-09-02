# Where the non-vector time goes — recall path profile after the contiguous index

`profile_recall_path.py` alongside this file produced every number below. The
contiguous index record (`results-contiguous-index.md`) ended with a number it
could not explain: at 100,000 rows `do_recall` spent 221 ms outside the vector
arm, and the benchmark README's claim that "most of the remaining time is
FTS5" was a guess. This is the measurement that replaces the guess.

## Regime

- Reference machine: Intel N150, 4 threads, `governor=performance`, idle at
  start (load 0.15; the later runs started at 1.5–1.8 from the previous run's
  teardown). numpy 2.4.6, Python 3.11.2, aiosqlite 0.22.1.
- The real path: `do_recall` end to end, FTS on, contiguous index built
  before every measurement (this is the post-index state — the vector arm's
  memory scan is already at its 77 ms).
- 1024-dimensional embeddings, synthetic corpus, response limit 10, median of
  12 queries per set, two warm-up queries per set.
- **Episodes are in the corpus this time**: one per five memories (20,000 at
  100,000 rows). The earlier records had none, and that turned out to matter
  more than anything in the FTS arm — see below. A run with zero episodes is
  kept for comparison with those records.
- Two attributions per run: function stages (nested, not summable) and every
  statement that went through `aiosqlite.execute_fetchall` (the `execute`
  path — the recall-count bump — is not wrapped, because a coroutine wrapper
  breaks aiosqlite's context-manager result; it accounts for the ≤ 9 ms gap
  between `do_recall` and its stages under `production`).

Three query sets, because an FTS5 arm has no single cost — it has a cost per
matching row:

| Set | Query | Rows it matches |
| --- | --- | --- |
| `broad` | `topic {i} question` | every memory and every episode (`topic` is in all of them) |
| `narrow` | a 4-digit run | a handful of rows |
| `none` | `zqxjvk{i}` | nothing — the MATCH returns empty and the LIKE fallback runs |

Two configurations: `default` (rrf, confidence off — what an install runs)
and `production` (rsf, confidence on — what this project's own deployment
runs; it adds the temporal span, the cosine backfill and the recall-count
bookkeeping).

## The answer to the 221 ms

100,000 rows, window 100,000, **no episodes**, default config — the regime
of the earlier record:

| Set | `do_recall` | vector arm | FTS5 memories (MATCH + bm25 + ORDER BY rank) | LIKE fallback | `COUNT(*)` for the gate | everything else |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| broad | 319.1 ms | 80.4 ms | **224.4 ms** (70%) | — | 7.1 ms | ~7 ms |
| narrow | 89.4 ms | 80.8 ms | 0.6 ms | — | 7.3 ms | ~1 ms |
| none | 201.7 ms | 104.4 ms | 0.2 ms | **79.2 ms** | 7.2 ms | ~10 ms |

So the guess was right for the query set the earlier record happened to use:
on a query that matches the whole corpus, **the non-vector part is FTS5 to
within 6%** (224 of 239 ms). On a query that matches a handful of rows the
same arm costs 0.6 ms and the non-vector part is the two `COUNT(*)` statements
the quality gate issues to size its pool. And on a query that matches nothing
the arm is replaced by something worse: the LIKE fallback scans the whole
`content` column (79 ms) to confirm there is nothing.

The FTS5 cost is proportional to the number of matching rows, not to the
corpus. 10,000 → 100,000 rows on the broad set: 20.5 → 213–224 ms, ×10.4–11.0
for ×10 rows, about 2.1–2.2 µs per matching row. The episode table shows the
same rate from a different size (2,000 episodes 5.4 ms, 20,000 episodes 48.1
ms; 2.4–2.7 µs/row). This is what `ORDER BY rank` means: bm25 is evaluated for
every row the MATCH admits before the top 10 can be known.

### Which set is a real query?

The synthetic sets bracket the cost; they do not say where a real query sits.
Five queries of the shape this project's own sessions issue (mixed Japanese
and identifiers, run through `_build_fts_query`) were counted against the
production database (2,919 memories, 621 episodes, a different machine):

| Query (abbreviated) | memories matched | episodes matched | ranked top-10 |
| --- | ---: | ---: | ---: |
| `CPersona scale ladder sidecar chunked scan Phase` | 48.4% | 72.9% | 8.0 ms |
| `N150 で perf_contiguous_index.py を走らせた手順 ssh checkout uv venv パス` | 13.3% | 21.6% | 6.5 ms |
| `公開リポのドキュメントを英語に統一する方針` (19 trigrams) | 10.9% | 19.6% | 3.0 ms |
| `cloudflared config overrides url` | 25.3% | 38.5% | 4.6 ms |
| `hook が沈黙して落ちる原因` | 13.7% | 23.5% | 2.0 ms |

Real queries match 11–48% of the memories and 20–73% of the episodes — far
nearer `broad` than `narrow`. Two things push them there: the trigram
tokeniser turns a Japanese sentence into a disjunction of every 3-gram it
contains, and common ASCII terms (`config`, `scan`) are in a quarter of the
corpus. At 100,000 rows a query in this range is on the order of 30–100 ms of
bm25 evaluation, which is the same order as the indexed vector arm.

## What the earlier records could not see: episodes

100,000 rows **with 20,000 episodes**, window 100,000:

| Config / set | `do_recall` | vector arm | of which episode scan | FTS5 memories | FTS5 episodes | span MIN/MAX | LIKE fallback | `COUNT(*)` |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| default / broad | 497.2 | 227.8 | **96.2** | 213.1 | 48.2 | — | — | 8.6 |
| default / narrow | 238.4 | 226.4 | 95.8 | 0.6 | 2.5 | — | — | 8.4 |
| default / none | 297.0 | 228.7 | 96.2 | 0.3 | 2.3 | — | 57.2 | 8.3 |
| production / broad | 569.2 | 224.3 | 96.9 | 215.1 | 48.1 | **72.0** | — | 7.4 |
| production / narrow | 315.1 | 227.8 | 96.2 | 0.6 | 2.6 | 73.9 | — | 8.5 |
| production / none | 295.4 | 226.8 | 95.7 | 0.3 | 2.3 | — | 56.9 | 8.5 |

(ms; the vector arm here is `_search_vector`, which covers both tables.)

**The contiguous index covers memories only.** `_search_vector` still scans
the episodes table row by row (`_scan_episodes_local`, `SELECT id, summary,
start_time, embedding, … FROM episodes`), materialising every 4 KB blob:
96 ms for 20,000 episodes, 9.5 ms for 2,000 — 4.8 µs per row, linear. That is
**more than the indexed scan of 100,000 memories costs (77–80 ms)**, and it is
paid on every query regardless of what the query matches. The vector arm at
this corpus is 228 ms, of which the part the index was built for is 80.

Under the production configuration a second linear term appears:
`SELECT MIN(timestamp), MAX(timestamp) … WHERE datetime(timestamp) IS NOT
NULL` — the temporal span that anchors confidence scoring — costs 72–74 ms at
100,000 rows and 7.4 ms at 10,000. The `datetime()` call has to run on every
row in the isolation scope; no index helps it. It is issued on every recall
that has at least one result.

## Where the time goes, ranked

At 100,000 memories + 20,000 episodes, a realistic query (say 30% match),
production config:

| Term | ms | Grows with | Covered by |
| --- | ---: | --- | --- |
| FTS5 memories, bm25 over matching rows | ~65 (0.6 … 215) | matching rows | nothing — this is tier 5 |
| episode vector scan | 96 | episodes | nothing — the index stops at `memories` |
| indexed memory vector arm | 77 | memories (memory bus) | tier 2, done |
| temporal span with `datetime()` | 73 | memories in scope | nothing; production config only |
| FTS5 episodes | ~15 (2 … 48) | matching episodes | nothing — tier 5 |
| LIKE fallback | 57 (zero-hit queries only) | memories | nothing |
| lost-embedding probe + empty-tail query | 42 | memories (row walk) | the tier-2 leftovers goal |
| gate pool `COUNT(*)` ×2 | 8 | rows in scope | nothing |
| fusion, scoring, gate, autocut, advisory | < 1 | results | — |

Three things this table says that the goal's question did not ask:

1. **FTS5 is the dominant non-vector term, and it is a cost per matching row,
   so it is a cost per query shape.** A design for tier 5 has to start from
   the match fraction, not from the corpus size — reducing the disjunction
   (fewer, longer terms), bounding the candidate set before ranking, or
   ranking a recency window rather than the whole postings list are the
   shapes of an answer; a faster bm25 is not, because bm25 is not slow, it is
   evaluated 30,000 times.
2. **Episodes are the largest single term the index did not touch**, and they
   need nothing new: the contiguous index already knows how to build, load
   and merge a table; it was pointed at one table. Extending it to
   `episodes` is additive and needs no schema change.
3. **The production configuration pays 73 ms per query for a two-number
   answer that changes only when a row is written.** The span is a function
   of the isolation scope's contents, not of the query.

## What this record does not settle

- Real-corpus FTS timing at scale. The per-row rate was measured on synthetic
  30-character rows; the production probe (5.7 µs per matching row for real
  content on a different machine) suggests the rate rises with content
  length, as bm25 over longer documents should. The 100,000-row number for a
  real corpus is therefore a lower bound.
- Whether the LIKE fallback is reachable in practice. It runs only when the
  FTS MATCH admits zero rows, and the match fractions above say a real query
  almost never does. It is recorded because it is O(N) and silent, not because
  it was observed firing outside the `none` set.
- The `execute` path (the recall-count bump under confidence) was not wrapped;
  it is inside the ≤ 9 ms the stage table leaves unattributed.
