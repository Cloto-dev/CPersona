"""Contiguous embedding index — builder and loader.

Design: `docs/CONTIGUOUS_INDEX_DESIGN.md`. In one line: the local vector scan
spends 72.9% of its time turning SQLite rows into Python objects and 2.7% on the
arithmetic, so the embeddings are written out once in the layout numpy wants and
read back with a single `fromfile`.

This module builds and validates that file. It never computes a similarity and
never decides what a caller may see — the arithmetic stays where it is, and
`isolation_where()` stays the authority on ownership. What lives here is a
derived artifact: it is not backed up, never repaired, and safe to delete.

Layout (little-endian throughout, offsets 8-byte aligned by construction):

    magic  b"CPXIDX01"                       8 bytes
    header_len                               uint32
    header                                   JSON, padded to a 64-byte boundary
    ids                                      int64[count]
    embeddings                               float32[count][dim]
    agent_code / project_code /
    channel_code / source_code               int32[count] each
    created_at                               19 ASCII bytes per row

The header carries no wall-clock field, deliberately: the file is then a pure
function of the database content it was built from, so "rebuild produced the
same bytes" is a property a test can assert rather than a claim. When it was
built is the file's mtime, which is where that belongs.
"""

from __future__ import annotations

import json
import logging
import os
import struct
from dataclasses import dataclass

import numpy as np

from cpersona import config
from cpersona import fileperms
from cpersona.isolation import isolation_where, source_id_matches
from cpersona.utils import SCORING_VERSION

logger = logging.getLogger(__name__)

MAGIC = b"CPXIDX01"
# 2 (bug-278): the header gained unembedded_ids. A sidecar written by the previous
# builder has no record of the rows it skipped for a NULL embedding, so a new reader
# cannot make it correct — it can only be rebuilt. Bumping the version turns those
# files into IndexUnusable, which the query path already answers by using the scan.
# 3 (bug-514): the header gained the change log's generation, head and schema
# cookie at the build. A file without them cannot say which of its rows changed
# since, so it is refused for the same reason and rebuilt.
FORMAT_VERSION = 3
HEADER_ALIGN = 64

# `created_at` is TEXT with a one-second-resolution default. Fixed-width ASCII in
# this exact form is what lets the merge against the live tail compare byte-wise
# and mean the same thing SQLite means by comparing the column as text.
CANONICAL_CREATED_AT = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]"
CREATED_AT_WIDTH = 19

# A row whose created_at is not in that form (the import path carries a restored
# record's own value through) is named in the header so the query path can union
# it into the exact tail read. Past this many, naming them stops being cheaper
# than not having an index: the build declines instead, which leaves the caller
# on the path that was always correct. The number itself is a corpus-scale
# setting and lives with the others in config, where the measurement that sizes
# it is written down; this module binds it once so a test can steer the cap by
# patching this name.
MAX_EXCLUDED_IDS = config.VECTOR_INDEX_MAX_EXCLUDED_IDS

# Only agent_id is compared for equality; the other three axes are compared
# against a small set. Either way the per-row test is an integer one.
_AXIS_FIELDS = ("agent_code", "project_code", "channel_code", "source_code")

NULL_CODE = -1  # a source id that is SQL NULL: LIKE never matches it


#: The tables an index can be built for. One list, because anything that has to
#: walk every index file (the purge in ``admin_handlers``) and anything that has
#: to offer them (the CLI) would otherwise each carry their own copy and drift.
INDEXED_TABLES = ("memories", "episodes")


def index_path(table: str = "memories") -> str:
    """Where the index for `table` lives — beside the database, like the calibration sidecar."""
    return f"{config.DB_PATH}.{table}.vecindex"


# How many agent purges have dropped the index files in this process. A build
# reads its rows on a snapshot and publishes them afterwards; a purge that commits
# and drops the files in between would otherwise be undone on disk, the build
# putting the purged agent's identifiers and vectors back where the purge had just
# removed them (bug-515). Every index file's build checks this before publishing.
_purges = 0


def purges() -> int:
    """The purge count a build reads before it reads any row."""
    return _purges


def note_purge() -> None:
    """Called by a purge before it drops the index files, so in-flight builds discard theirs."""
    global _purges
    _purges += 1


def purged_since(purges_at_start: int) -> bool:
    """Whether a purge dropped the index files since a build read ``purges_at_start``.

    A build asks this immediately before its ``os.replace``, with no await between
    the two, so on the event loop the purge (which counts and unlinks without
    awaiting either) lands wholly before or wholly after. A build that answers yes
    discards its file: the rows it holds may include a purged agent's, and the
    next build reads the database as the purge left it.
    """
    return _purges != purges_at_start


#: What a build that was discarded by a purge answers instead of a file.
SUPERSEDED_REASON = "an agent purge dropped the index files while this build read its rows; build again"


# --------------------------------------------------------------------------------------
# The change log (VECTOR_LOG_SQL in database.py, design §5).
# --------------------------------------------------------------------------------------

#: The change log's name for each indexed table's rows.
LOG_KINDS = {"memories": "mem", "episodes": "ep"}

#: The logging triggers a build requires, by their names in `checks._EXPECTED_OBJECTS`.
LOG_TRIGGERS = (
    "memories_vector_log_au",
    "memories_vector_log_ak",
    "memories_vector_log_ad",
    "episodes_vector_log_au",
    "episodes_vector_log_ak",
    "episodes_vector_log_ad",
)

#: Rows changed since a build that a read takes from the live table before it gives
#: the file up and scans. The same number as the build's cap on the holes it names,
#: and for the same reason: each is read by id on every query, through the same
#: statement, and past this many the file stops being cheaper than the scan. Bound
#: here once, so a test can steer it by patching this name.
MAX_CHANGED_IDS = config.VECTOR_INDEX_MAX_EXCLUDED_IDS

#: Why a build cannot vouch for its rows without the log, worded for an operator.
LOG_MISSING_REASON = "the database has no vector change log; start the server once to migrate it to schema 20"
LOG_OFF_REASON = "the vector change log is off; a build turns it on before it reads any row (build again)"


async def log_clock(db) -> tuple[int, int, int, int] | None:
    """(logging, generation, head, pruned_through), or None before schema 20."""
    import sqlite3

    try:
        rows = await db.execute_fetchall(
            "SELECT logging, generation, head, pruned_through FROM vector_log_clock WHERE id = 0"
        )
    except sqlite3.OperationalError:
        # An operator tool opens the database without migrating it.
        return None
    return tuple(rows[0]) if rows else None


