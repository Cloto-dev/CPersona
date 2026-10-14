"""Health checks whose cost must track their input, and a drift key for every script.

Four checks that each did more work than the question they answer:

* The non-finite embedding scan pages the agent in keyset order. Scoped to an
  agent, the planner served the page from an agent index and sorted the whole
  agent once per page, so a scan of n rows did n/100 sorts of n rows. The merge
  copy passes page the same way. Pinned on the plan of the statements the code
  actually issues.
* ``deep_orphaned_episodes`` compared every episode with every memory of the
  agent (the window test wraps both sides in ``datetime()``, which no index can
  serve), and the report-only deep checks ran on the connection request reads
  share. Pinned on SQLite VM steps, which are deterministic, and on the
  connection the check is handed.
* ``deep_near_duplicate`` built a dict for every pair above the threshold to
  return twenty of them. Pinned on traced peak memory, and on the samples and
  count against a brute-force reference.
* ``axis_hygiene`` dropped everything outside ``[a-z0-9]``, so any two project
  ids written in another script folded to the same (often empty) key.
"""

from __future__ import annotations

import asyncio
import tracemalloc

import numpy as np
import pytest
import pytest_asyncio

from cpersona import admin_handlers, checks, database, maintenance_handlers
from cpersona.database import get_db

AGENT = "w05.agent"
OTHER = "w05.other"
UTC_TS = "2026-01-01T00:00:00+00:00"


@pytest_asyncio.fixture
async def db():
    conn = await get_db()
    for table in ("memories", "episodes"):
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()
    yield conn
    for table in ("memories", "episodes"):
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()


class _Recording:
    """Delegates to a connection and keeps every statement read through it."""

    def __init__(self, inner):
        self._inner = inner
        self.statements: list[tuple[str, tuple]] = []

    async def execute_fetchall(self, sql, params=()):
        self.statements.append((sql, tuple(params)))
        return await self._inner.execute_fetchall(sql, params)

    def __getattr__(self, name):
        return getattr(self._inner, name)


async def _plan(db, sql, params) -> list[str]:
    return [r[-1] for r in await db.execute_fetchall("EXPLAIN QUERY PLAN " + sql, params)]


def _blob(vec) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


# ---------------------------------------------------------------------------
# Keyset pages walk the primary key instead of sorting the agent per page
# ---------------------------------------------------------------------------


async def _seed_embedded_rows(db):
    vec = _blob(np.ones(8))
    for agent in (AGENT, OTHER):
        await db.execute(
            "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp, embedding)"
            " VALUES (?, '', '', ?, '{}', ?, ?)",
            (agent, f"row of {agent}", UTC_TS, vec),
        )
        await db.execute(
            "INSERT INTO episodes (agent_id, summary, embedding) VALUES (?, ?, ?)",
            (agent, f"episode of {agent}", vec),
        )
    await db.commit()


@pytest.mark.asyncio
async def test_scoped_nonfinite_scan_pages_without_sorting_the_agent(db):
    await _seed_embedded_rows(db)
    rec = _Recording(db)
    await checks.check_nonfinite_embedding(rec, AGENT, False)
    pages = [(sql, params) for sql, params in rec.statements if "LIMIT" in sql and "embedding" in sql]
    assert {("episodes" in sql) for sql, _ in pages} == {False, True}, rec.statements
    for sql, params in pages:
        plan = await _plan(db, sql, params)
        assert not any("TEMP B-TREE" in step for step in plan), (
            f"a scoped page sorts the whole agent each time it is read: {plan} for {sql}"
        )
        assert any("INTEGER PRIMARY KEY" in step for step in plan), plan


@pytest.mark.asyncio
async def test_scoped_nonfinite_scan_still_sees_only_its_agent(db):
    """The page no longer leans on the agent index, so the agent term must still filter."""
    await db.execute(
        "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp, embedding)"
        " VALUES (?, '', '', 'nan row', '{}', ?, ?)",
        (OTHER, UTC_TS, _blob([float("nan")] * 8)),
    )
    await _seed_embedded_rows(db)
    assert await checks.check_nonfinite_embedding(db, AGENT, False) == []
    found = await checks.check_nonfinite_embedding(db, OTHER, False)
    assert [(f["table"], f["count"]) for f in found] == [("memories", 1)]


