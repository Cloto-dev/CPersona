# Reach, Recency and the Far Vote

Status: findings recorded; a plan for the 2.6 line. This page consolidates
three measurements and two designs into one account, and says what is
settled, what is shipped, and what the next line does about the rest.
The mechanism of the reach setting is in `SCAN_WINDOW_REACH_DESIGN.md`; the
measurements are in `benchmarks/measurements/` under the names quoted below.

## 1. The account in one paragraph

The vector scan window was documented as a cost bound. It is also a recency
prior: by ranking only the newest rows it hands every recent answer a small
field to win, and widening the window to reach older rows removed that
prior and cost twenty NDCG@10 points on recent answers. Keeping the window
as it is and adding the rows below it as a second ranked list preserved the
prior — the shipped list was identical on every query — but the second list
votes at the same strength as the first, and its ten votes displaced recent
answers that held only one vote, for a third of the wide window's loss.
Bounding how many far rows may vote is the last structural knob; whatever it
buys, the remaining question is how much a far vote should be worth, and
that is a scoring decision. The 2.6 line makes it, together with the
recency-weighted search it was always going to need, as one design with one
instrument that already exists.

## 2. What the window turned out to be

`results-scan-window-default-ab.md`. LongMemEval, 237,654 stored documents,
the real recall path under shipped defaults, queries split by where their
answer lies relative to the 10,000-row window, twelve rotations of the near
cohort, 240 paired queries per stratum. The replicate control reproduced to
the digit, so every difference below is the window and nothing else.

| Window 10,000 → 200,000 | Δ NDCG@10 |
| --- | ---: |
| answer inside the window (near) | **−20.19 ± 1.70** |
| answer below the window (far) | +4.93 ± 0.90 |

Nothing was truncated: every call returned ten rows and no gate fallback
fired. The near loss is rank displacement inside reciprocal-rank fusion —
more precisely, the vector arm hands the fusion its top `limit` rows, and a
recent answer that ranked third among 10,000 candidates and thirtieth among
200,000 is not lower on that list, it is off it, and its vote is gone.

Two more readings from the same run shape everything after it. A window of
50,000 bought more far-stratum quality than 200,000 (+12.87 against +6.06),
because the far answers sat at depth 20,000–29,500 and the wider window only
made them compete against 150,000 more rows: **reaching past the answer
costs something and buys nothing.** And a window of 300,000 and one of
500,000 agreed on every cell: once a window covers the corpus, its value
stops mattering, so a larger default is free for anyone whose corpus is
smaller than it.

The conclusion was not a number but a design fault: one setting was doing
two jobs, and neither could move while it did.

## 3. Separating reach from the prior

`SCAN_WINDOW_REACH_DESIGN.md` gave the two jobs two settings. The window
stays the **near list**, ranked exactly as it ships. `CPERSONA_VECTOR_REACH`
adds a **far list** — the top rows by cosine among scan positions
`[window, reach)` — as one more ranked list for the fusion. Existing lists
are untouched, so existing rows keep their votes; far rows can only be
added. No time term, deliberately: whether a far vote should weigh less than
a near one is a scoring question, and the design left it to the line that
owns scoring.

`results-scan-window-reach-ab.md`, same instrument, same rule:

| Reach 200,000, far list of ten | Δ NDCG@10 |
| --- | ---: |
| near | **−6.67 ± 0.85** |
| far | +3.87 ± 0.88 |

The structural claim held: the near list was identical, ids and order, on
480 of 480 paired queries; a reach equal to the window reproduced the
shipped answers exactly; the replicate reproduced to the digit. Every point
the near stratum lost was lost in the fusion after both lists existed.

The mechanism is the far list's vote. Reciprocal-rank fusion gives a row
`1 / (k + rank + 1)` per list it is on, so the far list's first row carries
exactly the near list's first row's vote. Over the 84 near-stratum queries
that lost, 251 rows entered the top ten that had not been there, and **187
of them (75%) carried a far-list vote and nothing else**. They displaced
recent answers that held only a vector vote; recent answers that also held a
lexical vote survived. The smaller far gain has the mirror cause: a far
answer with one vote loses to any near row with two, and ties with the near
row of equal rank, which is fused first.

