"""Coarse record index — one bit per dimension, builder and loader.

Design: `docs/BINARY_COARSE_SEARCH_DESIGN.md` §3. In one line: every record's
stored vector, reduced to the sign of each dimension, in a file of its own beside
the contiguous index, so that a Hamming pass can reach the whole store at a
resident cost of one bit per dimension.

This module builds, validates and loads that file. It ranks nothing and decides
nothing about who may see a row: `isolation_where()` stays the authority, and the
callers re-apply it when they hydrate. Like the contiguous index, the file is a
derived artifact: it is not backed up, never repaired, and safe to delete.

The row set, its order, the rows it names instead of holding and its axis string
tables come from the same plan as the contiguous index
(`vector_index.plan_rows`), so the two files cannot disagree about which rows an
index holds.

Layout (little-endian throughout):

    magic  b"CPXBIT01"                       8 bytes
    header_len                               uint32
    header                                   JSON, padded to a 64-byte boundary
    ids                                      int64[count]
    agent_code / project_code /
    channel_code / source_code               int32[count] each
    bits                                     uint8[count][ceil(dim / 8)]
    created_at                               19 ASCII bytes per row
    timestamp                                19 ASCII bytes per row, or 19 NUL bytes

The fixed-width columns come before the bits so that every int64 and int32 array
starts on its own alignment whatever the dimension; the byte columns follow.

Like the contiguous index's header, this one carries no wall-clock field: the
file is a pure function of the database content, so "a rebuild produced the same
bytes" is a property a test can assert.
"""

from __future__ import annotations

import json
import os
import struct
from dataclasses import dataclass

import numpy as np

from cpersona import config
from cpersona import fileperms
from cpersona import vector_index
from cpersona.isolation import isolation_where
from cpersona.utils import SCORING_VERSION
from cpersona.vector_index import IndexUnusable

MAGIC = b"CPXBIT01"
FORMAT_VERSION = 1
HEADER_ALIGN = 64

#: Records only. Episodes are counted in the hundreds where records are counted in
#: the thousands, and they fit the scan window; whether they need a coarse index is
#: decided once the record index is established (design §11).
COARSE_TABLES = ("memories",)

#: The record's own time as SQLite reads it. A time cue's period is compared
#: through `datetime()` because stored timestamps mix spellings (bug-394), and
#: `datetime()` answers in one fixed-width form, so the bytes this file holds
#: compare the way the SQL compares.
TIMESTAMP_EXPR = "datetime(timestamp)"
TIMESTAMP_WIDTH = 19
#: A cue's period `[start, end)` over the record's own time, both bounds read by
#: `datetime()` (the cue arm binds `cue.sql_instant` strings to the two
#: placeholders). One spelling for every reader of a period — the cue arm's
#: exact part, the live tail of an index read, and the coarse search's live
#: supplier — so the rows a period holds cannot depend on which of them asked.
PERIOD_PREDICATE = f"{TIMESTAMP_EXPR} >= datetime(?) AND {TIMESTAMP_EXPR} < datetime(?)"
#: A timestamp `datetime()` cannot read is NULL in SQL, and a NULL is inside no
#: period. NUL bytes sort below every instant `datetime()` can write, so a period
#: compared against them excludes the row, as the SQL does.
UNREADABLE_TIMESTAMP = b"\0" * TIMESTAMP_WIDTH

_AXIS_FIELDS = ("agent_code", "project_code", "channel_code", "source_code")


def index_path(table: str = "memories") -> str:
    """Where the coarse index for `table` lives — beside the database and the contiguous index."""
    return f"{config.DB_PATH}.{table}.coarseindex"


def bits_width(dim: int) -> int:
    """Bytes a row's bits occupy: one bit per dimension, padded to a whole byte."""
    return (dim + 7) // 8


def sign_bits(vectors) -> np.ndarray:
    """One bit per dimension, set where the component is greater than zero.

    The quantiser block reach stores (`blocks.pack_bits`), applied to a matrix:
    a row of the result is the bytes `pack_bits` returns for that row. The two
    are held together by a test, because a Hamming distance between bits made
    by two different rules measures nothing.
    """
    return np.packbits(np.asarray(vectors, dtype=np.float32) > 0, axis=-1)


