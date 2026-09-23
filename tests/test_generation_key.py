"""The key a derived vector is stored under, and what counts as current.

Over HTTP the embedding request carries texts and the backend picks the model, so
the name never crossed the wire and this server wrote its own configured default
— a constant that does not move when the model does. `/capabilities` moves it.

These tests pin the three things that make the change safe to ship against a
corpus that already exists: a fingerprint, once learned, is what new rows carry;
rows written before this server could ask keep counting as current, so nothing is
re-embedded on a suspicion; and a backend that cannot identify itself leaves the
keys exactly where they were, because unknown is not unchanged.
"""

import pytest

from cpersona import blocks, config, generation, nodes, reconstruct
from cpersona._vendored_mcp_common.embedding_client import BackendIdentity, EmbedOutcome

FINGERPRINT = "1:53c2228b30556a83fd2f5ca50f0e56e510cbbdde226bd7eaf9664949b3d35b91"
OTHER = "1:b56bb13005a56d4144d81b5d14cbd29a10353337858117ae31e1898aaf8f1915"


class Backend:
    """A client that answers /capabilities with whatever it is handed."""

    def __init__(self, identity, error=None):
        self._identity = identity
        self._error = error
        self.asked = 0

    async def capabilities_with_outcome(self):
        self.asked += 1
        ok = self._identity is not None
        return self._identity, EmbedOutcome(attempted=True, ok=ok, error=self._error)


def _identity(fingerprint, incomplete=()):
    return BackendIdentity(
        fingerprint=fingerprint, fields={"model": "bge-m3"}, incomplete=tuple(incomplete)
    )


@pytest.fixture(autouse=True)
def forget():
    """Each test starts with nothing learned: a fingerprint kept from another test
    would make the fallback tests pass for the wrong reason."""
    generation.reset()
    yield
    generation.reset()


# --- what is written -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_learned_fingerprint_becomes_the_key_new_rows_carry():
    await generation.refresh(Backend(_identity(FINGERPRINT)))

    assert generation.fingerprint() == FINGERPRINT
    assert generation.node_keys()[0] == FINGERPRINT
    assert generation.block_keys()[0] == FINGERPRINT


@pytest.mark.asyncio
async def test_a_backend_that_cannot_identify_itself_leaves_the_keys_alone():
    """The state this server was in before it could ask, reached again on purpose."""
    await generation.refresh(Backend(None, error="mode=http / GET … failed"))

    assert generation.fingerprint() is None
    assert generation.node_keys() == (config.EMBEDDING_MODEL, config.EMBEDDING_MODEL)
    assert generation.block_keys() == (
        config.reported_embedding_model(),
        config.reported_embedding_model(),
    )


@pytest.mark.asyncio
async def test_an_incomplete_identity_is_not_a_fingerprint():
    """The client refuses to hand one over; this pins that nothing here invents one
    from the fields that did arrive."""
    await generation.refresh(Backend(_identity(None, incomplete=("digests.graph",))))

    assert generation.fingerprint() is None
    assert generation.node_keys()[0] == config.EMBEDDING_MODEL


@pytest.mark.asyncio
async def test_a_client_without_the_call_is_unknown_not_an_error():
    """An embedding client older than the capability report — the upgrade order
    this will actually meet in the field."""

    class Older:
        pass

    assert await generation.refresh(Older()) is None
    assert generation.node_keys()[0] == config.EMBEDDING_MODEL


@pytest.mark.asyncio
async def test_no_client_is_unknown():
    assert await generation.refresh(None) is None
    assert generation.fingerprint() is None


# --- what counts as current ------------------------------------------------------


@pytest.mark.asyncio
async def test_the_legacy_key_stays_current_beside_the_fingerprint():
    """The whole migration. Rows written before this server could ask carry no
    evidence about which model made them, so calling them stale would re-embed the
    corpus on a suspicion rather than on a finding."""
    await generation.refresh(Backend(_identity(FINGERPRINT)))

    write, legacy = generation.block_keys()
    assert write == FINGERPRINT
    assert legacy == config.reported_embedding_model()
    assert write != legacy


