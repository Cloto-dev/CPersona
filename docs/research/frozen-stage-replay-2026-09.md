# Where the loss is — a frozen-stage replay of the hybrid pipeline (September 2026)

Status: measurement, exploratory (no pre-registration), with identity checks.
The previous note established that the calibrated admission floor is not the
main cause of the tasks on which the hybrid pipeline scores below the raw
embedding, and left the loss "after admission — in the combination, in the
post-fusion stages, or in the candidate population", unseparated. This note
separates it. Every stage of the Track B path is scored on the same frozen
embeddings and lexical scores, per query, on three embedding models, and the
replay is pinned to the live pipeline in three ways before any number is read.
The theory that the result motivated is in the companion
[identifiability note](adaptive-fusion-identifiability.md). Nothing here is a
shipped behaviour.

**The result in five lines.**

1. **On the pure-ranking tasks the whole difference is the fusion step, and
   its sign depends on the model.** Admission and the gate contribute nothing
   (or a few tenths of a point) on SciFact, ESGReports, TMD, ReMe, MemBench and
   ConvoMem. On the mid and strong models the equal-vote reciprocal rank fusion
   of the dense list with the lexical list loses four to eight and two to seven
   points against the dense order; on the weakest model the same fusion, on the
   same frozen candidates, *gains* on SciFact, ESGReports and TMD and loses only
   on the three dialogue tasks. The sign flip the line was founded on is real
   and is not an artefact of pipeline versions. No lexical weight serves all
   three models: the best task-constant weight is 0 or 0.1 on the two stronger
   models and 0.5 to 1 on the weakest.
2. **The harm is not lexical rows breaking in.** On the losing tasks 92–100 %
   of the rows that enter the top ten under fusion were already on the dense
   list and were lifted by a lexical vote, and 86–100 % of the relevant rows
   pushed out had a lexical vote themselves. A third to two thirds of the loss
   is reordering inside the top ten with no membership change at all.
3. **Gorilla's loss is admission, not fusion**, and **EPBench's loss is the
   gate, not fusion.** On the mid model Gorilla loses 7.1 points at the floor
   and gains 5.2 in fusion; EPBench gains 9.7 in fusion and loses 10.2 at the
   pool-size gate, 99.6 % of it in the four corpus groups of 19–20 rows where
   the gate deletes every lexical-only row and cuts relevant dense rows too.
4. **QASPER is subset retrieval and shows all three stages at once**: with a
   median eligible universe of 45 documents the absolute floor empties the
   top ten for 31.8 % of the queries, lexical rows refill it, and the gate
   removes the refill. Its positive fusion delta is a replenishment effect —
   +8.4 on the starved queries, −2.2 on the rest — not a sign that the lexical
   arm ranks better there.
5. **The Track B regime is not gate-free.** The launcher turns off the
   calibrated gate and autocut, but the pool-size heuristic gate still runs
   inside `do_recall`, and on small pools it is the dominant loss. The
   benchmark documentation's statement that the truncation layers are off, and
   neutral at best, is wrong for that regime; it will be corrected together
   with the numbers it affects.

## 1. The instrument and its three identities

The replay (`benchmarks/frozen_replay.py`) stores each corpus exactly as the
Track B runner does — same schema, same FTS5 triggers, same dedup — and then,
for every query, computes the stages the recall path actually has:

| Stage | What it is | How it is computed |
| --- | --- | --- |
| S0 | dense-only ranking | cosine against the preloaded matrix the `--fast` path scores, stable argsort |
| S1 | S0 after the admission floor | rows with cosine ≥ threshold × 0.5; identical to what `_search_vector` hands the fusion |
| S2 | reciprocal rank fusion, before the gate | `_recall_rrf`'s arithmetic (K = 60, dense list then FTS list, stable sort), lexical list from the real `_search_memories_keyword` |
| S3 | after the heuristic gate | `_apply_quality_gate` with the pool-size `min_score`: cosine branch for rows with a dense vote, rank branch for lexical-only rows |

Every stage is filtered by the task's candidate subset where one exists and
scored with the harness's own `compute_ndcg`, so S3 is what the Track B runner
would have scored. Three identities pin the replay to the pipeline:

- **S0 equals Track A.** On the eleven tasks and the two models that have a
  raw-embedding record on this pipeline the dense-only stage reproduces it to
  within the subsample's resolution (largest deviation 0.6 on a 1,200-query
  subsample; ≤ 0.1 where every query is replayed). The weakest model's only
  raw-embedding record predates the current pipeline, so its S0 is the
  reference here rather than a check.
