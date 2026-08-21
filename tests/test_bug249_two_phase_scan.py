"""bug-249: the local vector scan ranks on `(id, embedding)` and hydrates only
the rows that clear the threshold.

Two distinct claims are pinned here, because passing one says nothing about the
other:

    equivalence   the split returns exactly what the single-query scan returned
                  -- same rows, same order, same scores, same field set. The
                  oracle is a verbatim copy of the pre-split implementation
                  (`_legacy_scan_memories_local`), run against the same rows in
                  the same process, so nobody writes down what the answer should
                  be.
    amplification the payload columns are read for the SURVIVORS, not for the
                  window. This is the whole point of the change and it is
                  invisible to any assertion about the return value: a refactor
                  that re-added `content` to the phase-1 SELECT would keep every
                  equivalence test green.

The scan window is `MAX_MEMORIES` rows regardless of the response limit
(bug-085), and the write cap is 16000 characters, so "read the window's text in
order to rank it" is worth up to ~160M characters per recall -- on the path that
every session-start recall takes.
"""

import os
import tempfile

os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "test_bug249.db"))
os.environ.setdefault("CPERSONA_EMBEDDING_MODE", "none")

import numpy as np  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from conftest import FakeEmbeddingClient, fake_embed_one  # noqa: E402
from cpersona import vector  # noqa: E402
from cpersona.database import get_db  # noqa: E402
from cpersona.isolation import isolation_where  # noqa: E402

AGENT = "agent.bug249"
TOPIC = "zebra migration corridor sighting report"
_PURGE = "DELETE FROM memories WHERE agent_id = ?"


# ---------------------------------------------------------------------------
# The oracle: the implementation as it stood before the split.
# ---------------------------------------------------------------------------


async def _legacy_scan_memories_local(
    db,
    iso,
    src_clause,
    src_params,
    scan_limit,
    query_vec,
    query_dim,
    effective_min_sim,
    limit=None,  # accepted and ignored: the pre-split scan had no such bound
):
    """Pre-bug-249 `_scan_memories_local`: one statement, payload columns
    materialised for the whole window before any similarity exists.

    Copied rather than paraphrased -- a paraphrase would be a second opinion
    about what the old code did, and the point of a differential test is to keep
    opinions out of the comparison.
    """
    rows = await db.execute_fetchall(
        f"""SELECT id, msg_id, content, source, timestamp, embedding
           FROM memories
           WHERE {iso.clause} AND embedding IS NOT NULL{src_clause}
           ORDER BY created_at DESC
           LIMIT ?""",
        (*iso.params, *src_params, scan_limit),
    )
    if not rows:
        return []

    valid_rows = []
    blobs = []
    for row in rows:
        blob = row[5]
        if blob and len(blob) == query_dim * 4:
            valid_rows.append(row)
            blobs.append(blob)

    if not valid_rows:
        return []

    sims = vector._cosine_batch(query_vec, query_dim, blobs)

    candidates = []
    for i, sim_val in enumerate(sims):
        if sim_val >= effective_min_sim:
            mem_id, msg_id, content, source, timestamp, _ = valid_rows[i]
            sim = float(sim_val)
            candidates.append(
                (
                    sim,
                    {
                        "id": mem_id,
                        "_rid": ("mem", mem_id),
                        "_cosine": sim,
                        "msg_id": msg_id,
                        "content": content,
                        "source": source,
                        "timestamp": timestamp,
                    },
                )
            )
    return candidates