The exploratory sweep narrowed it further. **The near cost barely depends
on the reach** — −5.97 at 50,000, −8.49 at 300,000 — while **the far gain
depends on it strongly** — +10.20 at 50,000, +4.25 at 300,000. It is the ten
full-strength votes that displace, not the depth they were drawn from. At
`limit=100` the loss more than doubles and the displacing rows arrive with
two votes each, since hundred-row lexical lists overlap nearly everything.

Shipped: `CPERSONA_VECTOR_REACH`, default off, bit-identical at the default.
It is an opt-in whose measured cost is in `behavior-contracts.md` §4.

## 4. Bounding the far vote by count

`CPERSONA_VECTOR_FAR_LIMIT` cuts the far list to `min(limit, N)` rows before
it reaches the fusion — a candidate-count setting, changing which far rows
may vote and nothing about how a row is scored. It is the one structural
knob the reach result left, and it was registered
(`prereg-scan-window-far-limit-ab.md`) with the reach held at 200,000, the
far list at one, two, three, five and ten rows, and a rule of near within
−1.0 and far within a point of what the full list bought.

`results-scan-window-far-limit-ab.md`, same instrument, reach 200,000:

| Far rows | Δ near | Δ far |
| ---: | ---: | ---: |
| 1 | **−2.73 ± 0.48** | +1.32 ± 0.70 |
| 2 | −4.35 ± 0.63 | +1.87 ± 0.79 |
| 3 | −5.06 ± 0.70 | +2.24 ± 0.85 |
| 5 | −5.61 ± 0.75 | +3.25 ± 0.91 |
| 10 | −6.67 ± 0.85 | +3.87 ± 0.88 |

Every control held — the full list reproduced the previous run to the digit,
the near list was identical on every query, the loss fell monotonically with
the length — and **no length passed**: even one far row costs 2.73 points,
and the two sides of the rule never cross. The reason is arithmetic. The
first row of any list carries `1 / (k + 1)`, the largest vote a single list
can cast, so the far list's first row ties the near list's first and beats
every near row of rank two or below that holds no second vote. A count can
remove the second through tenth far votes — that is the distance from −6.67
to −2.73 — but a list of one row is still a list whose first row votes at
full strength. The candidate-count knob is exhausted; what remains is the
weight of that first vote.

Shipped: `CPERSONA_VECTOR_FAR_LIMIT`, default off (the response `limit`),
bit-identical at the default. An opt-in like the reach, documented with its
measured effect.

## 5. What the three measurements establish

1. **The window is a recency prior with no name.** Its near-stratum value on
   this instrument is twenty points; any change that extends reach must
   supply that prior by some other means or pay it.
2. **The prior can be preserved at the candidate level.** A second list
   leaves the first list's votes exactly as they were — measured per query,
   not argued.
3. **Reaching past the answer costs and buys nothing.** For the window and
   for the far list alike, the gain peaks where the reach just covers the
   answers and falls as it goes further; a reach past the end of the corpus
   is inert. "As large as possible" is the wrong shape for either setting.
4. **What displaces is a vote, not a row.** A far row at full strength
   outranks a recent answer with a single vote; how many such rows there are
   sets the size of the loss, and how much each is worth sets whether there
   is a loss at all. The first is a count and has a knob; the second is a
   weight and does not — yet.
5. **Depth does not rescue a displaced answer.** Asking for more rows
   deepens every list and the far rows pick up lexical votes; at
   `limit=100` the loss is larger, not smaller.

## 6. The plan for the 2.6 line: a priced far vote, designed with recency

### 6.1 Why it is a scoring change, and why it waits

Everything shipped so far leaves the fusion's arithmetic alone: a list is a
list and a vote is `1 / (k + rank + 1)`. Making a far vote worth less than a
near vote changes what the fusion computes, and the 2.5 line ships changes
that preserve shipped behaviour at the default and can be soaked without
asking whether an answer moved because it broke or because it changed. A
change to how the fusion prices a vote is held for the line that redefines
scoring, where it is designed once with the other change that touches the
same layer.

