"""Block index file — the rows the block arm examines, read from a file instead of SQLite.

Design: `docs/BLOCK_CANDIDATES_CONTRACT.md` §3. In one line: every block row's
key, axes and bits in a file beside the database, so that recall stops reading a
quarter of a million rows out of SQLite on every call (360-375 ms on an Intel
N150, against 6 ms from a file, measured over 100,000 memories).

The answer must not change. The file holds the rows as they were when it was
built; a change log kept by triggers (`BLOCK_LOG_SQL` in database.py) names the
records whose rows changed since, and a recall reads exactly those records from
SQLite and every other record from the file, then applies both caps to the
merged sequence in key order. Under the premises written down in the contract,
that is the sequence the SQLite read would have produced. Any state the file
cannot answer for exactly — no file, another width, a log that was off or pruned
past the file, a schema change, too many changed rows — sends the call to the
SQLite read, which is always correct.

Like the coarse index, the file is a derived artifact: it is not backed up,
never repaired, and safe to delete.

Layout (little-endian throughout):

    magic  b"CPXBLK01"                       8 bytes
    header_len                               uint32
    header                                   JSON, padded to a 64-byte boundary
    parent_id                                int64[count]
    block_index                              int32[count]
    agent_code / project_code / channel_code int32[count] each
    model_code                               uint16[count]
    kind                                     uint8[count]  (0 = ep, 1 = mem)
    measurable                               uint8[count]
    bits                                     uint8[count][width]

Rows are in key order (kind, parent id, block index), the order SQLite's primary
key reads them in. Rows with NULL bits are not in the file: the examined read
never admits them, so they spend no place in either cap. A row whose bits are
another width is in the file, with `measurable` 0 and its bits zeroed: it keeps
its place in the caps, as it does in the SQLite read, and is never measured.
"""

from __future__ import annotations

import bisect
import json
import logging
import os
import shutil
import sqlite3
import struct
import time
from dataclasses import dataclass

import numpy as np

from cpersona import blocks
from cpersona import config
from cpersona import fileperms
from cpersona import vector_index
from cpersona.isolation import isolation_where
from cpersona.vector_index import IndexUnusable

logger = logging.getLogger(__name__)

MAGIC = b"CPXBLK01"
FORMAT_VERSION = 1
HEADER_ALIGN = 64

#: Kind codes, in the order SQLite's BINARY collation puts the strings ('ep' <
#: 'mem'), so that sorting the codes sorts the keys the way the table does. A
#: kind outside this tuple is refused at the build and at the read.
KINDS = ("ep", "mem")
_KIND_CODE = {kind: code for code, kind in enumerate(KINDS)}

#: The logging triggers a build requires, by their names in `checks._EXPECTED_OBJECTS`.
LOG_TRIGGERS = ("record_block_log_ai", "record_block_log_ad", "record_block_log_au", "record_block_log_ak")

#: How many rows of changed records a recall reads from SQLite before it gives
#: the file up and reads everything from SQLite. Server policy. Set below the
#: examined cap (250,000 rows) by a wide margin: past it, the rows read live
#: would approach what the SQLite read costs. About 1,000 records at the
#: measured 35 blocks each; the time it buys is measured, not assumed.
LIVE_ROW_BOUND = 35_000

#: Rows the reader masks and merges at a time. The read stops at the chunk in
#: which the examined cap fills, so a call costs the rows it reaches, not the file.
_CHUNK = 65_536

#: Rows fetched per round trip while a build streams the table.
_BUILD_PAGE = 8192

_INT32_MIN, _INT32_MAX = -(2**31), 2**31 - 1

TASK_TYPE = "block_index_build"

#: How long a build that was declined keeps the queue from asking again. A
#: declined build (a value the file cannot hold, a trigger missing) is declined
#: again until someone changes the database, and every block write would ask.
_RETRY_AFTER_DECLINE_SECONDS = 600.0
_declined_until = 0.0


def index_path() -> str:
    """Where the block index lives — beside the database and its other index files."""
    return f"{config.DB_PATH}.blocks.blockindex"


def enabled() -> bool:
    """Whether recall reads the file and the server keeps the change log.

    Both halves follow the one setting, and the reader needs the block arm itself
    to run: a log kept for a file nothing reads would charge every write a row.
    """
    return config.BLOCK_INDEX_ENABLED and config.BLOCK_RETRIEVAL_ENABLED


