# Contiguous Embedding Index

Status: proposed for the 2.5.x line. `SCHEMA_VERSION` stays 13, no new runtime
dependency is added, and the answers are the ones the current scan already
returns — bit for bit, including the order of equally-similar rows.

## 1. What changes and what does not

The local vector search ranks the newest `MAX_MEMORIES` rows by cosine. Phase 1
of that scan reads the embeddings:

```sql
SELECT id, embedding FROM memories
 WHERE <isolation> AND embedding IS NOT NULL [AND <source>]
 ORDER BY created_at DESC
 LIMIT ?
```

Measured at 100,000 rows of 768 dimensions on the reference machine, the time
splits like this:

| Segment | Time | Share |
| --- | ---: | ---: |
| SQLite row read (index order, with the blob) | 485.9 ms | 72.9% |
| `b"".join` + `frombuffer` | 162.3 ms | 24.4% |
| The matrix multiply itself | 18.1 ms | **2.7%** |
| Same arithmetic, read from a contiguous file | **95.1 ms** | 7.0x faster |

The bottleneck is not the arithmetic. It is turning stored bytes into a numpy
array one Python object at a time. This design moves the *source* of `(id,
embedding)` to a file that is already laid out the way numpy wants it, and
changes nothing else.

**The arithmetic stays in numpy, deliberately.** The value of this change is
that it is exact, and exactness is the half a re-implementation spends. Measured
on the same bytes, a hand-written Go dot product disagrees with numpy on 77.9%
of rows (largest absolute difference 1.9e-5), and two Go variants disagree with
*each other* on 94.4% — summation order is the whole story, and a blocked BLAS
order is not reproducible by a hand-written loop. Since a row's dot product does
not depend on any other row, keeping the same `mat @ query_vec` call means the
scores are identical and the existing equivalence gate keeps its teeth.

## 2. The invariants the index must preserve

**The scan window stays `MAX_MEMORIES`.** An index can afford to look at the
whole corpus, but a scan that ranks more rows than today returns different
answers — that is a different feature, needing a different gate, and it is not
this one.

**The row order is `created_at` DESC, then `id` ASC.** That order is the
tie-break: survivors keep scan order, the top-`limit` cut re-sorts back into
scan order, and `heapq.nlargest` is stable. It also decides *membership*, not
just sequence: when the limit cuts through a group of rows sharing a
`created_at`, the low ids are the ones that survive. On a production corpus 1.6%
of rows share a `created_at` with another row of the same agent (largest group:
9), so this is rare enough to be absent from an ordinary test fixture and common
enough to happen in service.

The statement will also **say** that order rather than inheriting it. Today
`id` ASC is a by-product of SQLite walking an index whose last column is
`created_at DESC`; nothing in the SQL asks for it, and one axis combination
resolves to a sort instead of an index walk — a sorter that is not required to
be stable. Adding `, id ASC` was measured rather than assumed, because a term
that forces a sort would cost the very scan this work speeds up: it produced an
identical order under an identical plan in every case. Measurement record:
`benchmarks/measurements/results-scan-order-stability.md`.

**The rest of the scan is untouched.** The two-phase split, the pre-hydrate
`limit` cut, and the by-id hydrate that re-applies the isolation axes all stay
exactly as they are.

## 3. The index is not an authority

`isolation_where()` remains the single source of the isolation predicate, and
the hydrate keeps re-applying it. The index carries axis columns for one reason
only: **the top-k cut happens before the hydrate.** An index that ignored the
axes would rank the whole corpus, fill the top-k with rows the hydrate then
drops, and return a short, wrong answer — the failure gets worse as a bucket
gets smaller relative to the corpus.

So the index filters, but it is never asked to be right about who owns a row.
Its only obligation is one-directional:

> **index candidates ⊇ the rows the authority admits.**

| If the index filter is | What happens | Severity |
| --- | --- | --- |
| Too loose | The hydrate drops the extras | **Correct.** Merely wasteful |
| Too strict | Rows vanish silently | **Undetectable recall loss** |

One side, machine-checkable: assert the containment over random corpora and
random axis values, and confirm by mutation that a deliberately over-strict
index turns it red.

## 4. File format

A header followed by parallel arrays, all fixed-width:

| Part | Contents |
| --- | --- |
| Header | magic, format version, dimension, dtype, row count, watermark, and a fingerprint over the embedding model, the dimension and the scoring version |
| `embeddings` | `float32[count][dim]`, contiguous, in canonical order |
| `ids` | `int64[count]` |
| `agent_code`, `project_code`, `channel_code`, `source_code` | `int32[count]`, interned against small string tables in the header |
| `created_at` | fixed 19-byte ASCII, the column's canonical `YYYY-MM-DD HH:MM:SS` form |

`agent_code` carries the axis the isolation predicate compares for equality;
the other three are compared against a small set. Either way the per-row test is
an integer one.

Interning is cheap because the axes are low-cardinality: a production corpus has
7 agents, 12 distinct projects, 15 channels and 38 source ids. The whole axis set
costs 16 bytes per row next to 3,072 bytes of embedding.

`source_code` exists because the cardinality was measured rather than assumed.
The source filter is a prefix match against a JSON field, which looks like
something an index cannot do — but with 38 distinct values the prefix resolves
against the string table into a small set of codes, and the per-row test is
integer membership. Without it, every recall from a multi-user conversation
would fall back to the old path, which is a large share of real traffic.

