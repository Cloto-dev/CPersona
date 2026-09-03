# Reach and Recency in the Vector Scan Window

Status: proposed for the 2.5.x line, default off. `SCHEMA_VERSION` stays 13,
no new runtime dependency is added, and with the new setting at its default
the answers are the ones the current scan already returns — bit for bit,
including the order of equally-similar rows.

## 1. One number, two jobs

The local vector search ranks the newest `CPERSONA_MAX_MEMORIES` rows by
cosine (default 10,000; memories and episodes are each scanned under it). The
setting was documented as a cost bound: it limits how many rows a recall reads
and keeps the contiguous index the size it is. On a six-figure corpus it also
makes most of the corpus invisible to the vector arm, and the obvious fix is
to raise it.

Measured on the real recall path, raising it is not a relaxation
(`benchmarks/measurements/results-scan-window-default-ab.md`). On 237,654
stored documents, a window of 200,000 instead of 10,000:

| Stratum | What it means | Δ NDCG@10 |
| --- | --- | ---: |
| far — the answer lies below the 10,000-row window | only the wide window's vector arm can see it | **+4.93 ± 0.90** |
| near — the answer lies inside the 10,000-row window | both windows can see it | **−20.19 ± 1.70** |

Nothing was truncated: every call in every arm returned exactly ten rows and
no quality-gate fallback fired. The near-stratum loss is rank displacement
inside reciprocal-rank fusion. The vector arm hands the fusion its top `limit`
rows, so a recent answer that ranked third among 10,000 candidates and ranked
thirtieth among 200,000 is not lower on the vector list — it is *off* it, and
its reciprocal-rank vote is gone. The lexical arms still carry it, but without
the vector vote it loses to rows that fused better.

So the window has been doing two jobs at once:

1. **Reach** — how far back the vector arm looks. This is the cost bound the
   setting was named for.
2. **A recency prior** — by keeping only the newest rows, the window hands
   every recent memory a candidate field of 10,000 instead of the whole
   corpus. For a query whose answer is recent that prior is free accuracy,
   and on this instrument it is worth twenty points. It has no name and no
   setting; it is a side effect of the cost bound.

Widening the window removes the prior in the act of extending the reach. The
two cannot be moved separately while one number controls both. This document
gives each its own knob.

The exploratory sweep in the same measurement says what shape the answer must
not take: a window of 50,000 bought *more* far-stratum quality than 200,000
(+12.87 against +6.06), because the far answers sat at depth 20,000–29,500 and
the wider window only made them compete against 150,000 more rows. Reaching
past the answer costs something and buys nothing. Whatever the reach is set
to, the near field must stay small.

## 2. The shape that is not taken: a graded window

The first idea is a window with two widths: rows inside the inner width are
scored as today, rows between the inner and the outer width are admitted with
a lower weight. At equal widths it is today's scan, bit for bit.

It is not taken, because the weight has to come from somewhere. A row past
the inner width either keeps its cosine — in which case the inner width does
nothing and this is a single wide window — or its cosine is multiplied by a
function of its age. That function is a time-weighted score, and a
time-weighted score is the recency-weighted search already planned for the
2.6 line, with its own open questions (how it interacts with the confidence
re-sort, and what a time term is worth on a query distribution that is not
uniform in time). Folding it into a window setting would decide those
questions by accident. This design changes *which rows become candidates*,
not how a candidate is scored; it has no time term and no weight.

## 3. The design: a second vector list

Two settings, one of them existing:

| Setting | Meaning | Default |
| --- | --- | --- |
| `CPERSONA_MAX_MEMORIES` | the **near window**: the newest N rows the vector arm ranks as it does today | 10,000 (unchanged) |
| `CPERSONA_VECTOR_REACH` | the **reach**: how many of the newest rows the vector arm may look at in total; rows at scan positions `[N, REACH)` form a second list | `0` = same as the window; the far list does not exist |

When `REACH > N`, the vector retriever produces two ranked lists instead of
one:

- the **near list** — the top `limit` rows by cosine among positions `[0, N)`.
  This is exactly the list it produces today: same rows, same threshold, same
  stable tie-break, same order.
- the **far list** — the top `limit` rows by cosine among positions
  `[N, REACH)`, cut the same way.

Positions are in the scan's own order, `created_at DESC, id ASC`, which is
also the order the contiguous index file is written in. Memories and
episodes are split at the same position each, mirroring how they are scanned
today.

The fusion receives the far list as one more ranked list. Under reciprocal-rank
fusion (the shipped mode) that is the whole change: a fourth loop identical to
the three that exist. Every existing list is untouched, so every existing row
keeps exactly the vote it has today; far rows can only be *added*. The
recency prior is now a thing with a name — it is the near list existing at
all, at width N — and the reach is a separate number that no longer takes it
away.

