<!-- mcp-name: io.github.Cloto-dev/cpersona -->

<div align="center">

# CPersona

### MCP Memory Server

Give Claude persistent memory across sessions.
Single SQLite file. 29 tools. Zero LLM dependency.

[![PyPI](https://img.shields.io/pypi/v/cpersona)](https://pypi.org/project/cpersona/)
[![CI](https://github.com/Cloto-dev/cpersona/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/Cloto-dev/cpersona/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://github.com/Cloto-dev/cpersona/blob/master/pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](https://github.com/Cloto-dev/cpersona/blob/master/LICENSE)

[Documentation](https://cloto-dev.github.io/CPersona/) · [Getting Started](https://cloto-dev.github.io/CPersona/getting-started/) · [Architecture](https://cloto-dev.github.io/CPersona/architecture/) · [Tools](https://cloto-dev.github.io/CPersona/tools/) · [PyPI](https://pypi.org/project/cpersona/) · [Zenn Book (JP)](https://zenn.dev/cloto/books/claude-memory-mcp-server)

</div>

---

> **Standalone repository** — This is the standalone version for use with Claude Desktop, Claude Code, and any MCP client.
> If you are a [ClotoCore](https://github.com/Cloto-dev/ClotoCore) user, install CPersona from the in-app marketplace ([ClotoHub](https://hub.cloto.dev)) instead — it distributes this same repository.

> **Project status (August 2026)** — **2.4.x is the Stable line** (latest
> v2.4.41, gated by three comprehensive audit rounds). **2.5.x is the Current
> line** (latest v2.5.5): an internal stabilization line that has passed the
> full release gate and is where all fixes land, pending production-soak
> certification. The DB schema is preserved across the line, and feature
> development resumes in 2.6. Tiers and support windows:
> [Release Channels & Support](#release-channels--support).

> **Upgrading from 2.5.2 or earlier?** Two changes need a decision from you:
>
> - **v2.5.3 refuses to start the HTTP transport when `CPERSONA_AUTH_TOKEN` is
>   unset**, wherever it binds. Earlier versions allowed an unauthenticated
>   loopback bind, which a tunnel or reverse proxy silently turns into public
>   exposure (bug-198, HIGH). Set a token, or state that you really want none
>   with `CPERSONA_ALLOW_UNAUTHENTICATED_HTTP=true`. stdio is unaffected —
>   [details](https://cloto-dev.github.io/CPersona/configuration/#remote-http-transport).
> - **v2.5.2 changed tool response shapes.** Branch on `ok is false`, and treat
>   any response carrying `error` as a failure whether or not `ok` is present —
>   [contract §10](https://cloto-dev.github.io/CPersona/behavior-contracts/#10-response-shapes-how-to-tell-success-from-failure).

## The Problem

Claude forgets everything between sessions. Every conversation starts from zero — no context about your project, your preferences, or what you discussed yesterday.

cpersona fixes this. It's an [MCP](https://modelcontextprotocol.io/) server that stores memories in a local SQLite file and retrieves them through hybrid search. Claude remembers you.

## Quick Start

**Prerequisites:** Python 3.11+ (and [uv](https://docs.astral.sh/uv/) for the one-command path).

> **Claude Code? Let the agent do the setup.** This repo ships an
> [Agent Skill](https://github.com/Cloto-dev/cpersona/blob/master/skills/cpersona-memory/SKILL.md)
> that installs everything *and* teaches Claude when to store, recall, and
> archive afterwards. Copy it in, then say *"Set up CPersona — I want
> persistent memory."*
>
> ```bash
> # Installed from PyPI? The skill ships inside the wheel — no clone needed:
> python -c "import cpersona,pathlib,shutil; s=pathlib.Path(cpersona.__file__).parent/'skills'/'cpersona-memory'; shutil.copytree(s, pathlib.Path.home()/'.claude/skills/cpersona-memory', dirs_exist_ok=True)"
> ```

**1. Install**

```bash
uvx cpersona          # run directly, no install step
# or
pip install cpersona
```

**2. Run an embedding server** (recommended — it powers the vector layer)

```bash
uvx --from "cembedding[onnx]" cembedding-download-model --model jina-v5-nano
EMBEDDING_PROVIDER=onnx_jina_v5_nano uvx --from "cembedding[onnx]" cembedding   # serves http://127.0.0.1:8401/embed
```

Any HTTP endpoint implementing the [embedding contract](https://cloto-dev.github.io/CPersona/getting-started/#the-contract)
works. Without one, cpersona runs on FTS5 + keyword search and tells you it is degraded.

**3. Register it with your MCP client**

```bash
claude mcp add-json cpersona '{"type":"stdio","command":"uvx","args":["cpersona"],"env":{"CPERSONA_DB_PATH":"/home/you/.claude/cpersona.db","EMBEDDING_MODE":"http","EMBEDDING_HTTP_URL":"http://127.0.0.1:8401/embed"}}' -s user
```

That's it. Ask Claude to `store` something and `recall` it in a later session.

**Claude Desktop config, Windows paths, installing from source, and the full
setup walkthrough:**
[Getting Started](https://cloto-dev.github.io/CPersona/getting-started/).

## What You Get

- **Hybrid search** — vector (cosine), FTS5 (trigram tokenizer, so it works on
  Japanese and other space-less scripts), and keyword matching, fused by rank
  or relative score. The FTS/keyword layers rescue the queries vector search
  misses: identifiers, error strings, exact names.
- **Three memory types** — declarative facts (`store`), session summaries
  (`archive_episode`), and an accumulated profile (`update_profile`).
- **Zero LLM dependency** — cpersona never calls a generative model. Your agent
  does the summarizing and hands over the result. Embeddings are a separate
  question: `EMBEDDING_MODE=http` talks to a local server and costs nothing per
  call, while `api` mode bills against an OpenAI-compatible endpoint. Recall is
  deterministic given a calibrated gate, though the gate is measured by random
  sampling, so two installs on identical data can settle differently.
- **Single-file SQLite** — no external database. `sqlite3 .backup` copies the
  whole corpus; the calibration sidecar beside it needs copying too
  ([backup runbook](https://cloto-dev.github.io/CPersona/operations/#backup-and-restore)).
- **Operable** — auto-calibrated retrieval thresholds, a severity-tagged health
  check with auto-repair, an advisory that tells you when the embedding layer
  has died, JSONL export/import, and agent-to-agent merge.
- **Isolation** — `agent_id`, `project_id` and `channel` axes let several
  agents and projects share one database without bleeding into each other.

How it all fits together: [Architecture](https://cloto-dev.github.io/CPersona/architecture/).
What each of the 29 tools does: [Tools](https://cloto-dev.github.io/CPersona/tools/).

## Benchmarks

Measured on LMEB (Long-horizon Memory Embedding Benchmark, arXiv:2603.12572) — 22 datasets subsuming LoCoMo and LongMemEval, measured here as 22 retrieval tasks. The metric is Mean NDCG@10 across all 22 tasks.

Two tracks isolate the pipeline's contribution:

- **Track A** — the raw embedding model alone (baseline retrieval).
- **Track B** — the same embeddings routed through cpersona's real `store`/`recall` code paths: SQLite + FTS5 + RRF fusion + per-agent auto-calibration (cpersona v2.4.40, full-ranking regime).

| Embedding Model | Params | Dim | Track A (raw) | Track B (cpersona) | Δ |
|---|---|---|---|---|---|
| all-MiniLM-L6-v2 | 22M | 384 | 43.67 | **50.10** | +6.43 |
| bge-m3 | 568M | 1024 | 56.83 | **57.66** | +0.83 |

On both models measured here, Track B lands at or above Track A — the fusion layers add signal rather than merely persisting vectors. The size of that contribution depends on the embedding: the FTS5/keyword layers rescue queries the vector search alone misses, so a weaker embedding gains more (+6.43 on all-MiniLM-L6-v2), while a strong one moves within the harness's run-to-run noise (+0.83 on bge-m3, against ±1–2 pt per task mean). Read the deltas as "the pipeline does not cost ranking quality, and recovers a lot of it on weaker embeddings" rather than as a uniform gain. Methodology, the measurement harness, the noise envelope, and the reproduction regime live in [`benchmarks/`](https://github.com/Cloto-dev/cpersona/blob/master/benchmarks/README.md).

## Documentation

[**cloto-dev.github.io/CPersona**](https://cloto-dev.github.io/CPersona/) is the
canonical documentation — when this README disagrees with it, the site wins.

| | |
|---|---|
| [Getting Started](https://cloto-dev.github.io/CPersona/getting-started/) | Install, embedding server, client registration, verification |
| [Behavior Contracts](https://cloto-dev.github.io/CPersona/behavior-contracts/) | What you may rely on: recall ordering, dedup, scan window, response shapes |
| [Tools](https://cloto-dev.github.io/CPersona/tools/) | All 29 tools, grouped by what you reach for them for |
| [Architecture](https://cloto-dev.github.io/CPersona/architecture/) | Storage, the retrieval pipeline, isolation axes |
| [Operations Runbook](https://cloto-dev.github.io/CPersona/operations/) | Backup, degradation detection, tuning, CJK guidance, corpus sync |
| [Configuration](https://cloto-dev.github.io/CPersona/configuration/) | Every environment variable and its default |
| [FAQ](https://cloto-dev.github.io/CPersona/faq/) | Short answers to the questions operators actually ask |

Japanese translations of the main pages are available from the language
selector; the English pages are canonical. An index for AI agents is published
at [`llms.txt`](https://cloto-dev.github.io/CPersona/llms.txt).

## Stats

- **~14,100 LOC** Python across focused modules, plus a 3,300-line vendored MCP
  common snapshot
- **~950 test functions** across ~86 test modules — ~1,190 cases once the
  behavioural matrix is parametrised (~28,900 LOC, more test code than server
  code), including structural-enforcement gates
- **Schema v13** (auto-migrating)
- **MIT License**

## Works With

cpersona is an MCP server — it works with any MCP-compatible host: [Claude Desktop](https://claude.ai/download), [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [ClotoCore](https://github.com/Cloto-dev/ClotoCore) (the AI agent platform where cpersona originated, and whose memory layer it is), or a custom MCP client. cpersona is fully standalone and MIT-licensed.

## Quality Assurance

Every release is gated by a machine-verifiable quality process:

- **Audit-gated releases** — before a release is cut, the codebase goes through
  comprehensive multi-agent audit rounds (independent finders per dimension,
  each finding adversarially verified from multiple lenses). v2.4.39 shipped
  after three such rounds — 43 fixes, every one re-verified against the tree
  it landed on.
- **Issue registry** — every audited defect lives in
  [`qa/issue-registry.json`](https://github.com/Cloto-dev/cpersona/blob/master/qa/issue-registry.json)
  with a machine-checkable code pattern, and
  [`scripts/verify-issues.sh`](https://github.com/Cloto-dev/cpersona/blob/master/scripts/verify-issues.sh)
  fails loudly if a fix marker disappears or a removed defect returns.
- **Structural CI gates** — invariants a plain test can't express are enforced
  by AST- and behaviour-level gates in the pytest suite (Python 3.11/3.13):
  every writer holds the shared write lock, agent-scoped SQL carries its
  isolation predicates, identity/dedup probes carry the project/channel axes,
  and `check_health` performs no embedding network I/O while holding the lock.
- **Documented facts are gated too** — tool counts, schema version and
  environment-variable defaults in the docs are checked against the source that
  defines them, and Japanese translations are checked against the English
  content they were translated from.
- **Release lifecycle standard** — the release process itself is specified in
  [RELEASE_LIFECYCLE_STANDARD](https://cloto-dev.github.io/CPersona/RELEASE_LIFECYCLE_STANDARD/)
  (v1.0), piloted here as the reference implementation for Cloto-family projects.

## Release Channels & Support

Releases follow a three-tier model — **Stable** (production-certified, critical
fixes only), **Current** (newest release line, all fixes land here), and
**Experimental** (alpha/beta pre-releases, opt-in). When a new line is certified
Stable, the previous one keeps critical-fix support for 30 more days, then
reaches EOL.

**Known issues that change what you should run** — including the pre-v2.4.40
vector under-scan (bug-085) and the unauthenticated HTTP bind on the Stable line
(bug-198) — are listed in
[SUPPORT.md § Known issues](https://github.com/Cloto-dev/cpersona/blob/master/SUPPORT.md#known-issues).
Read it before pinning a version.

Full policy: [SUPPORT.md](https://github.com/Cloto-dev/cpersona/blob/master/SUPPORT.md) ·
specification: [Release lifecycle](https://cloto-dev.github.io/CPersona/RELEASE_LIFECYCLE_STANDARD/) ·
security reports: [SECURITY.md](https://github.com/Cloto-dev/cpersona/blob/master/SECURITY.md).

### Found a bug, or something the docs do not explain?

Open an issue — [bug report](https://github.com/Cloto-dev/cpersona/issues/new?template=bug_report.yml)
or [feature request](https://github.com/Cloto-dev/cpersona/issues/new?template=feature_request.yml).

Reports are welcome even when you are not certain it is a bug. If it turns out
to be a configuration problem, that is still useful signal — it means the
documentation was unclear, which is a defect of its own. Security
vulnerabilities are the one exception: please report those privately via
[SECURITY.md](https://github.com/Cloto-dev/cpersona/blob/master/SECURITY.md) rather than in a public issue.

## Learn More

- [Official documentation](https://cloto-dev.github.io/CPersona/) — canonical: getting started, behavior contracts, tools, architecture, operations, configuration, FAQ
- [Zenn Book (Japanese)](https://zenn.dev/cloto/books/claude-memory-mcp-server) — Full design walkthrough and setup guide
- [Replacing /compact with external memory (Japanese)](https://zenn.dev/cloto/articles/claude-code-compact-external-memory) — Measured token economics of the session-end → `/clear` → `recall` workflow
- [Memory System Design](https://github.com/Cloto-dev/ClotoCore/blob/master/docs/CPERSONA_MEMORY_DESIGN.md) — Technical specification
- [ClotoCore](https://github.com/Cloto-dev/ClotoCore) — The AI agent platform

## License

MIT — free to use from any MCP host without restriction.
