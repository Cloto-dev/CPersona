"""check_missing_nodes: finding and building the nodes the write path did not.

docs/OVERFLOW_TREE_DESIGN.md §3 -- records written before the feature, writes made
with the queue disabled, builds that failed, and nodes from another model all get
their nodes here. The repair must leave every record and every recall unchanged
(invariants 1 and 2), and each node it writes must fit the window and partition its
parent (invariants 3 and 4).
"""

import os
import tempfile

import pytest

from cpersona import checks, config, database, maintenance_handlers, memory_handlers, session, tasks

AGENT = "agent.health"
WINDOW = 24


def _long(i: int, words: int = 60) -> str:
    return " ".join(f"record{i} word{j} about topic{j % 5}." for j in range(words))


class _TempDB:
    async def __aenter__(self):
        session.reset_pauses_for_tests()
        self._saved = (database._db, database.DB_PATH, tasks._task_queue)
        database._db = None
        database.DB_PATH = os.path.join(tempfile.mkdtemp(), "nodes_health.db")
        tasks._task_queue = None  # the write path queues nothing: this check is the only builder
        self.db = await database.get_db()
        return self

    async def __aexit__(self, *exc):
        await database.close_db()
        database._db, database.DB_PATH, tasks._task_queue = self._saved
        session.reset_pauses_for_tests()

    async def memory(self, content, *, agent=AGENT, locked=0):
        cur = await self.db.execute(
            "INSERT INTO memories (agent_id, content, timestamp, locked) VALUES (?, ?, ?, ?)",
            (agent, content, "2026-09-17T00:00:00+00:00", locked),
        )
        await self.db.commit()
        return cur.lastrowid

    async def episode(self, summary, *, agent=AGENT):
        cur = await self.db.execute("INSERT INTO episodes (agent_id, summary) VALUES (?, ?)", (agent, summary))
        await self.db.commit()
        return cur.lastrowid

    async def run(self, fix, agent=AGENT):
        issues, _ = await checks.run_health_checks(self.db, agent_id=agent, fix=fix, checks=["missing_nodes"])
        await self.db.commit()
        return issues

    async def nodes_of(self, kind, parent_id):
        return await self.db.execute_fetchall(
            "SELECT start_char, end_char, token_count, window, embedding_model FROM record_nodes "
            "WHERE parent_kind = ? AND parent_id = ? ORDER BY node_index",
            (kind, parent_id),
        )


@pytest.fixture
def windowed(fake_embedding_client):
    fake_embedding_client.token_window = WINDOW
    return fake_embedding_client


async def _assert_nodes_partition(tmp, kind, parent_id, text):
    rows = await tmp.nodes_of(kind, parent_id)
    assert len(rows) > 1
    assert rows[0][0] == 0 and rows[-1][1] == len(text)
    assert all(a[1] == b[0] for a, b in zip(rows, rows[1:]))
    assert all(r[2] <= r[3] == WINDOW for r in rows)
    assert {r[4] for r in rows} == {config.EMBEDDING_MODEL}


@pytest.mark.asyncio
async def test_records_past_the_window_are_found_and_built_and_the_count_goes_to_zero(windowed):
    async with _TempDB() as tmp:
        mem = await tmp.memory(_long(1))
        ep = await tmp.episode(_long(2))
        await tmp.memory("a short one that fits")
        before_rows = await tmp.db.execute_fetchall("SELECT * FROM memories ORDER BY id")
        before_eps = await tmp.db.execute_fetchall("SELECT * FROM episodes ORDER BY id")

        (found,) = await tmp.run(fix=False)
        assert (found["count"], found["memories"], found["episodes"], found["repairable"]) == (2, 1, 1, 2)
        assert found["severity"] == "info"
        assert "built" not in found

        (fixed,) = await tmp.run(fix=True)
        assert fixed["built"] == 2

        assert await tmp.run(fix=False) == []
        await _assert_nodes_partition(tmp, "mem", mem, _long(1))
        await _assert_nodes_partition(tmp, "ep", ep, _long(2))
        # invariant 2: the repair wrote nodes, not records
        assert await tmp.db.execute_fetchall("SELECT * FROM memories ORDER BY id") == before_rows
        assert await tmp.db.execute_fetchall("SELECT * FROM episodes ORDER BY id") == before_eps


@pytest.mark.asyncio
async def test_a_locked_record_is_repaired(windowed):
    async with _TempDB() as tmp:
        mem = await tmp.memory(_long(3), locked=1)
        (fixed,) = await tmp.run(fix=True)
        assert (fixed["repairable"], fixed["built"]) == (1, 1)
        await _assert_nodes_partition(tmp, "mem", mem, _long(3))
        assert (await tmp.db.execute_fetchall("SELECT locked FROM memories WHERE id = ?", (mem,))) == [(1,)]


@pytest.mark.asyncio
async def test_nodes_from_another_model_are_found_and_rebuilt(windowed, monkeypatch):
    async with _TempDB() as tmp:
        mem = await tmp.memory(_long(4))
        await tmp.run(fix=True)
        assert await tmp.run(fix=False) == []

        monkeypatch.setattr(config, "EMBEDDING_MODEL", "the-next-model")
        (found,) = await tmp.run(fix=False)
        assert found["count"] == 1
        await tmp.run(fix=True)
        assert {r[4] for r in await tmp.nodes_of("mem", mem)} == {"the-next-model"}


