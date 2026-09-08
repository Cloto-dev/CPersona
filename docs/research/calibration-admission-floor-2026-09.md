# Is calibration the cause of the lexical-arm losses? (September 2026)

Status: measurement, exploratory (no pre-registration). Seven of the tasks on
which the hybrid pipeline scores below the raw embedding were re-run under
each of the three calibration methods, twice each, with the admission
statistics recorded per task; a separate probe compared the two null
distributions a threshold can be taken from. The numbers feed the
[adaptive fusion derivation](adaptive-fusion-derivation.md); nothing here is
a shipped behaviour. Raw data lives in the benchmark harness output
directories named in section 6; the aggregation scripts are not yet in the
repository.

**The result in four lines.**

1. **Calibration — the dense arm's admission floor — is not the main cause
   of the losses.** Under all three methods (`separation`, `percentile`,
   `zscore`) SciFact, ESGReports and TMD score the same within a point, and
   all three stay four to eight points below the raw embedding. Replicates
   agree to within 0.21 NDCG@10, so the calibration's random draw does not
   move the top ten. The remaining loss sits **after admission** — in the
   combination, in the post-fusion stages, or in the candidate population —
   and this measurement did not separate those three.
2. **The exception is QASPER**: `zscore`, which places the floor lower, gains
   +2.1 (45.46 against 43.32). Where the floor cuts into gold, admission
   matters.
3. **A document–document null is the wrong reference for query–document
   admission.** With an asymmetric-prompt model the doc–doc null mean is 1.3
   to 2.2 times the query–doc null mean; a floor taken from it (even after
   the 0.5 factor) rejects 12 to 46 % of gold pairs on several tasks, and
   without the factor would reject 68 to 100 %. **The 0.5 factor, introduced
   in March 2026, was a crude correction for a null taken from the wrong
   population.**
4. **On small corpora the positive proxy degenerates** (Youden J 0.02 to 0.19
   on EPBench and Gorilla groups of 19 to 200 documents), and the dense arm
   admits nothing for 5 to 18 % of EPBench queries and 8 to 13 % of Gorilla
   queries. That starvation is the only place where the method choice moves
   NDCG.

## 1. Conditions

- Model: jina-embeddings-v5-text-nano (768 dimensions, asymmetric
  `Query:`/`Document:` prompts); embeddings served from the harness cache, so
  no encoding was performed.
