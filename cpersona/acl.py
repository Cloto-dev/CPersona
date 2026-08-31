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
from cpersona.aliases import ALIAS_PREFIX

logger = logging.getLogger(__name__)

# Permission lattice (design §3): none < read < read-write.
PERM_NONE = 0
PERM_READ = 1
PERM_WRITE = 2
_PERMISSION_LEVELS = {"none": PERM_NONE, "read": PERM_READ, "read-write": PERM_WRITE}
_PERMISSION_NAMES = {PERM_READ: "read", PERM_WRITE: "read-write"}

WILDCARD = "*"

# A value no caller can send (NUL is not legal in a JSON string), used to ask a
# demands function a counterfactual: "would this still span every agent if the
# scope argument had been filled in?" It never reaches a grant lookup.
_SCOPE_PROBE = "\x00scope-probe"
# The arguments a tool can scope itself with. Named once because two separate
# counterfactuals iterate them, and a key present in one and not the other
# would make the advice disagree with the test that produced it.
_SCOPE_KEYS = ("agent_id", "source_agent_id", "target_agent_id")

# The stdio transport's reserved principal (design §5.4): no credential to
# resolve, the peer is whoever spawned the process. Grants for it come from
# the same file; if absent, stdio calls are denied like any ungranted client.
LOCAL_CLIENT_ID = "local"

# Whole-string environment reference for token values: "${VAR}".
_ENV_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")

_CLIENT_KEYS = {"client_id", "token", "grants", "per_subject"}

# The self sentinel (docs/OAUTH_DESIGN.md §12): a caller behind a per-subject
# boundary cannot know its alias before the server has issued one, so "@me"
# names "my own memory space" and the guard resolves it — resolve, then ACL,
# then query, in that order, so no grant is ever evaluated against the literal.
SELF_SENTINEL = "@me"


class AclConfigError(Exception):
    """Malformed ACL configuration — startup must fail, not degrade (§7)."""


@dataclass(frozen=True)
class Principal:
    """An authenticated identity. The only thing enforcement consumes.

    ``issuer`` and ``subject`` are filled only by a resolver that verified
    them — the OAuth verifier, which checked both as signed claims. Static
    resolvers leave them empty: a static token authenticates a client, and
    pretending it names a person would give the per-subject boundary a value
    nothing vouched for. Two kinds of identity, kept apart as fields rather
    than mixed into one namespace (docs/OAUTH_DESIGN.md §9, §12).
    """

    client_id: str
    issuer: str = ""
    subject: str = ""


