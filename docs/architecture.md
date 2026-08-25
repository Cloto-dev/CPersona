# Architecture

> **Applies to: CPersona 2.5.x.** This page explains how the pieces fit
> together and why. Where a mechanism has a caller-visible guarantee, the
> guarantee lives in [Behavior Contracts](behavior-contracts.md) and is linked
> from here rather than restated.

## The pieces

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
                         │  ┌─────────┐  ┌─────────┐           │
                         │  │  store  │  │ recall  │  ...      │
                         │  └────┬────┘  └────┬────┘           │
                         │       │            │                │
                         │  ┌────▼────────────▼─────────────┐  │
                         │  │         SQLite DB             │  │
                         │  │                               │  │
                         │  │  memories   (content + embed) │  │
                         │  │  episodes   (summaries)       │  │
                         │  │  profiles   (attributes)      │  │
                         │  │  memories_fts (FTS5 index)    │  │
                         │  │  episodes_fts (FTS5 index)    │  │
                         │  │  pending_memory_tasks (queue) │  │
                         │  └───────────────────────────────┘  │
                         │                                     │
                         └──────────────┬──────────────────────┘
                                        │ HTTP
                         ┌──────────────▼──────────────────────┐
                         │       Embedding Server              │
                         │  (jina-v5-nano ONNX, 768d)          │
                         └─────────────────────────────────────┘
```

Two things follow from this shape and are worth stating plainly:

- **The embedding server is the only external dependency**, it is optional,
  and it is reached over HTTP — so it is also the only part that can fail at a
  network boundary. That is why degradation has [its own detection
  surface](operations.md#detecting-a-dead-embedding-server) instead of being
  left to whoever notices worse answers.
- **Almost everything else is one file.** No daemon to supervise, no service
  to provision — the corpus is a `.db` you can copy. Three small files live
  *outside* it and are the ones people forget when moving hosts: the
  calibration sidecar `<CPERSONA_DB_PATH>.calibration.json` (per-agent
  thresholds and gate state), the operator's
  `~/.cpersona/operating-context.toml`, and the ACL file if you use one.
  Restoring the `.db` alone restores every memory and silently loses the
  tuning — see [backup and restore](operations.md#backup-and-restore).

## Storage

One SQLite database in WAL mode (`CPERSONA_DB_PATH`), currently **schema
v13**, migrated forward automatically on startup. Four data tables —
`memories`, `episodes`, `profiles`, `pending_memory_tasks` — alongside a
`schema_version` bookkeeping table and two FTS5 virtual tables kept in step by
triggers.

The FTS5 indexes use the **trigram** tokenizer. That choice is what makes
CPersona work on Japanese and other space-less scripts at all: a word-boundary
tokenizer indexes a Japanese sentence as one enormous token, while trigrams
match substrings regardless of where words begin. It is also why the keyword
channel earns its keep for identifiers and error strings, which vector search
routinely misses.

Since WAL keeps a live `-wal` sidecar, **a plain `cp` of a running database can
straddle a checkpoint and produce a corrupt copy** — the
[backup runbook](operations.md#backup-and-restore) gives the safe forms.

## Retrieval

**Three retrievers** feed the fusion step: vector search, FTS5 over memories,
and FTS5 over episodes.

| Retriever | Method | What it is good at |
|---|---|---|
| Vector | Cosine similarity over stored embeddings | Meaning — paraphrases, synonyms, "the thing about X" |
| FTS5 (memories) | SQLite full-text search, trigram tokenizer | Exact terms: names, identifiers, error strings, CJK substrings |
| FTS5 (episodes) | The same, over episode summaries and keywords | Finding the session in which something was discussed |

A **keyword (`LIKE`) pass is not a fourth retriever.** It sits inside the
memories channel as a fallback and runs only when FTS is disabled or its
`MATCH` returns nothing — so it never merges alongside the FTS memories
retriever, it stands in for it.

The pipeline in `rrf` mode:

```
Query → ┌── Vector search (cosine similarity) ──────────┐
        ├── FTS5 over memories (keyword LIKE fallback) ─┼── fusion → quality gate → limit → reverse
        └── FTS5 over episodes ────────────────────────┘
