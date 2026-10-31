# Upgrading from 2.5 to 2.6

This page takes an existing 2.5.x store to **2.6.0** in one pass. Each 2.6
pre-release documented only its own step in its release notes; this page puts
the steps from 2.5.12 onward in one place.

2.6.0 is the 2.6 line's first final release (Current in the
[support policy](https://github.com/Cloto-dev/CPersona/blob/master/SUPPORT.md)),
and the 2.5 line becomes Candidate: no channel serves it, and it stays
reachable by exact version.

## Before you start

1. **Back up the database and its calibration file.** Follow
   [Backup and restore](operations.md#backup-and-restore): an online
   `sqlite3 "$CPERSONA_DB_PATH" ".backup 'cpersona-backup.db'"`, plus a copy of
   `<CPERSONA_DB_PATH>.calibration.json`, which lives outside the database.
   This backup is the only way back to 2.5 (see
   [Going back to 2.5](#going-back-to-25)).
2. **Check the embedding server.** Building the overflow nodes for records
   already stored needs a server that reports token counts: CEmbedding 0.8.0 or
   later. An older server answers `count: null`, which is not zero.
3. **Install the newest 2.6 release.** A plain upgrade resolves to it. Keep it
   within the 2.6 line, and note which version you got so you know what you are
   running:

   ```sh
   pip install --upgrade 'cpersona>=2.6,<2.7'
   pip show cpersona | grep Version
   ```

## What the first start does

On its first start, 2.6 migrates the database from schema version 13 (every
2.5.x release) to schema version 20 (19 for 2.6.7a2 to 2.6.11a1, 18 for 2.6.3 to 2.6.7a1, 17 for 2.6.0 to 2.6.2), one step at a time. **No
stored row is rewritten**: each step adds tables and triggers, or one column. A step that fails is not
recorded as done, so it is retried on the next start.

| Schema version | Added in | What it adds | Anything to build afterwards? |
| --- | --- | --- | --- |
| 14 | 2.6.0a3 | `record_nodes`: the overflow tree, pieces of a record past the embedding window | **Yes**: nodes for long records already stored ([below](#build-the-overflow-nodes)) |
| 15 | 2.6.0a4 | `entities`, `entity_aliases`, `entity_mentions`, `relations`: declared associations | No |
| 16 | 2.6.0a5 | `record_blocks`: block reach | **Built for you**: blocks for the records already stored, unless you turn block reach off ([below](#block-reach-is-on-by-default)) |
| 17 | 2.6.0a6 | `record_block_vectors`: one vector per block | Built with the blocks |
| 18 | 2.6.3a1 | `embedding_model` on `memories` and `episodes`: the label of the model that produced each vector. Rows already stored take an empty label, which means unknown | No: a vector is labelled when it is next written |
| 19 | 2.6.7a2 | `block_log_clock`, `record_block_changes`: the log of which records' blocks changed, kept only while the block index file is on ([contract §3.4](BLOCK_CANDIDATES_CONTRACT.md#34-how-the-file-stays-exact)) | **Built for you** on the queue, unless you turn the file off ([below](#the-block-index-file-is-on-by-default)) |
| 20 | 2.6.11a2 | `vector_log_clock`, `record_vector_changes`: the log of which rows held by the vector index files changed, kept only while one of those files exists ([design §5](CONTIGUOUS_INDEX_DESIGN.md#5-freshness-the-watermark)) | **Yes, if you build the vector index**: build it again ([below](#build-the-vector-index-again)) |
| 21 | 2.6.14a1 | `folders`, `folder_members`: declared folders and the one folder each filed record sits in, used only with `CPERSONA_DECLARED_FOLDERS` on ([configuration](configuration.md)) | No: the tables stay empty until something is filed |

2.6.0a1, 2.6.0a2, 2.6.0a7, 2.6.0a8, 2.6.0b1, 2.6.0b2, 2.6.0, 2.6.1, 2.6.2, 2.6.4a1, 2.6.4a2, 2.6.4, 2.6.5a1, 2.6.5a2, 2.6.5a3, 2.6.5, 2.6.6a1, 2.6.6, 2.6.7a1, 2.6.7, 2.6.8a1, 2.6.8, 2.6.9a1, 2.6.9, 2.6.10a1, 2.6.10a2, 2.6.10a3, 2.6.10a4, 2.6.10, 2.6.11a1, 2.6.11a3, 2.6.11a4, 2.6.11a5, 2.6.11a6, 2.6.11, 2.6.12a1, 2.6.12, 2.6.13a1, 2.6.13a2, 2.6.13, 2.6.14, 2.6.15a1, 2.6.15, 2.6.16a1, 2.6.16a2, 2.6.16a3 and 2.6.16a4 changed no schema.

Going back from 2.6.14 or 2.6.14a1 to 2.6.13 needs no restore. 2.6.13 opens a schema 21 database with a
warning that it is newer, reads and writes it as before, and the triggers schema 21 added stay in
place, so a record deleted meanwhile leaves no membership behind. Its folders are not read until
2.6.14 or 2.6.14a1 starts again.

## After the first start

### The gate is recalibrated

2.6.0a7 changed the scoring version, so a calibration stored by an earlier
version is treated as stale. With the default
`CPERSONA_CALIBRATE_ON_MODEL_CHANGE=true`, the server recalibrates the global
threshold at startup. If both that and `CPERSONA_AUTO_CALIBRATE` are off, the
stale gate is not applied and `deep_check` reports `stale_scoring_version`
until `calibrate_threshold` is run.

**Per-agent calibrations are not redone.** A threshold or gate calibrated for
one agent with `calibrate_threshold` is discarded with the old scoring version;
that agent falls back to the global threshold and the heuristic gate until you
run `calibrate_threshold` for it again. Preferences set with
`set_recall_precision` are kept.

### Build the overflow nodes

Long records stored before the upgrade have no nodes until they are built.
Until then they are quoted from their start, and `node_unavailable: no_nodes`
says so. Build them with the health check, which handles 50 records per run;
repeat it until it reports none left:

```text
check_health(agent_id="<id>", fix=true, checks=["missing_nodes"])
```

The repair never modifies a record, so it also covers locked memories. Run it
again after changing the embedding model.

### Block reach is on by default

From 2.6.0, block reach is on unless you turn it off. It makes text past a long
record's embedding window reachable by search, and `reconstruct` quotes the
block that matched ([design](BLOCK_REACH_DESIGN.md)).

- **The first start begins a bounded backfill** of the records already stored.
  It embeds every block through the same embedding server that embeds your
  records, one call per batch of blocks; on this project's own store, 4,478
  records divided into 70,130 blocks. `check_health` shows the progress as
  `missing_blocks` and moves it along under `fix=true`. Until a record's blocks
  exist, recall reaches it through the other arms, as it does with block reach
  off.
- **Each block is stored twice**, as one bit per dimension and as one byte per
  dimension: 1,152 bytes of vector per block with a 1,024-dimension model
  (128 + 1,024).
- **Recall reads the index** on every query that has a vector, and may return
  up to 2 rows beyond `limit` for records only the block arm reached. It has no
  effect where vector search is remote.
- **Recall gets slower and larger.** On that store, on one machine, the median
  `recall` took 138 ms with the block arm against 17 ms for 2.5.12 (the query's
  embedding excluded), and the
  process peaked at 180 MB against 106 MB. The cost grows with the number of
  blocks; a store ten times larger has not been measured.
- **To turn it off**, set `CPERSONA_BLOCK_BUILD_ENABLED=false`: no embedding
  calls, no rows and no queued work, and the reader follows it off.
  `CPERSONA_BLOCK_RETRIEVAL_ENABLED=false` alone keeps the index built and
  unread. Setting the reader on with construction off is a startup error.

If you ran 2.6.0a5 with block construction on, its block sets have no vectors
and are rebuilt by the same backfill.

### The block index file is on by default

From 2.6.7, the block arm reads the rows it examines from a file beside the
database instead of from SQLite, with the same answer
([operations](operations.md#the-block-index-file)).

- **The first start builds the file** on the queue, once there are blocks to
  hold. On a store of 100,000 memories with 3,480,069 blocks, it was 431,528,748
  bytes and took 11 to 14 s to build on an Intel N150. Until it is built,
  recalls read SQLite.
- **Recall gets faster.** On that store and machine, against rules registered
  before the run, the median recall fell from 960 to 798 ms and from 931 to
  844 ms, the block arm from about 575 ms to 64 ms, and every recall returned
  the same rows in the same order ([results](https://github.com/Cloto-dev/CPersona/blob/master/benchmarks/measurements/results-recall-latency-block-index-file.md)).
- **Writes pay a little.** The server logs each block row written or deleted,
  about 16 bytes per entry, which added under 1% to a block build there. The
  file is rebuilt once the log has more than 35,000 entries since it was built.
- **On Windows the file is read into memory** rather than mapped.
- **To turn it off**, set `CPERSONA_BLOCK_INDEX=false`: the server stops the
  log at startup and recalls read SQLite.

### Build the vector index again

If you build the contiguous vector index (and with it the coarse index), build
it again after the first start:

```bash
python -m cpersona.vector_index --db "$CPERSONA_DB_PATH" build
python -m cpersona.vector_index --db "$CPERSONA_DB_PATH" --table episodes build
```

A file now records where the change log stood when it was built, and a query
reads every row changed since from the database
([operations](operations.md#the-contiguous-vector-index)). A file written before
schema 20 cannot say which of its rows changed, so it is refused: `status` exits
2 and recall uses the scan, with the same answers and without the speed, until
the file is built again.

## Behaviour that changed

Check these against what your deployment relies on. Each is off, or equal to
2.5, unless noted.

- **Block reach is on by default** (2.6.0). A store queues the record's
  blocks and says so (`blocks: {"status": "queued"}`), recall can return up to
  2 reserved rows beyond `limit`, and `reconstruct` quotes the block that
  matched. [Above](#block-reach-is-on-by-default) is how to turn it off.
- **The block index file is on by default** (2.6.7). The first start builds a
  file beside the database, and recalls read the block arm's rows from it with
  the same answer. [Above](#the-block-index-file-is-on-by-default) is how to
  turn it off.
- **`reconstruct` returns 10 items when `count` is omitted** (2.6.0; it
  returned 1). With block reach on, this is the configuration 2.6.0 recommends
  for answering from memory. A deployment that relied on one item sets
  `CPERSONA_RECONSTRUCT_DEFAULT_COUNT=1`; lowering
  `CPERSONA_RECONSTRUCT_MAX_COUNT` alone lowers the default with it.

- **`limit` is the number of ranked rows returned** (2.6.0a2). How deep fusion
  looks is `max(limit, CPERSONA_RECALL_DEPTH_FLOOR)`; the floor defaults to 0,
  so the ranking is the one 2.5 gave. Rows held beside the count come on top of
  it: the block reservation above and the time-cue seats below.
- **`reconstruct` needs the same per-agent read grant as `recall`** (2.6.0a2).
  This matters only where [access control](ACL_DESIGN.md) is configured.
- **Confidence no longer re-sorts or gates recall** (2.6.0a7). With
  `CPERSONA_CONFIDENCE_ENABLED=true`, each row still carries a `confidence`
  value, but the order is the fusion order. The profile row (`update_profile`)
  used to rise to the top under the old re-sort; it now comes last and a full
  `limit` can cut it. If something must reach the agent every time, put it in
  the client's instructions file or system prompt. `CPERSONA_CONFIDENCE_ORDERING=legacy`
  restores the old order.
- **The episode-boundary penalty is off by default** (2.6.0a7). Deployments
  that used it to damp older sessions set `CPERSONA_EPISODE_PENALTY_ENABLED=true`.
- **A time cue can add up to three rows** (2.6.0a8). With `time_cue`, `recall`
  returns up to `limit` + 3 rows (3 / 2 / 1 by the cue's confidence), and a cue
  that points only at the last 24 hours is not used (`time_cue.ignored`).
  Without a cue nothing changes.
- **`declare_associations` is marked destructive** (2.6.0b1). Its `retract`
  argument deletes, so a client that asks before running destructive tools now
  asks before this one.
- **`reconstruct` walks at most 5 hops**, as `traverse` does (2.6.0b1). A larger
  `max_hops` is lowered, and `bounds.max_hops` states the value applied.
- **An alias follows the scope it is declared in** (2.6.0b1). A declaration made
  in a project that gives aliases to a name the global pool already has now
  registers the project's own entity, so other projects do not read those
  aliases. Aliases declared this way before 2.6.0b1 stay readable everywhere:
  the stored data does not record which scope declared an alias.
- **Smaller corrections** (2.6.0b1): a `store` without `associations` no longer
  answers with an empty `associations` object; `retract` refuses `true` and
  `false` as ids; a quote carries a qualifier that starts on the next line; an
  episode's `reconstruct` quote is measured in the stored summary, without the
  `[Episode] ` label; `check_health` reports and rebuilds node sets with a gap or
  a missing embedding.
- **`session_key` is at most 256 characters** (2.6.0b2). Every tool that takes
  it declares `maxLength: 256`, and a longer key is refused before the call
  runs, with an input validation error. Keys minted from a process id and its
  start time, or a UUID, are well under the bound.
- **A listing the row cap cut says so** (2.6.0b2). `list_memories` and
  `list_episodes` still clamp `limit` to 500 and 200 rows. When the caller asked
  for more and rows past the cap exist, the response carries `budget_rows` (the
  cap). A client that stops when it gets fewer rows than it asked for should
  read this key.
- **How far behind the vector index is counts every row a query reads from the
  table** (2.6.0b2). `python -m cpersona.vector_index status` adds
  `rows_read_exactly`, `excluded` and `unembedded`, and `build` adds
  `unembedded`. `check_health`'s `vector_index_tail_grown` compares
  `rows_read_exactly` against the rebuild ratio, so after `fix=true` fills
  missing embeddings it can appear with `rows_past_watermark` at 0. Rebuilding
  the index takes in the rows that have since gained an embedding.
- **Smaller corrections** (2.6.0b2): on an in-memory database, `export_memories`
  reads a consistent copy; the vector index path scores a window it has to copy
  one chunk at a time, in the ranges the table scan uses.
- **A `reconstruct` budget below one head quote is raised to it** (2.6.0). With
  the default `CPERSONA_RECONSTRUCT_QUOTE_CHARS` of 800, a smaller `budget`
  becomes 800 and `budget_policy` says so. It used to be raised only to 500,
  while the head could run to 800 without the overrun being reported.
- **A recall excerpt reads the block index only while block retrieval is on**
  (2.6.0), as `reconstruct`'s quote does. With `CPERSONA_BLOCK_BUILD_ENABLED=true`
  and `CPERSONA_BLOCK_RETRIEVAL_ENABLED=false`, `excerpt_basis` is now `lexical`.
- **A block set is current only if it respects the node layout and has every
  re-rank vector** (2.6.0). Where block building is on, sets built before their
  record's nodes existed are rebuilt by the sweep and by
  `check_health(fix=true)` after the upgrade.
- **A non-default `CPERSONA_PRIOR_FAR_WEIGHT` is part of the calibration**
  (2.6.0). A deployment that sets it to anything but 1 recalibrates on its first
  start of 2.6.0, because a gate measured at another weight is no longer
  restored. At the default nothing changes.
- **A time cue searches past the scan window through the coarse index by
  default** (2.6.4a2). With `CPERSONA_CUE_COARSE_ENABLED` unset, the part of a
  cue's period past the scan window is searched when a coarse index exists, and
  otherwise left out with `time_cue.remainder` saying so; nothing reads every
  stored vector. A store within the window sees no change. `true` and `false`
  keep their meaning ([settings](BINARY_COARSE_SEARCH_DESIGN.md#7-settings)).
- **The server sends instructions, and three tools load with the session**
  (2.6.5a2). The `initialize` instructions now open with CPersona's guidance on
  when to use its tools (about 500 characters), followed by the operating
  context's summary when one is configured; without one they used to be empty.
  In Claude Code, `reconstruct`, `store` and `archive_episode` load at session
  start instead of through tool search, so every session carries their
  definitions, and startup waits up to 5 seconds for the server's tools. Five
  tool descriptions are shorter; what they dropped is in the design documents
  they name.
- **Rows held beside the answer earn no recall count** (2.6.0). With confidence
  enabled, the block reservation, the time cue's seats and the propagation seat
  no longer raise `recall_count`, and their `confidence` reads their own history.
- **Smaller corrections** (2.6.0): a `reconstruct` quote cut to the preview
  tier hands over the whole range it began in `expand`; `shortfall_reason`
  blames the budget only when it cut an item of the window; `bounds.reached`
  counts only the rows inside recall's limit; a `time_cue` past the
  representable range is clipped rather than raising, and a unit that is not a
  string is refused; a failing node or block build no longer holds the other
  queued tasks; in `api` embedding mode the node check no longer reads every
  record's text.
- **An ACL token no client can present stops the server at startup**
  (2.6.11a6). A token with leading or trailing whitespace (a secret written with
  `echo` ends in a newline) or a control character used to load as a row that
  answered its client `401` on every request. The startup error names the client.
  Check the values your `${ENV}` references resolve to, as well as literal tokens,
  before you upgrade ([ACL design](ACL_DESIGN.md)).

- **The agent skill moved, and an agent plugin ships** (2.6.12a1). The skill's
  source is now `plugin/skills/cpersona-memory` in the repository (a copy
  installed from the wheel stays at `cpersona/skills/cpersona-memory`), and
  `examples/claude-code-hooks/prompt_hook.py` is gone: its hook is now one of the
  plugin's three. Links to the old paths on GitHub no longer resolve. If you
  registered that hook yourself or copied the skill, read
  [getting started](getting-started.md#let-the-agent-do-it-claude-code) before you
  install the plugin, so the agent does not get every note twice.

- **A `reconstruct` call without a mode can return one more item** (2.6.12). It now
  holds the keyword seat `lite` and `pro` hold: a row only the keyword arms found,
  returned beside the window as an item marked `admission: "reservation"`. Held
  items could already follow the window (the block reservation); a caller that
  sizes what it handles by `count` should read `returned_count`
  ([Reliable Recall](RELIABLE_RECALL_2_6.md)).

- **The plugin's notes name an `agent_id`** (2.6.13a1). With no id set, they name
  `default` and tell the agent to keep an id its `CLAUDE.md` or `AGENTS.md` already
  names. If you used the 2.6.12 plugin without such an id, your agent stored under
  names it guessed: list them with `list_memories` and an empty `agent_id`, then
  set the plugin's `agent_id` option to that name, or move its records into
  `default` with `merge_memories`
  ([getting started](getting-started.md#let-the-agent-do-it-claude-code)).

- **`reconstruct` with `mode: "pro"` returns different items** (2.6.13a2). Pro asks
  for eight extra keyword places and eight depth places, held items that follow the
  window, and quotes the later passages of a long held record last, within the same
  5,000-token cap. `lite` and a call that names no mode return what they did
  ([Reliable Recall](RELIABLE_RECALL_2_6.md)).

- **A server notes its database for the dashboard** (2.6.16a2). On start it writes the
  absolute path of the database it opens, with the time, to `~/.cpersona/stores.json`
  (owner-only; `CPERSONA_DASHBOARD_STORES_PATH` moves it), so `cpersona dashboard`
  opens that database without being told. Nothing read from the store is written
  there, and a note that cannot be written does not stop the server.
  `CPERSONA_DASHBOARD_STORES=false` stops the notes (2.6.16a3).

- **`open_dashboard` is deprecated** (2.6.16a2) and is removed in 2.7. Its descriptions
  say so and its answer carries a `deprecated` field. `cpersona dashboard` opens the
  dashboard without an agent: on port 48700 until `--stop`, signed in by cookie, with
  `--install-shortcut` for a desktop shortcut
  ([operations](operations.md#the-dashboard)). From 2.6.16a3 its session ends when it
  stops, so a browser signed in by 2.6.16a2 opens it once more from the shortcut or the
  command. Over HTTP the tool is, for now, the only way to sign in to the remote
  dashboard; a way without it comes before 2.7.

New in 2.6 and inert unless asked for: the `reconstruct` tool, the recall trace
(`trace=true`), the time cue (`time_cue`), associations declared with
`declare_associations` or on `store`, the propagation seat
(`CPERSONA_RECALL_PROPAGATION_SEAT`), the folder view of `list_memories`
(`CPERSONA_LOGICAL_DIRECTORY`), and declared folders (`CPERSONA_DECLARED_FOLDERS`).

## Checking the result

- `check_health(agent_id="<id>", checks=["missing_nodes"])` reports no records
  left to build.
- `deep_check` does not report `stale_scoring_version`.
- A few recalls you know the answer to return it.

## Going back to 2.5

Restore the backup you took before the upgrade (the database and the
calibration file), then install 2.5.12. **Do not point 2.5 at a migrated
database.** 2.5.12 opens a newer schema only with a warning that it may misread
it; nothing converts a schema version 17 database back.
