"""Regression test for the do_recall response path (v2.4.28).

v2.4.27 factored the post-recall scoring (episode penalty + confidence) out of
do_recall into _apply_recall_scoring, but do_recall's response-metadata loop and its
recall-count update reuse ``time_range_hours`` / ``recall_counts`` — which the refactor
left undefined in do_recall's scope. Under CONFIDENCE_ENABLED every recall then raised
``NameError: name 'recall_counts' is not defined``. The integration recall tests hang
without a resident embedding server, so this exercises do_recall's full code path with a
mocked DB + recall function instead, which runs in CI and catches this class of bug.
"""
import os
import tempfile

os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "x.db"))
os.environ["CPERSONA_EMBEDDING_MODE"] = "none"
os.environ["CPERSONA_CONFIDENCE_ENABLED"] = "true"  # the branch that regressed
os.environ["CPERSONA_RECALL_MODE"] = "rsf"

import pytest  # noqa: E402

import memory_handlers as M  # noqa: E402


class _FakeDB:
    """Answers only the queries do_recall / _apply_recall_scoring issue."""

    async def execute_fetchall(self, sql, params=()):
        s = " ".join(sql.split())
        if s.startswith("SELECT MIN(timestamp), MAX(timestamp)"):
            return [("2026-06-26T10:00:00+00:00", "2026-06-26T12:00:00+00:00")]
        if s.startswith("SELECT id, recall_count, last_recalled_at"):
            return [(1, 2, "2026-06-26T11:00:00+00:00"), (2, 0, "")]
        if s.startswith("SELECT COUNT(*)"):
            return [(3,)]
        if s.startswith("SELECT created_at FROM episodes"):
            return []
        return []

    async def execute(self, sql, params=()):
        return None

    async def commit(self):
        return None


async def _fake_rsf(db, agent_id, query, limit, deep, channel="", exclude_set=None,
                    project_id=None, source_id=""):
    return [
        {"id": 1, "content": "recall precision calibration gate", "source": {"System": "t"},
         "timestamp": "2026-06-26T12:00:00+00:00", "_cosine": 0.82, "_rsf_score": 0.82,
         "_rid": ("mem", 1)},
        {"id": 2, "content": "python asyncio tips", "source": {"System": "t"},
         "timestamp": "2026-06-26T10:30:00+00:00", "_cosine": 0.55, "_rsf_score": 0.55,
         "_rid": ("mem", 2)},
    ]


def _patch(monkeypatch):
    async def fake_getdb():
        return _FakeDB()
    monkeypatch.setattr(M, "get_db", fake_getdb)
    monkeypatch.setattr(M, "_recall_rsf", _fake_rsf)


@pytest.mark.asyncio
async def test_do_recall_confidence_enabled_returns_messages(monkeypatch):
    """The regression: CONFIDENCE_ENABLED recall must reach the response loop + the
    recall-count update without a NameError on the moved scoring locals."""
    _patch(monkeypatch)
    out = await M.do_recall("agent.t", "recall precision calibration", limit=5)
    assert "messages" in out
    assert len(out["messages"]) == 2
    for m in out["messages"]:
        assert "confidence" in m and "score" in m["confidence"]


@pytest.mark.asyncio
async def test_do_recall_deep_skips_recall_count_update(monkeypatch):
    """deep=True takes the other recall_counts branch (`if not deep and recall_counts`)."""
    _patch(monkeypatch)
    out = await M.do_recall("agent.t", "x", limit=5, deep=True)
    assert "messages" in out and len(out["messages"]) == 2