```

Four stages deserve individual attention, because each has a caller-visible
consequence:

1. **Fusion** (`CPERSONA_RECALL_MODE`). `rrf` merges by rank only — robust,
   scale-free, and it discards score magnitude. `rsf` normalizes each
   channel's raw score per query and sums them, so bm25 magnitude survives the
   merge; that magnitude is the discriminating signal on
   [Japanese corpora](operations.md#japanese-and-cjk-corpora), which is why
   `rsf` is recommended there. `cascade` fills channels sequentially and is
   legacy.
2. **The quality gate** decides what is good enough to return at all. Its
   threshold is derived from the corpus by `calibrate_threshold`, and
   `set_recall_precision` is the knob you actually turn. When every candidate
   falls below it the response comes back **empty** — except under confidence
   scoring, where the below-gate lexical matches are returned marked with
   [`gate_fallback`](behavior-contracts.md#8-gate_fallback-responses-are-low-confidence)
   instead. That marker is unreachable in the default configuration
   (confidence off), which is worth knowing before you go looking for it.
3. **Confidence scoring** (`CPERSONA_CONFIDENCE_ENABLED`, off by default) is
   not a metadata switch: with it on, the result set is **re-sorted by the
   confidence score** and the gate keys on that score instead of the fused one
   ([contract §2](behavior-contracts.md#2-confidence-scoring-overrides-the-fusion-mode)).
   Confidence blends cosine similarity, dynamic time decay, resolved status
   and recall count — so it is **not** match strength, and an exact match can
   legitimately rank below a paraphrase on that scale.
4. **The final reverse.** Results are cut to `limit` and then reversed, so the
   response is ordered worst-to-best and **the last element is the strongest
   match** ([contract §1](behavior-contracts.md#1-recall-return-order-last-is-best)).
   This is deliberate: LLMs attend most strongly to the end of their context,
   so the best memory is placed nearest the injection point.

Two bounds sit on either side of fusion. `CPERSONA_MAX_MEMORIES` is the vector
retriever's [scan window](behavior-contracts.md#4-the-vector-scan-window-cpersona_max_memories)
— not a storage cap — and it bounds what fusion ever gets to see. The
[episode boundary penalty](behavior-contracts.md#3-episode-boundary-penalty)
works on the other side: it multiplies the *already fused* score of memories
older than the most recent `archive_episode`, before the gate runs.

## The three memory types

- **Declarative** (`store` / `recall`) — individual facts, decisions, rules.
  The everyday unit.
- **Episodic** (`archive_episode`) — session summaries. They are searched
  alongside declarative memories, and archiving one also moves the boundary
  that ages everything written before it, which is what keeps an old corpus
  from drowning today's answers.
- **Profile** (`update_profile`) — accumulated attributes about the user or
  project. Appended to recall responses **when the scope holds at least 50
  rows** (memories and episodes together — the pool the gate governs; below
  that the gate drops it), never preview-trimmed, and it
  [carries no score](behavior-contracts.md#7-profile-rows-carry-no-score) — so
  in the default configuration it sorts last and can be cut by `limit`.

## Isolation axes

Rows are separated on three axes that compose rather than nest. The read
semantics are deliberately **not** uniform, because the axes answer different
questions:

| Axis | Omitted (`None`) | Empty (`''`) | A value `X` |
|---|---|---|---|
| `agent_id` | no filter — a deliberate cross-agent scan | exact match on `''` | exact match on `X`; agents never share rows |
| `project_id` | no filter | the global pool only | `X` **plus** the global pool |
| `channel` | no filter | no filter | `X` plus channel-less rows |

The asymmetry is the point. `agent_id` is hard isolation with no union —
agents never share rows — and binding `''` narrows rather than widens: internal
code that assembles a predicate without deciding the axis addresses the
empty-agent bucket, never another agent's rows. That bucket is a real address,
not a value no write produces: `store` accepts an empty `agent_id`, because
*required* in a tool schema means present, not non-empty. **Omitting the axis
is the opposite case, and it is deliberate: a cross-agent scan.** The listing tools take it that way — a
`list_memories` call with no `agent_id` returns rows belonging to every agent
in the database. `project_id` unions with the global pool so shared
context reaches every project without being copied into each. `channel`
treats "unset" as "everything", so adding a channel to a bridge never hides
the memories written before it existed.

## Zero LLM dependency

CPersona never calls a generative model. It does not summarize, extract,
rewrite, or judge — the calling agent does all of that and hands CPersona the
result (`archive_episode` takes the summary you computed; `update_profile`
takes the profile you computed).

This is a deliberate trade: you write slightly more agent-side logic, and in
exchange memory adds **no API cost, no hidden latency, and no
nondeterminism**. It also means a CPersona answer can be reproduced — the same
corpus and query return the same rows, which is what makes the
[benchmarks](https://github.com/Cloto-dev/cpersona/blob/master/benchmarks/README.md)
measurable at all.

## Background task queue

`pending_memory_tasks` is a DB-persisted work queue with a worker that drains
it at startup and retries failed tasks on a fixed delay
(`CPERSONA_TASK_RETRY_DELAY`). Because it lives in the database
rather than in memory, a crash or a restart resumes the work instead of
losing it.

**In the current line, nothing enqueues onto it.** The queue existed for
server-side episode summarisation, which was removed before v2.4.10 — today
`archive_episode` requires a pre-computed summary and writes the row directly,
and profile updates are synchronous too. What remains is the drain side: rows
left behind by an older version, or by a run that was interrupted mid-task,
are still completed correctly. `get_queue_status` reports depth and retries,
and on a healthy modern instance it reports an empty queue — that is the
expected reading, not a symptom.

## Transports

The default is **stdio**: the MCP client owns the process and no network is
involved. `CPERSONA_TRANSPORT=streamable-http` serves several clients over
HTTP instead — at which point the server makes you decide about
authentication: set `CPERSONA_AUTH_TOKEN`, configure an ACL file, or say
explicitly with `CPERSONA_ALLOW_UNAUTHENTICATED_HTTP=true` that you want none.
v2.5.3 made that refusal **unconditional**; earlier versions inferred it from
the bind address, which is not a reachability boundary. The requirements and
per-client ACLs are covered in
[Remote HTTP transport](configuration.md#remote-http-transport).
