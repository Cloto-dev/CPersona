# Results — what the shipping line did to the score since 2.4.40

Pre-registration: `prereg-version-comparison-2440-to-2512.md` (with its two
amendments). This file is written in the order the arms finish: the third arm
finished first, because it turned out to need seven tasks rather than
twenty-two, and its section was recorded while the second arm — the
development head `e43ad34` on all 22 tasks — was still running. The second
arm's section follows it now that it is complete.

## Second arm: the development head on all 22 tasks (2026-09-12)

The run the pre-registration describes: the development head at package
commit `e43ad34` (the harness commit was `a68c646`, which is `e43ad34` plus a
harness-only change so that an empty default prompt does not alter the
embedding cache key), `run_trackb.sh --fast` on the bare cache, started
2026-09-11 09:39 and finished 2026-09-12 01:51. It shared the machine with
two other benchmark runs for part of that time, which affects the clock and
nothing else. The records are under `trackb_results_dev_e43ad34_bgem3/`.

**The invalidation conditions, checked before the table was read:** every
batch in every task hit the embedding cache in full (zero `cache: k/n` lines
with k < n across the run); the `--fast` self-check reported no mismatch;
twenty-two task records landed. The run stands.

| task | v2.4.40 | second arm `e43ad34` | Δ |
| --- | ---: | ---: | ---: |
| REALTALK | 43.04 | 38.01 | −5.03 |
| KnowMeBench | 51.62 | 47.40 | −4.22 |
| LoCoMo | 45.91 | 41.85 | −4.06 |
| DeepPlanning | 56.70 | 53.89 | −2.81 |
| NovelQA | 36.11 | 33.83 | −2.28 |
| LooGLE | 64.12 | 61.99 | −2.13 |
| ToolBench | 52.22 | 50.11 | −2.11 |
| TMD | 25.18 | 23.20 | −1.98 |
| ESGReports | 43.23 | 41.38 | −1.85 |
| CovidQA | 83.93 | 82.34 | −1.59 |
| LongMemEval | 81.17 | 79.79 | −1.38 |
| MemBench | 65.61 | 64.59 | −1.02 |
| PeerQA | 30.04 | 29.10 | −0.94 |
| MLDR | 80.66 | 79.80 | −0.86 |
| ConvoMem | 61.60 | 60.80 | −0.80 |
| ReMe | 59.61 | 58.96 | −0.65 |
| LMEB_SciFact | 74.87 | 74.32 | −0.55 |
| EPBench | 90.23 | 89.94 | −0.29 |
| Proced_mem_bench | 52.13 | 52.13 | +0.00 |
| MemGovern | 89.04 | 89.16 | +0.12 |
| Gorilla | 32.90 | 33.03 | +0.13 |
| QASPER | 48.56 | 48.71 | +0.15 |
| **mean (22 tasks)** | **57.66** | **56.11** | **−1.55** |

**Verdict, by the rule fixed before the run.** The overall mean fell, from
57.66 to 56.11. That is a **regression**, and it is reported in that word.
Eighteen tasks fell, three rose by at most 0.15, one did not move. There is
no per-task story under which this is "mixed": the tasks that fell are not a
minority, they are all but four.

**Orientation, conditional on the task-population model the pre-registration
declines to claim:** the per-task difference has mean −1.55 and standard
deviation 1.46, so the resolution fixed in advance is 0.6264 × 1.46 = 0.91
points and the observed mean shift is 1.7 times it; a paired *t* over the 22
differences is −5.00 (p = 6 × 10⁻⁵). The deterministic estimand needs none
of this: the 22-task difference is −1.55 and is determined.

**The one task past 5 points.** REALTALK fell 5.03, and it fell on all three
of its subtasks by about the same amount (commonsense −3.99, multi_hop −5.24,
temporal_reasoning −5.89) on one corpus group of 8,944 rows. A loss that is
uniform across subtasks that ask different things is the shape of a change
in what gets admitted or selected, not of a change in how fused candidates
are ordered for a particular kind of query; the pre-registration's named
candidates on the gate and selection side (the bug-183/184 gate, the ranking
of rows without an age, the two-phase scan) are where a bisection would look
first. This run's calibrated threshold on that corpus was 0.6602; the 2.4.40
record carries no calibration record, so whether the threshold moved between
the arms is not known from the records. **This is a hypothesis about where to
look, not a finding**: the design cannot attribute the delta to a cause.

**Where the LongMemEval part of it sits.** The per-question-type instrument
built alongside this measurement (`results-longmemeval-by-type-baseline.md`,
on its own branch until merged) reads the −1.38 on LongMemEval as three
question types — `single_session_user` −3.96, `temporal_reasoning` −2.32,
`multi_session` −1.75 — with the other three flat, in the full-ranking regime
and again with ten rows over one scene's history. The same reading, under the
production regime, of the same regression.

