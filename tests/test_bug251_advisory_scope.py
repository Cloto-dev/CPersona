"""bug-251: the degraded advisory's "tell the user" runbook is process-scoped.

``health._advisory_emitted`` is a module global, and the once-per-episode
downgrade keys on it. Under stdio that is exactly right — one process serves
one client session, so "already emitted" means "this session already saw it".
Under ``CPERSONA_TRANSPORT=streamable-http`` the same process serves every
connected client (``StreamableHTTPSessionManager(stateless=True)``), so the
first recall of an outage consumed the full runbook for everybody: every other
session got ``FAULT_RUNBOOK_SHORT``, which carries no ``**Notify the user:**``
imperative and reads as a follow-up to a message that session never received.
The user is told once per OUTAGE instead of once per session.

The first test here is the reproduction: two distinct ACL clients, one process,
one real HTTP transport, both recalling during the same outage. Before the fix
the second one came back with the short reminder.
"""

import os
import tempfile

os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "bug251.db"))
os.environ["CPERSONA_EMBEDDING_MODE"] = "none"

import pytest  # noqa: E402
from transport_harness import (  # noqa: E402
    post_tool_call,
    run_with_real_transport,
    tool_result,
    write_acl_config,
)

from cpersona import config, health  # noqa: E402
from cpersona.database import init_db  # noqa: E402

NOTIFY = "**Notify the user:**"


@pytest.fixture(autouse=True)
def _reset_health():
    """health is a process-level singleton; reset it around every test."""
    health._reset()
    yield
    health._reset()


def _fault(monkeypatch, *, shared: bool):
    """Latch the fault state the way two consecutive probe failures would."""
    monkeypatch.setenv("CPERSONA_TRANSPORT", "streamable-http" if shared else "stdio")
    # http keeps observe_config a no-op, so the probe-driven fault survives.
    monkeypatch.setattr(config, "EMBEDDING_MODE", "http")
    health.observe_failure("connection refused")
    health.observe_failure("connection refused")


# --- the reproduction: two client sessions, one process, one outage ---


@pytest.mark.asyncio
async def test_every_http_session_is_told_during_one_outage(tmp_path, monkeypatch):
    _fault(monkeypatch, shared=True)
    await init_db()
    acl_config = write_acl_config(
        tmp_path,
        [
            {"client_id": "claude-web", "token": "web-token", "grants": {"*": "read-write"}},
            {"client_id": "claude-code", "token": "code-token", "grants": {"*": "read-write"}},
        ],
    )

    async def drive(app):
        out = []
        for token in ("web-token", "code-token"):
            status, raw = await post_tool_call(
                app, token, "recall", {"agent_id": "alpha", "query": ""}
            )
            assert status == 200
            out.append(tool_result(raw))
        return out

    first, second = await run_with_real_transport(acl_config, drive)

    for who, result in (("first", first), ("second", second)):
        advisory = result.get("advisory")
        assert advisory is not None, f"{who} session got no advisory: {result}"
        assert advisory["severity"] == "fault"
        assert NOTIFY in advisory["runbook"], (
            f"the {who} session was not told to notify the user — it received "
            f"{advisory['runbook']!r}"
        )
        # The suppression state belongs to the process, not to this caller, and
        # the payload has to say so (no-persist discloses the same way).
        assert advisory["advisory_scope"] == "process"


# --- the rule itself, driven directly ---


def test_shared_transport_does_not_downgrade_a_fault(monkeypatch):
    _fault(monkeypatch, shared=True)
    first = health.maybe_advisory()
    second = health.maybe_advisory()
    assert first["runbook"] == second["runbook"]
    assert NOTIFY in second["runbook"]


def test_stdio_keeps_the_once_per_session_downgrade(monkeypatch):
    """The anti-nag rule is correct where the process IS the session — keep it."""
    _fault(monkeypatch, shared=False)
    first = health.maybe_advisory()
    second = health.maybe_advisory()
    assert len(second["runbook"]) < len(first["runbook"])
    assert NOTIFY not in second["runbook"]
    assert first["advisory_scope"] == "session"


def test_hint_still_downgrades_under_a_shared_transport(monkeypatch):
    """mode=none is permanent, so repeating a 700-char runbook forever is the
    worse failure. The exemption is scoped to ``fault`` on purpose; the payload
    still discloses that the suppression is not this session's."""
    monkeypatch.setenv("CPERSONA_TRANSPORT", "streamable-http")
    monkeypatch.setattr(config, "EMBEDDING_MODE", "none")
    health.observe_config()
    first = health.maybe_advisory()
    second = health.maybe_advisory()
    assert first["severity"] == "hint"
    assert len(second["runbook"]) < len(first["runbook"])
    assert second["advisory_scope"] == "process"


def test_recovery_still_rearms_under_a_shared_transport(monkeypatch):
    _fault(monkeypatch, shared=True)
    assert health.maybe_advisory() is not None
    health.observe_ok()
    assert health.maybe_advisory() is None
