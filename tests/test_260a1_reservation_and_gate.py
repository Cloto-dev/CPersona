"""2.6 adaptive-fusion structural decisions: the reservation, the gate, the weight.

Three decisions ship together (docs/ADAPTIVE_FUSION_DESIGN.md sections D1, D2 and
D3-now), and they are separable only on paper — the measurement that chose the
shape of D2 did so by scoring it *with* D1 in place. So the file pins each one on
its own and then the pair together.

D1, the reservation. The dense arm's top rows are kept reachable across the
admission floor, and the answer is refilled from them — appended, marked
`fallback`, never scored — whenever it would otherwise be shorter than ten rows.
The defect it removes is not a ranking error: an absolute floor calibrated over a
large corpus emptied the top ten for a third of one benchmark task's queries, and
no fusion rule repairs an empty list.

D2, the gate. On a fused order the pool-size heuristic is not applied to any
fused row. Against a reciprocal-rank score it is a cut at a lexical *rank* that
moves with the pool size; against a dense row's cosine it is a second absolute
floor above the calibrated one the retriever already applied. Removing only the
first half was measured and is worse than removing neither, which is why the two
branches are one decision (frozen-stage replay, and the note in
`_apply_quality_gate`).

D3-now, the weight. `CPERSONA_RRF_LEXICAL_WEIGHT` scales the lexical arms' votes
and defaults to 1.0, where it is the same division the fusion has always
computed. It is the control arm of the comparison that decides the adaptive
layer, so the two things worth pinning are that the default changes nothing and
that the knob does something.

Fixture style follows `test_gate_remediation.py`: rows are inserted directly so a
row's stored vector can be chosen independently of its text, which is what makes
"below the floor" and "lexical-only" constructible at all.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio

from conftest import fake_embed_one

from cpersona import admin_handlers, config, memory_handlers as M, vector
from cpersona._vendored_mcp_common.embedding_client import EmbeddingClient
from cpersona.database import get_db

AGENT = "agent.260a1"


def _pack_of(text: str) -> bytes:
    return EmbeddingClient.pack_embedding(fake_embed_one(text))


async def _insert(content: str, blob_text: str | None = None, ts: str = "2026-05-01T00:00:00Z") -> int:
    """One memory row, with its vector chosen independently of its text."""
    db = await get_db()
    cur = await db.execute(
        "INSERT INTO memories (agent_id, content, embedding, timestamp, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (AGENT, content, _pack_of(blob_text if blob_text is not None else content), ts, ts),
    )
    await db.commit()
    return cur.lastrowid


def _reset_calibration_globals() -> None:
    vector._agent_thresholds.clear()
    vector._agent_fused_gates.clear()
    vector._global_fused_gate = None
    vector._fused_gate_signal = None
    vector._agent_betas.clear()
    config.VECTOR_MIN_SIMILARITY = 0.3


@pytest_asyncio.fixture(autouse=True)
async def _fresh_state(tmp_path, monkeypatch):
    """Truncate before AND after.

    The after half is not symmetry for its own sake: `get_db()` is one
    process-wide connection, so rows left behind are deleted by whichever file
    truncates next — and a delete of a row this file inserted, executed under
    another file's fixtures, is a write nothing in either file is reading.
    Cleaning up here keeps every row's whole life inside the test that made it.
    """
    db = await get_db()

    async def _truncate():
        for table in ("memories", "episodes", "profiles"):
            await db.execute(f"DELETE FROM {table}")
        await db.commit()

    await _truncate()
    _reset_calibration_globals()
    monkeypatch.setattr(
        admin_handlers,
        "_calibration_sidecar_path",
        lambda: os.path.join(str(tmp_path), "sidecar.calibration.json"),
    )
    yield
    await _truncate()
    _reset_calibration_globals()


def _qualified(result: dict) -> list[dict]:
    return [m for m in result["messages"] if not m.get("fallback")]


def _fallback(result: dict) -> list[dict]:
    return [m for m in result["messages"] if m.get("fallback")]


# ===========================================================================
# D1 — the reservation
# ===========================================================================


@pytest.mark.asyncio
async def test_reservation_fills_an_answer_the_floor_starved(fake_embedding_client):
    """A universe the floor empties still answers, and says the rows are fallback.

    Twelve rows sharing no token with the query, under a per-agent threshold that
    puts the floor at 0.45: the dense arm hands the fusion nothing and the lexical
    arm has no term to match. Before the reservation this returned an empty list —
    the shape the benchmark measured as a third of one task's queries.

    The threshold is set rather than inferred. The deterministic test embedder
    gives an unrelated pair anything from 0.02 to 0.18, so a fixture that leans on
    the default floor of 0.15 admits a row now and then and stops measuring
    starvation on that run.
    """
    vector._agent_thresholds[AGENT] = 0.9
    for index in range(12):
        await _insert(f"gardening notes {index} soil water sun")

    out = await M.do_recall(AGENT, "photolithography stepper reticle", limit=10)

    assert _qualified(out) == [], (
        f"a row cleared the floor, so this fixture is not measuring starvation: {out}"
    )
    assert len(_fallback(out)) == 10, (
        "the reservation did not fill the answer to ten rows; that is the cardinality "
        f"contract D1 exists to hold: {out}"
    )
    assert out["fallback_rows"] == 10, (
        f"the response did not report how many of its rows are fallback: {out}"
    )
    assert all(m.get("fallback") is True for m in out["messages"]), (
        f"a reservation row arrived unmarked, which reads as a hit: {out}"
    )
    # Dense order, and the response is last-is-best, so the strongest sits last.
    scores = [m["match_reason"]["cosine"] for m in out["messages"]]
    assert scores == sorted(scores), f"the reservation was not appended in dense order: {scores}"


@pytest.mark.asyncio
async def test_reservation_is_absent_when_the_answer_is_full(fake_embedding_client):
    """No starvation, no marker: the response shape is untouched in the common case."""
    for index in range(12):
        await _insert(f"reticle stepper alignment note {index}")

    out = await M.do_recall(AGENT, "reticle stepper alignment", limit=10)

    assert len(_qualified(out)) == 10, f"the fixture did not fill the answer on its own: {out}"
    assert "fallback_rows" not in out, (
        "`fallback_rows` appeared on a recall that needed no reservation. A key present "
        f"on every response is a shape change for every consumer: {out}"
    )
    assert not any("fallback" in m for m in out["messages"]), (
        f"a `fallback` marker leaked onto a qualified row: {out}"
    )


@pytest.mark.asyncio
async def test_reservation_never_precedes_a_qualified_row(fake_embedding_client):
    """Appended, not merged: a fallback row cannot take a qualified row's place.

    Reading the marker is a caller's job; not being handed a near-miss where the
    best answer belongs is the server's.
    """
    await _insert("reticle stepper alignment exact match")
    for index in range(9):
        await _insert(f"gardening notes {index} soil water sun")

    out = await M.do_recall(AGENT, "reticle stepper alignment", limit=10)

    assert len(_qualified(out)) == 1, f"the fixture needs exactly one qualified row: {out}"
    assert out["fallback_rows"] >= 1, f"the answer was not short, so nothing was appended: {out}"
    order = [bool(m.get("fallback")) for m in out["messages"]]
    assert order == sorted(order, reverse=True), (
        f"a reservation row was placed after a qualified one in last-is-best order: {out}"
    )


@pytest.mark.asyncio
async def test_reservation_stays_inside_the_caller_limit(fake_embedding_client):
    """The reservation is a floor inside the caller's ceiling, never above it."""
    vector._agent_thresholds[AGENT] = 0.9  # starve the dense arm; see the test above
    for index in range(12):
        await _insert(f"gardening notes {index} soil water sun")

    out = await M.do_recall(AGENT, "photolithography stepper reticle", limit=3)

    assert len(out["messages"]) == 3, (
        f"`limit` stopped bounding the response once the reservation could fill it: {out}"
    )
    assert out["fallback_rows"] == 3


