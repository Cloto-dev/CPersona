# bug-249 — two-phase memory scan, measurement record

The deferral condition on bug-249 was "should land with a perf number attached".
This file is that record; `perf_bug249_two_phase_scan.py` alongside it re-derives
every number (it keeps a verbatim copy of the pre-split single-query scan as the
baseline).

## Regime

- Synthetic corpus: 10,000 rows (`MAX_MEMORIES` scan window), 768-d float32
  embeddings, identical rows except for content size; 10 rows planted near the
  query vector so survivors are realistic.
- `CPERSONA_EMBEDDING_MODE=none` (the scan is handed a query vector directly);
  FTS off (not on the path under test); per-variant fresh DB copy.
- Warm page cache, median of 7 runs (1 warm-up discarded), Darwin 25.5.0.
  Cold-cache numbers would favour the split further (the old path reads the
  whole 377 MB file, the new one ~40 MB) but macOS offers no unprivileged way
  to drop the page cache, so they are not claimed.

## Results — content = 16,000 chars/row (current write cap; DB 377 MB)

| threshold (survivors) | limit | single-query | two-phase | text materialised |
|---|---|---|---|---|
| 0.5 (10) | 10 | 45.9 ms | **33.5 ms (1.37x)** | 160,328,890 → 160,300 chars |
| 0.05 (855) | 100 | 45.5 ms | **34.1 ms (1.33x)** | 160,328,890 → 1,603,263 chars |
| 0.0 (4,941) | 100 | 46.4 ms | **35.6 ms (1.30x)** | 160,328,890 → 1,603,263 chars |
| −1.0 (10,000) | 100 | 48.1 ms | **36.0 ms (1.34x)** | 160,328,890 → 1,603,263 chars |

Peak Python heap during one scan (tracemalloc): **225.4 MB → 63.6 MB (3.5x)**.
The remaining 63.6 MB is the embedding blobs both variants must read (~30 MB of
rows plus the `b"".join` copy inside `_cosine_batch` — recorded in the registry
entry as open follow-up surface, together with the identical single-query shape
still present in `_scan_episodes_local`).

## Results — content = 2,000 chars/row (the old write cap; DB 104 MB)

23.1 ms → 16.4 ms (**1.41x**), peak heap 85.4 MB → 63.6 MB. The 8x amplification
the 2000 → 16000 write-cap raise silently added to this read path is gone.

## The honest remainder

With `limit` at the library ceiling (10,000) **and** a threshold that admits
nearly the whole window, the by-id hydrate is slower than the sequential read it
replaced: 52.1 ms → 90.0 ms. The MCP `recall` tools cap `limit` at 100 in their
schemas, so no tool call reaches this shape; it exists for bench full-ranking /
bulk-export callers. At the same `limit=10000` with an ordinary threshold
(855 survivors) the split still wins, 45.0 → 37.6 ms.
