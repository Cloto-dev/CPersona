# Pre-registration — 2.6.0 against 2.5.12 on LMEB, each in its best configuration

Registered before the comparison runs. The 2.6.0 release notes compare the two
versions on a private set of questions about a real agent's memory and promise
the same comparison on a public benchmark after the release. This is that
comparison: **on LMEB, does 2.6.0 in its best configuration rank the relevant
memory higher than 2.5.12 in its best configuration?**

## The regime: what a caller of the tools receives

The standing Track B record (`run_trackb.sh`) ranks the whole corpus (`limit` =
corpus size). That regime cannot see what 2.6 changed, for structural reasons
rather than statistical ones:

- **Block reach adds rows beside the window, never inside it**
  ([BLOCK_REACH_DESIGN.md §5](../../docs/BLOCK_REACH_DESIGN.md)). With the
  window as large as the corpus, a reserved row lands after every row the gate
  admitted, far below rank ten.
- **`reconstruct` returns its count of items** (10 by default in 2.6.0), plus
  any items a reservation holds. It cannot be asked for a ranking of the whole
  corpus.
- The third arm of the previous comparison
  ([prereg-version-comparison-2440-to-2512.md](prereg-version-comparison-2440-to-2512.md))
  already found that regime blind to work of this kind.

So both versions are measured in the production regime that
`run_longmemeval_by_type.sh` calls `limit10`: `recall` at `limit=10`,
`reconstruct` at its default count, autocut and the fused gate at each build's
own defaults (the variables are left unset), the vector threshold calibrated per
corpus (`--auto_calibrate`), and the haystack of a query is the memory that one
caller has.

**NDCG@10 is scored on the order the response gives.** A `recall` response is
the gate's window followed by any reserved rows; a `reconstruct` response is its
items, most relevant first, followed by any reserved items, and an item is
scored as its head claim. Rows past the tenth do not count toward NDCG@10.

## Tasks: 15 of the 22

Where the haystack is one caller's memory can be expressed as a recall scope in
two kinds of task, and not in the third. Counted over the LMEB data on
2026-09-30:

| Kind | Tasks | Haystack of a query |
| --- | --- | --- |
| Per-conversation histories | LoCoMo, LongMemEval, MemBench, ConvoMem, REALTALK, TMD, DeepPlanning | its scene, stored as its own channel (`--isolate_scenes`). Every document id's scene prefix is the scene that lists it, and no document is in two scenes |
| No candidate lists | EPBench, KnowMeBench, SciFact, Gorilla, Proced_mem_bench, MemGovern, ToolBench, ReMe | the whole corpus the subtask reads |
| Per-query candidate subsets of a shared pool | QASPER, NovelQA, PeerQA, Covid-QA, ESG-Reports, MLDR, LooGLE | **not measured** |

The third kind gives each query its own subset of one shared pool, and the
subsets overlap: in NovelQA each of the 79,286 documents belongs to 27 query
subsets on average (2,164,924 memberships), in LooGLE 28,151 documents belong to
more than one. A recall scope is a partition, so the only way to give each query
its own haystack is to store the pool once per query. The claim below is about
the 15 tasks only, and says so wherever it is quoted.

## Builds and harness

