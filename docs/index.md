# CPersona Documentation

CPersona is an [MCP](https://modelcontextprotocol.io/) server that gives
Claude — or any MCP-capable agent — **persistent memory across sessions**.
Memories live in a single local SQLite file and are retrieved with a 3-layer
hybrid search (vector + FTS5 + keyword, fused by rank or relative score). The
server has **zero LLM dependency**: it never calls a generative model, so
memory adds no API cost and stays deterministic.

> **Applies to: CPersona 2.5.x.** This site is the canonical documentation —
> when the README or the bundled skill disagrees with a page here, this site
> wins, and the discrepancy is a bug worth
> [reporting](https://github.com/Cloto-dev/cpersona/issues/new?template=bug_report.yml).

## Where to go

| You want to… | Read |
|---|---|
| Install and set up | [README Quick Start](https://github.com/Cloto-dev/cpersona#quick-start) (also the PyPI page) |
| Know what behaviors you can rely on | [Behavior Contracts](behavior-contracts.md) |
| Run it well: backup, tuning, degradation, corpus indexing | [Operations Runbook](operations.md) |
| Look up an environment variable | [Configuration](configuration.md) |
| Quick answers to common operator questions | [FAQ](faq.md) |
| Understand a subsystem's design | Design documents (sidebar) |
| Release tiers and support windows | [Release lifecycle](RELEASE_LIFECYCLE_STANDARD.md) + [SUPPORT.md](https://github.com/Cloto-dev/cpersona/blob/master/SUPPORT.md) |

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
