"""Per-client capability enforcement (ACL v1) — implements docs/ACL_DESIGN.md.

Two-stage default (design §4.1): with no active configuration this module
contributes zero decisions — the guard wrap is installed unconditionally but
passes straight through, so legacy behavior is byte-for-byte unchanged. With
``CPERSONA_ACL_FILE`` set, every call is resolved to a principal and checked
against the grant table, deny-by-default.

The identity seam (design §3.1): enforcement consumes only ``Principal``.
Nothing outside a resolver may assume identity came from a static token —
that constraint is what lets an OAuth resolver drop in later without touching
the grant table, the guard, or the tests below them.

Failure posture (design §7): configuration problems raise ``AclConfigError``
at load — the server refuses to start rather than serve a policy other than
the one written. At dispatch, a missing principal or an unclassified tool is
denied, never waved through.
"""

from __future__ import annotations

import contextvars
import hmac
import json
import logging
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass

from cpersona import config

logger = logging.getLogger(__name__)

# Permission lattice (design §3): none < read < read-write.
PERM_NONE = 0
PERM_READ = 1
PERM_WRITE = 2
_PERMISSION_LEVELS = {"none": PERM_NONE, "read": PERM_READ, "read-write": PERM_WRITE}
_PERMISSION_NAMES = {PERM_READ: "read", PERM_WRITE: "read-write"}

WILDCARD = "*"

# The stdio transport's reserved principal (design §5.4): no credential to
# resolve, the peer is whoever spawned the process. Grants for it come from
# the same file; if absent, stdio calls are denied like any ungranted client.
LOCAL_CLIENT_ID = "local"

# Whole-string environment reference for token values: "${VAR}".
_ENV_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")

_CLIENT_KEYS = {"client_id", "token", "grants"}


class AclConfigError(Exception):
    """Malformed ACL configuration — startup must fail, not degrade (§7)."""


@dataclass(frozen=True)
class Principal:
    """An authenticated client identity. The only thing enforcement consumes."""

    client_id: str


@dataclass(frozen=True)
class AclConfig:
    """Loaded, validated grant table.

    ``token_entries`` is a tuple of (token, client_id) pairs the resolver
    compares constant-time; ``grants_by_client`` maps client_id → its grant
    dict (agent pattern → permission level).
    """

    grants_by_client: dict[str, dict[str, int]]
    token_entries: tuple[tuple[str, str], ...]


def _resolve_token_value(raw: str, client_id: str) -> str:
    ref = _ENV_REF.match(raw)
    if not ref:
        if "${" in raw:
            # A partial reference ("pre${VAR}", "${VAR}x") would silently
            # become a literal, guessable credential in a file operators
            # commit more casually than a secret. Fail closed (§7).
            raise AclConfigError(
                f"ACL client {client_id!r}: token contains '${{' but is not a "
                "whole-string ${ENV_VAR} reference; use a literal without '${' "
                "or exactly \"${VAR}\""
            )
        return raw
    value = os.environ.get(ref.group(1), "")
    if not value:
        raise AclConfigError(
            f"ACL client {client_id!r}: token references ${{{ref.group(1)}}} but the "
            "environment variable is unset or empty"
        )
    return value