async def start_logging_on(db) -> int | None:
    """Turn the change log on, on the caller's transaction; the generation it runs under.

    For a build that reads its rows on the transaction the caller holds: the rows
    are then read on the same transaction, so a write the log misses cannot land
    between the two. The server's own builds (the operator command and the
    coarse repair, which runs after its fix run commits) use ``start_logging``.
    """
    clock = await log_clock(db)
    if clock is None:
        return None
    if clock[0] == 1:
        return clock[1]
    await db.execute("UPDATE vector_log_clock SET logging = 1, generation = generation + 1 WHERE id = 0")
    return clock[1] + 1


async def start_logging() -> int | None:
    """Turn the change log on, in a commit of its own; the generation it runs under.

    Committed before a build reads its snapshot, so every write after that
    snapshot is logged. A write between this commit and the snapshot is logged
    and is in the file too, which costs nothing: reading a row live that the file
    holds unchanged gives the same row.
    """
    from cpersona.database import transaction

    async with transaction(scope_stats_neutral=True) as db:
        return await start_logging_on(db)


async def stop_logging(db) -> bool:
    """Turn the change log off and empty it, on the caller's transaction.

    A file is refused while logging is off, and after logging is next turned on
    its generation no longer matches, so no file built before the gap is read.
    """
    clock = await log_clock(db)
    if clock is None or clock[0] != 1:
        return False
    await db.execute("UPDATE vector_log_clock SET logging = 0, pruned_through = head WHERE id = 0")
    await db.execute("DELETE FROM record_vector_changes")
    return True


async def log_triggers_differ(db) -> list[str]:
    """The logging triggers that are missing or not as the schema defines them.

    A build refuses while any is: comparing the schema cookie later only notices
    a change made after the build, not a build that began without complete
    logging, and the writes such a gap missed would never be read live.
    """
    from cpersona import checks

    rows = await db.execute_fetchall(
        "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name IN ('memories', 'episodes')"
    )
    actual = {r[0]: r[1] or "" for r in rows}
    return [
        name
        for name in LOG_TRIGGERS
        if checks._normalize_sql(actual.get(name, "")) != checks._normalize_sql(checks._EXPECTED_OBJECTS[name]["sql"])
    ]


async def log_position(db) -> dict | str:
    """Where the change log stands for a build about to read its rows on ``db``.

    Read before the first row, on the connection the rows are read on: a write
    that commits after this read carries a higher number than the head returned
    here, so a reader of the file takes that row from the live table whatever the
    file holds for it. Read after the rows instead, a write between the two would
    be in neither. A string is the reason the build cannot vouch for its rows.
    """
    clock = await log_clock(db)
    if clock is None:
        return LOG_MISSING_REASON
    if clock[0] != 1:
        return LOG_OFF_REASON
    differ = await log_triggers_differ(db)
    if differ:
        return (
            f"the vector change log's triggers are missing or changed ({', '.join(differ)}); "
            "repair them with check_health(checks=['schema_objects'], fix=true)"
        )
    cookie = (await db.execute_fetchall("PRAGMA schema_version"))[0][0]
    return {"generation": int(clock[1]), "built_seq": int(clock[2]), "schema_cookie": int(cookie)}


def _changes_statement() -> str:
    """The log's clock, the schema cookie, and the ids changed since a build, in one statement.

    One statement is one snapshot: the ids and the clock they are checked against
    cannot come from two states of the database. The clock is the left side of the
    outer join, so the statement returns a row when nothing changed.
    """
    return (
        "SELECT c.logging, c.generation, c.head, c.pruned_through,"
        " (SELECT schema_version FROM pragma_schema_version), d.record_id"
        " FROM vector_log_clock AS c LEFT JOIN ("
        "SELECT DISTINCT record_id FROM record_vector_changes WHERE seq > ? AND kind = ? LIMIT ?"
        ") AS d ON 1 WHERE c.id = 0"
    )


async def changed_since_build(db, index, table: str) -> tuple[np.ndarray | None, str]:
    """The ids of `table` the log says changed since `index` was built, or why it cannot say.

    ``(ids, "")`` when the file can be read with those rows taken from the live
    table, ``(None, reason)`` when it cannot answer for its rows exactly and the
    caller must scan. ``index`` is a contiguous or a coarse index: both carry the
    generation, head and schema cookie of their build.
    """
    import sqlite3

    try:
        rows = await db.execute_fetchall(
            _changes_statement(), (index.built_seq, LOG_KINDS[table], MAX_CHANGED_IDS + 1)
        )
    except sqlite3.OperationalError:
        return None, "no_log"
    if not rows:
        return None, "no_log"
    logging_on, generation, head, pruned_through, cookie = rows[0][:5]
    if logging_on != 1:
        return None, "logging_off"
    if generation != index.generation:
        return None, "generation"
    if index.built_seq > head:
        return None, "newer_than_snapshot"
    if pruned_through > index.built_seq:
        return None, "pruned"  # changes it would need are gone
    if cookie != index.schema_cookie:
        return None, "schema_changed"  # a trigger dropped and recreated could have missed writes
    ids = [r[5] for r in rows if r[5] is not None]
    if len(ids) > MAX_CHANGED_IDS:
        return None, "too_many_changes"
    return np.array(sorted(int(i) for i in ids), dtype=np.int64), ""


#: What each refusal of `changed_since_build` means, for the health check and `status`.
LOG_REFUSALS = {
    "no_log": "the database has no vector change log",
    "logging_off": "the vector change log was turned off since the build",
    "generation": "the vector change log was restarted since the build",
    "newer_than_snapshot": "the file claims changes the database has not logged",
    "pruned": "a later build pruned changes this file needs",
    "schema_changed": "the schema changed since the build, so a change may have gone unlogged",
    "too_many_changes": f"more than {MAX_CHANGED_IDS} rows changed since the build",
}


