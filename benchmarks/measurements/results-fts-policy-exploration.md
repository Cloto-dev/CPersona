# FTS punctuation policy exploration

Measured 2026-09-15. Value: mitigate natural-language retrieval loss while
preserving punctuated identifier lookup. This is not release acceptance.
The policy and selection rules were [registered before measurement](prereg-fts-policy-exploration.md).
The source endpoint is `e43ad34`; each policy lives in an isolated worktree.
Only the recall-policy helper and its two FTS reader call sites changed.
The literal FTS compiler and the remaining development code are unchanged.

## Screening results

Fixed REALTALK threshold 0.6602, 679 queries, macro NDCG@10 (0-100 scale).
Controls are reused from the [completed diagnostic](results-version-comparison-2440-to-dev.md).

| Policy | Commonsense | Multi-hop | Temporal | Macro | Identifier/retrieval tests |
| --- | ---: | ---: | ---: | ---: | --- |
| Development control | 24.884225 | 24.083188 | 65.059915 | 38.009109 | Existing 5 pass |
| Legacy normalization intervention | 28.869513 | 29.316193 | 70.946637 | 43.044114 | Existing 4 fail, 1 pass |
| `edges` | 27.655923 | 29.391134 | 70.968927 | 42.671995 | 29 pass |
| `exact_edges` | 27.790129 | 29.250765 | 70.960327 | 42.667074 | 29 pass |
| `exact_legacy` | 28.884802 | 29.241348 | 71.056709 | 43.060953 | 14 fail, 15 pass |

`edges` strips only outer sentence delimiters, preserving internal punctuation.
`exact_edges` unions exact and edge-normalized terms. `exact_legacy` unions
exact and globally punctuation-stripped terms. The last arm is ineligible:
both variants miss identifiers with outer sentence punctuation, including
`CVE-2024-3094?`, `(bug-183)`, `v2.5.4.`, email/path forms, and `C++?`.
Each of those seven fixtures failed in both memory and episode FTS readers.

Both eligible arms clear macro >=42 and improve all three subtasks.
Their macro difference is 0.004921; the preregistered <0.5 simplicity rule
selects **`edges`**, not additional tuning on these same queries.
Its macro gain against development is 4.662885, still 0.372120 below legacy.
The commonsense result remains 1.213590 below legacy; restoration is incomplete.

## Per-query tradeoffs

Improved / worsened / unchanged relative to the retained native development
control (absolute comparison tolerance 1e-9):

| Policy | Commonsense (103) | Multi-hop (265) | Temporal (311) |
| --- | --- | --- | --- |
| `edges` | 19 / 14 / 70 | 86 / 41 / 138 | 56 / 25 / 230 |
| `exact_edges` | 18 / 13 / 72 | 88 / 35 / 142 | 56 / 24 / 231 |
| `exact_legacy` | 21 / 11 / 71 | 89 / 35 / 141 | 56 / 24 / 231 |

These are aggregate gains, not improvements on every query.

## Validation and retained evidence

- Existing `tests/test_bug215_fts_punctuation.py` plus new
  `tests/test_fts_query_policy.py`: 29 pass for each eligible arm.
  Fixtures exercise actual FTS retrieval with an unrelated matching row,
  so an empty-result LIKE fallback cannot mask the missing target.
- Mutation: bypassing the new policy in both readers yields **20 failures and
  4 passes** in the new tests for each eligible arm. Failures assert missing
  target content, not import/setup errors. Restoring both call sites restores
  **29 passes** including the existing tests.
- All three accelerated arms: 679 queries, 11 native self-checks, 0 mismatches,
  0 fallbacks. Independent qrel/ranking recomputation passes for every arm.
- Selected `edges`: a separate full native run of all 679 queries reproduces
  all three subtask scores exactly; independent recomputation also passes.
- CI-pinned `ruff@0.15.21 check` passes for the six changed source/test files.