@pytest.mark.asyncio
async def test_reservation_earns_no_recall_count_credit(fake_embedding_client, monkeypatch):
    """A fallback row is not a hit, so it does not raise its own future ranking.

    `recall_count` lifts the confidence floor, so crediting a row that came back
    only because nothing else filled the answer would let it drift upward until it
    starts qualifying on unrelated queries. Same rule the gate rescue follows.
    """
    monkeypatch.setattr(M, "CONFIDENCE_ENABLED", True)  # the bump only runs here
    vector._agent_thresholds[AGENT] = 0.9  # starve the dense arm; see the test above
    ids = [await _insert(f"gardening notes {index} soil water sun") for index in range(12)]

    out = await M.do_recall(AGENT, "photolithography stepper reticle", limit=10)
    assert out["fallback_rows"] == 10, f"nothing was appended, so nothing is measured: {out}"

    db = await get_db()
    rows = await db.execute_fetchall(
        f"SELECT id, recall_count FROM memories WHERE id IN ({','.join('?' * len(ids))})", ids
    )
    credited = [row[0] for row in rows if row[1]]
    assert credited == [], (
        f"reservation rows were credited with a recall: {credited}"
    )


@pytest.mark.asyncio
async def test_reservation_obeys_exclude_contents(fake_embedding_client):
    """A fallback row is still an answer, so the caller's exclusion list applies."""
    vector._agent_thresholds[AGENT] = 0.9  # starve the dense arm; see the test above
    for index in range(12):
        await _insert(f"gardening notes {index} soil water sun")

    plain = await M.do_recall(AGENT, "photolithography stepper reticle", limit=10)
    excluded_text = plain["messages"][-1]["content"]

    filtered = await M.do_recall(
        AGENT, "photolithography stepper reticle", limit=10, exclude_contents=[excluded_text]
    )

    assert excluded_text not in [m["content"] for m in filtered["messages"]], (
        "an excluded row came back through the reservation, so the caller is handed "
        f"text it told the server it already holds: {filtered}"
    )
    # Nine, not ten: the reservation holds ten candidates and an exclusion spends
    # one of them. It does not reach deeper into the corpus to replace it — the
    # reservation is bounded by construction, and "bounded" has to mean the bound
    # survives a filter.
    assert filtered["fallback_rows"] == 9, (
        f"the exclusion did not simply shorten the reservation: {filtered}"
    )


