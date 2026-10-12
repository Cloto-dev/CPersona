# CPersona Documentation

CPersona is an [MCP](https://modelcontextprotocol.io/) server. It gives
Claude — or any MCP-capable agent — **memory that survives across sessions**.

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

-   :material-database-arrow-up:{ .lg .middle } **Upgrading to 2.6**

    ---

    Taking a 2.5.x store to 2.6.0 in one pass: backup,
    what the first start migrates, what to build afterwards, how to go back.

    [:octicons-arrow-right-24: Upgrading to 2.6](upgrading-to-2.6.md)

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

-   :material-flask-outline:{ .lg .middle } **Design and research**

    ---

    How each behaviour was decided, the roadmap, the standards other projects
    can adopt, and the measurements underneath. Not needed to use CPersona.

    [:octicons-arrow-right-24: Design and research](design/index.md)

</div>

## How it works

Memories are kept in one local SQLite file. Recall searches them three ways
(vector, FTS5, keyword) and fuses the results by rank or by relative score.

The server has **zero LLM dependency**: it never calls a generative model. That
does not mean it is free, or that it behaves the same everywhere:

- **It is not always free.** `EMBEDDING_MODE=api` bills every embedding request
  against an endpoint that defaults to OpenAI's. `http` mode, pointed at an
  embedding server you run, does not.
- **It is not identical everywhere.** Recall is deterministic once the quality
  gate is calibrated. Calibration samples your corpus at random, so two
  installs holding the same data can settle on different gates.

## The three memory types

- **Declarative** — individual facts, decisions, rules (`store` / `recall`).
- **Episodic** — session summaries (`archive_episode`), which also drive the
  opt-in [episode boundary penalty](behavior-contracts.md#3-episode-boundary-penalty).
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