@dataclass(frozen=True)
class BlockIndex:
    """A validated block index file, memory-mapped (read into memory on Windows)."""

    path: str
    width: int
    count: int
    generation: int
    built_seq: int
    schema_cookie: int
    parent_id: np.ndarray  # int64[count]
    block_index: np.ndarray  # int32[count]
    agent_code: np.ndarray  # int32[count]
    project_code: np.ndarray
    channel_code: np.ndarray
    model_code: np.ndarray  # uint16[count]
    kind: np.ndarray  # uint8[count]
    measurable: np.ndarray  # uint8[count]
    bits: np.ndarray  # uint8[count][width]
    models: tuple[str, ...]
    agents: tuple[str, ...]
    projects: tuple[str, ...]
    channels: tuple[str, ...]


# --------------------------------------------------------------------------------------
# the change log
# --------------------------------------------------------------------------------------


async def _clock(db) -> tuple[int, int, int, int] | None:
    """(logging, generation, head, pruned_through), or None before schema 19."""
    try:
        rows = await db.execute_fetchall(
            "SELECT logging, generation, head, pruned_through FROM block_log_clock WHERE id = 0"
        )
    except sqlite3.OperationalError:
        # An operator tool opens the database without migrating it.
        return None
    return tuple(rows[0]) if rows else None


async def start_logging() -> int | None:
    """Turn the change log on, in a commit of its own; the generation it runs under.

    Committed before a build reads its snapshot, so every write after that
    snapshot is logged. A write between this commit and the snapshot is logged
    and is in the file too, which costs nothing: reading a record live that the
    file already holds unchanged gives the same rows.
    """
    from cpersona.database import transaction

    async with transaction(scope_stats_neutral=True) as db:
        clock = await _clock(db)
        if clock is None:
            return None
        if clock[0] == 1:
            return clock[1]
        await db.execute(
            "UPDATE block_log_clock SET logging = 1, generation = generation + 1 WHERE id = 0"
        )
        return clock[1] + 1


async def stop_logging(db) -> bool:
    """Turn the change log off and empty it, on the caller's transaction.

    A file is refused while logging is off, and after logging is next turned on
    its generation no longer matches, so no file built before the gap is read.
    """
    clock = await _clock(db)
    if clock is None or clock[0] != 1:
        return False
    await db.execute("UPDATE block_log_clock SET logging = 0, pruned_through = head WHERE id = 0")
    await db.execute("DELETE FROM record_block_changes")
    return True


async def _log_triggers_differ(db) -> list[str]:
    """The logging triggers that are missing or not as the schema defines them.

    A build refuses while any is: comparing the schema cookie later only notices
    a change made after the build, not a build that began without complete
    logging, and the writes such a gap missed would never be read live.
    """
    from cpersona import checks

    rows = await db.execute_fetchall(
        "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'record_blocks'"
    )
    actual = {r[0]: r[1] or "" for r in rows}
    return [
        name
        for name in LOG_TRIGGERS
        if checks._normalize_sql(actual.get(name, "")) != checks._normalize_sql(checks._EXPECTED_OBJECTS[name]["sql"])
    ]


async def _prune(generation: int, built_seq: int) -> bool:
    """Drop the log rows a file now holds, and move the mark past them.

    One transaction, so no reader sees the rows gone and the mark not yet moved.
    The mark only rises (`max`): a slower, older build finishing second must not
    lower it again, or a file whose changes were pruned would be read as current.
    Only within the generation the build read, so a build from before logging was
    turned off and on cannot prune the log of the next.
    """
    from cpersona.database import transaction

    async with transaction() as db:
        clock = await _clock(db)
        if clock is None or clock[0] != 1 or clock[1] != generation:
            return False
        await db.execute("DELETE FROM record_block_changes WHERE seq <= ?", (built_seq,))
        await db.execute(
            "UPDATE block_log_clock SET pruned_through = max(pruned_through, ?) WHERE id = 0",
            (built_seq,),
        )
        return True


# --------------------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------------------


class _Declined(Exception):
    """A build that cannot produce a file that answers exactly."""


