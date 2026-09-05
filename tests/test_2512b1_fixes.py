"""Regression tests for the 2.5.12b1 comprehensive fix pass.

Each defect here was re-measured on the b1 branch point before it was fixed --
the 2026-07-29 audit had adjudicated them "mechanism true, impact minor" and
left them unregistered, and two of the group turned out to have been closed
since by unrelated work. What survived was reproduced live rather than read off
the report, and every test below is written against that reproduction: it fails
with the fix reverted, which is the only property that makes it a regression
test rather than a description.

  bug-299  archive_episode lost the whole episode to a TypeError when history
           carried mixed-type timestamps -- the write-side sibling of bug-291.
  bug-300  a same-width non-finite embedding produced a NaN `_cosine` on the
           backfill path, which RFC 8259 does not admit as a number.
  bug-301  the backfill's `IN (...)` was unchunked while its sibling in
           vector.py splits at the same bound.
  bug-304  a remote index push reported partial synchronisation as success,
           at a log level that is off.
"""

import os
import tempfile

os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "test_2512b1.db"))
os.environ.setdefault("CPERSONA_EMBEDDING_MODE", "none")

import logging  # noqa: E402
import struct  # noqa: E402

import pytest  # noqa: E402

from cpersona import memory_handlers, session, vector  # noqa: E402
from cpersona.database import get_db  # noqa: E402

AGENT = "agent.2512b1"


# ===========================================================================
# bug-299 — a non-string timestamp in `history` cost the whole episode.
# ===========================================================================


@pytest.mark.asyncio
async def test_archive_episode_survives_mixed_type_timestamps():
    """`min()`/`max()` ran on whatever the caller put in `timestamp`, so one
    epoch int beside one ISO string raised TypeError out of the entire call --
    BEFORE the embed and the INSERT, so the episode was lost rather than merely
    stamped poorly. The declared contract is an ISO string; a value that is not
    one names no instant, so it is read as absent (bug-291's ruling, applied on
    the write side) and the well-formed stamps still bound the span.
    """
    session.reset_pauses_for_tests()
    result = await memory_handlers.do_archive_episode(
        AGENT,
        [
            {"role": "user", "content": "a", "timestamp": {"not": "a stamp"}},
            {"role": "assistant", "content": "b", "timestamp": "2026-03-02T00:00:00+00:00"},
            {"role": "user", "content": "c", "timestamp": 1772000000},
            {"role": "assistant", "content": "d", "timestamp": "2026-03-01T00:00:00+00:00"},
        ],
        summary="an episode whose history mixes stamp types",
    )

    assert result["ok"] is True, result
    episode_id = result["episode_id"]
    assert episode_id

    db = await get_db()
    row = await db.execute_fetchall(
        "SELECT start_time, end_time FROM episodes WHERE id = ?", (episode_id,)
    )
    start_time, end_time = row[0]
    # The two ISO stamps bound the span; the int and the dict contribute nothing
    # rather than inventing a position in the chronology for themselves.
    assert start_time == "2026-03-01T00:00:00+00:00"
    assert end_time == "2026-03-02T00:00:00+00:00"


@pytest.mark.asyncio
async def test_archive_episode_with_no_usable_timestamps_still_stores():
    """The degenerate case: nothing usable at all leaves the span unset, which
    is where an episode with no `timestamp` keys already sat."""
    session.reset_pauses_for_tests()
    result = await memory_handlers.do_archive_episode(
        AGENT,
        [{"role": "user", "content": "a", "timestamp": 0}, {"role": "user", "content": "b"}],
        summary="an episode with no usable stamps",
    )
    assert result["ok"] is True, result

    db = await get_db()
    row = await db.execute_fetchall(
        "SELECT start_time, end_time FROM episodes WHERE id = ?", (result["episode_id"],)
    )
    assert row[0] == (None, None)


# ===========================================================================
# bug-300 / bug-301 — the cosine backfill's read path.
# ===========================================================================


class _RecordingDB:
    """Answers the backfill's only query and records each statement it ran."""

    def __init__(self, blobs: dict[int, bytes | None]):
        self._blobs = blobs
        self.statements: list[tuple[str, list]] = []

    async def execute_fetchall(self, sql: str, params):
        self.statements.append((sql, list(params)))
        return [(rid, self._blobs.get(rid)) for rid in params if rid in self._blobs]


class _FixedEmbedder:
    def __init__(self, dim: int):
        self._dim = dim

    async def embed(self, texts):
        return [[1.0] * self._dim for _ in texts]


