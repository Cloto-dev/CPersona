<!-- mcp-name: io.github.Cloto-dev/cpersona -->

<div align="center">

# CPersona

### MCP Memory Server

Give Claude persistent memory across sessions.
Single SQLite file. 31 tools. Zero LLM dependency.

[![PyPI](https://img.shields.io/pypi/v/cpersona)](https://pypi.org/project/cpersona/) [![CI](https://github.com/Cloto-dev/cpersona/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/Cloto-dev/cpersona/actions/workflows/ci.yml) [![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://github.com/Cloto-dev/cpersona/blob/master/pyproject.toml) [![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](https://github.com/Cloto-dev/cpersona/blob/master/LICENSE) [![Sponsor](https://img.shields.io/badge/sponsor-Cloto--dev-ea4aaa?logo=githubsponsors&logoColor=white)](https://github.com/sponsors/Cloto-dev)

[Documentation](https://cloto-dev.github.io/CPersona/) · [Getting Started](https://cloto-dev.github.io/CPersona/getting-started/) · [Architecture](https://cloto-dev.github.io/CPersona/architecture/) · [Tools](https://cloto-dev.github.io/CPersona/tools/) · [PyPI](https://pypi.org/project/cpersona/) · [Zenn Book (JP)](https://zenn.dev/cloto/books/claude-memory-mcp-server)

</div>

---

> **Standalone repository** — This is the standalone version for use with Claude Desktop, Claude Code, and any MCP client.
> If you are a [ClotoCore](https://github.com/Cloto-dev/ClotoCore) user, install CPersona from the in-app marketplace ([ClotoHub](https://hub.cloto.dev)) instead — it distributes this same repository.

> **Project status** — **2.4.x is Stable**; **2.5.x is Current**, an internal
> stabilization line where all fixes land, pending production-soak
> certification. The DB schema is preserved across the line. Additive,
> rollback-safe features may land here as well ([lifecycle standard
> §2.6](https://cloto-dev.github.io/CPersona/RELEASE_LIFECYCLE_STANDARD/#26-feature-releases-within-a-line));
> a change that cannot be rolled back waits for 2.6. Which version to run, and
> how long each line keeps receiving fixes:
> [SUPPORT.md](https://github.com/Cloto-dev/cpersona/blob/master/SUPPORT.md).

> **Upgrading from 2.5.2 or earlier?** Two things need a decision from you.
> **v2.5.3 will not start the HTTP transport without `CPERSONA_AUTH_TOKEN`**,
> wherever it binds — set one, or opt out with
> `CPERSONA_ALLOW_UNAUTHENTICATED_HTTP=true` ([why](https://github.com/Cloto-dev/cpersona/blob/master/SUPPORT.md#known-issues); stdio is unaffected).
> **v2.5.2 changed tool response shapes** — branch on `ok is false`, and treat any
> response carrying `error` as a failure whether or not `ok` is present
> ([contract §10](https://cloto-dev.github.io/CPersona/behavior-contracts/#10-response-shapes-how-to-tell-success-from-failure)).

## The Problem

Claude forgets everything between sessions. Every conversation starts from zero — no context about your project, your preferences, or what you discussed yesterday.

cpersona fixes this. It's an [MCP](https://modelcontextprotocol.io/) server that stores memories in a local SQLite file and retrieves them through hybrid search. Claude remembers you. It runs against any MCP-compatible host — [Claude Desktop](https://claude.ai/download), [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [ClotoCore](https://github.com/Cloto-dev/ClotoCore) (the AI agent platform where cpersona originated, and whose memory layer it is), or a client of your own.

## Quick Start

> **Claude Code? Let the agent do the setup.** The wheel ships an
> [Agent Skill](https://github.com/Cloto-dev/cpersona/blob/master/skills/cpersona-memory/SKILL.md)
> that installs everything *and* teaches Claude when to store, recall and
> archive. Copy it in, then say *"Set up CPersona."*
>
> ```bash
> python -c "import cpersona,pathlib,shutil; s=pathlib.Path(cpersona.__file__).parent/'skills'/'cpersona-memory'; shutil.copytree(s, pathlib.Path.home()/'.claude/skills/cpersona-memory', dirs_exist_ok=True)"
> ```

**1. Install** — Python 3.11+, and [uv](https://docs.astral.sh/uv/) for the one-command path.

```bash
uvx cpersona          # run directly, no install step
pip install cpersona  # or install it
```

**2. Run an embedding server** — strongly recommended; it powers the vector layer

```bash
uvx --from "cembedding[onnx]" cembedding-download-model --model jina-v5-nano
EMBEDDING_PROVIDER=onnx_jina_v5_nano uvx --from "cembedding[onnx]" cembedding   # serves http://127.0.0.1:8401/embed
```

Any endpoint implementing the [embedding contract](https://cloto-dev.github.io/CPersona/getting-started/#the-contract) works and is equally recommended; CEmbedding is the reference implementation. The choice of backend is yours — the recommendation is to connect one, not to connect that one.

**Without a backend, cpersona still runs** — FTS5 + keyword search, and it says on every recall that it is degraded rather than quietly returning less. That is a supported fallback, not a recommended way to run: recall then matches on shared words, so a memory phrased differently from your question can be missed, and so can an older one.

**3. Register it with your MCP client**

```bash
claude mcp add-json cpersona '{"type":"stdio","command":"uvx","args":["cpersona"],"env":{"CPERSONA_DB_PATH":"/home/you/.claude/cpersona.db","EMBEDDING_MODE":"http","EMBEDDING_HTTP_URL":"http://127.0.0.1:8401/embed"}}' -s user
```

That's it. Ask Claude to `store` something and `recall` it in a later session.

At startup the server asks pypi.org whether a newer release exists and tells the
calling agent through `recall`; set `CPERSONA_UPDATE_CHECK=false` to turn that
off. Updating is never automatic.

Claude Desktop config, Windows paths, installing from source and the full
walkthrough: [Getting Started](https://cloto-dev.github.io/CPersona/getting-started/).

## What You Get

- **Hybrid search** — vector (the layer an embedding server powers), FTS5
  (trigram, so it works on Japanese and other space-less scripts) and keyword,
  fused by rank or relative score. The FTS and keyword layers rescue what vectors
  miss: identifiers, error strings, exact names.
- **Three memory types** — facts, session summaries and an accumulated profile.
- **Zero LLM dependency** — cpersona never calls a generative model; your agent
  summarizes and hands over the result. Recall is deterministic given a calibrated
  gate, but the gate is sampled, so two installs on identical data can settle
  differently.
- **Single-file SQLite** — no external database; `sqlite3 .backup` copies the
  corpus (the calibration sidecar beside it needs copying too).
- **Operable** — auto-calibrated thresholds, a health check with auto-repair, an
  advisory when the embedding layer dies, JSONL export/import, agent-to-agent merge.
- **Isolation** — `agent_id`, `project_id` and `channel` let several agents and
  projects share one database without bleeding into each other.

How it fits together: [Architecture](https://cloto-dev.github.io/CPersona/architecture/) ·
what the tools do: [Tools](https://cloto-dev.github.io/CPersona/tools/) ·
what you may rely on: [Behavior Contracts](https://cloto-dev.github.io/CPersona/behavior-contracts/).

## Benchmarks

Measured on LMEB (Long-horizon Memory Embedding Benchmark, arXiv:2603.12572) — 22 datasets subsuming LoCoMo and LongMemEval, measured here as 22 retrieval tasks. The metric is Mean NDCG@10 across all 22 tasks. **Track A** is the raw embedding model alone; **Track B** routes the same embeddings through cpersona's real `store`/`recall` code paths (SQLite + FTS5 + RRF fusion + per-agent auto-calibration).

| Embedding Model | Params | Dim | Track A (raw) | Track B (cpersona) | Δ |
|---|---|---|---|---|---|
| all-MiniLM-L6-v2 | 22M | 384 | 43.67 | **50.10** | +6.43 |
| bge-m3 | 568M | 1024 | 56.83 | **57.66** | +0.83 |

Track B lands at or above Track A on both models: the fusion layers add signal rather than merely persisting vectors, and a weaker embedding gains more because the FTS5/keyword layers rescue what its vectors miss. How to read the deltas, the noise envelope, the measurement harness and the reproduction regime: [`benchmarks/`](https://github.com/Cloto-dev/cpersona/blob/master/benchmarks/README.md).

## Documentation

[**cloto-dev.github.io/CPersona**](https://cloto-dev.github.io/CPersona/) is canonical — when this README disagrees with it, the site wins.

| | |
|---|---|
| [Getting Started](https://cloto-dev.github.io/CPersona/getting-started/) | Install, embedding server, client registration, verification |
| [Behavior Contracts](https://cloto-dev.github.io/CPersona/behavior-contracts/) | What you may rely on: recall ordering, dedup, scan window, response shapes |
| [Tools](https://cloto-dev.github.io/CPersona/tools/) | All 31 tools, grouped by what you reach for them for |
| [Architecture](https://cloto-dev.github.io/CPersona/architecture/) | Storage, the retrieval pipeline, isolation axes |
| [Operations Runbook](https://cloto-dev.github.io/CPersona/operations/) | Backup, degradation detection, tuning, CJK guidance, corpus sync |
| [Configuration](https://cloto-dev.github.io/CPersona/configuration/) | Every environment variable and its default |
| [Quality Assurance](https://cloto-dev.github.io/CPersona/quality-assurance/) | How a release is gated: audits, the bug ledger, structural and mutation gates |
| [FAQ](https://cloto-dev.github.io/CPersona/faq/) | Short answers to the questions operators actually ask |

Japanese translations are in the language selector (English is canonical) and
agents can read [`llms.txt`](https://cloto-dev.github.io/CPersona/llms.txt).
Longer reads in Japanese: a [book](https://zenn.dev/cloto/books/claude-memory-mcp-server)
on the design and setup, and an [article](https://zenn.dev/cloto/articles/claude-code-compact-external-memory)
on the token economics of session-end → `/clear` → `recall`.

## Quality Assurance

Every release is gated by a machine-verifiable process: multi-agent audit rounds with adversarial verification, a [bug ledger](https://github.com/Cloto-dev/cpersona/blob/master/qa/issue-registry.json) that fails CI if a fix marker disappears or a removed defect returns, structural gates for invariants a plain test cannot express, a mutation proof that those gates go red when the invariant is broken, and gates holding the documented counts, defaults and version claims to the source that defines them.

Behind it: **~1,390 test functions** across ~117 test modules (~1,750 cases parametrised, more test code than server code), on **Schema v13** — [how a release is gated](https://cloto-dev.github.io/CPersona/quality-assurance/).

## Support

Three tiers — **Stable** (production-certified, critical fixes only), **Current**
(newest line, all fixes land here) and **Experimental** (opt-in pre-releases). A
superseded line keeps critical-fix support for 30 more days. **Read
[SUPPORT.md § Known issues](https://github.com/Cloto-dev/cpersona/blob/master/SUPPORT.md#known-issues)
before pinning a version** — some of them change what you should run.

Found a bug, or something the docs do not explain? Open a
[bug report](https://github.com/Cloto-dev/cpersona/issues/new?template=bug_report.yml)
or [feature request](https://github.com/Cloto-dev/cpersona/issues/new?template=feature_request.yml),
even when you are not certain — a configuration problem mistaken for a bug means
the documentation was unclear, which is a defect of its own. Report security
vulnerabilities privately via
[SECURITY.md](https://github.com/Cloto-dev/cpersona/blob/master/SECURITY.md).

## Sponsorship

CPersona is MIT-licensed and stays fully usable whether or not anyone sponsors
it. Sponsorship buys no feature, no release tier and no position in the issue
queue — issues are triaged by impact, reproducibility and safety, and that does
not change for anyone.

If CPersona has earned a place in your workflow and you would like the work to
continue, you can [sponsor Cloto-dev on GitHub](https://github.com/sponsors/Cloto-dev).
The same page covers CPersona, ClotoCore and the other projects published
under that account; sponsorship goes toward development time, testing
and infrastructure, documentation and maintenance.

Money is not the only thing that helps, and it is not the thing this project
needs most. Starring the repository, saying which part of the setup was
confusing, filing a reproducible issue, or correcting a sentence in the
documentation all move it forward.

## License

MIT — free to use from any MCP host without restriction.
