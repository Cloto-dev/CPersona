<div align="center">

# cpersona

### MCP Memory Server

Give Claude persistent memory across sessions.
Single SQLite file. 21 tools. Zero LLM dependency.

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)]()
[![Tests](https://img.shields.io/badge/tests-117%20passing-brightgreen)]()

[Quick Start](#quick-start) · [Features](#features) · [Architecture](#architecture) · [All Tools](#all-tools) · [Zenn Book (JP)](https://zenn.dev/clotodev/books/claude-memory-mcp-server)

</div>

---

> **Standalone repository** — This is the standalone version for use with Claude Desktop, Claude Code, and any MCP client.
> If you are a [ClotoCore](https://github.com/Cloto-dev/ClotoCore) user, use the version in [cloto-mcp-servers](https://github.com/Cloto-dev/cloto-mcp-servers/tree/main/servers/cpersona) instead.

## The Problem

Claude forgets everything between sessions. Every conversation starts from zero — no context about your project, your preferences, or what you discussed yesterday.

cpersona fixes this. It's an [MCP](https://modelcontextprotocol.io/) server that stores memories in a local SQLite file and retrieves them through hybrid search. Claude remembers you.

## Quick Start

**Prerequisites:** Python 3.10+, Git

### 1. Install cpersona

```bash
git clone https://github.com/Cloto-dev/cpersona.git
cd cpersona
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
# source .venv/bin/activate

pip install .
```

### 2. Set up Embedding Server (Recommended)

cpersona's hybrid search works best with an embedding server for vector similarity. We recommend using [cloto-mcp-servers/embedding](https://github.com/Cloto-dev/cloto-mcp-servers/tree/main/servers/embedding) with the jina-v5-nano model (33M params, 768d, runs locally on CPU):

```bash
git clone https://github.com/Cloto-dev/cloto-mcp-servers.git
cd cloto-mcp-servers/servers
pip install ./embedding
```

> Without an embedding server, cpersona falls back to FTS5 + keyword search only. Vector search (the strongest retrieval layer) will be disabled.

### 3. Configure your MCP client

**Claude Desktop** — add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "embedding": {
      "command": "/path/to/.venv/bin/python",
      "args": ["/path/to/servers/embedding/server.py"],
      "env": {
        "EMBEDDING_PROVIDER": "onnx_jina_v5_nano",
        "EMBEDDING_HTTP_PORT": "8401"
      }
    },
    "cpersona": {
      "command": "/path/to/.venv/bin/python",
      "args": ["/path/to/cpersona/server.py"],
      "env": {
        "CPERSONA_DB_PATH": "/home/you/.claude/cpersona.db",
        "CPERSONA_EMBEDDING_MODE": "http",
        "CPERSONA_EMBEDDING_URL": "http://127.0.0.1:8401/embed"
      }
    }
  }
}
```

> **Windows:** use `.venv/Scripts/python.exe` and `C:/Users/you/.claude/cpersona.db`

**Claude Code:**

```bash
claude mcp add-json embedding '{"type":"stdio","command":"/path/to/.venv/bin/python","args":["/path/to/servers/embedding/server.py"],"env":{"EMBEDDING_PROVIDER":"onnx_jina_v5_nano","EMBEDDING_HTTP_PORT":"8401"}}' -s user

claude mcp add-json cpersona '{"type":"stdio","command":"/path/to/.venv/bin/python","args":["/path/to/cpersona/server.py"],"env":{"CPERSONA_DB_PATH":"/home/you/.claude/cpersona.db","CPERSONA_EMBEDDING_MODE":"http","CPERSONA_EMBEDDING_URL":"http://127.0.0.1:8401/embed"}}' -s user
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
- Health check — 16 automated detections with auto-repair (contamination, duplicates, FTS desync, invalid data, stale tasks, empty content, invalid sources)
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
                         │  │  task_queue   (async jobs)     │  │
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

Tested on LMEB (Long-term Memory Evaluation Benchmark, [results](https://github.com/Cloto-dev/cloto-mcp-servers/tree/main/lmeb_results)) — 22 evaluation tasks measuring memory retrieval quality:

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
| `get_profile` | Get current agent profile |
| `update_profile` | Save pre-computed agent profile |
| `archive_episode` | Archive conversation episode with summary and keywords |
| `list_memories` | List recent memories |
| `list_episodes` | List archived episodes |
| `delete_memory` | Delete a single memory (ownership enforced) |
| `delete_episode` | Delete a single episode (ownership enforced) |
| `delete_agent_data` | Delete all data for an agent |
| `calibrate_threshold` | Auto-calibrate vector search threshold via z-score |
| `export_memories` | Export to JSONL (memories, episodes, profiles) |
| `import_memories` | Import from JSONL (idempotent via msg_id dedup) |
| `merge_memories` | Merge one agent's data into another (atomic, with dedup) |
| `get_queue_status` | Background task queue status |
| `recall_with_context` | Recall with external conversation context (auto-dedup) |
| `update_memory` | Update memory content (rejects if locked) |
| `lock_memory` | Lock memory to prevent deletion/editing |
| `unlock_memory` | Unlock memory to allow deletion/editing |
| `check_health` | 16-point database health check with auto-repair |
| `deep_check` | Deep semantic data quality analysis with auto-repair |

## Configuration

All settings via environment variables with sensible defaults:

| Variable | Default | Description |
|----------|---------|-------------|
| `CPERSONA_DB_PATH` | `./cpersona.db` | SQLite database path |
| `CPERSONA_EMBEDDING_MODE` | `http` | Embedding mode (`http` or `disabled`) |
| `CPERSONA_EMBEDDING_URL` | `http://127.0.0.1:8401/embed` | Embedding server URL |
| `CPERSONA_VECTOR_SEARCH_MODE` | `remote` | Vector search mode |
| `CPERSONA_SEARCH_MODE` | `rrf` | Search strategy (`rrf` or `cascade`) |
| `CPERSONA_RRF_K` | `60` | RRF smoothing parameter |
| `CPERSONA_CONFIDENCE_ENABLED` | `false` | Include confidence metadata in results |
| `CPERSONA_AUTO_CALIBRATE` | `false` | Auto-calibrate on startup |
| `CPERSONA_TASK_QUEUE_ENABLED` | `false` | Enable background task queue |
| `CPERSONA_RECENT_RECALL_PENALTY` | `0.7` | Penalty for recently recalled memories |
| `CPERSONA_RECENT_RECALL_WINDOW_MIN` | `5` | Window (minutes) for recent recall penalty |

## Stats

- **~3,500 LOC** Python (single file, `server.py`)
- **117 tests** across 12 test modules
- **Schema v8** (auto-migrating)
- **MIT License**

## Works With

cpersona is an MCP server — it works with any MCP-compatible host:

- [Claude Desktop](https://claude.ai/download)
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
- [ClotoCore](https://github.com/Cloto-dev/ClotoCore) (AI agent platform, where cpersona originated)
- Any custom MCP client

## Part of ClotoCore

cpersona is the memory layer of [ClotoCore](https://github.com/Cloto-dev/ClotoCore), an open-source AI agent platform written in Rust. While cpersona is fully standalone (MIT license), it was designed to give AI agents persistent, searchable memory within the ClotoCore ecosystem.

## Learn More

- [Zenn Book (Japanese)](https://zenn.dev/clotodev/books/claude-memory-mcp-server) — Full design walkthrough and setup guide
- [Memory System Design](https://github.com/Cloto-dev/ClotoCore/blob/main/docs/CPERSONA_MEMORY_DESIGN.md) — Technical specification
- [ClotoCore](https://github.com/Cloto-dev/ClotoCore) — The AI agent platform

## License

MIT — free to use from any MCP host without restriction.
</div>