@dataclass(frozen=True)
class AclConfig:
    """Loaded, validated grant table.

    ``token_entries`` is a tuple of (token, client_id) pairs the resolver
    compares constant-time; ``grants_by_client`` maps client_id → its grant
    dict (agent pattern → permission level). ``per_subject_clients`` names the
    clients whose row declared ``"per_subject": true`` — a restrictive
    boundary, consulted before any grant (docs/OAUTH_DESIGN.md §12).
    """

    grants_by_client: dict[str, dict[str, int]]
    token_entries: tuple[tuple[str, str], ...]
    per_subject_clients: frozenset[str] = frozenset()


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
    per_subject_clients: set[str] = set()

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
            # (§5.4). A token here would be an entry nothing can ever present —
            # an explicit null says precisely that, so it is the one value this
            # row may carry for the key.
            if entry.get("token") is not None:
                raise AclConfigError(
                    f"ACL client {LOCAL_CLIENT_ID!r} is the stdio principal and must "
                    "not carry a token"
                )
        elif "token" not in entry:
            # Omission stays a startup error (§7). A static client whose token
            # was forgotten must fail loudly rather than load as a row nothing
            # can present; declaring "no credential" is what the explicit null
            # is for, and keeping the two apart is what makes that safe.
            raise AclConfigError(
                f"ACL client {client_id!r}: token must be a non-empty string, or "
                "null for a principal a resolver asserts rather than one a caller "
                "presents; omitting the key does not declare that"
            )
        elif entry["token"] is not None:
            token_raw = entry["token"]
            if not isinstance(token_raw, str) or not token_raw:
                raise AclConfigError(
                    f"ACL client {client_id!r}: token must be a non-empty string"
                )
            token = _resolve_token_value(token_raw, client_id)
            if not token.isascii():
                # RFC 6750 token68 is ASCII, and HTTP header decoding
                # (latin-1) would mangle a non-ASCII token before the UTF-8
                # comparison ever saw it — the client could present the right
                # secret forever and never authenticate, with nothing
                # diagnosing why. Fail loudly at load instead (§7).
                raise AclConfigError(
                    f"ACL client {client_id!r}: token contains non-ASCII "
                    "characters; bearer tokens must be ASCII (RFC 6750)"
                )
            if token in seen_tokens:
                # Which client is a presented token? must have exactly one answer.
                raise AclConfigError(
                    f"ACL client {client_id!r}: token duplicates another client's"
                )
            seen_tokens.add(token)
            token_entries.append((token, client_id))

        per_subject = entry.get("per_subject", False)
        if not isinstance(per_subject, bool):
            raise AclConfigError(
                f"ACL client {client_id!r}: per_subject must be true or false"
            )
        if per_subject:
            if client_id == LOCAL_CLIENT_ID or entry.get("token") is not None:
                # A subject arrives only on a token an identity provider issued
                # and this server verified. The stdio principal is asserted by
                # transport and a static token authenticates a client — neither
                # carries a person, so a per_subject flag there is a written
                # policy that can never apply. Fail at load, not silently at
                # dispatch (§7).
                raise AclConfigError(
                    f"ACL client {client_id!r}: per_subject applies only to a "
                    "resolver-asserted principal (\"token\": null) — a subject "
                    "arrives only on a provider-issued token, so a static token "
                    "row or the stdio principal has no subject to partition by"
                )
            per_subject_clients.add(client_id)

        grants_by_client[client_id] = grants

    return AclConfig(
        grants_by_client=grants_by_client,
        token_entries=tuple(token_entries),
        per_subject_clients=frozenset(per_subject_clients),
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


# The alias ledger (docs/OAUTH_DESIGN.md §12), active only when some client row
# declared per_subject. Process-wide like the config above, and activated next
# to it at startup — a per_subject boundary with no ledger to resolve against
# is a wiring regression the guard denies rather than waves through.
_active_ledger = None


def activate_ledger(ledger) -> None:
    """Install (or clear, with None) the process-wide alias ledger."""
    global _active_ledger
    _active_ledger = ledger


def active_ledger():
    return _active_ledger


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

    demands._sweep_cause = lambda args: (  # type: ignore[attr-defined]
        ""
        if config.EXPORT_DIR
        else (
            "CPERSONA_EXPORT_DIR is unset, so the path argument is caller-chosen "
            "anywhere on the filesystem and the call is not confined to one agent"
        )
    )
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


def _process_wide(reason: str, required: int = PERM_WRITE) -> Demands:
    """A tool whose effect is process-wide: the all-agents demand is intrinsic.

    No argument narrows it, so the guard's scope advice would be advice the
    caller cannot follow. The reason travels with the demand rather than
    living in a table beside it, so a tool cannot be classified in one place
    and explained in another.
    """

    def demands(args: dict) -> list[tuple[str, int]]:
        return [(WILDCARD, required)]

    demands._sweep_cause = lambda args: reason  # type: ignore[attr-defined]
    return demands


_AUTHENTICATED_ONLY: list[tuple[str, int]] = [("", PERM_READ)]

ACL_CLASSIFICATION: dict[str, Demands] = {
    # Unscoped reads (D5): no per-agent data; any authenticated principal.
    "persistence_status": lambda args: _AUTHENTICATED_ONLY,
    "get_queue_status": lambda args: _AUTHENTICATED_ONLY,
    "get_operating_context": lambda args: _AUTHENTICATED_ONLY,
    # Process-wide persistence switch affects every agent's writes.
    "pause_persistence": _process_wide(
        "persistence is a process-wide switch, so pausing it stops writes for "
        "every agent this process serves, not only the caller's"
    ),
    "resume_persistence": _process_wide(
        "persistence is a process-wide switch, so resuming it restarts writes "
        "for every agent this process serves, not only the caller's"
    ),
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
    # The findings channel is whole-database by contract (SUPERAUDITOR_STANDARD
    # §7): no argument scopes it, so the all-agents READ demand is intrinsic.
    "get_session_findings": _process_wide(
        "findings are reported over the whole database by contract — the channel "
        "surfaces forgotten state, so no argument narrows it to one agent; scope a "
        "repair with check_health(agent_id=...) instead",
        required=PERM_READ,
    ),
    "migrate_channel_axis": _scoped(PERM_WRITE),
}


# ---------------------------------------------------------------------------
# Dispatch guard (design §5.2)
# ---------------------------------------------------------------------------


def _widened_by_omission(demands: Demands, arguments: dict) -> bool:
    """Would filling in the scope arguments have avoided the all-agents demand?

    Asked rather than assumed. A wildcard demand does not always come from an
    empty argument: export_memories / import_memories escalate to ``"*"`` on
    their own while ``CPERSONA_EXPORT_DIR`` is unset, whatever agent_id says
    (see _file_io_demands). Telling that caller to "pass agent_id" would be
    advice that does not work, so the counterfactual is run instead of guessed.
    """
    probed = dict(arguments)
    for key in _SCOPE_KEYS:
        probed[key] = _SCOPE_PROBE
    try:
        return all(pattern != WILDCARD for pattern, _ in demands(probed))
    except Exception:  # a demands function that dislikes the probe tells us nothing
        return False


def _omitted_scope_keys(demands: Demands, arguments: dict) -> list[str]:
    """Which scope arguments this call has to send to stop demanding every agent.

    The same reason _widened_by_omission runs a counterfactual instead of
    guessing: naming the wrong argument is advice that does not work. Every
    tool does not scope itself through ``agent_id`` — merge_memories takes
    ``source_agent_id`` / ``target_agent_id`` and has no ``agent_id`` at all,
    so a caller told to "pass agent_id" can comply and be denied again.

    Each key is tested by leaving it out while the others are filled: if the
    demand is still a sweep without it, the caller has to send it. That names
    both halves of a merge when both are missing, and only the missing half
    when one was sent.
    """
    needed = []
    for key in _SCOPE_KEYS:
        probed = dict(arguments)
        for other in _SCOPE_KEYS:
            if other != key:
                probed[other] = _SCOPE_PROBE
        try:
            if any(pattern == WILDCARD for pattern, _ in demands(probed)):
                needed.append(key)
        except Exception:  # as above: a demands function that dislikes the probe
            continue
    return needed


def _sweep_reach(grants: dict[str, int]) -> str:
    """How far an all-agents demand gets with these grants (§3, D6).

    A sweep fails in two ways that take different answers, and "denied" alone
    does not separate them: a client with no wildcard grant can never satisfy
    one — naming agents does not add up to ``"*"`` — while a client that has
    one is held down to its weakest row, so the call it wants is available per
    agent but not across all of them.
    """
    if WILDCARD not in grants:
        return "this client holds no all-agents grant, and named grants never add up to one"
    weakest = min(grants.values())
    limiting = sorted(name for name, level in grants.items() if level == weakest)
    return (
        "a sweep is satisfied only at the weakest grant this client holds "
        f"({_PERMISSION_NAMES.get(weakest, 'none')} on {' and '.join(limiting)})"
    )


def _scope_advice(omitted: list[str], wildcarded: list[str], grants: dict[str, int]) -> str:
    """What to tell a caller whose call resolved to the all-agents demand.

    Two causes arrive here and only one of them is "you forgot to scope this".
    A caller that sent ``agent_id="*"`` DID send a scope — it asked for every
    agent on purpose — so telling it to pass the argument it just passed is
    advice it can follow to the letter and be denied again, the same failure
    _omitted_scope_keys exists to prevent, one layer up.

    The counterfactuals cannot see the difference: both probes overwrite every
    scope key, so by the time the demand is computed the original value is gone
    and an omitted key looks exactly like one sent as ``"*"``. The arguments
    are therefore read directly here, and each cause gets the advice that works
    for it: fill the scope in, or stop asking for every agent.
    """
    parts = []
    if omitted:
        parts.append(
            "no agent scope was sent, so the call resolved to the all-agents demand — pass "
            + " and ".join(omitted)
            + " to scope it"
        )
    if wildcarded:
        parts.append(
            " and ".join(wildcarded)
            + ' was sent as "*", so the call demands every agent at once — '
            + _sweep_reach(grants)
            + ". Name a single agent to scope it"
        )
    if not parts:
        return (
            "no agent scope was sent, so the call resolved to the all-agents demand — "
            "pass an agent scope to scope it"
        )
    return "; ".join(parts)


def _intrinsic_sweep_detail(demands: Demands, arguments: dict, grants: dict[str, int]) -> str:
    """Why a sweep no argument can narrow was refused.

    The scope advice above answers the caller that widened its own call. This
    answers the caller that did not: some tools demand every agent by their
    nature — a process-wide switch, or caller-directed file I/O while
    CPERSONA_EXPORT_DIR leaves the path unconfined — whatever agent_id says.
    Both counterfactuals correctly decline to claim scoping would have helped,
    and the branch that used to follow them said nothing at all: a client
    holding read-write on its own agent, having scoped the call to exactly
    that agent, was told the call demanded ``"*"`` and given no reason it
    could act on (bug-264).

    The cause travels on the demands function (``_sweep_cause``) so that a
    tool classified as a sweep and a tool explained as one cannot drift apart.
    A classification that carries no cause still gets the part that is always
    true — scoping will not narrow this one — rather than an empty ``detail``.
    """
    cause = ""
    explain = getattr(demands, "_sweep_cause", None)
    if explain is not None:
        try:
            cause = explain(arguments) or ""
        except Exception:  # a classification that cannot explain itself must not raise here
            cause = ""
    base = (
        "no agent scope narrows this call — it demands every agent by its nature, and "
        + _sweep_reach(grants)
    )
    return f"{cause}; {base}" if cause else base


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
        known_client = principal.client_id in config.grants_by_client
        grants = config.grants_by_client.get(principal.client_id, {})

        # Per-subject boundary and the @me sentinel (docs/OAUTH_DESIGN.md §12).
        # Order is load-bearing: resolve, then ACL, then query. The sentinel is
        # rewritten to the alias before any demand is computed, so no grant is
        # ever evaluated against the literal "@me" and the handler only ever
        # sees the resolved alias.
        boundary = principal.client_id in config.per_subject_clients
        resolved_alias = ""
        alias_issued = False
        if boundary and not principal.subject:
            # A per_subject row matched a principal nothing attached a subject
            # to. The loader forbids the flag on every row a subject-less
            # resolver can produce, so this is a resolver regression — fail
            # closed like the missing-principal case above.
            logger.error(
                "ACL: per_subject client %s resolved with no subject on %s — "
                "resolver regression; denying",
                principal.client_id,
                name,
            )
            return _denial(
                name,
                principal.client_id,
                detail="per-subject client resolved with no subject",
            )
        if any(arguments.get(key) == SELF_SENTINEL for key in _SCOPE_KEYS):
            if not boundary:
                # Includes the stdio principal and every static-token client:
                # falling back to the client identity here would let "@me"
                # quietly mean "my client", a different kind of identity.
                return _denial(
                    name,
                    principal.client_id,
                    detail=(
                        '"@me" resolves the signed-in subject to its alias, and '
                        "this client's identity carries no subject boundary — the "
                        "sentinel is honored only for a client whose ACL row "
                        'declares "per_subject": true; name an agent_id explicitly'
                    ),
                )
            ledger = _active_ledger
            if ledger is None:
                logger.error(
                    "ACL: per_subject client %s sent %s but no alias ledger is "
                    "active — startup wiring regression; denying",
                    principal.client_id,
                    SELF_SENTINEL,
                )
                return _denial(
                    name, principal.client_id, detail="alias ledger not active"
                )
            try:
                resolved_alias, alias_issued = ledger.resolve_or_issue(
                    principal.issuer, principal.subject
                )
            except Exception as exc:
                # An alias that authorized this call but was never durably
                # recorded would be re-rolled on restart, stranding whatever
                # the call stored — refuse instead (aliases.py has the full
                # argument).
                logger.error(
                    "ACL: alias issuance failed for client=%s: %s",
                    principal.client_id,
                    exc,
                )
                return _denial(
                    name,
                    principal.client_id,
                    detail="alias could not be recorded; see the server log",
                )
            arguments = {
                **arguments,
                **{
                    key: resolved_alias
                    for key in _SCOPE_KEYS
                    if arguments.get(key) == SELF_SENTINEL
                },
            }

        demand_list = demands(arguments)

        if boundary:
            # The restrictive half: a per-subject principal reaches its own
            # alias and nothing else, whatever the grant table would allow —
            # explicit-deny beats every allow, the wildcard included, so a
            # "*: read-write" row stays a convenience for the client without
            # becoming cross-subject reach. Evaluated BEFORE the grant loop on
            # purpose; moving it after (or deleting it) turns the
            # deny-overrides tests in tests/test_per_subject.py red.
            own = resolved_alias
            if not own and _active_ledger is not None:
                own = _active_ledger.peek(principal.issuer, principal.subject) or ""
            for agent_pattern, required in demand_list:
                if agent_pattern == "":
                    continue  # no per-agent data touched; the boundary has no say
                if own and agent_pattern == own:
                    continue
                logger.warning(
                    "ACL denial (per-subject boundary): client=%s tool=%s agent=%s",
                    principal.client_id,
                    name,
                    agent_pattern,
                )
                if agent_pattern == WILDCARD:
                    detail = (
                        "this client partitions by signed-in subject "
                        "(per_subject), and this call demands every agent at "
                        'once — scope it to your own space with agent_id "@me"'
                    )
                else:
                    detail = (
                        "this client partitions by signed-in subject "
                        "(per_subject): each subject reaches only its own alias "
                        '— address your own space as "@me"; the alias it '
                        "resolves to is echoed back as resolved_agent_id"
                    )
                return _denial(
                    name,
                    principal.client_id,
                    agent_id=agent_pattern,
                    required=required,
                    detail=detail,
                )

        for agent_pattern, required in demand_list:
            if agent_pattern == "":
                continue  # authenticated-only demand: the principal suffices
            if effective_permission(grants, agent_pattern) < required:
                logger.warning(
                    "ACL denial: client=%s tool=%s agent=%s required=%s%s",
                    principal.client_id,
                    name,
                    agent_pattern,
                    _PERMISSION_NAMES[required],
                    "" if known_client else " (no entry in the grant table)",
                )
                detail = ""
                if not known_client:
                    # Authenticated and granted nothing at all. The unscoped
                    # tools answer while every scoped one refuses, so the
                    # connection looks healthy and remembers nothing — the
                    # shape hardest to read as a configuration gap. The scope
                    # advice below cannot apply here: no agent is reachable at
                    # any level, so "pass agent_id" is advice a caller can
                    # follow exactly and be denied again. Name the cause.
                    detail = (
                        "this client has no entry in the ACL grant table, so no "
                        "agent is reachable at any level — an operator running "
                        "ACL mode states every principal explicitly, this one "
                        "included"
                    )
                elif agent_pattern == WILDCARD:
                    malformed = [
                        k
                        for k in _SCOPE_KEYS
                        if k in arguments and not isinstance(arguments[k], str)
                    ]
                    if malformed:
                        # §5.3: a mis-wired client diagnoses itself from its
                        # side of the wire — say why the scope widened.
                        detail = (
                            f"non-string {malformed[0]} argument resolved to "
                            "the all-agents demand"
                        )
                    elif _widened_by_omission(demands, arguments):
                        # The common case, and the one a caller can fix itself:
                        # it sent no scope, so every agent was demanded. Said
                        # explicitly because "agent_id": "*" in the response
                        # only shows the consequence, not the cause — a caller
                        # would have to already know that "*" means "you did
                        # not scope this". The argument is named from the
                        # counterfactual rather than assumed to be agent_id,
                        # which merge_memories does not even accept.
                        #
                        # A key the caller sent as "*" itself lands in the same
                        # list — the probe cannot tell an omitted key from a
                        # deliberately widened one — so the two are separated
                        # here before either is named (bug-263).
                        needed = _omitted_scope_keys(demands, arguments)
                        wildcarded = [k for k in needed if arguments.get(k) == WILDCARD]
                        detail = _scope_advice(
                            [k for k in needed if k not in wildcarded], wildcarded, grants
                        )
                    else:
                        # Neither malformed nor widened by omission: the demand
                        # is intrinsic to the tool. Silence here is what left a
                        # correctly-scoped caller reading "agent_id": "*" with
                        # nothing naming the real cause.
                        detail = _intrinsic_sweep_detail(demands, arguments, grants)
                return _denial(
                    name,
                    principal.client_id,
                    agent_id=agent_pattern,
                    required=required,
                    detail=detail,
                )
        result = await handler(arguments)
        if resolved_alias and isinstance(result, dict):
            # The @auto idiom (resolved_project_id): a resolution the server
            # performed is echoed so the caller can see — and later address —
            # what it actually reached. The alias, never the raw subject.
            result["resolved_agent_id"] = resolved_alias
            if alias_issued:
                result["alias_issued"] = True
        return result

    guarded._acl_guarded = True  # type: ignore[attr-defined]
    guarded._acl_inner = handler  # type: ignore[attr-defined]
    return guarded


def reserved_agent_id_collisions(agent_ids, known_aliases=frozenset()) -> list[str]:
    """Which of these agent ids collide with per-subject reserved names.

    Per-subject partitioning reserves two shapes in the agent namespace: the
    literal ``@me`` sentinel (a stored row under it could never be addressed —
    the guard rewrites the name before any query) and the ``u-`` alias prefix
    (an agent there that the alias ledger does not record is indistinguishable
    from an issued alias, so the boundary could hand one subject another
    tenant's data). ``known_aliases`` — the ledger's issued set — is exempt
    (bug-267): those names ARE issued aliases, and refusing them meant the
    server's own issuance failed its next boot. The ``@me`` sentinel is never
    exempt; the ledger cannot issue it. Checked at startup, only when some
    client row declared per_subject: an existing deployment that never opts in
    keeps every name it has.
    """
    return sorted(
        aid
        for aid in set(agent_ids)
        if aid == SELF_SENTINEL
        or (aid.startswith(ALIAS_PREFIX) and aid not in known_aliases)
    )


def install(registry) -> None:
    """Wrap every registered handler with the capability guard.

    Installed unconditionally at import (idempotent); whether the guard decides
    anything is governed by ``activate`` — see the module docstring.
    """
    for name, handler in list(registry._handlers.items()):
        if not getattr(handler, "_acl_guarded", False):
            registry._handlers[name] = _wrap(name, handler)
