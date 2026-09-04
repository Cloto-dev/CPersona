"""What may become a stored vector, and what is refused at the storage seam.

A vector that reaches a BLOB is not recoverable by reading the row back. A NaN
scores against every query, and because the similarity floor is a ``<``
comparison a NaN does not fall below it — the bad row stays and pushes good rows
out. ``vector.pack_for_storage`` is the last point where that can be stopped, so
these tests pin what it lets through.

The companion to this file is Gate 19 in ``test_structural_gates.py``, which
asserts that no caller packs a vector without coming through here. The two are
different questions: that one is about reach, this one about judgement.
"""

import math
import struct

import pytest
import pytest_asyncio

from cpersona import session, vector
from cpersona._vendored_mcp_common.embedding_client import EmbeddingClient, EmbedOutcome
from cpersona.database import get_db

DIM = 8


def _vec(width=DIM, fill=0.5):
    return [fill] * width


# ---------------------------------------------------------------------------
# Accepted
# ---------------------------------------------------------------------------


def test_a_valid_vector_round_trips_through_the_seam():
    blob = vector.pack_for_storage(_vec())
    assert blob is not None
    assert EmbeddingClient.unpack_embedding(blob) == pytest.approx(_vec())


def test_the_seam_packs_exactly_what_the_client_would_have():
    """The refactor must not change the bytes any existing row would have got."""
    assert vector.pack_for_storage(_vec()) == EmbeddingClient.pack_embedding(_vec())


def test_a_one_element_vector_is_accepted():
    assert vector.pack_for_storage([0.25]) is not None


def test_integer_elements_are_accepted_as_numbers():
    """JSON has one number type; a backend may serialise 0.0 as 0."""
    blob = vector.pack_for_storage([0, 1, -1] + [0] * (DIM - 3))
    assert EmbeddingClient.unpack_embedding(blob) == pytest.approx([0.0, 1.0, -1.0] + [0.0] * (DIM - 3))


def test_a_tuple_is_accepted():
    assert vector.pack_for_storage(tuple(_vec())) == EmbeddingClient.pack_embedding(_vec())


def test_a_zero_vector_is_accepted():
    """Degenerate for search, but it is a number and not this seam's call to make."""
    assert vector.pack_for_storage([0.0] * DIM) is not None


# ---------------------------------------------------------------------------
# Refused — returned as None, which is every caller's existing "no embedding"
# branch, so the column stays NULL for the repair pass to retry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad", [float("nan"), float("inf"), float("-inf")], ids=["nan", "inf", "-inf"]
)
def test_a_non_finite_element_is_refused(bad):
    assert vector.pack_for_storage([bad] + _vec(DIM - 1)) is None


def test_a_vector_of_all_nan_is_refused():
    assert vector.pack_for_storage([float("nan")] * DIM) is None


def test_a_finite_but_unstorable_element_is_refused():
    """1e300 is a finite float64 and an infinite float32. The store is float32."""
    with pytest.raises(OverflowError):
        struct.pack("<f", 1e300)
    assert vector.pack_for_storage([1e300] + _vec(DIM - 1)) is None


def test_a_string_element_is_refused():
    assert vector.pack_for_storage(["0.5"] + _vec(DIM - 1)) is None


def test_a_bool_element_is_refused():
    """bool is a subclass of int, so a bare numeric check would store True as 1.0."""
    assert vector.pack_for_storage([True] + _vec(DIM - 1)) is None


def test_a_null_element_is_refused():
    assert vector.pack_for_storage([None] + _vec(DIM - 1)) is None


def test_an_empty_vector_is_refused():
    assert vector.pack_for_storage([]) is None


def test_a_non_sequence_is_refused():
    assert vector.pack_for_storage(None) is None
    assert vector.pack_for_storage("not a vector") is None
    assert vector.pack_for_storage({"vector": _vec()}) is None


def test_a_nested_vector_is_refused():
    assert vector.pack_for_storage([_vec()]) is None


# ---------------------------------------------------------------------------
# The end this is for
# ---------------------------------------------------------------------------


def test_nothing_the_seam_returns_unpacks_to_a_non_finite_number():
    """The invariant stated as one assertion, over every shape tried above."""
    candidates = [
        _vec(),
        [0.25],
        [0, 1, -1],
        [0.0] * DIM,
        [float("nan")] + _vec(DIM - 1),
        [float("inf")] * DIM,
        [float("-inf")] + _vec(DIM - 1),
        [1e300] + _vec(DIM - 1),
        ["0.5"] + _vec(DIM - 1),
        [True] + _vec(DIM - 1),
        [None],
        [],
        None,
    ]
    for candidate in candidates:
        blob = vector.pack_for_storage(candidate)
        if blob is None:
            continue
        assert all(math.isfinite(v) for v in EmbeddingClient.unpack_embedding(blob)), (
            f"the seam packed a non-finite value from {candidate!r}"
        )


@pytest_asyncio.fixture
async def clean_db():
    session.reset_pauses_for_tests()
    db = await get_db()
    await db.execute("DELETE FROM memories")
    await db.commit()
    return db


@pytest.mark.asyncio
async def test_a_backend_returning_nan_leaves_the_column_null(clean_db, monkeypatch):
    """End to end: a store against a backend that returns NaN writes no vector,
    rather than a row that outranks good ones for every query afterwards.

    The row itself is still written. A missing vector is a gap the repair pass in
    check_health fills later; a NaN vector is not a gap and nothing would ever
    come back for it.
    """
    from cpersona import memory_handlers

    class NaNClient:
        mode = "fake"
        _http_url = None
        _client = None

        async def embed(self, texts):
            return [[float("nan")] * DIM for _ in texts]

        async def embed_with_outcome(self, texts):
            return await self.embed(texts), EmbedOutcome(attempted=True, ok=True)

    monkeypatch.setattr(vector, "_embedding_client", NaNClient())

    result = await memory_handlers.do_store(
        "seam-test", {"content": "a memory whose backend went wrong"}
    )
    assert result["ok"] is True, result

    rows = await clean_db.execute_fetchall(
        "SELECT embedding FROM memories WHERE agent_id = 'seam-test'"
    )
    assert len(rows) == 1, rows
    assert rows[0][0] is None, "a NaN vector was written to the store"


@pytest.mark.asyncio
async def test_a_healthy_backend_still_writes_a_vector(clean_db, monkeypatch):
    """The control for the test above: without it, a seam that refused everything
    would pass just as well."""
    from cpersona import memory_handlers

    class GoodClient:
        mode = "fake"
        _http_url = None
        _client = None

        async def embed(self, texts):
            return [_vec() for _ in texts]

        async def embed_with_outcome(self, texts):
            return await self.embed(texts), EmbedOutcome(attempted=True, ok=True)

    monkeypatch.setattr(vector, "_embedding_client", GoodClient())

    result = await memory_handlers.do_store(
        "seam-test", {"content": "a memory whose backend is fine"}
    )
    assert result["ok"] is True, result

    rows = await clean_db.execute_fetchall(
        "SELECT embedding FROM memories WHERE agent_id = 'seam-test'"
    )
    assert len(rows) == 1, rows
    assert rows[0][0] is not None, "a good vector was not written"
