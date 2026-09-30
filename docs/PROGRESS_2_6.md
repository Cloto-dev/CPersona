# Where the 2.6 line stands

Updated: 2026-09-30. This page says how far the 2.6 line has come, item by
item, with the evidence for each. The [line's page](RELIABLE_RECALL_2_6.md)
says what the line builds and what "done" means; the
[release notes](https://github.com/Cloto-dev/cpersona/releases) say what each
release contains. Where this page and a release disagree, the release is
right.

2.6.0 is the line's first final release, and a plain install resolves to it.
Its pre-releases are on PyPI as `2.6.0aN` and `2.6.0bN`; the 2.5 line is now
Candidate ([SUPPORT.md](https://github.com/Cloto-dev/cpersona/blob/master/SUPPORT.md)).

## The four states

Every row below carries exactly one of these. They are kept apart so that a
design is never read as a feature.

| State | Meaning |
| --- | --- |
| **Released** | In a published release. Anyone can install it and call it. |
| **In development** | Code exists on `master`, but it is unreleased, or released without the measurement that decides its default. |
| **In research** | A hypothesis, an experiment or a design comparison. No server code yet. |
| **Withdrawn or reworked** | Measured and rejected, or found wrong and being redesigned. These rows stay on the page. |

## Against the line's completion conditions

The numbering follows
[what "done" means](RELIABLE_RECALL_2_6.md#10-what-done-means).

| # | Condition | State | Evidence |
| --- | --- | --- | --- |
| 1 | The recall process and Cued Recall ship behind a gate | **Released** in 2.6.0a8, v0.3 | The loop's basic form: a caller may pass `time_cue` (when, with a confidence); the server also searches that period to a depth of its own, moves a row found there up by at most 3 / 2 / 1 places after the quality gate, holds as many seats for records that search found which the answer does not hold (one the count cut included), and widens the period once if it holds nothing ([#325](https://github.com/Cloto-dev/cpersona/pull/325), [#331](https://github.com/Cloto-dev/cpersona/pull/331)–[#333](https://github.com/Cloto-dev/cpersona/pull/333), [#340](https://github.com/Cloto-dev/cpersona/pull/340), [design](RECALL_PROCESS_DESIGN.md#2-the-loops-basic-form)). Opt-in per call. Measured under a pre-registered rule on the LMEB TMD task (1,167 questions that state a time): mean NDCG over the returned rows rose from 0.189 to 0.281 with the cue and to 0.204 with as many extra uncued rows ([results](https://github.com/Cloto-dev/cpersona/blob/master/benchmarks/measurements/results-tmd-time-cue.md)). On LongMemEval the previous policy was not distinguishable from no cue ([results](https://github.com/Cloto-dev/cpersona/blob/master/benchmarks/measurements/results-longmemeval-time-cue.md)). Cue propagation and richer stopping are later. |
| 2 | The final re-sort has been decided | **Released** in 2.6.0a7 | With confidence enabled, the confidence score no longer re-sorts or gates recall, and `CPERSONA_CONFIDENCE_ORDERING=legacy` restores it. The far weight and an age weight are one prior function that orders only what the gate admitted, both at identity defaults until measured ([#322](https://github.com/Cloto-dev/cpersona/pull/322), [design](PRIOR_FUNCTION_DESIGN.md)). The far weight has since been measured under a pre-registered rule: at a reach of 200,000, no constant weight kept the recent answers while keeping what the far list buys, so its default stays at `1` ([results](https://github.com/Cloto-dev/cpersona/blob/master/benchmarks/measurements/results-far-weight-sweep.md)). |
| 3 | Depth and count are separated | **Released** in 2.6.0a2 | [#274](https://github.com/Cloto-dev/cpersona/pull/274). `CPERSONA_RECALL_DEPTH_FLOOR` defaults to `0`, which keeps the 2.5 coupling until a depth is chosen by measurement. |
| 4 | Reconstructive Recall exists as a tool | **Released** in 2.6.0a2, extended in a3 | The tool: [#274](https://github.com/Cloto-dev/cpersona/pull/274). Breadth before depth and the payload budget: [#280](https://github.com/Cloto-dev/cpersona/pull/280), [#286](https://github.com/Cloto-dev/cpersona/pull/286). One item shape, and a response that says what it dropped: [#288](https://github.com/Cloto-dev/cpersona/pull/288), [#289](https://github.com/Cloto-dev/cpersona/pull/289), [#290](https://github.com/Cloto-dev/cpersona/pull/290). Measured so far: a [count replay](https://github.com/Cloto-dev/cpersona/blob/master/benchmarks/measurements/results-reconstruct-v1-count-replay.md) over ceilings 1 to 10, which by its own statement does not choose a default; and a pre-registered [reader study](https://github.com/Cloto-dev/cpersona/blob/master/benchmarks/measurements/results-reconstruct-v1_1-reader.md) whose first run found no reduction in what a reader takes in, and whose re-measurement after two changes met the registered rule on 18 questions. The default count is still a contract choice, not a measured optimum. |
| 5 | Adaptive fusion beats the raw embedding on both models | **In research** | The design and the stage-by-stage loss analysis it rests on: [#260](https://github.com/Cloto-dev/cpersona/pull/260), [#261](https://github.com/Cloto-dev/cpersona/pull/261), [design record](ADAPTIVE_FUSION_DESIGN.md). No server code. |
| 6 | Every benchmark failure has a code and a replayable trace | **In development** | Released in 2.6.0a7: on request, `recall` and `reconstruct` return a trace of what each stage kept, dropped and reordered, and a tool in `benchmarks/` assigns a confirmed failure code from a trace and the answer's references ([#324](https://github.com/Cloto-dev/cpersona/pull/324), [design](RECALL_PROCESS_DESIGN.md#1-the-recall-trace)). Not yet applied to every benchmark failure. |
| 7 | The accuracy–token–latency–memory frontier has moved | **In research** | Not measured. It is measured last, against the frozen 2.5 baseline. |
| 8 | The open quality debt of the 2.5 baseline is closed or carried with reasons | **In development** | The calibration a benchmark run measured under is now recorded with the run: [#258](https://github.com/Cloto-dev/cpersona/pull/258). The other items are open. |

## Released in this line, beyond the conditions

| Release | What it added | Evidence |
| --- | --- | --- |
| 2.6.0a1 | Full-text normalisation that keeps identifiers searchable | [#271](https://github.com/Cloto-dev/cpersona/pull/271) |
| 2.6.0a3 | Long records divided into nodes, kept out of the search index; a reconstruct quote taken from the best node; part of a record expanded through `get_contents` | [#282](https://github.com/Cloto-dev/cpersona/pull/282)–[#287](https://github.com/Cloto-dev/cpersona/pull/287), [design record](OVERFLOW_TREE_DESIGN.md) |
| 2.6.0a4 | Declared associative memory: entities, aliases and relations the agent states; `reconstruct` reads them as cues with a bounded walk; `traverse` returns a declared neighbourhood | [#291](https://github.com/Cloto-dev/cpersona/pull/291)–[#295](https://github.com/Cloto-dev/cpersona/pull/295), [design record](ASSOCIATIVE_MEMORY_DESIGN.md) |
| 2.6.0a7 | The episode-boundary penalty off by default: it was found to push the record holding an answer down the ranking on a store where each session ends in an episode summary | [#320](https://github.com/Cloto-dev/cpersona/pull/320) |
| 2.6.0a8 | An opt-in propagation seat: one held place for the row a deeper ranking of the same recall leads to from the answer's first row (`CPERSONA_RECALL_PROPAGATION_SEAT`, off by default) | [#338](https://github.com/Cloto-dev/cpersona/pull/338) |
| 2.6.0a8 | Recall and reconstruct call each stage through a provider slot, and a traced recall records the budget it declared and what each stage received; nothing a caller receives changes | [#335](https://github.com/Cloto-dev/cpersona/pull/335), [#336](https://github.com/Cloto-dev/cpersona/pull/336) |
| 2.6.0b1 | No feature: fixes for the eleven MEDIUM defects a review of 2.6.0a1..a8 found, among them a project's alias other projects could read, a qualifier on the next line left out of a quote, `reconstruct` walking with no bound on its hops, an episode quote measured 10 characters off, and node sets the reader rejected that nothing rebuilt | [#356](https://github.com/Cloto-dev/cpersona/pull/356), [#357](https://github.com/Cloto-dev/cpersona/pull/357), [#358](https://github.com/Cloto-dev/cpersona/pull/358), [#359](https://github.com/Cloto-dev/cpersona/pull/359), [#361](https://github.com/Cloto-dev/cpersona/pull/361) |
| 2.6.0b2 | No feature: fixes for the five remaining MEDIUM defects, among them an in-memory export whose header could disagree with its body, an index status that reported rows each query reads as none, a list cut by its row cap that looked complete, a `session_key` with no length bound, and an index path that held a copied window whole | [#363](https://github.com/Cloto-dev/cpersona/pull/363), [#364](https://github.com/Cloto-dev/cpersona/pull/364), [#365](https://github.com/Cloto-dev/cpersona/pull/365), [#366](https://github.com/Cloto-dev/cpersona/pull/366) |
| 2.6.0 | The line's first final release, and no feature: block reach on by default, and fixes for forty-three LOW defects the alpha review registered, among them block sets that crossed a node end, a time cue past the representable range that raised, a failing build that held every queued task, and held rows that earned a recall count no gate granted. |

The associative layer is released and off by default. Whether it becomes the
default is decided by its own A/B run; no result of that run is recorded yet.

## Withdrawn or reworked

| What | What happened | Evidence |
| --- | --- | --- |
| The reference panel for adaptive fusion, as first specified | It identified no null: the two densities it specified were the same distribution, so the evidence it defined could not separate a mixture from its own null. The definition was replaced before any code was built on it. | [#262](https://github.com/Cloto-dev/cpersona/pull/262) |
| `rsf` on a fixed channel scale (the bug-247 fix) | It passed its pre-registered rule on LongMemEval at ten rows, where it also returned more rows. On LMEB Track B it scored lower on 19 of 22 tasks (macro 58.53 → 57.51), so it was reverted before release. The gate defect it addressed is open again. | [results](https://github.com/Cloto-dev/cpersona/blob/master/benchmarks/measurements/results-rsf-fixed-scale.md) |
| `rsf` gated on a fixed scale with its order unchanged (the second bug-247 design) | It passed both of its pre-registered rules: no LMEB Track B task fell (macro 57.96 → 57.97) and no LongMemEval question fell at ten rows. It was not merged: under the calibrated fused gate a deployment runs by default, it chose a threshold that emptied or thinned most answers on a private pack of real agent memories (rows at ten 9.89 → 2.16, evidence reached 295 → 204 of 389). bug-247 stays open. | [results](https://github.com/Cloto-dev/cpersona/blob/master/benchmarks/measurements/results-rsf-gate-scale.md) |

## What is not on this page

Dates. The [roadmap](roadmap.md) is descriptive and this page follows it: a
row moves when its evidence exists, not when a date arrives.