- **S3 equals the recorded Track B.** With the threshold pinned to the
  calibration record of the earlier arms (`--pin_from`), the seven tasks of
  that note reproduce their recorded Track B scores to within 0.005 — the
  rounding of the record — on every query of every task.
- **The orders match row for row.** Every twentieth query is additionally
  run through the live `_recall_rrf` and `do_recall`; their orders are
  compared with S2 and S3 over the first hundred rows. Across the eleven
  tasks and three models, 3,226 sampled queries out of 33,491 replayed, there
  were zero mismatches.

Two choices bound the cost without touching the top ten. Fusion is sorted
over a working set — every eligible row when a candidate subset applies,
otherwise the rows with dense rank or lexical rank below 3,000; a row outside
that set scores at most \((1+w)/(K+3001)\), less than the least any of the ten
best in-set rows can score, so the top ten and the identity checks are
unchanged. And the 10,000- and 5,867-query tasks are replayed on every k-th
query so that at most 200 per subtask are scored; those rows of the tables
below are therefore subsample means and are not the published Track B
numbers, which the full-query run reproduced exactly.

Conditions: cpersona at the commit this note ships with, Track B regime
(`rrf`, `auto_calibrate`, fused gate off, autocut off, `limit` = corpus size),
`--fast` with the numpy backend, embeddings from the per-model caches so no
text was encoded. jina-embeddings-v5-text-nano with its asymmetric prompts and
the threshold pinned to the earlier separation arm; BAAI/bge-m3 and
all-MiniLM-L6-v2 calibrated live with the default method. Eleven tasks: the
seven losing tasks of the calibration note, the two dialogue tasks on which
all three models lose (MemBench, ConvoMem), and two contrasts on which the
mid model gains (LongMemEval, MLDR).

## 2. Stage decomposition, three models, matched queries

adm = S1 − S0, fus = S2 − S1, gate = S3 − S2, all in NDCG@10 points, mean of
subtask means. H and C are the mean discounted NDCG lost and gained by the
fusion transition across queries (C − H = fus). "best w" is the lexical
weight in {0, 0.1, 0.25, 0.5, 0.75, 1} with the highest S2-style score, where
w = 0 means dense rows first and lexical-only rows filling the tail.

**jina-embeddings-v5-text-nano (mid)**

| task | S0 (= A) | S1 | S2 | S3 (= B) | adm | fus | gate | H | C | best w |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| SciFact | 82.18 | 82.18 | 76.81 | 76.81 | 0.00 | −5.37 | 0.00 | 8.46 | 3.08 | 0 |
| ESGReports | 49.11 | 49.11 | 41.19 | 41.19 | 0.00 | −7.92 | 0.00 | 11.95 | 4.03 | 0 |
| TMD | 30.04 | 30.04 | 23.59 | 23.72 | 0.00 | −6.44 | +0.12 | 9.00 | 2.56 | 0 |
| ReMe | 64.98 | 64.98 | 60.68 | 61.32 | 0.00 | −4.30 | +0.64 | 8.02 | 3.71 | 0.1 |
| MemBench | 69.78 | 69.11 | 64.71 | 65.50 | −0.67 | −4.40 | +0.78 | 8.70 | 4.30 | 0.1 |
| ConvoMem | 66.35 | 66.22 | 62.09 | 62.00 | −0.13 | −4.13 | −0.09 | 6.59 | 2.46 | 0 |
| QASPER | 48.84 | 43.96 | 46.11 | 42.43 | −4.88 | +2.15 | −3.68 | 5.50 | 7.65 | 0.1 |
| EPBench | 80.84 | 77.97 | 87.67 | 77.47 | −2.87 | +9.69 | −10.19 | 1.67 | 11.37 | 1.0 |
| Gorilla | 36.06 | 29.00 | 34.22 | 33.83 | −7.07 | +5.22 | −0.39 | 6.28 | 11.50 | 0.75 |
| LongMemEval | 77.42 | 77.42 | 80.09 | 80.76 | 0.00 | +2.67 | +0.67 | 3.27 | 5.93 | 0.5 |
| MLDR | 79.98 | 79.98 | 80.13 | 80.13 | 0.00 | +0.15 | 0.00 | 5.57 | 5.72 | 0.1 |

