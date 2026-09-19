# FTS policy compatibility exploration

Registered 2026-09-15 before measuring the candidate policies.

> First committed on 2026-09-19. Unlike the
> [version-comparison registration](prereg-version-comparison-2440-to-2512.md),
> no commit predates the measurements, so the line above is the only record of
> when this was written.

Value: mitigate the measured natural-language retrieval loss while preserving
punctuated identifier lookup. This is exploratory, not release acceptance.

Keep e43ad34 as the source endpoint and use the already-qualified offline
diagnostic driver, cache and fixed REALTALK threshold 0.6602. Do not rerun
unchanged controls. The historical/control/intervention measurements are
already retained. Test three candidate recall policies:

1. `edges`: remove only outer sentence delimiters from each token; preserve
   internal punctuation and characters such as slash, plus, hash and hyphen.
2. `exact_edges`: union exact and edge-normalized FTS terms, without duplicates.
3. `exact_legacy`: union exact and legacy punctuation-stripped FTS terms,
   without duplicates. This is a comparison arm, not the preferred design.

Keep the literal `_build_fts_query` compiler unchanged. Apply the candidate
normalization/expansion in a separate retrieval-policy helper, used by BOTH
memory keyword and episode FTS readers. The compiler's existing tests alone
do not qualify the new policy: exercise actual retrieval with the policy active.
Do not weaken existing assertions. Add tests for sentence punctuation,
identifier-internal punctuation, quotes, CJK, both retrieval readers, and
demonstrate that disabling the policy makes its specific test fail.

Screening requires all existing identifier tests plus the added retrieval
checks to pass. REALTALK must reach >=42.0 macro NDCG@10 and improve every
subtask over the development endpoint. This threshold screens restoration,
not superiority over the old 43.044114 score. Record all three policies,
including failures and per-query regressions. Select at most one passing
candidate by macro score; a difference <0.5 points favors the simpler policy
in this order: edges, exact_edges, exact_legacy. Do not tune more variants to
the same benchmark within this exploration.

Only a screened candidate may proceed to a native REALTALK confirmation and
LongMemEval fixed-threshold 0.4493 confirmation. Reuse the existing control;
run the changed candidate in the background with independent post-run scoring,
not frequent LLM polling. Report all six types. A promising confirmation
requires macro >=80.5 and positive user/temporal/multi-session changes against
the control. These screens are exploratory: both datasets have already informed
the diagnosis, so neither is a new held-out generalization test.

Known boundaries: sentence punctuation and identifier suffixes can be ambiguous;
query expansion may add irrelevant lexical matches; FTS precision/top-rank,
short-token fallback, Unicode punctuation, production limit=10, other embedding
models and the full regression suite need separate acceptance before shipping.
No claim that all improvements in the 2.5 line are preserved follows from these
screens. No commit, push, version bump, merge or deployment is authorized here.