- Regime: the Track B regime of `benchmarks/run_trackb.sh` (reciprocal rank
  fusion with automatic calibration, the calibrated fused gate and autocut
  disabled, `limit` equal to the corpus size), on the tree at commit
  [`ad7dfed`](https://github.com/Cloto-dev/cpersona/tree/ad7dfed5a554f76f606bd69e6bdd303455a3b91c),
  the one that added the admission instrumentation. Note that disabling the calibrated
  gate does not disable `_apply_quality_gate`; its heuristic branch still
  runs, which the derivation note records as an open attribution problem.
- `--fast --accel_backend numpy`; the self-check found 0 mismatches in 1,084
  comparisons. The torch backend orders equal-cosine ties differently beyond
  rank 1,000 and was not used.
- Arms: 3 methods × 2 replicates × 7 tasks (the five largest losses —
  SciFact, ESGReports, QASPER, TMD, EPBench — plus ReMe and Gorilla). One arm
  takes about five minutes.

## 2. Main table — the method does not move the losing tasks

Track A is the raw-embedding score measured in July 2026; B (Jul) is the
hybrid score from the same month. Each method column gives the mean NDCG@10
over the two replicates, the calibrated threshold, and the null admission
rate (the fraction of random pairs above the threshold).

| Task | A | B (Jul) | sep | thr | null adm | pct | thr | null adm | z | thr | null adm |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| LMEB_SciFact | 82.18 | 76.92 | 76.81 | 0.74 | 0.007 | 75.89 | 0.39 | 0.050 | 75.89 | 0.33 | 0.129 |
| ESGReports | 49.11 | 44.64 | 41.19 | 0.44 | 0.105 | 42.21 | 0.49 | 0.050 | 41.19 | 0.41 | 0.161 |
| QASPER | 48.50 | 44.44 | 43.32 | 0.51 | 0.039 | 43.70 | 0.49 | 0.050 | **45.46** | 0.42 | 0.163 |
| TMD | 30.07 | 25.89 | 23.85 | 0.57 | 0.119 | 23.91 | 0.62 | 0.050 | 23.84 | 0.56 | 0.144 |
| EPBench | 80.84 | 77.23 | **77.47** | 0.49 | 0.556 | 75.43 | 0.69 | 0.051 | 76.83 | 0.60 | 0.141 |
| ReMe | 65.24 | 61.70 | 61.46 | 0.29 | 0.471 | 61.64 | 0.62 | 0.050 | 61.50 | 0.47 | 0.147 |
| Gorilla | 35.89 | 34.24 | 33.62 | 0.74 | 0.148 | 33.14 | 0.81 | 0.050 | 33.72 | 0.72 | 0.169 |

Reading:

- SciFact, TMD, ESGReports: the spread across methods is at most 1.02 points
  and does not account for the gap to Track A (−5.4, −6.2, −7.9). The null
  admission rate moves twentyfold (0.007 to 0.16 on SciFact) while NDCG does
  not: **the floor is not cutting the top ten.**
- QASPER: lowering the floor from 0.25 to 0.21 gains 2.1 points. The probe
  in section 4 shows why — 20 % of QASPER's gold pairs sit below the floor.
- EPBench and Gorilla: the differences are explained by dense-arm starvation
  (section 5).

## 3. Replicates — the calibration draw does not move NDCG@10

The `separation` thresholds move between replicates (ESGReports J 0.64 to
0.62, for example), but NDCG@10 moves by at most 0.21 across 7 tasks × 3
methods (QASPER `separation`: 43.42 and 43.21). The benchmark README's
doctrine that calibration noise is "±1 to 3 points per subtask" does not hold
in this regime; it can only hold where the starvation of section 5 occurs.
That doctrine is corrected separately.

## 4. Probe — the null must be taken from query–document pairs

For each corpus group: the doc–doc null (all pairs among 200 documents), the
query–doc null (200 queries × 200 documents), and the gold pairs (up to 500
positives from the relevance judgements), all as cosines computed from the
cached embeddings. `floor` is the fusion admission floor of `separation`
replicate 1 (the threshold times the 0.5 factor).

| Task / group | docs | dd mean | dd p95 | qd mean | qd p95 | gold mean | gold p10 | floor | gold < floor | gold < dd p95 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| SciFact | 1748 | 0.210 | 0.388 | 0.165 | 0.332 | 0.709 | 0.559 | 0.372 | 0.3 % | 0.5 % |
| ESGReports | 2407 | 0.301 | 0.507 | 0.239 | 0.425 | 0.512 | 0.324 | 0.220 | 4.2 % | 45.8 % |
| QASPER | 20508 | 0.313 | 0.499 | 0.201 | 0.384 | 0.407 | 0.195 | 0.252 | **20.0 %** | 67.8 % |
| TMD | 7463 | 0.462 | 0.621 | 0.313 | 0.434 | 0.366 | 0.275 | 0.285 | **12.4 %** | 100 % |
| EPBench sci_fi | 200 | 0.685 | 0.810 | 0.334 | 0.520 | 0.421 | 0.225 | 0.306 | **33.2 %** | 100 % |
| EPBench world_news | 200 | 0.500 | 0.713 | 0.232 | 0.381 | 0.399 | 0.240 | 0.297 | **27.6 %** | 99.6 % |
| Gorilla huggingface | 907 | 0.447 | 0.684 | 0.202 | 0.423 | 0.462 | 0.243 | 0.306 | **16.2 %** | 94.2 % |
| Gorilla tensorflow | 55 | 0.700 | 0.955 | 0.321 | 0.612 | 0.441 | 0.237 | 0.445 | **45.5 %** | 100 % |
| ReMe appworld | 218 | 0.374 | 0.705 | 0.289 | 0.575 | 0.592 | 0.373 | 0.128 | 0.0 % | 70.2 % |

Reading:

- **The doc–doc null is consistently higher than the query–doc null** (mean
  ratio 1.27 to 2.18). For an asymmetric-prompt model "how close documents
  are to each other" and "how close a query is to a document" are different
  distributions, and the second is lower.
- Calibration takes its null from doc–doc pairs and halves the threshold
  before using it for query–doc admission. **The 0.5 factor is the constant
  that happened to bridge that gap**, and the correction each task needs is
  different (SciFact loses 0.3 % of gold, Gorilla tensorflow 45 %).
- A null-referenced admission at a fixed p must therefore take its dense
  null from **random query–document pairs**; against the doc–doc null,
  p = 0.05 would reject 68 to 100 % of gold (rightmost column).
- No query distribution exists at start-up. The candidates — retained past
  queries, document fragments encoded as pseudo-queries, or a doc–doc null
  with a transport — are weighed in the derivation note, section 6.
- One open discrepancy: the calibration record for QASPER reports 65,300
  corpus rows, while the probe counted 20,508 unique document ids. Whether
  the two instruments scored the same population has not been reconciled.

## 5. Small corpora — proxy degeneration and dense-arm starvation

| Task | Method | Queries with no dense candidate | J (`separation`, min / median) |
| --- | --- | --- | --- |
| EPBench (9 groups, 19 to 1,967 docs) | separation | 176 / 3,644 (4.8 %) | 0.02 / 0.05 |
| | percentile | 665 / 3,644 (18.2 %) | — |
| | zscore | 362 / 3,644 (9.9 %) | — |
| Gorilla (3 groups, 907 / 43 / 55 docs) | separation | 58 / 598 (9.7 %) | 0.14 / 0.23 |
| | percentile | 78 / 598 (13.0 %) | — |
| | zscore | 47 / 598 (7.9 %) | — |
| ReMe (6 groups, 96 to 218 docs) | separation | 0 | 0.05 / 0.08 |
| | percentile | 2 per replicate | — |
| The other four tasks | all | 0 | 0.64 to 0.76 |

- The benchmark stores every document at the same instant, so the
  temporal-adjacency "same session" proxy carries no information here, yet
  the calibration did not fall back to its nearest-neighbour proxy
  (`proxy_source` is `temporal` for every group). A J of 0.02 to 0.19 means
  a threshold placed where positives and null are not separated.
- Starvation is the product of a high doc–doc null on a small corpus
  (EPBench sci_fi 0.69, Gorilla tensorflow 0.70) and the high floor placed
  from it: the extreme case of the wrong-null problem of section 4, not a
  separate defect.
- Production corpora have temporal structure, so the proxy degenerates less
  easily there; a **minimum sample size and a lower bound on J** are still
  required in the design (the derivation note gives a rule).

## 6. Reproduction

```bash
MODEL_PATH=jinaai/jina-embeddings-v5-text-nano EMB_CACHE_DIR=<cache> OUTPUT_DIR=<out> \
  bash benchmarks/run_trackb.sh --fast --accel_backend numpy --selfcheck_rate 0.02 \
    --trust_remote_code --default_task retrieval \
    --tasks LMEB_SciFact,ESGReports,QASPER,TMD,EPBench,ReMe,Gorilla \
    --record_admission --calibrate_method <separation|percentile|zscore>
```

Each task's JSON carries a `calibration` block (method, proxy source,
thresholds, null and positive admission rates, Youden J, fusion admission
floor) and a `vector_admission` block per corpus group (admitted fraction
mean and percentiles, queries admitting none or all, queries censored by the
limit). The null probe and the aggregation over arms were run from scripts
outside the repository; bringing them in is a follow-up.

