<div align="center">

# cpersona

### MCP Memory Server

Give Claude persistent memory across sessions.
Single SQLite file. 27 tools. Zero LLM dependency.

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)]()
[![Tests](https://img.shields.io/badge/tests-275-brightgreen)]()

[Quick Start](#quick-start) · [Features](#features) · [Architecture](#architecture) · [All Tools](#all-tools) · [Zenn Book (JP)](https://zenn.dev/clotodev/books/claude-memory-mcp-server)

</div>

---

> **Standalone repository** — This is the standalone version for use with Claude Desktop, Claude Code, and any MCP client.
> If you are a [ClotoCore](https://github.com/Cloto-dev/ClotoCore) user, install CPersona from the in-app marketplace ([ClotoHub](https://hub.cloto.dev)) instead — it distributes this same repository.

> **Project status (July 2026)** — The 2.4 series is the **Stable** line (latest: v2.4.39, gated by three comprehensive audit rounds — see [Quality Assurance](#quality-assurance)). The 2.5 series is an internal stabilization line (**Experimental** pre-releases; the DB schema and MCP tool contract are preserved), and feature development resumes in 2.6. Tiers and support windows: [Release Channels & Support](#release-channels--support).

## The Problem

Claude forgets everything between sessions. Every conversation starts from zero — no context about your project, your preferences, or what you discussed yesterday.

cpersona fixes this. It's an [MCP](https://modelcontextprotocol.io/) server that stores memories in a local SQLite file and retrieves them through hybrid search. Claude remembers you.

## Quick Start

**Prerequisites:** Python 3.11+ (and [uv](https://docs.astral.sh/uv/) for the one-command path).

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

The reference server is [CEmbedding](https://github.com/Cloto-dev/CEmbedding) (MIT) — it runs jina-v5-nano on-device (CPU) and exposes exactly this endpoint:

```bash
git clone https://github.com/Cloto-dev/CEmbedding.git && cd CEmbedding
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install ".[onnx]"
python download_model.py --model jina-v5-nano
EMBEDDING_PROVIDER=onnx_jina_v5_nano python server.py   # serves http://127.0.0.1:8401/embed
```

cpersona was tuned and benchmarked against jina-v5-nano (33M params, 768d), so CEmbedding reproduces the numbers below. Any other server that satisfies the contract above works too.

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

Tested on LMEB (Long-term Memory Evaluation Benchmark) — 22 evaluation tasks measuring memory retrieval quality:

| Embedding Model | Params | Dimensions | Mean NDCG@10 |
|----------------|--------|------------|--------------|
| MiniLM-L6-v2 | 22M | 384 | 36.88 |
| e5-small | 33M | 384 | 46.36 |
| jina-v5-nano | 33M | 768 | **54.14** |

jina-v5-nano achieves +47% improvement over the MiniLM baseline.

## All Tools

| Tool | Description |
|------|-------------|
| `store` | Store a message in agent memory |
| `recall` | Recall relevant memories (vector + FTS5 + keyword, RRF merge) |
| `recall_with_context` | Recall with external conversation context (auto-dedup) |
| `get_profile` | Get current agent profile |
| `update_profile` | Save pre-computed agent profile |
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

- **~7,500 LOC** Python across focused modules
- **275 tests** across 24 test modules (including structural-enforcement gates)
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
  [`qa/issue-registry.json`](qa/issue-registry.json) with a machine-checkable
  code pattern; [`scripts/verify-issues.sh`](scripts/verify-issues.sh) verifies
  that every fix marker is still present (and every removed defect stays
  removed), so a regression or a silently-reverted fix fails loudly.
- **Structural CI gates** — invariants that a plain test can't express are
  enforced by AST- and behaviour-level gates in the pytest suite (run in CI on
  Python 3.11/3.13): every writer holds the shared write lock, agent-scoped SQL
  carries its isolation predicates, identity/dedup probes carry the
  project/channel axes, and `check_health` performs no embedding network I/O
  while holding the write lock.
- **Release lifecycle standard** — the release process itself is specified in
  [`docs/RELEASE_LIFECYCLE_STANDARD.md`](docs/RELEASE_LIFECYCLE_STANDARD.md)
  (v1.0), piloted in this repository as the reference implementation for
  Cloto-family projects.

## Release Channels & Support

Releases follow a three-tier model — **Stable** (production-certified,
critical fixes only), **Current** (newest release line, all fixes land here),
and **Experimental** (alpha/beta pre-releases, opt-in). When a new line is
certified Stable, the previous one keeps critical-fix support for 30 more
days, then reaches EOL. Current status: **2.4.x is the Stable line**
(latest v2.4.39); 2.5.x pre-releases are Experimental.

> **Known issue:** v2.4.39 and earlier under-scan vector recall on corpora
> beyond a few hundred rows (bug-085; v2.4.38–v2.4.39 are the most affected —
> the limit clamp closed the only workaround). Fixed in v2.4.40; upgrading is
> strongly recommended. See [SUPPORT.md § Known issues](SUPPORT.md#known-issues).

Full policy:
[SUPPORT.md](SUPPORT.md) · specification:
[RELEASE_LIFECYCLE_STANDARD.md](docs/RELEASE_LIFECYCLE_STANDARD.md) · security
reports: [SECURITY.md](SECURITY.md).

## Learn More

- [Zenn Book (Japanese)](https://zenn.dev/clotodev/books/claude-memory-mcp-server) — Full design walkthrough and setup guide
- [Memory System Design](https://github.com/Cloto-dev/ClotoCore/blob/main/docs/CPERSONA_MEMORY_DESIGN.md) — Technical specification
- [ClotoCore](https://github.com/Cloto-dev/ClotoCore) — The AI agent platform

## License

MIT — free to use from any MCP host without restriction.
