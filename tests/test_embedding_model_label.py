"""Schema v18: the embedding_model label on memories and episodes, and what reads it.

A stored vector used to carry nothing about the model that produced it, so a swap to
another model of the same width was undetectable: every old vector kept the expected
length. Each vector is now written with a label -- the backend's fingerprint when it
reports one, else the name this process sends or was configured with, else '' -- in
the same statement as the vector, and cleared wherever the vector is.

Pinned here, in the order a row lives through them:

- every write path labels the vector it writes, and only a vector it writes;
- an existing database gains the column with '' (unknown) and nothing else moves;
- which labels count as current, and that nothing is judged without an identity;
- the ``embedding_model`` health check under each mode;
- ``reject`` keeps another model's vectors out of every reader that compares a stored
  record vector with the query, and ``warn`` changes nothing those readers return.
"""

import base64
import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
import pytest_asyncio

from cpersona import (
    admin_handlers,
    checks,
    coarse_search,
    config,
    cue,
    database,
    far_seats,
    generation,
    maintenance_handlers,
    memory_handlers,
    session,
    vector,
)
from cpersona._vendored_mcp_common.embedding_client import BackendIdentity, EmbedOutcome
from cpersona.database import get_db
from tests.conftest import FakeEmbeddingClient, fake_embed_one

FP = "1:" + "a" * 64
OTHER = "1:" + "b" * 64
AGENT = "label.agent"


class IdentifiedClient(FakeEmbeddingClient):
    """The conftest double, plus the capability report a CEmbedding 0.9 backend gives."""

    mode = "http"

    def __init__(self, fingerprint: str | None = FP):
        self.fingerprint = fingerprint

    async def capabilities_with_outcome(self):
        if self.fingerprint is None:
            return None, EmbedOutcome(attempted=True, ok=False, error="no capability report")
        identity = BackendIdentity(fingerprint=self.fingerprint, fields={"model": "m"}, incomplete=())
        return identity, EmbedOutcome(attempted=True, ok=True)


class FailingClient(IdentifiedClient):
    """Names itself, then fails to embed: the vector is absent, so its label must be."""

    async def embed(self, texts):
        return []


def _install(monkeypatch, client):
    monkeypatch.setattr(vector, "_embedding_client", client)
    generation.reset()
    return client


@pytest_asyncio.fixture
async def clean_db():
    session.reset_pauses_for_tests()
    db = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()
    return db


async def _label(db, table: str, row_id: int):
    rows = await db.execute_fetchall(f"SELECT embedding IS NOT NULL, embedding_model FROM {table} WHERE id = ?", (row_id,))
    return (bool(rows[0][0]), rows[0][1])


async def _store(content: str, agent: str = AGENT) -> int:
    res = await memory_handlers.do_store(agent, {"content": content, "source": {"type": "User"}})
    assert res["result"] == "stored", res
    return res["id"]


# --- what is written ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_labels_the_vector_with_the_fingerprint_the_backend_reports(clean_db, monkeypatch):
    _install(monkeypatch, IdentifiedClient(FP))
    mem = await _store("harbor lighthouse")
    assert await _label(clean_db, "memories", mem) == (True, FP)


@pytest.mark.asyncio
async def test_without_a_report_the_label_is_the_declared_name_or_nothing(clean_db, monkeypatch):
    _install(monkeypatch, IdentifiedClient(None))
    monkeypatch.setattr(config, "EMBEDDING_MODEL_CONFIGURED", False)
    unnamed = await _store("first row")
    assert await _label(clean_db, "memories", unnamed) == (True, "")

    monkeypatch.setattr(config, "EMBEDDING_MODEL_CONFIGURED", True)
    monkeypatch.setattr(config, "EMBEDDING_MODEL", "declared-model")
    named = await _store("second row")
    assert await _label(clean_db, "memories", named) == (True, "declared-model")


@pytest.mark.asyncio
async def test_a_failed_embed_leaves_neither_vector_nor_label(clean_db, monkeypatch):
    """The label is learned before the embed; it must not outlive the embed failing."""
    _install(monkeypatch, FailingClient(FP))
    mem = await _store("nothing embeds this")
    assert await _label(clean_db, "memories", mem) == (False, "")


