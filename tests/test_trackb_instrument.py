"""The Track B runner's depth check and dev/test split.

These are instrument code: a check that always passes, or a split that scores
the held-out half as zeros, would turn a measurement into a wrong number with
nothing to show for it. The tests pin both halves of each behaviour -- that
the check fires on a wrong depth and stays quiet on a right one, and that the
split both narrows the queries and narrows what they are scored against.

See benchmarks/measurements/prereg-recall-depth-floor-sweep.md.
"""

from __future__ import annotations

import importlib.util
import json
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "benchmarks"


def _load(name: str, filename: str):
    if str(BENCH) not in sys.path:
        sys.path.insert(0, str(BENCH))
    spec = importlib.util.spec_from_file_location(name, BENCH / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ti = _load("trackb_instrument", "trackb_instrument.py")
runner = _load("benchmark_trackb_lmeb_under_test", "benchmark_trackb_lmeb.py")


# --------------------------------------------------------------------------- DepthCheck.expected


@pytest.mark.parametrize(
    "floor, ceiling, mode, limit, query, want",
    [
        (0, 10000, "rrf", 10, "q", 10),        # shipped default: depth == limit
        (50, 10000, "rrf", 10, "q", 50),       # floor above the count raises the depth
        (5, 10000, "rrf", 10, "q", 10),        # floor below the count does nothing
        (10, 10000, "rrf", 10, "q", 10),       # floor equal to the count does nothing
        (500, 100, "rrf", 10, "q", 100),       # the library ceiling clamps the depth
        (50, 10000, "rsf", 10, "q", 50),       # rsf fuses too
        (50, 10000, "cascade", 10, "q", 10),   # cascade fuses nothing: no depth
        (50, 10000, "rrf", 10, "   ", 10),     # an empty query takes no fusion path
        (0, 100, "rrf", 500, "q", 100),        # the limit itself is clamped first
        (0, 100, "cascade", 500, "q", 100),    # ... on every path
    ],
)
def test_expected_depth(floor, ceiling, mode, limit, query, want):
    assert ti.DepthCheck(floor, ceiling, mode).expected(limit, query) == want


# --------------------------------------------------------------------------- DepthCheck.observe


def test_absent_depth_key_is_a_mismatch_when_a_floor_was_intended():
    # The failure this check exists for: the package ranked at the default
    # (so it reported no depth) while the run meant floor 50.
    dc = ti.DepthCheck(50, 10000, "rrf")
    dc.observe(10, "q", {"messages": []}, query_id="a")
    assert dc.checked == 1
    assert dc.mismatches == 1
    assert dc.examples == [{"query_id": "a", "limit": 10, "expected": 50, "reported": 10}]


def test_reported_depth_that_matches_is_not_a_mismatch():
    dc = ti.DepthCheck(50, 10000, "rrf")
    dc.observe(10, "q", {"depth": 50})
    assert (dc.checked, dc.mismatches) == (1, 0)


def test_absent_depth_key_matches_when_no_floor_was_intended():
    dc = ti.DepthCheck(0, 10000, "rrf")
    dc.observe(10, "q", {"messages": []})
    assert (dc.checked, dc.mismatches) == (1, 0)


def test_a_limit_above_the_ceiling_is_not_a_false_mismatch():
    # The full-ranking convention passes limit=corpus_size. When that exceeds the
    # ceiling the package clamps the limit, the depth equals the clamped limit,
    # and no `depth` key is reported. That run did what it was asked.
    dc = ti.DepthCheck(0, 100, "rrf")
    dc.observe(500, "q", {"messages": []})
    assert (dc.checked, dc.mismatches) == (1, 0)


def test_a_depth_nobody_asked_for_is_a_mismatch():
    dc = ti.DepthCheck(0, 10000, "rrf")
    dc.observe(10, "q", {"depth": 50})
    assert dc.mismatches == 1


def test_examples_are_capped_but_every_mismatch_is_counted():
    dc = ti.DepthCheck(50, 10000, "rrf", max_examples=2)
    for i in range(5):
        dc.observe(10, "q", {}, query_id=str(i))
    assert dc.mismatches == 5
    assert [e["query_id"] for e in dc.examples] == ["0", "1"]


def test_from_env_reads_floor_and_ceiling(monkeypatch):
    monkeypatch.setenv("CPERSONA_RECALL_DEPTH_FLOOR", "200")
    monkeypatch.setenv("CPERSONA_RECALL_LIBRARY_MAX_LIMIT", "300000")
    dc = ti.DepthCheck.from_env("rrf")
    assert (dc.floor, dc.ceiling, dc.mode) == (200, 300000, "rrf")


def test_from_env_defaults_match_the_package_defaults(monkeypatch):
    monkeypatch.delenv("CPERSONA_RECALL_DEPTH_FLOOR", raising=False)
    monkeypatch.delenv("CPERSONA_RECALL_LIBRARY_MAX_LIMIT", raising=False)
    dc = ti.DepthCheck.from_env("rrf")
    assert (dc.floor, dc.ceiling) == (0, 10000)


def test_a_mismatch_marks_the_task_result_invalid():
    dc = ti.DepthCheck(50, 10000, "rrf")
    dc.observe(10, "q", {})
    result = {"mean_ndcg_at_10": 42.0}
    ti.record_depth_check(result, dc)
    assert result["invalid"] is True
    assert "1 of 1 recalls" in result["invalid_reason"]
    assert result["depth_check"]["mismatches"] == 1


def test_a_clean_check_leaves_the_result_valid():
    dc = ti.DepthCheck(50, 10000, "rrf")
    dc.observe(10, "q", {"depth": 50})
    result = {"mean_ndcg_at_10": 42.0}
    ti.record_depth_check(result, dc)
    assert "invalid" not in result and "invalid_reason" not in result
    assert result["depth_check"]["checked"] == 1


# --------------------------------------------------------------------------- split_queries


IDS = [f"q{i:03d}" for i in range(101)]


def test_split_is_a_partition_with_dev_the_smaller_half():
    a = ti.split_queries(IDS, 20260925, "sub")
    assert set(a) == set(IDS)
    parts = [a[q] for q in IDS]
    assert parts.count("dev") == 50
    assert parts.count("test") == 51


def test_split_is_reproducible_and_ignores_input_order():
    shuffled = IDS[:]
    random.Random(1).shuffle(shuffled)
    assert ti.split_queries(IDS, 20260925, "sub") == ti.split_queries(shuffled, 20260925, "sub")


def test_split_depends_on_seed_and_on_subtask():
    base = ti.split_queries(IDS, 20260925, "sub")
    assert ti.split_queries(IDS, 20260926, "sub") != base
    assert ti.split_queries(IDS, 20260925, "other") != base


def test_split_is_not_a_prefix_cut():
    # A split that put the first half of the sorted ids in dev would pass the
    # partition test; a shuffle must not.
    a = ti.split_queries(IDS, 20260925, "sub")
    assert [q for q in IDS if a[q] == "dev"] != IDS[:50]


# --------------------------------------------------------------------------- run_subtask wiring


class _FakeModel:
    def encode(self, texts, **_kw):
        return [[0.0] for _ in texts]


class _FakeEmb:
    def preload(self, *_a, **_kw):
        pass


class _FakeServer:
    """do_recall that returns each query's own relevant doc, and a depth."""

    def __init__(self, depth=None):
        self.depth = depth
        self.calls: list[tuple[str, int]] = []

    async def do_recall(self, agent_id, query, limit):
        self.calls.append((query, limit))
        # do_recall returns rows least-relevant first; one row is enough.
        res = {"messages": [{"id": f"d-{query}"}]}
        if self.depth is not None:
            res["depth"] = self.depth
        return res


def _subtask(tmp_path: Path, n: int = 20) -> dict:
    q = tmp_path / "queries.jsonl"
    q.write_text("\n".join(json.dumps({"id": f"q{i}", "text": f"t{i}"}) for i in range(n)) + "\n")
    r = tmp_path / "qrels.tsv"
    r.write_text("".join(f"q{i}\td-t{i}\t1\n" for i in range(n)))
    return {"name": "sub", "queries": str(q), "qrels": str(r), "candidates": None}


@pytest.mark.asyncio
async def test_split_scores_only_the_kept_half_and_records_the_assignment(tmp_path):
    st = _subtask(tmp_path)
    server = _FakeServer()
    sink: dict = {}
    ndcg = await runner.run_subtask(
        server, _FakeEmb(), _FakeModel(), st, corpus_size=20, recall_limit=10,
        split=(7, "dev"), split_sink=sink,
    )
    assignment = sink["sub"]
    dev = {q for q, part in assignment.items() if part == "dev"}
    assert len(dev) == 10 and len(assignment) == 20
    # Only the dev half was recalled ...
    assert {f"q{t[1:]}" for t, _ in server.calls} == dev
    # ... and every recalled query found its doc, so the score is perfect. Had the
    # held-out half stayed in qrels it would have scored 0 and pulled this to 50.
    assert ndcg == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_without_split_every_query_is_recalled(tmp_path):
    st = _subtask(tmp_path)
    server = _FakeServer()
    ndcg = await runner.run_subtask(server, _FakeEmb(), _FakeModel(), st, corpus_size=20, recall_limit=10)
    assert len(server.calls) == 20
    assert ndcg == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_depth_check_sees_both_passes(tmp_path):
    st = _subtask(tmp_path, n=4)
    server = _FakeServer(depth=50)
    dc = ti.DepthCheck(50, 10000, "rrf")
    await runner.run_subtask(
        server, _FakeEmb(), _FakeModel(), st, corpus_size=4, recall_limit=10,
        latencies_limit10=[], depth_check=dc,
    )
    assert dc.checked == 8          # 4 NDCG-pass recalls + 4 latency-pass recalls
    assert dc.mismatches == 0


@pytest.mark.asyncio
async def test_depth_check_catches_a_package_that_ignored_the_floor(tmp_path):
    st = _subtask(tmp_path, n=4)
    server = _FakeServer(depth=None)     # ranked at the default, reported nothing
    dc = ti.DepthCheck(50, 10000, "rrf")
    await runner.run_subtask(
        server, _FakeEmb(), _FakeModel(), st, corpus_size=4, recall_limit=10, depth_check=dc,
    )
    assert dc.mismatches == 4


@pytest.mark.asyncio
async def test_latency_pass_runs_at_the_requested_window_and_restores_it(tmp_path, monkeypatch):
    import types

    seen: list[int] = []
    vec = types.ModuleType("cpersona.vector")
    vec.MAX_MEMORIES = 300000
    monkeypatch.setitem(sys.modules, "cpersona.vector", vec)

    class _WindowServer(_FakeServer):
        async def do_recall(self, agent_id, query, limit):
            seen.append(vec.MAX_MEMORIES)
            return await super().do_recall(agent_id, query, limit)

    st = _subtask(tmp_path, n=3)
    await runner.run_subtask(
        _WindowServer(), _FakeEmb(), _FakeModel(), st, corpus_size=3, recall_limit=10,
        latencies_limit10=[], latency_scan_window=10000,
    )
    assert seen == [300000] * 3 + [10000] * 3   # NDCG pass wide, latency pass as asked
    assert vec.MAX_MEMORIES == 300000           # restored afterwards
