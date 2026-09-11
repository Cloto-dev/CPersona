# Results — what the shipping line did to the score since 2.4.40

Pre-registration: `prereg-version-comparison-2440-to-2512.md` (with its two
amendments). This file is written in the order the arms finish. The second
arm — the development head `e43ad34` on all 22 tasks — was still running when
the section below was recorded (three tasks scored), so its table is not here
yet; what is here is the third arm, which finished first because it turned out
to need seven tasks rather than twenty-two.

## Third arm: the line with its 2.6 work merged (2026-09-11)

The merge of the two unmerged runtime branches (depth/count separation, and
the reservation with its gate change) on top of `e43ad34`; 2405 tests pass.
Same protocol, model, cache and regime as the second arm. Every batch in every
run below hit the embedding cache in full.

| Task | v2.4.40 | second arm `e43ad34` | third arm (merged) | third − second | pool sizes |
|---|---|---|---|---|---|
| EPBench | 90.23 | 89.94 | **90.11** | **+0.17** | 19–1,967 |
| KnowMeBench | 51.62 | 47.40 | 47.40 | 0.00 | 6,644–11,995 |
| LoCoMo | 45.91 | 41.85 | 41.85 | 0.00 | 5,882 |
| MLDR | 80.66 | 79.80 † | 79.80 | 0.00 | 1,536 |
| ReMe | 59.61 | 58.96 † | 58.96 | 0.00 | 96–218 |
| Gorilla | 32.90 | 33.04 † | 33.04 | 0.00 | 43–907 |
| Proced_mem_bench | 52.13 | 52.13 † | 52.13 | 0.00 | 336 |

† The second-arm value comes from a separate short run of the same build
(`e43ad34`, same launcher, same regime), taken while the full second-arm run
was still on its fourth task. The full run will publish its own number for
each; the two must agree to the last digit, as a replay of four EPBench
subtasks did against the full run's record. The per-task JSON of the third arm
is in `trackb_results_thirdarm_bgem3/`.

One more disclosure, from Amendment 3: every run in this section looked the
cache up under prompt-tagged keys (the harness defect that amendment fixes), so
its vectors were re-encodes written under today's settings by an earlier
replay, not the baseline's own vectors. Bare and tagged vectors for the same
text agree to a cosine of 0.999997 or better, and the reading below rests on
row membership inside a cosine band a hundred times wider than that; it does
not depend on the difference. The second arm's full run, restarted on the
fixed harness, uses the baseline's vectors.

Every task but one reproduced the second arm to the digit, subtask by subtask.
The one that did not is EPBench, and within it only the twelve subtasks whose
corpus has 19 or 20 rows: eight rose, four fell, and the 36 subtasks with
196 rows or more were identical row for row.

### Why EPBench moved: the registered reading that was wrong

The pre-registration predicted that the third arm reproduces the second on
every task, on four readings of why the merged work is inert under this
protocol. The fourth was wrong. It said the gate change — no pool-size
heuristic applied to a fused row — is out of reach because every calibrated
admission floor in the run (0.27–0.36) sits above a heuristic of 0.20. But
0.20 is not the heuristic; it is the heuristic's value at a pool of 500 rows
and beyond. The function is `0.5 − 0.3·log(n+1)/log(500)`: 0.353 at 20 rows,
0.245 at 196, 0.20 from 500 up. The 2.5 gate keys a fused row on its cosine,
so on a small pool a row the dense arm had admitted past the calibrated floor
was then refused by the heuristic. The fused order is not the cosine order,
so those rows are not a suffix of the list; re-admitting them re-orders the
top ten in both directions.

Verified by replaying the four EPBench short-corpus subtasks that moved on
both builds (3 seconds each; the replay reproduced both runs' numbers to the
digit, no cache miss):

| Group | rows | floor | heuristic | rows returned, 2.5 gate | 2.6 |
|---|---|---|---|---|---|
| default_claude_short | 19 | 0.271 | 0.355 | 8.7 (mean) | 19.0 |
| default_gpt4o_short | 20 | 0.327 | 0.353 | 13.1 | 20.0 |
| world_news_claude_short | 20 | 0.273 | 0.353 | 10.6 | 20.0 |

All 1,911 re-admitted rows carrying a cosine sat in `[floor, heuristic)`;
none sat outside it; the 369 re-admitted rows without a cosine were lexical-only
rows the rank branch had refused for the same reason. Every row the 2.5 gate
kept had a cosine at or above the heuristic.

### Amendment 2's prediction, scored

Amendment 2 named the groups where the heuristic exceeds the floor, from the
frozen replay's recorded floors: four in EPBench, two in ReMe, one in MLDR. It
predicted that MLDR and ReMe move and nothing else does. The second half held
— KnowMeBench, LoCoMo, Gorilla and Proced_mem_bench reproduced to the digit —
and the first half did not: MLDR and ReMe were identical too.

- MLDR is not reachable on the live calibration. The replay's floor for it
  was 0.196; the live run calibrated a threshold of 0.4063, a floor of 0.203,
  above the heuristic's 0.20 at 1,536 rows.
- ReMe's two groups (99 and 110 rows) are reachable, and the mechanism does
  fire there: a dump of both builds' rankings shows 14 and 7 re-admitted rows
  inside the top twenty across 198 and 218 queries, every one inside
  `[floor, heuristic)`. They changed the top ten of 2 queries and the NDCG@10
  of none, because these pools return twenty or more qualified rows and the
  re-admitted rows land below them.

So the reading is narrower than Amendment 2 put it: the heuristic sitting
above the floor is necessary for a move, not sufficient. A move needs the
re-admitted rows to reach the top ten, which happens when the qualified rows
are fewer than ten — automatic on a 19-row pool that the gate had cut to
nine, and not the case on a 99-row pool. In this suite that is EPBench's four
short-corpus groups and nothing else.

### What this settles, and what it does not

- The full 22-task third arm is not run. Seven tasks, chosen to cover every
  group where the mechanism can fire and four where it cannot, agree with the
  reading above; the remaining fifteen tasks have no group under 311 rows and
  no group where the heuristic exceeds the floor.
- The second arm's regression against 2.4.40 (KnowMeBench −4.22, LoCoMo
  −4.06, DeepPlanning −2.81, EPBench −0.29, MLDR −0.86, ReMe −0.65) is carried
  unchanged into the merged line: the unmerged 2.6 work neither causes it nor
  repairs it under this protocol.
- Whether the re-admission is an improvement is not answered. One task moved
  by +0.17 with subtasks on both sides. The mechanism's real domain is the
  regime the pre-registration said this protocol does not look at — a
  caller's ten rows on a pool small enough that the old gate emptied it — and
  a measurement of that needs a protocol built for it.