async def build_block_index(path: str | None = None, width: int | None = None) -> dict:
    """Write the block index file and prune the change log behind it.

    Runs outside any transaction: it commits the log on, reads the rows in one
    snapshot on a connection of its own, and prunes in a transaction of its own.
    ``width`` is the bits width in bytes the file measures; by default the width
    most rows have, the narrower on a tie.
    """
    from cpersona.database import read_snapshot

    out = path or index_path()
    generation = await start_logging()
    if generation is None:
        return {
            "built": False,
            "reason": "the database has no block change log; start the server once to migrate it to schema 19",
        }
    tmp, bits_tmp = f"{out}.tmp", f"{out}.bits.tmp"
    try:
        async with read_snapshot() as snap:
            header = await _write_rows(snap, tmp, bits_tmp, width)
        os.replace(tmp, out)
    except _Declined as exc:
        return {"built": False, "reason": str(exc)}
    finally:
        # A half-written temp file is not the index and must not be left where a
        # later build could mistake it for one.
        for name in (tmp, bits_tmp):
            if os.path.exists(name):
                os.unlink(name)
    pruned = await _prune(header["generation"], header["built_seq"])
    return {
        "built": True,
        "path": out,
        "count": header["count"],
        "width": header["width"],
        "generation": header["generation"],
        "built_seq": header["built_seq"],
        "pruned": pruned,
        "bytes": os.path.getsize(out),
    }


async def _write_rows(snap, tmp: str, bits_tmp: str, width: int | None) -> dict:
    """Read every block row in one snapshot and write the file under ``tmp``."""
    clock = await _clock(snap)
    if clock is None or clock[0] != 1:
        raise _Declined("the change log was turned off before the build read the rows")
    differ = await _log_triggers_differ(snap)
    if differ:
        raise _Declined(
            f"the change log's triggers are missing or changed ({', '.join(differ)}); "
            "repair them with check_health(checks=['schema_objects'], fix=true)"
        )
    cookie = (await snap.execute_fetchall("PRAGMA schema_version"))[0][0]
    # Deliberately global: the file holds every agent's rows, and a recall masks
    # them by the axes each row carries, as the SQLite read filters them.
    iso = isolation_where(agent_id=None)
    widths = await snap.execute_fetchall(
        "SELECT length(embedding_bits), COUNT(*) FROM record_blocks"
        f" WHERE embedding_bits IS NOT NULL{iso.and_clause} GROUP BY 1 ORDER BY 2 DESC, 1 ASC",
        iso.params,
    )
    if not widths:
        raise _Declined("there are no block rows to index")
    if width is None:
        width = int(widths[0][0])
    if width < 1:
        raise _Declined(f"width {width} cannot be measured")

    tables: dict[str, dict[str, int]] = {"models": {}, "agents": {}, "projects": {}, "channels": {}}
    columns: dict[str, list] = {name: [] for name in ("parent_id", "block_index", "agent", "project", "channel", "model", "kind", "measurable")}
    zeros = bytes(width)
    count = 0

    def code(table: str, value: object) -> int:
        if type(value) is not str:
            raise _Declined(f"a block row's {table[:-1]} is {type(value).__name__}, not text")
        held = tables[table]
        return held.setdefault(value, len(held))

    with fileperms.open_private(bits_tmp, "wb") as bits_out:
        async with snap.execute(
            "SELECT parent_kind, parent_id, block_index, agent_id, project_id, channel,"
            " embedding_model, embedding_bits FROM record_blocks"
            " INDEXED BY sqlite_autoindex_record_blocks_1"
            f" WHERE embedding_bits IS NOT NULL{iso.and_clause}"
            " ORDER BY parent_kind, parent_id, block_index",
            iso.params,
        ) as cursor:
            while True:
                page = await cursor.fetchmany(_BUILD_PAGE)
                if not page:
                    break
                chunk = bytearray()
                for kind, parent_id, block_index, agent, project, channel, model, bits in page:
                    if type(kind) is not str or kind not in _KIND_CODE:
                        raise _Declined(f"a block row's kind is {kind!r}, not one of {KINDS}")
                    if type(parent_id) is not int or type(block_index) is not int:
                        raise _Declined(f"block row {kind}:{parent_id}:{block_index} has a key that is not an integer")
                    if not _INT32_MIN <= block_index <= _INT32_MAX:
                        raise _Declined(f"block row {kind}:{parent_id}:{block_index} has an index outside int32")
                    if type(bits) is not bytes:
                        raise _Declined(f"block row {kind}:{parent_id}:{block_index} has bits that are not a blob")
                    columns["kind"].append(_KIND_CODE[kind])
                    columns["parent_id"].append(parent_id)
                    columns["block_index"].append(block_index)
                    columns["agent"].append(code("agents", agent))
                    columns["project"].append(code("projects", project))
                    columns["channel"].append(code("channels", channel))
                    columns["model"].append(code("models", model))
                    fits = len(bits) == width
                    columns["measurable"].append(1 if fits else 0)
                    chunk += bits if fits else zeros
                    count += 1
                bits_out.write(chunk)
        bits_out.flush()

    if len(tables["models"]) > 65_535:
        raise _Declined(f"{len(tables['models'])} model labels do not fit the file's 16-bit code")

    header = {
        "format": FORMAT_VERSION,
        "width": width,
        "count": count,
        "generation": clock[1],
        "built_seq": clock[2],
        "schema_cookie": cookie,
        "kinds": list(KINDS),
        "models": list(tables["models"]),
        "agents": list(tables["agents"]),
        "projects": list(tables["projects"]),
        "channels": list(tables["channels"]),
    }
    blob = json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")
    blob += b" " * ((-(len(MAGIC) + 4 + len(blob))) % HEADER_ALIGN)
    with fileperms.open_private(tmp, "wb") as fh:
        fh.write(MAGIC)
        fh.write(struct.pack("<I", len(blob)))
        fh.write(blob)
        fh.write(np.asarray(columns["parent_id"], dtype="<i8").tobytes())
        fh.write(np.asarray(columns["block_index"], dtype="<i4").tobytes())
        for name in ("agent", "project", "channel"):
            fh.write(np.asarray(columns[name], dtype="<i4").tobytes())
        fh.write(np.asarray(columns["model"], dtype="<u2").tobytes())
        fh.write(np.asarray(columns["kind"], dtype="u1").tobytes())
        fh.write(np.asarray(columns["measurable"], dtype="u1").tobytes())
        with open(bits_tmp, "rb") as bits_in:
            shutil.copyfileobj(bits_in, fh, 1 << 20)
        fh.flush()
        os.fsync(fh.fileno())
    return header


