"""Which backend produced a stored vector.

Every derived vector this server keeps — a node's, a block's — sits next to a key
naming what embedded it, and every "is this set still current" question compares
that key against the current one. Over HTTP the name never crossed the wire: the
request carries texts and the backend picks the model. So the key this server
wrote was its own configured default, a constant that does not move when the
model does, and a model swap left every derived vector reading as current.

The embedding server can now be asked (``/capabilities``). When it answers with a
fingerprint, that is the key new rows carry, and a swap moves it.

What this deliberately does not do is invalidate what is already stored. A row
written before this server could ask carries no evidence about which model made
it — that evidence does not exist anywhere — so declaring those rows stale would
not be a discovery, it would be a decision to re-embed the whole corpus on a
suspicion. Instead both keys are accepted: the current one, and the one this
deployment would have written before it could ask. Rows gain the fingerprint as
they are rebuilt for their own reasons.

The residue, stated plainly: for a row still carrying the legacy key, a model
swap is as invisible as it was before this module existed. That is not fixed
here. Fixing it means rewriting those rows once, under the judgement that they
did come from the current backend, and that is a decision about a production
database rather than a thing to infer.
"""

import logging
import time

from cpersona import config

logger = logging.getLogger(__name__)

#: How long a learned identity is reused before the backend is asked again. The
#: backend can be redeployed under the same URL, which is exactly what this is
#: for, so the answer is not kept for the life of the process; it is also not
#: asked per write, which would put a round trip in front of every record.
REFRESH_INTERVAL_SECONDS = 300

#: How soon a backend is asked again after it could not say what it is, while a
#: fingerprint learned earlier is being kept. A failed ask is usually a restart,
#: and the answer that ends it should not wait out the whole interval.
RETRY_UNKNOWN_SECONDS = 30

#: (when it was learned, what was learned). ``None`` for the second element means
#: no fingerprint has been learned: asked and not answered, which is *unknown*,
#: never *unchanged*. Once one has been learned, an ask that cannot name the
#: backend keeps it (see :func:`refresh`).
_learned: tuple[float, str | None] | None = None
#: When an unanswered ask is retried while a learned fingerprint is kept, or None.
_retry_at: float | None = None
#: The model name the backend gave in its last capability report, or None. A label for
#: saying what produced a result, never a key for deciding whether vectors compare.
_reported_model: str | None = None


def reset() -> None:
    """Forget what was learned. For tests and for a client being replaced."""
    global _learned, _reported_model, _retry_at
    _learned = None
    _reported_model = None
    _retry_at = None


def fingerprint() -> str | None:
    """The backend identity learned so far, or None.

    A pure read: whoever is on an async path calls :func:`refresh` first. None
    means this server could not establish what is behind ``/embed`` — not that
    nothing changed.
    """
    return _learned[1] if _learned else None


async def refresh(client=None) -> str | None:
    """Ask the backend what it is, at most once per :data:`REFRESH_INTERVAL_SECONDS`.

    Returns the fingerprint, or None when the backend has none to give: a client
    that is not configured, a mode with no such route, a server predating the
    report, a failed request, or a backend that could not establish its own whole
    identity. Every one of those is unknown, and every one of them leaves this
    server writing the key it wrote before.

    Which, once a fingerprint has been learned, is that fingerprint: an ask that
    cannot name the backend keeps it, and the backend is asked again after
    :data:`RETRY_UNKNOWN_SECONDS` rather than after the whole interval. Dropping
    it moved the keys to the legacy pair for the interval, so every set stamped
    with it read as stale, the block arm stopped examining its rows, and a build in
    that window embedded it again under the legacy key — one failed request,
    typically a restart, treated as a change. Only a fingerprint the backend
    reports moves the key.
    """
    global _learned, _reported_model, _retry_at
    now = time.monotonic()
    if _learned and now - _learned[0] < REFRESH_INTERVAL_SECONDS and (_retry_at is None or now < _retry_at):
        return _learned[1]

    if client is None:
        from cpersona import vector

        client = vector._embedding_client
    if client is None or not callable(getattr(client, "capabilities_with_outcome", None)):
        _reported_model = None
        return _unknown(now, None)

    identity, outcome = await client.capabilities_with_outcome()
    # A report that could not complete its identity can still name its model.
    model = identity.fields.get("model") if identity is not None else None
    _reported_model = model if isinstance(model, str) and model else None
    # An identity the backend could not complete carries no fingerprint by
    # construction, so this reads the field rather than re-deriving the rule.
    learned = identity.fingerprint if identity is not None else None
    if learned is None:
        return _unknown(now, outcome.error)
    if learned != fingerprint():
        # Worth a line: gaining one means new rows become checkable, and a
        # different one means the backend behind the URL changed.
        logger.info(
            "Embedding backend identity: %s (was %s)", learned, fingerprint() or "unknown"
        )
    _learned = (now, learned)
    _retry_at = None
    return learned


