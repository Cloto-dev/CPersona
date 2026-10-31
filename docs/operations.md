# Operations Runbook

> **Applies to: CPersona {{ version_line }}.** This page is the canonical operations
> reference: backup, degradation detection, recall tuning, CJK guidance, and
> corpus indexing patterns. The behaviour facts it relies on are contracts —
> see [Behavior Contracts](behavior-contracts.md).

---

## Backup and restore

The database is a single SQLite file (`CPERSONA_DB_PATH`) running in WAL mode.
**A plain `cp` of a live WAL database can straddle a checkpoint and produce a
corrupt copy**, so do not script one.

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
   and `import_memories` is idempotent (memories and episodes already present
   are skipped). A profile is the one record an import replaces rather than
   skips: the file's copy replaces the stored one unless the stored one is
   newer, in which case the stored profile is kept and the line is reported
   in `errors`. A drill with an older export therefore does not move a
   profile backwards. Monthly is a reasonable cadence. The file carries the
   declared associations (entities, aliases, mentions and relations) as well
   as the records, and a restore points them at the records it restores. A
   file written by an earlier version has no associations in it and still
   imports as it always did. A file written by 2.4.37 or earlier also has no
   project or channel axis: its rows are restored into the global pool, where
   every project reads them, and the import says how many in `errors`. An
   import refuses a file larger than
   `CPERSONA_MAX_IMPORT_BYTES` (100 MiB by default), and an export has no such
   cap: compare the size of each backup file with it, and raise it
   ([configuration](configuration.md#export-and-import)) before a restore needs
   it.

**The `.db` is not the whole instance.** State lives outside it that none of
the backup forms above touch:

- `<CPERSONA_DB_PATH>.calibration.json` — per-agent vector thresholds, gate
  state, scoring version, and the per-agent `set_recall_precision` betas.

    Restore without it and the measurements repair themselves. With the default
    `CPERSONA_CALIBRATE_ON_MODEL_CHANGE=true`, a startup with no sidecar
    recalibrates rather than falling back, and `deep_check` reports
    `never_calibrated` if it did not.

    **The betas do not repair themselves.** They are policy inputs, not
    measurements, and nothing re-derives a preference you stated. An operator
    who tuned precision loses that tuning without being told, and the
    recalibration measures every gate at the default beta. Copy the sidecar
    beside the database, or re-apply `set_recall_precision` after a restore.
- `~/.cpersona/operating-context.toml` (or `CPERSONA_OPERATING_CONTEXT_PATH`)
  — the operator instructions served to every client.
- The ACL file, if `CPERSONA_ACL_FILE` is set. Restoring without it changes
  who may connect.
- **The alias ledger**, if any ACL row sets `per_subject`:
  `alias_ledger.json` beside the database, or the file
  `CPERSONA_ALIAS_LEDGER_FILE` names. The server writes it when a subject
  first signs in, and it is the only record of which `u-` agent belongs to
  which person. Restore the database without it and the server refuses to
  start, naming those agents. Restore the ledger to that path, then start the
  server again. Do not rename the agents or remove `per_subject` instead:
  either one leaves every signed-in subject with a fresh alias and none of the
  memory it stored.
- **The external vector index**, under `CPERSONA_VECTOR_SEARCH_MODE=remote`
  with `CPERSONA_STORE_BLOB=false`. In that configuration the embeddings exist
  only in the remote index, so restoring the database and every file above
  still comes back with no vector arm. `check_health` reports it as
  `no_local_vector_fallback`. Back the index up on the same schedule, or keep
  `STORE_BLOB=true` so the `.db` stays self-sufficient.
- **Not** `<CPERSONA_DB_PATH>.memories.vecindex` or `.episodes.vecindex`, the
  contiguous vector indexes. They are derived from the database, and are
  rebuilt rather than restored. A backup that includes one wastes space, and a
  restore that brings back a stale one is corrected by the next build. See
  [the contiguous vector index](#the-contiguous-vector-index).

Keep the live database **outside cloud-sync folders** (Dropbox, Drive, and so
on). Sync clients interact badly with SQLite WAL files. Sync the *backups*
instead.

## Detecting a dead embedding server

Vector search is the strongest retrieval layer, and it degrades without
announcing itself at the network boundary. Three detection surfaces exist, and
the calling agent should be instructed to watch the first.

1. **`advisory` on recall responses** (v2.4.33+, the primary surface). A state
   machine observes real embedding failures, and degraded recalls carry
   `advisory = {degraded, severity, reason, evidence, runbook, advisory_scope}`
   — "the vector layer is down; serving FTS + keyword only".

    Instruct your agent to surface this field to the user and follow its
    `runbook` (usually: start or repoint the embedding server, then recall
    again). A shortened runbook means "you were already told", and
    `advisory_scope` says who "you" was: `session` when the process is yours
    alone, `process` on the HTTP transport, where the state is the whole
    server's. On a shared server a fault repeats its full runbook rather than
    assume your session saw it (bug-251).

    `CPERSONA_DEGRADED_ADVISORY=false` silences the advisory. Set it to record
    that the operator accepts running without an embedding backend, which is a
    supported fallback and not a recommended configuration. Design:
    [DEGRADED_ADVISORY_DESIGN](DEGRADED_ADVISORY_DESIGN.md).
2. **`embedded` on store responses.** Every write reports whether its
   embedding was persisted. Writes made while the server is down come back
   `embedded: false`, and those rows are repairable (next item).
3. **`check_health(agent_id, fix=true)`** detects rows stored with NULL
   embeddings and re-embeds them once the server is back. The dimension check
   still *skips* rather than fails when the endpoint is unreachable, but the
   endpoint itself is now watched: `embedding_backend` reports `warn`, with
   the failing call's own evidence, when a configured backend does not answer.
   When the run's own probe reaches the backend and gets a vector back, that
   answer outranks a fault latched earlier: a restarted backend reads as
   connected, and its NULL rows are re-embedded on the same run.
4. **Read `not_probed` for what it is.** `check_health(fix=false)` does not
   judge liveness. It can still contact the backend — the dimension check
   embeds a probe text, the model check asks the backend for its identity,
   and the node scan asks for token counts — but it reads none of those
   calls as liveness evidence. It says so — `embedding_backend` returns
   `not_probed` — rather than leaving you to infer health from silence.

    A `fix=true` run returns it too when the probe was answered from the
    embedding client's five-minute embed cache: the dimension comes back
    without a request reaching the endpoint, so nothing was learned about it
    either way. The finding's `reason` says which of the two happened. A fault
    a recall already latched is still reported there.

    With no backend configured this check is quiet by design. That is a
    supported configuration, and the `advisory` surface is where it is
    raised.

## Tuning recall

The knobs, in the order you should reach for them:

1. **`set_recall_precision(agent_id, precision)`** — the main policy knob, and
   under the default fusion modes effectively the only one. It moves the
   operating point β of the fused quality gate: `strict=2.0` (fewer
   contaminants, more misses), `balanced=1.0` (default), `lenient=0.5` (fewer
   misses, more contaminants). It takes effect immediately, with no restart.
2. **`calibrate_threshold(agent_id)`** — re-derives the gate positions from
   the corpus, with no labels needed. Called **with** an `agent_id` (and the
   fused gate enabled), it calibrates **both** the vector threshold and the
   fused gate; called without one, it calibrates the vector threshold only.
   The value it prints is the vector-side threshold, while under fusion modes
   the component actually cutting results is the fused gate. Re-run it after
   the corpus changes substantially, after a bulk rebuild, and after changing
   the embedding model.
3. **`CPERSONA_AUTOCUT_MIN_RESULTS`** — autocut fires on similarity-scale
   signals: under confidence scoring, or on a homogeneous raw-cosine list,
   which is what `cascade` produces with confidence on or off. It is
   deliberately inert under `rsf` and `rrf`
   ([contract §6](behavior-contracts.md#6-autocut-fires-only-on-similarity-scale-signals)),
   so under the default configuration this knob does nothing. Note that it is
   the fusion mode that decides that, not the confidence flag.
4. **`CPERSONA_FUSED_GATE_ENABLED=false`** — last resort. Without the gate,
   filtering falls back to the pool-size heuristic (`_adaptive_min_score`),
   which still rejects weak matches: it is a coarser gate, not an open door.
   What you give up is the operating point measured for *this* corpus.

**Choosing a precision.** The trade is asymmetric per use case. For
index-style corpora, where recall feeds an AI that can discard irrelevant
rows, a **miss is worse than a contaminant**: run `lenient` for a few days and
step back to `balanced` only if contamination becomes a real cost. For
contamination-sensitive contexts, where recall is injected into a small
window, prefer `balanced` or `strict`.

### When not to rely on recall

Probabilistic retrieval can lose. Facts whose *absence from context causes
harm* — the current top-priority decision, standing safety rules — belong in a
**deterministically injected** surface: `CLAUDE.md`, a system prompt, or an
index file your agent always loads. They do not belong primarily in memory.

A useful split: *what should fire without being asked goes in deterministic
injection; what should be findable when asked goes in memory.* Two corollaries
follow.

- **Update decisions by overwrite, not append.** When a decision changes,
  `update_memory` the old row (it re-embeds automatically). The most reliable
  way to stop a stale decision from winning recall is for it not to exist in
  the search space.
- `lock_memory` protects against loss, not against losing a ranking
  ([contract §9](behavior-contracts.md#9-lock_memory-protects-it-does-not-boost)).

### Prompting recall from Claude Code hooks

An agent does not always look in memory before it answers, and an instruction
in an always-loaded file is read once and then competes with everything else. A
hook fires at the moment itself. The agent plugin
([getting started](getting-started.md#let-the-agent-do-it-claude-code)) installs
three, for Claude Code and Codex alike: at the start of a session, on every
prompt, and after the conversation is compacted. The per-prompt one adds a note
telling the agent to call `reconstruct` for the request, with the prompt as a
suggested query, and to confirm "there is no such record" with an exact search
rather than a score
([contract §12](behavior-contracts.md#12-scores-order-one-response-they-do-not-say-whether-an-answer-exists)).
Their source is
[`plugin/scripts/cpersona_hooks.py`](https://github.com/Cloto-dev/CPersona/blob/master/plugin/scripts/cpersona_hooks.py),
standard library only, if you would rather register one by hand.

Two mistakes fail silently on Windows. A production user made both and got no
recall from a hand-registered hook for five days, and the plugin's hooks are
written against both:

- **Read and write UTF-8 yourself.** Python's standard streams use the console
  code page on Windows (cp932 on Japanese Windows), while Claude Code sends and
  reads UTF-8, so a hook that reads `sys.stdin` as text fails on a Japanese
  prompt and adds nothing. The hooks read and write bytes and decode UTF-8
  themselves.
- **Give the script an absolute path.** A relative path stops resolving when the
  session's working directory is a subfolder. The plugin names its script
  through `${CLAUDE_PLUGIN_ROOT}`; a hook registered by hand can use
  `$CLAUDE_PROJECT_DIR`, which expands to the project root.

## Japanese and CJK corpora

- Set **`CPERSONA_RECALL_MODE=rsf`**. FTS5 tokenizes CJK poorly, and rsf keeps
  the keyword channel's score magnitude in the merge, which is the signal that
  compensates. No further CJK-specific settings exist.
- Known characteristics of `jina-v5-nano` — the model this project runs, and
  the one the reference embedding server downloads by default — on Japanese,
  confirmed by long-term production use: **strong when at least one
  proper-noun or identifier anchor overlaps** between query and memory;
  **weak on pure concept matches** with no shared vocabulary. Phrasing queries
  with a concrete anchor term ("keyword anchoring") is a correct adaptation to
  the current model, not a workaround to feel bad about.
- The model slot is replaceable (`CEmbedding` provider). If you swap the
  embedding model, re-embed the corpus in full and then run
  `calibrate_threshold`. The server auto-recalibrates on a *dimension* change,
  but a same-dimension model swap needs the manual recalibration.

## Corpus indexing and sync patterns

CPersona's primary design center is memory that accrues from conversation.
Using it as a **search index over canonical Markdown files** works, but two
facts shape the correct pattern. CPersona is a **passive server** — it watches
no files, and ingestion is always caller-driven — and dedup is **skip, not
upsert** ([contract §5](behavior-contracts.md#5-dedup-semantics-skip-not-upsert)).

**Pattern A — rebuild the index as a disposable projection (recommended
first).**

1. Give the index a **dedicated `agent_id`** (for example `md-index`). Do not
   mix index chunks into a conversational agent's memory. A separate agent
   also keeps the episode boundary machinery out of the picture.
2. When the source documents change: `delete_agent_data(agent_id="md-index")`,
   then re-`store` every chunk.
3. **Re-run `calibrate_threshold` after each rebuild**, and re-apply
   `set_recall_precision` if you use it. `delete_agent_data` discards that
   agent's calibration state along with its rows. If the rebuild is a nightly
   batch, make the recalibration part of the batch.
4. Cost: re-embedding a few thousand chunks on CPU takes minutes to tens of
   minutes, depending on the environment. What you buy is no diff logic, and
   an index that matches the source once the rebuild finishes.

    It does **not** match during one. `delete_agent_data` commits before the
    re-`store` begins, so a recall issued inside that window sees a partial
    index or none at all. Run rebuilds when nothing is querying, or build
    under a second `agent_id` and switch readers over when it is complete.

**Pattern B — differential updates with an external content-hash ledger.**

1. `store` each chunk with a stable `msg_id` (for example `path#heading`) and
   the source document in `source.id`. The `source_id` recall argument then
   doubles as a per-document filter: a value that ends in a letter or digit
   (`docs/a.md`) matches that one document exactly, and a value that ends in a
   delimiter (`docs/`) matches every document under it.
2. Keep a caller-side ledger of `key → content hash`, and process only chunks
   whose hash changed.
3. **Changed chunks must go through `update_memory`**, or `delete_memory` plus
   `store`. Re-storing changed content under the same `msg_id` is skipped
   without a word. Unchanged chunks may be re-submitted blindly, because
   exact-match dedup guarantees that is harmless.

Prefer A until the corpus is large enough that rebuild time actually hurts. It
has no ledger to corrupt and no drift mode.

## Scale

Growth needs no archival or thinning routine. The vector layer scans a recency
window (`CPERSONA_MAX_MEMORIES`, default 10,000). The FTS and keyword channels
reach the full history, and old rows sink through windows and decay rather than
being deleted.

To let the vector arm reach rows past the window, set `CPERSONA_VECTOR_REACH`
above it or turn on the far seats (`CPERSONA_FAR_SEATS_ENABLED`). Widening the
window itself also removes the recency prior it provides: measured, it cost 20
NDCG@10 points on recent answers to gain 5 on old ones
([contract §4](behavior-contracts.md#4-the-vector-scan-window-cpersona_max_memories)).

## The contiguous vector index

The local vector scan reads every embedding in its window out of SQLite one
row at a time, and that read — not the arithmetic — is where the scan's time
goes. The contiguous index is a file beside the database holding the same
embeddings in the layout the arithmetic wants, so the scan reads them in one
pass. Same rows, same scores, same order: only the latency changes. On the
reference machine at 100,000 rows, the vector arm went from 604 ms to 77 ms.

There is one index per table. Memories and episodes are scanned separately, so
each has its own file, and each is built and checked on its own. An unindexed
episode table costs more per query than an indexed memory table five times its
size, so a deployment that builds one should build both.

**It is a derived artifact.** The database is the only source of truth. The
index is a projection of it and is treated the way a cache is treated: not
backed up, never repaired, safe to delete at any time. If anything about it
looks wrong, delete the file and build again.

**It is read through a change log.** A row the file holds can change after the
build: `update_memory` replaces its text and vector, the missing-embedding repair
fills its vector, a re-tag moves it, a repair rewrites a time, or it is deleted.
Triggers log the id of every such row, and a query reads those rows from the
database instead of the file, so the answer is the one the scan gives. The log
runs only while an index file exists: `build` turns it on, and the server turns
it off at startup when no file is left. An index file written before the log
existed is refused as unusable; build it again after upgrading.

`delete_agent_data` removes it along with the rows it held, but only when the
caller's grant covers every agent (always, with no ACL): the file holds every
agent's rows, so a grant on one agent does not reach it. After a purge under
a grant on one agent the file stays until its next build, and the purged rows are not
served from it in the meantime, because recall reads every candidate back from
the database. A build the server was running when a purge removed the file
discards its output rather than put the purged rows back. The `build` command
runs in a process of its own and is not ordered against a purge, so run it
again if a purge happened while it ran.

**Building it** is an operator action. Nothing in the running server builds or
refreshes the index. The entry point is a command, made for a cron job or a
systemd timer:

```bash
python -m cpersona.vector_index --db "$CPERSONA_DB_PATH" build
python -m cpersona.vector_index --db "$CPERSONA_DB_PATH" --table episodes build
python -m cpersona.vector_index --db "$CPERSONA_DB_PATH" status
python -m cpersona.vector_index --db "$CPERSONA_DB_PATH" --table episodes status
```

`--table` selects the file (`memories` is the default).

`build` reads the rows without writing them and replaces the index
atomically. Its one write is the change log's switch: it turns the log on before
it reads, and afterwards drops the log rows every index file present already
holds. The server picks the new file up on its next query, with no
restart. It exits 0 when built, and 1 when it declined, printing why: a corpus
with no embedded rows, one that carries two embedding widths at once (the
transient state of a model change; build again when the re-embedding has
finished), or a database no server has migrated yet (start the server once).

`status` reports whether an index is present and usable, how many rows it
holds, and how many rows have been written and changed since it was built. It also reports
how many rows every query reads from the database instead of the index
(`rows_read_exactly`). That count includes the rows written or changed since the build,
and the rows the build named because it could not hold them: rows whose
`created_at` is not in the standard form (`excluded`), and rows with no
embedding yet (`unembedded`). A named row costs a read only once it has an
embedding, and filling missing embeddings is what `check_health` with
`fix=true` does. So after that repair, `rows_read_exactly` can grow while
`rows_since_build` stays at 0. It exits 1 when there is no index, and 2 when
the file exists but cannot be used, which includes a change log that can no
longer vouch for it. Add `--json` to either for a
machine-readable line.

**For memories, `build` also writes the coarse index**, a second derived file
beside the first (`<database>.memories.coarseindex`). It holds the same rows,
each reduced to one bit per dimension, and it is read only by the reach
described in [Binary coarse search](BINARY_COARSE_SEARCH_DESIGN.md): the far
seats, when turned on, and a time cue's search of its period past the scan
window, which since 2.6.4 uses the file by default when it exists. It is
handled like the contiguous index: never backed up, never repaired, safe to
delete, and removed by a purge under the same rule. With both files,
`build` exits 0 only when both were built. `status` reports the coarse index
under `coarse` without changing its own exit code, because nothing reads the
coarse index unless a setting asks for it.

**When a setting does read it, health reports it.** With
`CPERSONA_FAR_SEATS_ENABLED=true` or `CPERSONA_CUE_COARSE_ENABLED=true`, and an
agent holding records past the scan window, a recall that cannot use the coarse
index reads the stored vector of every one of those records from the database:
the answer is the same, and the cost grows with the store instead of the window.
`check_health` raises `coarse_index_absent` (with the records past the window and
the bytes a recall reads), `coarse_index_unusable`, `coarse_index_dimension_drift`
or `coarse_index_log_refused` as warnings, and `coarse_index_tail_grown` as an
observation, and says nothing while both settings are off or the store fits the
window.

With `CPERSONA_CUE_COARSE_ENABLED` unset, its default, a time cue asks the file
only: without a usable one the part of a period past the window is not
searched, and the recall says so in `time_cue.remainder`. While the far seats
are off, no recall then pays for a missing file, so the same four states are
reported as observations (info), not warnings. A recall that searches vectors locally, whose scope holds 1,000 or more
records with a stored vector past the window and no file, also carries a
`suggestion`, once per session, for the agent to relay to the user. Building the file is never automatic. `check_health(checks=["coarse_index"], fix=true)` builds the file again, from
the first record past the window;
it holds every agent's records, so the repair asks for write access to every
agent. The findings reach `get_session_findings` like every other check's, so a
monitor can hear of the cost before anyone notices it as latency. Whether a
finding leads to the repair is the monitor's decision, not the server's.

**What happens between builds.** The index knows the highest row id that
existed when it was built. Rows written after that are not in it, and are not
lost: every query reads them from the database exactly as the scan always did,
and merges them with the indexed rows. Rows it holds that changed since are read
the same way, through the change log.

So a late rebuild never changes an answer. It only grows the part of each query
that is still read row by row, until latency drifts back toward the unindexed
figure. Rebuild frequency is a performance setting, not a correctness one.
Nightly is a reasonable default, hourly for a corpus that grows fast, and
`status` shows how far behind the index is at any moment: `rows_read_exactly`.

**When the index is not used.** The scan the index replaces is still there,
and is the fallback. Recall falls back to it — slower, and still correct — when
the file is missing, when it fails its own integrity check, when the query
vector's width does not match the index, and when the change log cannot vouch
for the file: the log was turned off or restarted since the build, a later build
dropped changes this file needs, or the schema changed since.

Each of these is reported where an operator reads health. `check_health` raises
`vector_index_absent` (with the count of rows it would cover) when there is no
index, `vector_index_unusable` when the file cannot be trusted, and
`vector_index_log_refused` (with the `reason`) when the change log cannot vouch
for it — one line per table, each naming its `table` — and the server logs a
warning on every query it had to hand back to the scan.

An index that had been unusable for a week without anyone noticing would
otherwise read as "somehow not faster", which is the failure the reporting
exists to prevent.

There is no configuration for the index. Its path is derived from
`CPERSONA_DB_PATH`, and the scan window it serves is the same
`CPERSONA_MAX_MEMORIES` the scan uses.

## The block index file

Unless `CPERSONA_BLOCK_INDEX=false`, the block arm reads its rows from a third
derived file, `<database>.blocks.blockindex`, instead of reading up to 250,000
rows out of SQLite on every recall. The answer is the same either way
([contract §3](BLOCK_CANDIDATES_CONTRACT.md#3-decisions)); only the time
differs. Like the other two files it is never backed up, never repaired, safe to
delete, and removed by a purge under the same rule.

The file takes 28 bytes per block row plus one bit per dimension of the model:
124 bytes with a 768-dimension model, 156 with a 1,024-dimension one. On Linux
and macOS it is mapped, so the operating system pages in what a recall reads; on
Windows it is read into memory whole. To go without it, set
`CPERSONA_BLOCK_INDEX=false` and restart: the server stops the log at startup,
recalls read SQLite, and the file can be deleted.

You do not build it by hand. While the setting is on, the server keeps a log of
the records whose blocks change, and its queue builds the file at startup and
after it writes blocks, whenever the file is absent, unusable or too far behind
that log; one build is queued at a time. Until a build finishes, recalls read
SQLite. With the task queue disabled, or to build it now:

```bash
python -m cpersona.block_index --db /path/to/cpersona.db build
python -m cpersona.block_index --db /path/to/cpersona.db status
```

`status` says whether a recall can read the file now (`current`) and how far the
log has moved since the build (`changes_since_build`). It exits 1 when there is
no file, and 2 when the file exists but cannot be used. To see which read served
a particular recall, ask for its trace (`trace=true`): `block_source` says `file`,
or `sqlite` with the reason the file was not read
([recall trace](RECALL_PROCESS_DESIGN.md#12-shape-trace_version-1)).

`check_health` reports `block_index_absent`, `block_index_unusable` and
`block_index_behind` as observations while the setting is on: every recall
still gets its answer, from SQLite. With the setting off, the one finding is
`block_log_without_index`, a log left running (for instance by a build from the
command line) that adds a row to every block write with nothing to read it;
`check_health(checks=["block_index"], fix=true)` stops it, and so does starting
the server with the setting off.

## The dashboard

`cpersona dashboard` opens a read-only page of a store in the browser: an
overview of what it holds and when it was written, with the five memories stored
last and the five recalled most (a memory never recalled is not listed there),
the logical directory as a
tree (agent, project, channel, year, month), a list you can narrow by folder,
month and text, and each memory in full with how it is split into nodes and
blocks.

```bash
cpersona dashboard                                 # the database a CPersona server opened last
cpersona dashboard --db ~/.claude/cpersona.db      # or: uvx cpersona dashboard --db …
cpersona dashboard --list                          # the databases CPersona servers have opened
cpersona dashboard --stop                          # stop the dashboard of that database
cpersona dashboard --install-shortcut              # a desktop shortcut that opens it
```

- **From a terminal or a shortcut**: `cpersona dashboard` opens the database
  `--db` or `CPERSONA_DB_PATH` names, else the one a CPersona server opened last:
  each server notes the database it opens in `~/.cpersona/stores.json` when it
  starts (paths and times only, owner-only; `CPERSONA_DASHBOARD_STORES_PATH` moves
  it). It listens on port 48700 and runs until `cpersona dashboard --stop`. Run
  again for the same database, it starts nothing and opens the running one; a
  port held by another database or program is reported, and `--port` picks
  another. The browser signs in with a one-time code for a session cookie, so a
  bookmark or the installed app opens the page again while the dashboard runs.
  The session ends when the dashboard stops; after a restart, open it from the
  shortcut or `cpersona dashboard`, which signs the browser in again. A browser
  sends the cookie to every program on 127.0.0.1, whatever its port, so on a
  machine other people log in to, stop the dashboard when you are not using it.
  `CPERSONA_DASHBOARD_STORES=false` stops the servers' notes.
- **A shortcut, and an app**: `cpersona dashboard --install-shortcut` makes a
  shortcut that opens that database's dashboard with a double-click: on Windows
  on the desktop and in the Start menu (pin it to the taskbar from there), on
  macOS an app in `~/Applications` with a link on the desktop (drag it to the
  Dock), on Linux an entry among the applications and on the desktop.
  `--remove-shortcut` takes it away. The shortcut runs the Python CPersona is
  installed in, so install CPersona with `uv tool install` or `pip` for it:
  `uvx` runs from a cache that can be cleared. The page can also be installed
  from the browser as an app (Chrome and Edge; Safari's Add to Dock); its service
  worker passes every request through and keeps nothing in the browser.
- **From your agent** (deprecated; `open_dashboard` is removed in 2.7, and
  `cpersona dashboard` above opens the dashboard without an agent): ask it to open the dashboard. Under the stdio transport the
  server registers `open_dashboard`, which opens the page for the database the
  server already uses and returns the address, so nobody has to look the path up.
  Opened this way the store's status page also shows the server's check, the
  same checks as `check_health` without repairs. The page runs until the server stops.
  `CPERSONA_DASHBOARD=false` removes the tool. Over HTTP it is registered only with
  the remote dashboard on (next item). With the agent plugin, `/cpersona:dashboard`
  asks the agent to open it in your browser: on this machine through the tool's
  `open_browser`, and from a remote server by opening the link it returns at once,
  so nobody copies a link by hand.
- **On a server people reach remotely** (off by default): with
  `CPERSONA_DASHBOARD_REMOTE=true` and `CPERSONA_DASHBOARD_PUBLIC_URL` set to the
  address people reach the server at, the HTTP transport serves the page under
  `/dashboard/` and registers `open_dashboard` too. Asked by an authenticated
  client, the tool returns a new link each time, which opens once within ten
  minutes; the tool tells the agent to open it in the person's browser itself when
  it can. The link's code is after `#`, so no request line, access log or Referer
  carries it: the page sends it once in a header and gets a session cookie for it
  (HttpOnly, Secure, SameSite=Strict, sent only to `/dashboard/`). The session lasts
  30 days and is renewed once half of it has passed, so a bookmark of the page
  keeps working for as long as it is used.
  The address must be https, or plain http on localhost; a browser drops a Secure
  cookie from any other http address, so the tool refuses one. Over HTTP the
  deprecated `open_dashboard` is, for now, the only way to sign in; a way that
  does not need it comes before 2.7, which removes the tool.
  - **Who sees what**: only the agents the client's grants let it read. Memories,
    episodes, entities and blocks of any other agent are left out, and so is a
    mention, relation, alias or node that names one of them, so no id of a hidden
    record reaches the page. The server's check, the store's files and the
    calibration values for every agent are shown only to a client that reads
    every agent; an agent's own calibration values to a client that reads it. Without an ACL the holder of the one token sees everything. A
    per-subject connection cannot open it yet.
  - **What it answers**: GET and HEAD only. Everything outside `/dashboard/`,
    the bare `/dashboard` included, still needs the bearer token.
  - **The session is the cookie**, signed by the server: it names the client and
    when it ends, and its signature covers the client's current bearer token
    (the server's token without an ACL). A restart keeps it. Replacing the
    client's token, or removing the client from the ACL, ends every session it
    opened; the grants are read from the ACL on every request. The settings'
    *Sign out* drops the cookie in that browser. The signing key is
    `CPERSONA_DASHBOARD_SESSION_KEY` when set, else a file beside the store
    (`<name>-dashboard.key`, owner-only) made on first use; deleting it ends
    every session. Unused links live in the server process, so a restart ends
    them; each client holds at most 20, and past that its own oldest goes first,
    never another client's.
  - **Memory**: pictures computed for the page are cached per set of grants, so a
    server whose clients hold many different grants holds a cache for each.

- **Which database**: the one `--db` names, else `CPERSONA_DB_PATH`. There is
  no fallback to the server's relative default, which would resolve against the
  terminal's directory. If CPersona runs from an MCP client, the path is the
  `CPERSONA_DB_PATH` in that client's server configuration.
- **It writes nothing.** The file is opened with SQLite's `mode=ro`, so a write
  is refused by SQLite itself, and it can stay open beside a running server:
  each page load reads the last committed state. A database whose schema this
  release does not know (newer, or older than schema 19) is refused with a
  message instead of being read.
- **Who can see it**: it listens on 127.0.0.1 only: on port 48700 from the
  command line, on a free port when an agent opens it. The address it prints
  carries a token made for this start after `#`, which the browser never sends in
  a request; the data is answered only to requests carrying it (or, from the
  command line, the session cookie its one-time code opened), only for a Host
  header naming the loopback address, and only to GET. Asking a running dashboard
  for a code, or to stop, takes a proof made with the store's key file, which only
  its owner can read. Another user of the same machine can reach the port but not
  the data. `--no-browser` prints the address without opening it.
- **How it is laid out**: two layouts, chosen in the settings (the gear at the
  foot of the side panel) and kept in the browser. *Standard*, the default, is
  one side panel of the views, grouped under headings (the overview; records:
  memories, episodes and profiles; connections: entities and the network; and
  the store's status), over the logical directory; a memory opens in a drawer beside the
  list, so the list does not move. The list behind the drawer is dimmed, and a
  click on it closes the drawer. Under the views, a line names the key that opens
  the command palette: ⌘K on a Mac, Ctrl+K elsewhere. *Tabbed* puts a
  column of icons on the left (the same views in the same groups), a side panel that browses the logical directory or
  searches, and tabs: each memory opens in a tab of its own, and each tab has its
  own back and forward (Alt+← / Alt+→). Switching keeps the memory that is open.
  In both, Ctrl or ⌘ with K, O or P opens the command palette, and a record opens
  with its text first: a line of when and where it was stored sits above the text,
  and its other attributes follow it.
- **Times** are shown in the viewer's time zone, which the settings name at
  their foot. A stored time is read as the moment it names, with its own offset;
  one stored without an offset is read as UTC. *Today* and *Yesterday* over the
  list are the viewer's days.
- **Search**, in the tabbed layout's side panel, finds memories whose text holds every word typed,
  and shows the lines that matched. `"a phrase"` matches as written and `-word`
  leaves out memories holding the word. `path:` matches agent/project/channel,
  `agent:` the agent, `tag:` an entity's name or alias that the memory mentions,
  `line:` words that must share a line, and `month:` the start of the timestamp
  (`month:2026-10`). `[locked]`, `[recalled]` and `[vector]` keep memories that
  are locked, have been recalled, or hold a vector; `[project:x]` and
  `[channel:x]` keep that exact project or channel. Aa matches case, and the
  panel's own settings sort the results by newest, oldest or most recalled.
- **The network is of memories**: every memory, each a point, as a notes app's
  graph view draws every note (the next item but one), or the memories of a
  folder, a month, an entity or a group of similar memories when a link narrows
  it.
- **How it looks**: on a dark ground, memories are small plain dots and each
  agent is a flat dot carrying a figure (stripes, a crescent, a ring, dots or
  quarters) chosen from its id, so the same agent looks the same on every load.
  Colour marks only the point pointed at and its lines, or every agent when
  *Colour by agent* is on. Agents are always named; other names appear as the
  view comes closer. In a picture of
  more than 1,000 points the dots are drawn brighter over the lines. The setting
  *Avoid clusters of dots* draws them small and faint instead, with the lines as
  the subject, for people who find dense fields of dots unpleasant to look at.
- **The memories** are drawn as points joined by three kinds of line, each
  of which can be hidden: relations an agent declared (entities a memory
  mentions, and relations between entities and memories); similar content, each
  memory to its 3 nearest memories by stored embedding at cosine 0.55 or more,
  which the server computes and no agent declared; and each agent to its
  memories. A folder's view draws every memory in the folder, as a notes app
  draws every note; the nearest neighbours are found 1,024 rows at a time, so the
  similarities held at once grow with the folder rather than with its square, and
  the page carries no memory text (a selected point fetches its own). Past 1,000
  points the page sums the push between points by Barnes–Hut and starts with the
  lines from agents to their memories hidden. Nothing is settled before the first
  frame: a picture seen before is drawn where it was left, its new points find
  their place on screen while the rest hold still, and a first view settles on
  screen and is framed as it goes. The server keeps the last picture of each
  folder for each viewer. When the store has changed since, it answers with the
  kept picture at once and computes a new one in the background for the next
  request, so a memory stored a moment ago can be missing from one opening; a
  kept picture is computed again at least every ten minutes. Reading the
  overview starts the whole store's picture in the background, so the network is
  usually ready when it is opened. The page in turn fetches the whole store's
  network as soon as it has opened and keeps the last few networks it fetched:
  opening one draws the kept one at once and fetches it again behind the drawing,
  for the next opening. "See it in the network" on a memory draws that memory and the
  memories reached from it in two nearest-neighbour steps across the whole
  store. Vectors are compared only with vectors of the same width, are held in
  the process serving the dashboard for up to five minutes while the store keeps the same
  rows, and are never sent to the page. A store whose vectors exceed 128 MB is
  read in chunks on each step instead of being held.
- **Points stay where they were left.** A point's first place is derived from
  its id, so the same data is laid out the same way on every load. Where the
  points settled is then kept in the browser, for the 20 pictures opened most
  recently: on the next load they start there, a point added since starts beside
  the points it is tied to, and only the new points move while they settle. The
  positions are kept per database file name and picture, in that browser only.
- **Episodes and entities** are listed and opened as memories are: in the drawer
  in the standard layout, in a tab of their own in the tabbed one. Episodes come
  the latest conversation first, searched in their summaries and keywords; an
  episode shows when the conversation took place, whether its topic was
  concluded, and the entities declared as mentioned in it. Entities come the most
  mentioned first, searched in their names and aliases; an entity shows the
  memories and episodes that mention it and the relations it takes part in, and
  each of those opens from there. A record a relation names that cannot be read
  (outside what the viewer may see, or gone) is named as not found rather than
  shown.
- **Profiles**: each agent's profile, the summary it keeps of its user, in full
  with when it was written and its length against the cap a write is cut to. A
  recall sends an agent's own profile with every answer; one written for a named
  user is marked as not sent. Choosing an agent in the memory list shows that
  agent's profile above its memories.
- **Store status**: what the store's own files say, read without the server: how
  many memories and episodes have no embedding (a vector search cannot reach
  them), the schema, the file's size and last write, whether each index file is
  there, and the calibration file's values (the threshold and gates for every
  agent and for each; a gate stored without a value is marked). Opened by an
  agent, it adds the server's check, notes included.
- Recall traces are not shown yet, and nothing is edited from the page. The
  server's check appears only when an agent opened the page: started from a terminal,
  the dashboard cannot judge it from the file alone.

## Maintenance cadence

- **Monthly**: `check_health(agent_id, fix=true)` for deterministic integrity
  checks and auto-repair, `deep_check(agent_id, fix=true)` for the semantic
  pass, and a logical `export_memories` backup.
- **After corpus upheaval** (bulk import, rebuild, model change):
  `calibrate_threshold(agent_id)`.
- **On a timer, if the vector index is in use**:
  `python -m cpersona.vector_index build`, and the same with
  `--table episodes`. Nightly by default; see
  [the contiguous vector index](#the-contiguous-vector-index) for what a late
  rebuild costs (latency, never correctness).
- **Version upgrades**: the schema migrates itself forward. Calibration is
  fingerprinted to the scoring function and embedding dimension, and the
  server recalibrates at first boot when either changes (v2.5.2+).