| | Commit |
| --- | --- |
| 2.5.12 | `8088429` (tag `v2.5.12`) |
| 2.6.0 | `bac63da` (tag `v2.6.0`) |
| Harness | `benchmarks/benchmark_trackb_lmeb.py` at the commit that adds this file, used for every arm (the package is loaded from each build's tree through `CPERSONA_REPO`) |

Model bge-m3 at float16 with token-budget batching and the shared embedding
cache, as `run_trackb.sh` runs it. The record vectors come from that cache.

**Blocks.** The harness stores rows directly, so the store path that queues a
block build never runs. For the arms with block reach, the harness builds every
record's blocks itself, with the package's own `blocks.prepare_blocks` and
`blocks.write_blocks` (`build_corpus_blocks`). The block texts were encoded ahead
of the run by the same model and precision on the same cache, so the build reads
them from the cache. Two differences from a deployment, stated now:

- **No overflow-tree nodes.** Nothing in the harness builds them, so the
  division runs with no node bounds. A deployment divides a record past the
  embedding window at its node ends as well, which adds at most one boundary
  per node.
- **The encoder.** A deployment embeds through its embedding server; here the
  vectors come from sentence-transformers at float16. Both versions read the
  same record vectors, so this moves the absolute numbers, not the comparison.

## Arms

| Version | Tool | Fusion | Other | Arms |
| --- | --- | --- | --- | ---: |
| 2.5.12 | `recall` | `rrf`, `rsf` | confidence off, on | 4 |
| 2.6.0 | `recall` | `rrf`, `rsf` | block reach off, on | 4 |
| 2.6.0 | `reconstruct` | `rrf`, `rsf` | block reach off, on | 4 |

- Block reach off is `CPERSONA_BLOCK_BUILD_ENABLED=false` (retrieval follows it
  off); on is the 2.6.0 default with `--build_blocks`. Confidence on is
  `CPERSONA_CONFIDENCE_ENABLED=true`.
- **Left out, each an omission against 2.6.0 or an inert setting:** 2.6.0 with
  confidence on (since 2.6.0a7 an enabled confidence score neither orders nor
  gates); the propagation seat, the recall depth floor and the prior age rate
  (all off by default; the depth floor did not help in its own registered
  sweep, [results-recall-depth-floor-sweep.md](results-recall-depth-floor-sweep.md)); the
  2.5.12 episode-boundary penalty turned off — LMEB stores no episodes, and the
  penalty applies only when an episode boundary exists
  (`memory_handlers.py` at `8088429`, the `EPISODE_PENALTY_ENABLED` block), so
  off and on are the same arm.

## Selection on the dev half

Each subtask's queries are split into halves by the harness (`--split dev` /
`--split test`, `--split_seed 20260930`; the assignment is written into every
task file). All 12 arms run on the dev half.

**The best configuration of a version** is its arm with the highest macro
NDCG@10 — the mean over the 15 tasks of each task's mean over its subtasks. A tie
goes to the arm whose responses carried fewer documents on average, then to the
arm name in alphabetical order. The test half is not read until both are chosen
and the choice is committed.

## Comparison on the test half

Run on the test half: each version's best arm, and the configurations the
versions ship — 2.5.12 `rrf` `recall`; 2.6.0 `rrf` `recall` with block reach;
2.6.0 `rrf` `reconstruct` with block reach — where these differ from the best.

**The estimand is this benchmark's difference**, as in the previous comparison:
one deterministic run per arm over fixed data determines it, so no significance
test is the basis of the verdict. A sign count over the 15 tasks is shown for
orientation only.

**Verdict, fixed now.** A task *fell* if its test NDCG@10 is lower by more than
0.01 (the two-decimal storage), *rose* if higher by more than 0.01.

- **"2.6.0 is better on these 15 tasks"** requires the macro NDCG@10 to rise and
  fewer than half the tasks (at most 7) to fall.
- **"2.6.0 is worse"** if the macro falls, whatever the per-task story is.
- Otherwise **mixed**, naming the tasks that fell.

The per-task table is published whole. A task that moves by more than 5 points
either way gets a sentence on what could account for it, labelled as a
hypothesis.

**Reported, not judged:** the shipped configurations against each other; for
every arm, the share of queries whose response carried any relevant document
anywhere in it (reserved rows and every `reconstruct` claim included) and the
mean number of documents carried; the per-category means (Dialogue, Episodic,
Procedural).

## What would invalidate an arm

- Any task that does not complete, or a harness exit other than 0.
- A block-reach arm with no block rows in a corpus group whose records divide,
  or a block-off arm with any block row (`blocks` in the task file).
- A dump header whose regime fields disagree with this file: `recall_limit` 10,
  autocut and fused gate on, `scene_isolated` true for exactly the seven
  per-conversation tasks, the split and seed above.
- A record-vector cache miss (every store batch logs its hits).

## Disclosure: what was seen before this file

Smoke runs on LoCoMo proved each path runs, before the arms were fixed:

- 2.6.0 `rrf` `recall` with block reach, **all queries**: 48.01.
- On the dev half: 2.6.0 `rrf` `reconstruct` with block reach 48.72;
  2.5.12 `rrf` `recall` 47.19.

LoCoMo is one task of 15, and the first number includes its test queries.

## What this cannot answer

- The seven candidate-pool tasks above.
- Answer quality. NDCG@10 ranks documents; the private comparison in the release
  notes measured answers with a reader, which is where a row carried past the
  tenth place counts.
- Latency and memory. The runs share one machine and are not isolated.
- Stores with overflow-tree nodes, and vectors from a deployment's server.