**BAAI/bge-m3 (strong)**

| task | S0 (= A) | S1 | S2 | S3 (= B) | adm | fus | gate | H | C | best w |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| SciFact | 76.49 | 76.49 | 74.33 | 74.33 | 0.00 | −2.17 | 0.00 | 6.86 | 4.69 | 0.1 |
| ESGReports | 40.74 | 40.74 | 41.36 | 41.36 | 0.00 | +0.62 | 0.00 | 7.91 | 8.52 | 0.25 |
| TMD | 27.92 | 27.92 | 23.00 | 23.00 | 0.00 | −4.92 | 0.00 | 8.19 | 3.27 | 0 |
| ReMe | 61.30 | 61.30 | 58.88 | 58.88 | 0.00 | −2.42 | 0.00 | 6.68 | 4.26 | 0.1 |
| MemBench | 71.05 | 71.01 | 64.07 | 64.31 | −0.04 | −6.93 | +0.23 | 9.42 | 2.48 | 0 |
| ConvoMem | 65.04 | 65.04 | 61.64 | 61.64 | 0.00 | −3.40 | 0.00 | 6.03 | 2.63 | 0 |
| QASPER | 51.98 | 51.98 | 47.84 | 48.00 | 0.00 | −4.14 | +0.16 | 8.00 | 3.86 | 0 |
| EPBench | 87.46 | 87.46 | 90.11 | 89.94 | 0.00 | +2.66 | −0.17 | 2.61 | 5.27 | 0.75 |
| Gorilla | 34.27 | 33.16 | 33.01 | 33.01 | −1.10 | −0.15 | 0.00 | 8.29 | 8.14 | 0 |
| LongMemEval | 78.26 | 78.26 | 79.78 | 79.78 | 0.00 | +1.52 | 0.00 | 3.54 | 5.06 | 0.25 |
| MLDR | 81.33 | 81.33 | 79.80 | 79.80 | 0.00 | −1.53 | 0.00 | 5.03 | 3.50 | 0 |

**all-MiniLM-L6-v2 (weakest)**

| task | S0 | S1 | S2 | S3 | adm | fus | gate | H | C | best w |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| SciFact | 70.00 | 70.00 | 71.33 | 71.33 | 0.00 | +1.33 | 0.00 | 5.07 | 6.41 | 0.5 |
| ESGReports | 26.27 | 25.34 | 35.54 | 35.54 | −0.93 | +10.19 | 0.00 | 3.98 | 14.18 | 1 |
| TMD | 10.98 | 10.98 | 15.13 | 15.13 | 0.00 | +4.16 | 0.00 | 4.83 | 8.98 | 1 |
| ReMe | 60.60 | 60.48 | 59.20 | 57.70 | −0.12 | −1.28 | −1.50 | 6.20 | 4.92 | 0.1 |
| MemBench | 66.15 | 64.92 | 62.60 | 62.79 | −1.23 | −2.32 | +0.19 | 8.07 | 5.75 | 0.1 |
| ConvoMem | 61.26 | 60.96 | 59.53 | 59.47 | −0.30 | −1.43 | −0.07 | 5.59 | 4.16 | 0.1 |
| QASPER | 40.37 | 38.30 | 42.69 | 40.15 | −2.07 | +4.38 | −2.53 | 6.05 | 10.44 | 0.75 |
| EPBench | 61.36 | 51.12 | 76.64 | 57.70 | −10.24 | +25.52 | −18.94 | 0.57 | 26.08 | 1 |
| Gorilla | 26.50 | 8.29 | 29.02 | 26.59 | −18.21 | +20.73 | −2.43 | 0.63 | 21.37 | 0.75 |
| LongMemEval | 67.65 | 67.65 | 76.79 | 75.62 | 0.00 | +9.14 | −1.17 | 2.66 | 11.80 | 1 |
| MLDR | 72.75 | 72.75 | 79.64 | 79.25 | 0.00 | +6.89 | −0.39 | 3.31 | 10.20 | 1 |

Reading across the models:

- The six pure-ranking tasks lose in fusion on the mid and strong models,
  and the strong model loses on QASPER and MLDR as well. The strong model's
  admission floor admits nearly the whole corpus (its cosines sit higher than
  the threshold the calibration places), so for it *everything* is fusion.