**Rows the format cannot represent are named, not dropped.** A `created_at`
outside the canonical 19-character form (the import path carries a restored
record's own value through) is excluded from the arrays and its id is listed in
the header; the query path unions that list into the tail read below, so the row
stays reachable. The list is capped — past the cap the builder declines to
build, which leaves the system on the current path rather than quietly serving a
corpus with holes in it. On the production corpus the list is empty: every
`created_at` in both tables is exactly the canonical form.

Fixed-width ASCII is what makes the merge safe: SQLite compares these values as
text, and for equal-length strings in this form a byte comparison and a
chronological comparison are the same comparison.

## 5. Freshness: the watermark

A stale index is worse for a memory system than a slow one: an index built an
hour ago silently withholds the last hour of memories, which are the most likely
to be asked for.

> **The index records the maximum `id` present when it was built, answers only
> for rows at or below that watermark, and the scan reads everything above it
> from SQLite exactly.**

Five things follow, and they are the reason this is the design rather than a
rebuild schedule:

1. **There is no window in which a new memory is invisible.** Not "the window is
   small" — the window does not exist.
2. **Rebuild frequency stops being a correctness question** and becomes a
   performance knob. A late rebuild grows the tail and degrades latency
   measurably; it never returns a wrong answer.
3. **The tail is exact.** The newest rows — the ones most likely to be
   retrieved — are never served from a derived structure.
4. **Deletion needs no index path at all.** A row deleted after the build still
   appears as a candidate, and the hydrate fails closed on it. The index never
   needs a delete, a repair or a compaction: if it is wrong, throw it away and
   rebuild.
5. **The tail read is the shape SQLite is best at** — the newest few rows in
   index order.

**The watermark answers one question, and it is not the only one that matters.**
It says whether a row existed at build time. It cannot say whether that row had
an embedding at build time, and those come apart for every row the builder
skipped because its vector was still NULL: such a row is not in the matrix, its
id is at or below the watermark so the tail does not read it either, and the
moment maintenance fills the embedding in the scan returns it and the index does
not. That is the silent withholding this section opens by ruling out, reached
through `check_health(fix=True)`, whose prefetch exists to fill exactly those
rows (bug-278). So the build names them too: `unembedded_ids` sits beside
`excluded_ids` in the header and rides the same exact tail read. The cap covers
the two lists together, because they are bound into one statement, and a corpus
that exceeds it declines to build rather than shipping an index that cannot name
its own holes.

**The merge is an ordered merge, not a concatenation.** It is tempting to assume
the tail is uniformly newer than the index and simply prepend it. Nothing
promises that: the import path carries a restored record's original `created_at`
while ids are assigned fresh by `AUTOINCREMENT`, so restoring an old export into
a database that already holds newer rows produces new ids bearing old
timestamps. A production corpus has no such inversion today, which is exactly
the kind of fact that stops being true without anyone noticing. Both sides are
already sorted, so merging them on `(created_at DESC, id ASC)` costs nothing
over prepending and does not have the failure mode.

## 6. Absence, corruption, and dimension mismatch

The index is optional in the strongest sense: the scan it replaces is still
there, still correct, and still the fallback. Three conditions return to it —
the file is missing, the file fails its own integrity check (row count against
file length, or fingerprint mismatch), or the query vector's dimension does not
match the header's.

A mid-flight model swap leaves a mixed-dimension corpus behind, and that is a
fourth condition — one the *builder* refuses rather than the reader. The live
scan applies its window **before** it skips foreign-width rows: it ranks whatever
survives inside the newest `MAX_MEMORIES`. An index holding a single width cannot
reproduce that window while other widths exist, because it would rank the newest
`MAX_MEMORIES` *of its own width* — more rows, and more rows is a different
answer even when every one of them scores identically. So the build declines
while the corpus is mixed. That state is transient by construction, and
throughout it the scan this index replaces stays correct and merely slower, which
is the trade the whole design exists to make safely.

**A fallback has to be visible.** If the only trace is a log line, an index that
has been dead for a week reads as "somehow not faster", which is the failure
this project has already made once. The condition is reported where a reader of
the system's health will meet it.

## 7. What this design deliberately leaves out

- **Approximation of any kind.** No quantization, no approximate neighbours.
  Those buy resident memory, which is a different problem with a different
  acceptable-degradation question; mixing them in would cost the exactness that
  makes this change gateable against the existing suite.
- **Episodes, in the first wave.** The episode scan is structurally the same and
  the format applies to it unchanged, but episodes are a fifth of the rows. Once
  the memory arm is several times faster the bottleneck moves somewhere that has
  not been measured, and that measurement — not a guess made now — is what
  should decide where the next change goes.
- **Repair.** The index is a derived artifact: it is not backed up, and it is
  never fixed. It is deleted and rebuilt.
- **Re-tagging after a build.** Moving a row to another project or channel
  leaves the index's axis columns stale until the next rebuild. In the loose
  direction the hydrate covers it; in the strict direction the row is missed
  until rebuild. Re-tagging is rare, and this is accepted rather than solved —
  stated here so that the next reader does not have to discover it.

## 8. How it is gated

The equivalence gate already exists. A benchmark harness in this repository
preloads a contiguous matrix, calls the same `mat @ q`, and compares against the
unmodified search at two levels — the result id sequence with its cosines, and
the full recall path's message ids — over a real corpus. It gains a backend
rather than a replacement.

Two additions, both because the existing gate cannot see them:

- **Ties.** A corpus of well-separated vectors ranks the same under any
  tie-break, so the gate must be fed a fixture containing rows of equal
  similarity, or it will confirm an order it never tested.
- **Scoped queries.** The existing harness falls back to the original function
  whenever a channel, project or source filter is present, so every axis-carrying
  call — which is to say, nearly every real one — has never been through it.

Both the equivalence gate and the containment assertion are confirmed by
mutation: an order deliberately broken in one place must turn the gate red, and
an index deliberately made over-strict must turn the containment test red, with
each mutation killing only its own check.