# --------------------------------------------------------------------------------------
# load
# --------------------------------------------------------------------------------------


def load_block_index(path: str | None = None) -> BlockIndex | None:
    """Map (or, on Windows, read) a block index file, or None when there is none.

    A file that exists but does not hold together raises `IndexUnusable`.
    """
    src = path or index_path()
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
    if tuple(header.get("kinds", ())) != KINDS:
        raise IndexUnusable(f"{src}: kinds {header.get('kinds')!r} are not {list(KINDS)!r}")
    try:
        width, count = int(header["width"]), int(header["count"])
        generation, built_seq = int(header["generation"]), int(header["built_seq"])
        cookie = int(header["schema_cookie"])
    except (KeyError, TypeError, ValueError) as exc:
        raise IndexUnusable(f"{src}: header lacks a usable field ({exc!r})") from exc
    if width < 1 or count < 0:
        raise IndexUnusable(f"{src}: width {width} / count {count}")

    base = len(MAGIC) + 4 + header_len
    expect = base + count * (8 + 4 + 3 * 4 + 2 + 1 + 1 + width)
    if size != expect:
        raise IndexUnusable(f"{src}: expected {expect} bytes for {count} rows of width {width}, found {size}")

    mapped = vector_index._maps_files()

    def _map(offset: int, dtype: str, shape) -> np.ndarray:
        if mapped:
            return np.memmap(src, dtype=dtype, mode="r", offset=offset, shape=shape)
        items = int(np.prod(shape))
        return np.fromfile(src, dtype=dtype, count=items, offset=offset).reshape(shape)

    off = base
    arrays = {}
    for name, dtype, item in (
        ("parent_id", "<i8", 8), ("block_index", "<i4", 4), ("agent_code", "<i4", 4),
        ("project_code", "<i4", 4), ("channel_code", "<i4", 4), ("model_code", "<u2", 2),
        ("kind", "u1", 1), ("measurable", "u1", 1),
    ):
        arrays[name] = _map(off, dtype, (count,))
        off += count * item
    bits = _map(off, "u1", (count, width))

    return BlockIndex(
        path=src,
        width=width,
        count=count,
        generation=generation,
        built_seq=built_seq,
        schema_cookie=cookie,
        bits=bits,
        models=tuple(header.get("models", ())),
        agents=tuple(header.get("agents", ())),
        projects=tuple(header.get("projects", ())),
        channels=tuple(header.get("channels", ())),
        **arrays,
    )