- On the weakest model the same fusion gains on SciFact (+1.3), ESGReports
  (+10.2), TMD (+4.2), QASPER (+4.4), LongMemEval (+9.1) and MLDR (+6.9) and
  loses only on ReMe, MemBench and ConvoMem. Its cosines are low, so its
  calibrated floor cuts deep — Gorilla loses 18.2 points and EPBench 10.2 at
  admission alone — and the pool-size gate takes 18.9 more from EPBench.
- The best task-constant lexical weight is 0 or 0.1 on 8 of 11 tasks for
  each of the two stronger models, and 0.5 or above on 8 of 11 for the
  weakest. The tasks where the stronger models still want a lexical vote are
  the ones where it genuinely corrects the dense arm — EPBench, LongMemEval,
  ESGReports on the strong model — and even there the best weight is below
  one on the strong model.
- Three tasks change sign across the three models on matched candidates:
  SciFact (+1.33 / −5.37 / −2.17), TMD (+4.16 / −6.44 / −4.92) and ESGReports
  (+10.19 / −7.92 / +0.62). QASPER's fusion delta is positive on the mid model
  only because of the replenishment described in section 4; on the strong
  model, where nothing is starved, it is a plain fusion loss of 4.1.

## 3. What the fusion step does to the top ten

The replay classifies every change of top-ten membership between S1 and S2.
Counts are summed over subtasks; percentages are the share of each row's
mechanism.

| task | model | intruders: lexical-only | intruders: on the dense list, lifted by a lexical vote | relevant out: no lexical vote | relevant out: had a lexical vote |
| --- | --- | --- | --- | --- | --- |
| SciFact | jina | 35 | 387 (91.7 %) | 3 | 21 (87.5 %) |
| SciFact | bge-m3 | 0 | 542 (100 %) | 8 | 14 (63.6 %) |
| SciFact | MiniLM | 23 | 455 (95.2 %) | 6 | 15 (71.4 %) |
| ESGReports | jina | 8 | 124 (93.9 %) | 2 | 12 (85.7 %) |
| ESGReports | bge-m3 | 1 | 133 (99.3 %) | 1 | 6 (85.7 %) |
| ESGReports | MiniLM | 9 | 112 (92.6 %) | 1 | 6 (85.7 %) |
| TMD | jina | 358 | 5,361 (93.7 %) | 0 | 1,472 (100 %) |
| TMD | bge-m3 | 1 | 6,002 (100 %) | 0 | 1,352 (100 %) |
| TMD | MiniLM | 671 | 7,593 (91.9 %) | 0 | 760 (100 %) |
| ReMe | jina | 25 | 2,767 (99.1 %) | 8 | 53 (86.9 %) |
| ReMe | bge-m3 | 14 | 2,879 (99.5 %) | 13 | 54 (80.6 %) |
| ReMe | MiniLM | 364 | 2,333 (86.5 %) | 5 | 48 (90.6 %) |
| MemBench | jina | 2,005 | 1,874 (48.3 %) | 121 | 183 (60.2 %) |
| MemBench | bge-m3 | 976 | 2,727 (73.6 %) | 170 | 344 (66.9 %) |
| MemBench | MiniLM | 3,418 | 1,487 (30.3 %) | 130 | 218 (62.6 %) |
| ConvoMem | jina | 733 | 1,252 (63.1 %) | 9 | 98 (91.6 %) |
| ConvoMem | bge-m3 | 17 | 1,775 (99.1 %) | 10 | 96 (90.6 %) |
| ConvoMem | MiniLM | 1,187 | 1,254 (51.4 %) | 7 | 109 (94.0 %) |

(The jina rows for SciFact, ESGReports, TMD and ReMe are from the full-query
run; the others from the matched 200-per-subtask run.)

On the four pure-ranking tasks the intruder is almost never a row the dense
arm had not seen, on any of the three models. It is a row the dense arm had
ranked lower which a lexical vote lifted past a relevant row — and the
relevant row usually had a lexical vote of its own, just a weaker one. The
same mechanism runs in both directions: on the weakest model, where fusion
gains on SciFact and TMD, the rows that enter the top ten are still 92–95 %
dense rows lifted by lexical votes; there the lifted rows are more often the
relevant ones. Whether the lexical reordering of dense candidates is right is
what changes with the model — not whether lexical-only rows break in. MemBench and ConvoMem, whose corpora are
one to two orders of magnitude larger, have a larger lexical-only share on the
mid model, and the strong model removes most of it. The membership taxonomy
also misses part of the loss: queries in which the relevant rows stay in the
top ten but move down account for 55 % of the fusion loss on SciFact, 31 % on
ESGReports, 7 % on TMD and 64 % on ReMe (mid model). Suppressing lexical-only
candidates would therefore not repair the losing tasks; the companion note
derives what would.

