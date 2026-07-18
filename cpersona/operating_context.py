"""Server-served operating context (2.5.1, docs/OPERATING_CONTEXT_DESIGN.md).

Loads the operator-owned sidecar (`~/.cpersona/operating-context.toml`) that
carries the operating doctrine every connected client should receive:

- Soft layer: `[instructions].summary` is threaded into the MCP `initialize`
  response verbatim (deterministic distribution; compliance stays probabilistic).
- Hard layer: `[registry].project_ids` validation and the opt-in `"@auto"`
  sentinel resolved via `[defaults]` (deterministic enforcement, mode-gated).

Design invariants (§3, §5):

- Absent file, kill switch, or an unparseable file -> the feature is fully
  dormant: no instructions, no validation, zero behavior deltas. A config typo
  must never take memory down, so parse failures degrade to dormant + a
  `check_health` finding instead of raising.
- Reload is lazy and mtime-based, so the Hard layer and `get_operating_context`
  pick up operator edits live. The instructions text is naturally frozen per
  connection (MCP sends it only at `initialize`).
- Explicit caller values are never rewritten; `""` (global pool) and omitted /
  None are always valid and never validated.

The write path is the filesystem (§7): there is deliberately no MCP write tool
for this file, and no code in this module mutates it.
"""

import logging
import os
import tomllib

logger = logging.getLogger(__name__)

AUTO_SENTINEL = "@auto"
ENFORCE_MODES = ("off", "warn", "reject")
DEFAULT_PATH = "~/.cpersona/operating-context.toml"
# §4 size discipline: the summary is a per-session fixed cost on every
# connected client. check_health warns above this (operating_context_size).
SUMMARY_WARN_CHARS = 3000
FILE_FORMAT_VERSION = 1


class OperatingContext:
    """Parsed, validated sidecar content. Immutable value object."""

    __slots__ = ("revision", "summary", "project_ids", "enforce", "defaults", "doctrine")

    def __init__(
        self,
        revision: str,
        summary: str,
        project_ids: list[str],
        enforce: str,
        defaults: dict[str, str],
        doctrine: dict[str, str],
    ):
        self.revision = revision
        self.summary = summary
        self.project_ids = project_ids
        self.enforce = enforce
        self.defaults = defaults
        self.doctrine = doctrine


# Lazy-reload cache: (resolved_path, mtime_ns) -> parse outcome. parse_error is
# retained for check_health even though the context itself degrades to None.
_cached_key: tuple[str, int] | None = None
_cached_context: OperatingContext | None = None
_cached_error: str | None = None


def _resolve_path() -> str | None:
    """Sidecar path, or None when the kill switch disables the feature.

    Env is read per call (not import-time) so tests and long-lived processes
    see live operator changes without a re-import.
    """
    if os.environ.get("CPERSONA_OPERATING_CONTEXT", "").lower() == "off":
        return None
    return os.path.expanduser(
        os.environ.get("CPERSONA_OPERATING_CONTEXT_PATH") or DEFAULT_PATH
    )


def _parse(raw: bytes) -> OperatingContext:
    """Parse + schema-validate the sidecar. Raises ValueError on any violation.

    Validation is strict on shape (a typo'd file must be *visibly* broken via
    check_health, not half-applied) but additive on unknown keys, so a newer
    file format can carry extra sections past an older server.
    """
    data = tomllib.loads(raw.decode("utf-8"))
    version = data.get("version")
    if version != FILE_FORMAT_VERSION:
        raise ValueError(f"unsupported file-format version {version!r} (expected {FILE_FORMAT_VERSION})")
    revision = data.get("context_revision", "")
    if not isinstance(revision, str):
        raise ValueError("context_revision must be a string")

    instructions = data.get("instructions", {})
    if not isinstance(instructions, dict):
        raise ValueError("[instructions] must be a table")
    summary = instructions.get("summary", "")
    if not isinstance(summary, str):
        raise ValueError("instructions.summary must be a string")

    registry = data.get("registry", {})
    if not isinstance(registry, dict):
        raise ValueError("[registry] must be a table")
    project_ids = registry.get("project_ids", [])
    if not isinstance(project_ids, list) or not all(isinstance(p, str) for p in project_ids):
        raise ValueError("registry.project_ids must be a list of strings")
    enforce = registry.get("enforce", "warn")
    if enforce not in ENFORCE_MODES:
        raise ValueError(f"registry.enforce must be one of {ENFORCE_MODES}, got {enforce!r}")

    defaults = data.get("defaults", {})
    if not isinstance(defaults, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in defaults.items()
    ):
        raise ValueError("[defaults] must map agent_id strings to project_id strings")

    doctrine: dict[str, str] = {}
    for i, section in enumerate(data.get("doctrine", [])):
        if (
            not isinstance(section, dict)
            or not isinstance(section.get("name"), str)
            or not section["name"]
            or not isinstance(section.get("body"), str)
        ):
            raise ValueError(f"[[doctrine]] entry {i} must have string 'name' and 'body'")
        if section["name"] in doctrine:
            raise ValueError(f"duplicate doctrine section name {section['name']!r}")
        doctrine[section["name"]] = section["body"]

    return OperatingContext(revision, summary, project_ids, enforce, dict(defaults), doctrine)


