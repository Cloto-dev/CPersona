# Results — LongMemEval by question type: the two baselines (2026-09-11)

Instrument: `benchmarks/run_longmemeval_by_type.sh` and
`benchmarks/longmemeval_by_type.py` (see `benchmarks/README.md`, "LongMemEval
by question type, under two regimes"). This file records the first two arms the
instrument was pointed at, so that later work on the recall path has a reading
to be compared against per question type rather than as one mean.

## Arms

| arm | checkout | what it is |
| --- | --- | --- |
| `v2.4.41` | tag `v2.4.41` (`784807a`) | the last 2.4 final; behaviour-identical to 2.4.40, the oldest Track B record in the repository (2026-07-10) |
| `dev-e43ad34` | `e43ad34` (master at the time) | the development head; the same package that the version-comparison measurement calls its second arm |

Both arms ran the harness at this branch's commit (`fdc9742` and its
predecessors), with `CPERSONA_REPO` pointing at the checkout under measurement.
bge-m3, float16, budget batching, rrf, live calibration, the bare embedding
cache. The two arms ran concurrently on one machine alongside a third
benchmark; wall-clock times in the records are therefore not representative
and are not what this measures.

## Regimes

- **`full`** — the pooled corpus (all 500 scenes in one store, ~476 sessions
  each, 237,655 in total), limit = corpus size, autocut and the fused gate off.
  The Track B regime. On the 2.4 checkout `--unclamp_limit` lifted the
  limit=100 clamp (the harness logged "pre-2.5.0 checkout, bug-032 limit=100
  clamp bypassed"); on the development head it logged the no-op.
- **`limit10`** — one scene's history as the haystack (`--isolate_scenes`:
  each scene stored as its own channel, recall inside the query's scene),
  limit = 10, autocut and the fused gate at their shipped defaults (on). What a
  caller of the `recall` tool receives over their own memory.

The regimes differ in haystack, limit and gates. A number is compared across
arms within a regime, never across regimes.

## Reproduction check

Before anything is read from the deltas: does the harness, driven this way,
reproduce the records that already exist? Every one of the twelve
full-regime subtask values equals the value previously recorded for that build
to two decimals — the six for `v2.4.41` equal the 2026-07-10 Track B record
(`trackb_results_v2440_bgem3/LongMemEval.json`), and the six for `dev-e43ad34`
equal the version-comparison run of the same package taken earlier the same
day. The full-regime instrument therefore measures what Track B measures, and
the `--unclamp_limit` path is confirmed to give the 2.4 build its full ranking.

The per-question-type reader also recomputed every NDCG@10 from the ranking
dump and found it equal to the harness's recorded value (it refuses to
tabulate otherwise).

## `full` regime

| question type | n | v2.4.41 NDCG@10 | v2.4.41 R@5 | v2.4.41 R@10 | dev-e43ad34 NDCG@10 | dev-e43ad34 R@5 | dev-e43ad34 R@10 | ΔNDCG@10 | ΔR@5 | ΔR@10 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `knowledge_update` | 78 | 92.12 | 95.51 | 97.44 | 92.19 | 95.51 | 98.72 | +0.07 | +0.00 | +1.28 |
| `multi_session` | 133 | 79.24 | 78.91 | 88.51 | 77.48 | 76.94 | 86.63 | -1.75 | -1.97 | -1.88 |
| `single_session_assistant` | 56 | 91.11 | 94.64 | 94.64 | 91.40 | 92.86 | 94.64 | +0.28 | -1.79 | +0.00 |
| `single_session_preference` | 30 | 59.28 | 63.33 | 80.00 | 58.69 | 63.33 | 83.33 | -0.60 | +0.00 | +3.33 |
| `single_session_user` | 70 | 84.97 | 95.71 | 98.57 | 81.02 | 91.43 | 97.14 | -3.96 | -4.29 | -1.43 |
| `temporal_reasoning` | 133 | 80.29 | 80.28 | 87.64 | 77.97 | 78.52 | 87.64 | -2.32 | -1.75 | +0.00 |
| **macro mean** | 500 | 81.17 | 84.73 | 91.13 | 79.79 | 83.10 | 91.35 | -1.38 | -1.63 | +0.22 |