def _header_log_position(src: str, magic: bytes) -> tuple[int, int] | None:
    """(generation, built_seq) from an index file's header, or None when it has none to give."""
    try:
        with open(src, "rb") as fh:
            head = fh.read(len(magic) + 4)
            if len(head) < len(magic) + 4 or head[: len(magic)] != magic:
                return None
            header_len = struct.unpack("<I", head[len(magic):])[0]
            header = json.loads(fh.read(header_len).decode("utf-8"))
        return int(header["generation"]), int(header["built_seq"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _log_readers() -> list[tuple[str, bytes]]:
    """Every file that reads the change log: (path, magic)."""
    from cpersona import coarse_index

    paths = [(index_path(table), MAGIC) for table in INDEXED_TABLES]
    paths += [(coarse_index.index_path(table), coarse_index.MAGIC) for table in coarse_index.COARSE_TABLES]
    return paths


async def prune_log_on(db) -> int | None:
    """Drop the log rows every file present holds, on the caller's transaction; the new mark.

    The mark is the lowest head among the files of the running generation: a file
    still needs every change after its own head. A file of another generation, or
    one that does not read, is refused by every reader already and holds nothing
    back. The mark only rises (`max`): a slower, older build finishing second must
    not lower it again, or a file whose changes were pruned would be read as current.
    A build still reading its rows is not a file yet, so a prune can pass its head;
    that file is then refused as pruned, and building it again makes it current.
    """
    clock = await log_clock(db)
    if clock is None or clock[0] != 1:
        return None
    heads = []
    for path, magic in _log_readers():
        position = _header_log_position(path, magic) if os.path.exists(path) else None
        if position is not None and position[0] == clock[1]:
            heads.append(position[1])
    if not heads:
        return None
    mark = min(heads)
    await db.execute("DELETE FROM record_vector_changes WHERE seq <= ?", (mark,))
    await db.execute(
        "UPDATE vector_log_clock SET pruned_through = max(pruned_through, ?) WHERE id = 0", (mark,)
    )
    return mark


async def prune_log() -> int | None:
    """`prune_log_on` in a transaction of its own, for a build that read its rows on a snapshot."""
    from cpersona.database import transaction

    async with transaction() as db:
        return await prune_log_on(db)


async def on_startup() -> None:
    """Stop the change log when no file reads it: a log nobody reads charges every write a row."""
    from cpersona.database import transaction

    if any(os.path.exists(path) for path, _magic in _log_readers()):
        return
    async with transaction() as db:
        if await stop_logging(db):
            logger.info("no vector index file is present: stopped the vector change log")


@dataclass(frozen=True)
class VectorIndex:
    """A validated index file, memory-mapped (read into memory on Windows, see _maps_files)."""

    path: str
    dim: int
    count: int
    watermark: int
    excluded_ids: tuple[int, ...]
    #: ids at or below the watermark that had no embedding when the index was
    #: built. Read exactly, like excluded_ids — see bug-278.
    unembedded_ids: tuple[int, ...]
    #: The change log at the build (bug-514): the rows logged after `built_seq`
    #: under `generation` are read exactly, like the two lists above.
    generation: int
    built_seq: int
    schema_cookie: int
    embedding_model: str
    scoring_version: str
    ids: np.ndarray  # int64[count]
    embeddings: np.ndarray  # float32[count][dim]
    agent_code: np.ndarray  # int32[count]
    project_code: np.ndarray
    channel_code: np.ndarray
    source_code: np.ndarray
    created_at: np.ndarray  # |S19[count]
    agents: tuple[str, ...]
    projects: tuple[str, ...]
    channels: tuple[str, ...]
    sources: tuple[str | None, ...]


class IndexUnusable(Exception):
    """The file exists but cannot be trusted — the caller falls back to the live scan.

    Raised rather than returned so that no caller can reach the arrays without
    having passed the checks: a half-validated index that silently answers with
    fewer rows is the one failure this design cannot afford, because nothing
    downstream can see it.
    """


def _canonical_predicate(alias: str = "") -> str:
    pre = f"{alias}." if alias else ""
    return f"{pre}created_at GLOB '{CANONICAL_CREATED_AT}'"


def _intern(values: list) -> tuple[list, dict]:
    """Sorted string table plus its lookup.

    Sorted rather than first-seen so the codes are a function of the *set* of
    values, not of the order rows happened to arrive in — one of the two things
    that make a rebuild byte-identical (the other is the absent timestamp).
    """
    table = sorted({v for v in values if v is not None})
    return table, {v: i for i, v in enumerate(table)}


@dataclass(frozen=True)
class RowPlan:
    """The rows a derived index of a table holds, decided once for every file built from it.

    The contiguous index and the coarse index (``cpersona.coarse_index``) are built
    from the same plan, so they hold the same rows in the same order, name the
    same holes and intern the same axis values. Two builders each deciding that
    for themselves would be two answers to "which rows does the index hold", while
    the query path merges both against one live tail.
    """

    table: str
    watermark: int
    width: int
    dim: int
    #: The metadata rows, in canonical order: id, agent_id, project_id, channel,
    #: created_at, length(embedding), then the source id for memories, then
    #: ``extra_columns`` from ``extra_offset`` on.
    kept: list
    excluded: list
    unembedded: list
    agents: list
    projects: list
    channels: list
    sources: list
    agent_ix: dict
    project_ix: dict
    channel_ix: dict
    source_ix: dict
    row_sources: list
    extra_offset: int

    @property
    def count(self) -> int:
        return len(self.kept)

    def axis_code_arrays(self) -> list:
        """The four axis columns as int32 bytes, in the order both file formats write them."""
        return [
            np.array([self.agent_ix[r[1]] for r in self.kept], dtype="<i4").tobytes(),
            np.array([self.project_ix[r[2]] for r in self.kept], dtype="<i4").tobytes(),
            np.array([self.channel_ix[r[3]] for r in self.kept], dtype="<i4").tobytes(),
            np.array(
                [NULL_CODE if value is None else self.source_ix[value] for value in self.row_sources],
                dtype="<i4",
            ).tobytes(),
        ]


async def plan_rows(db, table: str = "memories", *, extra_columns: tuple = ()) -> "RowPlan | dict":
    """Decide the row set, its order, its named holes and its string tables.

    Returns the declining result -- the same dict the builder returns -- when the
    corpus cannot be indexed as it stands. ``extra_columns`` are SQL expressions
    appended to each metadata row, for a file that carries a column the
    contiguous index does not; they never change which rows are kept.

    Read-only, and a deliberate global scan, spelled the way the isolation gate
    requires one to be spelled: the axes ride as columns, and the authority is
    re-applied when a caller hydrates.
    """
    iso = isolation_where(agent_id=None)
    # json_valid first: json_extract raises on a source that is not JSON, and this
    # statement reads every embedded row of every agent, so one such row would stop
    # both index files from being built at all -- for good when the row is locked,
    # because the JSON repair leaves locked rows alone. The row is indexed with no
    # source id instead, which matches no per-user filter, as NULL matches nothing.
    src_expr = (
        ", CASE WHEN json_valid(source) THEN json_extract(source, '$.id') END" if table == "memories" else ""
    )
    extra_expr = "".join(f", {column}" for column in extra_columns)

    row = await db.execute_fetchall(f"SELECT MAX(id) FROM {table}{iso.where}", iso.params)
    watermark = int(row[0][0] or 0)

    meta_sql = (
        f"SELECT id, agent_id, project_id, channel, created_at, length(embedding){src_expr}{extra_expr}"
        f" FROM {table}"
        f" WHERE embedding IS NOT NULL AND id <= ?{iso.and_clause}"
        f" ORDER BY created_at DESC, id ASC"
    )
    meta = await db.execute_fetchall(meta_sql, (watermark, *iso.params))
    if not meta:
        return {"built": False, "reason": "no embedded rows", "watermark": watermark}

    # bug-278: the rows at or below the watermark that carry no embedding YET. The
    # watermark answers "did this row exist at build time"; it cannot answer "did this
    # row have an embedding at build time", and those are different questions for any
    # row that gets embedded later — which is what check_health(fix=True) does to every
    # NULL row it finds. Such a row is in neither the matrix (the meta query requires an
    # embedding) nor the tail (its id is at or below the watermark), so once its
    # embedding lands it is returned by the scan and by nothing else. Naming them here
    # puts them in the same exact tail read the non-canonical rows already ride.
    null_sql = f"SELECT id FROM {table} WHERE embedding IS NULL AND id <= ?{iso.and_clause}"
    unembedded = [int(r[0]) for r in await db.execute_fetchall(null_sql, (watermark, *iso.params))]

    # A single width, or no index. The live scan applies its window BEFORE it
    # skips foreign-width rows: it ranks whatever survives inside the newest
    # MAX_MEMORIES rows. An index that holds only one width cannot reproduce that
    # window while other widths exist — it would rank the newest MAX_MEMORIES
    # rows *of its own width*, which is more rows, and more rows is a different
    # answer even when every one of them is scored identically.
    #
    # So a mixed-dimension corpus declines rather than approximating. That state
    # is what a model swap looks like from here, and it is transient by
    # construction: the scan this index replaces stays correct throughout, only
    # slower, which is the trade this whole design is built to make safely.
    widths = {r[5] for r in meta}
    if len(widths) > 1:
        return {
            "built": False,
            "reason": f"corpus carries {len(widths)} embedding widths ({sorted(widths)})",
            "watermark": watermark,
        }
    width = widths.pop()
    if not width or width % 4:
        return {"built": False, "reason": f"embedding width {width} is not float32-aligned"}
    dim = width // 4

    # A row whose created_at this fixed-width format cannot spell IS an
    # exclusion, and stays reachable because the query path unions the list into
    # its exact tail read.
    kept, excluded = [], []
    for r in meta:
        if not _is_canonical(r[4]):
            excluded.append(int(r[0]))
            continue
        kept.append(r)

    # Both lists are bound into the same IN clause on every query, so the cap is on
    # their sum. Declining is the same answer a mixed-width corpus already gets: an
    # index that cannot name all its holes would be approximate, and this file format
    # does not approximate.
    if len(excluded) + len(unembedded) > MAX_EXCLUDED_IDS:
        return {
            "built": False,
            "reason": (
                f"{len(excluded)} rows carry a non-canonical created_at and "
                f"{len(unembedded)} carry no embedding yet (cap {MAX_EXCLUDED_IDS} combined)"
            ),
            "watermark": watermark,
        }
    count = len(kept)
    if count == 0:
        return {"built": False, "reason": "no rows survive the format", "watermark": watermark}

    agents, agent_ix = _intern([r[1] for r in kept])
    projects, project_ix = _intern([r[2] for r in kept])
    channels, channel_ix = _intern([r[3] for r in kept])
    # str() at the boundary (bug-276). `json_extract(source, '$.id')` returns
    # whatever type the JSON held, and SQLite hands back a JSON number as an int.
    # Mixing types here is not a filtering problem, it is a build failure: the
    # sorted string table compares its values, and `int < str` raises. Normalising
    # once, where the column is read, also makes the table match what the scan
    # compares against — its LIKE coerces the number to text — so the two paths
    # answer the same question rather than two different ones.
    #
    # Computed once and used for both the table and the per-row codes below: two
    # spellings of the same normalisation is how one of them ends up looking the
    # value up under a key the other never wrote.
    row_sources = (
        [None if r[6] is None else str(r[6]) for r in kept] if table == "memories" else [None] * len(kept)
    )
    sources, source_ix = _intern(row_sources)

    return RowPlan(
        table=table, watermark=watermark, width=width, dim=dim, kept=kept,
        excluded=excluded, unembedded=unembedded, agents=agents, projects=projects,
        channels=channels, sources=sources, agent_ix=agent_ix, project_ix=project_ix,
        channel_ix=channel_ix, source_ix=source_ix, row_sources=row_sources,
        extra_offset=6 + (1 if table == "memories" else 0),
    )


async def build_index(db, table: str = "memories", path: str | None = None) -> dict:
    """Write the index for `table`. Read-only against the database.

    Two passes. The first reads every column except the embedding —
    `length(embedding)` rather than the blob itself, which is the whole point: it
    yields integers, not 3 KB Python objects. It fixes the row set, the
    dimension, the string tables and the exclusions. The second streams only the
    blobs, under a predicate built from the first pass's findings, and must
    produce exactly the rows the first pass counted.

    The two passes are two statements, and on a connection with no transaction
    open each reads its own snapshot (bug-589): a commit between them can delete
    one planned row and give an embedding to a row the plan skipped, which leaves
    the count unchanged and shifts every vector between the two rows one slot
    against its id. So the operator command builds inside one read transaction
    (`database.read_snapshot()`), and the second pass checks the id of every row
    it reads against the plan's order rather than only their count: whatever
    connection a caller passes, a file whose embeddings are shifted against their
    ids -- the one corruption that would still pass every length check below --
    is never written. A mismatch aborts the build; building again succeeds.

    The index spans every agent (the axes ride as columns, and the authority is
    re-applied when the caller hydrates), so this is a deliberate global scan,
    spelled the way the isolation gate requires one to be spelled.

    The change log must be on before ``db`` reads its first row (bug-514): the
    file records the log's head, and a reader takes every row logged after it
    from the live table. ``start_logging`` turns it on in a commit of its own,
    before a snapshot is opened, as the operator command and the coarse repair
    do; ``start_logging_on`` on a transaction the caller holds. Without it the
    build declines, and the caller prunes the log after the file is published
    (``prune_log``).
    """
    out = path or index_path(table)
    # Read before any row: a purge counted after this may have committed after the
    # rows below were read, and its files must not come back with them.
    purges_at_start = purges()
    position = await log_position(db)
    if isinstance(position, str):
        return {"built": False, "reason": position}
    plan = await plan_rows(db, table)
    if isinstance(plan, dict):
        return plan
    watermark, width, dim, count = plan.watermark, plan.width, plan.dim, plan.count
    kept, excluded, unembedded = plan.kept, plan.excluded, plan.unembedded
    agents, projects, channels, sources = plan.agents, plan.projects, plan.channels, plan.sources

    header = {
        "format": FORMAT_VERSION,
        "table": table,
        "dim": dim,
        "dtype": "float32",
        "count": count,
        "watermark": watermark,
        "excluded_ids": excluded,
        "unembedded_ids": unembedded,
        **position,
        "fingerprint": {
            # The dimension is the part that actually guards. This codebase
            # already treats EMBEDDING_MODEL as a label that can be stale or
            # plain wrong (it defaults to a name nothing verifies), so it is
            # recorded for a human reading the file rather than relied upon.
            "dim": dim,
            "embedding_model": config.EMBEDDING_MODEL,
            "scoring_version": SCORING_VERSION,
        },
        "agents": agents,
        "projects": projects,
        "channels": channels,
        "sources": sources,
    }
    blob = json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")
    blob += b" " * ((-(len(MAGIC) + 4 + len(blob))) % HEADER_ALIGN)

    # A temp name of this build's own: two builds of this file can overlap (the
    # command-line build beside a server's), and a name derived from the output
    # path alone made them write one inode, so the first rename published a file
    # the second build was still writing into.
    tmp = fileperms.temp_beside(out)
    try:
        with fileperms.open_private(tmp, "wb") as fh:
            fh.write(MAGIC)
            fh.write(struct.pack("<I", len(blob)))
            fh.write(blob)
            planned_ids = [r[0] for r in kept]
            fh.write(np.array(planned_ids, dtype="<i8").tobytes())

            written = await _stream_embeddings(db, fh, table, watermark, width, planned_ids)
            if written != count:
                raise IndexUnusable(
                    f"embedding pass returned {written} rows, metadata pass counted {count}"
                )

            for column in plan.axis_code_arrays():
                fh.write(column)
            fh.write(b"".join(r[4].encode("ascii") for r in kept))
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        # A half-written temp file is not the index and must not be left where a
        # later build could mistake it for a resumable one.
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise

    if purged_since(purges_at_start):
        os.unlink(tmp)
        return {"built": False, "reason": SUPERSEDED_REASON}
    os.replace(tmp, out)
    return {
        "built": True,
        "path": out,
        "count": count,
        "dim": dim,
        "watermark": watermark,
        "excluded": len(excluded),
        # bug-388: the other named hole. The cap above is checked against the
        # sum of both, and both are read by id on every query, so reporting one
        # of them made a build with thousands of holes look like a build with none.
        "unembedded": len(unembedded),
        "generation": position["generation"],
        "built_seq": position["built_seq"],
        "bytes": os.path.getsize(out),
    }

#: bug-368: str.isdigit() is true for digits SQLite's `[0-9]` class is not --
#: fullwidth forms, other scripts' digits, superscripts. The two predicates are
#: documented as the same test, and a row that answered true here and false in
#: SQL made the builder raise its snapshot-isolation mismatch (the embedding pass
#: returned one row, the metadata pass counted two) and delete its temp file, so
#: the corpus could not be indexed at all until the row was found by hand -- with
#: the message pointing at a concurrent writer that did not exist.
_ASCII_DIGITS = frozenset("0123456789")


def _is_canonical(value: str) -> bool:
    return (
        len(value) == CREATED_AT_WIDTH
        and value[4] == value[7] == "-"
        and value[10] == " "
        and value[13] == value[16] == ":"
        and all(value[i] in _ASCII_DIGITS for i in (0, 1, 2, 3, 5, 6, 8, 9, 11, 12, 14, 15, 17, 18))
    )


async def iter_embedding_chunks(db, table: str, watermark: int, width: int, planned_ids):
    """The blobs of the planned rows in canonical order, a chunk at a time.

    The predicate mirrors the metadata pass exactly — same watermark, same width,
    same canonical-created_at test, same ORDER BY — so the two passes describe one
    row set. Global by design, like the first pass. Both index files read their
    second pass through here, so they cannot disagree about which blob is which row.

    The predicate makes the two passes describe one row set only when they read
    one state of the database (bug-589). Each row's id is therefore read with its
    blob and checked against `planned_ids`, the metadata pass's ids in the same
    order, and a difference raises `IndexUnusable` before the chunk is yielded. A
    row the plan did not count shows up as a difference too; a row it counted and
    this pass did not reach is left to the caller's count check.
    """
    iso = isolation_where(agent_id=None)
    sql = (
        f"SELECT id, embedding FROM {table}"
        f" WHERE embedding IS NOT NULL AND id <= ? AND length(embedding) = ?"
        f"   AND {_canonical_predicate()}{iso.and_clause}"
        f" ORDER BY created_at DESC, id ASC"
    )
    seen = 0
    async with db.execute(sql, (watermark, width, *iso.params)) as cursor:
        while True:
            rows = await cursor.fetchmany(512)
            if not rows:
                return
            ids = [r[0] for r in rows]
            if ids != list(planned_ids[seen : seen + len(ids)]):
                raise IndexUnusable(
                    "the embedding pass read a different row set than the metadata pass "
                    "(a write landed between them); build again"
                )
            seen += len(ids)
            yield [r[1] for r in rows]


async def _stream_embeddings(db, fh, table: str, watermark: int, width: int, planned_ids) -> int:
    """Append the blobs in canonical order, without holding them all at once."""
    written = 0
    async for blobs in iter_embedding_chunks(db, table, watermark, width, planned_ids):
        fh.write(b"".join(blobs))
        written += len(blobs)
    return written


def _maps_files() -> bool:
    """Whether a loaded index maps its file (POSIX) or reads it into memory (Windows).

    A server keeps the index it loaded for as long as it runs (``cached_index``), and
    a rebuild moves a new file over the old one. Windows refuses to replace a file that
    is still mapped — ``PermissionError: [WinError 5]`` at the ``os.replace`` in
    ``build_index`` — so on Windows every rebuild failed while any process held the
    index, which is whenever the server had searched since it started (a production
    report; reproduced in CI). Reading the arrays leaves no handle open, and it is not
    free: the whole file -- every agent's rows, the float32 matrix included -- stays
    resident for as long as the server keeps the index, where the scan it replaces holds
    one chunk of one agent's window at a time. ``cached_index`` lets go of the index a
    rebuild replaced before it reads the new one, so a reload does not hold two copies.
    POSIX allows the replace, so it keeps the mapping, which the OS pages in and out.
    """
    return os.name != "nt"


def load_index(table: str = "memories", path: str | None = None) -> VectorIndex | None:
    """Map (or, on Windows, read) an index file, or return None when there is none.

    None means "no index", which is not an error — it is the ordinary state
    before the first build and after a deletion. A file that exists but does not
    hold together raises `IndexUnusable`: the difference matters because the
    second one is worth reporting and the first is not.
    """
    src = path or index_path(table)
    if not os.path.exists(src):
        return None
    # One open for the header, the size check and every array: a rebuild moves a new
    # file over this path, and a loader that reopened it by path for each array would,
    # when the move landed between two of them, pair this header's offsets and counts
    # with the other file's bytes -- and nothing would raise when that file is larger.
    with open(src, "rb") as fh:
        return _load_open(fh, src)


def _read_header(fh, src: str, magic: bytes = MAGIC) -> tuple[dict, int, int]:
    """The header of the open index file: `(header, base, size)`.

    `base` is where the arrays begin. `size` is the size of the file this handle
    holds, not of whatever the path names by now, so it is checked against the
    same file the arrays are read from. `magic` is the file kind's own.
    """
    size = os.fstat(fh.fileno()).st_size
    head = fh.read(len(magic) + 4)
    if len(head) < len(magic) + 4 or head[: len(magic)] != magic:
        raise IndexUnusable(f"{src}: bad magic")
    header_len = struct.unpack("<I", head[len(magic):])[0]
    raw = fh.read(header_len)
    if len(raw) != header_len:
        raise IndexUnusable(f"{src}: header truncated")
    try:
        header = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise IndexUnusable(f"{src}: header is not JSON ({exc})") from exc
    return header, len(magic) + 4 + header_len, size


def _array_reader(fh):
    """`read(offset, dtype, shape)`: one array of the open file `fh`.

    Mapped (POSIX) or read into memory (Windows, see `_maps_files`), and from the
    handle either way: a mapping made from it outlives the handle, and a read
    from it reads the file the header came from.
    """
    mapped = _maps_files()

    def read(offset: int, dtype: str, shape) -> np.ndarray:
        if mapped:
            return np.memmap(fh, dtype=dtype, mode="r", offset=offset, shape=shape)
        items = int(np.prod(shape))
        fh.seek(offset)
        return np.fromfile(fh, dtype=dtype, count=items).reshape(shape)

    return read


def _load_open(fh, src: str) -> VectorIndex:
    header, base, size = _read_header(fh, src)

    if header.get("format") != FORMAT_VERSION:
        raise IndexUnusable(f"{src}: format {header.get('format')} != {FORMAT_VERSION}")

    try:
        dim, count = int(header["dim"]), int(header["count"])
    except (KeyError, TypeError, ValueError) as exc:
        # A header that parsed as JSON but does not spell its own geometry is a
        # file that does not hold together, which is what IndexUnusable means.
        # Raising KeyError/ValueError instead would escape the caller's guard —
        # the same shape of hole as the unguarded select() call (bug-276).
        raise IndexUnusable(f"{src}: header lacks a usable dim/count ({exc!r})") from exc
    expect = base + count * (8 + dim * 4 + 4 * len(_AXIS_FIELDS) + CREATED_AT_WIDTH)
    if size != expect:
        # The one check that catches a build killed halfway, a truncated copy, and
        # a header that disagrees with its own body — all as the same condition.
        raise IndexUnusable(f"{src}: expected {expect} bytes for {count} rows of {dim}d, found {size}")

    # One array per field, addressed by offset, rather than one uint8 buffer that is
    # re-viewed: a view across a slice has to satisfy numpy's alignment rules for
    # the target dtype, and expressing the offsets here keeps the layout in the
    # code that reads it instead of in a chain of pointer arithmetic.
    _map = _array_reader(fh)

    off = base
    ids = _map(off, "<i8", (count,))
    off += count * 8
    emb = _map(off, "<f4", (count, dim))
    off += count * dim * 4
    codes = {}
    for field in _AXIS_FIELDS:
        codes[field] = _map(off, "<i4", (count,))
        off += count * 4
    created = _map(off, f"S{CREATED_AT_WIDTH}", (count,))

    fingerprint = header.get("fingerprint", {})
    try:
        generation, built_seq = int(header["generation"]), int(header["built_seq"])
        schema_cookie = int(header["schema_cookie"])
    except (KeyError, TypeError, ValueError) as exc:
        raise IndexUnusable(f"{src}: header lacks the change log's position ({exc!r})") from exc
    return VectorIndex(
        path=src,
        dim=dim,
        count=count,
        watermark=int(header["watermark"]),
        excluded_ids=tuple(int(i) for i in header.get("excluded_ids", ())),
        unembedded_ids=tuple(int(i) for i in header.get("unembedded_ids", ())),
        generation=generation,
        built_seq=built_seq,
        schema_cookie=schema_cookie,
        embedding_model=str(fingerprint.get("embedding_model", "")),
        scoring_version=str(fingerprint.get("scoring_version", "")),
        ids=ids,
        embeddings=emb,
        agent_code=codes["agent_code"],
        project_code=codes["project_code"],
        channel_code=codes["channel_code"],
        source_code=codes["source_code"],
        created_at=created,
        agents=tuple(header.get("agents", ())),
        projects=tuple(header.get("projects", ())),
        channels=tuple(header.get("channels", ())),
        sources=tuple(header.get("sources", ())),
    )


# --------------------------------------------------------------------------------------
# Operator entry point: `python -m cpersona.vector_index build|status`.
# --------------------------------------------------------------------------------------


def _build_parser():
    import argparse

    ap = argparse.ArgumentParser(
        prog="python -m cpersona.vector_index",
        description=(
            "Build or inspect the contiguous embedding index, a derived file beside "
            "the database that the local vector scan reads instead of SQLite rows, "
            "and for memories the coarse (one-bit) index beside it. Neither is ever "
            "repaired: delete them and build again."
        ),
    )
    ap.add_argument(
        "--db",
        help=(
            "Path to the cpersona SQLite database. Resolution order: the --db value, "
            "else $CPERSONA_DB_PATH, else the relative 'data/cpersona.db' from the "
            "current working directory. The index is written beside it."
        ),
    )
    ap.add_argument("--table", default=INDEXED_TABLES[0], choices=INDEXED_TABLES)
    ap.add_argument("--json", action="store_true", help="Emit the result as JSON")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "build",
        help="Write the index from the database, and for memories the coarse index "
        "too. The rows are only read; the one write is the change log's switch, turned "
        "on before the rows are read, and its pruning after. Exit 0 when every file was "
        "built, 1 when a builder declined (the reason is printed), 2 on error.",
    )
    sub.add_parser(
        "status",
        help="Report the index beside the database. Exit 0 when a usable index is "
        "present, 1 when there is none, 2 when the file exists but cannot be used.",
    )
    return ap


async def rows_read_exactly(db, index: VectorIndex, table: str, iso, changed=()) -> int:
    """How far behind the database `index` is: the rows every query reads from the
    table instead of from the index file.

    bug-388: three groups, the ones ``vector._index_tail_rows`` reads -- rows
    written since the build (``id > watermark``), and the two groups the build
    named because it could not hold them (``excluded_ids``, a created_at the
    format cannot spell; ``unembedded_ids``, no embedding yet when it ran). A
    named hole costs a read only once it carries an embedding, and filling a
    NULL embedding is what ``check_health(fix=True)`` does, so the ordinary
    repair turns a hole the build named into a per-query cost. Counting only the
    first group reported zero while every query paid for the other two.

    The predicate is the query path's own, so this number and the rows a query
    reads cannot disagree (tests/test_bug388_index_behind.py holds the two
    together). ``status`` and the health check both call this, so "behind" has
    one definition. ``iso`` is the caller's isolation filter: global for the
    operator's ``status``, the checked agent's for the health check.

    ``changed`` is the fourth group (bug-514): the rows the change log says
    changed since the build (``changed_since_build``), read by id like the holes.
    """
    holes_ids = tuple(index.excluded_ids) + tuple(index.unembedded_ids) + tuple(int(i) for i in changed)
    holes = " OR id IN (SELECT value FROM json_each(?))" if holes_ids else ""
    holes_params = (json.dumps([int(i) for i in holes_ids]),) if holes_ids else ()
    rows = await db.execute_fetchall(
        f"SELECT COUNT(*) FROM {table}"
        f" WHERE (id > ?{holes}) AND embedding IS NOT NULL{iso.and_clause}",
        (index.watermark, *holes_params, *iso.params),
    )
    return int(rows[0][0])


async def _status(table: str) -> dict:
    """What an operator can act on: present / usable / how far behind the database."""
    import datetime as _dt

    path = index_path(table)
    if not os.path.exists(path):
        return {"table": table, "path": path, "present": False}
    try:
        index = load_index(table)
    except IndexUnusable as exc:
        return {
            "table": table, "path": path, "present": True, "usable": False,
            "reason": str(exc),
            "hint": "delete the file and build again; it is a derived artifact and is never repaired",
        }
    assert index is not None
    from cpersona.database import connection

    # Deliberate global counts, spelled the way the isolation gate requires one to
    # be spelled. `rows_since_build` keeps its meaning -- rows WRITTEN since the
    # build -- and `rows_read_exactly` is how far behind the index is (bug-388):
    # the named holes were written before the build, so folding them into the
    # first number would make it answer the second question by misstating the first.
    iso = isolation_where(agent_id=None)
    async with connection() as db:
        changed, refusal = await changed_since_build(db, index, table)
        if changed is None:
            return {
                "table": table, "path": path, "present": True, "usable": False,
                "reason": LOG_REFUSALS[refusal],
                "hint": "build again; every query reads the live table until then",
            }
        row = await db.execute_fetchall(
            f"SELECT COUNT(*) FROM {table} WHERE id > ? AND embedding IS NOT NULL{iso.and_clause}",
            (index.watermark, *iso.params),
        )
        read_exactly = await rows_read_exactly(db, index, table, iso, changed)
    st = os.stat(path)
    return {
        "table": table,
        "path": path,
        "present": True,
        "usable": True,
        "rows_changed_since_build": len(changed),
        "rows": index.count,
        "dim": index.dim,
        "watermark": index.watermark,
        "rows_since_build": int(row[0][0]),
        "excluded": len(index.excluded_ids),
        "unembedded": len(index.unembedded_ids),
        "rows_read_exactly": read_exactly,
        "bytes": st.st_size,
        "built_at": _dt.datetime.fromtimestamp(st.st_mtime, tz=_dt.timezone.utc).isoformat(),
    }


def _render(result: dict, as_json: bool) -> str:
    if as_json:
        return json.dumps(result, sort_keys=True)
    if "coarse" in result:
        own = {k: v for k, v in result.items() if k != "coarse"}
        return f"{_render(own, False)}\ncoarse: {_render(result['coarse'], False)}"
    if "built" in result:
        if result["built"]:
            return (
                f"built {result['path']}: {result['count']} rows x {result['dim']} dims, "
                f"watermark {result['watermark']}, {result['bytes']} bytes"
            )
        return f"not built: {result.get('reason', 'unknown reason')}"
    if not result["present"]:
        return f"no index at {result['path']} (the scan reads SQLite rows directly)"
    if not result["usable"]:
        return f"unusable index at {result['path']}: {result['reason']} -- {result['hint']}"
    return (
        f"index at {result['path']}: {result['rows']} rows x {result['dim']} dims, "
        f"watermark {result['watermark']}, {result['rows_since_build']} rows written and "
        f"{result['rows_changed_since_build']} changed since "
        f"the build, {result['rows_read_exactly']} rows read exactly on every query "
        f"({result['excluded'] + result['unembedded']} named at the build), "
        f"built {result['built_at']}"
    )


def main(argv: list | None = None) -> int:
    import asyncio
    import sys

    args = _build_parser().parse_args(argv)
    if args.db:
        if not os.path.exists(args.db):
            print(f"error: database not found: {args.db}", file=sys.stderr)
            return 2
        # Both readers of the path: the module attribute (index_path) and the
        # environment the database module reads when it is first imported.
        os.environ["CPERSONA_DB_PATH"] = args.db
        config.DB_PATH = args.db

    async def run() -> int:
        # Reads go through a read seam, not get_db()'s connection: the
        # commit/rollback boundary belongs to database.py, and a builder that only
        # reads has no business owning one.
        from cpersona import database
        from cpersona.database import close_db, connection, read_snapshot

        # An operator tool does not migrate, and must not create a database behind
        # a mistyped path. Restored afterwards: this is process state, and the
        # in-process callers of main() (tests) share the process.
        skip_before = database.SKIP_BOOT_MIGRATIONS
        database.SKIP_BOOT_MIGRATIONS = True
        try:
            from cpersona import coarse_index

            if args.command == "build":
                # One read transaction for both passes of both builders (bug-589):
                # the documented way to run this is from a timer against a live
                # server, and the shared read connection gives each statement its
                # own snapshot. Entering the read seam first keeps the refusal
                # above: the snapshot's private connection would create a missing file.
                async with connection():
                    pass
                # The change log goes on in a commit of its own, before the snapshot
                # opens, so every write the snapshot does not see is logged (bug-514).
                # On a database no server has migrated yet, the builders decline.
                await start_logging()
                async with read_snapshot() as db:
                    result = await build_index(db, args.table)
                    if args.table in coarse_index.COARSE_TABLES:
                        # The same command builds both (design §10 B): an operator who
                        # builds one index is not left to discover the other.
                        result["coarse"] = await coarse_index.build_coarse_index(db, args.table)
                built = [result] + ([result["coarse"]] if "coarse" in result else [])
                if any(r.get("built") for r in built):
                    await prune_log()
                code = 0 if all(r.get("built") for r in built) else 1
            else:
                result = await _status(args.table)
                if args.table in coarse_index.COARSE_TABLES:
                    # Reported, not scored: the coarse index is read only when a setting
                    # asks for it, so its absence does not make the contiguous one unusable.
                    result["coarse"] = await coarse_index.status(args.table)
                code = 0 if result.get("usable") else (2 if result["present"] else 1)
        except Exception as exc:  # noqa: BLE001 — the reason must reach the operator
            print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        finally:
            database.SKIP_BOOT_MIGRATIONS = skip_before
            # Every aiosqlite connection owns a non-daemon worker thread; without
            # this the process would not exit after printing.
            await close_db()
        print(_render(result, args.json))
        return code

    return asyncio.run(run())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


# --------------------------------------------------------------------------------------
# Query side: selection, and the cache that keeps a rebuild visible.
# --------------------------------------------------------------------------------------

_cache: dict[str, tuple[tuple, VectorIndex | None]] = {}


def _stat_key(src: str) -> tuple:
    st = os.stat(src)
    # size and mtime_ns together: a rebuild writes a new inode through os.replace,
    # and even an identical-size rebuild moves mtime. Cheap enough to stat on every
    # query, which is what keeps "rebuild frequency is a performance knob" true —
    # a cache that had to be invalidated by hand would put correctness back into it.
    return (st.st_size, st.st_mtime_ns, st.st_ino)


def cached_index(table: str = "memories", path: str | None = None) -> VectorIndex | None:
    """`load_index`, but re-mapping only when the file on disk has changed.

    Raises `IndexUnusable` exactly as `load_index` does; a bad file is not
    cached, so a rebuild that fixes it is picked up on the next call.
    """
    src = path or index_path(table)
    if not os.path.exists(src):
        _cache.pop(src, None)
        return None
    key = _stat_key(src)
    hit = _cache.get(src)
    if hit is not None and hit[0] == key:
        return hit[1]
    # Let go of the replaced index before reading its successor. On Windows the
    # arrays are read into memory (`_maps_files`), so holding the old entry through
    # the load would keep two copies of the whole corpus resident at once.
    _cache.pop(src, None)
    index = load_index(table, src)
    _cache[src] = (key, index)
    return index


def _axis_codes(table: tuple, allowed) -> np.ndarray:
    """Codes for the values this axis admits, as an int32 array for isin()."""
    return np.array([i for i, v in enumerate(table) if v in allowed], dtype="<i4")


def select(
    index: VectorIndex,
    *,
    agent_id: str,
    project_id: str | None,
    channel: str,
    source_id: str,
    limit: int,
    exclude_ids=None,
) -> np.ndarray:
    """Positions of the rows a scan with these axes would rank, newest first.

    Mirrors `isolation_where()` axis for axis — that helper stays the authority,
    and the hydrate re-applies it; this is the index doing enough filtering that
    the top-k cut is taken over the right rows rather than the whole corpus. The
    obligation is one-directional: this may return rows the authority would drop
    (they are dropped later, at the cost of some wasted work) but never drop a
    row the authority admits.

    Positions come back in the file's canonical order — `created_at` DESC, then
    `id` ASC — which is the tie-break the caller's answer depends on.

    `exclude_ids` are the rows the change log says changed since the build
    (bug-514). What the file holds for them is out of date, so they are left out
    before the limit is taken -- the caller reads them from the live table and
    merges them in, and a limit taken first would leave the selection one row
    short for every changed row inside it.
    """
    mask = index.agent_code == _code_of(index.agents, agent_id)

    if exclude_ids is not None and len(exclude_ids):
        mask &= ~np.isin(index.ids, np.asarray(exclude_ids, dtype=np.int64))

    if project_id is not None:
        # γ semantics: 'X' is the union of 'X' and the global pool, '' is the
        # global pool alone, and None (above) filters nothing.
        allowed = {""} if project_id == "" else {project_id, ""}
        mask &= np.isin(index.project_code, _axis_codes(index.projects, allowed))

    if channel:
        # knob2 v2: a channel-scoped read still sees the channel-global rows, and
        # an empty channel is not a filter at all.
        mask &= np.isin(index.channel_code, _axis_codes(index.channels, {channel, ""}))

    if source_id:
        # The SQL is `source_id_where` over a JSON field: one id compared whole,
        # or a namespace compared as a prefix. Resolved here against the
        # header's string table, which is small because the values are: the
        # per-row test is integer membership. A NULL source id carries NULL_CODE
        # and matches nothing, exactly as the SQL comparison against NULL does.
        # `source_id_matches` draws the id/namespace line in the one place the
        # SQL draws it, so the two arms answer the same question.
        codes = np.array(
            # str() inside the helper: `json_extract(source, '$.id')` hands back
            # whatever type the JSON held, and SQLite returns a JSON number as an
            # int. The scan this mirrors compares the number as text and matches
            # it (bug-276) — so a non-string id must be compared the same way
            # here, not raise. The obligation above is one-directional: matching
            # a row the authority would drop costs wasted work, while raising
            # takes down the query the index exists to accelerate.
            [i for i, v in enumerate(index.sources) if source_id_matches(v, source_id)],
            dtype="<i4",
        )
        mask &= np.isin(index.source_code, codes)

    positions = np.flatnonzero(mask)
    return positions[:limit] if limit and len(positions) > limit else positions


def _code_of(table: tuple, value: str) -> int:
    try:
        return table.index(value)
    except ValueError:
        return NULL_CODE  # an agent with no rows in the index: matches nothing
