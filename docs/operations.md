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
   embeddings and re-embeds them once the server is back. Caveat: when the
   embedding endpoint is unreachable at check time, the dimension check is
   *skipped*, not failed — there is currently no dedicated red check for
   "endpoint unreachable" itself, so do not read a green `check_health` as
   proof the embedding server is up; the `advisory` surface is what watches
   liveness.

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

## Maintenance cadence

- **Monthly**: `check_health(agent_id, fix=true)` — deterministic integrity
  checks + auto-repair; `deep_check(agent_id, fix=true)` for the semantic
  pass; a logical `export_memories` backup.
- **After corpus upheaval** (bulk import, rebuild, model change):
  `calibrate_threshold(agent_id)`.
- **Version upgrades**: the schema migrates itself forward; calibration is
  fingerprinted to the scoring function and embedding dimension, and the
  server recalibrates at first boot when either changes (v2.5.2+).
