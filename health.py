"""Embedding-health runtime state for the degraded-advisory guard.

CPersona's ``recall`` silently degrades to keyword/FTS-only when the embedding backend
is unavailable — either because it is unconfigured (``mode=none``) or because a
configured http endpoint is unreachable (the process died, the port changed, the DB was
copied to a host without the embedding server, a startup race). The install-time
``cpersona-setup`` SKILL self-check is a snapshot and cannot catch post-install drift.
This module is the runtime guard: a process-level health state machine, fed by a
measurement at the embedding boundary (``vector.py``), read by ``do_recall`` to attach an
``advisory`` to the recall response so the calling agent can notify the user.

This is "Route B" (cpersona-local). The vendored ``EmbeddingClient.embed()`` swallows the
real error and returns ``None`` for both ``mode=none`` and an http failure —
indistinguishable to callers. Route B works around that without touching vendored common:
``vector.py`` runs a short health-probe to capture the real error string. Route A
(CPersona 2.5.0) makes ``embed()`` surface ``{attempted, ok, error}`` natively and removes
the probe; the advisory contract here (the payload struct + the ``do_recall`` ``advisory``
field) is the stable interface that survives that swap.

States
------
- ``unknown`` : no observation yet (initial; silent — no advisory).
- ``healthy`` : embedding produced a usable vector (silent — no advisory).
- ``hint``    : embeddings are unconfigured (``mode=none``) — recall is intentionally
                keyword/FTS-only. Detected statically at ``do_recall`` entry
                (``observe_config``); the vector embed path is never entered when
                ``mode=none``, so ``hint`` never reaches the probe.
- ``fault``   : ``mode=http``/``api`` but the endpoint is unreachable. Promoted only after
                ``FAULT_PROMOTE_THRESHOLD`` consecutive probe failures (single-blip
                debounce). Promotes only from ``unknown``/``healthy``; ``hint`` never
                reaches it.

Firing
------
On the first ``maybe_advisory()`` of a degraded episode the full runbook fires once;
subsequent calls in the same episode return a short reminder; ``healthy``/``unknown`` are
completely silent. Recovery (``observe_ok``) re-arms the full template.

Concurrency
-----------
State is module-level; the mutators contain no ``await``, so they run atomically between
awaits under the asyncio single thread / GIL — no ``asyncio.Lock`` (same argument as
``_vendored_mcp_common/no_persist.py``). Per-process: a server restart resets to
``unknown``, which is correct (a respawned server re-measures on its first recall).

Versioning: introduced in cloto-mcp-cpersona 2.4.33.
"""

from __future__ import annotations

import config

UNKNOWN = "unknown"
HEALTHY = "healthy"
HINT = "hint"
FAULT = "fault"

# Consecutive probe failures required before healthy/unknown -> fault. A single failure
# is debounced so a transient blip does not raise a fault advisory.
FAULT_PROMOTE_THRESHOLD = 2

_state: str = UNKNOWN
_severity: str | None = None
_reason: str | None = None
_evidence: str | None = None
_consecutive_failures: int = 0
_advisory_emitted: bool = False


# --- Runbook templates (static skeletons; the dynamic problem rides the evidence slot) ---

FAULT_RUNBOOK_FULL = (
    "**Notify the user:** CPersona's semantic (vector) recall is currently DEGRADED and "
    "has fallen back to keyword/FTS-only search. Measured failure: {evidence}.\n\n"
    "Impact: recall still returns results, but only on shared keywords — memories phrased "
    "differently from the query, and older memories, may be silently missed. Treat recall "
    "completeness as reduced until this is fixed.\n\n"
    "Investigate: (1) is the embedding server process alive? (2) is its port reachable "
    "from this host? (3) curl the embedding URL; (4) was the embedding model downloaded on "
    "this machine?\n\n"
    "Repair: start (or restart) the embedding server, or re-run the cpersona bootstrap "
    "script, or correct the embedding URL/port env (EMBEDDING_HTTP_URL / "
    "CPERSONA_EMBEDDING_URL) and restart CPersona.\n\n"
    "Plain version for the user: \"My long-term memory search is running in a reduced mode "
    "right now, so I might miss things I would normally remember — it needs the embedding "
    "service brought back up.\"\n\n"
    "To silence this advisory (e.g. an intentional keyword-only deployment) set "
    "CPERSONA_DEGRADED_ADVISORY=false."
)

FAULT_RUNBOOK_SHORT = (
    "Reminder: the embedding backend is still unreachable ({evidence}); recall remains "
    "keyword/FTS-only."
)

