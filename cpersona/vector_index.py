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
# on the path that was always correct.
MAX_EXCLUDED_IDS = 1000

# Only agent_id is compared for equality; the other three axes are compared
# against a small set. Either way the per-row test is an integer one.
_AXIS_FIELDS = ("agent_code", "project_code", "channel_code", "source_code")

NULL_CODE = -1  # a source id that is SQL NULL: LIKE never matches it


def index_path(table: str = "memories") -> str:
    """Where the index for `table` lives — beside the database, like the calibration sidecar."""
    return f"{config.DB_PATH}.{table}.vecindex"


@dataclass(frozen=True)
class VectorIndex:
    """A validated, memory-mapped index file."""

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
    iso = isolation_where(agent_id=None)
    src_expr = ", json_extract(source, '$.id')" if table == "memories" else ""

    row = await db.execute_fetchall(f"SELECT MAX(id) FROM {table}{iso.where}", iso.params)
    watermark = int(row[0][0] or 0)

    meta_sql = (
        f"SELECT id, agent_id, project_id, channel, created_at, length(embedding){src_expr}"
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
        with open(tmp, "wb") as fh:
            fh.write(MAGIC)
            fh.write(struct.pack("<I", len(blob)))
            fh.write(blob)
            fh.write(np.array([r[0] for r in kept], dtype="<i8").tobytes())

            written = await _stream_embeddings(db, fh, table, watermark, width)
            if written != count:
                raise IndexUnusable(
                    f"embedding pass returned {written} rows, metadata pass counted {count}"
                )

            fh.write(np.array([agent_ix[r[1]] for r in kept], dtype="<i4").tobytes())
            fh.write(np.array([project_ix[r[2]] for r in kept], dtype="<i4").tobytes())
            fh.write(np.array([channel_ix[r[3]] for r in kept], dtype="<i4").tobytes())
            fh.write(
                np.array(
                    [
                        NULL_CODE if value is None else source_ix[value]
                        for value in row_sources
                    ],
                    dtype="<i4",
                ).tobytes()
            )
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
        "bytes": os.path.getsize(out),
    }

def _is_canonical(value: str) -> bool:
    return (
        len(value) == CREATED_AT_WIDTH
        and value[4] == value[7] == "-"
        and value[10] == " "
        and value[13] == value[16] == ":"
        and all(value[i].isdigit() for i in (0, 1, 2, 3, 5, 6, 8, 9, 11, 12, 14, 15, 17, 18))
    )


async def _stream_embeddings(db, fh, table: str, watermark: int, width: int) -> int:
    """Append the blobs in canonical order, without holding them all at once.

    The predicate mirrors the metadata pass exactly — same watermark, same width,
    same canonical-created_at test, same ORDER BY — so the two passes describe one
    row set. Global by design, like the first pass.
    """
    iso = isolation_where(agent_id=None)
    sql = (
        f"SELECT embedding FROM {table}"
        f" WHERE embedding IS NOT NULL AND id <= ? AND length(embedding) = ?"
        f"   AND {_canonical_predicate()}{iso.and_clause}"
        f" ORDER BY created_at DESC, id ASC"
    )
    written = 0
    async with db.execute(sql, (watermark, width, *iso.params)) as cursor:
        while True:
            rows = await cursor.fetchmany(512)
            if not rows:
                break
            fh.write(b"".join(r[0] for r in rows))
            written += len(rows)
    return written


def load_index(table: str = "memories", path: str | None = None) -> VectorIndex | None:
    """Map an index file, or return None when there is none.

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

    # One memmap per array, addressed by offset, rather than one uint8 map that is
    # re-viewed: a view across a slice has to satisfy numpy's alignment rules for
    # the target dtype, and expressing the offsets here keeps the layout in the
    # code that reads it instead of in a chain of pointer arithmetic.
    def _map(offset: int, dtype: str, shape) -> np.ndarray:
        return np.memmap(src, dtype=dtype, mode="r", offset=offset, shape=shape)

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


def _main() -> int:  # pragma: no cover - operator entry point
    import argparse
    import asyncio

    ap = argparse.ArgumentParser(description="Build the contiguous embedding index.")
    ap.add_argument("--table", default="memories", choices=("memories", "episodes"))
    args = ap.parse_args()

    async def run() -> int:
        # The read seam, not get_db(): the commit/rollback boundary belongs to
        # database.py, and a builder that only reads has no business owning one.
        from cpersona.database import close_db, connection

        try:
            async with connection() as db:
                result = await build_index(db, args.table)
        finally:
            # Every aiosqlite connection owns a non-daemon worker thread; without
            # this the process would not exit after printing.
            await close_db()
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("built") else 1

    return asyncio.run(run())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())


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
