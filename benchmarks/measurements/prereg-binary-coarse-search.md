# Pre-registration: how many candidates the binary coarse search keeps

Registered before the agreement of any `K'` was computed on any store below,
apart from the mechanical smoke run described under "Seen before
registration". The instrument, grid, measure, rule and controls are fixed by
this document; a later change is written under "Amendments" with what had been
seen when it was written.

## Question

The far seats ([design §5](../../docs/BINARY_COARSE_SEARCH_DESIGN.md#5-far-seats))
hold two places for records past the scan window. They rank those records by
the cosine of their stored float32 vectors, but only the `K'` records whose
one-bit codes are nearest the query's reach that ranking. `K'` is the one
approximation in the step, and it ships at a provisional 1,000.

This measurement sets `K'` ([design §9](../../docs/BINARY_COARSE_SEARCH_DESIGN.md#9-measurement),
decision C of §10): the smallest value at which the seats a recall fills agree
with the seats an exact scan of the same positions would fill, at a bound of
95%, at the largest store measured. It also reports what the step costs at each
store size, which does not move `K'`.

What it opens: `K'` stops being a starting point. What it does not open:
whether a seated far record answers the question. That needs a reader and a
store with a real far stratum, and this measurement claims reach only.

## Instrument

**Stores.** Three stores, nested by recency, every row a bge-m3 vector read
from the Track A/B embedding disk cache, and every row's text the document's
own text:

| Store | Rows | Contents, newest first |
| --- | ---: | --- |
| `s100k` | 100,000 | the newest 100,000 rows of `s237` |
| `s237` | 237,654 | LongMemEval in the scene layout of `scan_window_ab.py` (seed 20260903, rotation 0) — the store the scan-window measurements used |
| `s1m` | 1,000,000 | `s237`, then 762,346 documents from the other LMEB corpora below it |

The filler of `s1m` is every distinct document text of the other LMEB corpora
that is not a LongMemEval text, shuffled by seed 20261001, keeping the first
762,346 that the cache holds. A row has the same `created_at` in every store
that holds it, so the newest 10,000 rows — the near window — are the same in
all three, and a smaller store's far region is the newest part of a larger
one's.

`design §9` names "synthetic stores of 100,000 and 1,000,000 records". Random
vectors are not used: the fidelity of a sign code depends on the geometry of
the model's coordinates ([design §2](../../docs/BINARY_COARSE_SEARCH_DESIGN.md#2-the-representation)),
and on unit Gaussian vectors almost no record clears the similarity floor, so
the seats would be empty. The 100,000 and 1,000,000 stores are composed from
real documents instead.

**Queries.** `q1` = the 500 LongMemEval queries. `q2` = 500 queries drawn by
seed 20261002 from the other LMEB tasks, one per distinct text, among those the
cache holds. `q1` runs on every store, `q2` on `s1m` only, where the corpora it
was written for are part of the far region.

**Settings.** `CPERSONA_MAX_MEMORIES = 10,000`, `CPERSONA_VECTOR_REACH = 0`, so
the far region is every scan position from 10,000 to the end of the store.
`limit = 10`, FTS on, no block index (rows are written directly, so no record
divides into blocks), no cue. The similarity floor is the vector arm's, read
from the build under test: the threshold 0.3 times
`CPERSONA_RRF_THRESHOLD_FACTOR` 0.5 = 0.15 in both regimes.

**Regimes.** *Shipped*: `rrf`, confidence scorer off — the defaults.
*Production*: `rsf` with the confidence scorer on, as the deployment this
project runs. The production regime writes on recall, so it reads its own
copy of the store.

**The candidates** come from the shipped supplier
(`coarse_search.coarse_candidates`) reading the shipped coarse index, built by
the shipped build command, asked once per query for the largest `K'` of the
grid. A smaller `K'` is the prefix of that answer: the supplier's order is
total (distance, then scan position). Control V2 checks it.

**The cosines.** For each store and query set, every far record's cosine with
every query is computed once, by one float32 matmul over the contiguous
index's rows. The exact list and every `K'` list are ranked with those same
values, by cosine then scan position, at or above the floor. A record's cosine
therefore cannot differ between two lists, and two lists differ only in which
records the Hamming pass let through. The production ranking
(`far_seats.ranked`, which reads the vectors from SQLite by id) is compared
with this one by control V3, not assumed equal.

**Eligibility.** A recall seats the best far records it is allowed to: not one
the answer already holds, and not one an ordinary arm reached and the gate or
autocut refused. For each store, query set and regime, the real `do_recall`
runs once per query with the far seats on. The far ranking it receives is the
union of the first 128 entries of every list that will be scored, and the
records it finds eligible are recorded. The seating of each list is then
replayed from that record: the first two eligible records of the list. The
eligibility of a record does not depend on the far list, because the far seats
displace nothing (invariant 2 of the design).

Harness: `coarse_search_k.py` at the commit that adds this file. The build
under test is `master` at that commit.

## Grid

`K' ∈ {32, 64, 128, 256, 512, 1000, 2048, 4096, 8192, 16384, 32768, 65536}`.
1,000 is the provisional value. Each list keeps its first 128 entries.

## Measure

For a query, `E` is the set of records the exact list seats and `C` the set the
`K'` list seats (at most two each). The query's agreement is `|E ∩ C| / |E|`,
so two seats of which one differs count one half. Queries whose exact list
seats nothing are left out (their count is reported). A cell's agreement is the
mean over its queries.

## Rule

The decision reads eight cells: `s100k/q1`, `s237/q1`, `s1m/q1` and `s1m/q2`,
each under the shipped and the production regime.

**`K'` is the smallest grid value whose agreement is at least 0.95 in all
eight cells.** The point estimate is read, as decision C states the bound; the
95% bootstrap interval is reported beside it.

If no grid value meets the bound in all eight cells, `K'` stays at its
provisional value, the curve is reported, and no rule in the store's size is
chosen from it. A size rule is reported as an exploratory reading only (below).

If `K'` is decided, the provisional constant in `coarse_search.py` becomes that
value. Its cost is reported and does not move it: whether the far seats are
worth turning on at that cost is a separate question, which this measurement
informs and does not settle. Both switches stay off by default.

## Controls, read before any outcome

A failed control makes the run invalid. Nothing is read from an invalid run.

1. **V1 identity.** Asking the supplier for as many candidates as there are far
   records returns every far position exactly once (three queries per store and
   query set).
2. **V2 prefix.** Asking for `K' ∈ {32, 1000, 8192}` directly returns the first
   `K'` of the largest answer (ten queries per store and query set).
3. **V3 bridge.** At the provisional `K'`, the first two records of
   `far_seats.ranked` equal those of this harness's list, or differ only where
   the harness's second and third cosines are within 1e-6 (twenty queries per
   store and query set). The largest cosine difference on shared records is
   reported.
