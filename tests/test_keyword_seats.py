"""Keyword seats: places held, when a caller asks for them, for rows only the keyword arms found.

Under rrf the quality gate keys a row without a cosine on its fused score, and one
arm's reciprocal-rank vote is at most 1/(K+1), so no keyword-only row can clear a
calibrated rrf gate. The seats hold a fixed number of places for such rows, as the
block arm's reservation does, and keep the other seats' rule for everything else:
a row the gate refused on a scale that could pass it (rsf here) stays out, while
one the gate admitted and the count cut may sit. The rows that hold more of the
question's parts sit first. reconstruct asks for one seat, under a mode since 2.6.9
and without one since 2.6.12 (bug-808); the recall tool never asks.

Fixture: as in test_gate_remediation, memories are inserted directly so a
keyword-only row can carry a blob disjoint from its own content (do_store would
embed the content). The block arm is closed so its own reservation adds nothing.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from conftest import fake_embed_one

from cpersona import config, vector
from cpersona import memory_handlers as M
from cpersona._vendored_mcp_common.embedding_client import EmbeddingClient
from cpersona.database import get_db

AGENT = "agent.keyword-seats"
STRONG = "apples orchard harvest notes"
KEYWORD_ONLY = "apples zzz yyy www"


def _pack_of(text: str) -> bytes:
    return EmbeddingClient.pack_embedding(fake_embed_one(text))


def _reset_gate() -> None:
    vector._agent_thresholds.clear()
    vector._agent_fused_gates.clear()
    vector._global_fused_gate = None
    vector._fused_gate_signal = None
    vector._agent_betas.clear()
    config.VECTOR_MIN_SIMILARITY = 0.3


@pytest_asyncio.fixture(autouse=True)
async def _fresh(monkeypatch, blocks_off, fake_embedding_client):
    db = await get_db()
    for table in ("memories", "episodes", "profiles"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()
    _reset_gate()
    monkeypatch.setattr(M, "CONFIDENCE_ENABLED", False)
    monkeypatch.setattr(config, "FUSED_GATE_ENABLED", True)
    yield
    _reset_gate()


async def _insert(content: str, blob: bytes, ts: str) -> int:
    db = await get_db()
    cur = await db.execute(
        "INSERT INTO memories (agent_id, channel, content, source, timestamp, embedding, created_at) "
        "VALUES (?, '', ?, '{}', ?, ?, ?)",
        (AGENT, content, ts, blob, ts),
    )
    await db.commit()
    return cur.lastrowid


async def _seed() -> tuple[int, int]:
    strong = await _insert(STRONG, _pack_of(STRONG), "2026-01-01T00:00:00Z")
    weak = await _insert(KEYWORD_ONLY, _pack_of("completely different unrelated content xxxx"), "2026-01-01T00:00:01Z")
    return strong, weak


def _gate(signal: str, value: float) -> None:
    vector._fused_gate_signal = signal
    vector._agent_fused_gates[AGENT] = value


async def _recall(monkeypatch, mode: str, seats: int, limit: int = 5):
    monkeypatch.setattr(M, "RECALL_MODE", mode)
    # Two words: the vector hit also holds both, so it ranks first on the keyword
    # arm too, and the keyword-only row (one word) ranks last on every scale.
    out = await M.do_recall(AGENT, "apples orchard", limit=limit, keyword_seats=seats)
    # The dict below keys by content, which would collapse a row returned twice (an
    # ordinary hit and a keyword seat for the same record); check refs are unique first.
    refs = [m.get("ref") for m in out["messages"]]
    assert len(refs) == len(set(refs)), f"the same row was returned twice: {refs}"
    return out, {m["content"]: m for m in out["messages"]}


@pytest.mark.asyncio
async def test_rrf_gate_drops_the_keyword_only_row_without_seats(monkeypatch):
    await _seed()
    _gate("rrf", 0.05)
    _, rows = await _recall(monkeypatch, "rrf", 0)
    assert STRONG in rows, "fixture regression: the vector hit did not survive the gate"
    assert KEYWORD_ONLY not in rows, "fixture is vacuous: the keyword-only row passed the rrf gate on its own"


@pytest.mark.asyncio
async def test_a_seat_holds_the_row_the_rrf_gate_cannot_pass(monkeypatch):
    await _seed()
    _gate("rrf", 0.05)
    out, rows = await _recall(monkeypatch, "rrf", 2)
    assert STRONG in rows and rows[STRONG]["match_reason"].get("admission") != "reservation"
    seat = rows[KEYWORD_ONLY]["match_reason"]
    assert seat["signal"] == "keyword" and seat["admission"] == "reservation" and seat["seat"] == 1
    assert 0 < seat["rrf"] < 0.05
    # Nothing the gate admitted was displaced, and the seat sits after the answer.
    contents = [m["content"] for m in out["messages"]]
    assert contents.index(STRONG) != contents.index(KEYWORD_ONLY)
    assert len(out["messages"]) == 2


@pytest.mark.asyncio
async def test_seats_hold_at_most_their_number(monkeypatch):
    await _seed()
    for i in range(3):
        await _insert(f"apples extra keyword row {i} qqq", _pack_of(f"unrelated filler text number {i} vvv"),
                      f"2026-01-01T00:00:0{2 + i}Z")
    _gate("rrf", 0.05)
    out, rows = await _recall(monkeypatch, "rrf", 2)
    seated = [m for m in out["messages"] if m["match_reason"].get("signal") == "keyword"]
    assert len(seated) == 2, [m["content"] for m in out["messages"]]
    assert sorted(m["match_reason"]["seat"] for m in seated) == [1, 2]


@pytest.mark.asyncio
async def test_a_row_refused_on_the_rsf_scale_takes_no_seat(monkeypatch):
    await _seed()
    _gate("rsf", 0.5)  # the vector hit scores 1.0 on rsf; the keyword-only row 0.0
    _, rows = await _recall(monkeypatch, "rsf", 2)
    assert STRONG in rows, "fixture regression: the vector hit did not survive the rsf gate"
    assert KEYWORD_ONLY not in rows, "a row refused on rsf came back through a keyword seat"


@pytest.mark.asyncio
async def test_a_keyword_only_row_the_count_cut_takes_a_seat(monkeypatch):
    # limit 1 is also the recall depth, so each arm hands the fusion its best row only.
    # "zzz apples" puts the keyword-only row first on the keyword arm (both words) and
    # the vector hit first on the vector arm (the shared word): both pass a zero gate,
    # and the count keeps one of them.
    await _seed()
    _gate("rsf", 0.0)
    monkeypatch.setattr(M, "RECALL_MODE", "rsf")
    out = await M.do_recall(AGENT, "zzz apples", limit=1, trace=True, keyword_seats=2)
    refs = [m.get("ref") for m in out["messages"]]
    assert len(refs) == len(set(refs)), f"the same row was returned twice: {refs}"
    rows = {m["content"]: m for m in out["messages"]}
    cut = out["trace"]["order"]["cut_by_count"]
    assert len(cut) == 1, f"fixture is vacuous: the count cut {cut}"
    seated = [c for c, m in rows.items() if m["match_reason"].get("signal") == "keyword"]
    assert seated == [KEYWORD_ONLY], rows
    assert "rsf" in rows[KEYWORD_ONLY]["match_reason"]


@pytest.mark.asyncio
async def test_a_seated_row_is_not_credited(monkeypatch):
    # The recall count is bumped only where confidence reads it (do_recall), which
    # under the fusion ordering gates nothing, so the seat is still decided on rrf.
    monkeypatch.setattr(M, "CONFIDENCE_ENABLED", True)
    strong, weak = await _seed()
    _gate("rrf", 0.05)
    _, rows = await _recall(monkeypatch, "rrf", 2)
    assert KEYWORD_ONLY in rows
    db = await get_db()
    counts = dict(await db.execute_fetchall("SELECT id, recall_count FROM memories WHERE id IN (?, ?)", (strong, weak)))
    assert counts[strong] >= 1 and counts[weak] == 0, counts


@pytest.mark.asyncio
async def test_the_recall_tool_holds_no_seat(monkeypatch):
    from cpersona import server

    await _seed()
    _gate("rrf", 0.05)
    monkeypatch.setattr(M, "RECALL_MODE", "rrf")
    out = await server.registry._handlers["recall"]({"agent_id": AGENT, "query": "apples orchard", "limit": 5})
    contents = [m["content"] for m in out["messages"]]
    assert STRONG in contents and KEYWORD_ONLY not in contents, contents
    schemas = {t.name: t.inputSchema for t in server.registry._tools}
    assert "recall" in schemas and "reconstruct" in schemas, sorted(schemas)
    for name, schema in schemas.items():
        assert "keyword_seats" not in (schema or {}).get("properties", {}), name


@pytest.mark.asyncio
async def test_the_seat_goes_to_the_row_holding_more_of_the_question(monkeypatch):
    # Two keyword-only rows: FEW holds one of the question's parts and ranks first on
    # the keyword arm (short, the word repeated); MORE holds both and ranks after it
    # (the words buried in filler). One seat goes to MORE.
    await _seed()
    few = "orchard orchard orchard"
    more = "apples " + " ".join(f"filler{i}" for i in range(60)) + " orchard"
    await _insert(few, _pack_of("unrelated vector text one kkk"), "2026-01-01T00:00:05Z")
    await _insert(more, _pack_of("unrelated vector text two jjj"), "2026-01-01T00:00:06Z")
    _gate("rrf", 0.05)
    monkeypatch.setattr(M, "RECALL_MODE", "rrf")
    out = await M.do_recall(AGENT, "apples orchard", limit=5, keyword_seats=1, trace=True)
    order = [d["ref"] for d in out["trace"]["gate"]["decisions"] if not d["passed"]]
    db = await get_db()
    ids = dict(await db.execute_fetchall("SELECT content, id FROM memories WHERE content IN (?, ?)", (few, more)))
    assert order.index(f"mem:{ids[few]}") < order.index(f"mem:{ids[more]}"), f"fixture is vacuous: gate order {order}"
    seated = [m["content"] for m in out["messages"] if m["match_reason"].get("signal") == "keyword"]
    assert seated == [more], seated


@pytest.mark.asyncio
async def test_reconstruct_holds_the_seat_beside_its_window(monkeypatch):
    from cpersona import reconstruct as R

    await _seed()
    _gate("rrf", 0.05)
    monkeypatch.setattr(M, "RECALL_MODE", "rrf")
    # The extra keyword places would hold the same refused row; without them only the seat can.
    monkeypatch.setattr(R, "MODE_EXTRA_KEYWORD_SEATS", {})
    out = await R.do_reconstruct(AGENT, "apples orchard", count=5, mode="pro")
    held = [it for it in out["items"] if it.get("admission") == "reservation"]
    assert [it["content"] for it in held] == [KEYWORD_ONLY], out["items"]
    assert out.get("reserved_count") == 1
    window = [it for it in out["items"] if it.get("admission") != "reservation"]
    assert window and window[0]["content"] == STRONG


@pytest.mark.asyncio
async def test_reconstruct_without_a_mode_holds_the_seat_too(monkeypatch):
    """bug-808: a call that names no mode asks for the seat as a mode does (before 2.6.12
    it asked for none). The recall tool still never asks."""
    from cpersona import reconstruct as R

    await _seed()
    _gate("rrf", 0.05)
    monkeypatch.setattr(M, "RECALL_MODE", "rrf")
    out = await R.do_reconstruct(AGENT, "apples orchard", count=5)
    assert [it["content"] for it in out["items"] if it.get("admission") == "reservation"] == [KEYWORD_ONLY], out["items"]
    assert STRONG in [it["content"] for it in out["items"] if it.get("admission") != "reservation"], out["items"]
    lite = await R.do_reconstruct(AGENT, "apples orchard", count=5, mode="lite")
    assert [it["content"] for it in lite["items"] if it.get("admission") == "reservation"] == [KEYWORD_ONLY]
    recalled = await M.do_recall(AGENT, "apples orchard", limit=5)
    assert KEYWORD_ONLY not in [m["content"] for m in recalled["messages"]]


@pytest.mark.asyncio
async def test_the_reservation_bound_counts_the_seat_without_a_mode(monkeypatch):
    """The count contract's bound on held items includes the seat for a call that names
    no mode too: with the other places it adds up exactly, so the check would refuse a
    response whose held places outnumber the places the call asked for."""
    from cpersona import blocks, cue, far_seats, providers
    from cpersona import reconstruct as R

    bounds = []
    real = providers.check_reconstruct_count
    monkeypatch.setattr(providers, "check_reconstruct_count",
                        lambda returned, reserved, effective, reservation: (bounds.append(reservation),
                                                                             real(returned, reserved, effective, reservation)))
    await _seed()
    _gate("rrf", 0.05)
    monkeypatch.setattr(M, "RECALL_MODE", "rrf")
    await R.do_reconstruct(AGENT, "apples orchard", count=5)
    others = blocks.BLOCK_RESERVATION + cue.MAX_SEATS + (far_seats.SEATS if far_seats.enabled() else 0)
    assert bounds == [others + R.MODE_KEYWORD_SEATS], bounds


@pytest.mark.asyncio
async def test_an_identifier_the_uncalibrated_gate_refuses_comes_back_on_its_seat(monkeypatch):
    """bug-808, as measured: five notes before calibration (the heuristic gate), one of
    them a longer note that mentions an invoice number among other things, its vector
    far from a search for the number. The gate refuses it (one keyword vote under a
    three-vote threshold) and nothing else comes back; a call that names no mode
    returns it on the keyword seat."""
    from cpersona import reconstruct as R

    monkeypatch.setattr(M, "RECALL_MODE", "rrf")
    monkeypatch.setattr(config, "FUSED_GATE_ENABLED", False)  # no calibrated gate: the pool-size heuristic
    note = ("shelf repo preference: when finishing a change commit it myself and start the commit "
            "message with shelf: because the changelog script only picks up that prefix; also the "
            "hosting renewal invoice INV-4471 due 30 Nov is already paid so no action is needed")
    # The suite's fake embedder is a 64-dimensional bag of words, too coarse to dilute one
    # token below the vector arm's floor as a real embedding of this note does, so its
    # vector is packed from the note without the identifier.
    await _insert(note, _pack_of(note.replace(" INV-4471", "")), "2026-01-01T00:00:00Z")
    for i, text in enumerate(("late fees decided by the board thirty yen per day capped at six hundred",
                              "the overnight job belongs to the city team so do not edit it",
                              "members will be allowed at most three books out at once",
                              "the front desk wants a report of overdue loans as a csv file"), 1):
        await _insert(text, _pack_of(text), f"2026-01-01T00:00:0{i}Z")
    recalled = await M.do_recall(AGENT, "INV-4471", limit=5)
    assert note not in [m["content"] for m in recalled["messages"]], "fixture is vacuous: the gate admitted the note"
    out = await R.do_reconstruct(AGENT, "INV-4471", count=5)
    seated = [it["content"] for it in out["items"] if it.get("admission") == "reservation"]
    assert seated == [note], out["items"]


@pytest.mark.asyncio
async def test_a_row_the_vector_arm_found_takes_no_seat(monkeypatch):
    # A depth floor above the count lets the vector arm bring two admitted rows that the
    # count of 1 cuts to one. The cut row was found by the vector arm, so it is not the
    # keyword seats' to hold, even though the gate admitted it.
    await _insert(STRONG, _pack_of(STRONG), "2026-01-01T00:00:00Z")
    near = "apples orchard harvest notes again"
    await _insert(near, _pack_of(near), "2026-01-01T00:00:01Z")
    _gate("rrf", 0.05)
    monkeypatch.setattr(M, "RECALL_MODE", "rrf")
    monkeypatch.setattr(config, "RECALL_DEPTH_FLOOR", 5)
    out = await M.do_recall(AGENT, "apples orchard", limit=1, keyword_seats=1, trace=True)
    cut = out["trace"]["order"]["cut_by_count"]
    assert len(cut) == 1, f"fixture is vacuous: the count cut {cut}"
    seated = [m["content"] for m in out["messages"] if m["match_reason"].get("signal") == "keyword"]
    assert seated == [], seated