HINT_RUNBOOK_FULL = (
    "**Notify the user:** CPersona is running WITHOUT embeddings (mode=none), so recall is "
    "keyword/FTS-only — semantic similarity search is off. This is fine for a deliberate "
    "keyword-only setup, but differently-worded memories may not be found.\n\n"
    "To enable semantic recall: set the embedding env (EMBEDDING_MODE=http + "
    "EMBEDDING_HTTP_URL, or run the cpersona bootstrap which configures a local embedding "
    "server) and restart CPersona.\n\n"
    "Plain version for the user: \"My memory search is in keyword-only mode — turning on "
    "the embedding service would let me recall by meaning, not just exact words.\"\n\n"
    "To silence this advisory set CPERSONA_DEGRADED_ADVISORY=false."
)

HINT_RUNBOOK_SHORT = "Reminder: embeddings are off (mode=none); recall is keyword/FTS-only."


def observe_config() -> None:
    """Detect the static ``hint`` case (``mode=none``) at ``do_recall`` entry.

    When ``EMBEDDING_MODE`` is ``none`` the embedding client is never constructed, so the
    vector embed path (the ``fault`` observe point) is never entered — ``hint`` can only be
    detected here. For ``http``/``api`` this is a pure no-op: it must not clobber a latched
    ``fault``/``healthy`` driven by the probe (the server's mode is fixed for its lifetime).
    """
    global _state, _severity, _reason, _evidence
    if config.EMBEDDING_MODE == "none":
        if _state != HINT:
            _state = HINT
            _severity = "hint"
            _reason = "embeddings disabled (mode=none); recall is keyword/FTS-only"
            _evidence = "mode=none / no embedding backend configured"
    # http/api: no-op — the probe drives healthy/fault.


def observe_ok() -> None:
    """Record a successful embed: clear any degraded state and re-arm the full template."""
    global _state, _severity, _reason, _evidence, _consecutive_failures, _advisory_emitted
    _state = HEALTHY
    _severity = None
    _reason = None
    _evidence = None
    _consecutive_failures = 0
    _advisory_emitted = False


def observe_failure(evidence: str | None) -> None:
    """Record a confirmed probe failure; promote to ``fault`` after the threshold.

    A single failure is debounced (state unchanged) so a transient blip does not raise a
    fault advisory. The Nth consecutive failure latches ``fault`` and the advisory fires on
    the next recall. Keeps the last good evidence string if a later call passes ``None``.
    """
    global _state, _severity, _reason, _evidence, _consecutive_failures, _advisory_emitted
    _consecutive_failures += 1
    if _consecutive_failures >= FAULT_PROMOTE_THRESHOLD:
        if _state != FAULT:
            # fresh promotion from unknown/healthy — arm the full template
            _advisory_emitted = False
        _state = FAULT
        _severity = "fault"
        _reason = "embedding endpoint unreachable; recall fell back to keyword/FTS-only"
        _evidence = evidence or _evidence


def is_faulted() -> bool:
    """True once latched into ``fault`` — ``vector.py`` uses this to stop re-probing."""
    return _state == FAULT


def maybe_advisory() -> dict | None:
    """Return the advisory payload to attach to a recall response, or ``None``.

    ``None`` when opted out, or when the state is silent (``unknown``/``healthy``). The full
    runbook fires once per degraded episode; subsequent calls return the short reminder.
    """
    global _advisory_emitted
    if not config.DEGRADED_ADVISORY_ENABLED:
        return None
    if _state in (UNKNOWN, HEALTHY):
        return None
    full = not _advisory_emitted
    _advisory_emitted = True
    return _build_payload(full)


def _build_payload(full: bool) -> dict:
    """Build the ``{degraded, severity, reason, evidence, runbook}`` advisory struct."""
    evidence = _evidence or "no detail captured"
    if _severity == "hint":
        runbook = HINT_RUNBOOK_FULL if full else HINT_RUNBOOK_SHORT
    else:
        template = FAULT_RUNBOOK_FULL if full else FAULT_RUNBOOK_SHORT
        runbook = template.format(evidence=evidence)
    return {
        "degraded": True,
        "severity": _severity,
        "reason": _reason,
        "evidence": _evidence,
        "runbook": runbook,
    }


def _reset() -> None:
    """Test-only: restore all module state to its initial values."""
    global _state, _severity, _reason, _evidence, _consecutive_failures, _advisory_emitted
    _state = UNKNOWN
    _severity = None
    _reason = None
    _evidence = None
    _consecutive_failures = 0
    _advisory_emitted = False
