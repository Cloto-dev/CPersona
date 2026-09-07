"""The three surfaces that answered as if an install were working when it was not.

An operator indexed a corpus, got `stored` for every row, a `healthy` checkup and a
named embedding model, and had no semantic recall at all: the embedding client had
never been built. Each surface was individually defensible and together they formed
a complete account of a working system.

  bug-427  do_store answers `embedded: false` with no log line, which is also the
           honest answer under EMBEDDING_MODE=none
  bug-428  status: healthy scores what is in the database, and was read as scoring
           the pipeline that fills it
  bug-429  the NULL-embedding repair reports `repairable: 0`, and the generic hint
           for that value blames row locking
  bug-430  calibrate_threshold named the api default model on a transport that
           never sends a model name

The control in every test below is the deliberately-off configuration: under
EMBEDDING_MODE=none the same absent client is the configured state, and a fix that
cannot tell the two apart is a fix that trades one wrong answer for another.
"""
import logging

import pytest
import pytest_asyncio

from cpersona import admin_handlers, checks, config, memory_handlers, vector
from cpersona._vendored_mcp_common.embedding_client import EmbeddingClient
from cpersona.database import get_db


@pytest.fixture(autouse=True)
def _no_client(monkeypatch):
    """No embedding client, which is the state every test here is about."""
    monkeypatch.setattr(vector, "_embedding_client", None)


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


@pytest.fixture
def warnings(caplog):
    caplog.set_level(logging.WARNING, logger="cpersona.memory_handlers")
    return caplog


def _reset_store_warning(monkeypatch):
    monkeypatch.setattr(memory_handlers, "_warned_no_embedding_client", False)


# ---------------------------------------------------------------------------
# bug-427 — storing unembedded rows says so, once, and only when it is wrong
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_says_the_client_is_missing_when_the_mode_asked_for_one(
    db, monkeypatch, warnings
):
    monkeypatch.setattr(config, "EMBEDDING_MODE", "http")
    _reset_store_warning(monkeypatch)

    res = await memory_handlers.do_store(
        "b427", {"content": "a row with no vector", "source": {}, "timestamp": "t"}
    )

    assert res["result"] == "stored" and res["embedded"] is False, res
    said = [r for r in warnings.records if "no embedding client is installed" in r.message]
    assert said, (
        "the row was stored unembedded under a mode that asks for embeddings and "
        f"nothing said so: {[r.message for r in warnings.records]}"
    )
    assert "'http'" in said[0].getMessage(), "the warning must name the mode it read"


@pytest.mark.asyncio
async def test_store_stays_silent_when_embeddings_are_switched_off(db, monkeypatch, warnings):
    """The control. Without it, warning on every unembedded store would 'pass'.

    `embedded: false` under EMBEDDING_MODE=none is the correct answer for a
    correctly configured install, and a warning there is noise on every write.
    """
    monkeypatch.setattr(config, "EMBEDDING_MODE", "none")
    _reset_store_warning(monkeypatch)

    res = await memory_handlers.do_store(
        "b427", {"content": "deliberately unembedded", "source": {}, "timestamp": "t"}
    )

    assert res["result"] == "stored" and res["embedded"] is False, res
    assert not [
        r for r in warnings.records if "no embedding client is installed" in r.message
    ], "warned about a configuration that is behaving exactly as configured"


@pytest.mark.asyncio
async def test_the_missing_client_is_reported_once_not_once_per_row(db, monkeypatch, warnings):
    """The condition is a property of the process, so N rows must not make N lines.

    Per-row logging would bury the one line that matters under the import that
    provoked it — the very import whose 198 files this was meant to be visible from.
    """
    monkeypatch.setattr(config, "EMBEDDING_MODE", "http")
    _reset_store_warning(monkeypatch)

    for i in range(5):
        await memory_handlers.do_store(
            "b427", {"content": f"row {i}", "source": {}, "timestamp": "t"}
        )

    said = [r for r in warnings.records if "no embedding client is installed" in r.message]
    assert len(said) == 1, f"expected exactly one line for five rows, got {len(said)}"


# ---------------------------------------------------------------------------
# bug-429 — the unrepairable NULL embeddings say which thing is missing
# ---------------------------------------------------------------------------


async def _seed_null_rows(db, agent="b429", count=3):
    for i in range(count):
        await db.execute(
            "INSERT INTO memories (agent_id, content, timestamp) VALUES (?,?,?)",
            (agent, f"unembedded {i}", "2026-05-14T00:00:00Z"),
        )
    await db.commit()


@pytest.mark.asyncio
async def test_null_embedding_hint_names_the_missing_writer_not_the_rows(db, monkeypatch):
    monkeypatch.setattr(config, "EMBEDDING_MODE", "http")
    await _seed_null_rows(db)

    issue = (await checks.check_null_embedding(db, "b429", fix=False))[0]

    assert issue["repairable"] == 0, issue
    hint = issue["hint"]
    assert "no embedding client is installed" in hint, hint
    assert "'http'" in hint, "the hint must name the mode that was read"
    assert "locked" not in hint or "not locked" in hint, (
        "the generic repairable=0 hint blames row locking, which sends the operator "
        f"to the corpus instead of to the configuration: {hint}"
    )