@dataclass(frozen=True)
class CoarseIndex:
    """A validated coarse index file, memory-mapped (read into memory on Windows)."""

    path: str
    dim: int
    count: int
    watermark: int
    excluded_ids: tuple[int, ...]
    unembedded_ids: tuple[int, ...]
    embedding_model: str
    scoring_version: str
    ids: np.ndarray  # int64[count]
    agent_code: np.ndarray  # int32[count]
    project_code: np.ndarray
    channel_code: np.ndarray
    source_code: np.ndarray
    bits: np.ndarray  # uint8[count][bits_width(dim)]
    created_at: np.ndarray  # |S19[count]
    timestamp: np.ndarray  # |S19[count]; NUL bytes where unreadable
    agents: tuple[str, ...]
    projects: tuple[str, ...]
    channels: tuple[str, ...]
    sources: tuple[str | None, ...]


async def build_coarse_index(db, table: str = "memories", path: str | None = None) -> dict:
    """Write the coarse index for `table`. Read-only against the database.

    Two passes, as the contiguous index's builder makes them: the plan fixes the
    row set from the metadata alone, then the stored vectors are streamed in the
    same order and reduced to bits a chunk at a time, so no more than one chunk
    of float32 is ever held. The second pass must produce exactly the rows the
    plan counted; a mismatch aborts the build rather than leaving bits shifted by
    a row against their ids.
    """
    if table not in COARSE_TABLES:
        return {"built": False, "reason": f"no coarse index is built for {table}"}
    out = path or index_path(table)
    plan = await vector_index.plan_rows(db, table, extra_columns=(TIMESTAMP_EXPR,))
    if isinstance(plan, dict):
        return plan

    stamps = []
    for r in plan.kept:
        value = r[plan.extra_offset]
        if value is None:
            stamps.append(UNREADABLE_TIMESTAMP)
            continue
        if not vector_index._is_canonical(value):
            # `datetime()` writes every instant it can read in the 19-character
            # form; any other answer is one this fixed-width column cannot hold,
            # and holding it wrongly would put the row in the wrong periods.
            return {
                "built": False,
                "reason": f"row {r[0]}: datetime(timestamp) returned {value!r}",
                "watermark": plan.watermark,
            }
        stamps.append(value.encode("ascii"))

    header = {
        "format": FORMAT_VERSION,
        "table": table,
        "dim": plan.dim,
        "quantiser": "sign",
        "count": plan.count,
        "watermark": plan.watermark,
        "excluded_ids": plan.excluded,
        "unembedded_ids": plan.unembedded,
        "fingerprint": {
            "dim": plan.dim,
            "embedding_model": config.EMBEDDING_MODEL,
            "scoring_version": SCORING_VERSION,
        },
        "agents": plan.agents,
        "projects": plan.projects,
        "channels": plan.channels,
        "sources": plan.sources,
    }
    blob = json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")
    blob += b" " * ((-(len(MAGIC) + 4 + len(blob))) % HEADER_ALIGN)

    tmp = f"{out}.tmp"
    try:
        with fileperms.open_private(tmp, "wb") as fh:
            fh.write(MAGIC)
            fh.write(struct.pack("<I", len(blob)))
            fh.write(blob)
            fh.write(np.array([r[0] for r in plan.kept], dtype="<i8").tobytes())
            for column in plan.axis_code_arrays():
                fh.write(column)

            written = 0
            async for blobs in vector_index.iter_embedding_chunks(db, table, plan.watermark, plan.width):
                matrix = np.frombuffer(b"".join(blobs), dtype="<f4").reshape(len(blobs), plan.dim)
                fh.write(sign_bits(matrix).tobytes())
                written += len(blobs)
            if written != plan.count:
                raise IndexUnusable(
                    f"embedding pass returned {written} rows, metadata pass counted {plan.count}"
                )

            fh.write(b"".join(r[4].encode("ascii") for r in plan.kept))
            fh.write(b"".join(stamps))
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
        "count": plan.count,
        "dim": plan.dim,
        "watermark": plan.watermark,
        "excluded": len(plan.excluded),
        "unembedded": len(plan.unembedded),
        "unreadable_timestamps": sum(1 for s in stamps if s == UNREADABLE_TIMESTAMP),
        "bytes": os.path.getsize(out),
    }