_cache: dict[str, tuple[tuple, BlockIndex | None]] = {}


def cached_block_index(path: str | None = None) -> BlockIndex | None:
    """`load_block_index`, re-reading only when the file on disk has changed."""
    src = path or index_path()
    if not os.path.exists(src):
        _cache.pop(src, None)
        return None
    key = vector_index._stat_key(src)
    hit = _cache.get(src)
    if hit is not None and hit[0] == key:
        return hit[1]
    index = load_block_index(src)
    _cache[src] = (key, index)
    return index


# --------------------------------------------------------------------------------------
# read
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Examined:
    """The rows one call examines, in the order the caps truncate, as arrays.

    The same sequence of keys `blocks._examined` returns. A row the file could not
    measure keeps its place and has ``measurable`` False; ``bits`` holds the
    measurable rows only, in sequence order.
    """

    kind: np.ndarray  # uint8
    parent_id: np.ndarray  # int64
    block_index: np.ndarray  # int64
    measurable: np.ndarray  # bool
    bits: np.ndarray  # uint8[measurable rows][width]

    def keys(self) -> list[tuple[str, int, int]]:
        return [
            (KINDS[k], int(p), int(b)) for k, p, b in zip(self.kind, self.parent_id, self.block_index)
        ]

    def measured(self, query_bits: bytes):
        """What `blocks._measured` returns for the same rows: the usable rows and their distances."""
        usable = np.flatnonzero(self.measurable)
        if not len(usable):
            return [], None
        rows = _Rows(self.kind[usable], self.parent_id[usable], self.block_index[usable], self.bits)
        return rows, blocks.hamming_matrix(self.bits, np.frombuffer(query_bits, dtype=np.uint8))


class _Rows:
    """The usable rows as `blocks._order` and `blocks._collapse` read them.

    A row is materialised as the tuple the SQLite read returns only when asked for:
    the order needs the rows near the cut, and the Hamming fallback the rest.
    """

    def __init__(self, kind, parent_id, block_index, bits):
        self._kind, self._parent_id, self._block_index, self._bits = kind, parent_id, block_index, bits

    def __len__(self) -> int:
        return len(self._kind)

    def __getitem__(self, j) -> tuple:
        return (KINDS[self._kind[j]], int(self._parent_id[j]), int(self._block_index[j]), self._bits[j].tobytes())

    def __iter__(self):
        return (self[j] for j in range(len(self)))


def _live_statement(iso) -> str:
    """The changed records' current rows, with the log's clock and the schema cookie, in one statement.

    One statement is one snapshot: the records the log names, their rows and the
    clock they are checked against cannot come from two states of the database.
    The admission filter sits in the join, not after it, and the record's identity
    comes from the log's side, so a changed record with no rows the filter admits
    — deleted, rebuilt to nothing, moved to another agent — still comes back, as a
    row of NULLs, and its rows in the file are still dropped. The clock is the left
    side of the outer join, so the statement returns a row when nothing changed.
    """
    return (
        "WITH changed AS ("
        "SELECT DISTINCT parent_kind, parent_id FROM record_block_changes WHERE seq > ?"
        "), live AS ("
        "SELECT d.parent_kind, d.parent_id, b.block_index, b.embedding_bits FROM changed AS d"
        " LEFT JOIN record_blocks AS b ON b.parent_kind = d.parent_kind AND b.parent_id = d.parent_id"
        f" AND b.embedding_bits IS NOT NULL AND b.embedding_model IN (?, ?){iso.and_clause}"
        " LIMIT ?"
        ") SELECT c.logging, c.generation, c.head, c.pruned_through,"
        " (SELECT schema_version FROM pragma_schema_version),"
        " l.parent_kind, l.parent_id, l.block_index, l.embedding_bits"
        " FROM block_log_clock AS c LEFT JOIN live AS l ON 1 WHERE c.id = 0"
    )


