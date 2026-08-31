"""Declared session identity (``session_key``) — the seam, and the two states it keys.

Authority: ``docs/SESSION_IDENTITY_DESIGN.md``. The design's §10 lists what must be
proven; this module is that list. The two properties worth stating up front, because
most of the assertions below exist to hold one of them:

- A caller that declares nothing must behave exactly as it did before the parameter
  existed. Every "keyless" test here is a preservation test, not a feature test.
- A caller that declares a key gets its own bucket of *process-local* state only. No
  assertion in this file touches stored rows, because the key never reaches them.
"""

import pytest

from cpersona import config, findings, health, session


@pytest.fixture(autouse=True)
def _clean_advisory_state():
    health._reset()
    session.reset_pauses_for_tests()
    yield
    health._reset()
    session.reset_pauses_for_tests()


@pytest.fixture
def shared_transport(monkeypatch):
    """Make config.shared_transport() report the streamable-HTTP condition."""
    monkeypatch.setattr(config, "shared_transport", lambda: True)


@pytest.fixture
def stdio_transport(monkeypatch):
    monkeypatch.setattr(config, "shared_transport", lambda: False)


def _fault(evidence="probe refused"):
    """Latch a fault: the promotion threshold is two consecutive failures."""
    for _ in range(health.FAULT_PROMOTE_THRESHOLD):
        health.observe_failure(evidence)


def _is_full(advisory):
    """The full runbook is the only variant carrying the notify imperative."""
    return "**Notify the user:**" in advisory["runbook"]


# ---------------------------------------------------------------------------
# §3 — resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "declared,expected_key,expected_flag",
    [
        ("s-1", "s-1", True),
        ("  s-1  ", "s-1", True),  # stripped, and still declared
        (None, session.TRANSPORT_KEY, False),
        ("", session.TRANSPORT_KEY, False),
        ("   ", session.TRANSPORT_KEY, False),  # whitespace-only is not a bucket
        (17, session.TRANSPORT_KEY, False),  # a non-string cannot declare
    ],
)
def test_resolution_covers_declared_absent_empty_and_whitespace(declared, expected_key, expected_flag):
    assert session.resolve_session_key(declared) == (expected_key, expected_flag)


def test_whitespace_only_is_undeclared_not_its_own_bucket():
    """A client template rendering an absent value emits "  ", not a session.

    Without the strip() this lands in a bucket of its own — one shared by every
    client whose template misfires, which is worse than the keyless bucket
    because it *looks* declared and would claim advisory_scope: session.
    """
    key, declared = session.resolve_session_key("   ")
    assert (key, declared) == (session.TRANSPORT_KEY, False)


# ---------------------------------------------------------------------------
# §5 stage 1 — the advisory becomes per-session
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shared", [False, True])
def test_two_declared_sessions_are_each_told_once_in_one_outage(monkeypatch, shared):
    """bug-251's deferred half: every session in an outage gets the runbook.

    Parametrised over the transport on purpose. Under a shared transport this
    assertion passes even WITHOUT per-session keying, because the keyless fault
    exemption already refuses to downgrade — the observable is identical, so that
    arm documents the intent but proves nothing. The stdio arm is where the two
    regimes disagree: keyless there downgrades after the first call, so a second
    session receiving the full runbook is only possible if the suppression is
    really keyed on the session. Measured: dropping the declared branch kills the
    stdio arm and leaves the shared arm green.
    """
    monkeypatch.setattr(config, "shared_transport", lambda: shared)
    _fault()
    first = health.maybe_advisory("s-1", True)
    second = health.maybe_advisory("s-2", True)
    assert _is_full(first), "the first session was not told"
    assert _is_full(second), "the second session was not told — bug-251 all over again"


def test_the_same_declared_session_is_not_told_twice(shared_transport):
    _fault()
    assert _is_full(health.maybe_advisory("s-1", True))
    repeat = health.maybe_advisory("s-1", True)
    assert not _is_full(repeat), "a session that was already told got the full runbook again"
    assert "Reminder:" in repeat["runbook"]


def test_declared_callers_report_session_scope(shared_transport):
    _fault()
    assert health.maybe_advisory("s-1", True)["advisory_scope"] == "session"


def test_a_new_episode_re_arms_every_declared_session(shared_transport):
    """Recovery then a fresh fault is news again, even to a session already told."""
    _fault()
    assert _is_full(health.maybe_advisory("s-1", True))
    health.observe_ok()
    _fault()
    assert _is_full(health.maybe_advisory("s-1", True))


def test_the_suppression_map_is_bounded_and_evicts_the_oldest(shared_transport):
    """The key space is client-supplied, so the map must not grow without limit.

    Eviction forgets only that a session was told, so the evicted one is told
    again — a repeated notice, which is the safe direction.
    """
    _fault()
    health.maybe_advisory("oldest", True)
    for n in range(health.ADVISORY_SESSION_CAP):
        health.maybe_advisory(f"s-{n}", True)
    assert len(health._told_sessions) <= health.ADVISORY_SESSION_CAP
    assert _is_full(health.maybe_advisory("oldest", True)), "eviction should re-arm, not silence"