### 3.1 Why the near list is preserved and not merely similar

The three lists the fusion sees today are the vector list, the episode FTS
list and the memory keyword list. The near list *is* today's vector list, by
construction: the scan positions `[0, N)` are the rows today's scan reads, the
threshold and the `limit` cut are the same code, and the tie-break is the
scan order both before and after. A row's reciprocal-rank contribution is a
function of its rank on its own list, so it does not change when another list
is appended. The far list and the near list are disjoint by position, so no
row appears on both and no row's vote is counted twice.

That disjointness also keeps the quality gate's scale right. The legacy gate
rescales its threshold by the maximum score three retrievers can give one row,
`3 / (K + 1)`. With the far list present the maximum a single row can reach is
still three votes — near *or* far, plus the two lexical lists — so the
constant remains the per-row maximum. The calibrated gate compares raw
fusion scores and is unaffected for the same reason: the score distribution
of rows that exist today does not move.

### 3.2 Relative-score fusion

Under `CPERSONA_RECALL_FUSION=rsf` each channel's raw scores are min-max
normalised per query and the sum is divided by the number of active
channels. A far list fused as a fourth channel keeps the near rows' normalised
values (their min and max are computed within their own list) but changes the
divisor from three to four for every row, which lowers every fused score
against the cosine-scale gate. Merging the far rows into the vector channel
instead would change the near rows' min and max. Neither is bit-preserving
once the far list exists, and the measurement below is registered for the
shipped `rrf` mode only. Under `rsf` the far list is fused as a fourth
channel, and no claim is made about it until it is measured.

### 3.3 The two suppliers

The vector scan has two suppliers for the same contract — `(ids, similarities)`
in scan order — and both produce the far list the same way:

- **Contiguous index**: `select()` returns positions in file order; the near
  list takes `positions[:N]` as today and the far list takes
  `positions[N:REACH]`. The rows above the index's watermark (stored since the
  last build) are the newest rows and belong to the near list, exactly as they
  are merged into the scan today.
- **SQLite scan** (no index, or the index declines): the same statement with
  `LIMIT ? OFFSET ?` for the far region, read in chunks so the peak memory is
  bounded by the chunk rather than by the reach. `OFFSET` walks the index
  order it skips, which is the price the index-less path already pays for its
  window; a reach that makes it too slow is a reason to build the index, not a
  reason for this path to approximate.

Both suppliers must be able to produce the far list. A separation that works
only when the index is present would make an index build change the answers,
which the index design forbids (`CONTIGUOUS_INDEX_DESIGN.md` §2).

## 4. What the setting costs

With `REACH > N` a recall reads `REACH − N` more embedding rows than today.
With the contiguous index that read is the fast path measured for the index
(7× the SQLite read); without it, the chunked scan pays roughly what the wide
window paid in the measurement above — p50 about doubled at a reach of 200,000
on 237,654 rows, with the lexical arm as the floor in both cases. The
`behavior-contracts.md` entry for the setting states this cost in the same
terms as the window's.

Memory is bounded by the chunk size on the scan path and by the index file on
the index path, neither of which grows with the reach beyond what the index
already holds.

## 5. What this design does not do

- It does not weight by time. A far row that fuses well is returned with its
  cosine; a near row is returned with its cosine. Whether the far list's vote
  should be worth *less* than the near list's is a scoring question, and the
  measurement below is the evidence that would raise it.
- It does not choose the values. The defaults stay at 10,000 and off. The
  pre-registered measurement decides whether the far list does what it is
  meant to; the values are chosen after that, as their own change with their
  own documentation.
- It does not touch the episode penalty, the confidence re-sort, autocut, or
  the quality gate. Each of them sees the fused list as before, longer by the
  far rows that fused well.

## 6. The gate

Two gates, one per state of the setting:

- **Default (off)**: the behaviour golden (`tests/golden/behaviour_252.json`)
  and the index equivalence tests must show no difference. The far list must
  not exist as code that runs when the setting is at its default — a guard,
  not a scan that returns nothing.
- **On**: `benchmarks/measurements/prereg-scan-window-reach-ab.md`, registered
  before any arm is run, on the same instrument and with the same decision rule
  that rejected the wide window — the far stratum must gain at least +5.0
  NDCG@10 and the near stratum must lose no more than 1.0. If the separation
  passes, raising the reach is a change that can be priced and documented. If
  it fails on the near stratum, the far list's vote is too strong at equal
  weight, and the next candidate is a per-list weight — which is the scoring
  question this design deliberately leaves to the recency-weighted line.
