# Tools

> **Applies to: CPersona 2.5.x.** The authoritative description of every
> argument is the tool's own MCP description — your client reads it, and it
> ships with the version you are running. This page groups the **30 tools** by
> what you reach for them for, and links to the contract when a tool behaves in
> a way its name does not suggest.

## Everyday memory

| Tool | What it does |
|---|---|
| `store` | Write one message to memory. Branch on `result` — `stored` / `skipped` / `rejected` — not on `ok` ([dedup contract](behavior-contracts.md#5-dedup-semantics-skip-not-upsert)) |
| `recall` | Retrieve memories through the three-layer hybrid search. **The last element is the best match** ([ordering contract](behavior-contracts.md#1-recall-return-order-last-is-best)) |
| `recall_with_context` | Recall *and* merge with conversation history you pass in, deduplicated. Returns a **chronological** merge, not a score ordering |
| `get_contents` | Expand preview refs (`mem:<id>` / `ep:<id>`) returned by recall into full text ([preview tier design](RECALL_PREVIEW_TIER_DESIGN.md)) |
| `archive_episode` | Store a session summary. Also moves the [episode boundary](behavior-contracts.md#3-episode-boundary-penalty), which down-weights everything written before it |
| `update_memory` | Change the content of an existing memory. This — not a re-`store` with the same `msg_id` — is how you correct a stored fact |

## Profile and operator context

| Tool | What it does |
|---|---|
| `get_profile` | Read the accumulated user/project profile for an agent |
| `update_profile` | Replace the profile with a summary you computed. CPersona never writes it for you — see [zero LLM dependency](architecture.md#zero-llm-dependency) |
| `get_operating_context` | Read the operator-owned instructions served to every connected client. Read-only over MCP; edited on the filesystem ([design](OPERATING_CONTEXT_DESIGN.md)) |

Profile rows are injected into recall responses but
[carry no score](behavior-contracts.md#7-profile-rows-carry-no-score) — a
detail that matters when you tighten `limit`.

## Browsing

| Tool | What it does |
|---|---|
| `list_memories` | Recent memories, newest first — no search, no scoring |
| `list_episodes` | Archived episodes, newest first |
| `get_queue_status` | Depth and retry state of the background task queue |

## Protection and deletion

| Tool | What it does |
|---|---|
| `lock_memory` | Refuse edits and deletes on a memory. It is **protection, not a ranking boost** ([contract](behavior-contracts.md#9-lock_memory-protects-it-does-not-boost)) |
| `unlock_memory` | Lift that protection |
| `delete_memory` | Delete one memory. Ownership is enforced **only when `agent_id` is passed** — omit it and the delete is unscoped and can remove another agent's row (rejected while locked either way) |
| `delete_episode` | Delete one episode. Same conditional ownership as `delete_memory` above |
| `delete_agent_data` | Delete **everything** belonging to one agent. Exposed over the network like any other tool — reason enough to set `CPERSONA_AUTH_TOKEN` on the [HTTP transport](configuration.md#remote-http-transport) |

## Retrieval quality

| Tool | What it does |
|---|---|
| `set_recall_precision` | The main gate knob. Sets an agent's precision preference and recalibrates its **post-fusion quality gate** — reach for this before touching raw thresholds ([tuning order](operations.md#tuning-recall)) |
| `get_recall_precision` | Read the effective precision for an agent |
| `calibrate_threshold` | Re-derive the **vector** threshold from the corpus itself — by default (`separation`) from where a null distribution of random pairs separates from same-session positives; `percentile` and `zscore` are the alternatives. No labels needed. Run it after a re-embed or a large import |

## Portability and migration

| Tool | What it does |
|---|---|
| `export_memories` | Write memories, episodes and profiles to JSONL — schema-version independent, so it doubles as a logical backup ([backup runbook](operations.md#backup-and-restore)) |
| `import_memories` | Read that JSONL back. Idempotent, though not by one key: memories dedup on `msg_id` **and** on identical content within the project/channel scope, while episodes dedup on an identical summary (they carry no `msg_id`) |
| `merge_memories` | Move or copy one agent's data into another, atomically and with deduplication |
| `migrate_channel_axis` | Re-channel bridge-type memories onto their concrete channel. A one-time repair, not a routine operation |

## Health and maintenance

| Tool | What it does |
|---|---|
| `check_health` | Registry-driven check with severity-tagged issues and, with `fix=true`, auto-repair: contamination, duplicates, FTS integrity, embedding-dimension drift, schema objects, stale tasks, invalid data. Some checks are report-only by design — isolation-axis hygiene among them, because which spelling of an axis is canonical is an operator's call, not a repair |
| `deep_check` | Semantic data-quality pass: anonymous sources, too-short content, stale profiles, orphaned episodes |
| `get_session_findings` | The same findings, pulled on demand — the SuperAuditor pull contract ([standard](SUPERAUDITOR_STANDARD.md)). Whole-database by design (no agent or project filter), read-only, with `capped_kinds` naming every kind that had more than `per_kind_limit` rows. A probe that raised shows up as a finding of kind `check_crashed` rather than failing the call |

`check_health` and `deep_check` are also reachable outside MCP as `python -m cpersona.checkup`, which is
the form to use in CI. Cadence guidance is in the
[operations runbook](operations.md#maintenance-cadence).

## Session controls

| Tool | What it does |
|---|---|
| `pause_persistence` | Turn writes into no-ops for a TTL window. Responses carry `persisted: false` — branch on that, not on an id |
| `resume_persistence` | Re-enable writes immediately |
| `persistence_status` | Whether writes are paused, and how much TTL remains |

Use these for benchmarking or throwaway exploration you do not want in the
corpus. **The blast radius follows `session_key`**, which each of the three
reports back as `scope`. Declare a key on the pause and on the write calls it
should cover, and the pause covers that key alone (`scope: "session"`); a
session sending a different key is neither silenced by it nor able to clear it.
The key is compared, never verified, so it partitions keys rather than callers —
anyone sending the same string shares the pause.

Omit the key and you arm the bucket every keyless caller shares
(`scope: "process"`). Under stdio, where the client owns its own process, that
bucket is the session; on a streamable-HTTP deployment one process serves every
client, so a keyless pause silences writes for every other keyless session — and
those sessions are not told.

Two paths do not fit the `persisted: false` shape: `check_health` and
`deep_check` are not blocked but downgrade to `fix=false`, and
`migrate_channel_axis` is forced into dry-run and reports `repairs_skipped`
without a `persisted` key at all.

## Isolation arguments

The three isolation axes are not offered uniformly. `agent_id` is accepted by
most tools (22 of 30); `project_id` by six; and `channel` by exactly four —
`store`, `recall`, `recall_with_context` and `archive_episode`. They are
independent axes rather than one nested hierarchy, and reads treat an empty
value differently from an omitted one — see
[isolation axes](architecture.md#isolation-axes).
