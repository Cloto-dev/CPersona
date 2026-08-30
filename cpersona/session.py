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
memory, and the owner of a no-persist pause).

What this is NOT
----------------
**Not authentication.** The key is compared, never verified. Any caller can
send any string, including one belonging to another session or the literal
:data:`TRANSPORT_KEY`; a caller that does simply joins that bucket. Nothing
here is a security boundary — the ACL layer decides what a caller may do.

**Not an isolation axis.** ``agent_id`` / ``project_id`` / ``channel`` select
*whose data* a query reads. This selects *whose in-process state* a call
touches. No stored row is ever filtered by it, and no memory becomes reachable
or unreachable because of it. Filtering recall by session would make memory
unreadable across the boundary it exists to cross.

Authority: ``docs/SESSION_IDENTITY_DESIGN.md``.
"""

from __future__ import annotations

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
    """
    if isinstance(declared, str):
        stripped = declared.strip()
        if stripped:
            return stripped, True
    return TRANSPORT_KEY, False


# --- Owner of the process-wide no-persist pause -------------------------------
#
# The pause itself stays process-wide: it lives in the vendored ``no_persist``
# module, one flag for the process, and every write in every session is skipped
# while it is armed. That is not changed here and must not be described as if it
# were. What is recorded here is WHO armed it, so a caller that declares a key
# can be told whether the pause silencing its writes is its own or another
# session's — disclosure, not isolation.
#
# A caller that declares nothing is told nothing new: its responses keep the
# exact shape they had, and the pre-existing ``scope: "process"`` field already
# states the blast radius honestly.

_pause_owner: str | None = None
_pause_owner_declared: bool = False


def record_pause_owner(session_key: str, declared: bool) -> None:
    """Remember which key armed the pause (called on a successful pause)."""
    global _pause_owner, _pause_owner_declared
    _pause_owner = session_key
    _pause_owner_declared = declared


def clear_pause_owner() -> None:
    """Forget the owner (called on resume)."""
    global _pause_owner, _pause_owner_declared
    _pause_owner = None
    _pause_owner_declared = False


def pause_owner() -> tuple[str | None, bool]:
    """``(owner_key, owner_declared)`` for the pause currently recorded, if any."""
    return _pause_owner, _pause_owner_declared


def pause_ownership(session_key: str, declared: bool) -> dict:
    """Disclosure fields for a caller that declared a key, or ``{}`` for one that did not.

    ``paused_by_self`` is True / False when both sides declared a key, and ``None`` when
    the pause was armed by a caller that did not — the honest answer, because a keyless
    pause belongs to the shared bucket and cannot be attributed to one session. The
    absent-owner case (nothing armed, or an owner forgotten across a restart) returns the
    field as ``None`` too, with ``pause_owner_known`` False to separate the two.
    """
    if not declared:
        return {}
    owner, owner_declared = pause_owner()
    if owner is None:
        return {"pause_owner_known": False, "paused_by_self": None}
    if not owner_declared:
        return {"pause_owner_known": True, "paused_by_self": None}
    return {"pause_owner_known": True, "paused_by_self": owner == session_key}