def _codes(table: tuple, values) -> np.ndarray:
    return np.array([i for i, v in enumerate(table) if v in values], dtype=np.int64)


async def examined(
    db, keys: tuple[str, str], *, agent_id: str | None, project_id: str | None, channel: str | None, width: int
) -> Examined | None:
    """The examined rows from the file and the changed records, or None to read SQLite.

    ``agent_id`` / ``project_id`` / ``channel`` are the values the caller handed
    `isolation_where`, read here the way that helper reads them, because the file
    is masked by them rather than by its SQL.
    """
    try:
        index = cached_block_index()
    except IndexUnusable as exc:
        logger.warning("Block index unusable, reading SQLite: %s", exc)
        return None
    if index is None or index.width != width:
        return None

    iso = isolation_where(agent_id=agent_id, project_id=project_id, channel=channel, alias="b")
    rows = await db.execute_fetchall(
        _live_statement(iso), (index.built_seq, *keys, *iso.params, LIVE_ROW_BOUND + 1)
    )
    if not rows:
        return None
    logging_on, generation, head, pruned_through, cookie = rows[0][:5]
    if (
        logging_on != 1
        or generation != index.generation
        or index.built_seq > head  # a file newer than this snapshot
        or pruned_through > index.built_seq  # changes it would need are gone
        or cookie != index.schema_cookie  # a trigger dropped and recreated could have missed writes
    ):
        return None
    live = [r[5:] for r in rows if r[5] is not None]
    if len(live) > LIVE_ROW_BOUND:
        return None

    changed: tuple[set, set] = (set(), set())
    admitted = []
    for kind, parent_id, block_index, bits in live:
        if type(kind) is not str or kind not in _KIND_CODE or type(parent_id) is not int:
            return None
        changed[_KIND_CODE[kind]].add(parent_id)
        if block_index is None:
            continue
        if type(block_index) is not int or not _INT32_MIN <= block_index <= _INT32_MAX or type(bits) is not bytes:
            return None
        admitted.append((_KIND_CODE[kind], parent_id, block_index, bits))
    admitted.sort(key=lambda r: r[:3])

    return _merge(index, admitted, changed, keys, agent_id=agent_id, project_id=project_id, channel=channel)


