"""Recall Depth is not the response count (2.6, docs/RELIABLE_RECALL_2_6.md section 4).

``limit`` used to be handed to every retrieval arm as its top-K, so asking for five
rows also fused only five candidates per arm, and rows past the fifth were out of
reach at every gate value. The line separates the two: ``limit`` is how many rows
come back, the depth is how far the fusion digs, ``max(limit, floor)``.

The invariant the design names as the verification: changing the count alone must
not change the set of candidate ids the fusion considered. It is pinned end to end
through ``do_recall`` -- not by calling the fusion helper with two numbers -- so it
answers whether the call site uses the depth, and the mutation that re-couples them
(``depth = limit`` in ``_recall_depth``) turns ``test_count_alone_does_not_move_the_pool``
red (checked by hand before this file was committed; the floor set here is 50
against 30 seeded rows and counts 3 / 5 / 10, so the recoupled pools are 3, 5 and 10
rows and the equality fails on the first pair).

The default floor is 0. That is deliberate and pinned: at 0 the depth equals the
count, the pool tracks ``limit`` exactly as it did on 2.5, and no ``depth`` key
appears in the response -- every golden recorded before the depth existed stays
byte-identical. The number the floor should default to is a measurement (the LMEB
depth sweep), not a constant chosen here.
"""

import os
import tempfile

os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "test_recall_depth.db"))
os.environ.setdefault("CPERSONA_EMBEDDING_MODE", "none")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from cpersona import config  # noqa: E402
from cpersona import memory_handlers as M  # noqa: E402
from cpersona.database import get_db  # noqa: E402

AGENT = "agent.recall-depth"
QUERY = "rollback"
SEEDED = 30
COUNTS = (3, 5, 10)
FLOOR = 50


@pytest_asyncio.fixture(autouse=True)
async def _fresh_db():
    db = await get_db()
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM episodes")
    await db.commit()
    yield


async def _seed() -> None:
    """``SEEDED`` distinct memories that all answer ``QUERY`` through the keyword arm.

    With embeddings off the vector arm is skipped and no episode is stored, so the
    keyword arm is the whole pool: its top-K IS the depth, which is what makes the
    pool size a direct reading of the number the fusion was handed.
    """
    for i in range(SEEDED):
        out = await M.do_store(
            AGENT,
            {"content": f"note {i}: rollback of the billing deploy, ticket {1000 + i}", "source": {"System": "t"}},
        )
        assert out["result"] == "stored", out


def _spy_pool(monkeypatch) -> list[set]:
    """Capture the candidate ids the fusion produced, before the quality gate.

    The gate is the first consumer of the fused list inside ``do_recall``; spying on
    it reads the pool the fusion considered without depending on what the gate or
    ``limit`` later keep (the same seam ``test_bug216_episode_only_pool`` reads).
    """
    seen: list[set] = []
    real = M._apply_quality_gate

    def spy(results, *args, **kwargs):
        seen.append({r["id"] for r in results if isinstance(r.get("id"), int) and r["id"] > 0})
        return real(results, *args, **kwargs)

    monkeypatch.setattr(M, "_apply_quality_gate", spy)
    return seen


@pytest.mark.asyncio
async def test_count_alone_does_not_move_the_pool(monkeypatch):
    """The invariant from section 4: same query, same floor, three counts, one pool."""
    await _seed()
    monkeypatch.setattr(config, "RECALL_DEPTH_FLOOR", FLOOR)
    pools = _spy_pool(monkeypatch)
    returned = []
    for count in COUNTS:
        out = await M.do_recall(AGENT, QUERY, limit=count)
        returned.append(len(out["messages"]))
    assert len(pools) == len(COUNTS)
    # The fixture must be able to show a difference: a pool no larger than the
    # smallest count would be equal under the recoupled mutation too.
    assert len(pools[0]) > max(COUNTS), f"pool {len(pools[0])} cannot distinguish the counts"
    assert len(pools[0]) == SEEDED, "the floor should reach every seeded row"
    assert pools[0] == pools[1] == pools[2], [len(p) for p in pools]
    # The count still bounds what comes back; it just no longer bounds what was seen.
    assert all(n <= c for n, c in zip(returned, COUNTS)), (returned, COUNTS)


@pytest.mark.asyncio
async def test_default_floor_keeps_the_pool_tracking_the_count(monkeypatch):
    """Floor 0 is the 2.5 coupling, bit for bit: the pool is the count and no key is added."""
    await _seed()
    assert config.RECALL_DEPTH_FLOOR == 0, "the shipped default is decided by the depth sweep, not here"
    pools = _spy_pool(monkeypatch)
    for count in COUNTS:
        out = await M.do_recall(AGENT, QUERY, limit=count)
        assert "depth" not in out, out.keys()
    assert [len(p) for p in pools] == list(COUNTS)


@pytest.mark.asyncio
async def test_response_reports_the_depth_only_when_it_exceeds_the_count(monkeypatch):
    await _seed()
    monkeypatch.setattr(config, "RECALL_DEPTH_FLOOR", FLOOR)
    out = await M.do_recall(AGENT, QUERY, limit=5)
    assert out["depth"] == FLOOR
    # A count at or above the floor is its own depth, and there is nothing to add.
    out = await M.do_recall(AGENT, QUERY, limit=FLOOR)
    assert "depth" not in out
    out = await M.do_recall(AGENT, QUERY, limit=FLOOR + 5)
    assert "depth" not in out


def test_depth_never_drops_below_the_count_and_never_exceeds_the_library_ceiling(monkeypatch):
    monkeypatch.setattr(config, "RECALL_DEPTH_FLOOR", 0)
    assert M._recall_depth(7) == 7
    monkeypatch.setattr(config, "RECALL_DEPTH_FLOOR", 40)
    assert M._recall_depth(7) == 40
    assert M._recall_depth(200) == 200
    monkeypatch.setattr(config, "RECALL_DEPTH_FLOOR", M.RECALL_LIBRARY_MAX_LIMIT * 10)
    assert M._recall_depth(7) == M.RECALL_LIBRARY_MAX_LIMIT


@pytest.mark.asyncio
async def test_cascade_is_untouched_by_the_floor(monkeypatch):
    """Cascade fills ``limit`` slots stage by stage and fuses nothing: no list to deepen."""
    await _seed()
    monkeypatch.setattr(config, "RECALL_DEPTH_FLOOR", FLOOR)
    monkeypatch.setattr(M, "RECALL_MODE", "cascade")
    pools = _spy_pool(monkeypatch)
    for count in COUNTS:
        await M.do_recall(AGENT, QUERY, limit=count)
    assert [len(p) for p in pools] == list(COUNTS)
