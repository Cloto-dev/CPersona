# Operations Runbook

> **Applies to: CPersona 2.5.x.** This page is the canonical operations
> reference: backup, degradation detection, recall tuning, CJK guidance, and
> corpus indexing patterns. The behavior facts it relies on are contracts —
> see [Behavior Contracts](behavior-contracts.md).

---

## Backup and restore

The database is a single SQLite file (`CPERSONA_DB_PATH`) running in WAL
mode. **A plain `cp` of a live WAL database can straddle a checkpoint and
produce a corrupt copy** — do not script one.

Recommended, in order:

1. **Online physical backup** (first choice — safe while the server runs):

   ```bash
   sqlite3 "$CPERSONA_DB_PATH" ".backup 'cpersona-backup.db'"
   # or
   sqlite3 "$CPERSONA_DB_PATH" "VACUUM INTO 'cpersona-backup.db'"
   ```

   Both produce a consistent snapshot under concurrent writes.

2. **Offline copy**: stop the server, then copy the `.db` **together with its
   `-wal` and `-shm` siblings**.

3. **Logical backup** (recommended as a low-frequency complement):
   `export_memories` writes JSONL that is independent of the schema version,
   and `import_memories` is idempotent (duplicates are skipped) — so restore
   drills are safe to rehearse. Monthly is a reasonable cadence.

**The `.db` is not the whole instance.** State lives outside it that none of
the backup forms above touch:

- `<CPERSONA_DB_PATH>.calibration.json` — per-agent vector thresholds, gate
  state, scoring version, and the per-agent `set_recall_precision` betas.
  Restore without it and the measurements repair themselves: with the default
  `CPERSONA_CALIBRATE_ON_MODEL_CHANGE=true`, a startup with no sidecar
  recalibrates rather than falling back, and `deep_check` reports
  `never_calibrated` if it did not. **The betas do not.** They are policy
  inputs, not measurements — nothing re-derives a preference you stated — so
  an operator who tuned precision loses that tuning silently and the
  recalibration measures every gate at the default beta. Copy the sidecar
  beside the database, or re-apply `set_recall_precision` after a restore.
- `~/.cpersona/operating-context.toml` (or `CPERSONA_OPERATING_CONTEXT_PATH`)
  — the operator instructions served to every client.
- The ACL file, if `CPERSONA_ACL_FILE` is set — restoring without it changes
  who may connect.
- **The external vector index**, under `CPERSONA_VECTOR_SEARCH_MODE=remote`
  with `CPERSONA_STORE_BLOB=false`. In that configuration the embeddings exist
  only in the remote index, so restoring the database and all three files
  above still comes back with no vector arm. `check_health` reports it as
  `no_local_vector_fallback`; back the index up with the same schedule, or
  keep `STORE_BLOB=true` so the `.db` stays self-sufficient.