def _merge(index: BlockIndex, live: list, changed, keys, *, agent_id, project_id, channel) -> Examined:
    """The file's admitted rows and the live rows, in key order, under both caps.

    The caps are taken over the merged sequence, never over either input: a
    changed record that gained or lost rows before the point where a cap binds
    moves that point.
    """
    examined_cap, per_parent_cap = blocks.BLOCK_EXAMINED_CAP, blocks.BLOCK_PER_PARENT_CAP
    model_codes = _codes(index.models, set(keys))
    agent_code = vector_index._code_of(index.agents, agent_id) if agent_id is not None else None
    project_codes = None
    if project_id is not None:
        project_codes = _codes(index.projects, {""} if project_id == "" else {project_id, ""})
    channel_codes = _codes(index.channels, {channel, ""}) if channel else None
    changed_ids = [np.fromiter(ids, dtype=np.int64, count=len(ids)) for ids in changed]
    live_keys = [r[:3] for r in live]

    parts: list[tuple[np.ndarray, ...]] = []
    taken = 0
    last_parent, last_count = None, 0
    next_live = 0
    starts = range(0, index.count, _CHUNK) if index.count else (0,)
    for start in starts:
        end = min(index.count, start + _CHUNK)
        sl = slice(start, end)
        kind = index.kind[sl]
        mask = np.isin(index.model_code[sl], model_codes)
        if agent_code is not None:
            mask &= index.agent_code[sl] == agent_code
        if project_codes is not None:
            mask &= np.isin(index.project_code[sl], project_codes)
        if channel_codes is not None:
            mask &= np.isin(index.channel_code[sl], channel_codes)
        for code, ids in enumerate(changed_ids):
            if len(ids):
                mask &= ~((kind == code) & np.isin(index.parent_id[sl], ids))
        source = np.flatnonzero(mask) + start
        k = index.kind[source].astype(np.uint8)
        p = index.parent_id[source].astype(np.int64)
        b = index.block_index[source].astype(np.int64)

        # The live rows that sort before the first row of the next chunk belong here.
        if end < index.count:
            stop = bisect.bisect_left(
                live_keys, (int(index.kind[end]), int(index.parent_id[end]), int(index.block_index[end])), next_live
            )
        else:
            stop = len(live)
        if stop > next_live:
            batch = live[next_live:stop]
            k = np.concatenate([k, np.array([r[0] for r in batch], dtype=np.uint8)])
            p = np.concatenate([p, np.array([r[1] for r in batch], dtype=np.int64)])
            b = np.concatenate([b, np.array([r[2] for r in batch], dtype=np.int64)])
            # A live row is named by its place in `live`, below zero.
            source = np.concatenate([source, -1 - np.arange(next_live, stop, dtype=np.int64)])
            order = np.lexsort((b, p, k))
            k, p, b, source = k[order], p[order], b[order], source[order]
            next_live = stop
        if not len(k):
            continue

        # Each row's place among its record's admitted rows, counting on from the
        # previous chunk when a record runs across the boundary.
        n = len(k)
        first = np.ones(n, dtype=bool)
        first[1:] = (k[1:] != k[:-1]) | (p[1:] != p[:-1])
        positions = np.arange(n)
        place = positions - np.maximum.accumulate(np.where(first, positions, 0))
        if last_parent == (int(k[0]), int(p[0])):
            later = np.flatnonzero(first[1:])
            run = int(later[0]) + 1 if len(later) else n
            place[:run] += last_count
        last_parent, last_count = (int(k[-1]), int(p[-1])), int(place[-1]) + 1

        keep = place < per_parent_cap
        room = examined_cap - taken
        kept = np.flatnonzero(keep)[:room]
        parts.append((k[kept], p[kept], b[kept], source[kept]))
        taken += len(kept)
        if taken >= examined_cap:
            break

    if parts:
        kind, parent_id, block_index, source = (np.concatenate(cols) for cols in zip(*parts))
    else:
        kind = np.zeros(0, dtype=np.uint8)
        parent_id = block_index = source = np.zeros(0, dtype=np.int64)

    from_file = source >= 0
    measurable = np.zeros(len(source), dtype=bool)
    measurable[from_file] = index.measurable[source[from_file]] == 1
    live_at = np.flatnonzero(~from_file)
    for j in live_at:
        measurable[j] = len(live[-1 - int(source[j])][3]) == index.width
    usable = np.flatnonzero(measurable)
    bits = np.empty((len(usable), index.width), dtype=np.uint8)
    usable_from_file = from_file[usable]
    bits[usable_from_file] = index.bits[source[usable][usable_from_file]]
    for slot in np.flatnonzero(~usable_from_file):
        bits[slot] = np.frombuffer(live[-1 - int(source[usable[slot]])][3], dtype=np.uint8)
    return Examined(kind, parent_id, block_index, measurable, bits)


# --------------------------------------------------------------------------------------
# keeping it current
# --------------------------------------------------------------------------------------


async def status() -> dict:
    """What an operator can act on: present / usable / whether a recall can read it now."""
    from cpersona.database import connection

    path = index_path()
    async with connection() as db:
        clock = await _clock(db)
    out: dict = {"path": path, "enabled": enabled(), "present": os.path.exists(path)}
    if clock is not None:
        out["log"] = {"logging": clock[0], "generation": clock[1], "head": clock[2], "pruned_through": clock[3]}
    if not out["present"]:
        return out
    try:
        index = load_block_index(path)
    except IndexUnusable as exc:
        out.update(usable=False, reason=str(exc), hint="delete the file and build again; it is never repaired")
        return out
    assert index is not None
    out.update(
        usable=True, rows=index.count, width=index.width, generation=index.generation,
        built_seq=index.built_seq, bytes=os.path.getsize(path),
    )
    if clock is not None:
        out["changes_since_build"] = max(0, clock[2] - index.built_seq)
        out["current"] = _current(index, clock)
    return out


def _current(index: BlockIndex, clock) -> bool:
    """Whether a recall now could read this file, as far as the clock alone says."""
    logging_on, generation, head, pruned_through = clock
    return (
        logging_on == 1
        and generation == index.generation
        and index.built_seq <= head
        and pruned_through <= index.built_seq
        and head - index.built_seq <= LIVE_ROW_BOUND
    )