# ===========================================================================
# D2 — the pool-size heuristic on a fused order
# ===========================================================================


@pytest.mark.asyncio
async def test_pool_size_heuristic_does_not_gate_a_fused_row(fake_embedding_client):
    """A small pool no longer deletes rows the calibrated floor admitted.

    Eleven rows: the heuristic's threshold at that pool size is 0.4262, far above
    the admission floor of 0.15, so before this change every row scoring between
    the two was retrieved and then thrown away by a number nobody measured against
    this corpus.
    """
    await _insert("reticle stepper alignment note", blob_text="reticle stepper alignment note")
    # A partial match: shares one token of three, so its cosine lands between the
    # floor and the heuristic rather than above both.
    middling = await _insert("stepper motor maintenance log", blob_text="stepper motor maintenance log")
    for index in range(9):
        await _insert(f"gardening notes {index} soil water sun")

    out = await M.do_recall(AGENT, "reticle stepper alignment", limit=10)

    contents = [m["content"] for m in _qualified(out)]
    assert "stepper motor maintenance log" in contents, (
        "the middling row was cut, so the pool-size heuristic is gating fused rows "
        f"again: {out}"
    )
    scored = {m["content"]: m["match_reason"]["score"] for m in _qualified(out)}
    threshold = M._adaptive_min_score(11)
    assert scored["stepper motor maintenance log"] < threshold, (
        "the fixture stopped straddling the heuristic — this row now clears it on its "
        f"own, so the test would pass with the change reverted (score {scored} vs "
        f"threshold {threshold})"
    )
    assert middling  # the id is the fixture's, kept so the row is addressable


@pytest.mark.asyncio
async def test_pool_size_heuristic_still_gates_a_dense_only_order(
    fake_embedding_client, monkeypatch
):
    """Cascade keeps the heuristic: D2 is about fused orders, not about the gate."""
    monkeypatch.setattr(M, "RECALL_MODE", "cascade")
    await _insert("reticle stepper alignment note")
    await _insert("stepper motor maintenance log")
    for index in range(9):
        await _insert(f"gardening notes {index} soil water sun")

    out = await M.do_recall(AGENT, "reticle stepper alignment", limit=10)

    contents = [m["content"] for m in _qualified(out)]
    assert "stepper motor maintenance log" not in contents, (
        "a dense-only order stopped applying the pool-size heuristic, so D2 reached "
        f"further than the fused orders it was scoped to: {out}"
    )


@pytest.mark.asyncio
async def test_calibrated_gate_still_applies_under_fusion(fake_embedding_client, monkeypatch):
    """What replaces the heuristic is the operating point measured for this corpus."""
    await _insert("reticle stepper alignment note")
    await _insert("stepper motor maintenance log")
    for index in range(9):
        await _insert(f"gardening notes {index} soil water sun")

    probe = await M.do_recall(AGENT, "reticle stepper alignment", limit=10)
    weakest = min(m["match_reason"]["score"] for m in _qualified(probe))
    strongest = max(m["match_reason"]["score"] for m in _qualified(probe))
    assert weakest < strongest, f"the fixture has nothing for a gate to separate: {probe}"

    monkeypatch.setattr(config, "FUSED_GATE_ENABLED", True)
    monkeypatch.setitem(vector._agent_fused_gates, AGENT, (weakest + strongest) / 2)
    monkeypatch.setattr(vector, "_fused_gate_signal", probe["messages"][-1]["match_reason"]["signal"])

    gated = await M.do_recall(AGENT, "reticle stepper alignment", limit=10)

    assert len(_qualified(gated)) < len(_qualified(probe)), (
        "a calibrated gate placed between the strongest and weakest row refused "
        f"nothing, so nothing filters a fused order at all: {gated}"
    )


