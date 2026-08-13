"""Second pass over the 2.5.3 tests: mutants that survived, pins that could not fail.

Every item here was found by applying a mutation and watching the suite stay
green, so each test below is paired with the specific mutation it kills. A test
added without that check is a guess about what would have failed.

Out of scope by the time this file was written: the `Middleware(BearerTokenMiddleware,
...)` mounting mutant (closed by tests/test_253_middleware_wiring.py) and the
tautological `mapped + unmapped + locked == count` assertion (closed by bug-205,
which counts `locked` instead of deriving it by subtraction).
"""

import json
import logging

import pytest
import pytest_asyncio

from cpersona import admin_handlers, checks, config, server, vector
from cpersona._vendored_mcp_common import mcp_utils, no_persist
from cpersona.database import get_db
from cpersona.utils import SCORING_VERSION

AGENT = "teeth-agent"


@pytest_asyncio.fixture
async def db():
    no_persist.resume()
    conn = await get_db()
    for table in ("memories", "episodes", "profiles"):
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()
    return conn


async def _insert(conn, source: str, content: str):
    await conn.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp) VALUES (?, ?, ?, ?)",
        (AGENT, content, source, "2026-08-13T00:00:00+00:00"),
    )
    await conn.commit()


# ---------------------------------------------------------------------------
# Mutant: drop `json_type(source) = 'object'` from the anonymous exclusion in
# invalid_source_type_where. Not an equivalent mutation — json_each('[]') is 0
# rows and json_type('[]') is 'array', so the mutant silently stops reporting a
# JSON container that is not an object. It survived because no fixture had one.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["[]", "[1,2]"])
async def test_a_non_object_json_container_is_still_a_type_defect(db, source):
    """`[]` is not the anonymous shape. `{}` is.

    The exclusion bug-187 added is for the documented "producer unknown" write,
    which the store path normalises to an empty OBJECT. An array carries no
    producer either, but nothing supported produces one, so it is a corrupt
    value rather than a blessed absence — and it is exactly what the mutant
    makes disappear.
    """
    await _insert(db, source, f"array source {source}")

    found = await checks.check_invalid_source_type(db, AGENT, fix=False)

    assert len(found) == 1, f"{source} vanished from the check"
    assert found[0]["count"] == 1


@pytest.mark.asyncio
async def test_the_empty_object_is_still_excluded(db):
    """The paired direction, so the test above cannot be satisfied by deleting
    the exclusion it is meant to keep honest."""
    await _insert(db, "{}", "anonymous")

    assert await checks.check_invalid_source_type(db, AGENT, fix=False) == []


# ---------------------------------------------------------------------------
# Mutant: make install_mgp_validation_filter() unreachable in main() (wrap it in
# a never-true guard). The existing pin reads main()'s source for the substring
# and compares index positions, so the call being DEAD does not disturb it —
# the docstring names "a guard nobody invokes" as the hazard, and could not tell
# that hazard from its absence.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_main_actually_calls_the_filter_installer(monkeypatch):
    """Behavioural, not textual: drive main() and watch the call happen.

    An unknown transport makes main() raise after startup and before serving,
    which is the window this needs — the installer runs on the startup path, so
    a call that is present in the source but unreachable fails here and passes
    every source-reading assertion.
    """
    called = []
    monkeypatch.setenv("CPERSONA_TRANSPORT", "definitely-not-a-transport")
    monkeypatch.setattr(server, "install_mgp_validation_filter", lambda: called.append(True))

    async def spy_init_db():
        pass

    async def spy_close_db():
        pass

    monkeypatch.setattr(server, "init_db", spy_init_db)
    monkeypatch.setattr(server, "close_db", spy_close_db)

    with pytest.raises(ValueError, match="Unknown transport"):
        await server.main()

    assert called == [True], "the installer is on the startup path but was never reached"