def _unknown(now: float, error: str | None) -> str | None:
    """Record an ask that could not name the backend; return the fingerprint kept."""
    global _learned, _retry_at
    kept = fingerprint()
    if kept is None:
        _learned = (now, None)
        return None
    if _retry_at is None:
        logger.info(
            "Embedding backend identity: unknown%s; keeping %s until it answers",
            f" — {error}" if error else "",
            kept,
        )
    _learned = (now, kept)
    _retry_at = now + RETRY_UNKNOWN_SECONDS
    return kept


def reported_model() -> str | None:
    """The model name the backend gave in its last capability report, or None."""
    return _reported_model


def trace_model() -> str:
    """The embedding model a recall trace names: the one the backend reported, else
    the one this process can vouch for (:func:`config.reported_embedding_model`), else
    empty -- not known here. Over HTTP the configured default never reaches the
    backend, so it says nothing about the model that answered and is not reported."""
    return reported_model() or config.reported_embedding_model()


def keys(legacy: str) -> tuple[str, str]:
    """``(key to write, key a row written before this server could ask carries)``.

    ``legacy`` is what this deployment's configuration alone says the model is —
    which differs between the tables that store it, and is kept per table rather
    than unified: changing what a table's legacy key is would make every row
    already in it read as stale, which is the outcome this module exists to avoid.

    The two are equal when no fingerprint is known, so a deployment that cannot
    ask behaves exactly as it did before. They are also both real values: the
    legacy key stays tied to the configuration, so an operator who changes
    ``CPERSONA_EMBEDDING_MODEL`` still invalidates the rows that named the old one.
    """
    return (fingerprint() or legacy, legacy)


def node_keys() -> tuple[str, str]:
    """:func:`keys` for ``record_nodes``, whose legacy key is the resolved model name."""
    return keys(config.EMBEDDING_MODEL)


def block_keys() -> tuple[str, str]:
    """:func:`keys` for ``record_blocks``, whose legacy key is empty when unreported.

    Empty compares equal to empty and never to a named model, which is the rule
    ``blocks`` already kept before there was anything better to compare.
    """
    return keys(config.reported_embedding_model())


def record_keys() -> tuple[str, str]:
    """:func:`keys` for the ``embedding_model`` label of ``memories`` and ``episodes``.

    The legacy half is the reported name, as for blocks, and not the resolved one
    nodes keep: over HTTP the resolved name is a default this process never sends,
    and a label written from it would name an OpenAI model on a corpus some other
    backend embedded. Empty says "not known here", which is what is true.
    """
    return keys(config.reported_embedding_model())


async def record_label() -> str:
    """The label a memory or episode vector is written with, learned just before it is.

    What the backend reported when last asked, else the name this process sends or
    was configured with, else empty. It is a declaration and nothing checks it: the
    vector it sits beside may come from the embedding client's cache, and a backend
    can be swapped between this call and the request that embeds.
    """
    await refresh()
    return record_keys()[0]


def current_identity() -> str | None:
    """What the vectors this server writes now come from, when that is known.

    Over HTTP only a fingerprint answers it: a configured name never reaches the
    backend, so it cannot say what answered. The ``api`` transport sends its model
    with every request, so there the model is the identity. Anything else is None —
    unknown, and never a reason to call a stored label different.
    """
    learned = fingerprint()
    if learned:
        return learned
    if config.EMBEDDING_MODE == "api":
        return config.EMBEDDING_MODEL
    return None


def accepted_labels() -> tuple[str, ...] | None:
    """The labels a stored vector may carry and still be compared, or None.

    None when there is no current identity to compare against — then no label is
    judged, in either direction. Otherwise empty (stored before labels existed,
    or by a backend nobody could name), the current key, and the key this
    deployment wrote before it could ask. A label outside the set was written
    under a different model.
    """
    if current_identity() is None:
        return None
    return tuple(dict.fromkeys(("", *record_keys())))
