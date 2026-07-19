<div align="center">

# CPersona

### MCP Memory Server

Give Claude persistent memory across sessions.
Single SQLite file. 29 tools. Zero LLM dependency.

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](https://github.com/Cloto-dev/cpersona/blob/master/LICENSE)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)]()
[![Tests](https://img.shields.io/badge/tests-435-brightgreen)]()

[Quick Start](#quick-start) · [Features](#features) · [Architecture](#architecture) · [All Tools](#all-tools) · [Zenn Book (JP)](https://zenn.dev/cloto/books/claude-memory-mcp-server)

</div>

---

> **Standalone repository** — This is the standalone version for use with Claude Desktop, Claude Code, and any MCP client.
> If you are a [ClotoCore](https://github.com/Cloto-dev/ClotoCore) user, install CPersona from the in-app marketplace ([ClotoHub](https://hub.cloto.dev)) instead — it distributes this same repository.

> **Project status (July 2026)** — The 2.4 series is the **Stable** line (latest: v2.4.40, gated by three comprehensive audit rounds — see [Quality Assurance](#quality-assurance)). The 2.5 series is the **Current** line (latest: v2.5.1) — an internal stabilization line that has passed the full release gate and is where all fixes land, pending production-soak certification; the DB schema and MCP tool contract are preserved, and feature development resumes in 2.6. Tiers and support windows: [Release Channels & Support](#release-channels--support).

## The Problem

Claude forgets everything between sessions. Every conversation starts from zero — no context about your project, your preferences, or what you discussed yesterday.

cpersona fixes this. It's an [MCP](https://modelcontextprotocol.io/) server that stores memories in a local SQLite file and retrieves them through hybrid search. Claude remembers you.

## Quick Start

**Prerequisites:** Python 3.11+ (and [uv](https://docs.astral.sh/uv/) for the one-command path).

> **Claude Code? Let the agent do the setup.** This repo ships an [Agent Skill](https://github.com/Cloto-dev/cpersona/blob/master/skills/cpersona-memory/SKILL.md) that walks Claude through the whole installation — cpersona, the embedding server, MCP registration, and a store/recall smoke test — and, more importantly, teaches it *when* to store, recall, and archive memories afterwards:
>
> ```bash
> # Installed from PyPI? The skill ships inside the wheel — no clone needed:
> python -c "import cpersona,pathlib,shutil; s=pathlib.Path(cpersona.__file__).parent/'skills'/'cpersona-memory'; shutil.copytree(s, pathlib.Path.home()/'.claude/skills/cpersona-memory', dirs_exist_ok=True)"
>
> # Running via uvx (isolated environment), or not installed yet:
> git clone --depth 1 https://github.com/Cloto-dev/cpersona.git /tmp/cpersona
> mkdir -p ~/.claude/skills && cp -r /tmp/cpersona/skills/cpersona-memory ~/.claude/skills/
> ```
>
> Then just tell Claude Code: *"Set up CPersona — I want persistent memory."* The manual steps below are for Claude Desktop users and anyone who prefers to configure things by hand.

### 1. Install cpersona

```bash
uvx cpersona          # run directly, no install step
# or
pip install cpersona  # then the `cpersona` command is on your PATH
```

<details>
<summary>From source (for development)</summary>

```bash
git clone https://github.com/Cloto-dev/cpersona.git
cd cpersona
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install .
```
Run it with `python -m cpersona` (or `python server.py`).
</details>

### 2. Set up Embedding Server (Recommended)

cpersona's hybrid search works best with an embedding server for vector similarity. cpersona is embedding-server-agnostic: point `CPERSONA_EMBEDDING_URL` (see step 3) at any HTTP endpoint that implements the following minimal contract.

```
POST /embed
Request:  { "texts": ["string", ...] }        # non-empty array, max 100 per batch
Response: { "embeddings": [[float, ...], ...], "dimensions": <int> }
```

Contract requirements (2.5.0b1 clarifications):

- **Embeddings MUST be L2-normalized.** cpersona computes similarity as a raw
  dot product; a backend returning unnormalized vectors biases ranking by
  vector magnitude. Every supported backend (the client's `api` mode and all
  CEmbedding providers) already normalizes.
- **The contract is role-less** — queries and documents are embedded through
  the same call. Prompt-prefix models (e5-style, prompted bge) will
  underperform behind it; symmetric or retrieval-merged models (jina-v5-nano,
  bge-m3, MiniLM) are the intended fit.
- **Swapping models behind the same URL:** cpersona fingerprints the backend
  by embedding *dimension* only (the contract carries no model identity). A
  same-dimension model swap silently invalidates the stored corpus — after
  one, re-embed (`check_health(fix=true)` repairs NULLed rows) and
  `calibrate_threshold`.

The reference server is [CEmbedding](https://github.com/Cloto-dev/CEmbedding) (MIT) — it runs jina-v5-nano on-device (CPU) and exposes exactly this endpoint:

```bash
git clone https://github.com/Cloto-dev/CEmbedding.git && cd CEmbedding
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install ".[onnx]"
python download_model.py --model jina-v5-nano
EMBEDDING_PROVIDER=onnx_jina_v5_nano python server.py   # serves http://127.0.0.1:8401/embed
```

cpersona ships with defaults tuned against jina-v5-nano (768d). Any other server that satisfies the contract above works too — see [Benchmarks](#benchmarks) for models with published measurements.

> Without an embedding server, cpersona falls back to FTS5 + keyword search only. Vector search (the strongest retrieval layer) will be disabled.

### 3. Configure your MCP client

**Claude Desktop** — add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "cpersona": {
      "command": "uvx",
      "args": ["cpersona"],
      "env": {
        "CPERSONA_DB_PATH": "/home/you/.claude/cpersona.db",
        "EMBEDDING_MODE": "http",
        "EMBEDDING_HTTP_URL": "http://127.0.0.1:8401/embed"
      }
    }
  }
}
```

The embedding server from step 2 is a plain HTTP process, not an MCP server —
run it however you run background services (a terminal, launchd/systemd, etc.);
cpersona only needs its URL.

> **Windows:** use `C:/Users/you/.claude/cpersona.db` for the DB path.
> **No embedding server yet?** Drop the two `EMBEDDING_*` lines (or set `EMBEDDING_MODE=none`) — cpersona runs on FTS5 + keyword and tells you when it's degraded.

**Claude Code:**

```bash
claude mcp add-json cpersona '{"type":"stdio","command":"uvx","args":["cpersona"],"env":{"CPERSONA_DB_PATH":"/home/you/.claude/cpersona.db","EMBEDDING_MODE":"http","EMBEDDING_HTTP_URL":"http://127.0.0.1:8401/embed"}}' -s user
```

That's it. Claude now has persistent memory. Ask it to `store` something and `recall` it in a later session.

## Features

**Hybrid Search** — Three independent retrieval strategies run in parallel and merge results via Reciprocal Rank Fusion (RRF):

| Layer | Method | Strength |
|-------|--------|----------|
| Vector | Cosine similarity (jina-v5-nano, 768d) | Semantic meaning |
| FTS5 | SQLite full-text search with trigram tokenizer | Exact terms, names, IDs |
| Keyword | Fallback pattern matching | Edge cases, partial matches |

**Memory Types:**

- **Declarative memory** — Individual facts, decisions, instructions stored via `store`
- **Episodic memory** — Conversation summaries archived via `archive_episode`
- **Profile memory** — Accumulated user/project attributes via `update_profile`

**Confidence Scoring** — Each recalled memory gets a confidence score combining:

- Cosine similarity (semantic relevance)
- Dynamic time decay (adapts to corpus time range — a 1-year-old corpus and a 1-day-old corpus use different decay curves)
- Recall boost (frequently useful memories surface more easily, with natural fade-out)
- Completion factor (resolved topics decay faster)

**Zero LLM Dependency** — cpersona is a pure data server. It never calls an LLM internally. All summarization and extraction is performed by the calling agent. This means zero API costs from cpersona itself, deterministic behavior, and no hidden latency.

**Additional capabilities:**

- Agent namespace isolation — multiple agents share one DB without interference
- Background task queue — DB-persisted, crash-recoverable async processing
- JSONL export/import — full memory portability between environments
- Agent-to-agent memory merge — atomic copy/move with deduplication
- Auto-calibration — statistical threshold tuning via null distribution z-score (no labels needed)
- Health check — a 20-check registry with severity-tagged issues (`critical`/`warn`/`info`) and auto-repair (contamination, duplicates, FTS integrity, embedding dimension drift, schema objects, isolation-axis hygiene, stale tasks, invalid data), plus a `python -m cpersona.checkup` CLI for CI gating
- Deep check — semantic data quality analysis (anonymous source recovery, short content, stale profiles, orphaned episodes)
- Memory protection — lock/unlock to prevent accidental deletion or editing
- Recent recall penalty — suppresses echo chamber effect for frequently recalled memories
- stdio + Streamable HTTP transport
- Single-file SQLite — no external database required

## Architecture

```
                         ┌─────────────────────────────────────┐
                         │            MCP Host                 │
                         │   (Claude Desktop / Claude Code)    │
                         └──────────────┬──────────────────────┘
                                        │ MCP (JSON-RPC)
                         ┌──────────────▼──────────────────────┐
                         │           cpersona                  │
                         │         (server.py)                 │
                         │                                     │
                         │  ┌─────────┐  ┌─────────┐          │
                         │  │  store   │  │ recall  │  ...     │
                         │  └────┬────┘  └────┬────┘          │
                         │       │             │               │
                         │  ┌────▼─────────────▼────────────┐  │
                         │  │         SQLite DB              │  │
                         │  │                                │  │
                         │  │  memories    (content + embed) │  │
                         │  │  episodes    (summaries)       │  │
                         │  │  profiles    (attributes)      │  │
                         │  │  memories_fts (FTS5 index)     │  │
                         │  │  episodes_fts (FTS5 index)     │  │
                         │  │  pending_memory_tasks (queue)  │  │
                         │  └────────────────────────────────┘  │
                         │                                      │
                         └──────────────┬───────────────────────┘
                                        │ HTTP
                         ┌──────────────▼──────────────────────┐
                         │       Embedding Server              │
                         │  (jina-v5-nano ONNX, 768d)          │
                         └─────────────────────────────────────┘
```

**Recall flow (RRF mode):**

```
Query → ┌── Vector search (cosine similarity)  ──┐
        ├── FTS5 search (episodes + memories)    ──┼── RRF merge → Confidence scoring → Top-K
        └── Keyword fallback                     ──┘
```

## Benchmarks

Measured on LMEB (Long-horizon Memory Embedding Benchmark, arXiv:2603.12572) — 22 datasets subsuming LoCoMo and LongMemEval, measured here as 22 retrieval tasks. The metric is Mean NDCG@10 across all 22 tasks.

Two tracks isolate the pipeline's contribution:

- **Track A** — the raw embedding model alone (baseline retrieval).
- **Track B** — the same embeddings routed through cpersona's real `store`/`recall` code paths: SQLite + FTS5 + RRF fusion + per-agent auto-calibration (cpersona v2.4.40, full-ranking regime).

| Embedding Model | Params | Dim | Track A (raw) | Track B (cpersona) | Δ |
|---|---|---|---|---|---|
| all-MiniLM-L6-v2 | 22M | 384 | 43.67 | **50.10** | +6.43 |
| bge-m3 | 568M | 1024 | 56.83 | **57.66** | +0.83 |

cpersona's hybrid pipeline outranks the raw embedding on both models (Track B > Track A) — the fusion layers add signal rather than merely persisting vectors. The weaker the embedding, the larger the pipeline's contribution: the FTS5/keyword layers rescue queries the vector search alone misses. Methodology, the measurement harness, and the reproduction regime live in [`benchmarks/`](https://github.com/Cloto-dev/cpersona/blob/master/benchmarks/README.md).

## All Tools

| Tool | Description |
|------|-------------|
| `store` | Store a message in agent memory |
| `recall` | Recall relevant memories (vector + FTS5 + keyword, RRF merge) |
| `recall_with_context` | Recall with external conversation context (auto-dedup) |
| `get_contents` | Expand recall preview refs (`mem:<id>` / `ep:<id>`) to full content |
| `get_profile` | Get current agent profile |
| `update_profile` | Save pre-computed agent profile |
| `get_operating_context` | Read the operator-owned operating context served to every client (read-only; edited on the filesystem) |
| `archive_episode` | Archive conversation episode with summary and keywords |
| `list_memories` | List recent memories |
| `list_episodes` | List archived episodes |
| `update_memory` | Update memory content (rejects if locked) |
| `lock_memory` | Lock memory to prevent deletion/editing |
| `unlock_memory` | Unlock memory to allow deletion/editing |
| `delete_memory` | Delete a single memory (ownership enforced) |
| `delete_episode` | Delete a single episode (ownership enforced) |
| `delete_agent_data` | Delete all data for an agent |
| `calibrate_threshold` | Auto-calibrate vector search threshold via z-score |
| `set_recall_precision` | Set an agent's recall precision (knob 3) and recalibrate its gate |
| `get_recall_precision` | Read an agent's effective recall precision (knob 3) |
| `pause_persistence` | Turn writes into no-ops for an opt-in TTL window |
| `resume_persistence` | Re-enable persistence immediately |
| `persistence_status` | Report whether persistence is paused and the TTL remaining |
| `migrate_channel_axis` | Re-channel bridge-type memories to their concrete channel |
| `export_memories` | Export to JSONL (memories, episodes, profiles) |
| `import_memories` | Import from JSONL (idempotent via msg_id dedup) |
| `merge_memories` | Merge one agent's data into another (atomic, with dedup) |
| `get_queue_status` | Background task queue status |
| `check_health` | Registry-driven health check (severity-tagged issues) with auto-repair |
| `deep_check` | Deep semantic data quality analysis with auto-repair |

## Configuration

All settings via environment variables with sensible defaults:

| Variable | Default | Description |
|----------|---------|-------------|
| `CPERSONA_DB_PATH` | `./cpersona.db` | SQLite database path |
| `CPERSONA_EMBEDDING_MODE` | `none` | Embedding mode (`http` or `none`) |
| `CPERSONA_EMBEDDING_URL` | *(unset)* | Embedding server URL, e.g. `http://127.0.0.1:8401/embed` |
| `CPERSONA_VECTOR_SEARCH_MODE` | `local` | Vector search execution (`local` in-process cosine, or `remote` offload) |
| `CPERSONA_RECALL_MODE` | `rrf` | Recall fusion strategy (`rrf`, `rsf`, or `cascade`) |
| `CPERSONA_RECALL_PREVIEW_CHARS` | `500` | Preview tier: max content chars returned by the recall tools (`0` disables; `full_content=true` / `get_contents` fetch full text) |
| `CPERSONA_RRF_K` | `60` | RRF smoothing parameter |
| `CPERSONA_CONFIDENCE_ENABLED` | `false` | Include confidence metadata in results |
| `CPERSONA_AUTO_CALIBRATE` | `false` | Auto-calibrate on startup |
| `CPERSONA_TASK_QUEUE_ENABLED` | `true` | Background task queue (DB-persisted, crash-recoverable) |
| `CPERSONA_RECENT_RECALL_PENALTY` | `0.7` | Penalty for recently recalled memories |
| `CPERSONA_RECENT_RECALL_WINDOW_MIN` | `5` | Window (minutes) for recent recall penalty |

The generic aliases `EMBEDDING_MODE` / `EMBEDDING_HTTP_URL` / `EMBEDDING_MODEL`
are also accepted (the `CPERSONA_`-prefixed form wins when both are set) — the
marketplace catalog and the Quick Start use the generic names.

### Recall fusion mode (`CPERSONA_RECALL_MODE`)

- **`rrf`** (default) — Reciprocal Rank Fusion: merges the vector + FTS channels by
  rank only. Robust and scale-free, but discards score magnitude.
- **`rsf`** — Relative Score Fusion: per-query min-max-normalizes each channel's raw
  score (cosine for vector, bm25 for keyword) and sums them, so the keyword channel's
  bm25 magnitude survives the merge. **Recommended for topic-drift-prone or space-less
  language (e.g. Japanese) contexts**, where that magnitude is the discriminating
  signal `rrf` flattens away (≈ Weaviate's `relativeScoreFusion`; see the ClotoCore
  `RECALL_CONTAMINATION_AB_2026-06-14` report §10–12). *Caveat:* min-max normalization
  can over-cut small, closely-scored result sets when `autocut` is enabled — `rrf`
  remains the default until that interaction is hardened.
- **`cascade`** — Sequential channel fill (legacy).

## Stats

- **~9,000 LOC** Python across focused modules, plus a 3,300-line vendored MCP
  common snapshot
- **435 tests** across 35 test modules (~10,000 LOC — more test code than
  server code), including structural-enforcement gates
- **Schema v13** (auto-migrating)
- **MIT License**

## Works With

cpersona is an MCP server — it works with any MCP-compatible host:

- [Claude Desktop](https://claude.ai/download)
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
- [ClotoCore](https://github.com/Cloto-dev/ClotoCore) (AI agent platform, where cpersona originated)
- Any custom MCP client

## Part of ClotoCore

cpersona is the memory layer of [ClotoCore](https://github.com/Cloto-dev/ClotoCore), an open-source AI agent platform written in Rust. While cpersona is fully standalone (MIT license), it was designed to give AI agents persistent, searchable memory within the ClotoCore ecosystem.

## Quality Assurance

Every release is gated by a machine-verifiable quality process:

- **Audit-gated releases** — before a release is cut, the codebase goes through
  comprehensive multi-agent audit rounds (independent finders per dimension,
  each finding adversarially verified from multiple lenses). v2.4.39 shipped
  after three such rounds — 43 fixes, every one re-verified against the tree
  it landed on.
- **Issue registry** — every audited defect lives in
  [`qa/issue-registry.json`](https://github.com/Cloto-dev/cpersona/blob/master/qa/issue-registry.json) with a machine-checkable
  code pattern; [`scripts/verify-issues.sh`](https://github.com/Cloto-dev/cpersona/blob/master/scripts/verify-issues.sh) verifies
  that every fix marker is still present (and every removed defect stays
  removed), so a regression or a silently-reverted fix fails loudly.
- **Structural CI gates** — invariants that a plain test can't express are
  enforced by AST- and behaviour-level gates in the pytest suite (run in CI on
  Python 3.11/3.13): every writer holds the shared write lock, agent-scoped SQL
  carries its isolation predicates, identity/dedup probes carry the
  project/channel axes, and `check_health` performs no embedding network I/O
  while holding the write lock.
- **Release lifecycle standard** — the release process itself is specified in
  [`docs/RELEASE_LIFECYCLE_STANDARD.md`](https://github.com/Cloto-dev/cpersona/blob/master/docs/RELEASE_LIFECYCLE_STANDARD.md)
  (v1.0), piloted in this repository as the reference implementation for
  Cloto-family projects.

## Release Channels & Support

Releases follow a three-tier model — **Stable** (production-certified,
critical fixes only), **Current** (newest release line, all fixes land here),
and **Experimental** (alpha/beta pre-releases, opt-in). When a new line is
certified Stable, the previous one keeps critical-fix support for 30 more
days, then reaches EOL. Current status: **2.4.x is the Stable line**
(latest v2.4.40) and **2.5.x is the Current line** (latest v2.5.1), where all
fixes land while it awaits production-soak certification.

> **Known issue:** v2.4.39 and earlier under-scan vector recall on corpora
> beyond a few hundred rows (bug-085; v2.4.38–v2.4.39 are the most affected —
> the limit clamp closed the only workaround). Fixed in v2.4.40; upgrading is
> strongly recommended. See [SUPPORT.md § Known issues](https://github.com/Cloto-dev/cpersona/blob/master/SUPPORT.md#known-issues).

Full policy:
[SUPPORT.md](https://github.com/Cloto-dev/cpersona/blob/master/SUPPORT.md) · specification:
[RELEASE_LIFECYCLE_STANDARD.md](https://github.com/Cloto-dev/cpersona/blob/master/docs/RELEASE_LIFECYCLE_STANDARD.md) · security
reports: [SECURITY.md](https://github.com/Cloto-dev/cpersona/blob/master/SECURITY.md).

### Found a bug, or something the docs do not explain?

Open an issue — [bug report](https://github.com/Cloto-dev/cpersona/issues/new?template=bug_report.yml)
or [feature request](https://github.com/Cloto-dev/cpersona/issues/new?template=feature_request.yml).

Reports are welcome even when you are not certain it is a bug. If it turns out
to be a configuration problem, that is still useful signal — it means the
documentation was unclear, which is a defect of its own. Security
vulnerabilities are the one exception: please report those privately via
[SECURITY.md](https://github.com/Cloto-dev/cpersona/blob/master/SECURITY.md) rather than in a public issue.

## Learn More

- [Zenn Book (Japanese)](https://zenn.dev/cloto/books/claude-memory-mcp-server) — Full design walkthrough and setup guide
- [Replacing /compact with external memory (Japanese)](https://zenn.dev/cloto/articles/claude-code-compact-external-memory) — Measured token economics of the session-end → `/clear` → `recall` workflow
- [Memory System Design](https://github.com/Cloto-dev/ClotoCore/blob/main/docs/CPERSONA_MEMORY_DESIGN.md) — Technical specification
- [ClotoCore](https://github.com/Cloto-dev/ClotoCore) — The AI agent platform

## License

MIT — free to use from any MCP host without restriction.
