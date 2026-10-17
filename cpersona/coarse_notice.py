"""The coarse index suggestion — `docs/BINARY_COARSE_SEARCH_DESIGN.md` §7.

In the default mode a time cue searches the part of its period past the scan window
only through a coarse index, and nothing builds that index automatically (decision
B). So a store that grows past the window loses that reach without anyone having
decided to. This tells the agent once, on a recall, so that it can propose the
build to the user: the server detects, the user decides, the agent relays — the
line the findings surface already draws (`checks.check_coarse_index`).

It rides on recall for the reason the `update` notice does: recall is the one call
every connected agent makes. It is absent unless there is something to say, and said
once per declared session (once per process without one), the contract of
`update_check.notice`.

What it suggests is the call it names, so it is said only where that call builds a
file a time cue then reads: the count is the check's own (rows with a stored
vector), taken in the scope the cue arm searches, source filter included, and
nothing is said where a recall has no local vector search for the remainder to
extend. The count is one statement, read only once the cheap gates have passed:
the mode, the search, the pool count the quality gate already read (an upper bound
on it), the session, and the index state, a cached file stat.
"""

from __future__ import annotations

from cpersona import coarse_index
from cpersona import config
from cpersona import vector

# Same bound, and the same reason, as update_check.NOTICE_SESSION_CAP: the key space
# is client-supplied, and eviction only forgets that a session was already told, so
# the worst case is telling one session twice.
NOTICE_SESSION_CAP = 256

KIND = "coarse_index"

# Concurrency: the mutators contain no `await` between read and write, so they run
# atomically between awaits under the asyncio single thread, as in update_check.
_told_sessions: dict[str, None] = {}
_notice_emitted: bool = False


def _index_usable() -> bool:
    """Whether a coarse index file loads. A broken one is not usable: the suggestion
    stands until it is built again, which is what check_health reports too."""
    try:
        return coarse_index.cached_coarse_index("memories") is not None
    except Exception:  # noqa: BLE001 — IndexUnusable or a corrupt file: either way, not usable
        return False


def local_vector_search() -> bool:
    """Whether a recall embeds its query and ranks the local store with it.

    The time cue's vector half, and so its remainder, exists only then: without an
    embedding client there is no query vector, and with remote vector search the
    service ranks under its own window. The predicate the recall path gates the
    remote branch on, read from the same module: a URL that does not end in /embed
    names no search route, so such a recall ranks the local store (bug-786).
    """
    if not vector._embedding_client:
        return False
    return not vector.remote_arm_armed(vector.VECTOR_SEARCH_MODE)


def _told(session_key: str, declared: bool) -> bool:
    return session_key in _told_sessions if declared else _notice_emitted


async def scope_notice(
    *,
    agent_id: str,
    project_id: str | None,
    channel: str,
    source_id: str,
    pool_records: int,
    window: int,
    session_key: str = "",
    declared: bool = False,
) -> dict | None:
    """`notice` for a recall's scope, counted as the check and the cue arm count.

    ``pool_records`` is every memory in the scope without the source filter, the
    count the recall already holds; the rows with a stored vector are never more,
    so below the line it decides without a statement.
    """
    if config.CUE_COARSE_MODE != "auto" or not local_vector_search():
        return None
    from cpersona import checks
    from cpersona.database import connection
    from cpersona.isolation import isolation_where, source_id_where

    if pool_records - window < checks.INDEX_MATTERS_ROWS or _told(session_key, declared) or _index_usable():
        return None
    iso = isolation_where(agent_id=agent_id, project_id=project_id, channel=channel)
    src = source_id_where(source_id)
    async with connection() as db:
        rows = await db.execute_fetchall(
            f"SELECT COUNT(*) FROM memories WHERE {iso.clause} AND embedding IS NOT NULL{src.and_clause}",
            (*iso.params, *src.params),
        )
    return notice(int(rows[0][0]), window, session_key, declared)


def notice(records: int, window: int, session_key: str = "", declared: bool = False) -> dict | None:
    """The ``suggestion`` payload for a recall response, or None.

    ``records`` is the number of memories with a stored vector in the recall's
    scope (`scope_notice` counts them), ``window`` the number a time cue's vector
    search reads. Something to say means: the cue mode
    is the default (auto), the scope holds at least ``checks.INDEX_MATTERS_ROWS``
    records past the window — the line from which the health check reports the
    index — and no coarse index loads. An operator who set the mode explicitly
    chose; this is for the deployment where nobody did.
    """
    global _notice_emitted
    if config.CUE_COARSE_MODE != "auto":
        return None
    from cpersona import checks  # the health check's own threshold, read at call time

    past = records - window
    if past < checks.INDEX_MATTERS_ROWS:
        return None
    if _index_usable():
        return None
    if declared:
        if session_key in _told_sessions:
            return None
        _remember_told(session_key)
    else:
        if _notice_emitted:
            return None
        _notice_emitted = True
    return {
        "kind": KIND,
        "records": records,
        "past_window": past,
        "message": (
            f"This scope holds {records} memories with a stored vector, and a time cue's vector search reads the "
            f"{window} most recently stored, so {past} are past it. Building the coarse index "
            "lets a time cue search the rest of its period. Building it needs write access "
            "to every agent on this server. Tell the user, and build it only if they agree."
        ),
        "fix": {"tool": "check_health", "arguments": {"checks": ["coarse_index"], "fix": True}},
    }


def _remember_told(session_key: str) -> None:
    _told_sessions[session_key] = None
    while len(_told_sessions) > NOTICE_SESSION_CAP:
        _told_sessions.pop(next(iter(_told_sessions)))


def reset() -> None:
    """Forget who was told. For tests."""
    global _notice_emitted
    _told_sessions.clear()
    _notice_emitted = False