def load_config(path: str) -> AclConfig:
    """Parse and validate an ACL file. Any defect raises ``AclConfigError``."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except OSError as e:
        raise AclConfigError(f"ACL file {path!r} unreadable: {e}") from e
    except json.JSONDecodeError as e:
        raise AclConfigError(f"ACL file {path!r} is not valid JSON: {e}") from e

    try:
        mode = os.stat(path).st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            logger.warning(
                "ACL file %s is group/world-accessible (mode %o); it carries "
                "credentials and should be private to the service user",
                path,
                stat.S_IMODE(mode),
            )
    except OSError:  # pragma: no cover - stat raced with deletion; load already succeeded
        pass

    if not isinstance(raw, dict) or set(raw) != {"clients"}:
        raise AclConfigError(
            f"ACL file {path!r}: top level must be exactly {{\"clients\": [...]}}"
        )
    clients = raw["clients"]
    if not isinstance(clients, list) or not clients:
        raise AclConfigError(f"ACL file {path!r}: \"clients\" must be a non-empty list")

    grants_by_client: dict[str, dict[str, int]] = {}
    token_entries: list[tuple[str, str]] = []
    seen_tokens: set[str] = set()

    for i, entry in enumerate(clients):
        if not isinstance(entry, dict):
            raise AclConfigError(f"ACL clients[{i}]: must be an object")
        unknown = set(entry) - _CLIENT_KEYS
        if unknown:
            raise AclConfigError(f"ACL clients[{i}]: unknown keys {sorted(unknown)}")

        client_id = entry.get("client_id")
        if not isinstance(client_id, str) or not client_id:
            raise AclConfigError(f"ACL clients[{i}]: client_id must be a non-empty string")
        if client_id in grants_by_client:
            raise AclConfigError(f"ACL clients[{i}]: duplicate client_id {client_id!r}")

        grants_raw = entry.get("grants")
        if not isinstance(grants_raw, dict):
            raise AclConfigError(f"ACL client {client_id!r}: grants must be an object")
        grants: dict[str, int] = {}
        for agent, perm in grants_raw.items():
            if not isinstance(agent, str) or not agent:
                raise AclConfigError(f"ACL client {client_id!r}: empty grant key")
            if perm not in _PERMISSION_LEVELS:
                raise AclConfigError(
                    f"ACL client {client_id!r}: unknown permission {perm!r} for "
                    f"{agent!r} (expected one of {sorted(_PERMISSION_LEVELS)})"
                )
            grants[agent] = _PERMISSION_LEVELS[perm]

        if client_id == LOCAL_CLIENT_ID:
            # The stdio principal is asserted by transport, not by credential
            # (§5.4). A token here would be an entry nothing can ever present.
            if "token" in entry:
                raise AclConfigError(
                    f"ACL client {LOCAL_CLIENT_ID!r} is the stdio principal and must "
                    "not carry a token"
                )
        else:
            token_raw = entry.get("token")
            if not isinstance(token_raw, str) or not token_raw:
                raise AclConfigError(
                    f"ACL client {client_id!r}: token must be a non-empty string"
                )
            token = _resolve_token_value(token_raw, client_id)
            if token in seen_tokens:
                # Which client is a presented token? must have exactly one answer.
                raise AclConfigError(
                    f"ACL client {client_id!r}: token duplicates another client's"
                )
            seen_tokens.add(token)
            token_entries.append((token, client_id))

        grants_by_client[client_id] = grants

    return AclConfig(
        grants_by_client=grants_by_client, token_entries=tuple(token_entries)
    )


# ---------------------------------------------------------------------------
# Runtime state: active config (process) + current principal (request context)
# ---------------------------------------------------------------------------

_active_config: AclConfig | None = None

_current_principal: contextvars.ContextVar[Principal | None] = contextvars.ContextVar(
    "cpersona_acl_principal", default=None
)


def activate(config: AclConfig | None) -> None:
    """Install (or clear, with None) the process-wide ACL configuration."""
    global _active_config
    _active_config = config


def active_config() -> AclConfig | None:
    return _active_config


def is_active() -> bool:
    return _active_config is not None


def set_principal(principal: Principal | None) -> contextvars.Token:
    return _current_principal.set(principal)


def reset_principal(token: contextvars.Token) -> None:
    _current_principal.reset(token)


def current_principal() -> Principal | None:
    return _current_principal.get()


def resolve_token(acl_config: AclConfig, presented: str) -> Principal | None:
    """Token → principal, comparing against EVERY entry (no early exit).

    The loop always visits the whole table so response timing does not narrow
    which entry matched. Duplicate tokens are rejected at load, so at most one
    entry can match. Comparison is over UTF-8 bytes: ``hmac.compare_digest``
    raises on non-ASCII str input, and a header a remote caller controls must
    never turn a 401 into a 500 (bug-259).
    """
    if not presented:
        return None
    presented_bytes = presented.encode("utf-8")
    matched: str | None = None
    for token, client_id in acl_config.token_entries:
        if hmac.compare_digest(presented_bytes, token.encode("utf-8")):
            matched = client_id
    return Principal(matched) if matched is not None else None


def effective_permission(grants: dict[str, int], agent_pattern: str) -> int:
    """Design §3: exact match beats wildcard in BOTH directions (D6).

    A demand on the wildcard itself (``agent_pattern == "*"``) is a sweep —
    the call touches EVERY agent, the excepted ones included — so it is
    satisfied only at the level every grant row allows: the minimum over the
    wildcard grant and every named exception. Named grants alone never add up
    to ``*`` (no wildcard grant → PERM_NONE), and a client whose grants say
    ``{"*": "read-write", "prod": "none"}`` cannot reach prod through an
    all-agents call — the operator meant the exception (review finding on
    PR #112; the D6 ruling's intent applied to sweeps).
    """
    if agent_pattern == WILDCARD:
        if WILDCARD not in grants:
            return PERM_NONE
        return min(grants.values())
    if agent_pattern in grants:
        return grants[agent_pattern]
    if WILDCARD in grants:
        return grants[WILDCARD]
    return PERM_NONE


# ---------------------------------------------------------------------------
# Tool classification (design §6)
# ---------------------------------------------------------------------------
# Each entry maps a tool name to a demands function: validated-arguments →
# list of (agent_pattern, required_level). The empty pattern "" means
# "any authenticated principal" (unscoped reads, D5). An empty agent_id on a
# tool that then spans every agent resolves to the wildcard demand — holding
# grants on named agents does not add up to "*".

Demands = Callable[[dict], list[tuple[str, int]]]


def _agent_arg(args: dict, key: str = "agent_id") -> str:
    """Coerce an agent-scope argument to the pattern the guard evaluates.

    The guard runs OUTSIDE the auto_tool parameter validation, so the value
    here is whatever the caller sent. Anything that is not a non-empty string
    — absent, empty, an int, a list — resolves to the wildcard demand: the
    broadest requirement, and immune to unhashable-type surprises inside the
    check (review finding on PR #112).
    """
    value = args.get(key)
    if isinstance(value, str) and value:
        return value
    return WILDCARD


def _scoped(required: int, key: str = "agent_id") -> Demands:
    def demands(args: dict) -> list[tuple[str, int]]:
        return [(_agent_arg(args, key), required)]

    return demands


def _file_io_demands(key: str) -> Demands:
    """export_memories / import_memories: caller-directed file I/O.

    With ``CPERSONA_EXPORT_DIR`` unset (the shipped default) the path argument
    is caller-chosen anywhere on the filesystem, so the blast radius is not
    one agent's data — the demand escalates to read-write on ``"*"`` (review
    finding on PR #112, extending D4). With the confinement configured, the
    agent-scoped read-write demand of §6 applies.
    """

    def demands(args: dict) -> list[tuple[str, int]]:
        if not config.EXPORT_DIR:
            return [(WILDCARD, PERM_WRITE)]
        return [(_agent_arg(args, key), PERM_WRITE)]

    return demands


def _health_demands(args: dict) -> list[tuple[str, int]]:
    # check_health / deep_check: read; fix=true repairs, which is a write.
    required = PERM_WRITE if args.get("fix") else PERM_READ
    return [(_agent_arg(args), required)]


def _merge_demands(args: dict) -> list[tuple[str, int]]:
    source = _agent_arg(args, "source_agent_id")
    target = _agent_arg(args, "target_agent_id")
    if args.get("mode", "copy") == "move":
        # move deletes source rows: read-write on BOTH sides.
        return [(source, PERM_WRITE), (target, PERM_WRITE)]
    return [(source, PERM_READ), (target, PERM_WRITE)]


_AUTHENTICATED_ONLY: list[tuple[str, int]] = [("", PERM_READ)]

ACL_CLASSIFICATION: dict[str, Demands] = {
    # Unscoped reads (D5): no per-agent data; any authenticated principal.
    "persistence_status": lambda args: _AUTHENTICATED_ONLY,
    "get_queue_status": lambda args: _AUTHENTICATED_ONLY,
    "get_operating_context": lambda args: _AUTHENTICATED_ONLY,
    # Process-wide persistence switch affects every agent's writes.
    "pause_persistence": lambda args: [(WILDCARD, PERM_WRITE)],
    "resume_persistence": lambda args: [(WILDCARD, PERM_WRITE)],
    # Per-agent reads.
    "recall": _scoped(PERM_READ),
    "recall_with_context": _scoped(PERM_READ),
    "get_contents": _scoped(PERM_READ),
    "get_profile": _scoped(PERM_READ),
    "get_recall_precision": _scoped(PERM_READ),
    "list_memories": _scoped(PERM_READ),
    "list_episodes": _scoped(PERM_READ),
    # Per-agent writes.
    "store": _scoped(PERM_WRITE),
    "update_profile": _scoped(PERM_WRITE),
    "archive_episode": _scoped(PERM_WRITE),
    "update_memory": _scoped(PERM_WRITE),
    "lock_memory": _scoped(PERM_WRITE),
    "unlock_memory": _scoped(PERM_WRITE),
    "delete_memory": _scoped(PERM_WRITE),
    "delete_episode": _scoped(PERM_WRITE),
    "delete_agent_data": _scoped(PERM_WRITE),
    # Calibration state is per-agent mutable state.
    "calibrate_threshold": _scoped(PERM_WRITE),
    "set_recall_precision": _scoped(PERM_WRITE),
    # D4 (amended twice): caller-directed file I/O — read-write, escalating to
    # the wildcard demand while CPERSONA_EXPORT_DIR leaves paths unconfined.
    "export_memories": _file_io_demands("agent_id"),
    "import_memories": _file_io_demands("target_agent_id"),
    "merge_memories": _merge_demands,
    # Empty agent_id sweeps every agent on these; _scoped maps "" to "*".
    "check_health": _health_demands,
    "deep_check": _health_demands,
    "migrate_channel_axis": _scoped(PERM_WRITE),
}


# ---------------------------------------------------------------------------
# Dispatch guard (design §5.2)
# ---------------------------------------------------------------------------


def _denial(tool: str, client_id: str, *, agent_id: str = "", required: int = 0, detail: str = "") -> dict:
    response: dict = {"ok": False, "error": "permission_denied", "tool": tool}
    if agent_id:
        response["agent_id"] = agent_id
    if required:
        response["required"] = _PERMISSION_NAMES[required]
    # The caller's own resolved identity is not a secret to itself (§5.3).
    response["client_id"] = client_id
    if detail:
        response["detail"] = detail
    return response


def _wrap(name: str, handler):
    async def guarded(arguments: dict) -> dict:
        config = _active_config
        if config is None:
            # Legacy mode: zero decisions (§4.1).
            return await handler(arguments)
        principal = _current_principal.get()
        if principal is None:
            # A wiring regression, not a caller mistake: the transport
            # authenticated nothing yet a call arrived. Fail closed (§7).
            logger.error(
                "ACL: no principal resolved for %s while ACL mode is active — "
                "transport wiring regression; denying",
                name,
            )
            return _denial(name, "", detail="no principal resolved")
        demands = ACL_CLASSIFICATION.get(name)
        if demands is None:
            logger.warning("ACL: unclassified tool %s denied (client=%s)", name, principal.client_id)
            return _denial(
                name,
                principal.client_id,
                detail="tool not classified for ACL enforcement",
            )
        grants = config.grants_by_client.get(principal.client_id, {})
        for agent_pattern, required in demands(arguments):
            if agent_pattern == "":
                continue  # authenticated-only demand: the principal suffices
            if effective_permission(grants, agent_pattern) < required:
                logger.warning(
                    "ACL denial: client=%s tool=%s agent=%s required=%s",
                    principal.client_id,
                    name,
                    agent_pattern,
                    _PERMISSION_NAMES[required],
                )
                return _denial(
                    name, principal.client_id, agent_id=agent_pattern, required=required
                )
        return await handler(arguments)

    guarded._acl_guarded = True  # type: ignore[attr-defined]
    guarded._acl_inner = handler  # type: ignore[attr-defined]
    return guarded


def install(registry) -> None:
    """Wrap every registered handler with the capability guard.

    Installed unconditionally at import (idempotent); whether the guard decides
    anything is governed by ``activate`` — see the module docstring.
    """
    for name, handler in list(registry._handlers.items()):
        if not getattr(handler, "_acl_guarded", False):
            registry._handlers[name] = _wrap(name, handler)