# ---------------------------------------------------------------------------
# Mutant: delete `root.addFilter(log_filter)` from install_mgp_validation_filter.
# The docstring justifies keeping the root-LOGGER filter alongside the handler
# filters ("still catches anything logged directly on root"), and nothing
# checked the sentence. Records that reach a handler are covered by the handler
# filters, so only a logger with no handlers of its own — logging directly on
# root before basicConfig, or a handler added after install — distinguishes them.
# ---------------------------------------------------------------------------


def test_the_root_logger_filter_is_load_bearing():
    """Attach nothing but the root logger's own filter chain and check it drops."""
    mcp_utils._MGP_FILTER_INSTALLED = False
    root = logging.getLogger()
    saved_filters = list(root.filters)
    saved_handlers = list(root.handlers)
    root.filters.clear()
    # No handlers: a handler-only installation would have nothing to attach to,
    # so what survives the mutation is exactly the root-logger filter.
    root.handlers.clear()
    try:
        mcp_utils.install_mgp_validation_filter()
        record = logging.LogRecord(
            name="root",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg="Failed to validate request: 1 validation error for ClientRequest",
            args=(),
            exc_info=None,
        )
        assert root.filter(record) is False, (
            "a record logged directly on root passed the filter chain — the "
            "root.addFilter call the docstring justifies is not there"
        )
    finally:
        root.filters[:] = saved_filters
        root.handlers[:] = saved_handlers
        mcp_utils._MGP_FILTER_INSTALLED = False


# ---------------------------------------------------------------------------
# Not a mutant — a leak, and it is checked in conftest rather than here.
# install_mgp_validation_filter attaches to the root logger AND to every handler
# on it; test_253_followups cleared only the logger side, so the filter stayed on
# pytest's capture handlers and silently dropped later "Failed to validate
# request:" assertions. A test in this file could only fail if it happened to run
# after the leak, and it would name the victim; the autouse fixture
# _no_leaked_mgp_log_filter runs after every test and names the culprit.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Mutant class: rewrite _purge_agent_calibration to keep the deleted agent (by
# inlining the carry, or renaming the helper it must not use). The existing pin
# only asserts the string "_stored_agent_maps_to_carry" is absent from the
# source, so any spelling of the same mistake passes it.
# ---------------------------------------------------------------------------


def _seed_sidecar(path: str) -> None:
    with open(path, "w") as fh:
        json.dump(
            {
                "embedding_dim": 1024,
                "embedding_model": "bge-m3",
                "scoring_version": SCORING_VERSION,
                "global_threshold": 0.55,
                "agent_thresholds": {"alice": 0.61, "bob": 0.58},
                "global_fused_gate": None,
                "agent_fused_gates": {"alice": 0.40, "bob": 0.33},
                "fused_gate_signal": "confidence",
                "agent_betas": {"alice": 1.5},
                "calibrated_at": "2026-07-01T00:00:00+00:00",
            },
            fh,
        )


def test_a_purged_agent_is_gone_from_the_sidecar_file(tmp_path, monkeypatch):
    """The behaviour the source pin was standing in for: read the file back.

    bug-036 is that a stale threshold outlives the corpus it was measured on and
    a later same-id agent inherits it. Only the file answers that.
    """
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "cpersona.db"))
    sidecar = str(tmp_path / "cpersona.db") + ".calibration.json"
    saved_threshold = config.VECTOR_MIN_SIMILARITY
    for d in (vector._agent_thresholds, vector._agent_fused_gates, vector._agent_betas):
        d.clear()
    vector._reset_calibration_authority()
    _seed_sidecar(sidecar)
    try:
        removed = admin_handlers._purge_agent_calibration("alice")

        assert removed is True
        payload = json.load(open(sidecar))
        assert "alice" not in payload["agent_thresholds"], "the purged agent came back"
        assert "alice" not in payload["agent_fused_gates"]
        assert "alice" not in payload["agent_betas"]
        assert payload["agent_thresholds"]["bob"] == 0.58, "the other agent must survive"
    finally:
        for d in (vector._agent_thresholds, vector._agent_fused_gates, vector._agent_betas):
            d.clear()
        vector._reset_calibration_authority()
        config.VECTOR_MIN_SIMILARITY = saved_threshold
