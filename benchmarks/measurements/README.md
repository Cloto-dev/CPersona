# Measurement records

Curated benchmark results, kept in the repo alongside the harness that produced
them. Everything here is an **evidence record**: it says what was measured, under
which code, under which regime — including runs that turned out to be invalid.

This is distinct from the scratch output of a local run. `benchmarks/run_trackb.sh`
writes to a repo-root directory (`OUTPUT_DIR`, default `trackb_results`), and the
root-anchored patterns in `.gitignore` keep that output untracked. A result set
only lands here once it is worth citing.

## Why invalid runs are kept

Two of the sets below are wrong, and are kept precisely because they are wrong.
A number that was quietly under-measured is worth more as a recorded artifact
than as a deleted mistake: it is what lets a later reader tell "the model got
worse" apart from "the harness stopped measuring the tail". Do not delete or
"clean up" the `.INVALID-*` / `.CLAMPED-*` directories.

## Track B — cpersona store/recall paths (`mean_ndcg_at_10`)

| Result set | LongMemEval | QASPER | EPBench | LoCoMo | KnowMeBench | Tasks |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `trackb_results_v2440_minilm` | 76.92 | 41.23 | 57.73 | 36.64 | 42.25 | 23 |
| `trackb_results_v2440_bgem3` | 81.17 | 48.56 | 90.23 | 45.91 | 51.62 | 23 |
| `trackb_results_jinanano` | 81.91 | 44.32 | 76.97 | 45.09 | 50.22 | 23 |
| `trackb_results_v2440_bgem3.CLAMPED-depth100` ⚠ | **48.98** | — | 90.19 | 45.88 | 51.40 | 5 |
| `trackb_results_v2440_bgem3.INVALID-v2439-window` ⚠ | **38.72** | — | 87.22 | 39.17 | **22.82** | 5 |

The three valid sets are the model-strength comparison behind the finding that
the filter layer's contribution moves with embedding strength: MiniLM leans on
FTS for QASPER, bge-m3 does not gain the same way, and jina-v5-nano lands close
to bge-m3 on dialogue tasks while trailing it on EPBench.

### `.CLAMPED-depth100` — the in-library limit cap

Run against a checkout (v2.4.38..v2.4.40) where `do_recall` clamped to 100
in-library. The harness convention is full ranking (`limit=corpus_size`), so any
task whose answer sits past rank 100 was scored as a miss. The distortion is
task-shaped, not uniform: LongMemEval collapses 81.17 → 48.98 while EPBench
(90.23 → 90.19) and LoCoMo (45.91 → 45.88) barely move — large-corpus tasks are
truncated, small ones are untouched. Reading only EPBench would have hidden the
bug entirely.

Fixed in 2.5.0: the in-library cap is now the scan window (`MAX_MEMORIES`) and
the agent-facing 100 cap moved to the MCP boundary, which the library path does
not traverse. `--unclamp_limit` is accepted as a no-op since then.

### `.INVALID-v2439-window` — the v2.4.39 scan window

Run under the v2.4.39 scan-window behaviour. Worse than the clamp case and
differently shaped: KnowMeBench falls 51.62 → 22.82, more than half. Superseded
by the v2.4.40 sets above; kept as the record of what that window did.

## Track A — raw embedding baseline (`lmeb_results/`)

`lmeb_results/jina-embeddings-v5-text-nano/` — LMEB via mteb, model revision
`8a7f00aac812071b69403df470f1038ec85f8925`. `_summary.json` holds the aggregate:

| Metric | Value |
| --- | ---: |
| mean (dataset) | 0.5737 |
| Episodic | 0.6326 |
| Dialogue | 0.5478 |
| Semantic | 0.5742 |
| Procedural | 0.5684 |

Per-task scores sit alongside it, e.g. EPBench 0.8084, LongMemEval 0.7742,
LoCoMo 0.4498, KnowMeBench 0.4369, REALTALK 0.4156, TMD 0.3007.

This model needs the prompted / task-adapter path (`--trust_remote_code`,
`--default_task`, explicit query/document prompts) added in PR #39 — encoding
queries under the document prompt breaks asymmetric retrieval and the numbers
above are not reproducible without it.

## Logs (`logs/`)

Run logs for the v2.4.40 Track B measurements and the v2.4.40 gate. They carry
the regime each run actually executed — temp DB path, whether the limit clamp
was bypassed, `mps_accel` backend and self-check, library versions — which is
the part that is not recoverable from the JSON.

**Modified from the raw output in exactly one way**: absolute home paths were
replaced with `~` before committing, since this repo is public. No measured
value, timing, or log line was otherwise altered.

## Provenance

- Track B v2.4.40 runs: 2026-07-10, `mps_accel` numpy/cpu backend
  (self-check 1.00%), temp SQLite DB per run, limit clamp bypassed.
- jina-v5-nano runs: harness support merged in PR #39.
- Migrated into this repo on 2026-07-20 from a second working clone where they
  had accumulated as untracked output and existed in no other copy.