# ---------------------------------------------------------------------------
# Corpus + argument construction
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def corpus():
    """A window that exercises every branch the split touches: the γ project
    axis, the channel axis, a source-tagged row, a NULL embedding, a
    foreign-width embedding, and rows on both sides of the threshold."""
    db = await get_db()
    await db.execute(_PURGE, (AGENT,))
    await db.commit()

    pack = FakeEmbeddingClient.pack_embedding
    rows = []

    def add(content, *, project_id="", channel="", source="{}", embedding="ok", msg_id=""):
        if embedding == "ok":
            blob = pack(fake_embed_one(content))
        elif embedding == "foreign":
            # A mid-flight model swap leaves rows at another width behind.
            blob = pack(fake_embed_one(content)[:16])
        else:
            blob = None
        created = f"2026-03-01 00:{len(rows) // 60:02d}:{len(rows) % 60:02d}"
        rows.append((AGENT, msg_id, content, source, "t", blob, created, project_id, channel))

    add(TOPIC)
    add("zebra migration corridor notes, second copy")
    add("zebra corridor", project_id="proj-x")
    add("zebra migration in the project bucket", project_id="proj-x", channel="c1")
    add("zebra sighting in another project", project_id="proj-y")
    add("zebra migration on a channel", channel="c1")
    add("zebra migration on another channel", channel="c2")
    add("zebra migration from a tagged source", source='{"id": "user-42", "type": "User"}')
    add("zebra migration with a msg id", msg_id="m-9")
    add("zebra migration, unembedded", embedding=None)
    add("zebra migration, foreign width", embedding="foreign")
    for i in range(20):
        add(f"unrelated grocery errand note number {i}")

    await db.executemany(
        "INSERT INTO memories"
        " (agent_id, msg_id, content, source, timestamp, embedding, created_at,"
        "  project_id, channel)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    await db.commit()
    yield db
    await db.execute(_PURGE, (AGENT,))
    await db.commit()


def _args(*, project_id=None, channel="", source_id="", min_sim=0.0, limit=vector.MAX_MEMORIES):
    """Build the scan's arguments exactly as `_search_vector` does.

    `limit` defaults to the scan window, which is the no-truncation case: the
    equivalence claims below are about the split itself, and the `limit` bound is
    a separate claim with its own section.
    """
    iso = isolation_where(agent_id=AGENT, project_id=project_id, channel=channel)
    src_like = vector._escape_like_prefix(source_id)
    src_clause = " AND json_extract(source, '$.id') LIKE ? ESCAPE '\\'" if src_like else ""
    src_params = (src_like,) if src_like else ()
    query_vec = np.array(fake_embed_one(TOPIC), dtype=np.float32)
    return dict(
        iso=iso,
        src_clause=src_clause,
        src_params=src_params,
        scan_limit=vector.MAX_MEMORIES,
        limit=limit,
        query_vec=query_vec,
        query_dim=len(query_vec),
        effective_min_sim=min_sim,
    )


# ---------------------------------------------------------------------------
# Claim 1: equivalence with the pre-split scan
# ---------------------------------------------------------------------------


_SCENARIOS = {
    "no-filter": dict(),
    "threshold-cuts": dict(min_sim=0.35),
    "threshold-above-everything": dict(min_sim=1.5),
    "project-gamma": dict(project_id="proj-x"),
    "project-global-only": dict(project_id=""),
    "channel": dict(channel="c1"),
    "source-prefix": dict(source_id="user-"),
    "source-and-channel": dict(source_id="user-", channel="c1"),
    "unknown-source": dict(source_id="nobody-"),
}


@pytest.mark.parametrize("name", sorted(_SCENARIOS))
@pytest.mark.asyncio
async def test_two_phase_scan_matches_the_single_query_scan(corpus, name):
    """Same rows, same order, same scores and same field set as before the split."""
    args = _args(**_SCENARIOS[name])
    expected = await _legacy_scan_memories_local(corpus, **args)
    actual = await vector._scan_memories_local(corpus, **args)

    assert [item for _, item in actual] == [item for _, item in expected], (
        f"[{name}] the split changed the returned rows, their order or their fields"
    )
    assert [score for score, _ in actual] == [score for score, _ in expected], (
        f"[{name}] the split changed the similarity scores"
    )


@pytest.mark.asyncio
async def test_a_similarity_exactly_on_the_threshold_survives_on_both_paths(corpus):
    """`>=`, not `>`. The scenario matrix cannot reach this boundary: it uses
    round thresholds no row's score lands on, so flipping the comparison would
    move nothing and the equivalence tests would stay green."""
    args = _args()
    rows = await corpus.execute_fetchall(
        "SELECT id, embedding FROM memories WHERE agent_id = ? AND content = ?",
        (AGENT, TOPIC),
    )
    row_id, blob = rows[0]
    exact = float(vector._cosine_batch(args["query_vec"], args["query_dim"], [blob])[0])

    args["effective_min_sim"] = exact
    expected = await _legacy_scan_memories_local(corpus, **args)
    actual = await vector._scan_memories_local(corpus, **args)
    assert row_id in {item["id"] for _, item in expected}, "the oracle lost the boundary row"
    assert [item for _, item in actual] == [item for _, item in expected]


@pytest.mark.asyncio
async def test_the_scenario_matrix_is_not_all_empty(corpus):
    """A matrix in which every case returns [] would pass the equivalence test
    while proving nothing. Pin that the filters admit rows, and that they do not
    all admit the SAME rows."""
    populated = {}
    for name, kw in _SCENARIOS.items():
        got = await vector._scan_memories_local(corpus, **_args(**kw))
        if got:
            populated[name] = tuple(item["id"] for _, item in got)
    assert len(populated) >= 6, populated
    assert len(set(populated.values())) >= 5, (
        f"the filters are not discriminating between rows: {populated}"
    )


# Two distinct contents that embed to the same vector: the deterministic test
# embedding sums a per-token vector, so a permutation of the same tokens is the
# same point in cosine space. Distinct content is required -- (agent_id,
# project_id, channel, content) is UNIQUE.
_TIE_A = "tiebreaker alpha beta"
_TIE_B = "beta tiebreaker alpha"


async def _add_tie_rows(db):
    """Insert the two equal-scoring rows and return their ids in scan order."""
    assert fake_embed_one(_TIE_A) == fake_embed_one(_TIE_B), "the tie must be real"
    await db.executemany(
        "INSERT INTO memories"
        " (agent_id, content, source, timestamp, embedding, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                AGENT,
                content,
                "{}",
                "t",
                FakeEmbeddingClient.pack_embedding(fake_embed_one(content)),
                created,
            )
            for content, created in (
                (_TIE_A, "2026-04-01 00:00:00"),
                (_TIE_B, "2026-04-02 00:00:00"),
            )
        ],
    )
    await db.commit()


