# Design and research

These pages record how CPersona arrived at its behaviour, and where it is
going. None of them is needed to install or use it; for that, start with the
[guides](../index.md#where-to-go).

## Where it is going

The [roadmap](../roadmap.md) records what each release
line is for, what it may break, and which measured problem each planned
feature answers. It covers three axes: release lines, runtime and scale,
support tiers. It describes intent, not delivery dates. What has shipped is in
the [release notes](https://github.com/Cloto-dev/cpersona/releases) and
[SUPPORT.md](https://github.com/Cloto-dev/cpersona/blob/master/SUPPORT.md).

The 2.6 line has its own page, [Reliable Recall](../RELIABLE_RECALL_2_6.md): the
recall loop that iterates inside a single call, the cue contract, the prior
function, the reconstruction exit and its count window, the failure taxonomy,
and what counts as done; [where the line stands](../PROGRESS_2_6.md) tracks each
of those conditions against its evidence. The line after it has one too,
[Memory Intelligence](../MEMORY_INTELLIGENCE_2_7.md) (2.7): correction and
contradiction, temporal state, evidence-weighted confidence, retention policy
and recall feedback — design only, with the undecided questions marked as
open.

## Project standards

**Project standards** define what a release, an audit report, or a generated
policy block must look like. Other projects can adopt them.

- [Release lifecycle standard](../RELEASE_LIFECYCLE_STANDARD.md) — tier
  definitions (Stable / Current), the risk-triggered pre-release ladder, and
  support windows. What this repository actually runs is in
  [SUPPORT.md](https://github.com/Cloto-dev/cpersona/blob/master/SUPPORT.md).
- [SuperAuditor standard](../SUPERAUDITOR_STANDARD.md) — how a client pulls
  findings from a server: the severity vocabulary, and what a cap means. It
  deliberately says nothing about which problems a server should detect.
- [Policy block standard](../CLAUDE_MD_POLICY_STANDARD.md) — how a project's
  skill writes a marker-wrapped policy block into the file an agent loads
  every session (`CLAUDE.md`, `AGENTS.md`, …), and why a skill alone cannot
  guarantee the block is there.

## Design notes

**Design notes** record how one behaviour was decided, including the routes
that were rejected. They are point-in-time records. Where a note and the
guides disagree, the guides win.

- [Per-client capabilities (ACL)](../ACL_DESIGN.md) — named bearer tokens,
  per-agent read/write grants, deny-by-default.
- [OAuth support](../OAUTH_DESIGN.md) — resource-server metadata, token
  verification, the three routes that were weighed, and where the per-subject
  boundary falls.
- [Server-served operating context](../OPERATING_CONTEXT_DESIGN.md) —
  distributing operator instructions to every connected MCP client.
- [Declared session identity](../SESSION_IDENTITY_DESIGN.md) — why one process is
  not one session under streamable HTTP, and which process-global state
  `session_key` splits apart.
- [Recorded access origin](../MEMORY_ORIGIN_DESIGN.md) — recording the observed
  caller on each stored row, for the paths where `agent_id` names nobody.
- [Recall preview tier](../RECALL_PREVIEW_TIER_DESIGN.md) — preview truncation and
  the `get_contents` expansion path.
- [Contiguous embedding index](../CONTIGUOUS_INDEX_DESIGN.md) — moving the vector
  scan off SQLite rows and onto a contiguous sidecar file. The answers do not
  change.
- [Reach and recency in the scan window](../SCAN_WINDOW_REACH_DESIGN.md) — why a
  wider vector scan window loses recent answers, and the second ranked list
  that widens it without giving up the recency preference.
- [Reach, recency and the far vote](../REACH_AND_RECENCY_PLAN.md) — three
  measurements in one account, what each establishes, and the plan for pricing
  the far vote in the 2.6 line.
- [One prior function](../PRIOR_FUNCTION_DESIGN.md) — every position and age
  weight as one function that orders the result and never gates it, and the
  confidence score returned beside each row instead of re-sorting the list.
- [The recall process, v0](../RECALL_PROCESS_DESIGN.md) — a recall trace that
  attributes a failure to a stage, and a time cue that moves a row found in the
  named period up by a bounded number of places without touching the gate.
- [Evidence allocation](../EVIDENCE_ALLOCATION_DESIGN.md) — one order of the
  quoted passages across every record in the window, and a payload sequence
  built by evidence and cut by the budget, so a smaller budget keeps the turn
  that answers instead of a fixed share of every item.
- [Adaptive fusion](../ADAPTIVE_FUSION_DESIGN.md) — reserving each retriever a
  share of the pool, removing the rank cut from the pool-size gate, and the
  pre-registered comparison between a measured lexical weight now and a
  conditional-evidence fusion mode later.
- [Block reach](../BLOCK_REACH_DESIGN.md) — clause-sized blocks ranked by
  Hamming distance on one bit per dimension, collapsed to their parent and
  admitted by a held reservation, so a long record's tail becomes reachable
  without a new score entering the quality gate.
- [Binary coarse search](../BINARY_COARSE_SEARCH_DESIGN.md) — a one-bit index
  of every record, scanned by Hamming distance and re-ranked by the stored
  vectors, so records past the scan window can be held beside the answer
  and a time cue's period can be searched whole, without widening the window.
- [Block candidate contract](../BLOCK_CANDIDATES_CONTRACT.md) — the block arm's
  candidate generation stated so a faster implementation returns the same rows
  in the same order, the golden that holds it there, and why that
  implementation reads a contiguous file with a SIMD library rather than SQLite.
- [Overflow tree](../OVERFLOW_TREE_DESIGN.md) — dividing a long record into
  spans that each fit the embedding window, so a returned record can be quoted
  by the part that matters, without changing what recall returns.
- [Associative memory](../ASSOCIATIVE_MEMORY_DESIGN.md) — a declared graph of
  entities, aliases and relations that reconstructive recall follows as cues,
  bundling keys, evidence and roles; nothing declared, nothing changed.
- [Embedding degradation advisory](../DEGRADED_ADVISORY_DESIGN.md) — how recall
  reports a dead embedding layer instead of quietly getting worse.

## Research notes

What the design pages rest on: derivations, measurements, and refutations,
written to be checked rather than trusted. The
[overview](../research/index.md) explains the status vocabulary.

- [Adaptive fusion, a derivation](../research/adaptive-fusion-derivation.md) —
  combines the retrieval arms through the probability that each score would be
  exceeded by chance. The rule has a closed form for each row's influence, and
  today's reciprocal rank fusion is its limiting case.
- [Calibration and the admission floor](../research/calibration-admission-floor-2026-09.md) —
  three calibration methods on seven tasks where recall was losing. The floor
  is not the cause. The null distribution came from the wrong population of
  pairs. Small corpora starve the dense arm.
- [Where the loss is, a frozen-stage replay](../research/frozen-stage-replay-2026-09.md) —
  every stage of the Track B path scored on frozen embeddings, three models,
  pinned to the live pipeline. The pure-ranking tasks lose at fusion, Gorilla
  at admission, EPBench at the gate. QASPER's gain comes from replenishment.
- [Two arms, one decision](../research/adaptive-fusion-identifiability.md) — what
  a fusion rule must know to avoid those losses. Not per-arm calibration: how
  much the lexical arm adds *given* the dense score. A joint density ratio
  supplies that without a fitted weight, and the note derives closed forms for
  starvation, gate extinction, and reservation.