@pytest.mark.asyncio
async def test_the_legacy_key_follows_the_configuration_not_a_constant():
    """An operator who renames the model still invalidates the rows that named the
    old one — the fallback did not become a wildcard."""
    await generation.refresh(Backend(_identity(FINGERPRINT)))
    before = generation.node_keys()[1]

    assert before == config.EMBEDDING_MODEL
    assert generation.keys("some-other-model") == (FINGERPRINT, "some-other-model")


@pytest.mark.asyncio
async def test_a_third_key_is_not_accepted():
    """Two keys, not "anything goes": a row from a backend this server never ran
    is not current."""
    await generation.refresh(Backend(_identity(FINGERPRINT)))

    assert OTHER not in generation.block_keys()
    assert OTHER not in generation.node_keys()


# --- asking again ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_backend_is_asked_once_within_the_interval():
    backend = Backend(_identity(FINGERPRINT))
    await generation.refresh(backend)
    await generation.refresh(backend)
    await generation.refresh(backend)

    assert backend.asked == 1


@pytest.mark.asyncio
async def test_a_redeployed_backend_is_picked_up_after_the_interval(monkeypatch):
    """The answer is not kept for the life of the process: a backend redeployed
    under the same URL is the state this exists to detect."""
    await generation.refresh(Backend(_identity(FINGERPRINT)))
    monkeypatch.setattr(generation, "REFRESH_INTERVAL_SECONDS", 0)
    backend = Backend(_identity(OTHER))
    await generation.refresh(backend)

    assert backend.asked == 1
    assert generation.fingerprint() == OTHER


# --- the readers agree with the writers ------------------------------------------


def _config_reads(module) -> set[str]:
    """Names this module reads off ``config`` in executable code.

    Parsed rather than grepped: the same names appear in prose explaining why they
    are no longer read, and a test that matched its own subject's documentation
    would go red on an edit that changed nothing.
    """
    import ast
    import inspect

    found = set()
    for node in ast.walk(ast.parse(inspect.getsource(module))):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "config":
                found.add(node.attr)
    return found


@pytest.mark.parametrize("module", [nodes, blocks, reconstruct])
def test_no_reader_derives_a_generation_key_from_configuration(module):
    """The builder, the sweep, the retrieval arm and the quotation path each decide
    "is this set current". Two answers to that question would have the builder
    writing sets the sweep keeps rebuilding, so they all ask one module — and none
    of them reaches past it to the configured name, which cannot see a backend that
    changed."""
    reads = _config_reads(module)

    assert "EMBEDDING_MODEL" not in reads, (
        f"{module.__name__} reads the configured model name directly, so a backend "
        "swap would leave its rows reading as current"
    )
    assert "reported_embedding_model" not in reads, (
        f"{module.__name__} derives a generation key from configuration directly"
    )


def test_the_detector_can_see_a_reader_that_reaches_past_the_module():
    """The check above passes trivially if the parse finds nothing, so this pins
    that it finds a real read when one is there."""
    assert "EMBEDDING_MODEL" in _config_reads(generation)


@pytest.mark.asyncio
async def test_the_stored_key_is_one_of_the_two_accepted_ones():
    """A row this server writes now must read as current on the next pass — the
    invariant that keeps the sweep from rebuilding what it just built."""
    await generation.refresh(Backend(_identity(FINGERPRINT)))

    for pair in (generation.node_keys(), generation.block_keys()):
        assert pair[0] in pair


# --- the predicate, against a real table -----------------------------------------
#
# The unit tests above pin what `keys()` returns. They cannot pin what the SQL does
# with it, and the SQL is where the migration either holds or re-embeds the corpus.


