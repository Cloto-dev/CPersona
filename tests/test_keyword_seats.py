"""Keyword seats (CPERSONA_KEYWORD_SEATS): places held for rows only the keyword arms found.

Under rrf the quality gate keys a row without a cosine on its fused score, and one
arm's reciprocal-rank vote is at most 1/(K+1), so no keyword-only row can clear a
calibrated rrf gate. The seats hold a fixed number of places for such rows, as the
block arm's reservation does, and keep the other seats' rule for everything else:
a row the gate refused on a scale that could pass it (rsf here) stays out, while
one the gate admitted and the count cut may sit.

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
    monkeypatch.setattr(config, "KEYWORD_SEATS", seats)
    # Two words: the vector hit also holds both, so it ranks first on the keyword
    # arm too, and the keyword-only row (one word) ranks last on every scale.
    out = await M.do_recall(AGENT, "apples orchard", limit=limit)
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
    monkeypatch.setattr(config, "KEYWORD_SEATS", 2)
    out = await M.do_recall(AGENT, "zzz apples", limit=1, trace=True)
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


def test_unset_the_seats_follow_the_fusion(monkeypatch):
    assert "CPERSONA_KEYWORD_SEATS" not in __import__("os").environ
    assert config.KEYWORD_SEATS is None
    assert config.keyword_seats("rrf") == 2
    assert config.keyword_seats("rsf") == 0
    assert config.keyword_seats("cascade") == 0


def test_a_set_number_applies_to_every_fusion(monkeypatch):
    for seats in (0, 3):
        monkeypatch.setattr(config, "KEYWORD_SEATS", seats)
        assert config.keyword_seats("rrf") == seats
        assert config.keyword_seats("rsf") == seats


@pytest.mark.asyncio
async def test_unset_rrf_seats_the_keyword_only_row(monkeypatch):
    await _seed()
    _gate("rrf", 0.05)
    _, rows = await _recall(monkeypatch, "rrf", None)
    seat = rows[KEYWORD_ONLY]["match_reason"]
    assert seat["signal"] == "keyword" and seat["admission"] == "reservation" and seat["seat"] == 1


@pytest.mark.asyncio
async def test_no_seat_when_the_gate_admitted_nothing(monkeypatch):
    # behavior-contracts.md section 8: a recall with nothing above the gate returns
    # nothing. A gate above the vector hit's scale too leaves only keyword-only rows.
    await _seed()
    _gate("rrf", 0.05)
    _, with_gate = await _recall(monkeypatch, "rrf", 2)
    assert KEYWORD_ONLY in with_gate, "fixture is vacuous: no seat to withhold"
    monkeypatch.setattr(M, "_adaptive_min_score", lambda count: 5.0)
    _gate("rrf", 5.0)
    out, rows = await _recall(monkeypatch, "rrf", 2)
    assert out["messages"] == [], [m["content"] for m in out["messages"]]


@pytest.mark.asyncio
async def test_unset_rsf_holds_no_seat(monkeypatch):
    # The count-cut fixture below: under rsf, two seats would take the keyword-only row
    # the count cut. Unset must hold none, as 0 does.
    await _seed()
    _gate("rsf", 0.0)

    async def contents(seats):
        monkeypatch.setattr(M, "RECALL_MODE", "rsf")
        monkeypatch.setattr(config, "KEYWORD_SEATS", seats)
        out = await M.do_recall(AGENT, "zzz apples", limit=1)
        return [m["content"] for m in out["messages"]]

    two, unset, none = await contents(2), await contents(None), await contents(0)
    assert KEYWORD_ONLY in two, "fixture is vacuous: two seats took nothing under rsf"
    assert unset == none and KEYWORD_ONLY not in unset


@pytest.mark.asyncio
async def test_reconstruct_holds_the_seat_beside_its_window(monkeypatch):
    from cpersona import reconstruct as R

    await _seed()
    _gate("rrf", 0.05)
    monkeypatch.setattr(M, "RECALL_MODE", "rrf")
    monkeypatch.setattr(config, "KEYWORD_SEATS", 2)
    out = await R.do_reconstruct(AGENT, "apples orchard", count=5)
    held = [it for it in out["items"] if it.get("admission") == "reservation"]
    assert [it["content"] for it in held] == [KEYWORD_ONLY], out["items"]
    assert out.get("reserved_count") == 1
    window = [it for it in out["items"] if it.get("admission") != "reservation"]
    assert window and window[0]["content"] == STRONG