@pytest.mark.asyncio
async def test_an_incomplete_node_set_is_found(windowed):
    async with _TempDB() as tmp:
        mem = await tmp.memory(_long(5))
        await tmp.run(fix=True)
        await tmp.db.execute(
            "DELETE FROM record_nodes WHERE parent_id = ? AND node_index = "
            "(SELECT MAX(node_index) FROM record_nodes WHERE parent_id = ?)",
            (mem, mem),
        )
        await tmp.db.commit()
        (found,) = await tmp.run(fix=False)
        assert found["count"] == 1


@pytest.mark.asyncio
async def test_one_run_builds_at_most_the_cap_and_the_rest_converge(windowed, monkeypatch):
    monkeypatch.setattr(checks, "NODE_REPAIR_RECORD_CAP", 2)
    async with _TempDB() as tmp:
        for i in range(5):
            await tmp.memory(_long(10 + i))
        (first,) = await tmp.run(fix=True)
        assert (first["count"], first["repairable"], first["built"]) == (5, 2, 2)
        (second,) = await tmp.run(fix=False)
        assert (second["count"], second["repairable"]) == (3, 2)


@pytest.mark.asyncio
async def test_another_agents_records_are_out_of_scope(windowed):
    async with _TempDB() as tmp:
        await tmp.memory(_long(6), agent="someone-else")
        assert await tmp.run(fix=True) == []
        assert await tmp.db.execute_fetchall("SELECT COUNT(*) FROM record_nodes") == [(0,)]


@pytest.mark.asyncio
async def test_no_embedding_client_reports_nothing():
    async with _TempDB() as tmp:
        await tmp.memory(_long(7))
        assert await tmp.run(fix=False) == []


@pytest.mark.asyncio
async def test_a_transport_without_a_token_report_reports_nothing(fake_embedding_client):
    assert fake_embedding_client.token_window is None
    async with _TempDB() as tmp:
        await tmp.memory(_long(8))
        assert await tmp.run(fix=False) == []


@pytest.mark.asyncio
async def test_an_http_backend_that_does_not_report_is_said_so(fake_embedding_client, monkeypatch):
    monkeypatch.setattr(fake_embedding_client, "mode", "http", raising=False)
    async with _TempDB() as tmp:
        await tmp.memory(_long(9))
        (issue,) = await tmp.run(fix=False)
        assert issue["count"] is None and issue["repairable"] == 0
        assert issue["severity"] == "info" and issue["needs_human_review"] is True
        assert "token" in issue["hint"]


@pytest.mark.asyncio
async def test_a_record_changed_after_the_prefetch_is_not_given_stale_nodes(windowed):
    async with _TempDB() as tmp:
        mem = await tmp.memory(_long(20))
        cache = {"nodes": await checks.prefetch_missing_nodes(AGENT)}
        assert len(cache["nodes"]["prepared"]) == 1
        await tmp.db.execute("UPDATE memories SET content = ? WHERE id = ?", (_long(21), mem))
        await tmp.db.commit()

        issues, _ = await checks.run_health_checks(
            tmp.db, agent_id=AGENT, fix=True, checks=["missing_nodes"], embedding_cache=cache
        )
        await tmp.db.commit()

        assert issues[0]["built"] == 0
        assert await tmp.nodes_of("mem", mem) == []


@pytest.mark.asyncio
async def test_check_health_builds_outside_the_write_lock(windowed, monkeypatch):
    """bug-072's rule: the divisions and embeddings run before the lock is taken."""
    from cpersona import nodes

    held = []
    real_prepare = nodes.prepare_nodes

    async def spy(*args):
        held.append(database.write_lock().locked())
        return await real_prepare(*args)

    monkeypatch.setattr(nodes, "prepare_nodes", spy)
    async with _TempDB() as tmp:
        mem = await tmp.memory(_long(30))
        result = await maintenance_handlers.do_check_health(agent_id=AGENT, fix=True, checks=["missing_nodes"])

        assert held == [False]
        (issue,) = [i for i in result["issues"] if i["check"] == "missing_nodes"]
        assert issue["built"] == 1
        await _assert_nodes_partition(tmp, "mem", mem, _long(30))


@pytest.mark.asyncio
async def test_repairing_nodes_leaves_recall_unchanged(windowed):
    """Invariant 1 across the repair."""
    async with _TempDB() as tmp:
        for i in range(4):
            await memory_handlers.do_store(AGENT, {"content": _long(40 + i)})
        await memory_handlers.do_store(AGENT, {"content": "topic3 is also a short memory"})
        queries = ["topic3", "record41 word7", "word12 about"]

        def ranking(r):
            return [(m.get("ref") or m.get("id"), m.get("content")) for m in r["messages"]]

        before = [ranking(await memory_handlers.do_recall(AGENT, q, 10)) for q in queries]
        (fixed,) = await tmp.run(fix=True)
        assert fixed["built"] == 4
        after = [ranking(await memory_handlers.do_recall(AGENT, q, 10)) for q in queries]
        assert after == before and any(before)