## 7. What this hands to the design

On the calibration side, three things remain:

1. Take the dense null from query–document pairs (section 4);
   `RRF_THRESHOLD_FACTOR` becomes a candidate for retirement once that is in.
2. A validity gate on the positive proxy (a J lower bound and a minimum
   sample) with a declared fallback when it degenerates (section 5).
3. An instrument that measures "gold below the floor" on a
   production-shaped corpus, so a fixed-p admission can be shown not to cut.

Beyond calibration: the losses on SciFact, TMD and ESGReports are the same
under all three methods and remain below Track A, so changing the admission
floor cannot explain them. What does is not settled by this measurement: the
loss may sit in the fusion, in the heuristic gate that still runs after it,
or in the candidate population (section 4 left one discrepancy open). The
next measurement therefore separates those stages on frozen candidates
before any fusion rule is chosen — the derivation note's section 9 gives the
order. If the loss does sit in the fusion, the derivation's mixture rule is
the candidate remedy; note that the influence it derives is per row, not per
query.

## 8. By-products

- Track B on this model is 0.1 to 3.5 points lower on every task than the
  July run on the same cache (TMD −2.0 over 2,134 queries, ESGReports −3.5
  over 36). Where in the intervening releases the ranking moved is not yet
  known; the published tables are from August and are not affected, but the
  next re-measurement will be.
