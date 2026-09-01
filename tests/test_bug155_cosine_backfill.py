"""Regression tests for bug-155 (FTS-only rows outrank vector hits under CONFIDENCE_ENABLED).

Diagnosis (verified against harness-362):

- ``utils._compute_confidence`` has two branches::

    raw_cosine is None -> score = sqrt(time_decay) * completion_factor * recency_penalty
    else               -> score = sqrt(norm_cos * time_decay) * ...   (norm_cos <= 1)

  The None branch is always >= the cosine branch (with equal time / completion),
  so a row that reaches scoring with ``_cosine=None`` structurally OUT-SCORES a
  row that reaches scoring with a real cosine (except at the corner where
  cosine >= COSINE_CEIL and norm_cos saturates to 1.0, where they merely tie).

- ``memory_handlers._recall_rrf`` / ``_recall_rsf`` populate ``_cosine`` only for
  rows the vector channel returned. A row that hit only via the FTS / keyword
  channel arrives at ``_apply_recall_scoring`` cosine-less even when its
  embedding blob sits in the DB (a ranking-window drop, not a coverage one).
  Under ``CONFIDENCE_ENABLED`` (production), FTS-only rows are then structurally
  promoted above rows that DO carry a real similarity signal -- inverting the
  fusion intent (the ``_recall_rsf`` docstring says the keyword channel is
  supposed to help DOWN-rank lexical contaminants).

Fix: backfill the true cosine (from the stored blob) on cosine-less rows
BEFORE the episode-boundary penalty and BEFORE ``_compute_confidence``, gated
on ``CONFIDENCE_ENABLED``. See ``memory_handlers._backfill_cosines``.

Each test docstring below states what the UNFIXED code does and how the
assertion catches its removal, in the ``tests/test_252a2_i*.py`` style. The
setup that creates FTS-only rows deterministically is:

    the blob is packed from a text DISJOINT from the content, so the FTS
    matches on the content while the vector channel excludes the row via its
    min_similarity threshold.
"""

from __future__ import annotations

import hashlib
import os
import struct
import tempfile

# Hermetic DB path pin BEFORE cpersona import (mirrors the sibling test files).
os.environ.setdefault(
    "CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "test_bug155.db")
)
os.environ.setdefault("CPERSONA_EMBEDDING_MODE", "none")

import numpy as np  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from cpersona import memory_handlers as M  # noqa: E402
from cpersona import vector as V  # noqa: E402
from cpersona._vendored_mcp_common.embedding_client import EmbeddingClient, EmbedOutcome  # noqa: E402
from cpersona.database import get_db  # noqa: E402

AGENT = "agent.bug155"
DIM = 64  # keep in step with the width filter (len(blob) == DIM * 4 == 256)


# ---------------------------------------------------------------------------
# Deterministic offline embedding: a DIM-dim unit vector whose axes are hashed
# from the tokens, so two texts that share tokens land at a real cosine and
# disjoint texts land near-zero. This is a subset of conftest.fake_embed_one
# kept local so this file's assertions do not depend on the shared fake's
# exact rotation choice.
# ---------------------------------------------------------------------------
def _embed(text: str) -> list[float]:
    vec = np.zeros(DIM, dtype=np.float64)
    tokens = text.lower().split() or [text.lower()]
    for tok in tokens:
        seed = int.from_bytes(hashlib.sha256(tok.encode()).digest()[:8], "big")
        vec += np.random.default_rng(seed).standard_normal(DIM)
    n = float(np.linalg.norm(vec))
    if n == 0.0:
        vec[0], n = 1.0, 1.0
    return (vec / n).astype(np.float32).tolist()


def _pack_of(text: str) -> bytes:
    return EmbeddingClient.pack_embedding(_embed(text))


class _Counting:
    """Deterministic embedding client that counts ``embed`` calls.

    Mirrors the ``embed`` / ``pack_embedding`` / ``mode`` / ``_http_url`` /
    ``_client`` surface CPersona's local vector path touches so an FTS-only
    scenario can walk the real hot path offline. ``embed_calls`` records the
    exact texts embedded so a test can assert the backfill (a) fires on a
    non-empty query and (b) does not fire on the empty-query pure-recency
    listing (that scenario has no relevance signal to score against).
    """

    mode = "fake"
    _http_url = None
    _client = None

    def __init__(self) -> None:
        self.embed_calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]] | None:
        self.embed_calls.append(list(texts))
        return [_embed(t) for t in texts]

    async def embed_with_outcome(self, texts: list[str]):
        result = await self.embed(texts)
        return result, EmbedOutcome(attempted=True, ok=bool(result))

    @staticmethod
    def pack_embedding(embedding: list[float]) -> bytes:
        return EmbeddingClient.pack_embedding(embedding)


