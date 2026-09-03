# CPersona Documentation

CPersona is an [MCP](https://modelcontextprotocol.io/) server that gives
Claude — or any MCP-capable agent — **persistent memory across sessions**.
Memories live in a single local SQLite file and are retrieved with a 3-layer
hybrid search (vector + FTS5 + keyword, fused by rank or relative score). The
server has **zero LLM dependency**: it never calls a generative model. Two
caveats on what that buys you — embeddings can still cost money
(`EMBEDDING_MODE=api` bills per request against an endpoint that defaults to
OpenAI's; `http` mode against a local server does not), and recall is
deterministic given a calibrated gate, but the gate itself is measured by
sampling the corpus at random, so two installs on identical data can settle on
different operating points.

> **Applies to: CPersona 2.5.x.** This site is the canonical documentation —
> when the README or the bundled skill disagrees with a page here, this site
> wins, and the discrepancy is a bug worth
> [reporting](https://github.com/Cloto-dev/cpersona/issues/new?template=bug_report.yml).

## Where to go

<div class="grid cards" markdown>

-   :material-rocket-launch:{ .lg .middle } **Getting Started**

    ---

    Install it, register it with an MCP client, and verify the connection end
    to end.

    [:octicons-arrow-right-24: Getting Started](getting-started.md)

-   :material-sitemap:{ .lg .middle } **Architecture**

    ---

    Storage layout, the three retrievers, and the fusion → gate → reverse
    pipeline, drawn.

    [:octicons-arrow-right-24: Architecture](architecture.md)

-   :material-toolbox:{ .lg .middle } **Tools**

    ---

    All 31 tools grouped by purpose, each linked to the contract it can
    surprise you with.

    [:octicons-arrow-right-24: Tools](tools.md)

-   :material-handshake:{ .lg .middle } **Behavior Contracts**

    ---

    The behaviours you may rely on, stated so that changing one is a bug and
    not a preference.

    [:octicons-arrow-right-24: Behavior Contracts](behavior-contracts.md)

-   :material-cog:{ .lg .middle } **Configuration**

    ---

    Every environment variable with its default, and what the HTTP transport
    requires before it will serve.

    [:octicons-arrow-right-24: Configuration](configuration.md)

-   :material-lifebuoy:{ .lg .middle } **Operations Runbook**

    ---

    Backup, degradation detection, the order to tune recall in, Japanese
    corpora, maintenance cadence.

    [:octicons-arrow-right-24: Operations Runbook](operations.md)

-   :material-help-circle:{ .lg .middle } **FAQ**

    ---

    Short answers to the questions operators actually ask, each pointing at
    the page that carries the detail.

    [:octicons-arrow-right-24: FAQ](faq.md)

-   :material-shield-check:{ .lg .middle } **Quality Assurance**

    ---

    How a release is gated: audit rounds, the bug ledger, structural CI gates,
    mutation proof.

    [:octicons-arrow-right-24: Quality Assurance](quality-assurance.md)

</div>

## Design notes and standards

Below the guides sit two kinds of page, and they answer different questions.

**Project standards** say what a release, an audit report or a generated policy
block MUST look like. They are written to be adopted by projects other than
this one.

- [Release lifecycle standard](RELEASE_LIFECYCLE_STANDARD.md) — tier definitions
  (Stable / Current), the risk-triggered pre-release ladder, and support
  windows. The instance this repository runs is
  [SUPPORT.md](https://github.com/Cloto-dev/cpersona/blob/master/SUPPORT.md).
- [SuperAuditor standard](SUPERAUDITOR_STANDARD.md) — the pull contract for
  reporting findings: severity vocabulary, cap semantics, and a deliberate
  silence on what a server chooses to detect.
- [CLAUDE.md policy standard](CLAUDE_MD_POLICY_STANDARD.md) — how a project's
  skill writes a marker-wrapped policy block into always-loaded agent memory,
  and why a skill alone cannot carry that guarantee.

**Design notes** record how one behaviour was decided, the routes that were
rejected included. They are point-in-time records: where a note and the guides
above disagree, the guides win.

- [Per-client capabilities (ACL)](ACL_DESIGN.md) — named bearer tokens,
  per-agent read/write grants, deny-by-default.
- [OAuth support](OAUTH_DESIGN.md) — resource-server metadata and token
  verification, the three routes weighed, and the per-subject boundary.
- [Server-served operating context](OPERATING_CONTEXT_DESIGN.md) — distributing
  operator instructions to every connected MCP client.
- [Declared session identity](SESSION_IDENTITY_DESIGN.md) — why one process is
  not one session under streamable-HTTP, and which process-global state
  `session_key` re-partitions.
- [Recorded access origin](MEMORY_ORIGIN_DESIGN.md) — recording the observed
  caller on each stored row, for the paths where `agent_id` names nobody.
- [Recall preview tier](RECALL_PREVIEW_TIER_DESIGN.md) — preview truncation and
  the `get_contents` expansion path.
- [Contiguous embedding index](CONTIGUOUS_INDEX_DESIGN.md) — moving the vector
  scan's read off SQLite rows onto a contiguous sidecar, bit-identically.
- [Reach and recency in the scan window](SCAN_WINDOW_REACH_DESIGN.md) — why
  widening the vector scan window loses recent answers, and the second ranked
  list that lets reach move without removing the recency prior.
- [Embedding degradation advisory](DEGRADED_ADVISORY_DESIGN.md) — how recall
  reports a dead embedding layer instead of quietly getting worse.

## The three memory types

- **Declarative** — individual facts, decisions, rules (`store` / `recall`).
- **Episodic** — session summaries (`archive_episode`), which also drive the
  [episode boundary penalty](behavior-contracts.md#3-episode-boundary-penalty).
- **Profile** — accumulated user/project attributes (`update_profile`), with
  a [scoring caveat](behavior-contracts.md#7-profile-rows-carry-no-score)
  worth knowing.

## For AI agents reading this site

A machine-readable index of these pages is published at
[`llms.txt`](llms.txt). The bundled
[`cpersona-memory` skill](https://github.com/Cloto-dev/cpersona/tree/master/skills/cpersona-memory)
teaches an agent the day-to-day store / recall / archive workflow and links
back here for the canonical detail.

## :material-gift-outline: Sponsorship { #sponsorship }

CPersona is MIT-licensed and stays that way regardless. If it has become useful
and you want the work to continue, [sponsorship](sponsorship.md) explains what
it does and does not buy — and the ways to help that cost nothing, which for a
project this size matter more.
