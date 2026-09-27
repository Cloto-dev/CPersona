# Upgrading from 2.5 to 2.6

This page takes an existing 2.5.x store to the current 2.6 pre-release,
**2.6.0a7**, in one pass. Each 2.6 pre-release documented only its own step in
its release notes; this page puts the steps from 2.5.12 onward in one place.

2.6 is still a pre-release line (Experimental in the
[support policy](https://github.com/Cloto-dev/CPersona/blob/master/SUPPORT.md)):
opt-in, and without the guarantees of a final release. The steps below can
still change before 2.6.0 final. This page is updated with each pre-release and
settled at the final.

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
3. **Install the pre-release explicitly.** pip does not pick pre-releases on its
   own:

   ```sh
   pip install 'cpersona==2.6.0a7'
   ```

## What the first start does

On its first start, 2.6 migrates the database from schema version 13 (every
2.5.x release) to schema version 17, one step at a time. **No stored row is
rewritten**: each step adds tables and triggers. A step that fails is not
recorded as done, so it is retried on the next start.

| Schema version | Added in | What it adds | Anything to build afterwards? |
| --- | --- | --- | --- |
| 14 | 2.6.0a3 | `record_nodes`: the overflow tree, pieces of a record past the embedding window | **Yes**: nodes for long records already stored ([below](#build-the-overflow-nodes)) |
| 15 | 2.6.0a4 | `entities`, `entity_aliases`, `entity_mentions`, `relations`: declared associations | No |
| 16 | 2.6.0a5 | `record_blocks`: block reach | Only if you turn block reach on ([below](#optional-turn-on-block-reach)) |
| 17 | 2.6.0a6 | `record_block_vectors`: one vector per block | Only if you turn block reach on |

2.6.0a1, 2.6.0a2 and 2.6.0a7 changed no schema.

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

### Optional: turn on block reach

Block reach is off by default and costs nothing while off: no embedding calls,
no rows, no queued work. It makes text past a long record's embedding window
reachable by search ([design](BLOCK_REACH_DESIGN.md)).

- `CPERSONA_BLOCK_BUILD_ENABLED=true` builds and maintains the block index and
  starts a bounded backfill of the records already stored. `check_health` shows
  the progress as `missing_blocks` and moves it along under `fix=true`.
- `CPERSONA_BLOCK_RETRIEVAL_ENABLED=true` reads the index during recall. It
  needs the build gate: reading an index nothing fills is a startup error. It
  has no effect where vector search is remote.

If you ran 2.6.0a5 with block construction on, its block sets have no vectors
and are rebuilt by the same backfill.

## Behaviour that changed

Check these against what your deployment relies on. Each is off, or equal to
2.5, unless noted.

- **`limit` is the number of rows returned** (2.6.0a2). How deep fusion looks
  is `max(limit, CPERSONA_RECALL_DEPTH_FLOOR)`; the floor defaults to 0, so the
  ranking is the one 2.5 gave.
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

New in 2.6 and inert unless asked for: the `reconstruct` tool, the recall trace
(`trace=true`), the time cue (`time_cue`), and associations declared with
`declare_associations` or on `store`.

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
