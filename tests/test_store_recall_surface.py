"""Agent-facing response polish for do_store and do_recall (v2.5.2, Task #282).

Both changes are strictly additive on top of the existing JSON shapes so any
existing agent that ignores unknown keys keeps working; these tests pin the
NEW keys so a future refactor cannot silently drop them (which is how the
original CSC Task #282 items were requested — the internal signals were
already computed but discarded before the response, and the id was already
in hand from the INSERT but not echoed).

Two areas covered:

(2) do_store success responses carry the inserted row id and an ``embedded``
    flag that reports whether any embedding surface was actually populated
    (local blob or remote index push). Dedup skips echo the pre-existing
    row's id when the SELECT probe already has it (the OR IGNORE fallback
    stays id-less by design; see the branch comment).

(3) do_recall messages carry ``match_reason = {"signal": ..., "score": ...,
    ...breakdown}`` for scored rows, so agents can tell WHY a row surfaced
    instead of guessing from opaque ``confidence``. Unscored cascade rows
    omit the key. The pre-existing ``confidence`` field's shape is
    unchanged (2.6.0 owns any scoring reshape per charter §5).
"""

import pytest
import pytest_asyncio

from cpersona import memory_handlers
from cpersona._vendored_mcp_common import no_persist
from cpersona.database import get_db


@pytest_asyncio.fixture
async def clean_db():
    """A freshly-truncated DB, mirroring the fixture in test_audit_2439.py.

    Also resets sqlite_sequence so id assertions below (memory #1, memory #2)
    are stable regardless of collection order — the whole suite shares one
    DB singleton, and a prior test's inserts otherwise shift AUTOINCREMENT.
    """
    no_persist.resume()
    db = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await db.execute(f"DELETE FROM {table}")
    await db.execute(
        "DELETE FROM sqlite_sequence WHERE name IN "
        "('memories','episodes','profiles','pending_memory_tasks')"
    )
    await db.commit()
    return db


# ---------------------------------------------------------------------------
# (2) do_store: id + embedded on success, id echo on dedup skip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_success_returns_id_matching_db_row(clean_db):
    """The success response's ``id`` must be the actual row id, so the caller
    can chain (e.g. update_memory) without a follow-up SELECT."""
    db = clean_db
    res = await memory_handlers.do_store(
        "a1", {"content": "first message", "source": {"type": "User"}, "timestamp": "2026-07-22T00:00:00+00:00"}
    )
    assert res["ok"] is True and not res.get("skipped"), res
    assert "id" in res, res
    row = await db.execute_fetchall("SELECT id, content FROM memories WHERE id = ?", (res["id"],))
    assert len(row) == 1 and row[0][1] == "first message"


@pytest.mark.asyncio
async def test_store_embedded_false_under_mode_none(clean_db):
    """Under the hermetic default (CPERSONA_EMBEDDING_MODE=none — see conftest),
    ``vector._embedding_client`` stays None so no local blob is produced and no
    remote push happens. ``embedded`` MUST be false — an agent using it to
    decide whether to fall back to keyword-only retrieval would otherwise
    silently over-trust an unembedded corpus."""
    res = await memory_handlers.do_store(
        "a1", {"content": "unembedded row", "source": {}, "timestamp": "t"}
    )
    assert res["ok"] is True and not res.get("skipped"), res
    assert res["embedded"] is False, res


@pytest.mark.asyncio
async def test_store_embedded_true_when_local_blob_present(clean_db, fake_embedding_client):
    """With the deterministic fake embedding client wired (mode == 'local' path
    in the current default), the store path packs a blob and the INSERT persists
    it — ``embedded`` MUST report true so the recall side's vector branch is
    trustable."""
    res = await memory_handlers.do_store(
        "a1", {"content": "embedded row", "source": {}, "timestamp": "t"}
    )
    assert res["ok"] is True and not res.get("skipped"), res
    assert res["embedded"] is True, res


@pytest.mark.asyncio
async def test_store_dedup_content_echoes_existing_id(clean_db):
    """The content-dedup skip response MUST echo the pre-existing row's id,
    so an idempotent write path (e.g. a retry on transient error) can pick
    up the row it collided with."""
    first = await memory_handlers.do_store(
        "a1", {"content": "shared content", "source": {}, "timestamp": "t"}
    )
    assert first["ok"] and not first.get("skipped"), first
    dup = await memory_handlers.do_store(
        "a1", {"content": "shared content", "source": {}, "timestamp": "t"}
    )
    assert dup.get("skipped") is True and dup.get("reason") == "duplicate content", dup
    assert dup.get("id") == first["id"], dup


