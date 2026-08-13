"""Two follow-ups closed in 2.5.3.

- CSC #136: the MGP validation log filter was never installed on this server's
  own main loop, so every kernel handshake probe logged a 31-line pydantic
  ValidationError for a method the MCP schema does not know.
- bug-188 residual: the profile write path was bounded, but nothing detected a
  profile that an earlier version had already stored oversized — and that row is
  injected into every recall response.
"""

import pytest
import pytest_asyncio

from cpersona import checks, maintenance_handlers, server
from cpersona._vendored_mcp_common import mcp_utils, no_persist
from cpersona.config import MAX_CONTENT_LENGTH
from cpersona.database import get_db

AGENT = "followup-agent"


@pytest_asyncio.fixture
async def db():
    no_persist.resume()
    conn = await get_db()
    for table in ("memories", "episodes", "profiles"):
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()
    return conn


async def _set_profile(conn, content: str, agent_id: str = AGENT):
    await conn.execute(
        """INSERT INTO profiles (agent_id, user_id, content, updated_at)
           VALUES (?, '', ?, datetime('now'))
           ON CONFLICT(agent_id, user_id) DO UPDATE SET content = excluded.content""",
        (agent_id, content),
    )
    await conn.commit()


# ---------------------------------------------------------------------------
# CSC #136 — the filter is installed on this server's own loop
# ---------------------------------------------------------------------------


def test_main_installs_the_mgp_validation_filter():
    """A custom main loop does not go through run_mcp_server, so it must call this.

    Asserted on the source because ordering is what this test is for; the
    behavioural half — that the call is REACHED — lives in
    tests/test_254_teeth.py, which drives main() with a spy.

    The indent anchor is load-bearing. A substring check passes for a call
    wrapped in a never-true guard, which is dead code that reads as installed —
    "a guard nobody invokes" is the hazard this docstring names, and the check
    could not tell it from the fix. `^    ` requires the call to sit at
    function-body level, unnested.
    """
    import inspect
    import re

    source = inspect.getsource(server.main)

    assert re.search(r"(?m)^    install_mgp_validation_filter\(\)$", source), (
        "the installer call is missing, or nested inside a conditional"
    )
    install_at = source.index("install_mgp_validation_filter()")
    transport_at = source.index("stdio_server()")
    assert install_at < transport_at, "the filter must be installed before serving"


def test_the_filter_actually_suppresses_a_cloto_probe_validation_error(caplog):
    """And the filter it installs does the job, on the real logger.

    Reset the module flag first: the filter is install-once, and another test
    (or an earlier import) may have installed it already, which would make this
    pass without proving anything.

    Teardown removes the filter from the HANDLERS too, and restores rather than
    clears the logger's own chain. The install attaches to both; clearing only
    the logger side left ``_MgpValidationFilter`` sitting on pytest's capture
    handlers for the rest of the session, where it silently dropped any later
    assertion about a "Failed to validate request:" line — a test leaking the
    exact failure class the code under test is about. Measured after this test
    ran: the filter was live on _LiveLoggingNullHandler, _FileHandler and
    LogCaptureHandler, and a probe from an unrelated logger came back invisible.
    tests/test_254_teeth.py asserts the clean state independently, so a
    reintroduced leak fails somewhere rather than nowhere.
    """
    import logging

    mcp_utils._MGP_FILTER_INSTALLED = False
    root = logging.getLogger()
    saved_filters = list(root.filters)
    root.filters.clear()
    mcp_utils.install_mgp_validation_filter()
    installed = [f for f in root.filters if type(f).__name__ == "_MgpValidationFilter"]

    try:
        with caplog.at_level("WARNING"):
            logging.getLogger("mcp.server.lowlevel.server").warning(
                "Failed to validate request: 1 validation error for ClientRequest\n"
                "  Input tag 'cloto/handshake' found using 'method' does not match any"
            )
        assert "cloto/handshake" not in caplog.text

        # The paired direction, stated at the altitude the filter actually works
        # at: it drops the "Failed to validate request:" class wholesale, so a
        # malformed request from a real client is silenced too — deliberate
        # upstream behaviour, and the reason the filter must not widen further.
        # What must still get through is everything else.
        with caplog.at_level("WARNING"):
            logging.getLogger("mcp.server.lowlevel.server").warning(
                "Embedding backend returned 500; falling back to lexical search"
            )
        assert "Embedding backend" in caplog.text
    finally:
        for log_filter in installed:
            root.removeFilter(log_filter)
            for handler in root.handlers:
                handler.removeFilter(log_filter)
        root.filters[:] = saved_filters
        mcp_utils._MGP_FILTER_INSTALLED = False


# ---------------------------------------------------------------------------
# bug-188 residual — an already-oversized profile is detected and repairable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oversized_profile_is_detected(db):
    await _set_profile(db, "x" * (MAX_CONTENT_LENGTH + 1_000))

    found = await checks.check_oversized_profile(db, AGENT, fix=False)

    assert len(found) == 1
    assert found[0]["type"] == "oversized_profile"
    assert found[0]["count"] == 1
    assert found[0]["max_len"] == MAX_CONTENT_LENGTH + 1_000


@pytest.mark.asyncio
async def test_profile_at_the_cap_is_not_reported(db):
    """Boundary: the cap is the allowed size, not the first rejected one."""
    await _set_profile(db, "x" * MAX_CONTENT_LENGTH)

    assert await checks.check_oversized_profile(db, AGENT, fix=False) == []


@pytest.mark.asyncio
async def test_fix_truncates_to_the_cap(db):
    await _set_profile(db, "y" * (MAX_CONTENT_LENGTH + 5_000))

    await checks.check_oversized_profile(db, AGENT, fix=True)

    rows = await db.execute_fetchall(
        "SELECT length(content) FROM profiles WHERE agent_id = ?", (AGENT,)
    )
    assert rows[0][0] == MAX_CONTENT_LENGTH
    assert await checks.check_oversized_profile(db, AGENT, fix=False) == []


@pytest.mark.asyncio
async def test_fix_leaves_another_agents_profile_alone(db):
    """The repair is scoped by the isolation filter, like every sibling fixer."""
    await _set_profile(db, "a" * (MAX_CONTENT_LENGTH + 100), agent_id=AGENT)
    await _set_profile(db, "b" * (MAX_CONTENT_LENGTH + 100), agent_id="other-agent")

    await checks.check_oversized_profile(db, AGENT, fix=True)

    rows = dict(
        await db.execute_fetchall("SELECT agent_id, length(content) FROM profiles")
    )
    assert rows[AGENT] == MAX_CONTENT_LENGTH
    assert rows["other-agent"] == MAX_CONTENT_LENGTH + 100


@pytest.mark.asyncio
async def test_check_is_registered_and_surfaces_in_health(db):
    """Registration is the part that makes it reachable — pin it end to end."""
    assert "oversized_profile" in checks.HEALTH_CHECK_NAMES

    await _set_profile(db, "z" * (MAX_CONTENT_LENGTH + 10))
    health = await maintenance_handlers.do_check_health(agent_id=AGENT, fix=False)

    assert "oversized_profile" in [issue["type"] for issue in health["issues"]]
    assert health["status"] == "degraded"


@pytest.mark.asyncio
async def test_ordinary_profile_keeps_health_clean(db):
    await _set_profile(db, "a normal profile")

    health = await maintenance_handlers.do_check_health(agent_id=AGENT, fix=False)

    assert "oversized_profile" not in [issue["type"] for issue in health["issues"]]
    assert health["status"] == "healthy", health["issues"]