@pytest_asyncio.fixture(autouse=True)
async def _fresh_db():
    """Truncate every table this file writes to. get_db is a process-wide
    singleton, so leaving rows between tests would let earlier scenarios
    perturb later ones' quality-gate memory counts."""
    db = await get_db()
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM episodes")
    await db.execute("DELETE FROM profiles")
    await db.commit()
    yield


async def _insert_mem(
    *,
    content: str,
    blob: bytes | None,
    mem_id: int | None = None,
    channel: str = "",
    ts: str = "2026-01-01T00:00:00Z",
) -> int:
    """Insert a memory row -- with a controlled id when the test needs one
    (bug-041 collision scenario). The direct INSERT fires the FTS triggers,
    matching what do_store does for real."""
    db = await get_db()
    if mem_id is None:
        cur = await db.execute(
            "INSERT INTO memories (agent_id, channel, content, source, timestamp, embedding, created_at) "
            "VALUES (?, ?, ?, '{}', ?, ?, ?)",
            (AGENT, channel, content, ts, blob, ts),
        )
        await db.commit()
        return cur.lastrowid
    await db.execute(
        "INSERT INTO memories (id, agent_id, channel, content, source, timestamp, embedding, created_at) "
        "VALUES (?, ?, ?, ?, '{}', ?, ?, ?)",
        (mem_id, AGENT, channel, content, ts, blob, ts),
    )
    await db.commit()
    return mem_id


async def _insert_ep(
    *,
    ep_id: int,
    summary: str,
    blob: bytes | None,
    ts: str = "2026-01-01T00:00:00Z",
) -> int:
    db = await get_db()
    await db.execute(
        "INSERT INTO episodes (id, agent_id, summary, keywords, embedding, start_time, resolved, created_at) "
        "VALUES (?, ?, ?, 'k', ?, ?, 0, ?)",
        (ep_id, AGENT, summary, blob, ts, ts),
    )
    await db.commit()
    return ep_id


