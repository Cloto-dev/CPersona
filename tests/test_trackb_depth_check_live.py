"""The Track B depth check against the real do_recall, not a fake.

test_trackb_instrument.py pins the check's own logic with a fake server. That
leaves one assumption untested: that the package reports ``depth`` exactly when
the depth differs from the limit it clamped. If the package ever reported it
differently, the check would pass fakes and mark every real run invalid -- or,
worse, pass real runs that ranked at the wrong depth. These tests feed real
responses to the check.

Same setup as test_recall_depth.py: embeddings off, so the keyword arm answers
and the fusion runs on it alone.
"""

import importlib.util
import os
import tempfile
from pathlib import Path

os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "test_trackb_depth_check_live.db"))
os.environ.setdefault("CPERSONA_EMBEDDING_MODE", "none")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from cpersona import config  # noqa: E402
from cpersona import memory_handlers as M  # noqa: E402
from cpersona.database import get_db  # noqa: E402

_BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
_spec = importlib.util.spec_from_file_location("trackb_instrument_live", _BENCH / "trackb_instrument.py")
ti = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ti)

AGENT = "agent.trackb-depth-check"
QUERY = "rollback"


@pytest_asyncio.fixture(autouse=True)
async def _seeded():
    db = await get_db()
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM episodes")
    await db.commit()
    for i in range(30):
        out = await M.do_store(
            AGENT,
            {"content": f"note {i}: rollback of the billing deploy, ticket {2000 + i}", "source": {"System": "t"}},
        )
        assert out["result"] == "stored", out
    yield


def _check(floor: int, ceiling: int) -> "ti.DepthCheck":
    assert M.RECALL_MODE in ti.FUSING_MODES, "these tests need a fusing mode"
    return ti.DepthCheck(floor=floor, ceiling=ceiling, mode=M.RECALL_MODE)


@pytest.mark.asyncio
async def test_real_response_at_the_intended_floor_passes(monkeypatch):
    monkeypatch.setattr(config, "RECALL_DEPTH_FLOOR", 50)
    out = await M.do_recall(AGENT, QUERY, limit=5)
    dc = _check(50, M.RECALL_LIBRARY_MAX_LIMIT)
    dc.observe(5, QUERY, out)
    assert (dc.checked, dc.mismatches) == (1, 0), (out.get("depth"), dc.examples)


@pytest.mark.asyncio
async def test_real_response_at_the_default_passes_a_check_expecting_the_default():
    assert config.RECALL_DEPTH_FLOOR == 0
    out = await M.do_recall(AGENT, QUERY, limit=5)
    dc = _check(0, M.RECALL_LIBRARY_MAX_LIMIT)
    dc.observe(5, QUERY, out)
    assert (dc.checked, dc.mismatches) == (1, 0), dc.examples


@pytest.mark.asyncio
async def test_real_response_at_the_default_fails_a_check_expecting_a_floor():
    # The case the check exists for, on the real package: the floor the run
    # meant (50) never reached it, so it ranked at the default.
    assert config.RECALL_DEPTH_FLOOR == 0
    out = await M.do_recall(AGENT, QUERY, limit=5)
    dc = _check(50, M.RECALL_LIBRARY_MAX_LIMIT)
    dc.observe(5, QUERY, out)
    assert dc.mismatches == 1


@pytest.mark.asyncio
async def test_real_clamped_limit_is_not_a_false_mismatch(monkeypatch):
    # limit above the library ceiling: the package clamps it, the depth equals
    # the clamped limit, and it reports no depth.
    monkeypatch.setattr(M, "RECALL_LIBRARY_MAX_LIMIT", 20)
    out = await M.do_recall(AGENT, QUERY, limit=100)
    assert "depth" not in out
    dc = _check(0, 20)
    dc.observe(100, QUERY, out)
    assert (dc.checked, dc.mismatches) == (1, 0), dc.examples


def test_module_under_test_is_the_one_the_runner_imports():
    # The runner imports `trackb_instrument` from benchmarks/; this file loads the
    # same path, so the tests above exercise the code the runner executes.
    assert Path(ti.__file__).resolve() == (_BENCH / "trackb_instrument.py").resolve()