@pytest.mark.asyncio
async def test_an_episode_is_labelled(clean_db, monkeypatch):
    _install(monkeypatch, IdentifiedClient(FP))
    res = await memory_handlers.do_archive_episode(AGENT, [], summary="a summarised session", keywords="k")
    assert await _label(clean_db, "episodes", res["episode_id"]) == (True, FP)


@pytest.mark.asyncio
async def test_update_memory_relabels_the_new_vector_and_clears_a_lost_one(clean_db, monkeypatch):
    _install(monkeypatch, IdentifiedClient(FP))
    mem = await _store("the old wording")

    _install(monkeypatch, IdentifiedClient(OTHER))
    assert (await admin_handlers.do_update_memory(mem, "the new wording", agent_id=AGENT))["ok"]
    assert await _label(clean_db, "memories", mem) == (True, OTHER)

    _install(monkeypatch, FailingClient(OTHER))
    assert (await admin_handlers.do_update_memory(mem, "a third wording", agent_id=AGENT))["ok"]
    assert await _label(clean_db, "memories", mem) == (False, "")


# --- export, import, merge ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_label_travels_with_the_vector_through_export_and_import(clean_db, monkeypatch, tmp_path):
    _install(monkeypatch, IdentifiedClient(FP))
    await _store("travelling memory")
    ep = await memory_handlers.do_archive_episode(AGENT, [], summary="travelling episode", keywords="k")
    assert ep["ok"]

    with_vectors = tmp_path / "with.jsonl"
    assert (await admin_handlers.do_export_memories(AGENT, str(with_vectors), include_embeddings=True))["ok"]
    records = [json.loads(line) for line in with_vectors.read_text().splitlines()]
    labelled = {r["_type"]: r.get("embedding_model") for r in records if r.get("_type") in ("memory", "episode")}
    assert labelled == {"memory": FP, "episode": FP}

    without = tmp_path / "without.jsonl"
    assert (await admin_handlers.do_export_memories(AGENT, str(without)))["ok"]
    assert all("embedding_model" not in json.loads(line) for line in without.read_text().splitlines())

    for table in ("memories", "episodes"):
        await clean_db.execute(f"DELETE FROM {table}")
    await clean_db.commit()
    assert (await admin_handlers.do_import_memories(str(with_vectors)))["ok"]
    restored = await clean_db.execute_fetchall(
        "SELECT embedding IS NOT NULL, embedding_model FROM memories UNION ALL "
        "SELECT embedding IS NOT NULL, embedding_model FROM episodes"
    )
    assert sorted(restored) == [(1, FP), (1, FP)]


@pytest.mark.parametrize(
    "extra, expected",
    [
        ({}, ""),  # a file written before labels existed
        ({"embedding_model": 42}, ""),  # hand-edited into something that is not a label
        ({"embedding_model": OTHER}, OTHER),
    ],
)
@pytest.mark.asyncio
async def test_an_imported_label_is_kept_only_beside_a_restored_vector(clean_db, tmp_path, extra, expected):
    blob = base64.b64encode(np.asarray(fake_embed_one("x"), dtype=np.float32).tobytes()).decode()
    lines = [
        {"_type": "header", "format": "cpersona-export", "version": 1},
        {"_type": "memory", "content": "with a vector", "timestamp": "t", "embedding_b64": blob, **extra},
        {"_type": "memory", "content": "without one", "timestamp": "t", "embedding_model": OTHER},
    ]
    path = tmp_path / "hand.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    res = await admin_handlers.do_import_memories(str(path), target_agent_id=AGENT)
    assert res["ok"], res
    rows = dict(
        await clean_db.execute_fetchall("SELECT content, embedding_model FROM memories WHERE agent_id = ?", (AGENT,))
    )
    assert rows == {"with a vector": expected, "without one": ""}


@pytest.mark.asyncio
async def test_merge_carries_the_label(clean_db, monkeypatch):
    _install(monkeypatch, IdentifiedClient(OTHER))
    await _store("merged memory", agent="merge.src")
    assert (await memory_handlers.do_archive_episode("merge.src", [], summary="merged episode"))["ok"]
    res = await admin_handlers.do_merge_memories("merge.src", "merge.dst")
    assert res["ok"], res
    labels = await clean_db.execute_fetchall(
        "SELECT embedding_model FROM memories WHERE agent_id = 'merge.dst' UNION ALL "
        "SELECT embedding_model FROM episodes WHERE agent_id = 'merge.dst'"
    )
    assert labels == [(OTHER,), (OTHER,)]