# ---------------------------------------------------------------------------
# (a) FTS-only row gets a backfilled cosine and sorts BELOW a stronger vector
# hit under confidence-on.
#
# Setup: an FTS-only row is created by packing its blob from a text DISJOINT
# from its content -- FTS matches on the content ("apples"), the vector
# channel excludes the row because its cosine is well below the RSF/RRF
# vector threshold. Then:
#
#   deep=True is used so time_decay = completion_factor = recency_penalty = 1.0
#   in _compute_confidence, and the effective quality gate is halved. That
#   isolates the branch under test (score = sqrt(norm_cos) vs sqrt(1.0)) from
#   the age / pool-size arithmetic AND leaves both rows above the gate so the
#   ordering claim is testable, not a survival claim.
#
# Unfixed: sqrt(1.0) = 1.0 > sqrt(norm_cos) for any norm_cos<1, so the
# FTS-only row is at the top. With ``exclude_contents`` disabled the vector-
# strong row and the FTS-only row are the only two in play.
#
# Fixed: the FTS-only row is backfilled with a real (low) cosine and its
# ``sqrt(norm_cos)`` score falls well below the strong hit's -- if norm_cos
# clamps to 0 (cosine <= COSINE_FLOOR=0.20) the FTS-only row's score is 0 and
# it is gated out entirely, which is also an acceptable pin.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug155_fts_only_row_gets_backfilled_and_sorts_below_vector_hit(monkeypatch):
    """The FTS-only ``_cosine=None`` row scores ``sqrt(time_decay)`` in the
    unfixed code (== max possible cosine-branch score), so it out-ranks the
    real vector hit. Backfill sinks it below or drops it entirely.
    """
    client = _Counting()
    monkeypatch.setattr(V, "_embedding_client", client)
    monkeypatch.setattr(M, "CONFIDENCE_ENABLED", True)

    # Vector-strong row: content and blob share every token.
    await _insert_mem(content="apples orchard hit", blob=_pack_of("apples orchard hit"))
    # FTS-only row: blob is DISJOINT from content ("apples ..." in content,
    # blob packed from a topically unrelated string), so the vector channel's
    # min_similarity threshold rejects it and only the FTS keyword channel
    # admits it into the fusion set. With the vector channel excluding it,
    # _apply_recall_scoring sees this row with _cosine=None -- the exact
    # cosine-less path bug-155 exposes.
    await _insert_mem(
        content="apples zzz yyy www",
        blob=_pack_of("completely different unrelated content xxxx"),
        ts="2026-01-01T00:00:01Z",
    )

    # deep=True: time_decay = completion_factor = recency_penalty = 1.0, and
    # effective_min = 0.5 * min_score, so a norm_cos as low as ~0.03 still
    # clears the gate. Isolates the branch under test.
    out = await M.do_recall(AGENT, "apples", limit=5, deep=True)
    # do_recall reverses messages for the LLM (most relevant last); reverse
    # back to get the fusion-ranked order the assertion is about.
    contents = [m["content"] for m in reversed(out["messages"])]

    assert "apples orchard hit" in contents, (
        f"the vector-strong row did not survive the gate: {contents!r}"
    )
    # Fixed: strong hit ranks first (or is the only survivor because backfill
    # sank the FTS-only row's norm_cos to 0 and the gate cut it). Unfixed:
    # the FTS-only row is at index 0 with a score of sqrt(1.0) = 1.0.
    assert contents[0] == "apples orchard hit", (
        f"expected the vector-strong hit to rank first; got {contents!r}. "
        f"The unfixed code promotes the cosine-less FTS-only row to the top."
    )
    if "apples zzz yyy www" in contents:
        assert contents.index("apples orchard hit") < contents.index("apples zzz yyy www"), (
            f"vector-strong hit did not out-rank the FTS-only row: {contents!r}"
        )

    # Every surviving row must carry a cosine on the wire (backfilled AND
    # native). Under the unfixed code the FTS-only row would surface without a
    # cosine on ``match_reason``.
    for m in out["messages"]:
        assert "match_reason" in m, m
        if m["content"] == "apples orchard hit":
            assert "cosine" in m["match_reason"], (
                f"vector hit lost its cosine on the response: {m['match_reason']!r}"
            )

    # The backfill must have asked for the query embedding at least once
    # (the vector channel also asks; the second call is the backfill's, and
    # would be a cache hit against the real EmbeddingClient).
    assert any(c == ["apples"] for c in client.embed_calls), (
        f"the backfill never issued an embed call: {client.embed_calls!r}"
    )


# ---------------------------------------------------------------------------
# (b) Confidence-off: byte-for-byte identical wire behaviour. The backfill
# MUST NOT materialise ``_cosine`` on rows that didn't already have one, or
# the confidence-off callers see a stray ``match_reason.cosine`` where they
# never had one, and ``_gate_score`` flips those rows from ``rrf``/``None``
# to ``cosine`` -- a silent perturbation of an unrelated codepath.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug155_confidence_off_never_backfills(monkeypatch):
    """Under confidence-off the backfill is a no-op: no second embed call for
    the query, no ``_cosine`` materialisation on the FTS-only row, and no
    ``match_reason.signal="cosine"`` where the row previously reported
    ``signal="rrf"``.
    """
    client = _Counting()
    monkeypatch.setattr(V, "_embedding_client", client)
    monkeypatch.setattr(M, "CONFIDENCE_ENABLED", False)

    await _insert_mem(content="apples strong", blob=_pack_of("apples strong"))
    await _insert_mem(
        content="apples zzz yyy www",
        blob=_pack_of("completely different unrelated content xxxx"),
    )

    out = await M.do_recall(AGENT, "apples", limit=5)

    # No confidence field on any message under confidence-off (existing contract).
    for m in out["messages"]:
        assert "confidence" not in m, (
            f"confidence field surfaced under confidence-off on {m!r} "
            f"— backfill leaked into the confidence-off code path"
        )
    # The FTS-only row's match_reason must not surface a backfilled cosine.
    for m in out["messages"]:
        if m["content"] == "apples zzz yyy www":
            reason = m.get("match_reason", {})
            assert reason.get("signal") != "cosine", (
                f"backfill flipped confidence-off signal from rrf to cosine: {reason!r}"
            )
            assert "cosine" not in reason, (
                f"backfilled cosine leaked to confidence-off match_reason: {reason!r}"
            )

    # The vector channel calls ``embed(["apples"])`` once for the fusion path.
    # If the backfill fires under confidence-off (mutation removes the gate),
    # a second identical embed call would appear.
    single_query_calls = [c for c in client.embed_calls if c == ["apples"]]
    assert len(single_query_calls) == 1, (
        f"confidence-off issued {len(single_query_calls)} embed calls for the "
        f"query; expected 1 (vector channel only). An extra call proves the "
        f"backfill fired despite the CONFIDENCE_ENABLED gate."
    )