- The benchmark README's noise doctrine contradicts section 3.
- The first run of the instrumentation caught two existing defects (`--fast`
  dropped the far list entirely, and the library-side limit cap cut
  full-ranking runs); both were fixed before these arms were run.

## 9. Three models, one table

Track A for bge-m3 was re-measured in September 2026 (mean 56.83, matching
the published value, so the instrument is intact). Track B for bge-m3 is the
August run; jina is the July pair; MiniLM is from the March pipeline and is
indicative only.

| Task | bge A | bge B | Δ bge | Δ jina | Δ MiniLM (old) |
| --- | --- | --- | --- | --- | --- |
| MemBench | 71.24 | 65.62 | **−5.62** | −2.88 | −0.04 |
| QASPER | 51.54 | 48.55 | **−2.99** | −4.18 | **+25.05** |
| TMD | 28.11 | 25.18 | **−2.93** | −4.16 | ±0.00 |
| ConvoMem | 64.41 | 61.59 | **−2.81** | −2.91 | −0.04 |
| REALTALK | 44.98 | 43.04 | −1.94 | +0.41 | +0.19 |
| ReMe | 61.39 | 59.61 | −1.78 | −3.54 | −0.01 |
| LMEB_SciFact | 76.41 | 74.90 | −1.51 | **−5.71** | ±0.00 |
| Gorilla | 34.37 | 32.93 | −1.44 | −1.63 | **−13.98** |
| MLDR | 81.33 | 80.66 | −0.67 | −1.98 | +0.37 |
| LoCoMo | 46.53 | 45.92 | −0.61 | +0.11 | +0.09 |
| PeerQA | 29.73 | 30.04 | +0.31 | −2.06 | −0.01 |
| Proced_mem_bench | 51.23 | 52.13 | +0.90 | −1.60 | +5.88 |
| ESGReports | 40.82 | 43.27 | +2.45 | −4.47 | ±0.00 |
| LongMemEval | 78.29 | 81.07 | +2.78 | +4.49 | +0.26 |
| EPBench | 87.45 | 90.24 | +2.79 | −3.87 | −3.31 |
| MemGovern | 85.98 | 89.04 | +3.06 | +1.94 | +0.05 |
| DeepPlanning | 52.86 | 56.70 | +3.84 | +4.55 | −7.82 |
| NovelQA | 32.24 | 36.10 | +3.86 | +4.21 | +0.72 |
| CovidQA | 79.46 | 83.94 | +4.48 | +3.60 | +0.08 |
| KnowMeBench | 46.51 | 51.62 | +5.11 | +6.53 | ±0.00 |
| LooGLE | 58.94 | 64.12 | +5.18 | +5.19 | +0.70 |
| ToolBench | 46.38 | 52.21 | +5.83 | +1.79 | −1.12 |

Reading:

- **bge-m3 also loses on ten tasks**; its +0.83 mean is gains and losses
  cancelling. Eight tasks lose on both jina and bge-m3 (MemBench, QASPER,
  TMD, ConvoMem, ReMe, SciFact, Gorilla, MLDR): **the stronger the model,
  the more the same family loses.**
- **Gorilla loses on all three models** (−13.98 / −1.63 / −1.44); MemBench,
  ConvoMem and ReMe are at or below zero on all three. This is the family
  the hybrid pipeline harms whatever the embedding model; whether the
  harm is done by the fusion or by a later stage is what the frozen-stage
  replay has to decide.
- QASPER changes sign (+25 → −4 → −3): the lexical arm rescues a weak model
  and the same votes harm a strong one. The tasks that gain on all three
  (LongMemEval, LooGLE, KnowMeBench, CovidQA, NovelQA) are equally
  consistent.
- Admission does not move this family (section 2), so whatever recovers it
  has to act after admission. The derivation note's candidate is a rule that
  weighs the lexical vote against the dense arm's distance from its null, per
  row; it remains a candidate until the stages are separated.