@pytest.mark.asyncio
async def test_survivors_keep_the_scan_order_when_scores_tie(corpus):
    """Rows with equal similarity are ordered by the scan (created_at DESC) --
    `heapq.nlargest` in `_search_vector` is stable, so that order is what breaks
    the tie, and hydrating through an id-keyed map must not disturb it."""
    await _add_tie_rows(corpus)

    args = _args()
    expected = await _legacy_scan_memories_local(corpus, **args)
    actual = await vector._scan_memories_local(corpus, **args)
    ties = [(s, i["id"]) for s, i in actual if i["content"] in (_TIE_A, _TIE_B)]
    assert len(ties) == 2 and ties[0][0] == ties[1][0], ties
    assert ties == [(s, i["id"]) for s, i in expected if i["content"] in (_TIE_A, _TIE_B)]


# ---------------------------------------------------------------------------
# Claim 1b: bounding the hydrate by `limit` cannot change a recall answer
# ---------------------------------------------------------------------------
#
# The seam-level equivalence above runs with limit == the scan window, so it says
# nothing about the bound. The bound IS a change at that seam -- `_scan_memories_local`
# now returns at most `limit` candidates where it used to return every survivor --
# and the claim being made is one level up: `_search_vector` merges the memory
# candidates with the episode candidates and takes `heapq.nlargest(limit, ...)`, so
# a memory with `limit` memories already above it cannot place whatever the episodes
# do. Pinning that requires the oracle at `_search_vector`, with episodes in the
# corpus and a threshold permissive enough that the bound actually bites.


@pytest_asyncio.fixture
async def episodes(corpus):
    """Episode rows that compete with the memories for the same response slots."""
    db = corpus
    await db.execute("DELETE FROM episodes WHERE agent_id = ?", (AGENT,))
    await db.commit()
    rows = [
        (
            AGENT,
            summary,
            FakeEmbeddingClient.pack_embedding(fake_embed_one(summary)),
            f"2026-03-02 00:00:{i:02d}",
        )
        for i, summary in enumerate(
            [
                "zebra migration corridor sighting report",  # ties the top memory
                "zebra migration corridor session summary",
                "unrelated grocery errand summary",
            ]
        )
    ]
    await db.executemany(
        "INSERT INTO episodes (agent_id, summary, embedding, created_at)"
        " VALUES (?, ?, ?, ?)",
        rows,
    )
    await db.commit()
    yield db
    await db.execute("DELETE FROM episodes WHERE agent_id = ?", (AGENT,))
    await db.commit()