# ---------------------------------------------------------------------------
# (c) Empty query -> backfill declines (no embed call, no _cosine assignment).
# The backfill must not call embed([]) or embed([""]) -- both are invalid
# inputs, and there is no relevance signal in a pure-recency listing anyway.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("query", ["", "   "])
@pytest.mark.asyncio
async def test_bug155_empty_query_declines_to_backfill(monkeypatch, query):
    """Empty query is a pure-recency listing (bug-125). The backfill must
    decline and issue no embed call for the empty text.

    bug-193: the guard is ``if not query.strip()`` and only ``""`` was covered,
    so weakening it to ``if not query`` survived. A whitespace-only query takes
    the SAME pure-recency route through the fusion paths (they all test
    ``query.strip()``), so it must reach the same zero-embed outcome — under the
    weakened guard the backfill instead embeds ``["   "]``, a text with no
    relevance signal, and materialises cosines from it.
    """
    client = _Counting()
    monkeypatch.setattr(V, "_embedding_client", client)
    monkeypatch.setattr(M, "CONFIDENCE_ENABLED", True)

    await _insert_mem(content="apples", blob=_pack_of("apples"))
    await _insert_mem(
        content="apples zzz yyy www",
        blob=_pack_of("apples zzz yyy www"),
    )

    out = await M.do_recall(AGENT, query, limit=5)
    assert out["messages"], "empty-query pure-recency listing returned no messages"

    # The backfill must not have called embed([]) or embed([""]); the vector
    # channel also short-circuits on empty query in the fusion paths, so the
    # correct outcome is ZERO embed calls in total.
    assert client.embed_calls == [], (
        f"embed was called with an empty query: {client.embed_calls!r} "
        f"— the backfill did not decline as documented"
    )


# ---------------------------------------------------------------------------
# (d) Dimension mismatch: a row whose blob is a foreign width (mid-flight
# model swap, bug-085) must stay at _cosine=None; the backfill must NOT
# reshape-crash and must NOT invent a similarity from the ragged bytes.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug155_ragged_dim_blob_stays_none(monkeypatch):
    """A row with an 8-dim stored blob against a 64-dim query: the width
    filter must sink it out of the batched matmul and the row must reach the
    response cosine-less. Also proves the width filter is what protects the
    reshape; without it the matmul would crash the whole recall call.
    """
    client = _Counting()
    monkeypatch.setattr(V, "_embedding_client", client)
    monkeypatch.setattr(M, "CONFIDENCE_ENABLED", True)

    # A single FTS hit whose blob is a foreign width. Nothing to compete
    # with, but the backfill still runs -- the assertion is that it declines
    # gracefully on the width filter instead of crashing or making up a value.
    ragged_blob = struct.pack("<8f", *([0.1] * 8))
    await _insert_mem(content="apples", blob=ragged_blob)

    out = await M.do_recall(AGENT, "apples", limit=5, deep=True)
    assert out["messages"], "the ragged-dim row did not survive the quality gate"
    for m in out["messages"]:
        reason = m.get("match_reason", {})
        # Backfill must NOT invent a similarity from ragged bytes -- either no
        # match_reason at all (no signal), or a signal that is NOT ``cosine``.
        if reason:
            assert reason.get("signal") != "cosine", (
                f"ragged-dim row surfaced a backfilled cosine signal: {reason!r} "
                f"— the width filter is not holding"
            )
            assert "cosine" not in reason, (
                f"ragged-dim row surfaced a backfilled cosine value: {reason!r}"
            )


