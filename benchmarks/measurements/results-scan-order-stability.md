# Local vector scan — row order, measurement record

`order_stability_scan.py` alongside this file re-derives every number below.

## Why the order was measured

Work is underway to feed phase 1 of `_scan_memories_local` from a contiguous
embedding file instead of a SQLite row read. That statement's row order is not
presentation: it **is** the tie-break. Survivors keep scan order, the top-`limit`
cut re-sorts back into scan order, and `heapq.nlargest` is stable — so two rows
of equal cosine are separated by nothing except where this query put them:

```sql
SELECT id, embedding FROM memories
 WHERE <isolation> AND embedding IS NOT NULL [AND <source>]
 ORDER BY created_at DESC
 LIMIT ?
```

`created_at` is `TEXT NOT NULL DEFAULT (datetime('now'))` — one-second
resolution — so equal keys are not an edge case in a corpus written faster than
one row per second. A replacement that reproduces the scores bit-for-bit but not
this order still changes answers, and the change would be invisible to any test
whose fixture has no ties in it.

## Regime

- Synthetic matrix: 216 rows over 2 agents x 3 projects x 3 channels x 3
  timestamps x 4 repeats, cells interleaved so a filtered tie group's surviving
  ids are non-adjacent (a rule that only holds for dense id runs would show up
  as a difference). Episodes carry the same matrix.
- Schema and index set built by the product's own `get_db()`, not a hand-copied
  DDL, so the planner sees exactly the indexes a real database has.
- Predicates built by the product's own `isolation_where()`, so the axis
  combinations under test are the ones the recall path actually issues.
- SQLite 3.50.4, via `aiosqlite` — the same driver the scan runs on.
- Every case is measured twice, before and after `ANALYZE`.

## Pre-registered expectations, and what happened

Written before the first run.

| | Expectation | Verdict |
| --- | --- | --- |
| H1 | Rows sharing a `created_at` come back in `id` ASC order | **holds**, every case |
| H2 | The order does not depend on which index the planner picks | **holds**, across 6 distinct plans |
| H3 | `ANALYZE` changes plans, not order | **holds** — and it changed no plan either |
| H4 | A `LIMIT` cutting a tie group keeps the ids H1 predicts | **holds** at limits 1 / 5 / 17 / 36 |
| H5 | Episodes behave identically | **holds** |

## The canonical order

> **`created_at` DESC, then `id` ASC.**

That is what a contiguous index has to reproduce, and it is a rule about
membership as much as ordering: at limits that cut through a tie group, the ids
that survive are the low ones.

## Three findings the design has to carry

**1. The order is inherited, not stated — and stating it is free.**

Nothing in the SQL asks for `id ASC`. It falls out of SQLite walking an index
whose last column is `created_at DESC`, where equal keys are ordered by rowid
ascending. Spelling it out was measured rather than assumed, because an extra
`ORDER BY` term that forces a sort would cost the very scan this work speeds up:

| Axis combination | same order | same plan |
| --- | --- | --- |
| agent only | yes | yes |
| agent + project (global pool) | yes | yes |
| agent + project X | yes | yes |
| agent + channel | yes | yes |
| agent + project X + channel | yes | yes |
| cross-agent scan | yes | yes |

`ORDER BY created_at DESC, id ASC` produced an identical order under an
identical plan in every case. The contract can be written down instead of
depending on planner behaviour, at no cost.

**2. One plan sorts instead of walking an index — and a sorter is not required
to be stable.**

Five of the six axis combinations resolve to `SEARCH ... USING INDEX`. The
cross-agent scan (no agent predicate) resolves to `SCAN ... USE TEMP B-TREE FOR
ORDER BY`. It happened to return `id` ASC here, but that is the sorter's choice,
not a guarantee. The recall path never issues it — `_search_vector` always binds
a string `agent_id` — so the measured rule covers every call the hot path makes.
It is finding 1 that removes the dependency for good.

**3. `id` order and `created_at` order can disagree, so the tail merge is a
merge, not a concatenation.**

On a production corpus (2,845 memory rows, all embedded, 7 agents):

| | memories | episodes |
| --- | ---: | ---: |
| rows | 2,845 | 608 |
| rows sharing a `created_at` with another row of the same agent | 46 (1.6%) | 0 |
| largest tie group | 9 | 1 |
| places where `id` order disagrees with `created_at` order | **0** | **0** |

Ties are rare but real, which is exactly the regime that makes them dangerous:
a fixture built from ordinary data will not contain one, and the check will pass
while proving nothing.

The zero in the last row is the current state, not a property. The import path
carries each restored record's original `created_at` while ids are assigned
fresh by `AUTOINCREMENT` — restoring an old export into a database that already
holds newer rows produces new ids bearing old timestamps. So a design that
splits the corpus at a max-`id` watermark and expects the tail to be uniformly
newer is relying on something the write paths do not promise. Both sides are
sorted, so an ordered merge on `(created_at DESC, id ASC)` costs nothing over a
concatenation and does not have the failure mode.

## Detector check

Every predicate in this record reports True, and a predicate that cannot report
False proves nothing. The harness therefore runs one probe against a
deliberately wrong order (`created_at DESC, id DESC`) and requires
`ties_id_ascending` to come back False. It does; the script exits non-zero if it
ever does not.
