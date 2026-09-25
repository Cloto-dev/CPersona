# Configuration

> **Applies to: CPersona {{ version_line }}.** All settings are environment variables with
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
| `CPERSONA_RECALL_EXCERPT_CHARS` | `800` | The query-relevant excerpt a recall row carries beside its preview when the preview cuts it: the parts of the record that matched, filled in ranking order up to this many characters and shown in text order ([design](RECALL_PREVIEW_TIER_DESIGN.md#excerpt-26)). Absent under `full_content=true` and on rows shown whole. `0` disables it, and it is off whenever the preview tier is disabled |
| `CPERSONA_RRF_K` | `60` | RRF smoothing parameter |
| `CPERSONA_MAX_CONTENT_LENGTH` | `16000` | Max characters per stored memory or episode. Longer writes are truncated; `check_health(fix=true)` also cuts existing rows above the cap, so lowering it shortens data that was already stored. Raised from `2000` in 2.5.4a2 — text past the embedding window is still searchable through the keyword channel, which indexes the stored row in full |
| `CPERSONA_MAX_PROFILE_LENGTH` | `2000` | Max characters per profile row, capped separately from memories: the profile is never preview-trimmed, so this cap is the only thing bounding it. It is not injected into *every* response: the quality gate drops profile rows while the pool holds fewer than 50 rows, and `limit` cuts them when the scored results already fill it ([contract §7](behavior-contracts.md#7-profile-rows-carry-no-score)) |
| `CPERSONA_CONFIDENCE_ENABLED` | `false` | Include a `confidence` value with each returned row. From 2.6.0a7 it neither orders the result nor keys the quality gate unless `CPERSONA_CONFIDENCE_ORDERING=legacy` ([contract §2](behavior-contracts.md#2-confidence-scoring-overrides-the-fusion-mode)) |
| `CPERSONA_CONFIDENCE_ORDERING` | `fusion` | `fusion`: confidence is returned beside each row and does nothing else. `legacy`: the behaviour before 2.6.0a7 — with confidence on, the result is re-sorted by the confidence score and the quality gate keys on it, so `CPERSONA_RECALL_MODE` no longer decides the returned order |
| `CPERSONA_AUTO_CALIBRATE` | `false` | Auto-calibrate on startup |
| `CPERSONA_BLOCK_BUILD_ENABLED` | `false` | Build clause-sized blocks for each record and store one sign-quantised vector per block ([block reach](BLOCK_REACH_DESIGN.md)). Off means no embedding calls, no rows and no queue work — not "built but unread". Turning it on also starts a bounded backfill of the records already stored |
| `CPERSONA_BLOCK_RETRIEVAL_ENABLED` | `false` | Read the block index during recall ([block reach](BLOCK_REACH_DESIGN.md)): the block arm, and the quotation `reconstruct` returns — a claim is quoted from the block that matches, carrying the contiguous context that governs it, or reported as incomplete. Records it reaches are admitted by **reservation**: a fixed, small number of places held for them after the quality gate, which is neither consulted for those places nor altered for any other. The response therefore carries up to that many rows **beyond** the requested `limit`, and every row the previous release returned is still returned. Needs `CPERSONA_BLOCK_BUILD_ENABLED=true` — reading an index nothing fills is a startup error, not a quiet no-op. No effect where vector search is remote: the arm ranks against the query vector the local search embedded, and a remote search does not produce one |
| `CPERSONA_TASK_QUEUE_ENABLED` | `true` | Background task queue (DB-persisted, crash-recoverable) |
| `CPERSONA_RECENT_RECALL_PENALTY` | `0.7` | Penalty for recently recalled memories |
| `CPERSONA_RECENT_RECALL_WINDOW_MIN` | `5` | Window (minutes) for recent recall penalty |
| `CPERSONA_MAX_MEMORIES` | `10000` | The vector retriever's **scan window** (not a storage cap) — raise it for large corpora ([contract §4](behavior-contracts.md#4-the-vector-scan-window-cpersona_max_memories)) |
| `CPERSONA_VECTOR_REACH` | `0` | How far past the scan window the vector retriever may look, in rows. It **must exceed `CPERSONA_MAX_MEMORIES` to have any effect**: at or below it (and at the default `0`) the far list does not exist and nothing extra runs. Above it, the rows between the two numbers are ranked as a **second list** and fused alongside the first, so the window keeps working as a recency prior while the reach extends independently. Local vector search and the `rrf`/`rsf` fusion modes only ([contract §4](behavior-contracts.md#4-the-vector-scan-window-cpersona_max_memories)) |
| `CPERSONA_VECTOR_FAR_LIMIT` | `0` | How many rows of that second list reach fusion. `0` (the default) means **the same as the response `limit`**, which is the second list exactly as it is built without this setting; a positive value cuts it to `min(limit, N)` rows. It bounds a candidate count and changes nothing about how a row is scored, so the rows it keeps are the ones the full-length list led with. Irrelevant unless `CPERSONA_VECTOR_REACH` is above `CPERSONA_MAX_MEMORIES`; the first list's own cut stays at `limit` ([contract §4](behavior-contracts.md#4-the-vector-scan-window-cpersona_max_memories)) |
| `CPERSONA_RECALL_DEPTH_FLOOR` | `0` | Recall Depth: the fewest candidates each retrieval arm hands to the fusion, whatever `limit` asks to receive. The depth is `max(limit, this)`, capped by `CPERSONA_RECALL_LIBRARY_MAX_LIMIT`; at `0` it equals `limit`, which is the coupling the 2.5 line shipped with — nothing in the ranking moves until you set it. When it exceeds `limit`, the response carries `depth`, so a caller can see that a 5-row answer was ranked over more than 5 candidates per arm. Fusion modes only: `cascade` fills `limit` slots stage by stage and has no list to deepen ([design](RELIABLE_RECALL_2_6.md#4-depth-is-not-count)) |
| `CPERSONA_RECALL_CUE_TIME_LIMIT_MS` | `1000` | The time cue's one revision (`recall` / `reconstruct` with `time_cue`): when the cue's period holds nothing, the period is widened once and searched again, unless the recall has already taken this many milliseconds. A stop at the limit is recorded in the recall trace ([design](RECALL_PROCESS_DESIGN.md#25-one-revision)) |
| `CPERSONA_AUTOCUT_MIN_RESULTS` | `3` | Result sets smaller than this are never autocut. Autocut fires on similarity-scale signals — under confidence ordering (`CPERSONA_CONFIDENCE_ORDERING=legacy`), or on the homogeneous raw-cosine list `cascade` produces — and is deliberately inert under `rsf`/`rrf` ([contract §6](behavior-contracts.md#6-autocut-fires-only-on-similarity-scale-signals)), so the fusion mode decides whether this knob does anything |
| `CPERSONA_FUSED_GATE_ENABLED` | `true` | The post-fusion quality gate. Disabling it is a last resort: filtering falls back to the pool-size heuristic, which is coarser but still rejects weak matches — what you lose is the operating point measured for this corpus |
| `CPERSONA_DEGRADED_ADVISORY` | `true` | Attach an `advisory` to recall responses while embeddings are unavailable ([runbook](operations.md#detecting-a-dead-embedding-server)) |
| `CPERSONA_UPDATE_CHECK` | `true` | Check pypi.org once per process start for a newer — or withdrawn — release of this server, and report it through `recall` / `check_health` / `check_update` ([what it sends](architecture.md#transports)). `false` disables the feature entirely: no request, no cache file, no notice. Updating is never automatic either way |
| `CPERSONA_UPDATE_CHECK_INTERVAL_SECONDS` | `86400` | How long that verdict stays usable, cached in `update-check.json` beside the database — a restart inside the window makes no request |
| `CPERSONA_EPISODE_PENALTY_ENABLED` | `false` | Episode boundary penalty ([contract §3](behavior-contracts.md#3-episode-boundary-penalty)) |
| `CPERSONA_EPISODE_DECAY_RATE` | `0.01` | Penalty decay rate per hour before the boundary |
| `CPERSONA_EPISODE_DECAY_FLOOR` | `0.5` | Penalty floor (older memories are at most halved) |
| `CPERSONA_PRIOR_FAR_WEIGHT` | `1.0` | What a vote from the far list is worth, from `0` to `1`, in both fusions ([one prior function](PRIOR_FUNCTION_DESIGN.md#2-the-prior)). Only meaningful when `CPERSONA_VECTOR_REACH` is set above the window; `1` is the unpriced far vote |
| `CPERSONA_PRIOR_AGE_RATE` | `0` | Rate of the age weight `max(floor, 1 / (1 + age_hours × rate))`, which reorders the rows the quality gate admitted and never admits or removes one. `0` turns it off |
| `CPERSONA_PRIOR_AGE_FLOOR` | `0.3` | Floor of the age weight |
| `CPERSONA_PRIOR_AGE_ANCHOR` | `newest` | Where age is measured from: the newest memory in the recall's scope (`newest`, so an idle store ranks as it did when last used) or the current time (`now`) |

The generic aliases `EMBEDDING_MODE` / `EMBEDDING_HTTP_URL` / `EMBEDDING_MODEL`
are also accepted, and the `CPERSONA_`-prefixed form wins when both are set.
The marketplace catalog and the Quick Start use the generic names.

## Reconstruction count

Declare `count` on each `reconstruct` call. It limits **assembled recall items**,
not stored rows or retrieval depth. With no count configuration or call argument,
the ceiling is **1**.

| Variable | Default | Description |
|----------|---------|-------------|
| `CPERSONA_RECONSTRUCT_DEFAULT_COUNT` | `1` | Ceiling used when the caller omits `count` |
| `CPERSONA_RECONSTRUCT_FORCED_COUNT` | *(unset)* | Override the caller's count and the default for every call; still a ceiling, never a fill target |
| `CPERSONA_RECONSTRUCT_MAX_COUNT` | `10` | Absolute ceiling; requests above it are clamped and reported. This is an experimental limit, not an empirically optimal count |
| `CPERSONA_RECONSTRUCT_QUOTE_CHARS` | `800` | An item's head quote: the parts of its record that matched, filled in ranking order up to this many characters and shown in text order — the same filling as the recall excerpt. A record no longer than this is quoted whole. `0` quotes the single governing passage instead, cut at the preview tier, as before 2.6 |
| `CPERSONA_RECONSTRUCT_DEFAULT_BUDGET` | `4000` | Payload budget in characters of quoted text when the caller omits `budget`; one head quote per item of the window when that is more. Provisional until the count and budget sweep chooses it |
| `CPERSONA_RECONSTRUCT_FORCED_BUDGET` | *(unset)* | Override the caller's budget and the default for every call |
| `CPERSONA_RECONSTRUCT_MAX_BUDGET` | `20000` | Absolute budget ceiling; requests above it are clamped and reported. A default or forced value above it, or any configured value below one preview-tier excerpt, stops the server at startup |

Precedence is `forced ?? requested ?? default`, capped by the maximum.
A default or forced value above the maximum is a startup error.
If `count=5` produces only two valid items, return two, with
`requested_count=5`, `effective_count=5`, `returned_count=2` and a
`shortfall_reason`. Do not duplicate items, split a cluster or relax selection
to fill the window. Forcing a count does not change this rule.
Bundling uses deterministic provenance keys; it does not establish semantic
equivalence between arbitrary texts.

## Corpus scale caps

These bound work that grows with the corpus: index maintenance, health repair
and calibration sampling.

Each one is an absolute row count, sized against a corpus of roughly 10,000
rows, where it covered the whole thing. Against a 150,000-row corpus the same
number is a sample. A cap that bites never raises an error — it returns a
smaller answer. Raise these deliberately rather than waiting for a symptom.

| Variable | Default | Description |
|----------|---------|-------------|
| `CPERSONA_VECTOR_INDEX_MAX_EXCLUDED_IDS` | `10000` | Rows the vector index may name as *holes* — rows it could not place in the file (a non-standard `created_at`) plus rows that carried no embedding when the build ran. They are read by id from the live table on every query. Past this many the index declines to build at all, which leaves recall on the (correct, slower) full scan — the state a bulk import produces while its embedding backlog drains. The default covers 6.7% of a 150,000-row corpus; the worst case, every named hole having since gained an embedding, costs roughly 65 ms per query until the next rebuild absorbs them |
| `CPERSONA_REEMBED_ROW_CAP` | `5000` | Rows without an embedding that one `check_health(fix=true)` run re-embeds, and the ceiling on the `repairable` count it reports. Embedding happens before the write lock is taken, so this bounds prefetch wall time and the number of locked `UPDATE`s. Raise it to drain a large backlog in fewer runs: at the previous default of 500, a 50,000-row backlog took 100 runs, and while a backlog exceeds `CPERSONA_VECTOR_INDEX_MAX_EXCLUDED_IDS` the index cannot be built either |
| `CPERSONA_NEAR_DUPLICATE_ROW_CAP` | `5000` | Embedded rows `deep_near_duplicate` compares. The comparison is O(n²) in memory: measured on 1024-dimension vectors, 5,000 rows peak at 266 MB for about 100 ms, and 10,000 rows at 982 MB — which is why the default samples rather than covering a large corpus |
| `CPERSONA_INVALID_SOURCE_CLASSIFY_CAP` | `10000` | Offending `source` rows one `check_invalid_source_type` run classifies. The cost is JSON parsing per row (microseconds), so this can afford to be larger than the caps above. Past the cap the sample is incomplete and the check declines to downgrade its own severity — the cap costs a verdict, not correctness |
| `CPERSONA_NORMALIZATION_SCAN_CAP` | `10000` | Rows `deep_unnormalized_content` reads per run. Unicode normalisation cannot be a SQL predicate — SQLite has no NFC function — so this is the check that must read the text to answer at all, and the cap is what keeps that from meaning "read the corpus". Past it the result says `complete: false` rather than letting a floor be read as a total |
| `CPERSONA_CALIBRATE_MAX_SAMPLE` | `5000` | Hard ceiling on `calibrate_threshold`'s `sample_size`, whatever the caller asks for. It feeds the same O(n²) matrix as the near-duplicate cap and exists to stop an unbounded value from exhausting memory for every agent on the connection, so raise it only as far as the machine can hold (see the measurements above) |

## Remote (HTTP) transport

The default transport is stdio, where the MCP client owns the process and no
network is involved. Set `CPERSONA_TRANSPORT=streamable-http` to serve over
HTTP instead: one server, several clients, reachable over a network.

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
| `CPERSONA_FUTURE_TIMESTAMP_SKEW_SECONDS` | `300` | How far ahead of this server's clock a caller-supplied `timestamp` may be before the write seam calls it wrong |
| `CPERSONA_FUTURE_TIMESTAMP_MODE` | `warn` | What a stamp past that allowance costs: `warn` stores the row and reports it in the log and in the write's own answer, `reject` refuses the write, `off` stores it silently |

**The body budget measures, it does not yet refuse.** Every other cap in this
server — `CPERSONA_MAX_CONTENT_LENGTH` and the rest — is applied by a tool
handler, which runs after the whole body has been received and parsed. Those
caps bound what is stored, and say nothing about what it costs to arrive.

`CPERSONA_HTTP_MAX_BODY_BYTES` is counted where the bytes appear, summed across
the chunks the server actually receives. A body sent in chunks with no
`Content-Length`, and a body whose `Content-Length` understates it, are both
measured by what arrived. The default of 4 MiB is roughly 29x the largest
single `store` this server can accept, and 10x a `recall_with_context` carrying
200 conversation turns, so ordinary traffic is nowhere near it.

The default mode is `warn` on purpose. The request is served in full and the
crossing is logged, at the 1st, 10th and 100th occurrence, so the line neither
floods nor disappears.

Nothing in this project knows what your payloads look like, and a limit that
refuses before anyone has measured is a limit set by guessing. Run with the
default, read the log, and set `CPERSONA_HTTP_BODY_LIMIT_MODE=reject` once you
know the number fits your traffic. Both paths are tested: enabling enforcement
changes a setting, not a code path.

**A context entry states its shape now.** Each entry in
`recall_with_context`'s `external_context` declares five string fields —
`role`, `content`, `name`, `user_id` and `timestamp` — and until 2.5.12 the
schema named only the first two. The other three were read all along, so a
caller working from the schema had no way to know that a `timestamp` was
consulted at all. An entry sent without one merges into the undated group that
sorts ahead of every dated message.

A field that is present but not a string names nothing the field can mean, so
it is read as absent and the entry merges without it. The response then carries
`context_field_issues`, naming the entry's index and the fields, so nothing is
absorbed silently.

Set `CPERSONA_EXTERNAL_CONTEXT_MODE=reject` to refuse such a call instead. The
default stays `warn` because no payload that works today should stop working in
the release that first states the rule. Fields the schema does **not** declare
are still accepted and ignored, so a caller carrying its own bookkeeping
alongside these keeps working.

**A timestamp ahead of the clock is a claim, not a small error.** `store` takes
the `timestamp` a caller supplies, and the confidence curve reads
`max(0, now - timestamp)`. A row stamped in the future is therefore scored as
one written this instant, and it never decays, because tomorrow it is still
ahead.

The larger half is what it does to its neighbours. The corpus span scales the
decay *rate*, so a single row stamped 2099 can widen a three-week corpus to
seventy years and flatten the time axis for every other row in the scope.

The allowance exists because a caller's clock is not this one. A stamp is
generated on another host and arrives after a network hop, so a correct client
can legitimately name a moment a little ahead of the moment the server reads
it. `CPERSONA_FUTURE_TIMESTAMP_SKEW_SECONDS` is what separates that from a
stamp that is simply wrong. Exactly `now + allowance` is accepted; only what is
past it is reported.

The default mode is `warn`, for the same reason the body budget's is. The row
is stored as it always has been, the write's answer carries
`timestamp_ahead_of_clock`, and the log names the setting to change. Set
`CPERSONA_FUTURE_TIMESTAMP_MODE=reject` to refuse such a write instead. Both
paths are tested: enabling enforcement changes a setting, not a code path.

Two things are deliberately outside this setting.

A restore (`import_memories`) reports such rows and imports them anyway, in
every mode. An export must be able to come back exactly as it left, and
refusing here would make the round trip lossy for precisely the rows worth
inspecting.

An unreadable stamp is not this finding's business either: `invalid_timestamp`
owns that row.

For rows already stored, `check_health` reports `future_timestamp`, and
`fix=true` restores each one from its own `created_at` — the insertion time the
row still carries honestly — leaving locked rows untouched.

**Discovery is off until you turn it on.** A client that supports OAuth looks
for RFC 9728 metadata. Finding none, it falls through to asking a human to type
in a client id. That is correct behaviour for a client given nothing to
discover, and it is easily misread as a broken credential.

Setting `CPERSONA_OAUTH_RESOURCE` **and** at least one entry in
`CPERSONA_OAUTH_AUTHORIZATION_SERVERS` publishes the metadata and puts
`resource_metadata` and `scope` on the 401. With either unset, the responses
are byte-identical to a build without the feature, so enabling it is a
deliberate act rather than an upgrade side effect.

**The same two settings accept tokens, and that needs `CPERSONA_ACL_FILE`.** A
token signed by a listed issuer and minted for exactly the configured resource
resolves to the client identifier `oauth:<issuer>:<client_id>`, which is what
you write grants against. A token for any other resource is refused, which is
the check the MCP SDK leaves to the resource server.

Verification requires ACL mode, because a verified identity with no grant table
behind it would reach every tool. With no ACL file the server logs that
verification is staying off and keeps serving discovery, so clients still find
the issuer and are then refused.

Grants are per client. Until someone adds the row, a newly connected client
authenticates and every scoped tool refuses it, saying in `detail` that the
grant table has no entry for it.

**A loopback bind is not a security boundary.** Tunnels (cloudflared, ngrok),
reverse proxies, `kubectl port-forward` and published container ports all
forward to `127.0.0.1`, so binding there says nothing about who can reach the
port. Every tool is exposed to whoever can, including `delete_agent_data` and
the file-reading and file-writing `export_memories` / `import_memories`. Set
`CPERSONA_AUTH_TOKEN` whenever the process is not something only you can talk
to.

Since v2.5.3 the server enforces that. With
`CPERSONA_TRANSPORT=streamable-http` and no `CPERSONA_AUTH_TOKEN`, it refuses
to start.

**If you are upgrading from 2.5.2 or earlier and run the HTTP transport without
a token, it will not start.** Set `CPERSONA_AUTH_TOKEN`, or set
`CPERSONA_ALLOW_UNAUTHENTICATED_HTTP=true` to state that you really do want no
authentication (local development only). Earlier versions allowed an
unauthenticated loopback bind and logged that it was "bound to loopback only",
which read as an all-clear and was not one.

Setting `CPERSONA_ACL_FILE` satisfies the same requirement a different way.
Every request must then resolve to a named client, so the single-token check
does not apply.

In that mode `CPERSONA_AUTH_TOKEN` is **ignored**, with a startup warning.
Credentials come from the ACL file only, and a client that should keep using
the old token must be listed there explicitly. For the grant model, file format
and per-tool classification, see [ACL design](ACL_DESIGN.md).

## Recall fusion mode (`CPERSONA_RECALL_MODE`)

- **`rrf`** (default) — Reciprocal Rank Fusion. Merges the vector and FTS
  channels by rank alone. Robust and scale-free, but it discards score
  magnitude.
- **`rsf`** — Relative Score Fusion. Min-max-normalizes each channel's raw
  score per query (cosine for vector, bm25 for keyword) and sums them, so the
  keyword channel's bm25 magnitude survives the merge. **Recommended for
  topic-drift-prone or space-less language (e.g. Japanese) contexts**, where
  that magnitude is the discriminating signal `rrf` flattens away (≈
  Weaviate's `relativeScoreFusion`; see the ClotoCore
  `RECALL_CONTAMINATION_AB_2026-06-14` report §10–12).

  Note what the normalization costs. It pins each channel's lowest-scoring row
  to 0.0, and a channel that returns a single candidate pins that row to 1.0.
  A fused score therefore places a row among the candidates retrieved with it,
  rather than measuring its similarity to the query.

  Autocut does not act on that pin — it fires only on similarity-scale signals
  ([contract §6](behavior-contracts.md#6-autocut-fires-only-on-similarity-scale-signals))
  — but the quality gate still compares the fused score against a cosine-scale
  threshold. So with `CPERSONA_CONFIDENCE_ENABLED=false`, which is the default
  and what the [CJK guidance](operations.md#japanese-and-cjk-corpora) assumes,
  a strongly matching row can be dropped for being the weakest of a strong set,
  and a weak lone match can pass. Turning confidence on under
  `CPERSONA_CONFIDENCE_ORDERING=legacy` moves the gate onto the confidence
  score and avoids this, at the cost described just below. `rrf` remains the
  default.
- **`cascade`** — sequential channel fill (legacy).

**From 2.6.0a7, `CPERSONA_CONFIDENCE_ENABLED=true` does not change the order
you get back.** The fusion mode decides it, and the confidence value is
returned beside each row. Under `CPERSONA_CONFIDENCE_ORDERING=legacy`, the
behaviour of earlier releases, confidence scoring re-sorts the result set and
the quality gate keys on the confidence score rather than on the fused one.

Measured on a 1,545-document corpus with 394 queries: with confidence on under
that legacy behaviour, `rsf` and `rrf` returned the same rows in the same order
for **all 394** queries; with it off, the two agreed on fewer than 10%.

So under `legacy`, if you set a fusion mode expecting a ranking change, either
leave confidence off, or expect the mode to affect which memories are
considered and not the order they come back in. Under the default `fusion`
ordering the mode decides the order whether confidence is on or off.