@pytest.mark.parametrize("limit", [1, 2, 3, 5, 10, 1000])
@pytest.mark.asyncio
async def test_the_limit_bound_does_not_change_the_recall_answer(
    episodes, monkeypatch, fake_embedding_client, limit
):
    """Same rows, same order, same scores out of `_search_vector` as when the
    scan returned every survivor."""
    db = episodes

    async def _unbounded(
        db_, iso, src_clause, src_params, scan_limit, _limit, query_vec, query_dim, min_sim
    ):
        """The scan as it was: every survivor, no `limit` bound."""
        return await _legacy_scan_memories_local(
            db_, iso, src_clause, src_params, scan_limit, query_vec, query_dim, min_sim
        )

    real = vector._scan_memories_local
    monkeypatch.setattr(vector, "_scan_memories_local", _unbounded)
    expected = await vector._search_vector(db, AGENT, TOPIC, limit, min_similarity=0.0)
    monkeypatch.setattr(vector, "_scan_memories_local", real)
    actual = await vector._search_vector(db, AGENT, TOPIC, limit, min_similarity=0.0)

    assert actual == expected, f"the `limit` bound changed the answer at limit={limit}"


@pytest.mark.asyncio
async def test_the_limit_bound_actually_bites_in_that_comparison(
    episodes, fake_embedding_client
):
    """The parametrisation above is only evidence while the bound removes rows.
    If every survivor fitted under `limit`, the comparison would be vacuous."""
    survivors = await _legacy_scan_memories_local(episodes, **_args(min_sim=0.0))
    assert len(survivors) > 10, (
        f"only {len(survivors)} survivors clear the threshold; the limits under "
        "test do not truncate anything"
    )
    bounded = await vector._scan_memories_local(episodes, **_args(min_sim=0.0, limit=3))
    assert len(bounded) == 3


@pytest.mark.asyncio
async def test_the_bound_breaks_a_tie_at_its_own_edge_the_way_nlargest_would(corpus):
    """The one case where the bound's tie-break is observable: two equal scores
    straddling `limit`. `nlargest` would have kept the one the scan saw first, so
    the bound must keep that one -- the other tests all place their ties on the
    same side of the cut, where either choice looks correct."""
    await _add_tie_rows(corpus)
    unbounded = await _legacy_scan_memories_local(corpus, **_args(min_sim=-1.0))
    ranked = sorted(range(len(unbounded)), key=lambda i: (unbounded[i][0], -i), reverse=True)

    tie_ranks = [r for r, i in enumerate(ranked) if unbounded[i][1]["content"] in (_TIE_A, _TIE_B)]
    assert len(tie_ranks) == 2 and tie_ranks[1] == tie_ranks[0] + 1, tie_ranks
    assert unbounded[ranked[tie_ranks[0]]][0] == unbounded[ranked[tie_ranks[1]]][0]

    cut = tie_ranks[0] + 1  # exactly one of the tied rows fits
    bounded = await vector._scan_memories_local(corpus, **_args(min_sim=-1.0, limit=cut))
    kept = [item["content"] for _, item in bounded if item["content"] in (_TIE_A, _TIE_B)]
    assert kept == [unbounded[ranked[tie_ranks[0]]][1]["content"]], (
        "the bound kept the wrong side of a tie it had to split"
    )
    assert [item["id"] for _, item in bounded] == [
        unbounded[i][1]["id"] for i in sorted(ranked[:cut])
    ]


@pytest.mark.asyncio
async def test_the_bound_keeps_the_highest_scoring_survivors(corpus):
    """Not just `limit` of them -- the top `limit`, in scan order."""
    unbounded = await _legacy_scan_memories_local(corpus, **_args(min_sim=0.0))
    bounded = await vector._scan_memories_local(corpus, **_args(min_sim=0.0, limit=4))
    top = sorted(range(len(unbounded)), key=lambda i: (unbounded[i][0], -i), reverse=True)[:4]
    assert [item["id"] for _, item in bounded] == [
        unbounded[i][1]["id"] for i in sorted(top)
    ]


# ---------------------------------------------------------------------------
# Claim 2: the payload columns are read for the survivors, not for the window
# ---------------------------------------------------------------------------


class _RecordingDb:
    """Pass-through connection proxy that records every statement and the text
    it materialises.

    A proxy rather than a monkeypatch of `_fetch_rows_by_id`: what has to be
    observed is the phase-1 SELECT itself -- its column list and the bytes it
    brings back -- and that statement is issued straight at the connection.
    """

    def __init__(self, db):
        self._conn = db
        self.calls = []  # (sql, params, rows)

    async def execute_fetchall(self, sql, params=()):
        rows = await self._conn.execute_fetchall(sql, params)
        self.calls.append((sql, tuple(params), rows))
        return rows

    @staticmethod
    def text_chars(rows):
        """Characters of text the statement handed back to Python."""
        return sum(len(v) for row in rows for v in row if isinstance(v, str))