class _TempDB:
    async def __aenter__(self):
        import os
        import tempfile

        from cpersona import database, session, tasks

        session.reset_pauses_for_tests()
        self._dir = tempfile.mkdtemp()
        self._saved = (database._db, database.DB_PATH, tasks._task_queue)
        database._db = None
        database.DB_PATH = os.path.join(self._dir, "generation.db")
        self.queue = tasks.MemoryTaskQueue()
        self.queue._running = True
        tasks._task_queue = self.queue
        await database.get_db()
        return self

    async def __aexit__(self, *exc):
        from cpersona import database, session, tasks

        await database.close_db()
        database._db, database.DB_PATH, tasks._task_queue = self._saved
        session.reset_pauses_for_tests()


async def _row_written_under(db, key: str, parent_id: int, text_len: int) -> None:
    """One complete, single-block set for a record, stored under ``key``.

    Complete includes the block's re-rank vector: a set without it is not
    current whatever its key, and these tests are about the key alone.
    """
    await db.execute(
        "INSERT INTO record_blocks (parent_kind, parent_id, block_index, agent_id, "
        "project_id, channel, start_char, end_char, forced_boundary, embedding_bits, "
        "embedding_model) VALUES ('mem', ?, 0, 'a', '', '', 0, ?, 0, X'00', ?)",
        (parent_id, text_len, key),
    )
    await db.execute(
        "INSERT INTO record_block_vectors (parent_kind, parent_id, block_index, embedding_i8) "
        "VALUES ('mem', ?, 0, X'7F00000000000000')",
        (parent_id,),
    )
    await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored, still_current",
    [
        (FINGERPRINT, True),  # written since the backend could be identified
        ("", True),  # written before it could — the corpus that already exists
        (OTHER, False),  # written by a backend this server is not running
        ("text-embedding-3-small", False),  # a named model this deployment never set
    ],
)
async def test_which_stored_keys_read_as_current(stored, still_current):
    async with _TempDB():
        from cpersona import database

        db = await database.get_db()
        await generation.refresh(Backend(_identity(FINGERPRINT)))
        await _row_written_under(db, stored, parent_id=1, text_len=12)

        assert (
            await blocks._blocks_current(db, "mem", 1, 12, generation.block_keys())
        ) is still_current


@pytest.mark.asyncio
async def test_before_a_fingerprint_arrives_the_predicate_is_what_it_was():
    """A deployment that cannot ask must behave exactly as it did, including
    refusing a row that names a model it did not configure."""
    async with _TempDB():
        from cpersona import database

        db = await database.get_db()
        await generation.refresh(Backend(None))
        await _row_written_under(db, config.reported_embedding_model(), 1, 12)
        await _row_written_under(db, FINGERPRINT, 2, 12)

        keys = generation.block_keys()
        assert await blocks._blocks_current(db, "mem", 1, 12, keys) is True
        assert await blocks._blocks_current(db, "mem", 2, 12, keys) is False


# --- the paths that run with a fingerprint present -------------------------------
#
# The block and node suites never learn one, so both keys are equal there and a
# writer that stores the wrong one is invisible. These reach the real paths with
# the two keys distinct, which is the state production is in after an upgrade.


LONG = "\n\n".join(f"paragraph {i} about budget headcount procurement" for i in range(6))


@pytest.fixture
def building(monkeypatch, fake_embedding_client):
    monkeypatch.setattr(config, "BLOCK_BUILD_ENABLED", True)
    return fake_embedding_client


async def _store_and_drain(tmp, text, agent="agent.gen"):
    from cpersona import admin_handlers, memory_handlers, nodes as nodes_mod

    row = await memory_handlers.do_store(agent, {"content": text})
    await tmp.queue._drain(admin_handlers, memory_handlers, nodes_mod)
    return row["id"]