async def queue_build_if_needed() -> bool:
    """Queue a build when the file is absent, unusable or too far behind. True when queued.

    Called at startup and after the queue writes blocks: those are the moments the
    file falls behind, and a recall does not write. "Too far behind" counts log
    rows rather than the rows a recall reads live, so it asks a little early.

    Never raises: it runs after a queued task has completed, and an exception
    there would mark that task failed and run it again.
    """
    from cpersona import tasks

    if not enabled() or tasks._task_queue is None or time.monotonic() < _declined_until:
        return False
    try:
        due = await _build_due()
    except Exception:  # noqa: BLE001 - derived work; every answer is still served from SQLite
        logger.warning("could not tell whether the block index needs a build", exc_info=True)
        return False
    if not due:
        return False
    try:
        await tasks._task_queue.enqueue(TASK_TYPE, "", {})
    except Exception as e:  # noqa: BLE001 - derived work; every answer is still served from SQLite
        logger.warning("could not queue the block index build: %s", e)
        return False
    return True


async def _build_due() -> bool:
    """Whether the file is absent, unusable or behind, rows exist, and no build is queued."""
    from cpersona.database import connection

    async with connection() as db:
        clock = await _clock(db)
        if clock is None:
            return False
        try:
            index = cached_block_index()
        except IndexUnusable:
            index = None
        if index is not None and _current(index, clock) and index.schema_cookie == (
            await db.execute_fetchall("PRAGMA schema_version")
        )[0][0]:
            return False
        iso = isolation_where(agent_id=None)
        if index is None and not await db.execute_fetchall(
            f"SELECT 1 FROM record_blocks WHERE embedding_bits IS NOT NULL{iso.and_clause} LIMIT 1", iso.params
        ):
            return False
        pending = await db.execute_fetchall(
            f"SELECT 1 FROM pending_memory_tasks WHERE task_type = ?{iso.and_clause} LIMIT 1",
            (TASK_TYPE, *iso.params),
        )
        return not pending


async def build_task(payload: dict) -> str:
    """The queue's build: the file, or the reason there is none."""
    global _declined_until
    if not enabled():
        return "block index is off; nothing built"
    result = await build_block_index()
    if not result["built"]:
        _declined_until = time.monotonic() + _RETRY_AFTER_DECLINE_SECONDS
        return f"block index not built: {result['reason']}"
    return (
        f"block index built: {result['count']} rows of width {result['width']}, "
        f"through change {result['built_seq']} of generation {result['generation']}"
    )


async def on_startup() -> None:
    """Keep the change log only while the file is in use, and start a build when one is due."""
    from cpersona.database import transaction

    if enabled():
        await queue_build_if_needed()
        return
    async with transaction() as db:
        if await stop_logging(db):
            logger.info("block index is off: stopped the block change log")


# --------------------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------------------


def _build_parser():
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m cpersona.block_index",
        description="Build or inspect the block index file the block arm reads instead of SQLite.",
    )
    parser.add_argument("--db", help="database path (default: CPERSONA_DB_PATH)")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    parser.add_argument("command", choices=("build", "status"))
    return parser


def main(argv: list | None = None) -> int:
    import asyncio
    import sys

    args = _build_parser().parse_args(argv)
    if args.db:
        if not os.path.exists(args.db):
            print(f"error: database not found: {args.db}", file=sys.stderr)
            return 2
        os.environ["CPERSONA_DB_PATH"] = args.db
        config.DB_PATH = args.db

    async def run() -> int:
        from cpersona import database

        # An operator tool does not migrate (a database before schema 19 has no
        # change log, and the build says so).
        skip_before = database.SKIP_BOOT_MIGRATIONS
        database.SKIP_BOOT_MIGRATIONS = True
        try:
            if args.command == "build":
                result = await build_block_index()
                code = 0 if result["built"] else 1
            else:
                result = await status()
                code = 0 if result.get("usable") else (2 if result["present"] else 1)
        except Exception as exc:  # noqa: BLE001 — the reason must reach the operator
            print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        finally:
            database.SKIP_BOOT_MIGRATIONS = skip_before
            await database.close_db()
        print(json.dumps(result, indent=None if not args.json else 2, sort_keys=True))
        return code

    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
