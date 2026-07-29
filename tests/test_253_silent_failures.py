"""bug-188 / bug-189: two silent failures in the 2.5.3 pre-existing sweep.

Neither raises, neither logs, and both report success while losing data — the
class the sweep exists to close.

- bug-188: an unbounded profile write. Whitespace overwrote a good profile and
  reported it as an update; an oversized profile was stored verbatim and then
  injected into every recall response.
- bug-189: the calibration sidecar was a snapshot of process state, so
  calibrating one agent dropped every other agent's threshold from the file.
"""

import json

import pytest
import pytest_asyncio

from cpersona import admin_handlers, config
from cpersona._vendored_mcp_common import no_persist
from cpersona.config import MAX_CONTENT_LENGTH
from cpersona.database import get_db
from cpersona.utils import SCORING_VERSION

AGENT = "sweep-agent"


@pytest_asyncio.fixture
async def db():
    no_persist.resume()
    conn = await get_db()
    for table in ("memories", "episodes", "profiles"):
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()
    return conn


async def _profile_of(conn, agent_id: str) -> str | None:
    rows = await conn.execute_fetchall(
        "SELECT content FROM profiles WHERE agent_id = ?", (agent_id,)
    )
    return rows[0][0] if rows else None


# ---------------------------------------------------------------------------
# bug-188 — the profile write path is bounded like every other text path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("blank", ["   ", "\n\n", "\t "])
async def test_whitespace_profile_does_not_destroy_the_stored_one(db, blank):
    """The defect in one assertion: this used to blank the profile and say 1."""
    await admin_handlers.do_update_profile(AGENT, "a genuinely useful profile")

    result = await admin_handlers.do_update_profile(AGENT, blank)

    assert result["profiles_updated"] == 0
    assert result.get("reason")
    assert await _profile_of(db, AGENT) == "a genuinely useful profile"


@pytest.mark.asyncio
async def test_empty_profile_says_why_it_did_nothing(db):
    result = await admin_handlers.do_update_profile(AGENT, "")

    assert result == {"ok": True, "profiles_updated": 0, "reason": "empty profile"}
    assert await _profile_of(db, AGENT) is None


@pytest.mark.asyncio
async def test_oversized_profile_is_truncated_and_says_so(db):
    """Unbounded here meant unbounded in every recall response downstream."""
    result = await admin_handlers.do_update_profile(AGENT, "x" * (MAX_CONTENT_LENGTH + 5_000))

    assert result["profiles_updated"] == 1
    assert result.get("truncated") is True
    stored = await _profile_of(db, AGENT)
    assert len(stored) <= MAX_CONTENT_LENGTH


@pytest.mark.asyncio
async def test_ordinary_profile_is_stored_unchanged(db):
    """The paired direction — bounding must not mangle a normal write."""
    text = "Prefers terse answers.\n\nWorks in Rust and Python."

    result = await admin_handlers.do_update_profile(AGENT, text)

    assert result["profiles_updated"] == 1
    assert "truncated" not in result
    assert await _profile_of(db, AGENT) == text


# ---------------------------------------------------------------------------
# bug-189 — calibrating one agent must not delete the others from the sidecar
# ---------------------------------------------------------------------------


@pytest.fixture
def sidecar(tmp_path, monkeypatch):
    """Point the sidecar at a temp path and seed it with two agents."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "cpersona.db"))
    path = str(tmp_path / "cpersona.db") + ".calibration.json"
    seed = {
        "embedding_dim": 1024,
        "embedding_model": "bge-m3",
        "scoring_version": SCORING_VERSION,
        "global_threshold": 0.55,
        "agent_thresholds": {"alice": 0.61, "bob": 0.58},
        "global_fused_gate": None,
        "agent_fused_gates": {"alice": 0.40, "bob": 0.33},
        "fused_gate_signal": "confidence",
        "agent_betas": {"alice": 1.5},
        "calibrated_at": "2026-07-01T00:00:00+00:00",
    }
    with open(path, "w") as fh:
        json.dump(seed, fh)
    return path


def test_stored_entries_are_carried_forward(sidecar):
    carried = admin_handlers._stored_agent_maps_to_carry(1024)

    assert carried["agent_thresholds"] == {"alice": 0.61, "bob": 0.58}
    assert carried["agent_fused_gates"] == {"alice": 0.40, "bob": 0.33}
    assert carried["agent_betas"] == {"alice": 1.5}


def test_a_measured_agent_overlays_the_stored_value(sidecar):
    """The process that just measured an agent holds the better number."""
    carried = admin_handlers._stored_agent_maps_to_carry(1024)
    merged = {**carried["agent_thresholds"], **{"alice": 0.70}}

    assert merged == {"alice": 0.70, "bob": 0.58}


@pytest.mark.parametrize(
    "field, value",
    [("embedding_dim", 768), ("scoring_version", "some-older-scoring")],
)
def test_stale_measurements_are_not_carried(sidecar, field, value):
    """bug-184 declines to RESTORE these; the write path must not smuggle them back.

    A dimension change means different vectors, a scoring change means a
    different quantity. Either way the stored numbers describe something the
    runtime no longer produces, and carrying them forward would launder them
    into a sidecar that looks freshly measured.
    """
    state = json.load(open(sidecar))
    state[field] = value
    with open(sidecar, "w") as fh:
        json.dump(state, fh)

    assert admin_handlers._stored_agent_maps_to_carry(1024) == {}


def test_no_sidecar_yet_carries_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "fresh.db"))

    assert admin_handlers._stored_agent_maps_to_carry(1024) == {}


def test_purge_still_removes_an_agent(sidecar):
    """The merge must not resurrect what a purge deliberately dropped.

    _purge_agent_calibration rewrites the payload from the file it just read, and
    it must keep doing exactly that — carrying stored entries in there would make
    dropping an agent impossible.
    """
    import inspect

    source = inspect.getsource(admin_handlers._purge_agent_calibration)

    assert "_stored_agent_maps_to_carry" not in source