# --- repair paths -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nulling_a_vector_clears_its_label_and_the_re_embed_stamps_the_current_one(clean_db, monkeypatch):
    _install(monkeypatch, IdentifiedClient(OTHER))
    mem = await _store("a vector of the wrong width")
    # Another width under the same row: the dimension repair NULLs it.
    await clean_db.execute(
        "UPDATE memories SET embedding = ? WHERE id = ?", (np.zeros(8, dtype=np.float32).tobytes(), mem)
    )
    await clean_db.commit()

    _install(monkeypatch, IdentifiedClient(FP))
    res = await maintenance_handlers.do_check_health(
        agent_id=AGENT, fix=True, checks=["embedding_dimension", "null_embedding"]
    )
    assert "error" not in res, res
    assert await _label(clean_db, "memories", mem) == (True, FP)


@pytest.mark.asyncio
async def test_a_dimension_repair_alone_leaves_no_stale_label(clean_db, monkeypatch):
    _install(monkeypatch, IdentifiedClient(OTHER))
    mem = await _store("another wrong width")
    await clean_db.execute(
        "UPDATE memories SET embedding = ? WHERE id = ?", (np.zeros(8, dtype=np.float32).tobytes(), mem)
    )
    await clean_db.commit()
    await checks.check_embedding_dimension(clean_db, AGENT, fix=True, embedding_cache={"expected_dim": 64})
    await clean_db.commit()
    assert await _label(clean_db, "memories", mem) == (False, "")


@pytest.mark.asyncio
async def test_a_content_rewrite_clears_the_label_with_the_vector(clean_db, monkeypatch):
    _install(monkeypatch, IdentifiedClient(OTHER))
    mem = await _store("plain text")
    await checks._rewrite_or_delete_on_collision(clean_db, mem, "rewritten text")
    await clean_db.commit()
    assert await _label(clean_db, "memories", mem) == (False, "")


# --- the migration ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_v17_database_gains_the_column_with_unknown_labels(tmp_path):
    saved_db, saved_path = database._db, database.DB_PATH
    database._db = None
    database.DB_PATH = str(tmp_path / "v17.db")
    try:
        db = await database.get_db()
        for table in ("memories", "episodes"):
            await db.execute(f"ALTER TABLE {table} DROP COLUMN embedding_model")
        await db.execute("DELETE FROM schema_version")
        await db.execute("INSERT INTO schema_version (version) VALUES (17)")
        await db.execute(
            "INSERT INTO memories (agent_id, content, timestamp, embedding) VALUES ('old', 'kept', 't', x'00000000')"
        )
        await db.execute("INSERT INTO episodes (agent_id, summary, keywords) VALUES ('old', 'kept', 'k')")
        await db.commit()
        before = await db.execute_fetchall("SELECT id, content, embedding FROM memories")

        await database.close_db()
        database._db = None
        db = await database.get_db()

        assert (await db.execute_fetchall("SELECT MAX(version) FROM schema_version")) == [(database.SCHEMA_VERSION,)]
        assert database.SCHEMA_VERSION == 18
        assert await db.execute_fetchall("SELECT id, content, embedding FROM memories") == before
        assert await db.execute_fetchall("SELECT embedding_model FROM memories") == [("",)]
        assert await db.execute_fetchall("SELECT embedding_model FROM episodes") == [("",)]
    finally:
        await database.close_db()
        database._db, database.DB_PATH = saved_db, saved_path


# --- what counts as current ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_which_labels_are_current(monkeypatch):
    monkeypatch.setattr(config, "EMBEDDING_MODE", "http")
    monkeypatch.setattr(config, "EMBEDDING_MODEL_CONFIGURED", False)
    generation.reset()
    assert generation.accepted_labels() is None, "no identity: nothing is judged"

    await generation.refresh(IdentifiedClient(FP))
    assert generation.accepted_labels() == ("", FP)

    monkeypatch.setattr(config, "EMBEDDING_MODEL_CONFIGURED", True)
    monkeypatch.setattr(config, "EMBEDDING_MODEL", "declared-model")
    assert generation.accepted_labels() == ("", FP, "declared-model"), "a row labelled before the report stays current"

    generation.reset()
    assert generation.accepted_labels() is None, "a declared name over http is not an identity"

    monkeypatch.setattr(config, "EMBEDDING_MODE", "api")
    assert generation.current_identity() == "declared-model"
    assert generation.accepted_labels() == ("", "declared-model")


