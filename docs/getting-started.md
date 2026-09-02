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
- An MCP client: Claude Desktop, Claude Code, or any other MCP host

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
configure things by hand.

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

## Where to go next

| You want to… | Read |
|---|---|
| Know which behaviors you can rely on | [Behavior Contracts](behavior-contracts.md) |
| See what each tool does | [Tools](tools.md) |
| Understand how retrieval works | [Architecture](architecture.md) |
| Back up, tune, or diagnose a live instance | [Operations Runbook](operations.md) |
| Look up a setting | [Configuration](configuration.md) |
| Serve several clients over the network | [Remote HTTP transport](configuration.md#remote-http-transport) |