def load_coarse_index(table: str = "memories", path: str | None = None) -> CoarseIndex | None:
    """Map (or, on Windows, read) a coarse index file, or return None when there is none.

    None is the ordinary state before the first build and after a deletion. A file
    that exists but does not hold together raises `IndexUnusable`, as the
    contiguous index's loader does.
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
    if header.get("quantiser") != "sign":
        raise IndexUnusable(f"{src}: quantiser {header.get('quantiser')!r} is not 'sign'")
    try:
        dim, count = int(header["dim"]), int(header["count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise IndexUnusable(f"{src}: header lacks a usable dim/count ({exc!r})") from exc

    width = bits_width(dim)
    base = len(MAGIC) + 4 + header_len
    expect = base + count * (8 + 4 * len(_AXIS_FIELDS) + width + vector_index.CREATED_AT_WIDTH + TIMESTAMP_WIDTH)
    if size != expect:
        raise IndexUnusable(f"{src}: expected {expect} bytes for {count} rows of {dim}d, found {size}")

    mapped = vector_index._maps_files()

    def _map(offset: int, dtype: str, shape) -> np.ndarray:
        if mapped:
            return np.memmap(src, dtype=dtype, mode="r", offset=offset, shape=shape)
        items = int(np.prod(shape))
        return np.fromfile(src, dtype=dtype, count=items, offset=offset).reshape(shape)

    off = base
    ids = _map(off, "<i8", (count,))
    off += count * 8
    codes = {}
    for field in _AXIS_FIELDS:
        codes[field] = _map(off, "<i4", (count,))
        off += count * 4
    bits = _map(off, "u1", (count, width))
    off += count * width
    created = _map(off, f"S{vector_index.CREATED_AT_WIDTH}", (count,))
    off += count * vector_index.CREATED_AT_WIDTH
    stamps = _map(off, f"S{TIMESTAMP_WIDTH}", (count,))

    fingerprint = header.get("fingerprint", {})
    return CoarseIndex(
        path=src,
        dim=dim,
        count=count,
        watermark=int(header["watermark"]),
        excluded_ids=tuple(int(i) for i in header.get("excluded_ids", ())),
        unembedded_ids=tuple(int(i) for i in header.get("unembedded_ids", ())),
        embedding_model=str(fingerprint.get("embedding_model", "")),
        scoring_version=str(fingerprint.get("scoring_version", "")),
        ids=ids,
        agent_code=codes["agent_code"],
        project_code=codes["project_code"],
        channel_code=codes["channel_code"],
        source_code=codes["source_code"],
        bits=bits,
        created_at=created,
        timestamp=stamps,
        agents=tuple(header.get("agents", ())),
        projects=tuple(header.get("projects", ())),
        channels=tuple(header.get("channels", ())),
        sources=tuple(header.get("sources", ())),
    )


_cache: dict[str, tuple[tuple, CoarseIndex | None]] = {}


def cached_coarse_index(table: str = "memories", path: str | None = None) -> CoarseIndex | None:
    """`load_coarse_index`, re-reading only when the file on disk has changed.

    Raises `IndexUnusable` exactly as `load_coarse_index` does; a bad file is not
    cached, so a rebuild that fixes it is picked up on the next call.
    """
    src = path or index_path(table)
    if not os.path.exists(src):
        _cache.pop(src, None)
        return None
    key = vector_index._stat_key(src)
    hit = _cache.get(src)
    if hit is not None and hit[0] == key:
        return hit[1]
    index = load_coarse_index(table, src)
    _cache[src] = (key, index)
    return index


async def status(table: str = "memories") -> dict:
    """What an operator can act on: present / usable / how far behind the database."""
    import datetime as _dt

    path = index_path(table)
    if not os.path.exists(path):
        return {"table": table, "path": path, "present": False}
    try:
        index = load_coarse_index(table)
    except IndexUnusable as exc:
        return {
            "table": table, "path": path, "present": True, "usable": False,
            "reason": str(exc),
            "hint": "delete the file and build again; it is a derived artifact and is never repaired",
        }
    assert index is not None
    from cpersona.database import connection

    # The same definition of "behind" the contiguous index reports: rows written
    # since the build, and the named rows every query reads exactly (bug-388).
    iso = isolation_where(agent_id=None)
    async with connection() as db:
        row = await db.execute_fetchall(
            f"SELECT COUNT(*) FROM {table} WHERE id > ? AND embedding IS NOT NULL{iso.and_clause}",
            (index.watermark, *iso.params),
        )
        read_exactly = await vector_index.rows_read_exactly(db, index, table, iso)
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
