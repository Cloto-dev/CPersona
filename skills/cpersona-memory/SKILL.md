---
name: cpersona-memory
description: >-
  Give Claude persistent, searchable memory across sessions using the CPersona
  MCP server. Use this skill whenever the user wants Claude to remember things
  between conversations, asks to install or set up CPersona / a memory server,
  or when CPersona tools are available and the conversation contains decisions,
  rules, preferences, or a session boundary worth recording. Covers install,
  MCP-client configuration, the embedding server, and the day-to-day
  store / recall / archive workflow.
---

# CPersona — persistent memory for Claude

CPersona is an [MCP](https://modelcontextprotocol.io/) server that gives Claude
persistent memory across sessions. It stores memories in a single local SQLite
file and retrieves them with a 3-layer hybrid search (vector + FTS5 + keyword,
merged by Reciprocal Rank Fusion). It has **zero LLM dependency** — the server
never calls a model, so there is no API cost or hidden latency from memory
itself; the calling agent (you) does all summarization.

- **27 tools**, single SQLite file, MIT licensed.
- Works with Claude Desktop, Claude Code, and any MCP host.
- Repo: <https://github.com/Cloto-dev/cpersona>

This skill has two jobs: **(1) help the user install and configure CPersona**,
and **(2) use it correctly** once it is connected.

---

## When to use this skill

Activate this skill when any of the following is true:

- The user asks Claude to **remember** something across sessions, or complains
  that Claude forgets context between conversations.
- The user asks to **install / set up / configure** CPersona or "a memory
  server".
- CPersona MCP tools (`store`, `recall`, `archive_episode`, …) are connected
  **and** the current turn contains a decision, a standing rule/preference, a
  bug finding, or a session boundary (start/end).

If CPersona tools are **not** connected and the user wants memory, go to
**Setup**. If they are connected, go to **Usage**.

---

## Setup

CPersona is a Python MCP server. Installing it has two parts: the **memory
server** and an optional but strongly recommended **embedding server** (it
powers the vector-search layer; without it CPersona still runs on FTS5 +
keyword only).

**Prerequisites:** Python 3.10+ and Git.

### 1. Install CPersona

```bash
git clone https://github.com/Cloto-dev/cpersona.git
cd cpersona
python -m venv .venv
# macOS / Linux:
source .venv/bin/activate
# Windows:
# .venv\Scripts\activate
pip install .
```

### 2. Install the embedding server (recommended)

CPersona is embedding-server-agnostic — it talks to any HTTP endpoint that
implements `POST /embed` → `{ "embeddings": [[float,…],…], "dimensions": int }`.
The reference server is [CEmbedding](https://github.com/Cloto-dev/CEmbedding)
(MIT), which runs `jina-v5-nano` on-device (CPU) — the exact model CPersona was
tuned and benchmarked against:

```bash
git clone https://github.com/Cloto-dev/CEmbedding.git && cd CEmbedding
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install ".[onnx]"
python download_model.py --model jina-v5-nano
EMBEDDING_PROVIDER=onnx_jina_v5_nano python server.py   # serves http://127.0.0.1:8401/embed
```

> Without an embedding server, set `EMBEDDING_MODE=none`. Vector search (the
> strongest retrieval layer) is then disabled and recall falls back to FTS5 +
> keyword. CPersona v2.4.33+ will *tell* you when it is running degraded (see
> Troubleshooting) instead of silently serving reduced recall.

### 3. Register with the MCP client

Replace `/path/to/...` with the real paths from steps 1–2, and pick an absolute
`CPERSONA_DB_PATH` (e.g. `~/.claude/cpersona.db`).

**Claude Code:**

```bash
claude mcp add-json cpersona '{"type":"stdio","command":"/path/to/cpersona/.venv/bin/python","args":["/path/to/cpersona/server.py"],"env":{"CPERSONA_DB_PATH":"/absolute/path/cpersona.db","EMBEDDING_MODE":"http","EMBEDDING_HTTP_URL":"http://127.0.0.1:8401/embed"}}' -s user
```

**Claude Desktop** — add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "cpersona": {
      "command": "/path/to/cpersona/.venv/bin/python",
      "args": ["/path/to/cpersona/server.py"],
      "env": {
        "CPERSONA_DB_PATH": "/absolute/path/cpersona.db",
        "EMBEDDING_MODE": "http",
        "EMBEDDING_HTTP_URL": "http://127.0.0.1:8401/embed"
      }
    }
  }
}
```

> **Windows:** use `.venv/Scripts/python.exe` and a `C:/Users/you/...` DB path.
> **ClotoCore users:** don't clone — install CPersona from the in-app
> marketplace ([ClotoHub](https://hub.cloto.dev)), which distributes this repo.

After restarting the client, confirm the `cpersona` server is connected, then
ask Claude to `store` a fact and `recall` it.

---

## Usage

Once connected, follow these triggers **proactively** — do not wait to be asked.
Pick a stable `agent_id` for the user (e.g. `"claude-desktop"` or
`"claude-code"`) and reuse it on every call.

### Mandatory triggers

1. **Session start** → `recall(agent_id, query="<keywords from the user's
   opening topic, or ''>", limit=10)` before the first substantive action, so
   you start with relevant past context. Use `recall_with_context` instead when
   you already hold conversation history (it de-dupes and merges automatically).
   Use `deep=true` to search the full history without time decay.
   *Skip for trivial one-shot questions.*

2. **A decision / rule / preference / bug finding** → `store` it immediately.
   Fire on phrases like "let's go with X", "from now on always Y", "remember
   that …", "that's a bug". Protect must-not-lose rules with `lock_memory`.

3. **Updating an existing rule** → use `update_memory` (not delete + store). If
   it's locked: `unlock_memory` → `update_memory` → `lock_memory`.

4. **Session end** → `archive_episode(agent_id, history=<the real turns>,
   summary=…, keywords=…, resolved=true|false)`. Pre-compute `summary` and
   `keywords` yourself so the server stores synchronously (it never calls an LLM).
   Pass the **real** conversation history, not an empty array — it drives
   timestamps and the episode embedding. Set `resolved=true` for finished
   topics so they decay out of future recalls faster.

5. **Benchmarking / throwaway / "don't save this" sessions** →
   `pause_persistence(ttl_seconds=1800)` turns all writes into no-ops for a TTL
   window; `resume_persistence()` (or TTL expiry) restores. Read tools are
   unaffected.

### Recall quality knobs

- **`CPERSONA_RECALL_MODE`** — `rrf` (default, rank-only fusion, robust) /
  `rsf` (relative-score fusion; **recommended for Japanese / CJK or
  topic-drift-prone** corpora, where keyword score magnitude is the
  discriminating signal RRF flattens) / `cascade` (legacy sequential).
- **`set_recall_precision(agent_id, precision)`** — `strict` (fewer wrong hits,
  more misses) / `balanced` (default) / `lenient`. Read it back with
  `get_recall_precision`. The threshold curve is auto-calibrated; this is the
  one policy choice.
- **`calibrate_threshold(agent_id)`** — re-tune the vector threshold from the
  corpus (no labels needed) after the corpus changes a lot or recall feels off.

### Memory types

- **Declarative** — individual facts/decisions/rules via `store`.
- **Episodic** — conversation summaries via `archive_episode`.
- **Profile** — accumulated user/project attributes via `update_profile` /
  `get_profile`.

### Maintenance (low frequency)

- `check_health(agent_id, fix=true)` — 16-point integrity check + auto-repair.
- `deep_check(agent_id, fix=true)` — semantic quality pass.
- `export_memories` / `import_memories` — JSONL portability (idempotent import).
- `merge_memories` — atomically fold one agent's data into another, de-duped.

---

## Tool reference (27)

| Group | Tools |
|-------|-------|
| Core read/write | `store`, `recall`, `recall_with_context`, `list_memories`, `list_episodes` |
| Episodes / profile | `archive_episode`, `get_profile`, `update_profile` |
| Editing / protection | `update_memory`, `lock_memory`, `unlock_memory`, `delete_memory`, `delete_episode`, `delete_agent_data` |
| Recall tuning | `set_recall_precision`, `get_recall_precision`, `calibrate_threshold` |
| Persistence control | `pause_persistence`, `resume_persistence`, `persistence_status` |
| Portability | `export_memories`, `import_memories`, `merge_memories` |
| Channels / multi-user | `migrate_channel_axis` (and the `channel` / `source_id` args on `store` / `recall`) |
| Health | `check_health`, `deep_check`, `get_queue_status` |

Argument details live in each tool's MCP description.

---

## Troubleshooting

- **Recall results look thin / off-topic, or an `advisory` field appears on a
  `recall` response** — CPersona v2.4.33+ attaches
  `advisory = {degraded, severity, reason, evidence, runbook}` when it is
  running **degraded** (embeddings unavailable: `EMBEDDING_MODE=none`, or the
  HTTP endpoint is unreachable — process died, port changed, DB copied to a
  host without the embedding server, or a startup race). **Surface this to the
  user** instead of quietly serving keyword-only recall, and follow the
  `runbook` (usually: start/point the embedding server, then recall again). Opt
  out with `CPERSONA_DEGRADED_ADVISORY=false` for a deliberate keyword-only
  deployment.
- **Vector search disabled** — embedding server not reachable. Check it's
  running on the configured `EMBEDDING_HTTP_URL` and that `EMBEDDING_MODE=http`.
- **Nothing recalls after moving machines** — the DB moved but the embedding
  server didn't, or the embedding model/dimension changed. CPersona
  recalibrates on a dimension change; otherwise run `calibrate_threshold`.

---

## Key facts

- 27 tools · Schema v10 (auto-migrating) · ~3,500 LOC single-file Python · MIT.
- Zero LLM dependency at the storage layer → deterministic, no API cost.
- Single SQLite file → the user owns their memory; back it up by copying one file.
- Benchmarked on LMEB: `jina-v5-nano` (768d) scores NDCG@10 54.14, +47% over the
  MiniLM-384d baseline.