# ---------------------------------------------------------------------------
# §3 / §8 — the keyless path is preserved exactly
# ---------------------------------------------------------------------------


def test_keyless_on_a_shared_transport_keeps_the_fault_exemption(shared_transport):
    """Unchanged bug-251 behaviour: a fault never downgrades when nobody can be keyed on."""
    _fault()
    assert _is_full(health.maybe_advisory())
    assert _is_full(health.maybe_advisory()), "the keyless exemption was lost"
    assert health.maybe_advisory()["advisory_scope"] == "process"


def test_keyless_on_stdio_keeps_the_once_per_process_downgrade(stdio_transport):
    """Unchanged: one process is one session there, so the second call is a reminder."""
    _fault()
    assert _is_full(health.maybe_advisory())
    second = health.maybe_advisory()
    assert not _is_full(second)
    assert second["advisory_scope"] == "session", "stdio's process IS the session"


def test_a_declared_key_does_not_disturb_the_keyless_bucket(shared_transport):
    """The two regimes must not share suppression state in either direction."""
    _fault()
    health.maybe_advisory("s-1", True)
    assert _is_full(health.maybe_advisory()), "a declared caller consumed the keyless notice"


def test_hint_severity_keeps_its_downgrade_for_declared_callers(shared_transport):
    """mode=none is permanent; repeating its runbook forever is what the downgrade avoids."""
    config_mode = config.EMBEDDING_MODE
    try:
        config.EMBEDDING_MODE = "none"
        health.observe_config()
        assert _is_full(health.maybe_advisory("s-1", True))
        assert not _is_full(health.maybe_advisory("s-1", True))
    finally:
        config.EMBEDDING_MODE = config_mode


# ---------------------------------------------------------------------------
# §5 stage 2 — the pause is scoped, not merely disclosed
#
# Stage 1 recorded WHO armed a still-global pause and reported it back
# (pause_owner_known / paused_by_self). Stage 2 removed that surface rather than
# adding to it: a caller cannot learn about another session's pause because a
# caller is no longer affected by one, and a disclosure field about a blast
# radius that no longer exists would be a field about nothing. The two tests
# below are the stage-1 handler round-trips, rewritten to assert the behaviour
# that replaced them; the full per-session matrix (isolation, TTL, eviction,
# queue attribution) lives in test_257_session_key_stage2.py.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause_and_status_round_trip_through_the_handlers():
    """The handlers wire resolution → this session's bucket, and no other."""
    from cpersona import server

    paused = await server.do_pause_persistence(ttl_seconds=60, session_key="s-1")
    try:
        assert paused["scope"] == "session", "a declared pause is the declaring session's"
        assert paused["paused"] is True
        assert (await server.do_persistence_status(session_key="s-1"))["paused"] is True
        theirs = await server.do_persistence_status(session_key="s-2")
        keyless = await server.do_persistence_status()
        assert theirs["paused"] is False, "one session's pause reached another"
        assert keyless["paused"] is False, "a declared pause reached the keyless bucket"
        # The stage-1 disclosure surface is gone, not merely unset: a field that
        # answered "whose pause is this?" has no referent once the answer is
        # always "yours".
        for response in (paused, theirs, keyless):
            assert "pause_owner_known" not in response
            assert "paused_by_self" not in response
    finally:
        await server.do_resume_persistence(session_key="s-1")


@pytest.mark.asyncio
async def test_resume_clears_only_the_callers_own_pause():
    """Stage 1 let any caller clear the one global pause. There is no longer one."""
    from cpersona import server

    await server.do_pause_persistence(ttl_seconds=60, session_key="s-1")
    cleared = await server.do_resume_persistence(session_key="s-2")
    assert cleared["was_active"] is False, "s-2 had nothing to clear; it reported otherwise"
    assert (await server.do_persistence_status(session_key="s-1"))["paused"] is True, (
        "a foreign resume cleared this session's pause"
    )

    mine = await server.do_resume_persistence(session_key="s-1")
    assert mine["was_active"] is True
    assert (await server.do_persistence_status(session_key="s-1"))["paused"] is False


# ---------------------------------------------------------------------------
# Reserved-key collision at the findings seam (the second review finding)
# ---------------------------------------------------------------------------


def test_a_probe_emitting_a_relocation_target_is_refused_not_overwritten():
    for target in findings.RELOCATION_TARGETS:
        with pytest.raises(findings.ReservedKeyCollision):
            findings.as_finding({"type": "x", "check": "some_probe", target: "mine"})


def test_no_probe_in_the_registry_emits_a_relocation_target():
    """The inventory pin that makes the raise above safe to ship.

    The guard turns a future field-name clash into a loud failure of the whole
    pull. That is only acceptable because it cannot reach production: this test
    reads the probe sources for the keys they build, so the clash is caught
    here rather than by a caller. If this fails, rename the probe's field —
    do not widen RELOCATION_TARGETS.
    """
    import inspect

    from cpersona import checks

    source = inspect.getsource(checks)
    for target in findings.RELOCATION_TARGETS:
        assert f'"{target}"' not in source, f"a probe now emits {target!r}; rename its field"
