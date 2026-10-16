"""isolation_where() — the single source for the 3-axis read predicate (2.5.0).

Every read-side isolation predicate on the agent-scoped tables (memories /
episodes / profiles / pending_memory_tasks) is built here. Before 2.5.0 each
handler hand-rolled its own fragment — ``_agent_scope`` in checks.py,
``agent_clause`` in maintenance, ``channel_clause`` + ``gamma_clause`` glue in
the recall paths — and the 2.4.37/39 audits showed that every hand-rolled copy
is one forgotten axis away from a cross-bucket leak (bug-044/045/047/055/058
class). The structural gate (test_structural_gates.py Gate 2) now demands that
any dynamically-assembled isolation predicate come from this helper; ad-hoc
clause variables and inline waivers no longer pass.

Per-axis read semantics (deliberately NOT uniform — each axis has its own
contract):

- ``agent_id`` — hard isolation. ``None`` → no filter (a deliberate
  cross-agent scan, spelled out in code as ``isolation_where(agent_id=None)``
  or ``agent_id=agent_id or None`` for the maintenance "empty = CLI global
  sweep" convention); any string INCLUDING ``''`` → exact ``agent_id = ?``.
  No γ union: agents never share rows. Binding ``''`` addresses the
  empty-agent bucket, which do_store does accept and write — so a caller that
  forgets the ``or None`` narrows to rows owned by no named agent rather than
  widening. Not always an empty result, but never a cross-agent leak (the
  bug-044 class).
- ``project_id`` — γ semantics via the vendored ``gamma_clause`` (the
  cross-server single implementation): ``None`` → no filter, ``''`` → global
  pool only, ``'X'`` → ``IN ('X', '')``.
- ``channel`` — knob2 v2: ``None``/``''`` → no filter (all channels);
  ``'X'`` → ``(channel = 'X' OR channel = '')`` so channel-global rows surface
  in every scoped recall. Unlike project_id, ``''`` does NOT narrow to the
  global rows — a channel-less read sees everything.

Write-side identity (the exact 3-axis dedup probes) stays static SQL — it is
pinned by the composite UNIQUE indexes and checked by Gate 4, not built here.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from cpersona._vendored_mcp_common.isolation import gamma_clause

__all__ = ["IsolationFilter", "isolation_where"]


@dataclasses.dataclass(frozen=True)
class IsolationFilter:
    """A composed isolation predicate: ``clause`` + positional ``params``.

    The three accessors cover the gluing styles the call sites need; the
    structural gate recognises exactly these attribute names as
    helper-derived fragments, so embed one of them (not a copy of the
    string) into the SQL f-string.
    """

    clause: str  # "agent_id = ? AND project_id IN (?, ?)" — "" when unfiltered
    params: tuple[Any, ...]

    @property
    def and_clause(self) -> str:
        """`` AND <clause>`` for appending after an existing predicate; "" when empty."""
        return f" AND {self.clause}" if self.clause else ""

    @property
    def where(self) -> str:
        """`` WHERE <clause>`` for a statement with no other predicate; "" when empty."""
        return f" WHERE {self.clause}" if self.clause else ""


def isolation_where(
    *,
    agent_id: str | None = None,
    project_id: str | None = None,
    channel: str | None = None,
    alias: str = "",
) -> IsolationFilter:
    """Build the composed 3-axis isolation predicate (see module docstring).

    All axes are keyword-only so a call site reads as a declaration of intent;
    an axis left at ``None`` is an explicit "no filter on this axis" decision
    (the gate makes global scans spell out ``agent_id=None`` instead of
    carrying a waiver comment). ``alias`` prefixes every column (``alias="m"``
    → ``m.agent_id``) for joined queries.

    Axis order in the emitted clause is the identity-index order
    (agent_id, project_id, channel); ``params`` aligns with it.
    """
    pre = f"{alias}." if alias else ""
    fragments: list[str] = []
    params: list[Any] = []

    if agent_id is not None:
        fragments.append(f"{pre}agent_id = ?")
        params.append(agent_id)

    proj_frag, proj_params = gamma_clause(f"{pre}project_id", project_id)
    if proj_frag:
        fragments.append(proj_frag)
        params.extend(proj_params)

    if channel:
        fragments.append(f"({pre}channel = ? OR {pre}channel = '')")
        params.append(channel)

    return IsolationFilter(clause=" AND ".join(fragments), params=tuple(params))


def source_id_where(source_id: str, *, alias: str = "") -> IsolationFilter:
    """The per-user ``source_id`` restriction, as a helper-derived fragment.

    bug-336: this was a ``LIKE`` prefix predicate, and SQLite folds ASCII case in
    ``LIKE`` unless ``PRAGMA case_sensitive_like`` is set, which nothing in this
    package sets. So a recall scoped to ``discord:Alice`` also returned rows
    tagged ``discord:alice`` -- a different principal -- on every arm that used
    the clause: the keyword arm, the empty-query recency arm, the vector window
    read and its by-id hydrate. The escape helper neutralised the wildcards and
    nothing neutralised the folding.

    ``substr(...) = ?`` instead. Equality compares with BINARY collation, which
    is what the schema means by an id, and it needs no wildcard escaping at all,
    so the ``%``/``_``/``\\`` dance that used to sit in front of every call site
    goes with it. It is also the resolution the contiguous index already used on
    its side (a Python ``startswith``), so the two arms now answer the same
    question -- they disagreed, and the SQL side was the permissive one.

    A value names one id, or a namespace of ids (see ``source_id_is_namespace``).
    One id is compared whole: ``discord:12345`` is that user, and ``user:1`` is
    not also ``user:10``, whose id merely starts with the same characters. Only
    the namespace form is a prefix. The JSON id is compared as text on both
    forms, as ``substr`` always did, because ``json_extract`` hands a JSON number
    back as an integer. The contiguous index applies the same rule through
    ``source_id_matches``.

    Built here rather than at each arm because it is a read-side isolation
    predicate, which is what this module is for, and because four hand-rolled
    copies is how the axis came to be spelled four times and fixed in none.
    Empty ``source_id`` yields an empty filter, so callers can branch on it the
    way they always did.

    bug-794: the id is read only when ``json_valid(source)``. ``json_extract``
    raises "malformed JSON" on a source that is not JSON instead of returning
    NULL, and nothing in the schema keeps such rows out (the JSON repair leaves
    locked ones alone), so one of them in scope made every source-scoped scan
    fail. The guard is a CASE, as in the index builder's row plan (bug-743),
    because a CASE evaluates only the branch it selects wherever the fragment
    lands, while an ``AND`` short-circuit holds only as a top-level WHERE
    conjunct. A malformed source reads as no id, so it matches no filter --
    what the contiguous index already makes of it.
    """
    if not source_id:
        return IsolationFilter(clause="", params=())
    pre = f"{alias}." if alias else ""
    source_id_expr = f"CASE WHEN json_valid({pre}source) THEN json_extract({pre}source, '$.id') END"
    if source_id_is_namespace(source_id):
        return IsolationFilter(
            clause=f"substr({source_id_expr}, 1, ?) = ?",
            params=(len(source_id), source_id),
        )
    return IsolationFilter(
        clause=f"CAST({source_id_expr} AS TEXT) = ?",
        params=(source_id,),
    )


def source_id_is_namespace(source_id: str) -> bool:
    """Whether a non-empty ``source_id`` names a namespace rather than one id.

    The namespace form has always been written with its delimiter at the end
    (``discord:``, ``docs/``, ``user-``), and a principal's id ends in a letter
    or a digit, so the last character is what tells the two apart. Asked here
    once so the SQL arms and the contiguous index cannot draw the line in
    different places.
    """
    return not source_id[-1].isalnum()


def source_id_matches(value: Any, source_id: str) -> bool:
    """The Python form of ``source_id_where`` for a value already read out.

    ``value`` is the row's ``$.id`` as text, or None where the row has none,
    which no filter admits -- as SQL compares against NULL.
    """
    if value is None:
        return False
    text = str(value)
    return text.startswith(source_id) if source_id_is_namespace(source_id) else text == source_id