# ===========================================================================
# D3-now — the lexical weight
# ===========================================================================


@pytest.mark.asyncio
async def test_lexical_weight_default_leaves_the_fused_score_identical(fake_embedding_client):
    """At 1.0 the vote is the same division, so the score is the same float.

    Compared against the arithmetic rather than a recorded number: a golden would
    also pass on a weight that happened to round to the same tenth decimal.
    """
    assert config.RRF_LEXICAL_WEIGHT == 1.0, "the shipped default moved without the ladder"
    # Lexical-only: the stored vector shares nothing with its own text, so the row
    # reaches the fusion through FTS alone and its score is one lexical vote.
    await _insert("thermionic cathode emission note", blob_text="unrelated gardening soil water")

    db = await get_db()
    rows = await M._recall_rrf(db, AGENT, "thermionic cathode emission", 10, False)

    row = next(r for r in rows if r["content"] == "thermionic cathode emission note")
    assert row["_rrf_score"] == 1.0 / (config.RRF_K + 0 + 1), (
        "the rank-1 lexical vote is no longer 1/(K+1); at weight 1.0 the fusion must be "
        f"bit-identical to the fusion that has always shipped: {row['_rrf_score']}"
    )


@pytest.mark.asyncio
async def test_lexical_weight_scales_the_lexical_vote(fake_embedding_client, monkeypatch):
    """The knob moves the order it is there to move, and zero silences the arm."""
    # One row the dense arm ranks first and the lexical arm not at all (its text
    # shares no term with the query), one the lexical arm ranks first and the dense
    # arm second. At weight 1 the second row's two votes outweigh the first row's
    # one; at weight 0 only the dense ranks remain and the order is the other way.
    # Nothing but the weight differs between the two runs.
    await _insert("orchard pears harvest", blob_text="thermionic cathode emission")
    await _insert("thermionic cathode emission", blob_text="thermionic cathode")

    db = await get_db()

    async def order_at(weight: float) -> list[str]:
        monkeypatch.setattr(config, "RRF_LEXICAL_WEIGHT", weight)
        rows = await M._recall_rrf(db, AGENT, "thermionic cathode emission", 10, False)
        return [r["content"] for r in rows]

    full = await order_at(1.0)
    silenced = await order_at(0.0)

    assert full[0] == "thermionic cathode emission", (
        f"the fixture's lexical row does not win at weight 1.0, so the sweep below "
        f"measures nothing: {full}"
    )
    assert silenced[0] == "orchard pears harvest", (
        "silencing the lexical arm left its row on top, so the weight is not reaching "
        f"the vote: {silenced}"
    )


@pytest.mark.asyncio
async def test_a_nan_score_is_not_admitted_by_the_absent_threshold(fake_embedding_client):
    """"No threshold applies" must not read as "everything passes".

    A non-finite embedding scores NaN, and every comparison against NaN is
    false — so through 2.5 such a row was refused by whichever threshold it met,
    and refused for the wrong reason: the arithmetic, not a decision. With the
    pool-size heuristic gone there is no comparison left to refuse it, and the
    row's position in the answer would then be decided by however the platform
    orders NaN. Measured the hard way: recorded on one interpreter, the same
    scenario ranked differently on another.
    """
    from conftest import _FAKE_DIM

    db = await get_db()
    await db.execute(
        "INSERT INTO memories (agent_id, content, embedding, timestamp, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (AGENT, "reticle stepper alignment with a broken vector",
         EmbeddingClient.pack_embedding([float("nan")] * _FAKE_DIM),
         "2026-05-01T00:00:00Z", "2026-05-01T00:00:00Z"),
    )
    await db.commit()
    await _insert("reticle stepper alignment note")

    out = await M.do_recall(AGENT, "reticle stepper alignment", limit=10)

    contents = [m["content"] for m in out["messages"]]
    assert "reticle stepper alignment note" in contents, (
        f"fixture regression: the finite row did not survive: {out}"
    )
    assert "reticle stepper alignment with a broken vector" not in contents, (
        "a row whose similarity is not a number reached the caller — as a hit if it "
        f"passed the gate, as a reservation row if it was reserved: {out}"
    )