The lexical-weight sweep says the same thing from the other side (mean
NDCG@10 at each weight, mid / strong / weakest model):

| task | w = 0 | 0.1 | 0.25 | 0.5 | 0.75 | 1 (shipped) |
| --- | --- | --- | --- | --- | --- | --- |
| SciFact | 82.18 / 76.49 / 70.00 | 81.32 / 77.10 / 70.77 | 80.72 / 76.81 / 70.75 | 79.78 / 75.51 / 71.77 | 77.66 / 75.08 / 71.02 | 76.81 / 74.33 / 71.33 |
| TMD | 30.04 / 27.92 / 10.98 | 29.59 / 27.92 / 11.12 | 29.00 / 27.42 / 11.82 | 27.55 / 26.38 / 12.81 | 25.46 / 24.85 / 14.01 | 23.59 / 23.00 / 15.13 |
| MemBench | 69.49 / 71.02 / 65.88 | 69.75 / 70.41 / 66.49 | 69.09 / 68.77 / 65.88 | 67.43 / 66.69 / 64.59 | 65.98 / 65.14 / 63.59 | 64.71 / 64.07 / 62.60 |
| ConvoMem | 66.32 / 65.04 / 61.22 | 65.77 / 64.76 / 61.42 | 65.12 / 64.19 / 61.31 | 63.98 / 63.30 / 60.83 | 62.80 / 62.32 / 59.95 | 62.09 / 61.64 / 59.53 |
| EPBench | 81.98 / 87.46 / 67.34 | 83.85 / 88.70 / 69.78 | 85.94 / 89.66 / 72.55 | 87.21 / 90.03 / 74.84 | 87.58 / 90.26 / 76.03 | 87.67 / 90.11 / 76.64 |
| LongMemEval | 77.42 / 78.26 / 67.65 | 79.58 / 79.64 / 71.92 | 80.30 / 80.21 / 75.11 | 80.63 / 80.09 / 75.72 | 80.23 / 80.05 / 76.43 | 80.09 / 79.78 / 76.79 |

On the mid and strong models the losing tasks decline monotonically
(ESGReports on the mid model has one non-monotone step, 43.62 at 0.5 against
43.71 at 0.75, and still peaks at zero) and the gaining tasks peak below one
on at least one of them. On the weakest model SciFact and TMD *rise* with the
weight, while MemBench and ConvoMem still peak at 0.1. There is no single
weight that is right across the families or across the models, which is the
fixed-weight problem this line set out to remove — but the sweep also shows
how much of the stronger models' loss a small constant already recovers.

## 4. The two mechanisms that are not fusion

**Admission empties small universes.** QASPER's candidate file restricts each
query to one paper: 7 to 294 eligible documents, median 45. Against that
universe the calibrated floor (0.252 for the mid model) leaves fewer than ten
admitted rows for 425 of 1,335 queries (31.8 %) and none at all for 66. Split
by that condition:

| QASPER, mid model | queries | S0 | S1 | S2 | S3 | fus |
| --- | --- | --- | --- | --- | --- | --- |
| fewer than 10 admitted | 425 | 52.83 | 39.48 | 47.92 | 39.93 | +8.44 |
| 10 or more admitted | 910 | 46.61 | 46.61 | 44.45 | 45.05 | −2.16 |
| none admitted | 66 | 36.46 | 0.00 | 29.72 | 0.96 | +29.72 |

The lexical arm's contribution on QASPER is refilling a list that admission
emptied; where the dense list is intact, fusion loses as it does elsewhere.
Gorilla's mid-model loss is the same mechanism without the subset: its
tensorflow group has 55 rows and a floor of 0.445, and admission alone costs
7.1 points. The weakest model's cosines are lower still and its calibrated
floor cuts deeper: Gorilla loses 18.2 points and EPBench 10.2 at admission on
it. The strong model's higher cosines put its floor below almost every row, so
neither effect appears there.