@pytest.mark.asyncio
async def test_null_embedding_hint_calls_switched_off_a_resting_state(db, monkeypatch):
    """The control again: with embeddings off, NULL is not a backlog to explain away."""
    monkeypatch.setattr(config, "EMBEDDING_MODE", "none")
    await _seed_null_rows(db)

    issue = (await checks.check_null_embedding(db, "b429", fix=False))[0]

    assert issue["repairable"] == 0, issue
    assert "resting state" in issue["hint"], issue["hint"]
    assert "no embedding client is installed" not in issue["hint"], (
        "an install that is deliberately not embedding must not be described as broken"
    )


@pytest.mark.asyncio
async def test_the_episode_twin_carries_the_same_hint(db, monkeypatch):
    """Episodes take the same repair and used to take the same wrong explanation."""
    monkeypatch.setattr(config, "EMBEDDING_MODE", "http")
    await db.execute(
        "INSERT INTO episodes (agent_id, summary, keywords, created_at) VALUES (?,?,?,?)",
        ("b429", "an episode", "", "2026-05-14 00:00:00"),
    )
    await db.commit()

    issue = (await checks.check_null_episode_embedding(db, "b429", fix=False))[0]

    assert "no embedding client is installed" in issue["hint"], issue


# ---------------------------------------------------------------------------
# bug-430 — the reported model is one that was actually used
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode, configured, expected",
    [
        # api posts {"model": ..., "input": ...}: the default really is the model.
        ("api", False, "text-embedding-3-small"),
        # http posts {"texts": ...}: the name never leaves this process.
        ("http", False, ""),
        ("none", False, ""),
        # An operator who declared a name gets it back on every transport.
        ("http", True, "declared-model"),
        ("none", True, "declared-model"),
    ],
)
def test_reported_model_is_only_the_one_that_is_sent(monkeypatch, mode, configured, expected):
    monkeypatch.setattr(config, "EMBEDDING_MODE", mode)
    monkeypatch.setattr(config, "EMBEDDING_MODEL_CONFIGURED", configured)
    monkeypatch.setattr(
        config, "EMBEDDING_MODEL", "declared-model" if configured else "text-embedding-3-small"
    )

    assert config.reported_embedding_model() == expected


@pytest.mark.asyncio
async def test_calibrate_reports_the_honest_model_not_the_api_default(db, monkeypatch):
    """Pinned at the call site: the helper being right does not make it reached.

    admin_handlers reads config.EMBEDDING_MODEL in three places and only two of
    them are this one.
    """
    monkeypatch.setattr(config, "EMBEDDING_MODE", "http")
    monkeypatch.setattr(config, "EMBEDDING_MODEL_CONFIGURED", False)
    monkeypatch.setattr(config, "EMBEDDING_MODEL", "text-embedding-3-small")
    for i in range(15):
        vec = [float((i + j) % 5) - 2.0 for j in range(8)]
        await db.execute(
            "INSERT INTO memories (agent_id, content, timestamp, embedding) VALUES (?,?,?,?)",
            ("b430", f"memory {i}", "2026-05-14T00:00:00Z", EmbeddingClient.pack_embedding(vec)),
        )
    await db.commit()

    result = await admin_handlers.do_calibrate_threshold("b430")

    assert result["ok"] is True, result
    assert result["embedding_model"] == "", (
        "calibrate named a model that this transport never sends: "
        f"{result['embedding_model']!r}"
    )


def test_the_staleness_keys_still_carry_the_resolved_model(monkeypatch):
    """The asymmetry is deliberate, so pin it rather than leave it to be tidied up.

    The calibration sidecar and the vector-index fingerprint compare a stored model
    name against the current one to decide whether the stored numbers still describe
    the corpus. Feeding them the reported value would make every existing http-mode
    sidecar and index read as stale on upgrade, recalibrating thresholds and
    rebuilding indexes that are still valid. They keep the resolved name.
    """
    import inspect

    source = inspect.getsource(admin_handlers.do_calibrate_threshold)
    assert "_save_calibration_state(" in source
    sidecar_call = source.split("_save_calibration_state(", 1)[1]
    assert "config.EMBEDDING_MODEL," in sidecar_call.split(")", 1)[0], (
        "the sidecar's model argument was switched to the reported value; that "
        "invalidates every stored calibration whose transport does not send a model"
    )

    from cpersona import vector_index

    fingerprint = inspect.getsource(vector_index)
    assert '"embedding_model": config.EMBEDDING_MODEL' in fingerprint, (
        "the index fingerprint was switched to the reported value; existing indexes "
        "would rebuild on upgrade"
    )