# --- the health check ---------------------------------------------------------------------


async def _seed_labels(db, agent=AGENT):
    blob = np.asarray(fake_embed_one("v"), dtype=np.float32).tobytes()
    rows = [
        ("memories", OTHER, blob),
        ("memories", OTHER, blob),
        ("episodes", OTHER, blob),
        ("memories", "older-name", blob),
        ("memories", FP, blob),
        ("memories", "", blob),
        ("memories", OTHER, None),  # a label beside no vector says nothing
    ]
    for i, (table, label, vec) in enumerate(rows):
        if table == "memories":
            await db.execute(
                "INSERT INTO memories (agent_id, content, timestamp, embedding, embedding_model) VALUES (?, ?, 't', ?, ?)",
                (agent, f"row {i}", vec, label),
            )
        else:
            await db.execute(
                "INSERT INTO episodes (agent_id, summary, embedding, embedding_model) VALUES (?, ?, ?, ?)",
                (agent, f"row {i}", vec, label),
            )
    await db.commit()


@pytest.mark.asyncio
async def test_the_check_counts_vectors_another_model_wrote(clean_db, monkeypatch):
    monkeypatch.setattr(config, "EMBEDDING_MODEL_CONFIGURED", False)
    await generation.refresh(IdentifiedClient(FP))
    await _seed_labels(clean_db)
    await _seed_labels(clean_db, agent="someone.else")

    [finding] = await checks.check_embedding_model(clean_db, AGENT, fix=False)
    assert finding["type"] == "embedding_model_mismatch"
    assert (finding["count"], finding["memories"], finding["episodes"]) == (4, 3, 1)
    assert finding["labels"] == [{"label": OTHER, "count": 3}, {"label": "older-name", "count": 1}]
    assert finding["current"] == FP
    assert finding["mode"] == "warn"
    assert "still compared" in finding["recall"]


@pytest.mark.asyncio
async def test_the_check_names_what_reject_does(clean_db, monkeypatch):
    monkeypatch.setattr(config, "EMBEDDING_MODEL_MODE", "reject")
    await generation.refresh(IdentifiedClient(FP))
    await _seed_labels(clean_db)
    [finding] = await checks.check_embedding_model(clean_db, AGENT, fix=False)
    assert finding["mode"] == "reject"
    assert "not compared" in finding["recall"]


@pytest.mark.asyncio
async def test_the_check_is_silent_under_off_and_without_an_identity(clean_db, monkeypatch):
    await _seed_labels(clean_db)
    monkeypatch.setattr(config, "EMBEDDING_MODE", "http")
    generation.reset()
    assert await checks.check_embedding_model(clean_db, AGENT, fix=False) == []

    await generation.refresh(IdentifiedClient(FP))
    monkeypatch.setattr(config, "EMBEDDING_MODEL_MODE", "off")
    assert await checks.check_embedding_model(clean_db, AGENT, fix=False) == []


@pytest.mark.asyncio
async def test_the_check_is_registered_as_a_report(clean_db, monkeypatch):
    registered = {c.name: c for c in checks.HEALTH_CHECKS}["embedding_model"]
    assert registered.base_severity == "info"
    assert not checks.is_fix_capable("embedding_model")

    # do_check_health learns the identity itself, before the run, so a server that has
    # not asked yet still judges on its first check_health.
    _install(monkeypatch, IdentifiedClient(FP))
    await _seed_labels(clean_db)
    res = await maintenance_handlers.do_check_health(agent_id=AGENT, checks=["embedding_model"])
    [issue] = res["issues"]
    assert issue["type"] == "embedding_model_mismatch" and issue["severity"] == "info"


def test_the_mode_falls_back_to_warn_on_an_unknown_value(monkeypatch):
    monkeypatch.setenv("CPERSONA_EMBEDDING_MODEL_MODE", "rejcet")
    assert config._parse_choice("CPERSONA_EMBEDDING_MODEL_MODE", "warn", ("warn", "reject", "off")) == "warn"
    assert config.EMBEDDING_MODEL_MODE in ("warn", "reject", "off")