- **Not** `<CPERSONA_DB_PATH>.memories.vecindex` or `.episodes.vecindex`, the
  contiguous vector indexes. They are derived from the database and are rebuilt, never restored: a
  backup that includes it wastes space, and a restore that brings back a stale
  one is corrected by the next build. See [the contiguous vector index](#the-contiguous-vector-index).

Keep the live database **outside cloud-sync folders** (Dropbox, Drive, etc.):
sync clients interact badly with SQLite WAL files. Sync the *backups* instead.

## Detecting a dead embedding server

Vector search is the strongest retrieval layer, and it degrades silently at
the network boundary if nothing watches for it. Three detection surfaces
exist, and the calling agent should be instructed to watch the first:

1. **`advisory` on recall responses** (v2.4.33+, the primary surface). A
   state machine observes real embedding failures, and degraded recalls carry
   `advisory = {degraded, severity, reason, evidence, runbook, advisory_scope}`
   — "the vector layer is down; serving FTS + keyword only". Instruct your agent
   to surface this field to the user and follow its `runbook` (usually: start or
   repoint the embedding server, then recall again). A shortened runbook means
   "you were already told"; `advisory_scope` says who "you" was — `session` when
   the process is yours alone, `process` on the HTTP transport, where the state
   is the whole server's. On a shared server a fault repeats its full runbook
   rather than assume your session saw it (bug-251). `CPERSONA_DEGRADED_ADVISORY=false`
   silences the advisory: set it to record that the operator accepts running without an
   embedding backend, which is a supported fallback and not a recommended configuration.
   Design: [DEGRADED_ADVISORY_DESIGN](DEGRADED_ADVISORY_DESIGN.md).
2. **`embedded` on store responses.** Every write reports whether its
   embedding was persisted. Writes made while the server is down come back
   `embedded: false` — those rows are repairable (next item).
3. **`check_health(agent_id, fix=true)`** detects rows stored with NULL
   embeddings and re-embeds them once the server is back. The dimension check
   still *skips* rather than fails when the endpoint is unreachable, but the
   endpoint itself is now watched: `embedding_backend` reports `warn` with the
   failing call's own evidence when a configured backend does not answer.
4. **Read `not_probed` for what it is.** `check_health(fix=false)` makes no
   network call, so it cannot test liveness. It says so — `embedding_backend`
   returns `not_probed` — rather than leaving you to infer health from silence.
   A `fix=true` run returns it too when the probe was answered from the
   embedding client's five-minute embed cache: the dimension comes back without
   a request reaching the endpoint, so nothing was learned about it either way.
   The finding's `reason` says which of the two happened. A fault a recall
   already latched is still reported there. With no backend configured this
   check is quiet by design: that is a supported configuration, and the
   `advisory` surface is where it is raised.

## Tuning recall

The knobs, in the order you should reach for them:

1. **`set_recall_precision(agent_id, precision)`** — the main (and under the
   default fusion modes, effectively the only) policy knob. It moves the
   operating point β of the fused quality gate: `strict=2.0` (fewer
   contaminants, more misses), `balanced=1.0` (default), `lenient=0.5` (fewer
   misses, more contaminants). Takes effect immediately; no restart.
2. **`calibrate_threshold(agent_id)`** — re-derives the gate positions from
   the corpus (no labels needed). Called **with** an `agent_id` (and the
   fused gate enabled), it calibrates **both** the vector threshold and the
   fused gate; called without one, it calibrates the vector threshold only.
   The value it prints is the vector-side threshold — under fusion modes the
   component actually cutting results is the fused gate. Re-run it after the
   corpus changes substantially, after a bulk (re)build, and after changing
   the embedding model.
3. **`CPERSONA_AUTOCUT_MIN_RESULTS`** — autocut fires on similarity-scale
   signals: under confidence scoring, or on a homogeneous raw-cosine list
   (which is what `cascade` produces, confidence on or off). It is
   deliberately inert under `rsf`/`rrf`
   ([contract §6](behavior-contracts.md#6-autocut-fires-only-on-similarity-scale-signals)),
   so under the default configuration this knob does nothing — but it is the
   fusion mode that decides that, not the confidence flag.
4. **`CPERSONA_FUSED_GATE_ENABLED=false`** — last resort. Without the gate,
   filtering falls back to the pool-size heuristic (`_adaptive_min_score`),
   which still rejects weak matches — it is a coarser gate, not an open door.
   What you give up is the operating point measured for *this* corpus.

**Choosing a precision**: the trade is asymmetric per use case. For
index-style corpora (recall feeds an AI that can discard irrelevant rows), a
**miss is worse than a contaminant** — run `lenient` for a few days and step
back to `balanced` only if contamination becomes a real cost. For
contamination-sensitive contexts (recall injected into a small window),
prefer `balanced`/`strict`.

### When not to rely on recall

Probabilistic retrieval can lose. Facts whose *absence from context causes
harm* — the current top-priority decision, standing safety rules — belong in
a **deterministically injected** surface (`CLAUDE.md`, system prompt, an
index file your agent always loads), not primarily in memory. A useful
split: *what should fire without being asked goes in deterministic
injection; what should be findable when asked goes in memory.* Two
corollaries:

- **Update decisions by overwrite, not append.** When a decision changes,
  `update_memory` the old row (it re-embeds automatically). The most reliable
  way to stop a stale decision from winning recall is for it not to exist in
  the search space.
- `lock_memory` protects against loss, not against losing a ranking
  ([contract §9](behavior-contracts.md#9-lock_memory-protects-it-does-not-boost)).

## Japanese and CJK corpora

- Set **`CPERSONA_RECALL_MODE=rsf`**. FTS5 tokenizes CJK poorly; rsf keeps
  the keyword channel's score magnitude in the merge, which is the signal
  that compensates. No further CJK-specific settings exist.
- Known characteristics of `jina-v5-nano` — the model this project runs, and
  the one the reference embedding server downloads by default — on
  Japanese, confirmed by long-term production use: **strong when at least
  one proper-noun / identifier anchor overlaps** between query and memory;
  **weak on pure concept matches** with no shared vocabulary. Phrasing
  queries with a concrete anchor term ("keyword anchoring") is a correct
  adaptation to the current model, not a workaround to feel bad about.
- The model slot is replaceable (`CEmbedding` provider). If you swap the
  embedding model: full re-embed of the corpus, then `calibrate_threshold`.
  The server auto-recalibrates on a *dimension* change, but a same-dimension
  model swap needs the manual recalibration.

## Corpus indexing and sync patterns

CPersona's primary design center is memory that accrues from conversation.
Using it as a **search index over canonical Markdown files** works, but two
facts shape the correct pattern: CPersona is a **passive server** (no file
watching — ingestion is always caller-driven), and dedup is **skip, not
upsert** ([contract §5](behavior-contracts.md#5-dedup-semantics-skip-not-upsert)).

**Pattern A — rebuild the index as a disposable projection (recommended
first).**

1. Give the index a **dedicated `agent_id`** (e.g. `md-index`). Do not mix
   index chunks into a conversational agent's memory — it also keeps the
   episode boundary machinery out of the picture.
2. When the source documents change: `delete_agent_data(agent_id="md-index")`
   → re-`store` every chunk.
3. **Re-run `calibrate_threshold` after each rebuild** (and re-apply
   `set_recall_precision` if you use it): `delete_agent_data` discards that
   agent's calibration state along with its rows. If the rebuild is a nightly
   batch, the recalibration is part of the batch.
4. Cost: re-embedding a few thousand chunks on CPU is minutes to tens of
   minutes (environment-dependent). What you buy: no diff logic, and an index
   that matches the source once the rebuild finishes. It does **not** match
   during one: `delete_agent_data` commits before the re-`store` begins, so a
   recall issued inside that window sees a partial index or none at all. Run
   rebuilds when nothing is querying, or build under a second `agent_id` and
   switch readers over when it is complete.

**Pattern B — differential updates with an external content-hash ledger.**

1. `store` each chunk with a stable `msg_id` (e.g. `path#heading`) and the
   source document in `source.id` — the `source_id` recall argument
   (prefix-match) then doubles as a per-document filter.
2. Keep a caller-side ledger of `key → content hash`; process only chunks
   whose hash changed.
3. **Changed chunks must go through `update_memory` (or `delete_memory` +
   `store`)** — re-storing changed content under the same `msg_id` is
   silently skipped. Unchanged chunks may be re-submitted blindly; exact-match
   dedup guarantees that is harmless.

Prefer A until the corpus is large enough that rebuild time actually hurts:
it has no ledger to corrupt and no drift mode.

## Scale

Growth needs no archival or thinning routine. The vector layer scans a
recency window (`CPERSONA_MAX_MEMORIES`, default 10,000 — raise it via env
for large corpora; [contract §4](behavior-contracts.md#4-the-vector-scan-window-cpersona_max_memories)),
FTS and keyword channels reach the full history, and old rows sink through
windows and decay rather than being deleted.

## The contiguous vector index

The local vector scan reads every embedding in its window out of SQLite one
row at a time, and that read — not the arithmetic — is where the scan's time
goes. The contiguous index is a file beside the database that holds the same
embeddings in the layout the arithmetic wants, so the scan reads them in one
pass. Same rows, same scores, same order; only the latency changes. On the
reference machine at 100,000 rows the vector arm went from 604 ms to 77 ms.

There is one index per table: memories and episodes are scanned separately,
so each has its own file, and each is built and checked on its own. An
unindexed episode table costs more per query than an indexed memory table
five times its size, so a deployment that builds one should build both.

**It is a derived artifact.** The database is the only source of truth; the
index is a projection of it and is treated the way a cache is treated: not
backed up, never repaired, safe to delete at any time. If anything about it
looks wrong, delete the file and build again.

**Building it** is an operator action. Nothing in the running server builds
or refreshes the index; the entry point is a command, made for a cron job or
a systemd timer:

```bash
python -m cpersona.vector_index --db "$CPERSONA_DB_PATH" build
python -m cpersona.vector_index --db "$CPERSONA_DB_PATH" --table episodes build
python -m cpersona.vector_index --db "$CPERSONA_DB_PATH" status
python -m cpersona.vector_index --db "$CPERSONA_DB_PATH" --table episodes status
```

`--table` selects the file (`memories` is the default). `build` reads the database (it never writes to it) and replaces the index
atomically; the server picks the new file up on its next query, with no
restart. It exits 0 when built and 1 when it declined, printing why — a
corpus with no embedded rows, or one that carries two embedding widths at
once (the transient state of a model change; build again when the
re-embedding has finished). `status` reports whether an index is present and
usable, how many rows it holds, and how many rows have been written since it
was built; it exits 1 when there is no index and 2 when the file exists but
cannot be used. Add `--json` to either for a machine-readable line.

**What happens between builds.** The index knows the highest row id that
existed when it was built. Rows written after that are not in it, and are not
lost: every query reads them from the database exactly, as the scan always
did, and merges them with the indexed rows. So a late rebuild never changes an
answer. It only grows the part of each query that is still read row by row,
until latency drifts back toward the unindexed figure. Rebuild frequency is a
performance setting, not a correctness one: nightly is a reasonable default,
hourly for a corpus that grows fast, and `status` shows how far behind the
index is at any moment.

**When the index is not used.** The scan the index replaces is still there
and is the fallback. Recall falls back to it — slower, and still correct —
when the file is missing, when it fails its own integrity check, when the
query vector's width does not match the index, and when a row the index holds
has since lost its embedding under a maintenance repair. Each of these is
reported where an operator reads health: `check_health` raises
`vector_index_absent` (with the count of rows it would cover) when there is no
index and `vector_index_unusable` when the file cannot be trusted — one line
per table, each naming its `table` — and the
server logs a warning on every query it had to hand back to the scan. An
index that has quietly been unusable for a week would otherwise read as
"somehow not faster", which is the failure the reporting exists to prevent.

There is no configuration for the index. Its path is derived from
`CPERSONA_DB_PATH`, and the scan window it serves is the same
`CPERSONA_MAX_MEMORIES` the scan uses.

## Maintenance cadence

- **Monthly**: `check_health(agent_id, fix=true)` — deterministic integrity
  checks + auto-repair; `deep_check(agent_id, fix=true)` for the semantic
  pass; a logical `export_memories` backup.
- **After corpus upheaval** (bulk import, rebuild, model change):
  `calibrate_threshold(agent_id)`.
- **On a timer, if the vector index is in use**: `python -m cpersona.vector_index
  build`, and the same with `--table episodes` — nightly by default; see [the contiguous vector index](#the-contiguous-vector-index)
  for what a late rebuild costs (latency, never correctness).
- **Version upgrades**: the schema migrates itself forward; calibration is
  fingerprinted to the scoring function and embedding dimension, and the
  server recalibrates at first boot when either changes (v2.5.2+).