Calibrated admission threshold: `v2.4.41` 0.4239, `dev-e43ad34` 0.4493 (200
sampled embeddings each; see the caveat below).

**Reading.** The −1.38 on the mean is three question types, not six.
`single_session_user` loses 3.96 points of NDCG@10 and 4.29 of Recall@5 while
losing only 1.43 of Recall@10: the relevant session is still in the top ten
but has dropped out of the top five. `temporal_reasoning` loses 2.32 of NDCG@10
and 1.75 of Recall@5 with Recall@10 unchanged: nothing left the top ten, rows
moved down inside it. `multi_session` loses about 1.9 on every metric.
`knowledge_update`, `single_session_assistant` and `single_session_preference`
are flat within a point (the preference type has thirty queries, where one
query is 3.33 points of recall). Per-type resolution has not been
pre-registered for this pair; these are the deltas, not a verdict.

## `limit10` regime

| question type | n | v2.4.41 NDCG@10 | v2.4.41 R@5 | v2.4.41 R@10 | dev-e43ad34 NDCG@10 | dev-e43ad34 R@5 | dev-e43ad34 R@10 | ΔNDCG@10 | ΔR@5 | ΔR@10 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `knowledge_update` | 78 | 91.22 | 94.87 | 97.44 | 91.85 | 95.51 | 98.72 | +0.63 | +0.64 | +1.28 |
| `multi_session` | 133 | 78.26 | 77.49 | 87.29 | 76.32 | 74.84 | 85.06 | -1.94 | -2.66 | -2.23 |
| `single_session_assistant` | 56 | 89.14 | 94.64 | 94.64 | 90.45 | 94.64 | 94.64 | +1.32 | +0.00 | +0.00 |
| `single_session_preference` | 30 | 59.64 | 66.67 | 80.00 | 56.98 | 63.33 | 76.67 | -2.65 | -3.33 | -3.33 |
| `single_session_user` | 70 | 84.21 | 90.00 | 98.57 | 80.27 | 91.43 | 97.14 | -3.95 | +1.43 | -1.43 |
| `temporal_reasoning` | 133 | 77.77 | 80.36 | 87.14 | 76.06 | 77.42 | 85.99 | -1.71 | -2.94 | -1.15 |
| **macro mean** | 500 | 80.04 | 84.01 | 90.85 | 78.66 | 82.86 | 89.70 | -1.38 | -1.14 | -1.14 |

Calibrated admission threshold: `v2.4.41` 0.4818, `dev-e43ad34` 0.4523 (200
sampled embeddings each; see the caveat below — in this regime the fused gate
reads the threshold, and the two arms drew values 0.03 apart).

**Reading.** The macro delta is the same −1.38 as in the full regime, and the
same three types carry it, with the same sign on every type except
`single_session_assistant` (+1.32 here, +0.28 there) and
`single_session_preference` (−2.65 here on thirty queries, −0.60 there).
`single_session_user` loses 3.95 of NDCG@10 in both regimes; here its
Recall@5 does not fall (+1.43), so in a ten-row call the relevant session is
still among the first five and the loss is in ordering within them.
`temporal_reasoning` loses 1.71 of NDCG@10 and 2.94 of Recall@5.
`multi_session` loses about 2 to 2.7 on every metric. One observation across
the regimes, offered as a check on the protocol rather than as a comparison
the instrument is for: for one build, the isolated ten-row haystack scores
each type within about two points of the pooled full ranking, so the pooled
protocol with the scene filter applied afterwards stood in for one user's
memory at the full ranking. It did not at limit ten: the pooled run of the
same build and regime settings scored 3.33 on `single_session_preference`
against 56.98 isolated, which is why the regime exists.

## What this pair can resolve, per type

