# Results: separating reach from the recency prior

Pre-registration: `prereg-scan-window-reach-ab.md`. Harness:
`scan_window_ab.py`. Run 2026-09-03, on the build of the harness that added
the reach arms (the instrument itself is the one that priced the window).

**Verdict, by the pre-registered rule: the separation as designed does not
pass.** With the near list held exactly as it ships and a far list added at
equal weight, reaching 200,000 rows costs 6.67 NDCG@10 points on queries whose
answer is recent and buys 3.87 on queries whose answer is old. The rule asked
for Δ_far ≥ +5.0 with Δ_near ≥ −1.0; both halves miss. The design's structural
claim — that the existing lists are untouched — held on every query, so the
loss is not the mechanism the wide window failed by. It is the one the
pre-registration named as the next possibility: at equal weight, the far
list's vote is too strong.

## What was measured

`CPERSONA_MAX_MEMORIES` at the shipped 10,000 in every arm, and
`CPERSONA_VECTOR_REACH` at 0 (off), at 10,000 (equal to the window, the off
state written as a number) and at 200,000 (the far list reaching where the
wide window reached). LongMemEval, 237,654 stored documents, the real
`do_recall` under the shipped regime (`rrf`, fused gate on, autocut on,
confidence off, threshold 0.3, local vector search, no contiguous index),
the same scene-blocked layout, seed and strata as the window measurement,
twelve rotations, 240 paired queries per stratum. Each arm asserted in-process
that both settings took effect and recorded them.

Beyond the fused answer, every arm recorded the four lists the fusion
weighed — the vector arm's near list, its far list, the episode FTS list and
the memory keyword list — so that the claim "the near list is unchanged" could
be checked per query rather than argued.

## Controls

| Control | Expectation | Measured |
|---|---|---|
| Replicate (`A-rep`) | identical | **identical** — Δ = ±0.00 on 480 paired queries, fused ids identical for 480/480 |
| Off-is-identical (`A0`, reach = window) | identical to reach unset | **identical** — Δ = ±0.00, fused ids identical for 480/480, no far list produced |
| Positive (the far list must matter above the window) | far stratum moves | **moves** — Δ = +3.87, 38 better, 11 worse |
| Near-list identity (`A` vs `S`) | 0 queries differ | **0 / 480** — the vector arm's near list is the same ids in the same order on every query, in both strata |

The last row is the design's load-bearing claim, measured: adding the far
list changed nothing about the list the shipped scan produces. Every point
the near stratum lost below was lost in the fusion, after both lists had
been produced.

## Primary result (`limit=10`)

| Arm | Window | Reach | Stratum | NDCG@10 | Recall@10 | MRR | p50 | p95 |
|---|---:|---:|---|---:|---:|---:|---:|---:|
| A | 10,000 | off | near | 34.18 | 43.72 | 0.363 | 313 ms | 789 ms |
| S | 10,000 | 200,000 | near | **27.51** | 37.83 | 0.288 | 612 ms | 1402 ms |
| A | 10,000 | off | far | 5.69 | 11.04 | 0.044 | 299 ms | 725 ms |
| S | 10,000 | 200,000 | far | **9.56** | 15.38 | 0.087 | 593 ms | 1007 ms |

| Pair | Stratum | Δ NDCG@10 | better | worse |
|---|---|---:|---:|---:|
| A → S | near | **−6.67 ± 0.85** | 0 | 84 |
| A → S | far | **+3.87 ± 0.88** | 38 | 11 |
| A → A-rep, A → A0 | either | ±0.00 | 0 | 0 |

Top-10 membership changed for 99.2% of queries in both strata (mean Jaccard
0.54). Every call in every arm returned ten rows; no gate fallback fired.

Against the wide window measured before (`results-scan-window-default-ab.md`,
arm B: near −20.19, far +4.93), the separation keeps two thirds of what the
wide window lost on recent answers, and recovers somewhat less on old ones.
Both differences have the same cause.

## Mechanism: the far list's vote at equal weight

Reciprocal-rank fusion gives a row `1 / (k + rank + 1)` per list it appears
on. The far list is a list, so its first row carries exactly the vote the
near list's first row carries. The far region holds no answer for a
near-stratum query — by construction, that answer's scene is inside the
window — but it holds rows that resemble the query, and each of the top ten
of them enters the fusion with a full-strength vote.

The exploratory reading registered for this question says how much of the
loss that is. Over the 84 near-stratum queries that lost NDCG@10, 251 rows
entered the top ten that were not there before, and **187 of them (75%)
carried a far-list vote and no other** — no near vote (impossible by
disjointness), no lexical vote. They displaced recent answers that had only
one vote themselves: a recent answer with a vector vote and a lexical vote
still wins against a far-only row, and the near stratum's answers that had
both survived; the ones that had only the vector vote were interleaved with
far rows of the same rank and pushed down or out.

