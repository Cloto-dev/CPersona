# Pre-registration: bug-155 cosine-backfill A/B on LMEB (CSC Task #374)

Registered BEFORE any A/B run was executed or inspected. The comparison basis
below — conditions, metrics, aggregation, and noise controls — is fixed by this
document; any post-hoc deviation must be called out as such in the results.

## Question

`_compute_confidence` structurally promotes rows without a cosine (see
registry bug-155). The fix backfills the true cosine for fusion rows whose
embedding exists but was not scored by the vector channel. This measurement
answers, on LMEB with the real cpersona store/recall paths:

1. **Prevalence** — how often does the unfixed code return cosine-less rows
   in production-shaped top-10?
2. **Impact** — what does the fix do to ranking quality (NDCG@10) and to the
   returned top-10 (membership/order)?

It does NOT decide the release vehicle (2.5.2 vs 2.6.0) — that is the
operator's call, made on these numbers.

## Code sides

- **master**: branch point of `fix-374-cosine-backfill` (unfixed).
- **fix**: head of `fix-374-cosine-backfill`.
Both sides run the SAME harness file — the fix branch's
`benchmarks/benchmark_trackb_lmeb.py` (which carries the measurement
extensions) — and differ only in `CPERSONA_REPO`, which points the cpersona
import at either a worktree of the branch point (master side) or the fix
branch checkout. Harness code is identical on both sides by construction.

## Shared regime (all runs)

- Model: bge-m3 (`MODEL_PATH=BAAI/bge-m3`), cache `~/lmeb/embcache`
  (encode-free), device mps, dtype float16, `--budget_encode`.
- `CPERSONA_CONFIDENCE_ENABLED=true` — the measured configuration is the
  production one; the official Track B regime (confidence off) never
  exercises the buggy branch.
- Truncation layers OFF (`CPERSONA_AUTOCUT_ENABLED=false`,
  `CPERSONA_FUSED_GATE_ENABLED=false`) — benchmark doctrine: pure ranking
  metric. Production truncation may amplify or mask membership changes;
  explicitly out of scope here.
- **No `--auto_calibrate`; vector threshold fixed at 0.3 on both sides.**
  Calibration samples with `ORDER BY RANDOM()` and is the harness's only
  run-to-run noise source; it is also per-task, so there is no single
  "per-model" value to pin. 0.3 is simultaneously the cpersona config
  default, the harness `--min_similarity` default, and what an uncalibrated
  production install runs with. Prevalence numbers are conditional on this
  value; production instances with calibrated per-agent thresholds may see
  different prevalence.
- All 22 LMEB tasks. Store path is deterministic (dataset order, cached
  embeddings), so both sides rank the identical corpus. **Cost fallback
  (fixed in advance):** if a one-task pilot projects the primary matrix
  above ~24 h total wall-clock, the task set is reduced to the six tasks of
  the published latency-benchmark cells (an existing, precedent-based
  subset) — chosen by that rule, never by looking at results.

## Run matrix (primary)

Full-ranking runs were considered and dropped before any execution: the
v2440 log shows a full-ranking 22-task bge-m3 run takes ~13 h, and the
full-ranking convention exaggerates the defect (every below-threshold FTS
hit outranks every non-perfect vector hit), so it would cost ~26 h to
produce a number that is not decision-relevant. Production-shaped
`--recall_limit 10` runs are the decision basis.

| Run | recall_mode | limit | sides | primary outputs |
|-----|-------------|-------|-------|-----------------|
| P2 | rsf (production ctools; harness gains the mode choice on the fix branch) | `--recall_limit 10` (production-shaped) | master, fix | NDCG@10, prevalence, disturbance |
| P3 | cascade (ClotoCore workaround path) | `--recall_limit 10` | master, fix | NDCG@10, prevalence, disturbance |

Conditional secondary (run only if P2 ΔNDCG mean is within ±1.0 pt, or on
operator request): MiniLM repeat of P2 (`embcache_minilm`) — the weak-model
check; #153 doctrine predicts FTS rescue matters more there, so the fix
could plausibly HURT MiniLM while helping bge-m3.

## Metrics and aggregation (fixed)

- **NDCG@10**: per task = mean over queries; headline = unweighted mean over
  the 22 task values (same aggregation as the published Track B 3-point
  table). Report Δ = fix − master per run pair. No max-statistics anywhere.
- **Prevalence** (master side of P2/P3): (i) % of queries whose returned
  top-10 contains ≥1 row without `match_reason.cosine`; (ii) mean count of
  such rows per query. Both computed from `--dump_rankings` JSONL.
- **Disturbance** (P2/P3 pairs): % of queries where top-10 membership
  differs (set inequality); % where order differs (sequence inequality);
  mean Jaccard of the two top-10 sets.
- Noise: with calibration pinned and the corpus deterministic, paired runs
  are expected to be exactly reproducible; any residual nondeterminism found
  will be reported, not silently absorbed.

## Pre-stated expectations (falsifiable)

- Prevalence is substantial (>10% of queries) — FTS regularly surfaces rows
  outside the vector channel.
- On bge-m3, ΔNDCG ≥ 0: demoting cosine-less rows to their true (usually
  mediocre) similarity should not hurt a strong-vector pipeline.
- If ΔNDCG is clearly negative on bge-m3, the elevation was accidentally
  protective (lexical rescue) and the backfill design goes back to the
  drawing board (#153), regardless of how principled it looks.

## Outputs

Results land under `benchmarks/measurements/` next to this document, with
the pinned thresholds, run commands, and per-task tables. The summary quotes
exactly the pre-registered metrics above, in that order, before any
exploratory observation.
