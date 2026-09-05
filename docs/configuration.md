# Configuration

> **Applies to: CPersona 2.5.x.** All settings are environment variables with
> sensible defaults. This page is the canonical reference; the README keeps
> only the quick-start subset.

## Core settings

| Variable | Default | Description |
|----------|---------|-------------|
| `CPERSONA_DB_PATH` | `data/cpersona.db` | SQLite database path, relative to the client's working directory — set it to an absolute path to keep one memory across sessions |
| `CPERSONA_EMBEDDING_MODE` | `none` | Embedding mode: `http` (a local embedding server), `api` (an OpenAI-compatible endpoint — `CPERSONA_EMBEDDING_API_URL` defaults to OpenAI's, so this mode bills per request), or `none` |
| `CPERSONA_EMBEDDING_URL` | *(unset)* | Embedding server URL, e.g. `http://127.0.0.1:8401/embed` |
| `CPERSONA_VECTOR_SEARCH_MODE` | `local` | Vector search execution (`local` in-process cosine, or `remote` offload) |
| `CPERSONA_RECALL_MODE` | `rrf` | Recall fusion strategy (`rrf`, `rsf`, or `cascade`) — see below |
| `CPERSONA_RECALL_PREVIEW_CHARS` | `500` | Preview tier: max content chars returned by the recall tools. `full_content=true` returns full text under a 200,000-char per-response budget (bug-211): past it, rows degrade back to the preview tier — most relevant kept whole first (bug-214) — and the response carries `full_content_budget_chars`; `get_contents` fetches the remainder under its own 40,000-char budget. `0` disables the preview tier **and both budgets** — degrading to a disabled tier would silently drop content, so opting out of trimming opts out of it everywhere |
| `CPERSONA_RRF_K` | `60` | RRF smoothing parameter |
| `CPERSONA_MAX_CONTENT_LENGTH` | `16000` | Max characters per stored memory or episode. Longer writes are truncated; `check_health(fix=true)` also cuts existing rows above the cap, so lowering it shortens data that was already stored. Raised from `2000` in 2.5.4a2 — text past the embedding window is still searchable through the keyword channel, which indexes the stored row in full |
| `CPERSONA_MAX_PROFILE_LENGTH` | `2000` | Max characters per profile row, capped separately from memories: the profile is never preview-trimmed, so this cap is the only thing bounding it. It is not injected into *every* response: the quality gate drops profile rows while the pool holds fewer than 50 rows, and `limit` cuts them when the scored results already fill it ([contract §7](behavior-contracts.md#7-profile-rows-carry-no-score)) |
| `CPERSONA_CONFIDENCE_ENABLED` | `false` | Include confidence metadata in results — and make it the ranking key: the result set is re-sorted by the score, and the quality gate keys on it. With this on, `CPERSONA_RECALL_MODE` no longer decides the returned order ([contract §2](behavior-contracts.md#2-confidence-scoring-overrides-the-fusion-mode)) |
| `CPERSONA_AUTO_CALIBRATE` | `false` | Auto-calibrate on startup |
| `CPERSONA_TASK_QUEUE_ENABLED` | `true` | Background task queue (DB-persisted, crash-recoverable) |
| `CPERSONA_RECENT_RECALL_PENALTY` | `0.7` | Penalty for recently recalled memories |
| `CPERSONA_RECENT_RECALL_WINDOW_MIN` | `5` | Window (minutes) for recent recall penalty |
| `CPERSONA_MAX_MEMORIES` | `10000` | The vector retriever's **scan window** (not a storage cap) — raise it for large corpora ([contract §4](behavior-contracts.md#4-the-vector-scan-window-cpersona_max_memories)) |
| `CPERSONA_VECTOR_REACH` | `0` | How far past the scan window the vector retriever may look, in rows. It **must exceed `CPERSONA_MAX_MEMORIES` to have any effect**: at or below it (and at the default `0`) the far list does not exist and nothing extra runs. Above it, the rows between the two numbers are ranked as a **second list** and fused alongside the first, so the window keeps working as a recency prior while the reach extends independently. Local vector search and the `rrf`/`rsf` fusion modes only ([contract §4](behavior-contracts.md#4-the-vector-scan-window-cpersona_max_memories)) |
| `CPERSONA_VECTOR_FAR_LIMIT` | `0` | How many rows of that second list reach fusion. `0` (the default) means **the same as the response `limit`**, which is the second list exactly as it is built without this setting; a positive value cuts it to `min(limit, N)` rows. It bounds a candidate count and changes nothing about how a row is scored, so the rows it keeps are the ones the full-length list led with. Irrelevant unless `CPERSONA_VECTOR_REACH` is above `CPERSONA_MAX_MEMORIES`; the first list's own cut stays at `limit` ([contract §4](behavior-contracts.md#4-the-vector-scan-window-cpersona_max_memories)) |
| `CPERSONA_AUTOCUT_MIN_RESULTS` | `3` | Result sets smaller than this are never autocut. Autocut fires on similarity-scale signals — under confidence scoring, or on the homogeneous raw-cosine list `cascade` produces — and is deliberately inert under `rsf`/`rrf` ([contract §6](behavior-contracts.md#6-autocut-fires-only-on-similarity-scale-signals)), so the fusion mode decides whether this knob does anything |
| `CPERSONA_FUSED_GATE_ENABLED` | `true` | The post-fusion quality gate. Disabling it is a last resort: filtering falls back to the pool-size heuristic, which is coarser but still rejects weak matches — what you lose is the operating point measured for this corpus |
| `CPERSONA_DEGRADED_ADVISORY` | `true` | Attach an `advisory` to recall responses while embeddings are unavailable ([runbook](operations.md#detecting-a-dead-embedding-server)) |
| `CPERSONA_UPDATE_CHECK` | `true` | Check pypi.org once per process start for a newer — or withdrawn — release of this server, and report it through `recall` / `check_health` / `check_update` ([what it sends](architecture.md#transports)). `false` disables the feature entirely: no request, no cache file, no notice. Updating is never automatic either way |
| `CPERSONA_UPDATE_CHECK_INTERVAL_SECONDS` | `86400` | How long that verdict stays usable, cached in `update-check.json` beside the database — a restart inside the window makes no request |
| `CPERSONA_EPISODE_PENALTY_ENABLED` | `true` | Episode boundary penalty ([contract §3](behavior-contracts.md#3-episode-boundary-penalty)) |
| `CPERSONA_EPISODE_DECAY_RATE` | `0.01` | Penalty decay rate per hour before the boundary |
| `CPERSONA_EPISODE_DECAY_FLOOR` | `0.5` | Penalty floor (older memories are at most halved) |

The generic aliases `EMBEDDING_MODE` / `EMBEDDING_HTTP_URL` / `EMBEDDING_MODEL`
are also accepted (the `CPERSONA_`-prefixed form wins when both are set) — the
marketplace catalog and the Quick Start use the generic names.

## Corpus scale caps

These bound work that grows with the corpus: index maintenance, health repair
and calibration sampling. Each is an absolute row count that was sized against a
corpus of roughly 10,000 rows, where it covered the whole thing — against a
150,000-row corpus the same number is a sample. A cap that bites never raises an
error, it returns a smaller answer, so raise these deliberately rather than
waiting for a symptom.

| Variable | Default | Description |
|----------|---------|-------------|
| `CPERSONA_VECTOR_INDEX_MAX_EXCLUDED_IDS` | `10000` | Rows the vector index may name as *holes* — rows it could not place in the file (a non-standard `created_at`) plus rows that carried no embedding when the build ran. They are read by id from the live table on every query. Past this many the index declines to build at all, which leaves recall on the (correct, slower) full scan — the state a bulk import produces while its embedding backlog drains. The default covers 6.7% of a 150,000-row corpus; the worst case, every named hole having since gained an embedding, costs roughly 65 ms per query until the next rebuild absorbs them |
| `CPERSONA_REEMBED_ROW_CAP` | `5000` | Rows without an embedding that one `check_health(fix=true)` run re-embeds, and the ceiling on the `repairable` count it reports. Embedding happens before the write lock is taken, so this bounds prefetch wall time and the number of locked `UPDATE`s. Raise it to drain a large backlog in fewer runs: at the previous default of 500, a 50,000-row backlog took 100 runs, and while a backlog exceeds `CPERSONA_VECTOR_INDEX_MAX_EXCLUDED_IDS` the index cannot be built either |
| `CPERSONA_NEAR_DUPLICATE_ROW_CAP` | `5000` | Embedded rows `deep_near_duplicate` compares. The comparison is O(n²) in memory: measured on 1024-dimension vectors, 5,000 rows peak at 266 MB for about 100 ms, and 10,000 rows at 982 MB — which is why the default samples rather than covering a large corpus |
| `CPERSONA_INVALID_SOURCE_CLASSIFY_CAP` | `10000` | Offending `source` rows one `check_invalid_source_type` run classifies. The cost is JSON parsing per row (microseconds), so this can afford to be larger than the caps above. Past the cap the sample is incomplete and the check declines to downgrade its own severity — the cap costs a verdict, not correctness |
| `CPERSONA_CALIBRATE_MAX_SAMPLE` | `5000` | Hard ceiling on `calibrate_threshold`'s `sample_size`, whatever the caller asks for. It feeds the same O(n²) matrix as the near-duplicate cap and exists to stop an unbounded value from exhausting memory for every agent on the connection, so raise it only as far as the machine can hold (see the measurements above) |

## Remote (HTTP) transport

The default transport is stdio, where the MCP client owns the process and no
network is involved. Set `CPERSONA_TRANSPORT=streamable-http` to serve over HTTP
instead — one server, several clients, reachable over a network.

| Variable | Default | Description |
|----------|---------|-------------|
| `CPERSONA_TRANSPORT` | `stdio` | `stdio`, or `streamable-http` to serve over HTTP |
| `CPERSONA_HTTP_HOST` | `127.0.0.1` | Bind address |
| `CPERSONA_HTTP_PORT` | `8402` | Bind port |
| `CPERSONA_AUTH_TOKEN` | *(unset)* | Bearer token required on every request |
| `CPERSONA_ALLOW_UNAUTHENTICATED_HTTP` | `false` | Run the HTTP transport with no authentication at all |
| `CPERSONA_ACL_FILE` | *(unset)* | Per-client capability mode: named bearer tokens with per-agent read/write grants, deny-by-default (see [ACL design](ACL_DESIGN.md)) |
| `CPERSONA_OAUTH_RESOURCE` | *(unset)* | Canonical resource identifier published in the RFC 9728 metadata and expected back from the client. Discovery stays off while this is empty (see [OAuth design](OAUTH_DESIGN.md)) |
| `CPERSONA_OAUTH_AUTHORIZATION_SERVERS` | *(unset)* | Whitespace- or comma-separated issuer URLs the client should authenticate against. Discovery stays off while none is listed |
| `CPERSONA_OAUTH_SCOPES` | *(unset)* | Scope advertised on the 401 and in `scopes_supported`. The client sends back exactly what is asked for, and the authorization server refuses a scope it does not define with `invalid_scope` — advertise only scopes your issuer defines |
| `CPERSONA_OAUTH_JWKS_URI` | *(unset)* | Where the issuer's signing keys are, for a provider whose metadata this server cannot read. Normally discovered from the issuer's own metadata; ignored unless exactly one authorization server is configured |
| `CPERSONA_ALIAS_LEDGER_FILE` | `alias_ledger.json` beside the DB | Where the per-subject alias ledger lives — the server-written `(issuer, subject) → alias` map behind `"per_subject": true` rows (see [OAuth design §12](OAUTH_DESIGN.md)). Defaults beside the database because the server writes it, unlike the operator-owned ACL file |
| `CPERSONA_HTTP_MAX_BODY_BYTES` | `4194304` | Budget for one request body, in bytes, counted as it arrives rather than read from `Content-Length` |
| `CPERSONA_HTTP_BODY_LIMIT_MODE` | `warn` | What crossing that budget costs: `warn` reports it and serves the request anyway, `reject` answers 413 and stops reading, `off` disables the accounting |
| `CPERSONA_EXTERNAL_CONTEXT_MODE` | `warn` | What a `recall_with_context` entry whose declared field is not a string costs: `warn` reads that field as absent and names the entry in `context_field_issues`, `reject` refuses the call, `off` keeps the safe read and drops the report |

**The body budget measures, it does not yet refuse.** Every other cap in this
server — `CPERSONA_MAX_CONTENT_LENGTH` and the rest — is applied by a tool
handler, which runs after the whole body has been received and parsed, so those
caps bound what is stored and say nothing about what it costs to arrive.
`CPERSONA_HTTP_MAX_BODY_BYTES` is counted where the bytes appear, summed across
the chunks the server actually receives: a body sent in chunks with no
`Content-Length`, and a body whose `Content-Length` understates it, are both
measured by what arrived. The default of 4 MiB is roughly 29x the largest single
`store` this server can accept and 10x a `recall_with_context` carrying 200
conversation turns, so ordinary traffic is nowhere near it.

The default mode is `warn` on purpose: the request is served in full and the
crossing is logged (at the 1st, 10th and 100th occurrence, so the line neither
floods nor disappears). Nothing in this project knows what your payloads
actually look like, and a limit that refuses before anyone has measured is a
limit set by guessing — so run with the default, read the log, and set
`CPERSONA_HTTP_BODY_LIMIT_MODE=reject` once you know the number fits your
traffic. Both paths are tested; enabling enforcement changes a setting, not a
code path.

**A context entry states its shape now.** Each entry in
`recall_with_context`'s `external_context` declares five string fields — `role`,
`content`, `name`, `user_id` and `timestamp` — and until 2.5.12 the schema named
only the first two. The other three were read all along, so a caller working from
the schema had no way to know that a `timestamp` was consulted at all, and an
entry sent without one merges into the undated group that sorts ahead of every
dated message.

A field that is present but not a string names nothing the field can mean, so it
is read as absent and the entry merges without it; the response then carries
`context_field_issues` naming the entry's index and the fields, so nothing is
absorbed silently. Set `CPERSONA_EXTERNAL_CONTEXT_MODE=reject` to refuse such a
call instead — the default stays `warn` because no payload that works today
should stop working in the release that first states the rule. Fields the schema
does **not** declare are still accepted and ignored: a caller carrying its own
bookkeeping alongside these keeps working.

**Discovery is off until you turn it on.** A client that supports OAuth looks for RFC 9728
metadata; finding none, it falls through to asking a human to type in a client id — correct
behaviour for a client given nothing to discover, and easily misread as a broken credential.
Setting `CPERSONA_OAUTH_RESOURCE` **and** at least one entry in
`CPERSONA_OAUTH_AUTHORIZATION_SERVERS` publishes the metadata and puts `resource_metadata` and
`scope` on the 401. With either unset the responses are byte-identical to a build without the
feature, so enabling it is a deliberate act rather than an upgrade side effect.

**The same two settings accept tokens, and that needs `CPERSONA_ACL_FILE`.** A token signed by a
listed issuer and minted for exactly the configured resource resolves to the client identifier
`oauth:<issuer>:<client_id>`, which is what you write grants against; a token for any other
resource is refused, which is the check the MCP SDK leaves to the resource server. Verification
requires ACL mode because a verified identity with no grant table behind it would reach every
tool — with no ACL file the server logs that verification is staying off and keeps serving
discovery, so clients still find the issuer and are then refused. Grants are per client: until
someone adds the row, a newly connected client authenticates and every scoped tool refuses it,
saying in `detail` that the grant table has no entry for it.

**A loopback bind is not a security boundary.** Tunnels (cloudflared, ngrok),
reverse proxies, `kubectl port-forward` and published container ports all forward
to `127.0.0.1`, so binding there says nothing about who can reach the port. Every
tool is exposed to whoever can — including `delete_agent_data` and the
file-reading/writing `export_memories` / `import_memories`. Set
`CPERSONA_AUTH_TOKEN` whenever the process is not something only you can talk to.

Since v2.5.3 the server enforces that: with `CPERSONA_TRANSPORT=streamable-http`
and no `CPERSONA_AUTH_TOKEN`, it refuses to start. **If you are upgrading from
2.5.2 or earlier and run the HTTP transport without a token, it will not start**
— set `CPERSONA_AUTH_TOKEN`, or set `CPERSONA_ALLOW_UNAUTHENTICATED_HTTP=true` to
state that you really do want no authentication (local development only).
Earlier versions allowed an unauthenticated loopback bind and logged that it was
"bound to loopback only", which read as an all-clear and was not one.

Setting `CPERSONA_ACL_FILE` satisfies the same requirement a different way:
every request must then resolve to a named client, so the single-token check
does not apply. In that mode `CPERSONA_AUTH_TOKEN` is **ignored** (with a
startup warning) — credentials come from the ACL file only, and a client that
should keep using the old token must be listed there explicitly. Grant model,
file format and per-tool classification: [ACL design](ACL_DESIGN.md).

## Recall fusion mode (`CPERSONA_RECALL_MODE`)

- **`rrf`** (default) — Reciprocal Rank Fusion: merges the vector + FTS channels by
  rank only. Robust and scale-free, but discards score magnitude.
- **`rsf`** — Relative Score Fusion: per-query min-max-normalizes each channel's raw
  score (cosine for vector, bm25 for keyword) and sums them, so the keyword channel's
  bm25 magnitude survives the merge. **Recommended for topic-drift-prone or space-less
  language (e.g. Japanese) contexts**, where that magnitude is the discriminating
  signal `rrf` flattens away (≈ Weaviate's `relativeScoreFusion`; see the ClotoCore
  `RECALL_CONTAMINATION_AB_2026-06-14` report §10–12). Note what the
  normalization costs: it pins each channel's lowest-scoring row to 0.0, and a
  channel that returns a single candidate pins that row to 1.0, so a fused score
  places a row among the candidates retrieved with it rather than measuring its
  similarity to the query. Autocut does not act on that pin — it fires only on
  similarity-scale signals
  ([contract §6](behavior-contracts.md#6-autocut-fires-only-on-similarity-scale-signals))
  — but the quality gate still compares the fused score against a cosine-scale
  threshold. So with `CPERSONA_CONFIDENCE_ENABLED=false`, which is the default and
  what the [CJK guidance](operations.md#japanese-and-cjk-corpora) assumes, a
  strongly matching row can be dropped for being the weakest of a strong set, and
  a weak lone match can pass. Turning confidence on moves the gate onto the
  confidence score and avoids this, at the cost described just below. `rrf`
  remains the default.
- **`cascade`** — Sequential channel fill (legacy).

**With `CPERSONA_CONFIDENCE_ENABLED=true`, the fusion mode does not decide the order you
get back.** Fusion selects which candidates enter the result set; confidence scoring then
re-sorts that set, and the quality gate keys on the confidence score rather than on the
fused one. Measured on a 1,545-document corpus with 394 queries: with confidence on,
`rsf` and `rrf` returned the same rows in the same order for **all 394** queries; with it
off, the two agreed on fewer than 10%. So if you set a fusion mode expecting a ranking
change, either leave confidence off, or expect the mode to affect which memories are
considered and not the order they come back in.