The smaller far-stratum gain has the mirror explanation. In the wide-window
arm, near rows and far rows shared one vector list of ten, so a far answer
faced at most nine vector-voted competitors in the fusion, and the near rows
among them had earned their place against the whole 200,000. Here the near
list contributes ten rows of its own at full strength whatever the far list
holds, a far answer at far rank *r* ties a near row at near rank *r*, and the
tie goes to the near row (its list is fused first). A far answer holding only
a far vote therefore loses to any near row holding two and to the near row
of equal rank. The separation protects the near stratum and, in the same
motion, protects it from the far answers too.

So the finding of the previous measurement is refined rather than reversed.
Reach and the recency prior *are* separable at the candidate level — the
near list is provably untouched — but the fusion still prices a far vote
and a near vote the same, and at that price the far list buys less than it
costs. What is left is a weighting question: the far list's vote is a
function of nothing but rank, and it would have to be worth less than a near
vote for the far rows to add without displacing.

## Cost

Latency roughly doubles at p50 (306 → 605 ms) and grows 1.5× at p95 (739 →
1109 ms) — the same shape as the wide window, since the same rows are read;
the lexical arm is the floor in both arms.

## Limitations

The same as the window measurement: one dataset, one embedding model, one
fusion mode (`rrf`), an authored near/far mix, and scene blocks as an
authored recency structure. The far-only reading counts rows, not points; it
says where the displacing rows came from, not how many points each cost.

## Exploratory (not part of the decision rule)

Six rotations, 120 paired queries per stratum, in a separate work directory
as registered. Arm A is re-run on these builds so every pair is paired.

| Arm | Reach | limit | near NDCG@10 | Δ near | far NDCG@10 | Δ far | p50 |
|---|---:|---:|---:|---:|---:|---:|---:|
| A | off | 10 | 35.68 | — | 6.92 | — | 297 ms |
| S50 | 50,000 | 10 | 29.71 | **−5.97 ± 1.02** | 17.12 | **+10.20 ± 1.81** | 367 ms |
| D | 300,000 | 10 | 27.19 | −8.49 ± 1.41 | 11.17 | +4.25 ± 1.34 | 654 ms |
| E | 500,000 | 10 | 27.19 | −8.49 ± 1.41 | 11.17 | +4.25 ± 1.34 | 655 ms |
| A100 | off | 100 | 42.05 | — | 5.22 | — | 286 ms |
| S100 | 200,000 | 100 | 25.97 | −16.08 ± 2.09 | 13.08 | +7.87 ± 1.73 | 608 ms |

Far-only votes among the displacing rows: S50 114/125, D and E 102/146,
S100 14/325.

**The identity control holds for the far list too.** D and E agree on every
cell — NDCG, recall, MRR, latency aside — so a reach past the end of the
corpus is inert, as it was for the window.

**The near cost barely depends on the reach; the far gain depends on it
strongly.** Reaching 50,000 rows costs the near stratum 5.97 points and
reaching 300,000 costs 8.49 — a far list of ten full-strength votes exists
in both, and that existence is most of the price. The far stratum, whose
answers sit at depth 20,000–29,500, gains 10.20 at a reach of 50,000 and
4.25 at 300,000: as with the window, reaching past the answer makes it
compete against rows it did not need to meet, and here the far list's own
top ten fills with them. The pooled figure at 50,000 is +2.11 ± 1.16 — the
one configuration in either measurement that comes out ahead on the authored
50/50 mix — but its near half still misses the rule by six points, and a
value tuned to where this instrument placed its far answers is tuned to the
instrument.

**Depth makes it worse, and changes who displaces.** At `limit=100` the near
loss more than doubles (−16.08) while far-only displacing rows all but vanish
(14 of 325). With hundred-row lexical lists nearly every far row also holds
a lexical vote, so the displacing rows arrive with two votes rather than
one, and a per-list weight on the far vote alone would reach less of the
loss at this depth than at ten. The MCP default is ten; this is the reading
for callers who ask for more.

## What this measurement does not settle

It does not choose a weight. The pre-registration placed a per-list weight
outside its scope, as a scoring change, and this result is the evidence that
would raise it: the far list's rows displace by vote strength, not by
membership, so the remedy is in how the vote is priced rather than in which
rows are produced. Two shapes are visible from here and neither is measured:
a smaller far list (fewer far rows can enter the fusion at all — a
candidate-count knob, structural), and a lighter far vote (a per-list weight
inside the fusion — a scoring change that belongs with the recency-weighted
search line). The exploratory sweep narrows the first: the near cost is
nearly the same at a reach of 50,000 as at 300,000, so it is the far list's
ten votes, not the rows they were drawn from, that displace. Shortening the
list attacks that directly and is cheap to try on this instrument; the
second changes what the fusion is, and is not decided by a measurement of
the reach.
