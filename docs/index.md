# CPersona Documentation

CPersona is an [MCP](https://modelcontextprotocol.io/) server. It gives
Claude — or any MCP-capable agent — **memory that survives across sessions**.

Memories are kept in one local SQLite file. Recall searches them three ways
(vector, FTS5, keyword) and fuses the results by rank or by relative score.

The server has **zero LLM dependency**: it never calls a generative model. Two
things that does not mean:

- **It is not always free.** `EMBEDDING_MODE=api` bills every embedding request
  against an endpoint that defaults to OpenAI's. `http` mode, pointed at an
  embedding server you run, does not.
- **It is not identical everywhere.** Recall is deterministic once the quality
  gate is calibrated. Calibration samples your corpus at random, so two
  installs holding the same data can settle on different gates.

> **Applies to: CPersona {{ version_line }}.** This site is the canonical documentation.
> If the README or the bundled skill disagrees with a page here, this site
> wins, and the disagreement is a bug worth
> [reporting](https://github.com/Cloto-dev/cpersona/issues/new?template=bug_report.yml).

## Where to go

<div class="grid cards" markdown>

-   :material-rocket-launch:{ .lg .middle } **Getting Started**

    ---

    Install it, register it with an MCP client, and verify the connection.

    [:octicons-arrow-right-24: Getting Started](getting-started.md)

-   :material-sitemap:{ .lg .middle } **Architecture**

    ---

    Where memories are stored, how the three retrievers search them, and what
    the fusion → gate → reverse pipeline does. With diagrams.

    [:octicons-arrow-right-24: Architecture](architecture.md)

-   :material-toolbox:{ .lg .middle } **Tools**

    ---

    Every tool, grouped by purpose. Tools that do not behave the way their
    name suggests link to the contract that explains them.

    [:octicons-arrow-right-24: Tools](tools.md)

-   :material-handshake:{ .lg .middle } **Behavior Contracts**

    ---

    Behaviours you can rely on. We treat a change to any of them as a bug.

    [:octicons-arrow-right-24: Behavior Contracts](behavior-contracts.md)

-   :material-cog:{ .lg .middle } **Configuration**

    ---

    Every environment variable and its default, and what the HTTP transport
    requires before it will answer.

    [:octicons-arrow-right-24: Configuration](configuration.md)

-   :material-lifebuoy:{ .lg .middle } **Operations Runbook**

    ---

    Backup, degradation detection, the order to tune recall in, Japanese
    corpora, maintenance cadence.

    [:octicons-arrow-right-24: Operations Runbook](operations.md)

-   :material-help-circle:{ .lg .middle } **FAQ**

    ---

    Short answers to the questions operators ask most, each linking to the
    page with the full detail.

    [:octicons-arrow-right-24: FAQ](faq.md)

-   :material-shield-check:{ .lg .middle } **Quality Assurance**

    ---

    How a release is gated: audit rounds, the bug ledger, structural CI gates,
    mutation proof.

    [:octicons-arrow-right-24: Quality Assurance](quality-assurance.md)

</div>

## Design notes and standards

Two kinds of page sit below the guides. They answer different questions.

**Project standards** define what a release, an audit report, or a generated
policy block must look like. Other projects can adopt them.

- [Release lifecycle standard](RELEASE_LIFECYCLE_STANDARD.md) — tier
  definitions (Stable / Current), the risk-triggered pre-release ladder, and
  support windows. What this repository actually runs is in
  [SUPPORT.md](https://github.com/Cloto-dev/cpersona/blob/master/SUPPORT.md).
- [SuperAuditor standard](SUPERAUDITOR_STANDARD.md) — how a client pulls
  findings from a server: the severity vocabulary, and what a cap means. It
  deliberately says nothing about which problems a server should detect.
- [Policy block standard](CLAUDE_MD_POLICY_STANDARD.md) — how a project's
  skill writes a marker-wrapped policy block into the file an agent loads
  every session (`CLAUDE.md`, `AGENTS.md`, …), and why a skill alone cannot
  guarantee the block is there.

**Where it is going.** The [roadmap](roadmap.md) records what each release
line is for, what it may break, and which measured problem each planned
feature answers. It covers three axes: release lines, runtime and scale,
support tiers. It describes intent, not delivery dates. What has shipped is in
the release notes and SUPPORT.md.

The 2.6 line has its own page, [Reliable Recall](RELIABLE_RECALL_2_6.md): the
recall loop that iterates inside a single call, the cue contract, the prior
function, the reconstruction exit and its count window, the failure taxonomy,
and what counts as done. The line after it has one too,
[Memory Intelligence](MEMORY_INTELLIGENCE_2_7.md) (2.7): correction and
contradiction, temporal state, evidence-weighted confidence, retention policy
and recall feedback — design only, with the undecided questions marked as
open.

**Design notes** record how one behaviour was decided, including the routes
that were rejected. They are point-in-time records. Where a note and the
guides disagree, the guides win.

- [Per-client capabilities (ACL)](ACL_DESIGN.md) — named bearer tokens,
  per-agent read/write grants, deny-by-default.
- [OAuth support](OAUTH_DESIGN.md) — resource-server metadata, token
  verification, the three routes that were weighed, and where the per-subject
  boundary falls.
- [Server-served operating context](OPERATING_CONTEXT_DESIGN.md) —
  distributing operator instructions to every connected MCP client.
- [Declared session identity](SESSION_IDENTITY_DESIGN.md) — why one process is
  not one session under streamable HTTP, and which process-global state
  `session_key` splits apart.
- [Recorded access origin](MEMORY_ORIGIN_DESIGN.md) — recording the observed
  caller on each stored row, for the paths where `agent_id` names nobody.
- [Recall preview tier](RECALL_PREVIEW_TIER_DESIGN.md) — preview truncation and
  the `get_contents` expansion path.
- [Contiguous embedding index](CONTIGUOUS_INDEX_DESIGN.md) — moving the vector
  scan off SQLite rows and onto a contiguous sidecar file. The answers do not
  change.
- [Reach and recency in the scan window](SCAN_WINDOW_REACH_DESIGN.md) — why a
  wider vector scan window loses recent answers, and the second ranked list
  that widens it without giving up the recency preference.
- [Reach, recency and the far vote](REACH_AND_RECENCY_PLAN.md) — three
  measurements in one account, what each establishes, and the plan for pricing
  the far vote in the 2.6 line.
- [Adaptive fusion](ADAPTIVE_FUSION_DESIGN.md) — reserving each retriever a
  share of the pool, removing the rank cut from the pool-size gate, and the
  pre-registered comparison between a measured lexical weight now and a
  conditional-evidence fusion mode later.
- [Overflow tree](OVERFLOW_TREE_DESIGN.md) — dividing a long record into
  spans that each fit the embedding window, so a returned record can be quoted
  by the part that matters, without changing what recall returns.
- [Associative memory](ASSOCIATIVE_MEMORY_DESIGN.md) — a declared graph of
  entities, aliases and relations that reconstructive recall follows as cues,
  bundling keys, evidence and roles; nothing declared, nothing changed.
- [Embedding degradation advisory](DEGRADED_ADVISORY_DESIGN.md) — how recall
  reports a dead embedding layer instead of quietly getting worse.

## Research notes

What the design pages rest on: derivations, measurements, and refutations,
written to be checked rather than trusted. The
[overview](research/index.md) explains the status vocabulary.

- [Adaptive fusion, a derivation](research/adaptive-fusion-derivation.md) —
  combines the retrieval arms through the probability that each score would be
  exceeded by chance. The rule has a closed form for each row's influence, and
  today's reciprocal rank fusion is its limiting case.
- [Calibration and the admission floor](research/calibration-admission-floor-2026-09.md) —
  three calibration methods on seven tasks where recall was losing. The floor
  is not the cause. The null distribution came from the wrong population of
  pairs. Small corpora starve the dense arm.
- [Where the loss is, a frozen-stage replay](research/frozen-stage-replay-2026-09.md) —
  every stage of the Track B path scored on frozen embeddings, three models,
  pinned to the live pipeline. The pure-ranking tasks lose at fusion, Gorilla
  at admission, EPBench at the gate. QASPER's gain comes from replenishment.
- [Two arms, one decision](research/adaptive-fusion-identifiability.md) — what
  a fusion rule must know to avoid those losses. Not per-arm calibration: how
  much the lexical arm adds *given* the dense score. A joint density ratio
  supplies that without a fitted weight, and the note derives closed forms for
  starvation, gate extinction, and reservation.

## The three memory types

- **Declarative** — individual facts, decisions, rules (`store` / `recall`).
- **Episodic** — session summaries (`archive_episode`), which also drive the
  [episode boundary penalty](behavior-contracts.md#3-episode-boundary-penalty).
- **Profile** — accumulated user/project attributes (`update_profile`), with a
  [scoring caveat](behavior-contracts.md#7-profile-rows-carry-no-score) worth
  knowing.

## For AI agents reading this site

A machine-readable index of these pages is published at
[`llms.txt`](llms.txt). The bundled
[`cpersona-memory` skill](https://github.com/Cloto-dev/cpersona/tree/master/skills/cpersona-memory)
teaches an agent the day-to-day store / recall / archive workflow and links
back here for the canonical detail.

## :material-gift-outline: Sponsorship { #sponsorship }

CPersona is MIT-licensed and will stay that way. If it has become useful and
you want the work to continue, the [sponsorship](sponsorship.md) page explains
what sponsoring does and does not buy. It also lists the ways to help that
cost nothing.