**What follows.** The pre-registration's exits, in order: this table (done);
a bisection over the recall-path commits between the arms for the tasks that
moved most; and a gate, so that the next loss of this size is seen when it
lands rather than two months later — the gate is defined beside the
per-question-type instrument.

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

## Bisection: punctuation handling, 2026-09-15

The REALTALK regression is caused by the `_build_fts_query` punctuation change
in `a5206f60f2f965adb4c2a5e89ef95649c45d36a2`, not by calibration drift.
This is a measured diagnostic finding, **not a recommendation to revert the
commit**. Its identifier-search correction remains necessary.

Protocol: [fixed-calibration preregistration](prereg-recall-bisection.md).
All arms used the same read-only BAAI/bge-m3 cache, 8,944 input documents
(8,943 stored after the existing dedup), 679 queries, full pooled ranking,
threshold 0.6602, RRF and confidence/autocut/fused gate off. No calibration
samples were drawn. NDCG@10 below is the three-subtask macro mean, in percentage
points. These are not production-limit or latency measurements.

| Revision / intervention | Commonsense | Multi-hop | Temporal | Macro |
| --- | ---: | ---: | ---: | ---: |
| v2.4.40 (`888614f`) | 28.869513 | 29.316193 | 70.946637 | 43.044114 |
| `68653f3` | 28.869513 | 29.316193 | 70.946637 | 43.044114 |
| `5499613` | 28.869513 | 29.316193 | 70.946637 | 43.044114 |
| Parent of `a5206f6` (`0b6b759`) | 28.869513 | 29.316193 | 70.946637 | 43.044114 |
| `a5206f6` | 24.884225 | 24.083188 | 65.059915 | 38.009109 |
| Development endpoint (`e43ad34`) | 24.884225 | 24.083188 | 65.059915 | 38.009109 |
| `e43ad34`, punctuation normalization restored | 28.869513 | 29.316193 | 70.946637 | 43.044114 |

The endpoints reproduce the historical rounded values at the same fixed
threshold. The adjacent parent/commit pair isolates the transition; reversing
only punctuation normalization on the development endpoint restores all
5.035005 points, with zero residual in every subtask. The vector-admission
summaries are identical across these arms. Later checkpoints were unnecessary
after the transition and intervention succeeded. `5499613` changes version
metadata only; `eafd3af` adds benchmark validation, not runtime indexing.

The accelerated arms each checked 11 native vector calls out of 679 and had
zero mismatches. Two additional **unaccelerated** full REALTALK runs reproduce
the development and intervention scores exactly, removing acceleration as an
explanation for this finding. An independent qrels/ranked-ID implementation
also reproduces every per-query and aggregate score for those two runs and
the parent/intervention accelerated runs (679 queries each).

### Mechanism and the constraint on a repair

The trigram FTS builder keeps each non-CJK whitespace token whole. Removing
punctuation normalization made sentence punctuation part of the required
phrase: for example, `salmon?` is searched as `"salmon?"`, not `"salmon"`.
The query builder changes on 678/679 REALTALK queries and 499/500 LongMemEval
queries. This changes lexical membership/ranks and therefore RRF voting even
when vector admission is unchanged. Preserving embedded punctuation is useful
for identifiers, but preserving sentence delimiters has a different effect.

The restoration is not uniformly beneficial per query:

| REALTALK subtask | Improved | Worsened | Unchanged |
| --- | ---: | ---: | ---: |
| Commonsense | 22 | 12 | 69 |
| Multi-hop | 86 | 42 | 137 |
| Temporal | 56 | 26 | 229 |

The existing `tests/test_bug215_fts_punctuation.py` passes 5/5 on unchanged
`e43ad34`. The diagnostic intervention fails 4/5, including the actual
keyword-retrieval assertion: the CVE identifier row disappears behind the
FTS early return while noise rows remain. This is the original failure,
not an import/setup error. **Do not ship the blanket normalization revert.**
A repair must retain punctuated identifiers while recovering useful natural
language terms, with both contracts and retrieval outcomes measured.

Full-precision completed-arm scores, input hashes and source fingerprints are
in [recall-bisection-results.json](recall-bisection-results.json).
The reversible diagnostic patch is
[fts-diagnostic-intervention.patch](fts-diagnostic-intervention.patch).
The main working checkout and deployed code were not modified.

### LongMemEval confirmation

