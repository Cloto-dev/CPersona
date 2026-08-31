"""bug-257: episode rows are exempt from the episode-boundary penalty.

The 2.5.5a1 created_at fallback (bug-213) gave every historical episode a real
timestamp, which silently pushed the NULL-start_time majority of episodes into
the cross-session boundary penalty — their fused scores saturated at
EPISODE_DECAY_FLOOR (0.5), exactly halving, and the session-start grounding
rows the fallback exists to surface were the rows demoted.

Owner ruling 2026-08-22 picked option (a) of the registry's design fork: exempt
episode rows from the penalty entirely. The penalty's charter is to weaken
cross-session MEMORIES so current-session signals take precedence; an episode
is a cross-session summary by construction, and its retrieval purpose is
exactly the cross-session grounding the penalty suppresses. The exemption is
uniform: dated episodes — penalised even before 2.5.5a1 — are exempt too,
closing the dated-punished / undated-exempt incoherence the same way bug-213
closed it on the confidence path.

These tests pin the CALL-SITE membership change in ``_apply_recall_scoring``.
The scoring functions themselves are untouched, so the 255a1 fingerprint grid
(test_255a1_scoring_fingerprint.py) deliberately does not move; the
SCORING_VERSION bump for this change is carried by GOLDEN_SCORING_VERSION
there, and the behaviour lives here.
"""

import pytest
import pytest_asyncio

from cpersona import session
from cpersona import config, memory_handlers
from cpersona.database import get_db

AGENT = "ep-exempt-agent"

# Far enough behind any fresh boundary that the decay clamps at the floor,
# making the expected penalised score exact (score * EPISODE_DECAY_FLOOR).
ANCIENT_TS = "2020-01-01T00:00:00+00:00"


@pytest_asyncio.fixture
async def clean_db():
    session.reset_pauses_for_tests()
    db = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()
    return db


async def _arm_boundary(db, agent_id: str) -> None:
    """Insert a fresh episode so MAX(created_at) puts every ANCIENT_TS row
    behind the boundary."""
    await db.execute(
        "INSERT INTO episodes (agent_id, summary, keywords, created_at) "
        "VALUES (?, 's', 'k', datetime('now'))",
        (agent_id,),
    )
    await db.commit()


def _scores(row: dict) -> dict:
    return {k: row.get(k) for k in ("_cosine", "_rrf_score", "_rsf_score")}


@pytest.mark.asyncio
async def test_episode_rid_row_is_exempt_memory_still_penalised(clean_db, monkeypatch):
    """An episode row (``_rid`` marker) keeps its scores verbatim while a memory
    of the same age is scaled by the decay floor — the mechanism survives, only
    the membership changed."""
    monkeypatch.setattr(memory_handlers, "CONFIDENCE_ENABLED", False)
    monkeypatch.setattr(memory_handlers, "EPISODE_PENALTY_ENABLED", True)
    await _arm_boundary(clean_db, AGENT)

    episode_row = {
        "id": 7,
        "_rid": ("ep", 7),
        "content": "old episode summary",
        "timestamp": ANCIENT_TS,
        "_cosine": 0.62,
        "_rrf_score": 0.05,
        "_rsf_score": 0.9,
    }
    memory_row = {
        "id": 8,
        "content": "old cross-session memory",
        "timestamp": ANCIENT_TS,
        "_cosine": 0.62,
        "_rrf_score": 0.05,
        "_rsf_score": 0.9,
    }
    results, _, _, _ = await memory_handlers._apply_recall_scoring(
        clean_db, AGENT, [dict(episode_row), dict(memory_row)], deep=False
    )
    by_id = {r["id"]: r for r in results}

    assert _scores(by_id[7]) == _scores(episode_row), (
        "episode row must pass the penalty block untouched"
    )
    floor = config.EPISODE_DECAY_FLOOR
    assert by_id[8]["_cosine"] == pytest.approx(0.62 * floor)
    assert by_id[8]["_rrf_score"] == pytest.approx(0.05 * floor)
    assert by_id[8]["_rsf_score"] == pytest.approx(0.9 * floor)


@pytest.mark.asyncio
async def test_episode_source_marker_row_is_exempt(clean_db, monkeypatch):
    """The other structural episode marker (dict source {'System': 'episode'},
    bug-040/041) is honoured by the exemption too."""
    monkeypatch.setattr(memory_handlers, "CONFIDENCE_ENABLED", False)
    monkeypatch.setattr(memory_handlers, "EPISODE_PENALTY_ENABLED", True)
    await _arm_boundary(clean_db, AGENT)

    episode_row = {
        "id": 3,
        "source": {"System": "episode"},
        "content": "old episode via source marker",
        "timestamp": ANCIENT_TS,
        "_rrf_score": 0.05,
    }
    results, _, _, _ = await memory_handlers._apply_recall_scoring(
        clean_db, AGENT, [dict(episode_row)], deep=False
    )
    assert results[0]["_rrf_score"] == pytest.approx(0.05), (
        "dict-source episode row must be exempt from the penalty"
    )