@pytest.mark.asyncio
async def test_store_dedup_msg_id_echoes_existing_id(clean_db):
    """The msg_id-dedup skip response MUST also echo the pre-existing row's id."""
    first = await memory_handlers.do_store(
        "a1", {"id": "m-1", "content": "row A", "source": {}, "timestamp": "t"}
    )
    assert first["ok"] and not first.get("skipped"), first
    dup = await memory_handlers.do_store(
        "a1", {"id": "m-1", "content": "row B", "source": {}, "timestamp": "t"}
    )
    assert dup.get("skipped") is True and dup.get("reason") == "duplicate msg_id", dup
    assert dup.get("id") == first["id"], dup


# ---------------------------------------------------------------------------
# (3) do_recall: match_reason on scored rows, absent on unscored, confidence unchanged
# ---------------------------------------------------------------------------


class _FakeDB:
    """Answers exactly the queries do_recall / _apply_recall_scoring issue in the
    scoring loop. Modelled on test_do_recall_response._FakeDB.

    ``memory_count`` is returned as 500 so the adaptive gate is permissive
    enough for the scored rows below to pass through untouched — this test is
    about the response shape, not the gate.
    """

    def __init__(self):
        self.executed: list[str] = []

    async def execute_fetchall(self, sql, params=()):
        s = " ".join(sql.split())
        if s.startswith("SELECT MIN(timestamp), MAX(timestamp)"):
            return [("2026-07-22T10:00:00+00:00", "2026-07-22T12:00:00+00:00")]
        if s.startswith("SELECT id, recall_count, last_recalled_at"):
            return [(1, 0, ""), (2, 0, ""), (3, 0, "")]
        if s.startswith("SELECT COUNT(*)"):
            return [(500,)]
        if s.startswith("SELECT created_at FROM episodes"):
            return []
        return []

    async def execute(self, sql, params=()):
        self.executed.append(" ".join(sql.split()))
        return None

    async def commit(self):
        return None


def _install_fake_recall(monkeypatch, rows, mode="rsf", confidence=True):
    """Patch the seam CMs + the mode's retrieval driver so do_recall runs its
    real scoring / response loop on the caller's synthetic ``rows``. Same
    pattern as test_do_recall_response._patch."""
    import contextlib

    fake = _FakeDB()

    @contextlib.asynccontextmanager
    async def fake_cm():
        yield fake

    async def fake_driver(db, agent_id, query, limit, deep, channel="", exclude_set=None,
                          project_id=None, source_id=""):
        # Deep-copy so the do_recall scoring pipeline mutates OUR list, not the
        # test's constant — otherwise a second recall in the same test would
        # see leftover _confidence_score / _rid entries.
        import copy
        return copy.deepcopy(rows)

    monkeypatch.setattr(memory_handlers, "connection", fake_cm)
    monkeypatch.setattr(memory_handlers, "transaction", fake_cm)
    monkeypatch.setattr(memory_handlers, "CONFIDENCE_ENABLED", confidence)
    monkeypatch.setattr(memory_handlers, "RECALL_MODE", mode)
    if mode == "rsf":
        monkeypatch.setattr(memory_handlers, "_recall_rsf", fake_driver)
    elif mode == "rrf":
        monkeypatch.setattr(memory_handlers, "_recall_rrf", fake_driver)
    else:
        monkeypatch.setattr(memory_handlers, "_recall_cascade", fake_driver)
    return fake