**The gate deletes the lexical arm on small pools.** The Track B launcher sets
`CPERSONA_FUSED_GATE_ENABLED=false` and `CPERSONA_AUTOCUT_ENABLED=false`, but
`do_recall` applies `_apply_quality_gate` regardless; with no calibrated gate
it uses the pool-size threshold
\(m(P)=0.5-0.3\min(1,\log(P+1)/\log 500)\). A lexical-only row's fused score
is \(1/(61+r_l)\) and the gate compares it with \(3m(P)/61\), so no
lexical-only row survives once \(m(P)>1/3\), that is for pools of **30 rows or
fewer**; at 196 rows the cut falls at lexical rank 21, at 500 or more at rank
40. The cosine branch applies \(m(P)\) to rows with a dense vote, and for small
pools that exceeds the admission floor and removes relevant dense rows as
well. EPBench, whose nine corpus groups range from 19 to 1,967 rows, shows the
consequence (mid model, per group):

| EPBench group | pool | S2 | S3 | gate |
| --- | --- | --- | --- | --- |
| default, Claude, short | 19 | 91.18 | 51.51 | −39.67 |
| default, GPT-4o, short | 20 | 92.92 | 67.47 | −25.45 |
| sci-fi, Claude, short | 20 | 89.83 | 57.20 | −32.63 |
| world-news, Claude, short | 20 | 91.72 | 53.01 | −38.71 |
| default, Claude, long | 196 | 76.48 | 75.91 | −0.57 |
| the other four groups | 196–1,967 | — | — | 0.00 to −0.01 |

The four 19–20-row groups carry 99.6 % of the task's gate loss. The published
Track B figure for EPBench on this model, 3.4 points below the raw embedding,
is a fused score 6.8 points *above* it that the gate reversed. On the weakest
model the gate takes 18.9 points from EPBench, on top of the 10.2 that
admission took. On the strong model the same groups lose only 0.17 in total: its dense rows carry cosines
well above \(m(P)\) and dominate the top ten, so the deleted lexical-only rows
were rarely in it. The gate is therefore not a fixed cost of the regime but a
model- and pool-dependent one, which is the worst kind to leave undocumented.
The regime description in the benchmark README (truncation layers off, neutral
at best) is wrong on both counts and will be corrected; whether the regime is
redefined to bypass the heuristic gate as well, which moves the published
small-corpus numbers, is a separate decision recorded in the benchmark
documentation when it is taken.

## 5. What the maximum cosine does not predict

If fusion harmed only queries the dense arm already answers well, a query-level
switch on the dense arm's confidence would repair it. The replay records the
maximum cosine over the corpus for every query. Binned into quartiles, the
fusion delta is *largest in magnitude in the lowest quartile* on both families
(mid model: SciFact −11.5 in Q1 against −4.0 in Q4; EPBench +30.4 against
+5.4). A one-parameter switch — dense order when the maximum cosine is at or
above τ, fused order otherwise — at its best in-sample τ gains at most 0.09
(SciFact), 0.83 (QASPER) and 0.55 (Gorilla) over the better of the two
constant policies on the mid model and zero elsewhere; on the strong model
the largest gain is 0.78 (Gorilla). The per-query oracle, choosing the better
of the two orders for each query, is 3 to 6 points above either constant on
every task. The information that would decide per query exists; the maximum
cosine does not carry it. The companion note gives the reason: the decision
depends on the lexical evidence conditional on the dense score, which no
per-arm statistic determines.

## 6. Corrections to earlier notes

- The calibration note left the loss "after admission" without locating it.
  It is now located: in fusion for the pure-ranking tasks, at the floor for
  Gorilla, at the gate for EPBench, and in all three for QASPER.
- The calibration note read EPBench's and Gorilla's losses as dense-arm
  starvation. Starvation is real (section 4) but for EPBench the gate is the
  larger term by a factor of three.
- The first derivation's remark that the mixture rule "silences lexical
  votes" when the dense arm is confident describes a row's score, not the
  order; the companion note corrects it and shows the rule is symmetric in the
  arms.
- The earlier expectation that Gorilla is a task of structural lexical harm
  is refuted on the mid model, where fusion helps Gorilla by five points, and
  on the weakest, where it helps by twenty; on the strong model fusion is
  neutral there.
- The model-strength sign flip, previously known only from runs of different
  pipeline versions, is confirmed on matched candidates: SciFact, TMD and
  ESGReports gain under fusion on the weakest model and lose on the two
  stronger ones, and QASPER's balance of gain and loss moves from positive to
  negative with model strength, as the first derivation predicted.