The per-query paired difference is sparse and, where it moves, large: on the
`full` regime between 2 and 54 of a type's queries moved at all, and the
standard deviation of the per-query difference runs from 5.6 to 14.2 points.
The minimum detectable mean difference at α = 0.05 two-sided and 80% power for
a paired *t* over that many queries (noncentral *t*; the factor is 0.6264 at
n = 22, the version comparison's value, and 0.24–0.53 at these n) is
therefore coarse, and **none of the per-type deltas above reaches it** —
`single_session_user`'s −3.96 sits under an MDE of 4.83, `temporal_reasoning`'s
−2.32 under 3.14, `multi_session`'s −1.75 under 2.48. The one deterministic
execution per arm determines this benchmark's difference, as the version
comparison says of its own; what the MDE bounds is what a *claim* per type
would need, and the pre-registration template fixes these values for that.
Produced by `longmemeval_by_type_resolution.py` on the two arms:

### regime `full` — per-query NDCG@10, dev-e43ad34 minus v2.4.41

| question type | n | mean Δ | sd | queries moved | MDE (points) |
| --- | --- | --- | --- | --- | --- |
| `knowledge_update` | 78 | +0.07 | 5.60 | 17 | 1.80 |
| `multi_session` | 133 | -1.75 | 10.12 | 54 | 2.48 |
| `single_session_assistant` | 56 | +0.28 | 5.72 | 2 | 2.18 |
| `single_session_preference` | 30 | -0.60 | 12.56 | 7 | 6.65 |
| `single_session_user` | 70 | -3.96 | 14.23 | 12 | 4.83 |
| `temporal_reasoning` | 133 | -2.32 | 12.85 | 47 | 3.14 |

### regime `limit10` — per-query NDCG@10, dev-e43ad34 minus v2.4.41

| question type | n | mean Δ | sd | queries moved | MDE (points) |
| --- | --- | --- | --- | --- | --- |
| `knowledge_update` | 78 | +0.63 | 8.15 | 16 | 2.62 |
| `multi_session` | 133 | -1.94 | 7.96 | 53 | 1.95 |
| `single_session_assistant` | 56 | +1.32 | 6.91 | 2 | 2.63 |
| `single_session_preference` | 30 | -2.65 | 21.90 | 9 | 11.59 |
| `single_session_user` | 70 | -3.95 | 14.51 | 15 | 4.93 |
| `temporal_reasoning` | 133 | -1.71 | 12.76 | 46 | 3.12 |

## Raw outputs

The harness records (`LongMemEval.json`, with the calibration record) for each
arm and regime are under `lme_by_type/<arm>/<regime>/` beside this file. The
ranking dumps (`rankings.jsonl`, about 2 MB per regime and arm) are not
committed; they are what the reader recomputes the tables from and are kept
with the run outputs. The commands were:

```bash
OUTPUT_DIR=~/lmeb/lme_by_type/v2.4.41 CPERSONA_REPO=<worktree at v2.4.41> \
    PYTHON_BIN=~/lmeb/.venv/bin/python benchmarks/run_longmemeval_by_type.sh
OUTPUT_DIR=~/lmeb/lme_by_type/dev-e43ad34 \
    PYTHON_BIN=~/lmeb/.venv/bin/python benchmarks/run_longmemeval_by_type.sh
python benchmarks/longmemeval_by_type.py v2.4.41=~/lmeb/lme_by_type/v2.4.41 \
    dev-e43ad34=~/lmeb/lme_by_type/dev-e43ad34
```

## Caveat: the calibrated threshold is a random draw

`--auto_calibrate` derives the admission threshold from a random sample of two
hundred embeddings (`ORDER BY RANDOM()`, unseeded), so two runs of one build
on one corpus do not share it — the development head drew 0.4491, 0.4493 and
0.4869 on this corpus in one day. In the `full` regime the threshold barely
reaches the ranking (rrf over the full list, gates off; the twelve
reproductions above held across different draws), but in the `limit10` regime
the fused gate reads it, so a delta between arms there carries a component the
build did not contribute. The reader prints each arm's threshold under its
table for that reason. Pinning the threshold across arms, or averaging over
draws, is a decision for the pre-registration that will use this instrument;
this record does neither.