@pytest.mark.asyncio
async def test_backfill_leaves_a_non_finite_blob_uncosined(monkeypatch):
    """bug-300: width was the only thing checked, so a correctly sized blob
    holding a NaN multiplied straight through to `_cosine`, and from there into
    `match_reason` and `confidence` as a bare NaN -- a token RFC 8259 does not
    admit, emitted by the one branch that exists to give these rows a REAL
    signal. The write seam refuses to store such a blob and check_health reports
    the ones already stored, but neither reaches the read side, which is where a
    row written by an older version or by an import arrives. The row keeps the
    None it had, which is the branch it sat on before the backfill existed.
    """
    dim = 8
    finite = struct.pack(f"<{dim}f", *([1.0] * dim))
    nonfinite = struct.pack(f"<{dim}f", *([float("nan")] + [1.0] * (dim - 1)))
    db = _RecordingDB({1: finite, 2: nonfinite})
    monkeypatch.setattr(vector, "_embedding_client", _FixedEmbedder(dim))

    rows = [
        {"id": 1, "content": "healthy row", "_cosine": None},
        {"id": 2, "content": "row with a NaN axis", "_cosine": None},
    ]
    await memory_handlers._backfill_cosines(db, rows, "a query", None, "")

    # The control proves the fixture can backfill at all, so the None below is
    # evidence about the guard and not about a backfill that did nothing.
    assert isinstance(rows[0]["_cosine"], float)
    assert rows[0]["_cosine"] == rows[0]["_cosine"], "the control row is itself NaN"
    assert rows[1]["_cosine"] is None, "a non-finite blob was scored instead of skipped"
    assert "_cosine_backfilled" not in rows[1]


@pytest.mark.asyncio
async def test_backfill_chunks_its_id_lookup_like_its_sibling(monkeypatch):
    """bug-301: `vector._fetch_rows_by_id` splits its `IN (...)` at
    `_ID_FETCH_CHUNK`; this one built a single placeholder list of whatever
    length the caller's result set happened to be. Both read the same two tables
    by id on the same recall, so the bound belongs to the pair.
    """
    dim = 4
    blob = struct.pack(f"<{dim}f", *([1.0] * dim))
    count = vector._ID_FETCH_CHUNK * 2 + 1
    ids = list(range(1, count + 1))
    db = _RecordingDB({rid: blob for rid in ids})
    monkeypatch.setattr(vector, "_embedding_client", _FixedEmbedder(dim))

    rows = [{"id": rid, "content": f"row {rid}", "_cosine": None} for rid in ids]
    await memory_handlers._backfill_cosines(db, rows, "a query", None, "")

    memory_statements = [s for s in db.statements if "FROM memories" in s[0]]
    assert len(memory_statements) == 3, (
        f"{count} ids were fetched in {len(memory_statements)} statement(s); the lookup is "
        f"not chunked at vector._ID_FETCH_CHUNK ({vector._ID_FETCH_CHUNK})"
    )
    assert all(len(params) <= vector._ID_FETCH_CHUNK for _, params in memory_statements)
    # Chunking must not lose a row: every id still got its cosine.
    assert sum(1 for r in rows if r["_cosine"] is not None) == count


# ===========================================================================
# bug-304 — a remote index push that only partly landed.
# ===========================================================================


class _StubResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


class _StubHTTP:
    """Answers the first chunk and refuses the rest, one way or the other."""

    def __init__(self, failure: str):
        self._failure = failure
        self.calls = 0

    async def post(self, url, json=None):
        self.calls += 1
        if self.calls == 1:
            return _StubResponse(200)
        if self._failure == "status":
            return _StubResponse(500)
        raise RuntimeError("connection reset")


class _StubClient:
    def __init__(self, http):
        self._http_url = "http://example.invalid/embed"
        self._client = http


@pytest.mark.parametrize("failure", ["status", "exception"])
@pytest.mark.asyncio
async def test_partial_remote_index_is_reported_not_swallowed(monkeypatch, caplog, failure):
    """Every chunk's exception went to `logger.debug` -- off in any normal
    deployment -- and the response status was never read at all, so a service
    answering 500 to half the chunks counted as a full push. The caller returns
    ok:true either way, which is why the log is the only place partial
    synchronisation can be seen: rows that are in the database and not in the
    index answer recalls with silence, and silence is the one symptom that does
    not look like a fault.
    """
    http = _StubHTTP(failure)
    monkeypatch.setattr(vector, "VECTOR_SEARCH_MODE", "remote")
    monkeypatch.setattr(vector, "_embedding_client", _StubClient(http))

    items = [{"id": i, "content": f"row {i}"} for i in range(300)]  # 3 chunks of 128
    with caplog.at_level(logging.WARNING, logger=vector.logger.name):
        await vector.remote_index_upsert(AGENT, items)

    assert http.calls == 3, "a failing chunk stopped the ones after it; the push is not resumable"
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "a partial push was silent at WARNING; only debug knew about it"
    message = warnings[0].getMessage()
    assert "2 of 3 chunks" in message, message
    assert AGENT in message, "the warning does not say whose index is short"


@pytest.mark.asyncio
async def test_a_fully_successful_push_says_nothing(monkeypatch, caplog):
    """The counterpart: the warning must mean something when it appears."""

    class _AllGood:
        calls = 0

        async def post(self, url, json=None):
            _AllGood.calls += 1
            return _StubResponse(200)

    monkeypatch.setattr(vector, "VECTOR_SEARCH_MODE", "remote")
    monkeypatch.setattr(vector, "_embedding_client", _StubClient(_AllGood()))

    with caplog.at_level(logging.WARNING, logger=vector.logger.name):
        await vector.remote_index_upsert(AGENT, [{"id": i} for i in range(300)])

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