@pytest.mark.asyncio
async def test_recall_scored_row_carries_match_reason_with_signal(monkeypatch):
    """Fusion-scored rows (rsf mode + CONFIDENCE_ENABLED) must expose
    match_reason. Under confidence-on, _gate_score's precedence gives
    signal='confidence' — that's the branch the runtime quality gate would
    key on, so agents get the same explanation as the gate."""
    rows = [
        {"id": 1, "content": "vector rich match", "source": {"System": "t"},
         "timestamp": "2026-07-22T12:00:00+00:00",
         "_cosine": 0.82, "_rsf_score": 0.82, "_rid": ("mem", 1)},
        {"id": 2, "content": "second match", "source": {"System": "t"},
         "timestamp": "2026-07-22T11:30:00+00:00",
         "_cosine": 0.55, "_rsf_score": 0.55, "_rid": ("mem", 2)},
    ]
    _install_fake_recall(monkeypatch, rows, mode="rsf", confidence=True)
    out = await memory_handlers.do_recall("agent.t", "vector rich match", limit=5)
    msgs = out["messages"]
    assert len(msgs) == 2, msgs
    for m in msgs:
        mr = m.get("match_reason")
        assert mr is not None, m
        # Confidence takes precedence over rsf/cosine in _gate_score.
        assert mr["signal"] == "confidence", mr
        assert isinstance(mr["score"], (int, float)) and 0.0 <= mr["score"] <= 1.0, mr
        # Breakdown carries the cosine / rsf the ranking layer produced. These
        # are agent-facing signals — pinning them here catches an accidental
        # pop-before-expose refactor.
        assert "cosine" in mr and mr["cosine"] > 0.0, mr
        assert "rsf" in mr and mr["rsf"] > 0.0, mr
        # rrf was never on these rows, so it must NOT be fabricated.
        assert "rrf" not in mr, mr


@pytest.mark.asyncio
async def test_recall_confidence_off_signal_reports_underlying_fused_branch(monkeypatch):
    """With CONFIDENCE_ENABLED=False, _gate_score's precedence chain falls
    through to the fused signal actually present on the row (rsf here) — so
    agents see the branch the gate keys on in this deployment, not a stale
    'confidence' answer from a config it isn't running."""
    rows = [
        {"id": 1, "content": "rsf-only row", "source": {"System": "t"},
         "timestamp": "2026-07-22T12:00:00+00:00",
         "_cosine": 0.7, "_rsf_score": 0.7, "_rid": ("mem", 1)},
    ]
    _install_fake_recall(monkeypatch, rows, mode="rsf", confidence=False)
    out = await memory_handlers.do_recall("agent.t", "rsf-only row", limit=5)
    msgs = out["messages"]
    assert len(msgs) == 1, msgs
    mr = msgs[0]["match_reason"]
    # With confidence off, rsf wins the precedence.
    assert mr["signal"] == "rsf", mr
    assert mr["score"] == pytest.approx(mr["rsf"]), mr
    # And the agent-facing confidence dict is not fabricated either.
    assert "confidence" not in msgs[0], msgs[0]


@pytest.mark.asyncio
async def test_recall_unscored_row_omits_match_reason(monkeypatch):
    """A cascade-style row with no fusion score AND no cosine (episode/keyword
    stage, confidence off) has no signal for _gate_score to key on. The key
    MUST be omitted so an agent can distinguish 'no signal at all' from
    'signal happened to be zero'."""
    rows = [
        {"id": 1, "content": "unscored keyword row", "source": {"System": "t"},
         "timestamp": "2026-07-22T12:00:00+00:00"},
    ]
    _install_fake_recall(monkeypatch, rows, mode="cascade", confidence=False)
    out = await memory_handlers.do_recall("agent.t", "unscored keyword row", limit=5)
    msgs = out["messages"]
    assert len(msgs) == 1, msgs
    assert "match_reason" not in msgs[0], msgs[0]


@pytest.mark.asyncio
async def test_recall_confidence_field_shape_unchanged(monkeypatch):
    """Charter §5: any scoring semantics reshape lives in 2.6.0. The 2.5.2
    additive changes MUST NOT touch the ``confidence`` dict's keys or nesting
    — a rename here would break every agent that already reads it."""
    rows = [
        {"id": 1, "content": "shape-pin row", "source": {"System": "t"},
         "timestamp": "2026-07-22T12:00:00+00:00",
         "_cosine": 0.82, "_rsf_score": 0.82, "_rid": ("mem", 1)},
    ]
    _install_fake_recall(monkeypatch, rows, mode="rsf", confidence=True)
    out = await memory_handlers.do_recall("agent.t", "shape-pin row", limit=5)
    msg = out["messages"][0]
    conf = msg.get("confidence")
    assert isinstance(conf, dict) and "score" in conf, conf
    assert isinstance(conf["score"], (int, float)) and 0.0 <= conf["score"] <= 1.0, conf