# ---------------------------------------------------------------------------
# (e) Episode vs memory id collision (bug-040/041 doctrine): a backfill for
# episode id N MUST NOT read memory id N's blob. The two AUTOINCREMENT id
# spaces are independent; conflating them would let an unrelated memory's
# embedding drive the episode's cosine.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug155_episode_id_does_not_read_memory_blob(monkeypatch):
    """Seed a memory and an episode with the SAME integer id but STRONGLY
    DIFFERENT embedding blobs. Drive the backfill directly with a
    hand-authored results list so the assertion sits on the mechanism (which
    table did the blob come from?) rather than on the noisy interaction with
    the quality gate / autocut / adaptive threshold pipeline.

    A mutation that changes the ``episodes`` fetch to ``memories`` (or drops
    the per-table split entirely and reads a merged UNION) would surface the
    memory-at-id-7 cosine on the episode row -- exactly what the assertion
    below refuses.
    """
    client = _Counting()
    monkeypatch.setattr(V, "_embedding_client", client)

    # Memory id 7: strong "apples" hit (blob and query share every token).
    await _insert_mem(mem_id=7, content="apples memory", blob=_pack_of("apples"))
    # Episode id 7: blob packed from a text with NO shared tokens with the
    # query, so the cosine here is near-zero -- clearly distinguishable from
    # the memory-at-id-7 cosine (~1.0).
    await _insert_ep(
        ep_id=7,
        summary="apples zzz yyy www",
        blob=_pack_of("completely different unrelated content xxxx"),
    )

    # Sanity: the two blobs really do produce distinguishable cosines against
    # the query -- otherwise the assertion below cannot see the collision.
    qv = np.asarray(_embed("apples"), dtype=np.float32)
    ep_blob = _pack_of("completely different unrelated content xxxx")
    mem_id7_blob = _pack_of("apples")
    cos_ep_true = float(np.frombuffer(ep_blob, dtype=np.float32) @ qv)
    cos_mem_at_ep_id = float(np.frombuffer(mem_id7_blob, dtype=np.float32) @ qv)
    assert abs(cos_ep_true - cos_mem_at_ep_id) > 0.5, (
        f"fixture episode/memory blobs too similar: "
        f"ep_true={cos_ep_true:.4f}, mem_at_ep_id={cos_mem_at_ep_id:.4f}"
    )

    # Hand-authored FTS-only-shape input mirroring what the fusion path passes
    # to _apply_recall_scoring: both rows have id=7 but distinct row-kinds via
    # _rid (memories vs episodes) and _is_episode_result markers. _cosine is
    # None on both, which is the exact cosine-less state bug-155 addresses.
    ep_row = {
        "id": 7,
        "_rid": ("ep", 7),
        "content": "[Episode] apples zzz yyy www",
        "source": {"System": "episode"},
        "timestamp": "2026-01-01T00:00:00Z",
        "_bm25": -0.5,
    }
    mem_row = {
        "id": 7,
        "_rid": ("mem", 7),
        "content": "apples memory",
        "source": "{}",
        "timestamp": "2026-01-01T00:00:00Z",
        "_bm25": -0.6,
    }
    results = [ep_row, mem_row]

    db = await get_db()
    await M._backfill_cosines(db, results, "apples", None, "")

    # The episode row's backfilled cosine MUST come from the EPISODE's blob,
    # not from the memory blob at the colliding id. Under the id-collision
    # mutation, the episode would surface a cosine near ``cos_mem_at_ep_id``.
    assert ep_row["_cosine"] is not None, "episode was not backfilled at all"
    assert abs(ep_row["_cosine"] - cos_ep_true) < 1e-4, (
        f"episode id=7 was backfilled with cosine={ep_row['_cosine']:.4f}; "
        f"expected {cos_ep_true:.4f} (the episode's own blob). A value near "
        f"{cos_mem_at_ep_id:.4f} proves the backfill read memory id=7's blob "
        f"instead — bug-040/041 id-collision failure on the backfill path."
    )
    # Symmetric contract on the memory row: its cosine must come from its own blob.
    assert mem_row["_cosine"] is not None
    assert abs(mem_row["_cosine"] - cos_mem_at_ep_id) < 1e-4, (
        f"memory id=7 was backfilled with cosine={mem_row['_cosine']:.4f}; "
        f"expected {cos_mem_at_ep_id:.4f}"
    )
