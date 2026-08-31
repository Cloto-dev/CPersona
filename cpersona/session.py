"""Declared session identity: the opaque partition hint (``session_key``).

Under stdio one process serves one client, so process-global state is
session-scoped by construction. Under streamable-HTTP it is not — the server
runs ``stateless=True``, one process answers every connected client, and no
session survives a request. Nothing in the transport carries a session
identity to the handler: an environment variable is captured when the process
spawns, a header value is expanded once at startup, and the transport session
id is not echoed back. Identity therefore travels as data, in the arguments of
the call.

This module is the one place that resolves it. A caller that declares nothing
lands in the transport bucket and behaves exactly as it did before this module
existed; a caller that declares a key gets its own bucket of the process-local
state that is keyed here (the degraded-recall advisory's "already told you"
memory, and the no-persist pause itself).

What this is NOT
----------------
**Not authentication.** The key is compared, never verified. Any caller can
send any string, including one belonging to another session or the literal
:data:`TRANSPORT_KEY`; a caller that does simply joins that bucket. Nothing
here is a security boundary — the ACL layer decides what a caller may do.

**Not an isolation axis.** ``agent_id`` / ``project_id`` / ``channel`` select
*whose data* a query reads. This selects *whose in-process state* a call
touches — which pause applies to a write, and whether an advisory has already
been delivered. Nothing about *which rows* a call can see. No stored row is ever filtered by it, and no memory becomes reachable
or unreachable because of it. Filtering recall by session would make memory
unreadable across the boundary it exists to cross.

Authority: ``docs/SESSION_IDENTITY_DESIGN.md``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cpersona._vendored_mcp_common import no_persist

# The bucket every caller that declares nothing shares. Under stdio that is
# already a session (the process is one); under the shared HTTP transport it
# is one bucket for every keyless caller — which is the state of the world
# before a key is declared, and so the behavior-preserving default.
#
# Deliberately NOT derived from the process (pid, parent lineage, start time):
# under the shared transport the process describes the server, not any of its
# callers, so a key derived from it would look like an answer while
# partitioning nothing.
TRANSPORT_KEY = "transport"


def resolve_session_key(declared: str | None) -> tuple[str, bool]:
    """Return ``(effective_key, declared)`` for one call's ``session_key``.

    A non-empty, non-whitespace string is the effective key and ``declared`` is
    True. Absent, empty, ``None`` and whitespace-only all fall through to
    :data:`TRANSPORT_KEY` with ``declared`` False — the behavior every existing
    caller already has.

    No length limit, no format validation, no sanitization beyond ``strip()``:
    the value is compared, never parsed, never interpolated into SQL, never
    recorded as an identity claim. ``strip()`` is what makes "  " undeclared
    rather than a bucket of its own, which matters because a client template
    that renders an absent value can easily emit whitespace.

    One string is not a key: :data:`TRANSPORT_KEY` itself. A caller may send it
    — the docstring above says any caller may send any string — and doing so
    lands in the bucket every keyless caller already shares. Returning
    ``declared=True`` for it would make every consumer of this flag describe
    that bucket as private: the pause would report ``scope: "session"`` while
    silencing every keyless caller, who could also clear it, and the advisory
    would key its suppression per-session on shared state. The bucket, not the
    caller's intent, decides — so the literal resolves as undeclared, which is
    the true statement about where the call landed.
    """
    if isinstance(declared, str):
        stripped = declared.strip()
        if stripped and stripped != TRANSPORT_KEY:
            return stripped, True
    return TRANSPORT_KEY, False


# --- The no-persist pause, per session ----------------------------------------
#
# Stage 2 of the design: the pause is keyed, so a session silences its own writes
# and nobody else's. Before this, one process-global flag in the vendored
# ``no_persist`` module silenced every connected client, and a caller could only
# be told who armed it (stage 1's disclosure).
#
# ONE implementation, not two. The vendored module still owns the response shape
# (``make_skipped_response``) and the TTL constants, but its ``pause`` / ``resume``
# / ``status`` / ``is_paused`` are no longer called by this package: routing the
# keyless bucket through the old global and declared keys through a new map would
# have been two implementations of one invariant, and the one nobody exercises is
# the one that drifts. ``test_no_second_pause_implementation`` fails if a call to
# the vendored switch comes back, and ``test_ttl_validation_matches_vendored``
# pins this module's argument handling against the vendored rules it replaced, so
# a re-vendor that changes them is caught rather than silently diverged from.
#
# The keyless bucket is TRANSPORT_KEY, so every caller that declares nothing keeps
# sharing exactly one pause — the behaviour that predates the parameter, preserved
# by construction rather than by a special case.

_MAX_PAUSED_SESSIONS = 256

# key -> deadline. Bounded for the same reason the advisory map is: the key space
# is client-supplied and a client that rotates keys must not grow it without
# limit. Eviction here is NOT as safe as it is there — forgetting a pause resumes
# writes for that session — so the cap is high enough that reaching it means a
# client is rotating keys per call, and eviction is nearest-deadline-first so the
# entry closest to expiring anyway is the one that goes.
_pauses: dict[str, datetime] = {}


def _now() -> datetime:
    return datetime.now(UTC)


def _decay(now: datetime | None = None) -> None:
    """Drop every pause whose TTL has elapsed (lazy; no background timer)."""
    if now is None:
        now = _now()
    for key in [k for k, deadline in _pauses.items() if now >= deadline]:
        del _pauses[key]


def _validate_ttl(ttl_seconds: int) -> int:
    """Validate and clamp a TTL, with the vendored module's exact rules.

    Pinned against that module by ``test_ttl_validation_matches_vendored`` — this
    is a copy of an invariant, and the test is what keeps the copy honest.
    """
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
        raise ValueError("ttl_seconds must be a positive integer")
    if ttl_seconds < 1:
        raise ValueError("ttl_seconds must be >= 1")
    return min(ttl_seconds, no_persist.MAX_TTL_SECONDS)


def _scope(declared: bool) -> str:
    """What a pause on this caller's bucket actually covers.

    ``session`` for a declared key. ``process`` for a keyless caller, which is
    still true and still the widest honest answer: every keyless caller on this
    process shares the one bucket, and under stdio that process is the session.
    """
    return "session" if declared else "process"


def is_paused_for(session_key: str) -> bool:
    """Return True iff this key's bucket is currently paused."""
    _decay()
    return session_key in _pauses


