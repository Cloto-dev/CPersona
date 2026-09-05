# Getting Started

> **Applies to: CPersona 2.5.x.** This page is the canonical installation and
> setup reference. The README keeps a condensed version of the same steps
> because it is also the PyPI project page; when the two disagree, this page
> wins.

CPersona is an [MCP](https://modelcontextprotocol.io/) server. You install it,
point an MCP client at it, and the client's agent gains `store` / `recall`
tools that survive across sessions. Nothing else in your stack changes.

## Prerequisites

- **Python 3.11+**
- **[uv](https://docs.astral.sh/uv/)** for the one-command path (optional —
  `pip` works too)
- An MCP client: Claude Desktop, Claude Code, Codex CLI, Cursor, VS Code, or
  any other MCP host — step 3 has the entry for each

## Let the agent do it (Claude Code)

The repository — and the published wheel — ship an
[Agent Skill](https://github.com/Cloto-dev/cpersona/tree/master/skills/cpersona-memory)
that walks Claude Code through the whole installation and, more importantly,
teaches it *when* to store, recall, and archive afterwards. Installing the
skill is the shortest path:

```bash
# Installed from PyPI? The skill ships inside the wheel — no clone needed:
python -c "import cpersona,pathlib,shutil; s=pathlib.Path(cpersona.__file__).parent/'skills'/'cpersona-memory'; shutil.copytree(s, pathlib.Path.home()/'.claude/skills/cpersona-memory', dirs_exist_ok=True)"

# Running via uvx (isolated environment), or not installed yet:
git clone --depth 1 https://github.com/Cloto-dev/cpersona.git /tmp/cpersona
mkdir -p ~/.claude/skills && cp -r /tmp/cpersona/skills/cpersona-memory ~/.claude/skills/
```

Then tell Claude Code: *"Set up CPersona — I want persistent memory."* The
manual steps below are for every other client, and for anyone who prefers to
configure things by hand — step 5 is the part the skill would otherwise do,
the one that makes the triggers fire without being asked.

## 1. Install CPersona

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

<details>
<summary>In a container</summary>

```bash
git clone https://github.com/Cloto-dev/cpersona.git
cd cpersona
docker build -t cpersona .

docker volume create cpersona-data
docker run -d --name cpersona -p 8402:8402 \
  -e CPERSONA_AUTH_TOKEN="$(openssl rand -hex 32)" \
  -v cpersona-data:/data cpersona
```

No image is published, so the build step is yours; the repository is the
distribution. Three things about this image are worth knowing before you run it:

- **It serves the Streamable HTTP transport**, on 8402. To run the stdio
  transport instead — the shape an MCP client spawns as a subprocess — pass
  `-i` and name it: `docker run -i --rm -e CPERSONA_TRANSPORT=stdio -v
  cpersona-data:/data cpersona`.
- **It will not start without `CPERSONA_AUTH_TOKEN`**, and that is not this
  image being strict. A published container port forwards to whatever the
  process bound, so binding inside the container is not evidence that only the
  container can reach it.
- **Your memory lives on the volume, not in the container.** `/data` is where
  the database goes; a container without a volume mounted there is a memory
  server that forgets when it is replaced. A named volume (above) works as
  shown. A bind-mounted host directory does not inherit ownership, so it has to
  be writable by uid `10001` — or pass `--user "$(id -u)"`.

Recall is keyword/FTS-only until you give it an embedding backend — see the next
section, and pass `-e CPERSONA_EMBEDDING_MODE=http -e
CPERSONA_EMBEDDING_URL=http://<host>:8401/embed` once you have one.
</details>

<details>
<summary>In containers, with an embedding server</summary>

`compose.yaml` in this repository runs CPersona and CEmbedding together, wired
to each other, so vector search works without a second setup:

```bash
export CPERSONA_AUTH_TOKEN=$(openssl rand -hex 32)
docker compose run --rm embedding cembedding-download-model --model jina-v5-nano
docker compose up -d
```

The middle line is not bookkeeping. Left alone, the embedding server downloads
its weights on the first request — around 800 MB — with the port already
accepting connections it cannot answer yet. Fetching them once into the volume
turns that into a step you watch instead of a timeout you diagnose.

Nothing publishes the embedding port: CPersona reaches it over the network
compose creates, and that is its whole audience. Both images are built locally
and the embedding server is pinned to a revision, so a later build gives you the
same pair.

Your memories are on the `cpersona-data` volume and the model on
`embedding-model`; `docker compose down` leaves both, `down -v` deletes them.
</details>

At startup the server checks pypi.org for a newer release and tells the calling
agent through `recall` (and `check_health`); `check_update` answers on demand.
Set `CPERSONA_UPDATE_CHECK=false` to turn that off. Updating is never
automatic — it needs an explicit `check_update(apply=true)` and a restart.

## 2. Set up an embedding server (recommended)

CPersona strongly recommends connecting to a compatible embedding server.
Running CPersona without an embedding backend is supported as a fallback
configuration, but is not recommended for normal operation.

Vector search is the strongest of the three retrieval layers, and it is the
only one that needs an external process. Without it CPersona still runs — on
FTS5 + keyword search — and
[says so on every recall](operations.md#detecting-a-dead-embedding-server).

CEmbedding is the reference and recommended embedding backend. Any other
embedding server that satisfies the contract below is equally supported and
equally recommended — the choice of backend is yours.

### The contract

CPersona is embedding-server-agnostic. Point `CPERSONA_EMBEDDING_URL` at any
HTTP endpoint that implements this:

```
POST /embed
Request:  { "texts": ["string", ...] }        # non-empty array
Response: { "embeddings": [[float, ...], ...], "dimensions": <int> }
```

CPersona reads **`embeddings`** and nothing else — `dimensions` is part of the
reference server's response and is ignored by the client, so a backend that
omits it still works. CPersona sends at most **32 texts per request**; the
reference server accepts up to 100, so batch limits in that range are not a
constraint you need to plan around.

Three requirements are easy to miss and each one degrades ranking silently:

- **Embeddings MUST be L2-normalized.** CPersona computes similarity as a raw
  dot product, so a backend returning unnormalized vectors biases ranking by
  vector magnitude. Every supported backend (the client's `api` mode and all
  CEmbedding providers) already normalizes.
- **The contract is role-less.** Queries and documents are embedded through
  the same call, with no instruction prefix. Prompt-prefix models (e5-style,
  prompted bge) underperform behind it; symmetric or retrieval-merged models
  (jina-v5-nano, bge-m3, MiniLM) are the intended fit.
- **Swapping models behind one URL invalidates the corpus.** CPersona
  fingerprints the backend by embedding *dimension* only — the contract
  carries no model identity — so a same-dimension swap is undetectable. It is
  also the case the repair tools cannot reach: `check_health(fix=true)`
  re-embeds rows whose blob is NULL, and the dimension check only NULLs blobs
  of the wrong *length*, so after a same-dimension swap every blob is the
  expected size, nothing is NULLed, and nothing is re-embedded. No tool
  force-re-embeds a row that already has a blob. The recovery is to rebuild
  the corpus — `delete_agent_data` then re-`store`, as in the
  [rebuild pattern](operations.md#corpus-indexing-and-sync-patterns) — and
  then run `calibrate_threshold`.

### The reference server

[CEmbedding](https://github.com/Cloto-dev/CEmbedding) (MIT) runs jina-v5-nano
on-device (CPU) and exposes exactly this endpoint:

```bash
# Download the model into ./data/models
uvx --from "cembedding[onnx]" cembedding-download-model --model jina-v5-nano

# Run the server (it reads ./data/models from the current directory)
EMBEDDING_PROVIDER=onnx_jina_v5_nano uvx --from "cembedding[onnx]" cembedding
```

Or put it on your PATH with `pip install "cembedding[onnx]"` and run
`cembedding-download-model --model jina-v5-nano`, then `cembedding`. From a
source checkout the same two steps are `python -m cembedding.download_model
--model jina-v5-nano` and `python -m cembedding`.

Either way you should see
`HTTP embedding endpoint started on http://127.0.0.1:8401/embed`. Verify it
before wiring CPersona to it:

```bash
curl -s http://127.0.0.1:8401/embed \
  -H 'content-type: application/json' \
  -d '{"texts":["hello world"]}' | head -c 200
```

CPersona's defaults are tuned against jina-v5-nano (768 dimensions). Any other
server satisfying the contract works; models with published measurements are
listed in [`benchmarks/`](https://github.com/Cloto-dev/cpersona/blob/master/benchmarks/README.md).

CPersona only needs its URL — but **how you supervise the reference server
matters**, and the obvious way does not work.

It is an MCP server, not a plain HTTP process. On its default transport it
runs an MCP session on stdio in the foreground and serves the REST `/embed`
endpoint from a background task, so **its lifetime is bound to stdin**: at
EOF the session ends and the `finally` clause cancels the HTTP task. Started
the way a service manager starts things — with stdin on `/dev/null` — it
binds the port, logs `HTTP embedding endpoint started`, and exits **in the
same second with status 0**. The supervisor sees a clean exit, and CPersona
is left pointed at a URL nothing answers.

Give it a stdin that stays open. Under a service manager that means running
it through something that holds the pipe, e.g.
`ExecStart=/bin/sh -c 'sleep infinity | cembedding'`; in a terminal the
terminal already does it.

`EMBEDDING_TRANSPORT=streamable-http` is a supervisable alternative in the
sense that it does not read stdin, but it serves the MCP endpoint **instead
of** REST `/embed` — so it is not an option for CPersona's `http` mode, which
posts to `/embed`.

## 3. Register CPersona with your MCP client

Every client launches the same process — command `uvx`, argument `cpersona`,
and the environment shown below — and differs only in which file it reads and
what it calls the keys. Pick an absolute `CPERSONA_DB_PATH` first; the examples
use `/home/you/.claude/cpersona.db` because that directory already exists for
Claude users, but any absolute path works.

| Client | Where the entry goes | Shape | Checked here |
| --- | --- | --- | --- |
| Claude Code | `claude mcp add-json … -s user` (writes the user config) | JSON, `type: stdio` | yes |
| Claude Desktop | `claude_desktop_config.json` | JSON, `mcpServers` | yes |
| Codex CLI | `codex mcp add … -- uvx cpersona` (writes `~/.codex/config.toml`) | TOML, `[mcp_servers.cpersona]` | yes (codex-cli 0.147.0) |
| Cursor | `~/.cursor/mcp.json` (global) or `.cursor/mcp.json` (project) | JSON, `mcpServers` | vendor docs |
| VS Code (Copilot) | `.vscode/mcp.json` (workspace) or the user `mcp.json` | JSON, `servers` | vendor docs |
| Any other MCP host | its stdio server configuration | the same command / args / env triple | vendor docs |

"Checked here" means the entry was written by that client's own tooling and
read back on a maintainer machine while this page was written. "Vendor docs"
means the shape is transcribed from the client's documentation and has not been
executed here — if it disagrees with what your client accepts, the client is
right, and a report is welcome.

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

**Claude Code** — one command:

```bash
claude mcp add-json cpersona '{"type":"stdio","command":"uvx","args":["cpersona"],"env":{"CPERSONA_DB_PATH":"/home/you/.claude/cpersona.db","EMBEDDING_MODE":"http","EMBEDDING_HTTP_URL":"http://127.0.0.1:8401/embed"}}' -s user
```

**Codex CLI** — one command; it writes the TOML shown after it into
`~/.codex/config.toml`:

```bash
codex mcp add cpersona --env CPERSONA_DB_PATH=/home/you/.claude/cpersona.db --env EMBEDDING_MODE=http --env EMBEDDING_HTTP_URL=http://127.0.0.1:8401/embed -- uvx cpersona
```

```toml
[mcp_servers.cpersona]
command = "uvx"
args = ["cpersona"]

[mcp_servers.cpersona.env]
CPERSONA_DB_PATH = "/home/you/.claude/cpersona.db"
EMBEDDING_HTTP_URL = "http://127.0.0.1:8401/embed"
EMBEDDING_MODE = "http"
```

Codex can also deny-list tools per server (`disabled_tools = ["delete_memory", …]`
under the same table), which is a client-side way to hand an agent read-mostly
access. The server-side equivalent, enforced for every client at once, is the
[per-client capability layer](ACL_DESIGN.md).

**Cursor** — `~/.cursor/mcp.json`, or `.cursor/mcp.json` inside a project. Same
shape as Claude Desktop:

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

**VS Code (Copilot)** — `.vscode/mcp.json` in the workspace, or the user-level
`mcp.json` (*MCP: Open User Configuration*). The top-level key is `servers`,
not `mcpServers`:

```json
{
  "servers": {
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

Notes that save a support round-trip:

- **Set `CPERSONA_DB_PATH` to an absolute path.** Its default,
  `data/cpersona.db`, is relative to the *client's* working directory — which
  means a client launched from somewhere else opens a different, empty
  database. On Windows, write it as `C:/Users/you/.claude/cpersona.db`.
- **No embedding server yet?** Drop the two `EMBEDDING_*` lines (or set
  `EMBEDDING_MODE=none`). CPersona runs on FTS5 + keyword and reports that it
  is degraded.
- `EMBEDDING_MODE` / `EMBEDDING_HTTP_URL` are the generic aliases of
  `CPERSONA_EMBEDDING_MODE` / `CPERSONA_EMBEDDING_URL`; the prefixed form wins
  when both are set. The [configuration reference](configuration.md) covers
  the settings you are likely to reach for; it is not exhaustive — a handful
  of variables (`CPERSONA_STORE_BLOB`, `CPERSONA_FTS_ENABLED`,
  `CPERSONA_EMBEDDING_API_KEY`, the `CPERSONA_CALIBRATE_*` pair and a few
  others) are read by the server without appearing there. `cpersona/config.py`
  is the complete list.

## 4. Verify it works

Ask the agent to store something, then recall it — ideally in a *new* session,
since surviving the session boundary is the whole point:

> "Store this: the deploy runbook lives in ops/deploy.md."
>
> …then, in a fresh session: "What did I tell you about the deploy runbook?"

Two checks worth running once the corpus is real:

- `check_health` — the registry-driven health check. `status` is the verdict;
  issues are severity-tagged (`critical` / `warn` / `info`), and
  `check_health(fix=true)` repairs the mechanical ones.
- Watch recall responses for an `advisory` field. It reports that vector
  search is not contributing, and its severity distinguishes the two reasons:
  a `hint` means embeddings are simply unconfigured (`mode=none`), while a
  fault means a configured endpoint stopped answering — see
  [detecting a dead embedding server](operations.md#detecting-a-dead-embedding-server).

## 5. Make the memory triggers fire in every session

Registration gives the agent the tools; it does not make the agent *use* them
unprompted. Recall at session start, store on a decision, archive at session
end — those have to live in something the client loads on **every** session,
not in a skill that activates only when the conversation happens to match.
Every client has such a file:

| Client | Always-loaded file (user-level default) | Project-level alternative |
| --- | --- | --- |
| Claude Code / Claude Desktop | `~/.claude/CLAUDE.md` | `./CLAUDE.md` |
| Codex CLI | `~/.codex/AGENTS.md` | `./AGENTS.md` at the repository root |
| Cursor | User Rules (*Customize → Rules* — a setting, not a file) | `.cursor/rules/cpersona.mdc` with `alwaysApply: true`, or `./AGENTS.md` |
| VS Code (Copilot) | see the client's custom-instructions documentation | `.github/copilot-instructions.md`, or `./AGENTS.md` with `chat.useAgentsMdFile` enabled |

Paste the block below into that file, replacing `<AGENT_ID>` with one stable
identifier the agent will use on every call (`"claude-code"`, `"codex"`, …).
Keep the markers: they are how a later version of the block finds and replaces
this one instead of stacking a second copy. On Claude Code the
`cpersona-memory` skill does this for you, with your approval; everywhere else
it is a paste. The rules the block follows — consent, placement, idempotency,
the 40-line budget, client neutrality — are the
[policy block standard](CLAUDE_MD_POLICY_STANDARD.md). The block is
maintained in the skill; this copy is checked against it in CI.

```markdown
<!-- BEGIN cpersona-policy v2 (managed by the cpersona-memory skill; re-run the skill to update) -->
## CPersona memory policy

Use the CPersona MCP tools proactively with `agent_id="<AGENT_ID>"` — never wait to be asked.

**Session start** → `recall(agent_id, query="<opening-topic keywords or ''>", limit=10)` before
the first substantive action. Prefer `recall_with_context` when conversation history is already
at hand; add `deep=true` when the first pass comes back thin. Skip only for trivial one-shot
questions.

**Decisions, rules, preferences, bug findings** → `store` immediately. Fire on phrases like
"let's go with X", "from now on always Y", "remember that…", "approved", "that's a bug".
Protect must-never-lose rules with `lock_memory`. After a successful `git commit`, `store` a
one-line record: hash, what changed, why.

**Changing an existing rule** → `update_memory`, never delete + store. If the memory is locked:
`unlock_memory` → `update_memory` → `lock_memory`.

**Session end** — fire on closing phrases ("that's all for today", "wrap it up", "good night") →
first `store` + lock any unsaved decisions, then `archive_episode(agent_id, history=<the REAL
turns>, summary=…, keywords=…, resolved=…)`, computing `summary` and `keywords` yourself.

**"Don't save this" / benchmark sessions** → `pause_persistence(ttl_seconds=1800)`;
`resume_persistence()` (or TTL expiry) restores. Reads still answer, minus the writes inside them.

**Degraded mode** — if a `recall` response carries an `advisory` field, surface it to the user
and follow its runbook. Never quietly serve keyword-only recall.

**Quality** — if recall feels off, `set_recall_precision` (strict/balanced/lenient) is the one
policy knob; run `calibrate_threshold(agent_id)` after the corpus changes substantially.
Monthly: `check_health(agent_id, fix=true)`.

**If this client keeps a memory file that loads every session** (Claude Code's `MEMORY.md`), use it
as the deterministic index over this store: one line per memory — `- <slug> — <the sentence that
changes behaviour>` — with the body stored here under `message.id="memory-index:<slug>"` and content
starting `[<slug>]`, so a line tells you what to `recall`. Recall is ranked and may not surface a
memory; the index always arrives. Its size cap fails **silently** when exceeded, so consolidate at
80%, not at the limit. Never migrate existing memories into this store without asking first.

Details, setup, and troubleshooting: the `cpersona-memory` skill.
<!-- END cpersona-policy -->
```

## Where to go next

| You want to… | Read |
|---|---|
| Know which behaviors you can rely on | [Behavior Contracts](behavior-contracts.md) |
| See what each tool does | [Tools](tools.md) |
| Understand how retrieval works | [Architecture](architecture.md) |
| Back up, tune, or diagnose a live instance | [Operations Runbook](operations.md) |
| Look up a setting | [Configuration](configuration.md) |
| Serve several clients over the network | [Remote HTTP transport](configuration.md#remote-http-transport) |