Both arms completed, and independent per-query verification finished at
2026-09-15 06:47:32 UTC. Each arm evaluated all 500 queries over 237,655 input
documents (237,654 stored after dedup), with threshold fixed at 0.4493 and
the same full-ranking regime. Each checked 10 native vector calls, with zero
mismatches and zero accelerator fallbacks. This was a paired causal check,
not another random calibration draw or a production-limit benchmark.

| Type | Queries | Development | Punctuation restored | Delta |
| --- | ---: | ---: | ---: | ---: |
| Knowledge update | 78 | 92.190023 | 92.117922 | -0.072101 |
| Multi-session | 133 | 77.482887 | 79.236651 | +1.753764 |
| Single-session assistant | 56 | 91.396168 | 91.113784 | -0.282384 |
| Single-session preference | 30 | 58.686234 | 59.284564 | +0.598330 |
| Single-session user | 70 | 81.015351 | 84.970804 | +3.955453 |
| Temporal reasoning | 133 | 77.967371 | 80.291278 | +2.323907 |
| **Six-type macro mean** | **500** | **79.789672** | **81.169167** | **+1.379495** |

All three preregistered fingerprint types improve. All six restored scores
match the historical v2.4.40 result at its recorded two-decimal precision;
the small changes in the other three types are reported rather than hidden.
The independent verifier recomputed all 1,000 query scores and both macro
means from ranked IDs and qrels. Full-precision results and fingerprints are
in [recall-bisection-longmemeval-results.json](recall-bisection-longmemeval-results.json).
The measured wall times were 9,797.48 and 9,843.02 seconds; the two arms ran
concurrently. These are diagnostic execution times, not shipped latency claims.

### What has been recovered, and what has not been combined

This experiment restores the old retrieval quality on the two measured tasks
while leaving the rest of the development checkout in place. It is **not**
a whole-repository rollback to v2.4.40. However, it does **not** yet combine
that quality with all improvements from the 2.5 line: the local reversal
reintroduces the identifier-search failures demonstrated above. Other 2.5
changes remain in the source, but were not comprehensively revalidated here.

The diagnostic attribution is complete; when this section was written, a
production repair was not implemented.
The next repair must recover useful natural-language terms without losing
punctuated identifiers. There is no claim of statistical significance from
this single controlled pair, no attribution of the entire 22-task regression,
and no generalization to other embeddings or the production limit=10 regime.
The diagnostic reversal itself was never committed or released.

**Follow-up exploration, completed later on 2026-09-15:** the
[punctuation-policy comparison](results-fts-policy-exploration.md) selects an
edge-delimiter policy that passes 29 identifier/retrieval tests, reaches
42.671995 on REALTALK and 81.872540 on LongMemEval. All 500 LongMemEval queries
completed with independent verification; all six type means improve over
the development control. This is a separate candidate, not the diagnostic
reversal above, and it has not completed production acceptance.

**Later, 2026-09-15:** the `edges` policy shipped in the 2.6.0a1 pre-release
([release PR #271](https://github.com/Cloto-dev/CPersona/pull/271)). The
REALTALK and LongMemEval figures above were measured on the `e43ad34` base, not
on the tagged package, and the 22-task comparison has not been re-run since.

### Reproduction and verification

Use `benchmarks/diagnose_recall_revision.py` with a clean revision checkout,
an existing BAAI/bge-m3 embedding cache, the LMEB directory and a new output
directory. The cache must be a quiescent snapshot with no WAL. It is opened
with SQLite `mode=ro&immutable=1`; its stat is checked again after the run.
Every arm retains its experimental DB, manifest, rankings and scores.
Initial `mode=ro` attempts stopped before scoring because SQLite could not
create sidecars in a read-only location; those failed arms are not evidence.

```sh
python benchmarks/diagnose_recall_revision.py \
  --repo /path/to/revision --output /path/to/new-arm \
  --cache /path/to/embcache.sqlite3 --lmeb /path/to/lmeb \
  --task REALTALK --threshold 0.6602
python benchmarks/verify_recall_diagnostic.py \
  --lmeb /path/to/lmeb /path/to/new-arm
```

For LongMemEval use `--task LongMemEval --threshold 0.4493`. Add `--native`
to bypass acceleration. The pre-2.5.0 literal 100-row clamp is bypassed only
for this historical full-ranking protocol. No production settings change.

The independent verifier's rank discount was deliberately changed from
`log2(rank + 2)` to `log2(rank + 3)`: it failed on `scene_9_q_4` with a
per-query score mismatch, exit 1. Restoring the discount returned both
native REALTALK arms to green. This checks detection of a subtle scoring
error rather than merely successful execution. No existing tests were weakened.