@pytest.mark.asyncio
async def test_merge_pages_walk_the_primary_key(db, monkeypatch):
    await _seed_embedded_rows(db)

    async def _skip_page(*_args, **_kwargs):
        return None

    monkeypatch.setattr(admin_handlers, "_merge_memory_page", _skip_page)
    monkeypatch.setattr(admin_handlers, "_merge_episode_page", _skip_page)
    rec = _Recording(db)
    await admin_handlers._merge_memory_rows(rec, AGENT, "w05.target", None)
    await admin_handlers._merge_episode_rows(rec, AGENT, "w05.target", None)
    assert len(rec.statements) >= 2
    for sql, params in rec.statements:
        plan = await _plan(db, sql, params)
        assert not any("TEMP B-TREE" in step for step in plan), (
            f"a merge page sorts the whole source agent each time it is read: {plan} for {sql}"
        )
        assert any("INTEGER PRIMARY KEY" in step for step in plan), plan
    # The pages still read the source agent only.
    rows = await db.execute_fetchall(rec.statements[0][0], rec.statements[0][1])
    assert [r[3] for r in rows] == [f"row of {AGENT}"]


# ---------------------------------------------------------------------------
# deep_orphaned_episodes: linear in its input, and off the request connection
# ---------------------------------------------------------------------------


async def _seed_orphan_corpus(db, episodes: int, memories: int):
    # Memories on the offset-aware spelling, episodes on the naive one: the two
    # formats bug-116 exists for. Every episode is an orphan, the worst case for
    # a NOT EXISTS that cannot stop at the first match.
    await db.executemany(
        "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp)"
        " VALUES (?, '', '', ?, '{}', ?)",
        [
            (AGENT, f"m{i}", f"2026-01-01T{(i // 3600) % 24:02d}:{(i // 60) % 60:02d}:{i % 60:02d}+09:00")
            for i in range(memories)
        ],
    )
    await db.executemany(
        "INSERT INTO episodes (agent_id, summary, start_time, end_time) VALUES (?, ?, ?, ?)",
        [(AGENT, f"e{i}", f"2025-06-01 00:{i % 60:02d}:00", f"2025-06-01 00:{i % 60:02d}:30") for i in range(episodes)],
    )
    await db.commit()


async def _vm_steps(db, fn) -> tuple[int, dict]:
    steps = 0

    def tick():
        nonlocal steps
        steps += 1
        return 0

    await db.set_progress_handler(tick, 100)
    try:
        out = await fn()
    finally:
        await db.set_progress_handler(None, 100)
    return steps, out


