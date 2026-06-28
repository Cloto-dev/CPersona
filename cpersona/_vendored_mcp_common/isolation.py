"""γ-semantics two-tier isolation helpers shared by CPersona & CScheduler.

This module is the **single source of truth** for the per-axis isolation
filter (project_id, agent_id, …) used to give each MCP server a
two-bucket-aware view of its data:

- Write side: a missing / non-string / empty value collapses to ``''``,
  which we treat as the *global pool* — rows visible from every bucket
  read query.
- Read side: callers can pass

  - ``None`` → no filter on this axis (return everything)
  - ``''``   → match only the global pool (rows whose axis = ``''``)
  - ``'X'``  → match the named bucket *plus* the global pool, i.e.
    ``axis IN ('X', '')`` — the eponymous "γ" union.

Each MCP server typically wires one or two axes (project_id alone for
CPersona, project_id × agent_id for CScheduler). The functions here are
axis-agnostic: pass any column name and any caller-supplied value, get
back a SQL fragment plus parameter list ready to splat into
``aiosqlite.Connection.execute``.

Pure-Python, stdlib-only. Database backend agnostic — emits ``IN (?, ?)``
or ``= ?`` placeholders that work with sqlite3, asyncpg, mysql, etc.

Versioning: introduced in cloto-mcp-cscheduler 0.2.4 / cloto-mcp-cpersona
2.4.18 as a refactor of the verbatim-duplicated logic from those two
servers' v0.2.3 / v2.4.17 patches.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "gamma_clause",
    "coerce_for_write",
    "coerce_for_read",
    "extract_axes_for_write",
    "extract_axes_for_read",
]


def gamma_clause(column: str, value: str | None) -> tuple[str, list[Any]]:
    """Build a SQL fragment + params for γ semantics on `column`.

    - ``value is None``  → ``('', [])`` so the caller can skip the filter
      entirely (= "return everything across all buckets").
    - ``value == ''``    → ``('column = ?', [''])`` — match only the
      global pool. Collapsed to a bare ``=`` so we don't double-bind the
      same string into ``IN``.
    - any other string   → ``('column IN (?, ?)', [value, ''])`` —
      γ union of the named bucket and the global pool.

    The column name is interpolated raw, so callers MUST pass a trusted
    identifier (typed as ``project_id`` / ``t.agent_id`` etc., never
    user-supplied input).
    """
    if value is None:
        return ("", [])
    if value == "":
        return (f"{column} = ?", [""])
    return (f"{column} IN (?, ?)", [value, ""])


def coerce_for_write(value: Any) -> str:
    """Coerce an isolation axis value for INSERT.

    Strings are preserved verbatim (including ``''`` for the global
    pool). Anything else (``None``, missing kwarg → ``None`` upstream,
    ints, dicts, etc.) collapses to ``''``. This matches the "default
    to global pool" semantic the schema column declares with
    ``DEFAULT ''``.
    """
    if isinstance(value, str):
        return value
    return ""


def coerce_for_read(value: Any) -> str | None:
    """Coerce an isolation axis value for γ-filtered SELECT.

    Strings (including ``''``) are preserved verbatim — the caller
    distinguishes "global pool only" (``''``) from "no filter"
    (``None``). Anything else collapses to ``None``, matching the
    "absent kwarg → no filter" intuition for read paths.
    """
    if isinstance(value, str):
        return value
    return None


def extract_axes_for_write(arguments: dict, *axes: str) -> tuple[str, ...]:
    """Bulk extract + coerce multiple isolation axes from an MCP
    arguments dict for write paths. Returns a tuple of strings (one per
    axis name in `axes`), each defaulting to ``''`` when missing.

    Example::

        project_id, agent_id = extract_axes_for_write(
            arguments, "project_id", "agent_id"
        )
    """
    return tuple(coerce_for_write(arguments.get(a)) for a in axes)


def extract_axes_for_read(arguments: dict, *axes: str) -> tuple[str | None, ...]:
    """Bulk extract + coerce multiple isolation axes from an MCP
    arguments dict for read paths. Returns a tuple of ``Optional[str]``
    (one per axis name), where ``None`` means "no filter on this axis"
    and ``''`` means "global pool only" — see :func:`gamma_clause` for
    the SQL semantics."""
    return tuple(coerce_for_read(arguments.get(a)) for a in axes)