## 7. What this hands to the design

1. The object to design is not an arm weight but the **lexical evidence
   conditional on the dense score** (companion note, section 4); a per-arm
   calibration cannot supply it. One construction that can, without a fitted
   weight, is the joint density ratio of section 5 there; its assumptions are
   the next measurement.
2. Two losses need no adaptivity: a **reservation invariant** (no stage may
   leave fewer than \(\min(10,n)\) rows while eligible candidates exist)
   removes the QASPER, Gorilla and EPBench mechanisms, and the pool-size gate's
   small-pool behaviour is a defect to remove rather than tune.
3. A small constant lexical weight recovers most of the pure-ranking loss on
   the mid and strong models and would cost the weakest model most of its
   gains: there is no constant that serves all three, which is the measured
   form of the fixed-weight problem. The adaptive layer's target is therefore
   both the gap to the per-query oracle and the gap between models, and the
   success condition — above the raw embedding on both endpoint models,
   22-task mean — must be evaluated against the dense order, not against the
   admitted or gated list.
4. The next instrument is a **row-keyed dump**: per row, both raw scores,
   both null exceedance probabilities, arm presence and censoring, the
   relevance label and the rank at every stage. The per-query summaries here
   cannot evaluate the reversal conditions of the companion note or estimate
   the conditional evidence.

## 8. Reproduction

```bash
# mid model, threshold pinned to the recorded calibration arm of the previous note
LMEB_DIR=~/lmeb python benchmarks/frozen_replay.py \
    --model_path jinaai/jina-embeddings-v5-text-nano \
    --emb_cache_dir ~/lmeb/embcache_jinanano --trust_remote_code --default_task retrieval \
    --pin_from ~/lmeb/trackb_cal_jinanano_separation_r1 \
    --tasks LMEB_SciFact,ESGReports,QASPER,TMD,EPBench,ReMe,Gorilla,MemBench,ConvoMem,LongMemEval,MLDR \
    --out_dir replay_jinanano --max_queries_per_subtask 200
# strong model, live calibration
LMEB_DIR=~/lmeb python benchmarks/frozen_replay.py \
    --model_path BAAI/bge-m3 --emb_cache_dir ~/lmeb/embcache_bgem3_p1 --emb_cache_model BAAI/bge-m3 \
    --tasks <same list> --out_dir replay_bgem3 --max_queries_per_subtask 200
# weakest model
LMEB_DIR=~/lmeb python benchmarks/frozen_replay.py \
    --model_path sentence-transformers/all-MiniLM-L6-v2 --emb_cache_dir ~/lmeb/embcache_minilm \
    --emb_cache_model sentence-transformers/all-MiniLM-L6-v2 \
    --tasks <same list> --out_dir replay_minilm --max_queries_per_subtask 200
# tables of sections 2, 3 and 5
REPLAY_ROOT=. python benchmarks/replay_summary.py jinanano=jina-v5-nano bgem3=bge-m3 minilm=MiniLM
REPLAY_ROOT=. python benchmarks/replay_query_analysis.py jinanano=jina-v5-nano bgem3=bge-m3 minilm=MiniLM
```

Omit `--max_queries_per_subtask` to replay every query; with it omitted the
weak-model run reproduced the seven recorded Track B numbers to within 0.005.
The per-query records (`<task>.queries.jsonl`) carry, for each query, the
NDCG@10 at every stage, the admitted and lexical list lengths inside and
outside the candidate subset, the ranks of the relevant rows on each arm, the
maximum cosine and the top-ten moves.

## 9. By-products

- **Duplicate corpus rows collapse at store time**, as the shipped dedup
  requires: QASPER stores 20,198 of its 65,300 lines, ConvoMem 490,432 of
  500,221. Track A scores the full file. This is why S0 differs from Track A
  by a few hundredths on those tasks and why any comparison of the two tracks
  at the row level must use the stored population.
- MLDR's Track B on the current code is 80.13 on the mid model against the
  78.0 recorded on the 2.4.40 harness; the drift between harness versions
  noted in the calibration note is real on this task too and is not
  addressed here.
- The lexical arm's `LIKE` fallback (a query with no term of three or more
  characters) fired on none of the 33,491 replayed queries; every lexical
  list here is a bm25 list.