4. **V4 the index answers what the live store answers.** At `K' = 1000`, the
   index and the live supplier return the same candidates, distances and
   positions (three queries, on `s100k` and `s237`).
5. **V5 no truncation.** No list runs out of its 128 kept entries before two
   eligible records are found while longer than that.
6. **Settings took effect**: the window, the reach, the far seats switch and
   the regime are asserted in-process; the far ranking is asked for exactly
   once per recall; the shipped regime writes nothing (`recall_count` stays 0).
7. **Every supplier answer came from the index**, not from the live fallback.

## Prediction, stated before the run

A sign code keeps about one bit per dimension of a vector's direction, and the
Hamming distance tracks the angle loosely on a real model's coordinates. The
best two far records by cosine are usually separated from the bulk by more than
the code's noise, so a few hundred to a few thousand candidates should hold
them. The number needed grows with the far region, because more records sit
near the query's code by chance. Expected: the provisional 1,000 meets the
bound on `s100k`; on `s1m` the smallest passing value lies between 1,000 and
8,192; `q2` needs no more than `q1`, because a query from a narrow corpus has
neighbours that stand further out from the rest of the store.

## Reported, not part of the rule

- The agreement of every cell at every `K'`, with its bootstrap interval, the
  expected number of seats lost per recall, and the share of recalls that keep
  both seats.
- **Raw agreement**: the same measure on the first two records of each list
  with no eligibility applied.
- How often the exact list fills zero, one and two seats, and the median
  number of far records a recall found ineligible.
- **A size rule (exploratory)**: the smallest passing `K'` per store, against
  the size of the far region. It cannot decide; if it suggests a rule, that is
  a reason to register one.
- **Cost**, measured on this machine (Apple M5, 32 GiB) with nothing else
  running on the stores, median of 25 queries after 3 warm-up, at
  `K' ∈ {256, 1000, 4096, 16384}` and the decided `K'`: the far ranking end to
  end (`far_seats.ranked`), the supplier through the index and through the live
  store, an exact scan of the same positions read from SQLite and from the
  contiguous index, the bytes the Hamming pass and the re-rank read, the peak
  memory of one far ranking, and each index's build time and size. Stores:
  `s100k`, `s237`, `s1m`, and the newest 12,000, 15,000, 20,000, 30,000 and
  50,000 rows of `s237`, to find the size below which the coarse path costs
  more than an exact scan. The live supplier is timed on 3 queries on `s1m`.
  Absolute numbers are this machine's; the reference machine of the scale
  ladder is not used here.

## Outputs

`results-binary-coarse-search.md` next to this file, and the JSON the harness's
`score` step writes. The record quotes the controls first, then the agreement
table and the decision, then the readings above.

## Seen before registration

- The design document, the supplier, the far seats and their tests.
- The `plan` step on the real data: how many filler documents the cache holds
  and how many queries were drawn. It computes no cosine.
- One mechanical smoke run on the 12,000-row cost store (`c12k`): `build-store`,
  `index`, `prepare` for `q1` and `eligibility` under the shipped regime. It
  checked that each step finished, that the controls it runs passed (V1, V2,
  V3; the largest cosine difference on shared records was 8.3e-7), how long a
  recall takes (median 17 ms), and that the eligibility record is not empty or
  total: a recall found a median of 1 and at most 4 far records ineligible, and
  the shipped regime wrote nothing. No agreement was computed or printed, and
  no list was compared with another. `c12k` is not a decision store.

No `K'` has been compared with another on any store.

## Amendments

None.
