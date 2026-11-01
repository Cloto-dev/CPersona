"""The dashboard's recall log pages (2.6.17): the logged calls and one call's trace.

The store is the sample the accessibility check uses (scripts/dashboard-fixture.py),
which logs a recall and a reconstruct through the tools' own boundaries. The pages
read the log as written and never re-run a recall; a viewer of a shared server sees
only the rows of the agents they read.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from cpersona import acl
from cpersona.dashboard import store
from cpersona.dashboard.scope import Scope

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def sample(tmp_path_factory):
    path = tmp_path_factory.mktemp("recalls") / "sample.db"
    env = {**os.environ, "CPERSONA_UPDATE_CHECK": "false", "CPERSONA_DASHBOARD_STORES": "false", "CPERSONA_OPERATING_CONTEXT": "off"}
    env.pop("CPERSONA_RECALL_LOG", None)
    subprocess.run([sys.executable, str(ROOT / "scripts" / "dashboard-fixture.py"), str(path)], check=True, env=env, cwd=ROOT)
    return str(path)


def test_the_logged_calls_are_listed_newest_first(sample):
    out = store.recalls(sample)
    assert out["exists"] is True and out["total"] == 2
    assert [r["tool"] for r in out["rows"]] == ["reconstruct", "recall"]
    recall = out["rows"][1]
    assert recall["query"] == "garden plan tomatoes" and recall["agent_id"] == "claude-code" and recall["returned"] == 2
    assert "trace" not in recall  # the list carries no trace


def test_text_narrows_by_query_and_a_reference_by_the_trace(sample):
    assert [r["query"] for r in store.recalls(sample, query="GARDEN")["rows"]] == ["garden plan tomatoes"]
    by_ref = store.recalls(sample, query="mem:1")["rows"]
    assert by_ref and all("returned_ref" in r for r in by_ref)
    # mem:1 (the Caroline memory) is what the reconstruct returned; the garden recall
    # did not return it.
    assert {r["query"]: r["returned_ref"] for r in by_ref}.get("Caroline Malmö studio") is True
    assert store.recalls(sample, query="mem:999")["rows"] == []


def test_one_call_carries_its_trace_and_the_records_it_names(sample):
    conn = store.open_store(sample)
    try:
        entry = store.recall(conn, sample, None, 1)
    finally:
        conn.close()
    assert entry["tool"] == "recall" and entry["returned"] == ["mem:2", "ep:2"]
    assert "arms" in entry["trace"]
    garden = entry["records"]["mem:2"]
    assert garden["missing"] is False and garden["text"].startswith("The garden plan")


def test_a_viewer_sees_only_the_agents_they_read(sample):
    others = Scope({"codex": acl.PERM_READ})
    assert store.recalls(sample, others)["rows"] == []
    conn = store.open_store(sample)
    try:
        assert store.recall(conn, sample, others, 1) is None
        mine = Scope({"claude-code": acl.PERM_READ})
        assert store.recall(conn, sample, mine, 1)["tool"] == "recall"
    finally:
        conn.close()
    assert store.recalls(sample, mine)["total"] == 2


def test_a_store_without_a_log_says_so(tmp_path, sample):
    other = tmp_path / "copy.db"
    other.write_bytes(Path(sample).read_bytes())
    assert store.recalls(str(other)) == {"exists": False, "rows": [], "total": 0}
    conn = store.open_store(str(other))
    try:
        assert store.recall(conn, str(other), None, 1) is None
    finally:
        conn.close()
