# Reconstruction v1 qualification

Registered before running the count replay. This qualifies a bounded capability,
not an improvement in retrieval or answer accuracy.

## Fixed scope

Integrate the depth/count separation and the v0 reconstruction exit on the
first 2.6 alpha. Preserve its FTS policy, identity handling and default recall
golden. V1 adds context-aware bundling, bounded conversation bursts, bounded
claims/timeline/evidence, explicit candidate/cluster/selection counts, an
optional reference-only trace, and read-scoped ACL classification.

No relation graph, overflow chain, semantic synthesis or answer reader is
introduced. Existing message-id supersession retains its v0 semantics; an
in-place update does not create history.

## Pass criteria

- Real stored records from different channels, projects or source roles do not
  merge merely because their source ids and timestamps match.
- A series of close timestamps cannot extend an adjacency burst beyond its
  configured window. Episode support requires matching project and channel.
- Every emitted claim, timeline row and role target has retained evidence;
  every evidence ref resolves, and the head content is a quotation.
- At fixed candidate bounds, counts 0/1/2/4/8 leave candidate refs and cluster
  membership unchanged. Returned count is the actual item count. Zero and
  short returns are explained, and gate fallback remains visible.
- An authenticated read-only principal can reconstruct its granted agent and
  cannot reconstruct another agent.
- Targeted mutations of those behaviors fail their named assertions, then the
  restored implementation passes. Existing reconstruction invariants and the
  recall golden remain required.

## Count replay

Reuse the retained LongMemEval `edges` rankings whose retrieval implementation
matches the first alpha (the handler differs only by one blank line). Freeze
their top 20 rows and sweep count 1/2/4/8/10. Record question-type evidence
recall, serialized payload characters, candidate and cluster counts, and the
fraction whose available items are at or below count. Verify that retained
evidence equals the selected source ids and every head quotes its source.

These are session-level documents without reliable per-turn source metadata.
Do not invent such metadata to make bundling fire. This arm measures the
singleton fallback and the count tradeoff; the real-write conversation fixtures
above measure bundling. Neither arm measures end-to-end answer quality.

The retained ranks are full-ranking retrieval, not a fresh production-limit
run. No latency claim is made from replay. Characters are not called tokens.
Defaults 1/10 remain experimental configuration values: this replay alone
cannot choose the answer-quality/payload knee. A scene-controlled count sweep
with a fixed answer reader remains required before claiming those defaults
are empirically optimal.

## Integration correction

The integration review found that the retained v0 message-id rule conflated
independent project namespaces. The candidate now keys bundling, supersession
and conflicts by stored project plus message id; unknown context establishes no
identity. Same-namespace version ordering remains stage-tested because normal
stores do not append versions. Real-store regressions cover sibling-project and
project/global collisions. This corrects the fixed-scope statement above; no
count replay or answer-quality measurement was repeated for this correction.
