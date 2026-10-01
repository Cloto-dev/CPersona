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
import os
import struct
from dataclasses import dataclass

import numpy as np

from cpersona import config
from cpersona import fileperms
from cpersona.isolation import isolation_where
from cpersona.utils import SCORING_VERSION

MAGIC = b"CPXIDX01"
# 2 (bug-278): the header gained unembedded_ids. A sidecar written by the previous
# builder has no record of the rows it skipped for a NULL embedding, so a new reader
# cannot make it correct — it can only be rebuilt. Bumping the version turns those
# files into IndexUnusable, which the query path already answers by using the scan.
FORMAT_VERSION = 2
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
    src_expr = ", json_extract(source, '$.id')" if table == "memories" else ""
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

    Both passes read on the seam's read connection, which WAL gives snapshot
    isolation from the serialised writer. The second pass's row count is checked
    against the first anyway: snapshot isolation is an argument, and an argument
    is not a check. A mismatch aborts the build rather than leaving a file whose
    embeddings are shifted by a row against their ids — the one corruption that
    would still pass every length check below.

    The index spans every agent (the axes ride as columns, and the authority is
    re-applied when the caller hydrates), so this is a deliberate global scan,
    spelled the way the isolation gate requires one to be spelled.
    """
    out = path or index_path(table)
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

    tmp = f"{out}.tmp"
    try:
        with fileperms.open_private(tmp, "wb") as fh:
            fh.write(MAGIC)
            fh.write(struct.pack("<I", len(blob)))
            fh.write(blob)
            fh.write(np.array([r[0] for r in kept], dtype="<i8").tobytes())

            written = await _stream_embeddings(db, fh, table, watermark, width)
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


async def iter_embedding_chunks(db, table: str, watermark: int, width: int):
    """The blobs of the planned rows in canonical order, a chunk at a time.

    The predicate mirrors the metadata pass exactly — same watermark, same width,
    same canonical-created_at test, same ORDER BY — so the two passes describe one
    row set. Global by design, like the first pass. Both index files read their
    second pass through here, so they cannot disagree about which blob is which row.
    """
    iso = isolation_where(agent_id=None)
    sql = (
        f"SELECT embedding FROM {table}"
        f" WHERE embedding IS NOT NULL AND id <= ? AND length(embedding) = ?"
        f"   AND {_canonical_predicate()}{iso.and_clause}"
        f" ORDER BY created_at DESC, id ASC"
    )
    async with db.execute(sql, (watermark, width, *iso.params)) as cursor:
        while True:
            rows = await cursor.fetchmany(512)
            if not rows:
                return
            yield [r[0] for r in rows]


async def _stream_embeddings(db, fh, table: str, watermark: int, width: int) -> int:
    """Append the blobs in canonical order, without holding them all at once."""
    written = 0
    async for blobs in iter_embedding_chunks(db, table, watermark, width):
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
    report; reproduced in CI). Reading the arrays costs what the scan already touches
    on every query, and it leaves no handle open. POSIX allows the replace, so it keeps
    the mapping.
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

    size = os.path.getsize(src)
    with open(src, "rb") as fh:
        head = fh.read(len(MAGIC) + 4)
        if len(head) < len(MAGIC) + 4 or head[: len(MAGIC)] != MAGIC:
            raise IndexUnusable(f"{src}: bad magic")
        header_len = struct.unpack("<I", head[len(MAGIC):])[0]
        raw = fh.read(header_len)
        if len(raw) != header_len:
            raise IndexUnusable(f"{src}: header truncated")
        try:
            header = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise IndexUnusable(f"{src}: header is not JSON ({exc})") from exc

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
    base = len(MAGIC) + 4 + header_len
    expect = base + count * (8 + dim * 4 + 4 * len(_AXIS_FIELDS) + CREATED_AT_WIDTH)
    if size != expect:
        # The one check that catches a build killed halfway, a truncated copy, and
        # a header that disagrees with its own body — all as the same condition.
        raise IndexUnusable(f"{src}: expected {expect} bytes for {count} rows of {dim}d, found {size}")

    # One array per field, addressed by offset, rather than one uint8 buffer that is
    # re-viewed: a view across a slice has to satisfy numpy's alignment rules for
    # the target dtype, and expressing the offsets here keeps the layout in the
    # code that reads it instead of in a chain of pointer arithmetic.
    mapped = _maps_files()

    def _map(offset: int, dtype: str, shape) -> np.ndarray:
        if mapped:
            return np.memmap(src, dtype=dtype, mode="r", offset=offset, shape=shape)
        items = int(np.prod(shape))
        return np.fromfile(src, dtype=dtype, count=items, offset=offset).reshape(shape)

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
    return VectorIndex(
        path=src,
        dim=dim,
        count=count,
        watermark=int(header["watermark"]),
        excluded_ids=tuple(int(i) for i in header.get("excluded_ids", ())),
        unembedded_ids=tuple(int(i) for i in header.get("unembedded_ids", ())),
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
        help="Write the index from the database (read-only against it), and for "
        "memories the coarse index too. Exit 0 when every file was built, 1 when a "
        "builder declined (the reason is printed), 2 on error.",
    )
    sub.add_parser(
        "status",
        help="Report the index beside the database. Exit 0 when a usable index is "
        "present, 1 when there is none, 2 when the file exists but cannot be used.",
    )
    return ap


async def rows_read_exactly(db, index: VectorIndex, table: str, iso) -> int:
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
    """
    holes_ids = tuple(index.excluded_ids) + tuple(index.unembedded_ids)
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
        row = await db.execute_fetchall(
            f"SELECT COUNT(*) FROM {table} WHERE id > ? AND embedding IS NOT NULL{iso.and_clause}",
            (index.watermark, *iso.params),
        )
        read_exactly = await rows_read_exactly(db, index, table, iso)
    st = os.stat(path)
    return {
        "table": table,
        "path": path,
        "present": True,
        "usable": True,
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
        f"watermark {result['watermark']}, {result['rows_since_build']} rows written since "
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
        # The read seam, not get_db(): the commit/rollback boundary belongs to
        # database.py, and a builder that only reads has no business owning one.
        from cpersona import database
        from cpersona.database import close_db, connection

        # An operator tool does not migrate, and must not create a database behind
        # a mistyped path. Restored afterwards: this is process state, and the
        # in-process callers of main() (tests) share the process.
        skip_before = database.SKIP_BOOT_MIGRATIONS
        database.SKIP_BOOT_MIGRATIONS = True
        try:
            from cpersona import coarse_index

            if args.command == "build":
                async with connection() as db:
                    result = await build_index(db, args.table)
                    if args.table in coarse_index.COARSE_TABLES:
                        # The same command builds both (design §10 B): an operator who
                        # builds one index is not left to discover the other.
                        result["coarse"] = await coarse_index.build_coarse_index(db, args.table)
                built = [result] + ([result["coarse"]] if "coarse" in result else [])
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
    """
    mask = index.agent_code == _code_of(index.agents, agent_id)

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
        # The SQL is a prefix LIKE over a JSON field. Resolved here against the
        # header's string table, which is small because the values are: the
        # per-row test is integer membership. A NULL source id carries NULL_CODE
        # and matches nothing, exactly as LIKE against NULL does.
        codes = np.array(
            # str(): `json_extract(source, '$.id')` hands back whatever type the
            # JSON held, and SQLite returns a JSON number as an int. The scan
            # this mirrors filters with LIKE, which coerces the number to text
            # and matches it (bug-276) — so a non-string id must be compared the
            # same way here, not raise. The obligation above is one-directional:
            # matching a row the authority would drop costs wasted work, while
            # raising takes down the query the index exists to accelerate.
            [i for i, v in enumerate(index.sources) if v is not None and str(v).startswith(source_id)],
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