def pause_for(session_key: str, declared: bool, ttl_seconds: int) -> dict:
    """Arm this key's pause for ``ttl_seconds``. Last-write-wins, no stacking.

    Raises ``ValueError`` for a non-int or non-positive TTL; values above the
    ceiling are clamped.
    """
    ttl_seconds = _validate_ttl(ttl_seconds)
    now = _now()
    _decay(now)
    deadline = now + timedelta(seconds=ttl_seconds)
    if session_key not in _pauses and len(_pauses) >= _MAX_PAUSED_SESSIONS:
        del _pauses[min(_pauses, key=_pauses.__getitem__)]
    _pauses[session_key] = deadline
    return {
        "paused": True,
        "expires_at": deadline.isoformat(),
        "ttl_seconds": ttl_seconds,
        "scope": _scope(declared),
    }


def resume_for(session_key: str, declared: bool) -> dict:
    """Clear this key's pause immediately.

    ``was_active`` reports whether this key's bucket was paused before the call
    (after decay). A caller reaches the bucket its key names and no other — but
    "no other" is a statement about keys, not about people: the key is compared,
    never verified, so anyone who sends the same string clears the same pause and
    reads the same TTL. That is the partition, and it is the whole of it.
    """
    _decay()
    was_active = _pauses.pop(session_key, None) is not None
    return {"paused": False, "was_active": was_active, "scope": _scope(declared)}


def pause_status_for(session_key: str, declared: bool) -> dict:
    """This key's pause state, in tool-response shape."""
    # Evaluate the clock once: a second reading after decay could cross the
    # deadline and report the contradictory paused:true / ttl_remaining:0.
    now = _now()
    _decay(now)
    deadline = _pauses.get(session_key)
    if deadline is None:
        return {
            "paused": False,
            "expires_at": None,
            "ttl_remaining_seconds": None,
            "scope": _scope(declared),
        }
    return {
        "paused": True,
        "expires_at": deadline.isoformat(),
        "ttl_remaining_seconds": max(0, int((deadline - now).total_seconds())),
        "scope": _scope(declared),
    }


def ttl_label_for(session_key: str) -> str:
    """Human-readable TTL suffix for a skipped-write ``reason`` (e.g. ``28m left``)."""
    deadline = _pauses.get(session_key)
    if deadline is None:
        return "TTL unknown"
    remaining = (deadline - _now()).total_seconds()
    if remaining < 0:
        return "TTL expired"
    minutes = int(remaining // 60)
    return f"{minutes}m left" if minutes >= 1 else f"{int(remaining)}s left"


def reset_pauses_for_tests() -> None:
    """Clear every pause. Test-only; there is no runtime caller."""
    _pauses.clear()


def make_skipped_response(default_body: dict, tool_name: str, session_key: str) -> dict:
    """A skipped-write response whose TTL is this session's, not a process global.

    The vendored builder owns the shape — the ``"no-persist"`` id sentinel and the
    nulling of every action-specific id key (bug-104) are invariants worth exactly
    one implementation, so they are not copied here. What it cannot do is name the
    remaining TTL: it reads the module global this package no longer arms, and so
    always renders ``TTL unknown``. That one parenthetical is substituted.

    ``test_skipped_response_carries_this_sessions_ttl`` asserts the substitution
    actually fired. Without it a re-vendor that reworded the reason would leave
    every skipped write saying ``TTL unknown`` — true-ish, useless, and silent.
    """
    body = no_persist.make_skipped_response(default_body, tool_name)
    reason = body.get("reason")
    if isinstance(reason, str):
        body["reason"] = reason.replace("(TTL unknown)", f"({ttl_label_for(session_key)})", 1)
    return body