def get_context() -> OperatingContext | None:
    """The current operating context, or None when the feature is dormant.

    Dormant = kill switch, absent file, or parse/schema failure (the error is
    kept for load_state / check_health). Re-parses only when the resolved path
    or its mtime changes.
    """
    global _cached_key, _cached_context, _cached_error
    path = _resolve_path()
    if path is None:
        _cached_key, _cached_context, _cached_error = None, None, None
        return None
    try:
        stat = os.stat(path)
    except OSError:
        _cached_key, _cached_context, _cached_error = None, None, None
        return None
    key = (path, stat.st_mtime_ns)
    if key == _cached_key:
        return _cached_context
    try:
        with open(path, "rb") as f:
            context = _parse(f.read())
        _cached_context, _cached_error = context, None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError) as e:
        # Non-fatal by design: the server boots with the feature off and
        # check_health surfaces the finding (operating_context_parse).
        logger.warning("operating context %s unusable, feature dormant: %s", path, e)
        _cached_context, _cached_error = None, str(e)
    _cached_key = key
    return _cached_context


def instructions_text() -> str | None:
    """The `initialize` instructions payload, or None when dormant/empty."""
    context = get_context()
    if context is None or not context.summary.strip():
        return None
    return context.summary


def load_state() -> dict:
    """Loader state snapshot for check_health (§8) and get_operating_context.

    `present` is whether a sidecar file exists at the resolved path;
    `parse_error` is the retained failure when that file is unusable.
    """
    path = _resolve_path()
    if path is None:
        return {"enabled": False, "path": None, "present": False, "parse_error": None, "summary_len": 0}
    context = get_context()  # refresh the cache against the live file
    return {
        "enabled": True,
        "path": path,
        "present": os.path.exists(path),
        "parse_error": _cached_error,
        "summary_len": len(context.summary) if context is not None else 0,
    }


def check_project_id(
    project_id: str | None, agent_id: str, write: bool
) -> tuple[str | None, str | None, str | None]:
    """Hard-layer gate for one tool call: (resolved_project_id, warning, error).

    Invariant table (§5): omitted/None and "" pass through untouched and are
    never validated; an explicit value is validated but never rewritten; only
    the literal "@auto" sentinel is resolved (via [defaults] by agent_id).
    Reads warn rather than reject even in reject mode — a bad read filter
    loses nothing, a bad write pollutes a bucket (§5.1 damage asymmetry).
    When the feature is dormant everything passes through unchanged.
    """
    context = get_context()
    if context is None or project_id is None or project_id == "":
        return project_id, None, None

    resolved = project_id
    warning = None
    if project_id == AUTO_SENTINEL:
        mapped = context.defaults.get(agent_id)
        if mapped is None:
            msg = (
                f"@auto has no [defaults] mapping for agent_id '{agent_id}' "
                f"(rev {context.revision}); resolved to '' (global pool)"
            )
            if context.enforce == "reject":
                return None, None, msg
            resolved = ""
            warning = None if context.enforce == "off" else msg
        else:
            resolved = mapped
        if resolved == "":
            return resolved, warning, None

    if context.enforce != "off" and resolved not in context.project_ids:
        msg = f"project_id '{resolved}' not in registry (rev {context.revision})"
        if context.enforce == "reject" and write:
            return None, None, msg
        warning = msg
    return resolved, warning, None