# --- reject: every reader of a stored record vector ---------------------------------------


async def _corpus(db, monkeypatch, texts, agent=AGENT):
    """Store texts under FP, then relabel the first one as written by OTHER."""
    _install(monkeypatch, IdentifiedClient(FP))
    ids = [await _store(t, agent=agent) for t in texts]
    await db.execute("UPDATE memories SET embedding_model = ? WHERE id = ?", (OTHER, ids[0]))
    await db.commit()
    return ids


@pytest.mark.parametrize("mode", ["warn", "off"])
@pytest.mark.asyncio
async def test_the_vector_arm_is_unchanged_unless_reject(clean_db, monkeypatch, mode):
    ids = await _corpus(clean_db, monkeypatch, ["harbor lighthouse keeper", "harbor lighthouse", "harbor"])
    monkeypatch.setattr(config, "EMBEDDING_MODEL_MODE", mode)
    rows = await vector._search_vector(clean_db, AGENT, "harbor lighthouse keeper", 10, min_similarity=-1.0)
    assert [r["id"] for r in rows][:1] == [ids[0]]
    assert {r["id"] for r in rows} == set(ids)


@pytest.mark.asyncio
async def test_reject_drops_another_models_vector_and_nothing_else(clean_db, monkeypatch):
    ids = await _corpus(clean_db, monkeypatch, ["harbor lighthouse keeper", "harbor lighthouse", "harbor"])
    warn = await vector._search_vector(clean_db, AGENT, "harbor lighthouse keeper", 10, min_similarity=-1.0)
    monkeypatch.setattr(config, "EMBEDDING_MODEL_MODE", "reject")
    rejected = await vector._search_vector(clean_db, AGENT, "harbor lighthouse keeper", 10, min_similarity=-1.0)
    assert rejected == [r for r in warn if r["id"] != ids[0]]
    assert len(rejected) == len(ids) - 1


@pytest.mark.asyncio
async def test_reject_judges_nothing_without_an_identity(clean_db, monkeypatch):
    ids = await _corpus(clean_db, monkeypatch, ["harbor lighthouse keeper", "harbor"])
    _install(monkeypatch, IdentifiedClient(None))
    monkeypatch.setattr(config, "EMBEDDING_MODEL_MODE", "reject")
    rows = await vector._search_vector(clean_db, AGENT, "harbor lighthouse keeper", 10, min_similarity=-1.0)
    assert {r["id"] for r in rows} == set(ids)


@pytest.mark.asyncio
async def test_reject_covers_the_remote_answer(clean_db, monkeypatch):
    ids = await _corpus(clean_db, monkeypatch, ["remote one", "remote two"])
    answer = [{"id": i, "_rid": ("mem", i), "_cosine": 0.9, "content": "c"} for i in ids]

    async def remote(*a, **kw):
        return list(answer)

    monkeypatch.setattr(vector, "_search_vector_remote", remote)
    monkeypatch.setattr(config, "EMBEDDING_MODEL_MODE", "reject")
    rows = await vector._search_vector(clean_db, AGENT, "q", 10)
    assert [r["id"] for r in rows] == ids[1:]


@pytest.mark.asyncio
async def test_reject_covers_the_far_list(clean_db, monkeypatch):
    ids = await _corpus(clean_db, monkeypatch, ["far one", "far two", "near"])

    async def far(db, **kw):
        return [{"id": i, "_rid": ("mem", i), "_cosine": 0.5, "content": "c"} for i in ids[:2]]

    monkeypatch.setattr(vector, "_search_vector_far", far)
    monkeypatch.setattr(vector, "far_list_enabled", lambda: True)
    monkeypatch.setattr(config, "EMBEDDING_MODEL_MODE", "reject")
    far_out: list = []
    await vector._search_vector(clean_db, AGENT, "near", 1, min_similarity=-1.0, far_out=far_out)
    assert [r["id"] for r in far_out] == [ids[1]]


