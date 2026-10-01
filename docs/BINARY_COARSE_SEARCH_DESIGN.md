# Binary Coarse Search — design

Status: proposed for 2.6.2. `SCHEMA_VERSION` does not change and no runtime
dependency is added. Both settings this page introduces are off by default, and
with them off every answer is the one 2.6.1 gives, bit for bit, including the
order of equally-similar rows.

## 0. What this step is

The local vector search ranks the newest `CPERSONA_MAX_MEMORIES` records by
cosine (default 10,000). A record past that window cannot be reached by meaning
at all, however close it is to the query. The lexical arms search the whole
corpus, so such a record is reachable when the query shares its words, and
unreachable when it does not.

Two later steps reduced that gap without closing it:

- **Block reach** ranks clause-sized blocks of every record that divides into
  more than one block, over the whole corpus
  ([Block reach](BLOCK_REACH_DESIGN.md)). A record that is a single clause has
  no blocks, because its one block would be the record itself. Past the window,
  such a record is still out of semantic reach.
- **The time cue** searches the period it names, over the whole corpus
  ([The recall process](RECALL_PROCESS_DESIGN.md#22-the-cue-arm)). Its vector
  half, however, ranks only the `CPERSONA_MAX_MEMORIES` most recently *stored*
  records inside the period. A long or vague period on a large store leaves the
  rest of the period out of the vector half.

This step adds one mechanism and two uses of it:

1. **A one-bit index of every record**, scanned by Hamming distance, whose best
   candidates are re-ranked by their stored float32 vectors.
2. **Far seats** (section 5): records past the window that the index finds are
   held beside the answer, the way block hits and cue hits already are.
3. **The cue arm's remainder** (section 6): the part of a cue's period past the
   vector half's cap is searched through the same index.

The two uses are independent. Either can ship without the other.

**What this step claims is reach, not precision.** It makes a class of records
reachable by meaning that is unreachable today, at a resident cost of one bit
per dimension. Whether the reached records answer the question is a separate
measurement, and this page does not speak for it. On the deployment this
project runs, the store held 5,341 records when block reach was designed, which
fits inside the window: there, this step changes nothing until the store grows
past 10,000 records.

**Naming.** The vector scan already reads in two steps (`(id, embedding)` first,
then the payload of the survivors by id), and the code calls that split "two
phases". That split is about the row payload and is unrelated to this one, which
is about the vector's representation. This page and the code it describes say
*binary coarse search*, and never reuse the other term.

## 1. Why the reach does not widen the window, and does not vote

The direct way to reach past the window is to widen it. Measured on the real
recall path over 237,654 stored documents, a window of 200,000 instead of
10,000 gained 4.93 NDCG@10 points on queries whose answer lies past the window,
and lost 20.19 on queries whose answer lies inside it
([Reach and recency in the scan window](SCAN_WINDOW_REACH_DESIGN.md#1-one-number-two-jobs)).
The window is also a recency prior: it gives every recent answer a small field
to win, and widening it removes that.

The next way is to keep the window and add the rows past it as one more ranked
list in the fusion. That preserved the window's own list exactly, but the far
list's votes displaced recent answers. A weight on the far vote was then
measured: at every weight above zero the near stratum still lost 3.01 points or
more, because the rows that cost it hold a far vote *and* a lexical vote, and a
single weight moves the gain and the loss together
([Results: the price of a far vote](https://github.com/Cloto-dev/CPersona/blob/master/benchmarks/measurements/results-far-weight-sweep.md)).
That measurement ends by naming the next candidates as structural.

This step takes the structural route that the project has already used twice:
**a far record is not voted into the fusion; it is held beside the answer.** A
held seat displaces nothing the fusion returned, so the near stratum cannot
lose a row by construction. Section 5 states what that costs.

## 2. The representation

Each record's stored vector is quantised to the sign of each dimension —
`1` where the component is greater than zero — and packed eight dimensions to a
byte. This is the quantiser block reach already uses, and the Hamming distance
is computed by the same function. One code path serves blocks and records.

The fidelity of a sign code on a model's own coordinates is a heuristic. Block
reach states why the random-hyperplane identity does not carry over as stated
([Block reach §3](BLOCK_REACH_DESIGN.md#3-the-representation-is-one-bit-per-dimension)),
and the same caveat holds here. Two things follow from it in this design:

- The Hamming pass only **selects** candidates. Every candidate that can reach
  an answer is ranked again by the cosine of its stored float32 vector, the
  same quantity the near scan ranks by.
- How many candidates the Hamming pass keeps (`K'`) is set by measurement
  (section 9), not by the identity.

## 3. The coarse index

The bits live in a derived file beside the contiguous embedding index
([Contiguous embedding index](CONTIGUOUS_INDEX_DESIGN.md)), built by the same
command and governed by the same doctrine: it is not backed up, it is never
repaired, it is safe to delete, and if it is wrong it is thrown away and built
again.

| Part | Contents |
| --- | --- |
| Header | magic, format version, dimension, row count, watermark, the same fingerprint the contiguous index carries, and the named-row lists below |
| `ids` | `int64[count]` |
| `bits` | `uint8[count][dim/8]` |
| `agent_code`, `project_code`, `channel_code`, `source_code` | `int32[count]`, interned exactly as the contiguous index interns them |
| `created_at` | 19-byte ASCII, the canonical form |
| `timestamp` | 19-byte ASCII: the record's own time as SQLite's `datetime()` reads it, or 19 zero bytes when it does not |

Rows are written in scan order, `created_at` DESC then `id` ASC, so a row's
position in the file is its scan position. The watermark, the excluded-row list
and the unembedded-row list have the meaning they have in the contiguous index
([§5 there](CONTIGUOUS_INDEX_DESIGN.md#5-freshness-the-watermark)): rows above
the watermark and the named rows are read live, so no stored record is ever
invisible to this path for want of a rebuild.

**Why a file of its own, and not more arrays in the contiguous index.** The
point of one bit per dimension is resident memory. The contiguous index holds
float32 for every record, and on Windows its loader reads the arrays into memory
rather than mapping them, so that a rebuild can replace the file. Bits stored in
the same file would bring every float32 vector into memory with them. In a file
of their own, the resident cost at one million records of 1,024 dimensions is
about 190 bytes a row — 128 of bits, 62 of ids, axes and times — or about
190 MB, against 4.1 GB for the same vectors in float32.

**`timestamp` is stored for section 6 only.** It is the axis a cue's period is
measured on, and it is not the axis the file is ordered by. SQLite compares it
as an instant through `datetime()`, because stored timestamps mix spellings;
the builder stores what `datetime()` returns, which is fixed-width, so a byte
comparison in the file is the comparison the SQL makes. A record whose
timestamp `datetime()` cannot read falls in no period, in the file as in SQL.

## 4. The supplier contract

Both uses ask one question:

> **Within these axes, these scan positions and, optionally, this period,
> which `K'` records have the smallest Hamming distance to the query's bits?**

The answer is a list of record ids with their distances, ordered by distance
and then by scan position. It carries no text and no metadata.

There are two suppliers, and they run one algorithm:

- **The coarse index**, as above.
- **The live store**, when the index is absent, unusable, or of another
  dimension. It reads the stored float32 vectors in scan order, in bounded
  chunks, quantises them with the same function, and keeps the same `K'`.

The two return the same list for the same store, because quantising a stored
vector is deterministic and a Hamming distance is an integer. **Building or
deleting the index changes what a recall costs, never what it returns.** The
contiguous index already holds itself to that rule, and the far list of the
reach setting was required to meet it; this step does too. The live supplier
is slow on a large store, and that is its whole cost: a missing index is
reported where a reader of the system's health meets it, as the contiguous
index's absence is.

**The index is not an authority.** Its axis codes narrow the scan before `K'`
is cut, because an index that ignored them would fill `K'` with rows the
hydrate then drops. Its obligation is one-directional, as in
[the contiguous index](CONTIGUOUS_INDEX_DESIGN.md#3-the-index-is-not-an-authority):
the rows it offers include every row the isolation predicate admits.
`isolation_where()` stays the single authority, and the hydrate re-applies it.

**`K'` is selected by counting, not by sorting.** A Hamming distance is an
integer from 0 to the dimension, so the best `K'` are found with one histogram
of the distances and one pass that keeps the rows below the cut-off distance,
breaking the tie at the cut-off by scan position. No heap and no full sort is
needed, and the result is deterministic.

## 5. Far seats

**Where it looks.** Scan positions from `max(CPERSONA_MAX_MEMORIES,
CPERSONA_VECTOR_REACH)` to the end of the store: the records no vector list of
the recall has ranked. The window's own rows are never searched here. A near
record the vector arm ranked low has already been judged by the arm whose job
it is, and a seat for it would make this a deeper vector search rather than a
reach.

**How it ranks.** The supplier returns `K'` candidates. Their stored float32
vectors are read by id, with the isolation and source predicates re-applied,
and ranked by cosine against the query vector the vector arm already embedded.
A candidate whose cosine is below the similarity floor the vector arm applies
to its own rows is dropped: a far record is a whole record, the same population
the floor was set for, so it is held to the same bar a near record must clear to
be ranked at all. Equal cosines go by scan position.

**How it is admitted.** Up to `CPERSONA_FAR_SEATS` places are held after the
block reservation, filled in cosine order with records the answer does not
already hold. As with the cue's seats, a record that an ordinary arm reached and
the quality gate or autocut refused is not eligible: a refused row does not come
back this way. The quality gate is not consulted for the seats and is not
changed for any other row.

What follows from that, stated rather than argued away:

- **The answer the fusion returned is unchanged.** The seats add rows and remove
  none, so a recent answer cannot be displaced by a far one. This is the
  property section 1 needs.
- **A response can be longer.** The seats are additional to `limit`, like the
  block reservation, the cue's seats and the propagation seat. The count check
  that bounds a response grows by `CPERSONA_FAR_SEATS`.
- **The seats are an upper bound on what a bad far hit can cost**, not a claim
  that far hits are good.
- **A seated row is not credited** to the record's recall count, for the reason
  the other held rows are not: no gate admitted it.
- Each seated row says where it came from: `match_reason.signal` = `far`,
  `admission` = `reservation`, and the recall trace records the reservation
  under its own kind. `reconstruct` holds a seated record as an item of its own
  after its window, as it holds a block's.

**Where it does not apply.** A deployment whose vector search is remote does not
run the local scan, so it has no query vector to quantise and no far seats, as
it has no block reach. Episodes are not searched (section 11).

## 6. The cue arm's remainder

Today the vector half of the cue arm ranks the records whose timestamp falls in
the period, reading at most `CPERSONA_MAX_MEMORIES` of them in storage order.
A period holding more embedded records than that loses the rest from the
vector half, and which ones it loses is decided by when they were stored, not
by the period.

The change keeps the exact part as it is and searches only the remainder:

1. The records of the period at the first `CPERSONA_MAX_MEMORIES` positions in
   scan order are ranked by exact cosine, as today.
2. The remaining records of the period are passed through the supplier, with
   the period as a filter on the `timestamp` column. Its `K'` candidates are
   re-ranked by the cosine of their stored float32 vectors, as in section 5.
3. The two lists are merged on cosine — they are on one scale, because both
   are exact cosines of stored vectors, held to the same floor — with scan
   position breaking ties, and the cue arm keeps its own depth from the merge.

**A period holding no more than `CPERSONA_MAX_MEMORIES` embedded records gives
exactly today's answer**, because the remainder is empty. Nothing about how a
cue moves a row, how its seats are filled or how it widens changes; only the
vector half's candidates do.

This part is switched by `CPERSONA_CUE_COARSE`, separately from the far seats,
so that either can ship alone.

## 7. Settings

| Setting | Meaning | Default |
| --- | --- | --- |
| `CPERSONA_FAR_SEATS` | places held for far records; `0` means the far scan does not run | `0` |
| `CPERSONA_CUE_COARSE` | search the remainder of a cue's period through the coarse index | `false` |
| `CPERSONA_COARSE_DEPTH` | `K'`, the candidates the Hamming pass keeps | set by the measurement of section 9 |

At the defaults the new code does not run: a guard, not a scan that returns
nothing. `CPERSONA_MAX_MEMORIES` keeps its meaning — the near window, and with
it the recency prior — and is where the far scan begins.

## 8. Invariants

1. **Isolation is never bypassed.** The index narrows by axis codes with the
   one-directional guarantee of section 4, and the hydrate re-applies
   `isolation_where()` and the source prefix filter, failing closed.
2. **The near list is untouched.** The window's rows, their scores, their order
   and the rows the fusion returns are those of 2.6.1 with any setting.
3. **The index never changes an answer.** The index and the live store return
   the same candidates for the same store.
4. **Order is total and stated.** Hamming ties and cosine ties break by scan
   position (`created_at` DESC, `id` ASC), never by the order a sort happened to
   leave.
5. **The hydrate is cut by count, not by threshold.** Only the `K'` candidates
   are read by id, and only the seated rows' payloads are materialised.
6. **Vectors of another width are skipped**, never reshaped. The builder
   declines a store of mixed widths, as the contiguous index's builder does,
   and the live supplier skips rows of another width.
7. **Bounded growth.** A response holds at most `limit` plus the held places of
   every kind, now including `CPERSONA_FAR_SEATS`.
8. **At the defaults, nothing changes**, pinned by the behaviour golden.

## 9. Measurement

The measurements are committed with their harnesses, as the scan's earlier
optimisations were: `benchmarks/measurements/results-binary-coarse-search.md`
and the script that re-derives it.

**Cost**, on synthetic stores of increasing size: latency of the far scan
through the index, through the live supplier, and as an exact float32 scan of
the same positions; bytes read in the Hamming pass and in the re-rank; peak
memory; index build time and size. The size at which the coarse scan becomes
slower than the exact one is reported, because below it there is a store size
where the index costs more than it saves.

**`K'`.** The approximation sits in one place: which far records survive the
Hamming pass to be re-ranked. With `K'` equal to the number of far records,
the far seats are filled exactly as an exact float32 scan of the same positions
would fill them, and the measurement starts from that identity. Across a grid
of `K'` fixed before the run, it reports how often the seats agree with the
exact scan's, and `K'` is the smallest value on the grid that meets the bound
the maintainer sets (section 10, C). The starting point of 1,000 is a starting
point, not a result.

**Precision is not measured here.** A claim that far seats improve answers
needs a store with a real far stratum and a rule fixed in advance, and it has to
beat the same number of extra rows without the seats, which is the rule the
time cue was held to. Until such a measurement exists, this step is reported as
reach.

**The published benchmark figures cannot move with the settings off**
(invariant 8). With them on, a benchmark whose store fits inside the window
has no far records, so its far scan seats nothing; one whose store does not is
measured, not assumed.

## 10. Decision points for the maintainer

- **A. Placement.** A file of its own beside the contiguous index (proposed), or
  more arrays in the contiguous index under a new format version. The earlier
  outline proposed the second; section 3 gives the reason to prefer the first.
  Neither changes the schema.
- **B. When the index is built.** By the existing build command, as the
  contiguous index is. The watermark makes a late build a matter of latency, not
  of correctness, so this step adds no automatic build.
- **C. The allowed approximation.** The share of queries on which the far seats
  must agree with an exact scan at the chosen `K'` — for example, at least 95%.
- **D. Enabling.** Both settings off by default. When far seats are enabled,
  how many: 2 is proposed, the size of the block reservation.
- **E. `CPERSONA_MAX_MEMORIES`.** Its meaning is unchanged: the near window and
  the recency prior. The far scan begins where it ends.
- **F. The far seats' floor.** The vector arm's similarity floor (proposed), or
  no floor, as the block reservation has none. Blocks have none because a short
  span scores on another scale; a far record does not.
- **G. Setting names.** The three names of section 7 are provisional.

## 11. Non-goals

- **A second language.** The pass is numpy. A compiled index service is a later
  step for stores past the point where numpy's pass is too slow, and it would
  implement the supplier contract of section 4 rather than replace it.
- **Approximate nearest-neighbour structures** such as HNSW or IVF.
- **Episodes.** Their counts are far smaller and fit the window; this is decided
  separately once the record index is established.
- **The remote vector path**, which answers for itself.
- **The far vote list** of `CPERSONA_VECTOR_REACH`. It is unchanged; the far
  seats begin where it ends.
- **A precision claim** (section 9).
