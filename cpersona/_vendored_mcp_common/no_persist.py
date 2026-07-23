"""Session no-persist mode: per-process opt-in flag that turns write tools
into no-ops without disturbing reads.

Used by CPersona & CScheduler to let an interactive client (typically
Claude Code) declare "this session is ephemeral — do not write to my
memory or scope/goal/task store" before running benchmarks, AB tests, or
throwaway exploration. The flag is module-level state, scoped to the
MCP server process (which is NOT the same as a session — see Concurrency
& scope); no filesystem, no IPC, no cross-server sync. Each server keeps
its own flag — clients that want both paused MUST call ``pause_persistence``
on each.

Semantics
---------
- ``pause(ttl_seconds=1800)`` arms a wall-clock TTL. While armed,
  ``is_paused()`` returns ``True``; once the TTL elapses, ``is_paused()``
  returns ``False`` and the internal state is cleared lazily (no
  background timer).
- ``resume()`` clears the flag immediately.
- ``status()`` returns a dict suitable for direct return from an MCP
  tool handler.
- ``make_skipped_response(default_body, tool_name)`` standardises the
  shape of a write-tool no-op response: it preserves the caller's
  default body, replaces any ``id`` field with the string sentinel
  ``"no-persist"``, and adds ``persisted=False``, ``dry_run=True``,
  ``reason=...``. Callers should snapshot ``is_paused()`` once at the
  top of a handler and reuse the boolean for the duration of that call
  (avoids TTL-edge flips inside long bulk operations).

Concurrency & scope
-------------------
``_no_persist_until`` is a single Python attribute; reads and writes are
atomic under the GIL, so no ``asyncio.Lock`` is required.

The flag is PROCESS-GLOBAL, and a process is not the same as a session in
general. Under a one-process-per-client transport (stdio) the two coincide:
the flag is effectively session-scoped and an MCP server restart correctly
drops it. But under a multiplexed transport — the streamable-HTTP session
manager runs a single process serving every connected client — the one
global is SHARED across all sessions. One client's ``pause()`` then turns
writes into no-ops for every other session until ``resume()`` or the TTL
elapses, and those sessions receive no signal. There is deliberately no
per-session keying (bug-151); callers surface this blast radius via the ``scope``
field the tool handlers add to the pause/resume/status responses, rather than
this module pretending per-process implies per-session.

Versioning: introduced in cloto-mcp-cscheduler 0.2.6 / cloto-mcp-cpersona
2.4.19.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

__all__ = [
    "DEFAULT_TTL_SECONDS",
    "MAX_TTL_SECONDS",
    "is_paused",
    "make_skipped_response",
    "pause",
    "resume",
    "status",
]

DEFAULT_TTL_SECONDS = 1800  # 30 minutes
MAX_TTL_SECONDS = 86400  # 1 day — clamp upper bound

_no_persist_until: datetime | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _decay(now=None) -> None:
    """Lazily clear the flag if its TTL has elapsed.

    ``now`` lets status() evaluate the clock exactly once across the decay
    check and the remaining-TTL subtraction (bug-103)."""
    global _no_persist_until
    if _no_persist_until is not None and (now or _now()) >= _no_persist_until:
        _no_persist_until = None


def is_paused() -> bool:
    """Return True iff the no-persist flag is currently armed.

    Calls ``_decay()`` first so an expired flag is observed as not-paused
    and cleaned up in place.
    """
    _decay()
    return _no_persist_until is not None


def pause(ttl_seconds: int = DEFAULT_TTL_SECONDS) -> dict[str, Any]:
    """Arm the no-persist flag for ``ttl_seconds`` from now.

    Calling ``pause()`` while already paused replaces the existing TTL
    with the new one (last-write-wins; no stacking).

    Raises ``ValueError`` for non-int or non-positive ``ttl_seconds``.
    Values above ``MAX_TTL_SECONDS`` are clamped to that ceiling.
    """
    global _no_persist_until
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
        raise ValueError("ttl_seconds must be a positive integer")
    if ttl_seconds < 1:
        raise ValueError("ttl_seconds must be >= 1")
    ttl_seconds = min(ttl_seconds, MAX_TTL_SECONDS)
    _no_persist_until = _now() + timedelta(seconds=ttl_seconds)
    return {
        "paused": True,
        "expires_at": _no_persist_until.isoformat(),
        "ttl_seconds": ttl_seconds,
    }


def resume() -> dict[str, Any]:
    """Clear the no-persist flag immediately.

    Returns ``was_active=True`` if the flag was armed before this call
    (after lazy decay) and ``False`` if it was already inactive. The
    distinction lets the caller emit a no-op log line for redundant
    ``resume()`` calls.
    """
    global _no_persist_until
    _decay()
    was_active = _no_persist_until is not None
    _no_persist_until = None
    return {"paused": False, "was_active": was_active}


def status() -> dict[str, Any]:
    """Return the current pause state in tool-response shape."""
    # bug-103: evaluate the clock once — a second _now() after _decay() could
    # cross the TTL boundary and report the contradictory paused:true / ttl:0.
    now = _now()
    _decay(now)
    if _no_persist_until is None:
        return {
            "paused": False,
            "expires_at": None,
            "ttl_remaining_seconds": None,
        }
    remaining = (_no_persist_until - now).total_seconds()
    return {
        "paused": True,
        "expires_at": _no_persist_until.isoformat(),
        "ttl_remaining_seconds": max(0, int(remaining)),
    }


def _ttl_remaining_label() -> str:
    """Human-readable suffix for ``reason`` strings (e.g. ``"28m left"``)."""
    if _no_persist_until is None:
        return "TTL unknown"
    remaining = (_no_persist_until - _now()).total_seconds()
    if remaining < 0:
        return "TTL expired"
    minutes = int(remaining // 60)
    if minutes < 1:
        return f"{int(remaining)}s left"
    return f"{minutes}m left"


def make_skipped_response(
    default_body: dict[str, Any],
    tool_name: str,
) -> dict[str, Any]:
    """Standardise a write-tool no-op response.

    Starts from ``default_body`` (the shape the caller would normally
    return on success), replaces any ``id`` key with the string sentinel
    ``"no-persist"`` (truthy, unambiguous, won't collide with real IDs),
    and merges in:

    - ``persisted: False``
    - ``dry_run: True``
    - ``reason``: human-readable message including TTL remaining

    The caller should pass an already-shaped ``default_body`` so
    downstream consumers see consistent keys (e.g. ``ok``, ``id``,
    ``status`` etc.) regardless of the no-persist branch.

    Note: this function does *not* call ``is_paused()``. The caller is
    responsible for snapshotting that boolean at the top of the handler;
    this function trusts that the caller has already decided to skip.
    """
    body: dict[str, Any] = dict(default_body)
    if "id" in body:
        body["id"] = "no-persist"
    # bug-104: action-specific id keys must not echo the caller-supplied id —
    # a truthy deleted_id/updated_id read as success to consumers that branch
    # on the id field instead of the authoritative `persisted: false`.
    for key in ("deleted_id", "updated_id", "locked_id", "unlocked_id", "episode_id"):
        if key in body:
            body[key] = None
    body["persisted"] = False
    body["dry_run"] = True
    body["reason"] = (
        f"session no-persist mode active ({_ttl_remaining_label()}) — "
        f"`{tool_name}` skipped. Call `resume_persistence` to re-enable, "
        f"or wait for TTL to elapse."
    )
    return body