@pytest.mark.asyncio
async def test_a_built_block_set_carries_the_fingerprint_not_the_legacy_key(building):
    async with _TempDB() as tmp:
        from cpersona import database

        await generation.refresh(Backend(_identity(FINGERPRINT)))
        row_id = await _store_and_drain(tmp, LONG)

        db = await database.get_db()
        stored = await db.execute_fetchall(
            "SELECT DISTINCT embedding_model FROM record_blocks WHERE parent_id = ?", (row_id,)
        )
        assert stored, "no blocks were built, so this proves nothing about their key"
        assert [r[0] for r in stored] == [FINGERPRINT]


@pytest.mark.asyncio
async def test_a_built_node_set_carries_the_fingerprint_not_the_legacy_key(building):
    # Nodes are only built for a record the backend reports as past its window, so
    # the fake has to report one; without it nothing is queued and the assertion
    # below would pass on an empty table.
    building.token_window = 16
    async with _TempDB() as tmp:
        from cpersona import database

        await generation.refresh(Backend(_identity(FINGERPRINT)))
        row_id = await _store_and_drain(tmp, LONG * 40)

        db = await database.get_db()
        stored = await db.execute_fetchall(
            "SELECT DISTINCT embedding_model FROM record_nodes WHERE parent_id = ?", (row_id,)
        )
        assert stored, "no nodes were built, so this proves nothing about their key"
        assert [r[0] for r in stored] == [FINGERPRINT]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored, still_current", [(FINGERPRINT, True), ("", True), (OTHER, False)]
)
async def test_the_node_predicate_accepts_both_keys_and_no_third(stored, still_current):
    async with _TempDB():
        from cpersona import database, nodes as nodes_mod

        db = await database.get_db()
        await generation.refresh(Backend(_identity(FINGERPRINT)))
        # nodes' legacy key is the resolved model name, which is "" only when unset;
        # store under whichever this deployment would have written.
        key = generation.node_keys()[1] if stored == "" else stored
        await db.execute(
            "INSERT INTO record_nodes (parent_kind, parent_id, node_index, start_char, "
            "end_char, token_count, window, embedding, embedding_model) "
            "VALUES ('mem', 1, 0, 0, 12, 3, 512, X'00', ?)",
            (key,),
        )
        await db.commit()

        assert (
            await nodes_mod._nodes_current(db, "mem", 1, 12, generation.node_keys())
        ) is still_current


@pytest.mark.asyncio
async def test_the_retrieval_arm_skips_rows_from_a_generation_this_server_never_ran():
    async with _TempDB():
        from cpersona import database
        from cpersona.isolation import isolation_where

        db = await database.get_db()
        await generation.refresh(Backend(_identity(FINGERPRINT)))
        await _row_written_under(db, FINGERPRINT, parent_id=1, text_len=12)
        await _row_written_under(db, OTHER, parent_id=2, text_len=12)

        rows = await blocks._examined(db, isolation_where(agent_id="a"), generation.block_keys())

        assert rows, "nothing was examined, so the filter proves nothing"
        assert {r[1] for r in rows} == {1}, "a foreign generation's rows were examined"


@pytest.mark.asyncio
async def test_the_quotation_path_refuses_blocks_from_a_generation_it_did_not_run():
    """A complete, gapless set is still not quotable when another model measured the
    offsets: the spans would point into text this server never divided that way."""
    async with _TempDB():
        from cpersona import database, memory_handlers

        db = await database.get_db()
        await generation.refresh(Backend(_identity(FINGERPRINT)))
        row = await memory_handlers.do_store("a", {"content": "twelve chars"})
        await db.execute("DELETE FROM record_blocks WHERE parent_id = ?", (row["id"],))
        await _row_written_under(db, OTHER, parent_id=row["id"], text_len=12)

        candidate = reconstruct._Candidate.__new__(reconstruct._Candidate)
        object.__setattr__(candidate, "kind", "mem")
        object.__setattr__(candidate, "row_id", row["id"])
        found = await reconstruct._current_block_sets("a", [candidate])

        assert found == {}, "blocks from another generation were offered for quotation"