@pytest.mark.asyncio
async def test_dated_episode_is_exempt_uniformly(clean_db, monkeypatch):
    """A dated episode (real recorded start_time as its timestamp) was penalised
    before this change; option (a) exempts it too. This is the delta that
    distinguishes the ruling from a plain restore of the pre-2.5.5a1 accident."""
    monkeypatch.setattr(memory_handlers, "CONFIDENCE_ENABLED", False)
    monkeypatch.setattr(memory_handlers, "EPISODE_PENALTY_ENABLED", True)
    await _arm_boundary(clean_db, AGENT)

    dated_episode = {
        "id": 4,
        "_rid": ("ep", 4),
        "content": "dated episode",
        # A recorded (not fallback) start_time is indistinguishable here by
        # value — what matters is that a dated episode row no longer enters
        # the penalty branch at all.
        "timestamp": ANCIENT_TS,
        "_cosine": 0.7,
    }
    results, _, _, _ = await memory_handlers._apply_recall_scoring(
        clean_db, AGENT, [dict(dated_episode)], deep=False
    )
    assert results[0]["_cosine"] == pytest.approx(0.7)


@pytest.mark.asyncio
async def test_exempt_episode_outranks_penalised_memory_after_resort(clean_db, monkeypatch):
    """The bug-115 re-sort now expresses the exemption: a memory whose fused
    score is nominally higher drops below an exempt episode once the penalty
    halves it — the grounding rows the fallback exists to surface win again."""
    monkeypatch.setattr(memory_handlers, "CONFIDENCE_ENABLED", False)
    monkeypatch.setattr(memory_handlers, "EPISODE_PENALTY_ENABLED", True)
    await _arm_boundary(clean_db, AGENT)

    memory_row = {
        "id": 1,
        "content": "old memory, nominally stronger",
        "timestamp": ANCIENT_TS,
        "_rrf_score": 0.050,
    }
    episode_row = {
        "id": 2,
        "_rid": ("ep", 2),
        "content": "old episode, nominally weaker",
        "timestamp": ANCIENT_TS,
        "_rrf_score": 0.048,
    }
    results, _, _, _ = await memory_handlers._apply_recall_scoring(
        clean_db, AGENT, [dict(memory_row), dict(episode_row)], deep=False
    )
    assert [r["id"] for r in results] == [2, 1], (
        "exempt episode (0.048) must outrank the penalised memory (0.050 * floor)"
    )


@pytest.mark.asyncio
async def test_exempt_episode_outranks_penalised_memory_under_confidence(clean_db, monkeypatch):
    """The production configuration (confidence on) expresses the exemption too.

    With CONFIDENCE_ENABLED the ordering is owned by the confidence re-sort, not
    the bug-115 fusion re-sort, so the ranking consequence must be pinned here
    separately: the penalty halves the memory's cosine before _compute_confidence
    reads it, while the exempt episode's cosine reaches confidence intact — same
    age, same raw cosine, the episode wins.
    """
    monkeypatch.setattr(memory_handlers, "CONFIDENCE_ENABLED", True)
    monkeypatch.setattr(memory_handlers, "EPISODE_PENALTY_ENABLED", True)
    await _arm_boundary(clean_db, AGENT)

    memory_row = {
        "id": 11,
        "content": "old cross-session memory",
        "timestamp": ANCIENT_TS,
        "_cosine": 0.62,
    }
    episode_row = {
        "id": 12,
        "_rid": ("ep", 12),
        "source": {"System": "episode"},
        "content": "old episode summary",
        "timestamp": ANCIENT_TS,
        "_cosine": 0.62,
    }
    results, _, _, _ = await memory_handlers._apply_recall_scoring(
        clean_db, AGENT, [dict(memory_row), dict(episode_row)], deep=False
    )
    by_id = {r["id"]: r for r in results}

    assert by_id[12]["_cosine"] == pytest.approx(0.62), "episode cosine must reach confidence intact"
    assert by_id[11]["_cosine"] == pytest.approx(0.62 * config.EPISODE_DECAY_FLOOR)
    assert [r["id"] for r in results] == [12, 11], (
        "under confidence the exempt episode must outrank the penalised memory of equal "
        "age and raw cosine"
    )
