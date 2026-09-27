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

#: (when it was learned, what was learned). ``None`` for the second element means
#: asked and not answered — which is *unknown*, never *unchanged*.
_learned: tuple[float, str | None] | None = None


def reset() -> None:
    """Forget what was learned. For tests and for a client being replaced."""
    global _learned
    _learned = None


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
    """
    global _learned
    now = time.monotonic()
    if _learned and now - _learned[0] < REFRESH_INTERVAL_SECONDS:
        return _learned[1]

    if client is None:
        from cpersona import vector

        client = vector._embedding_client
    if client is None or not callable(getattr(client, "capabilities_with_outcome", None)):
        _learned = (now, None)
        return None

    identity, outcome = await client.capabilities_with_outcome()
    # An identity the backend could not complete carries no fingerprint by
    # construction, so this reads the field rather than re-deriving the rule.
    learned = identity.fingerprint if identity is not None else None
    if learned != fingerprint():
        # Worth a line either way: gaining one means new rows become checkable,
        # and losing one means they stop being.
        logger.info(
            "Embedding backend identity: %s (was %s)%s",
            learned or "unknown",
            fingerprint() or "unknown",
            f" — {outcome.error}" if learned is None and outcome.error else "",
        )
    _learned = (now, learned)
    return learned


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
