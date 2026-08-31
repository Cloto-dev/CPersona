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
FORMAT_VERSION = 1
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

    widths: dict[int, int] = {}
    for r in meta:
        widths[r[5]] = widths.get(r[5], 0) + 1
    # Ties broken by the larger width so the choice is a function of the data,
    # not of dict ordering: a rebuild on the same corpus must pick the same one.
    modal_width = max(widths.items(), key=lambda kv: (kv[1], kv[0]))[0]
    if not modal_width or modal_width % 4:
        return {"built": False, "reason": f"embedding width {modal_width} is not float32-aligned"}
    dim = modal_width // 4

    # A row of another width is absent from the index AND unwanted by a query of
    # this dimension — the live scan skips it for the same reason — so it is not
    # an exclusion, merely not here. A row this format cannot spell IS an
    # exclusion, and stays reachable because the query path unions the list into
    # its exact tail read.
    kept, excluded = [], []
    for r in meta:
        if r[5] != modal_width:
            continue
        if not _is_canonical(r[4]):
            excluded.append(int(r[0]))
            continue
        kept.append(r)

    if len(excluded) > MAX_EXCLUDED_IDS:
        return {
            "built": False,
            "reason": f"{len(excluded)} rows carry a non-canonical created_at (cap {MAX_EXCLUDED_IDS})",
            "watermark": watermark,
        }
    count = len(kept)
    if count == 0:
        return {"built": False, "reason": "no rows of the modal width", "watermark": watermark}

    agents, agent_ix = _intern([r[1] for r in kept])
    projects, project_ix = _intern([r[2] for r in kept])
    channels, channel_ix = _intern([r[3] for r in kept])
    sources, source_ix = _intern([r[6] for r in kept] if table == "memories" else [])

    header = {
        "format": FORMAT_VERSION,
        "table": table,
        "dim": dim,
        "dtype": "float32",
        "count": count,
        "watermark": watermark,
        "excluded_ids": excluded,
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

            written = await _stream_embeddings(db, fh, table, watermark, modal_width)
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
                        NULL_CODE if (table != "memories" or r[6] is None) else source_ix[r[6]]
                        for r in kept
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

    dim, count = int(header["dim"]), int(header["count"])
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