@pytest.mark.asyncio
async def test_reject_leaves_the_cosine_backfill_empty(clean_db, monkeypatch):
    ids = await _corpus(clean_db, monkeypatch, ["lexical hit one", "lexical hit two"])
    monkeypatch.setattr(config, "EMBEDDING_MODEL_MODE", "reject")
    results = [{"id": i, "_rid": ("mem", i), "_cosine": None, "content": "c"} for i in ids]
    await memory_handlers._backfill_cosines(clean_db, results, "lexical hit", None, "")
    assert results[0]["_cosine"] is None
    assert results[1]["_cosine"] is not None


@pytest.mark.asyncio
async def test_reject_covers_the_far_seats(clean_db, monkeypatch):
    ids = await _corpus(clean_db, monkeypatch, ["seat one", "seat two"])

    async def candidates(db, query, **kw):
        return coarse_search.Candidates(ids=tuple(ids), distances=(0, 0), positions=(0, 1), source="live")

    monkeypatch.setattr(coarse_search, "coarse_candidates", candidates)
    query = fake_embed_one("seat one")
    kwargs = dict(agent_id=AGENT, project_id=None, channel="", source_id="", floor=-1.0)
    assert {h.id for h in await far_seats.ranked(clean_db, query, **kwargs)} == set(ids)
    monkeypatch.setattr(config, "EMBEDDING_MODEL_MODE", "reject")
    assert [h.id for h in await far_seats.ranked(clean_db, query, **kwargs)] == ids[1:]


@pytest.mark.asyncio
async def test_reject_covers_the_cue_arm(clean_db, monkeypatch):
    ids = await _corpus(clean_db, monkeypatch, ["cue period one", "cue period two"])
    _install(monkeypatch, IdentifiedClient(FP))
    ep = await memory_handlers.do_archive_episode(AGENT, [], summary="cue period episode")
    await clean_db.execute("UPDATE episodes SET embedding_model = ? WHERE id = ?", (OTHER, ep["episode_id"]))
    await clean_db.execute("UPDATE memories SET timestamp = ?", (datetime.now(timezone.utc).isoformat(),))
    await clean_db.execute("UPDATE episodes SET start_time = ?", (datetime.now(timezone.utc).isoformat(),))
    await clean_db.commit()

    async def nothing(*a, **kw):
        return []

    # The vector halves only: the keyword halves would find the rows by their words.
    monkeypatch.setattr(memory_handlers, "_search_memories_keyword", nothing)
    monkeypatch.setattr(memory_handlers, "_search_episodes_fts", nothing)
    now = datetime.now(timezone.utc)
    window = (now - timedelta(days=1), now + timedelta(days=1))

    async def arm():
        rows = await memory_handlers._search_cue_arm(
            clean_db, AGENT, "cue period", cue.DEPTH, window, channel="", project_id=None, source_id="",
            exclude_set=set(), query_vec=[fake_embed_one("cue period")],
        )
        return {r.get("_rid") for r in rows}

    assert {("mem", ids[0]), ("ep", ep["episode_id"])} <= await arm()
    monkeypatch.setattr(config, "EMBEDDING_MODEL_MODE", "reject")
    seen = await arm()
    assert ("mem", ids[0]) not in seen and ("ep", ep["episode_id"]) not in seen
    assert ("mem", ids[1]) in seen


@pytest.mark.asyncio
async def test_reject_covers_the_propagation_seat(clean_db, monkeypatch):
    """With every vector another model's, the seat has no anchor to measure from."""
    texts = [f"harbor lighthouse keeper logbook entry {i}" for i in range(12)]
    await _corpus(clean_db, monkeypatch, texts)
    await clean_db.execute("UPDATE memories SET embedding_model = ?", (OTHER,))
    await clean_db.commit()
    query = "harbor lighthouse keeper logbook"

    def seated(out):
        return [m for m in out["messages"] if m.get("match_reason", {}).get("signal") == "propagation"]

    # deep: a row with no vector vote carries one keyword vote, below the gate of a
    # corpus this small; halving the gate keeps the window, so the anchor exists.
    warn = await memory_handlers.do_recall(AGENT, query, limit=3, deep=True, propagation_seat=True)
    assert len(seated(warn)) == 1
    monkeypatch.setattr(config, "EMBEDDING_MODEL_MODE", "reject")
    rejected = await memory_handlers.do_recall(AGENT, query, limit=3, deep=True, propagation_seat=True)
    assert rejected["messages"], "the keyword arms still answer, so there is an anchor"
    assert seated(rejected) == []