Raw evidence for each arm — `result.json`, `manifest.json` (source/driver/input
hashes), ranked IDs, per-query judgments and the experiment database — was kept
on the measuring machine and is not committed. The selected policy is the one
that later shipped in 2.6.0a1.
The measurement and independent verifier are
`benchmarks/diagnose_recall_revision.py` and `benchmarks/verify_recall_diagnostic.py`.

## LongMemEval confirmation: completed

The selected `edges` arm completed all 500 queries at fixed threshold 0.4493.
The background runner finished independent verification at **2026-09-15
09:55:12 UTC**. It recomputed all 500 candidate and 500 retained control query
scores from qrels and ranked IDs; the unchanged control was not remeasured.

| Question type | Queries | Development control | `edges` | Delta | Improved / worsened / unchanged |
| --- | ---: | ---: | ---: | ---: | --- |
| knowledge_update | 78 | 92.190023 | 93.006930 | +0.816907 | 10 / 6 / 62 |
| multi_session | 133 | 77.482887 | 79.921333 | +2.438445 | 33 / 22 / 78 |
| single_session_assistant | 56 | 91.396168 | 91.772838 | +0.376670 | 2 / 1 / 53 |
| single_session_preference | 30 | 58.686234 | 59.284564 | +0.598330 | 4 / 3 / 23 |
| single_session_user | 70 | 81.015351 | 86.631528 | +5.616177 | 11 / 1 / 58 |
| temporal_reasoning | 133 | 77.967371 | 80.618045 | +2.650674 | 28 / 18 / 87 |
| **Macro (equal weight per type)** | **500** | **79.789672** | **81.872540** | **+2.082867** | **88 / 51 / 361** |

All six type means improve, but 51 individual queries worsen. Per-query counts
use the same 1e-9 tolerance as the REALTALK comparison. The preregistered
exploratory screen passes: macro >=80.5 and positive user, temporal, and
multi-session deltas. This is a screen result, not statistical significance
or independent holdout validation.

The candidate also exceeds the diagnostic legacy-normalization intervention
(81.169167 macro) by **0.703373**. Five type means exceed that intervention;
preference is equal. The intervention matched the retained historical
v2.4.40 scores to two decimal places; it is not a newly run historical arm.
Thus LongMemEval is not merely restored to the legacy-normalization result,
while REALTALK still falls 0.372120 short of that diagnostic result.

The candidate used 237,655 input documents, with 237,654 stored after the
existing duplicate collapse. There were 10 native self-checks, 0 mismatches,
and 0 accelerator fallbacks. The full LongMemEval candidate was not rerun in
native mode. Measured diagnostic runtime was 7,766.25 seconds (129.44 minutes),
not an as-shipped latency claim.

The run's result, manifest, rankings, per-query judgments and experiment
database, with a completion record and its verification log, were kept on the
measuring machine and are not committed.
No measurement or background wait remains pending for this exploration.

## Remaining acceptance and next step

Update later on 2026-09-15: [full regression qualification](results-fts-policy-regression.md)
passes on Python 3.11 for both the e43-based and b4-based candidates. Python
3.13 initially exposed one pre-existing SQLite-dependent fixture defect, also
reproduced without the candidate change. After repairing and mutation-testing
that fixture, the b4-based candidate passes **2,413 tests with five existing
skips on both Python 3.11 and 3.13**. The local full-suite gate is now clear.
The following paragraph records the outstanding acceptance scope as originally
identified, not a claim that no regression tests have since run.

The next step is acceptance work for the selected policy, not another run of
the completed unchanged benchmark: specify ambiguous punctuation behavior,
cover the remaining retrieval boundaries, run the full regression suite, and
evaluate production-limit behavior before choosing a release line. That work
has not been performed as part of this results-recording update.

This is evidence toward preserving identifier retrieval while recovering
natural-language accuracy, not proof that every improvement in the 2.5 line
is preserved. Full regression, production limit=10, other embeddings, Unicode
punctuation, short-token fallback, and ambiguous identifier suffixes remain
outside acceptance. Neither benchmark is a fresh holdout. No commit, push,
version bump, merge, deployment, or release had been performed when this
record was written; the `edges` policy later shipped in 2.6.0a1.
