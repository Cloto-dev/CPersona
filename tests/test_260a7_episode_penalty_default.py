"""2.6.0a7: the episode-boundary penalty is off unless a deployment asks for it.

An agent that archives an episode at the end of every session puts nearly its whole
history behind the boundary, so with the penalty on every earlier memory is halved
and unrelated rows outrank it. The default is therefore off, and
CPERSONA_EPISODE_PENALTY_ENABLED=true opts back in.
"""
import os
import subprocess
import sys

import pytest

from cpersona import memory_handlers as M
from cpersona.database import get_db

AGENT = "agent.a7-penalty-default"


def _flag_in_fresh_process(value):
    env = {k: v for k, v in os.environ.items() if k != "CPERSONA_EPISODE_PENALTY_ENABLED"}
    if value is not None:
        env["CPERSONA_EPISODE_PENALTY_ENABLED"] = value
    out = subprocess.run(
        [sys.executable, "-c", "from cpersona import config; print(config.EPISODE_PENALTY_ENABLED)"],
        env=env, capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def test_the_penalty_is_off_when_nothing_is_set():
    assert _flag_in_fresh_process(None) == "False"


@pytest.mark.parametrize("value", ["true", "TRUE", "True"])
def test_a_deployment_can_opt_back_in(value):
    assert _flag_in_fresh_process(value) == "True"


@pytest.mark.parametrize("value", ["false", "0", "yes", ""])
def test_anything_but_true_leaves_it_off(value):
    assert _flag_in_fresh_process(value) == "False"


@pytest.mark.asyncio
async def test_default_recall_scoring_leaves_a_pre_boundary_memory_alone():
    """At the call site, not the config: under the default a memory older than the
    latest in-scope episode keeps its score. The opt-in arm is the control — it shows
    the same fixture does get penalised, so the default arm cannot pass vacuously."""
    assert "CPERSONA_EPISODE_PENALTY_ENABLED" not in os.environ
    db = await get_db()
    await db.execute(
        "INSERT INTO episodes (agent_id, project_id, channel, summary, created_at) VALUES (?, '', '', 'ep', ?)",
        (AGENT, "2026-07-23 08:00:00"),
    )
    await db.commit()

    def row():
        return [{"id": 1, "timestamp": "2026-07-20T09:00:00+00:00", "_rrf_score": 0.5, "source": '{"User": "x"}'}]

    default_out, *_ = await M._apply_recall_scoring(db, AGENT, row(), deep=False, project_id="", channel="")
    assert default_out[0]["_rrf_score"] == pytest.approx(0.5), default_out

    original = M.EPISODE_PENALTY_ENABLED
    M.EPISODE_PENALTY_ENABLED = True
    try:
        opted_in, *_ = await M._apply_recall_scoring(db, AGENT, row(), deep=False, project_id="", channel="")
    finally:
        M.EPISODE_PENALTY_ENABLED = original
    assert opted_in[0]["_rrf_score"] < 0.5, opted_in