@pytest.mark.asyncio
async def test_orphaned_episodes_cost_is_not_episodes_times_memories(db):
    await _seed_orphan_corpus(db, 150, 1500)
    small, out_small = await _vm_steps(db, lambda: checks.deep_orphaned_episodes(db, AGENT, False))
    assert out_small["count"] == 150
    for table in ("memories", "episodes"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()
    await _seed_orphan_corpus(db, 300, 3000)
    large, out_large = await _vm_steps(db, lambda: checks.deep_orphaned_episodes(db, AGENT, False))
    assert out_large["count"] == 300
    # Doubling both sides doubles linear work and quadruples E x M work.
    assert large < 3 * small, f"VM steps grew {large / small:.2f}x for 2x episodes and 2x memories"


@pytest.mark.asyncio
async def test_orphaned_episodes_keeps_the_window_semantics(db):
    rows = [
        # 09:00+09:00 is 00:00 UTC: the inclusive lower bound of episode `start-edge`.
        (AGENT, "2026-01-01T09:00:00+09:00"),
        # 00:10 UTC, the inclusive upper bound of episode `end-edge`.
        (AGENT, "2026-01-01 00:10:00"),
        (AGENT, "not a time"),
        # Inside `other-agent-only`, but another agent's row covers nothing here.
        (OTHER, "2026-02-01T00:00:30+00:00"),
    ]
    for agent, ts in rows:
        await db.execute(
            "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp)"
            " VALUES (?, '', '', ?, '{}', ?)",
            (agent, f"m {ts}", ts),
        )
    episodes = {
        "start-edge": ("2026-01-01 00:00:00", "2026-01-01 00:00:05"),
        "end-edge": ("2026-01-01T00:09:00Z", "2026-01-01 00:10:00"),
        "just-after": ("2026-01-01 00:00:01", "2026-01-01 00:00:09"),
        "reversed": ("2026-01-01 00:20:00", "2026-01-01 00:00:00"),
        "unparseable": ("garbage", "2026-01-01 00:30:00"),
        "other-agent-only": ("2026-02-01 00:00:00", "2026-02-01 00:01:00"),
        "nested-cover": ("2025-12-31 00:00:00", "2026-01-02 00:00:00"),
    }
    for summary, (start, end) in episodes.items():
        await db.execute(
            "INSERT INTO episodes (agent_id, summary, start_time, end_time) VALUES (?, ?, ?, ?)",
            (AGENT, summary, start, end),
        )
    await db.execute(
        "INSERT INTO episodes (agent_id, summary, start_time, end_time) VALUES (?, 'open', NULL, NULL)", (AGENT,)
    )
    await db.commit()
    out = await checks.deep_orphaned_episodes(db, AGENT, False)
    orphans = {s["summary"] for s in out["samples"]}
    assert out["count"] == 4
    assert orphans == {"just-after", "reversed", "unparseable", "other-agent-only"}


@pytest.mark.asyncio
async def test_report_only_deep_checks_run_on_the_background_connection(db, monkeypatch):
    seen = []

    async def probe(conn, agent_id, fix):
        seen.append(conn)
        return {"count": 0}

    monkeypatch.setitem(checks.DEEP_CHECKS, "orphaned_episodes", probe)
    await maintenance_handlers.do_deep_check(AGENT, checks=["orphaned_episodes"])
    async with database.background_connection() as bg, database.connection() as request_db:
        assert bg is not request_db, "the test needs a file database with separate readers"
        assert seen == [bg], "a report-only deep check ran on the connection request reads share"


# ---------------------------------------------------------------------------
# deep_near_duplicate: memory bounded by the matrix, not by the pair count
# ---------------------------------------------------------------------------


async def _insert_vectors(db, items):
    await db.executemany(
        "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp, embedding)"
        " VALUES (?, ?, '', ?, '{}', ?, ?)",
        [(AGENT, rest[0] if rest else "", content, UTC_TS, _blob(vec)) for content, vec, *rest in items],
    )
    await db.commit()


@pytest.mark.asyncio
async def test_near_duplicate_memory_does_not_grow_with_pairs(db):
    rng = np.random.default_rng(7)
    base = rng.standard_normal(64)
    n = 1000
    await _insert_vectors(
        db, [(f"Heartbeat check {i}: all services nominal", base + 0.001 * rng.standard_normal(64)) for i in range(n)]
    )
    tracemalloc.start()
    try:
        out = await checks.deep_near_duplicate(db, AGENT, False)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert out["pairs"] == n * (n - 1) // 2
    assert len(out["samples"]) == 20
    # The n x n float32 matrix is 4 MB here; a dict per pair is ~170 MB.
    assert peak < 40 * 1024 * 1024, f"traced peak {peak / 2**20:.0f} MB for {out['pairs']} pairs"


def _reference(rows):
    """The pre-change computation: every pair, then the stable sort, then 20."""
    matrix = np.frombuffer(b"".join(r[2] for r in rows), dtype=np.float32).reshape(len(rows), -1)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    unit = matrix / norms
    sims = unit @ unit.T
    pairs = []
    idx_a, idx_b = np.where(np.triu(sims, k=1) > checks.NEAR_DUPLICATE_COSINE)
    for a, b in zip(idx_a.tolist(), idx_b.tolist()):
        if rows[a][1] == rows[b][1]:
            continue
        pairs.append(
            {
                "id_a": rows[a][0],
                "id_b": rows[b][0],
                "cosine": round(float(sims[a, b]), 4),
                "preview_a": rows[a][1][:60],
                "preview_b": rows[b][1][:60],
            }
        )
    pairs.sort(key=lambda p: -p["cosine"])
    return len(pairs), pairs[:20]


@pytest.mark.asyncio
async def test_near_duplicate_count_and_samples_match_the_reference(db):
    rng = np.random.default_rng(11)
    items = []
    # Thirty rows on one identical vector: 435 pairs tied at 1.0, so the sample
    # is decided entirely by the tie order.
    same = rng.standard_normal(32)
    items += [(f"tied {i}", same) for i in range(30)]
    # Exact-content duplicates on that vector are duplicate_content's, not ours.
    # The dedup index allows them only across buckets.
    items += [("tied 3", same, "w05.p1"), ("tied 3", same, "w05.p2")]
    # A looser cluster spread around the threshold.
    centre = rng.standard_normal(32)
    items += [(f"loose {i}", centre + 0.18 * rng.standard_normal(32)) for i in range(25)]
    # Unrelated rows.
    items += [(f"noise {i}", rng.standard_normal(32)) for i in range(25)]
    order = rng.permutation(len(items))
    await _insert_vectors(db, [items[i] for i in order])

    rows = await db.execute_fetchall(
        "SELECT id, content, embedding FROM memories WHERE agent_id = ? AND embedding IS NOT NULL"
        " ORDER BY id DESC LIMIT ?",
        (AGENT, checks.NEAR_DUPLICATE_ROW_CAP),
    )
    want_count, want_samples = _reference(rows)
    out = await checks.deep_near_duplicate(db, AGENT, False)
    assert out["pairs"] == want_count
    assert out["samples"] == want_samples


@pytest.mark.asyncio
async def test_near_duplicate_ties_on_the_reported_cosine_not_the_raw_one(db):
    """Cosines that differ in the raw float but print the same are tied, as before.

    Thirty rows a hair apart: every pair rounds to 1.0, while the raw values
    differ in the last bits. The sample order among them is row-major, so a
    selection that ranked by the raw value would pick different pairs.
    """
    rng = np.random.default_rng(13)
    anchor = rng.standard_normal(32)
    await _insert_vectors(db, [(f"near {i}", anchor + 2e-3 * rng.standard_normal(32)) for i in range(30)])
    rows = await db.execute_fetchall(
        "SELECT id, content, embedding FROM memories WHERE agent_id = ? ORDER BY id DESC", (AGENT,)
    )
    want_count, want_samples = _reference(rows)
    assert want_count == 435
    assert {s["cosine"] for s in want_samples} == {1.0}
    out = await checks.deep_near_duplicate(db, AGENT, False)
    assert out["samples"] == want_samples


@pytest.mark.asyncio
async def test_near_duplicate_ranks_by_cosine_before_tie_order(db):
    """A pair found late must outrank earlier, weaker pairs once the 20 are full."""
    rng = np.random.default_rng(5)
    centre = rng.standard_normal(32)
    weak = [(f"weak {i}", centre + 0.12 * rng.standard_normal(32)) for i in range(12)]
    strong_vec = rng.standard_normal(32)
    # Inserted first, so it sits at the end of the id-descending row order.
    await _insert_vectors(db, [("strong a", strong_vec), ("strong b", strong_vec)] + weak)
    rows = await db.execute_fetchall(
        "SELECT id, content, embedding FROM memories WHERE agent_id = ? ORDER BY id DESC", (AGENT,)
    )
    want_count, want_samples = _reference(rows)
    assert want_count > 20
    out = await checks.deep_near_duplicate(db, AGENT, False)
    assert out["samples"] == want_samples
    assert {out["samples"][0]["preview_a"], out["samples"][0]["preview_b"]} == {"strong a", "strong b"}


@pytest.mark.asyncio
async def test_near_duplicate_compares_off_the_event_loop(db, monkeypatch):
    rng = np.random.default_rng(3)
    vec = rng.standard_normal(16)
    await _insert_vectors(db, [("a one", vec), ("a two", vec)])
    called = []
    real = asyncio.to_thread

    async def recording(fn, *args, **kwargs):
        called.append(fn)
        return await real(fn, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", recording)
    out = await checks.deep_near_duplicate(db, AGENT, False)
    assert out["pairs"] == 1
    assert called, "the pairwise comparison ran on the event loop"


# ---------------------------------------------------------------------------
# axis_hygiene: a drift key for every script
# ---------------------------------------------------------------------------


async def _project_rows(db, project_ids):
    for pid in project_ids:
        await db.execute(
            "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp)"
            " VALUES (?, ?, '', ?, '{}', ?)",
            (AGENT, pid, f"in {pid}", UTC_TS),
        )
    await db.commit()


def _clusters(issues):
    return sorted(sorted(m["project_id"] for m in cluster) for issue in issues for cluster in issue["clusters"])


@pytest.mark.asyncio
async def test_axis_hygiene_does_not_fold_other_scripts_together(db):
    await _project_rows(db, ["研究", "開発", "仕事2025", "趣味2025", "projA-日本", "projA-中国", "---", "___"])
    assert await checks.check_axis_hygiene(db, AGENT, fix=False) == []


@pytest.mark.asyncio
async def test_axis_hygiene_still_reports_drift_in_any_script(db):
    await _project_rows(
        db,
        ["data-ops-audit", "dataops_audit", "研究-2025", "研究 2025", "ＡＢＣ", "abc", "Straße", "STRASSE", "研究"],
    )
    issues = await checks.check_axis_hygiene(db, AGENT, fix=False)
    assert _clusters(issues) == [
        ["STRASSE", "Straße"],
        ["abc", "ＡＢＣ"],
        ["data-ops-audit", "dataops_audit"],
        ["研究 2025", "研究-2025"],
    ]