@pytest.mark.asyncio
async def test_ranking_phase_reads_no_payload_text(corpus):
    """The window's text must not cross the SQLite boundary to be ranked."""
    spy = _RecordingDb(corpus)
    got = await vector._scan_memories_local(spy, **_args(min_sim=0.35))
    assert got, "the scenario must have survivors, or it proves nothing"

    ranking_sql, _, ranking_rows = spy.calls[0]
    for column in ("content", "msg_id", "source", "timestamp"):
        assert column not in ranking_sql, (
            f"the ranking statement selects `{column}`, so the whole scan window's "
            "payload is materialised again before a single cosine exists (bug-249)"
        )
    assert spy.text_chars(ranking_rows) == 0, (
        "the ranking statement returned text; only (id, embedding) may cross"
    )


@pytest.mark.asyncio
async def test_only_survivors_are_hydrated(corpus):
    """The hydrate binds the rows that cleared the threshold -- not the window."""
    spy = _RecordingDb(corpus)
    got = await vector._scan_memories_local(spy, **_args(min_sim=0.35))

    assert len(spy.calls) == 2, [sql for sql, _, _ in spy.calls]
    _, _, ranking_rows = spy.calls[0]
    hydrate_sql, hydrate_params, _ = spy.calls[1]

    assert "content" in hydrate_sql
    scanned = {row[0] for row in ranking_rows}
    hydrated = {p for p in hydrate_params if p in scanned}
    assert hydrated == {item["id"] for _, item in got}
    assert len(hydrated) < len(scanned), (
        f"every scanned row was hydrated ({len(hydrated)}/{len(scanned)}); the "
        "threshold is not narrowing the payload read"
    )


@pytest.mark.asyncio
async def test_the_hydrate_is_bounded_by_limit_not_by_the_threshold(corpus):
    """A permissive threshold must not enlarge the payload read. Without the
    bound this is the regression case: hydrating every survivor by id is slower
    than the sequential read the split replaced."""
    spy = _RecordingDb(corpus)
    got = await vector._scan_memories_local(spy, **_args(min_sim=0.0, limit=3))
    assert len(got) == 3

    _, _, ranking_rows = spy.calls[0]
    _, hydrate_params, _ = spy.calls[1]
    scanned = {row[0] for row in ranking_rows}
    assert len(scanned) > 20, "the window must be much larger than the limit"
    assert len({p for p in hydrate_params if p in scanned}) == 3


@pytest.mark.asyncio
async def test_no_survivors_issues_no_hydrate_at_all(corpus):
    """A threshold nothing clears must cost one statement, not two."""
    spy = _RecordingDb(corpus)
    assert await vector._scan_memories_local(spy, **_args(min_sim=1.5)) == []
    assert len(spy.calls) == 1, [sql for sql, _, _ in spy.calls]


@pytest.mark.asyncio
async def test_a_row_dropped_between_the_phases_is_skipped(corpus):
    """The split opens a window the single statement did not have. A survivor
    whose row is gone by hydrate time is dropped silently -- the treatment the
    remote branch already gives a stale index hit -- rather than surfacing a
    result whose content is None."""

    class _RetaggingDb(_RecordingDb):
        async def execute_fetchall(self, sql, params=()):
            rows = await self._conn.execute_fetchall(sql, params)
            self.calls.append((sql, tuple(params), rows))
            if len(self.calls) == 1:  # the ranking statement has just returned
                await self._conn.execute(
                    "UPDATE memories SET agent_id = ? WHERE agent_id = ? AND content = ?",
                    (AGENT + ".moved", AGENT, TOPIC),
                )
                await self._conn.commit()
            return rows

    rows = await corpus.execute_fetchall(
        "SELECT id FROM memories WHERE agent_id = ? AND content = ?", (AGENT, TOPIC)
    )
    moved_id = rows[0][0]

    spy = _RetaggingDb(corpus)
    got = await vector._scan_memories_local(spy, **_args(min_sim=0.35))
    assert got, "the remaining survivors must still be returned"
    assert moved_id not in {item["id"] for _, item in got}, (
        "a survivor the hydrate could not resolve was emitted anyway"
    )
    assert all(item["content"] for _, item in got), (
        f"a result carries no content: {[i for _, i in got if not i['content']]}"
    )
    await corpus.execute(_PURGE, (AGENT + ".moved",))
    await corpus.commit()