### 6.2 The shape: one prior, its special cases

A recency prior is a weight on a row's vote as a function of how far back
the row sits — by scan position, or by age. The settings that exist are all
special cases of one function:

| Prior `p(row)` | What ships it |
| --- | --- |
| 1 inside the window, 0 beyond | the window alone (today's default) |
| 1 inside the window, 1 in `[window, reach)`, 0 beyond | `CPERSONA_VECTOR_REACH` |
| 1 inside the window, 1 for the first *N* far rows, 0 for the rest | `CPERSONA_VECTOR_FAR_LIMIT` |
| 1 inside the window, **w** in `[window, reach)`, 0 beyond | the priced far vote (2.6) |
| a smooth function of age | recency-weighted search (2.6) |

The 2.6 design is the last two rows, built as one mechanism: the far list's
reciprocal-rank contribution is multiplied by a weight `w ∈ (0, 1]`, and the
smooth variant replaces the step at the window edge by a curve of age. The
step version interpolates between two points that are already measured —
`w = 0` is the shipped answer (arm A) and `w = 1` is the full far list (arm
S) — which gives the measurement two identity controls for free and a
one-dimensional sweep between them. Under relative-score fusion the same
weight scales the far channel's normalised score before the sum, and the
channel count is not raised by the far list (the weight replaces the fourth
divisor, so a far list at `w` costs the near rows nothing at `w → 0`).

### 6.3 The final re-sort has to be decided first

In production the confidence scorer is on, and when it is on the recall's
last step re-sorts the whole fused list by a confidence score that the
fusion's order does not survive: measured earlier, two fusion modes that
differ on about a tenth of their rows with confidence off agree on every
row with it on. A far-vote weight that only the fusion sees would be invisible in
production. The 2.6 design therefore either removes the final re-sort or
makes the confidence score a function of the fused score, and measures with
the same regime production runs — the benchmarks so far have been run with
confidence off, and the plan says so rather than assuming the two agree.

### 6.4 What is pre-registered before any arm runs

The instrument is the one used three times already: the scene-blocked
LongMemEval corpus, the near/far strata, twelve rotations, the replicate and
identity controls, the per-query retriever lists. The design adds:

- **Arms**: `w ∈ {0, 0.25, 0.5, 0.75, 1}` at a reach of 200,000 with the
  full far list — the configuration every record so far was judged at, so
  the sweep is read against numbers that already exist; the reach-50,000
  companion as the exploratory sweep.
- **Identity controls at both ends**: `w = 0` must reproduce arm A and
  `w = 1` must reproduce arm S, to the digit.
- **Rule**: the near stratum within −1.0 of the shipped answer, and the far
  stratum within a point of what the unweighted far list buys at that
  reach. The absolute far bar (+5.0) is applied where a reach value is
  chosen, not here.
- **The far-only reading**: whether the displacing rows shrink in number as
  `w` falls, which is what a weight is supposed to do and a count cannot.
- **Regime**: run once with confidence off (comparable to the record) and
  once with the production regime after §6.3 is settled; a weight that
  passes in one and not the other is reported as such.

### 6.5 Rollout

Default off, bit-identical at the default, as every setting in this account
has been. Then the measurement. Then — and only then — the defaults move
together, in one change with one documentation update: the reach, the
far-list length, the weight, and the operating range the larger-corpus
documentation may claim. The last of these is the reason the whole line
exists.

### 6.6 What stays in the 2.5 line

The reach and the far-list length, as opt-in settings with their measured
costs documented. A reader with a six-figure corpus can turn them on today
and knows what it buys and what it costs; the default does not move until a
priced far vote lets it move without paying the recent answers for it.

## 7. Open questions

- Whether the weight should be one number or a function of the far row's
  scan depth within the far region — the exploratory sweeps say the gain is
  concentrated where the reach just covers the answers, which argues for the
  former until measured otherwise.
- Whether the time-based curve and the position-based step can share one
  setting, or whether a corpus with uneven write rates needs both.
- How the far vote interacts with the episode penalty, which already
  distinguishes memories by their position relative to the newest episode.
