"""Locks for the per-question-type LongMemEval reader (benchmarks/longmemeval_by_type.py).

The reader is arithmetic over files, so what can go wrong is the files: a dump
from the other regime placed under this regime's name, a dump that is not the
dump of the recorded measurement, a dump whose header never recorded the gate
regime. Each refusal is asserted by planting exactly that defect on an
otherwise valid arm, because a refusal never seen red is a refusal observed
only green. The arithmetic is checked against hand-computed values on a
three-query type, not against the reader's own functions.
"""

import importlib.util
import json
import math
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

FULL = {"recall_limit": 0, "autocut_enabled_effective": False, "fused_gate_enabled_effective": False}
LIMIT10 = {"recall_limit": 10, "autocut_enabled_effective": True, "fused_gate_enabled_effective": True}


def _load():
    spec = importlib.util.spec_from_file_location(
        "longmemeval_by_type", ROOT / "benchmarks" / "longmemeval_by_type.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def lmeb_dir(tmp_path):
    """A LongMemEval task with two question types and hand-sized qrels."""
    task = tmp_path / "lmeb" / "eval_data" / "Dialogue" / "LongMemEval"
    (task / "temporal_reasoning").mkdir(parents=True)
    (task / "knowledge_update").mkdir(parents=True)
    # q1: one relevant doc; q2: two relevant docs; q3: one relevant doc.
    (task / "temporal_reasoning" / "qrels.tsv").write_text(
        "q1\td_a\t1\nq2\td_b\t1\nq2\td_c\t1\nq3\td_d\t1\n", encoding="utf-8"
    )
    (task / "knowledge_update" / "qrels.tsv").write_text("k1\td_k\t1\n", encoding="utf-8")
    return tmp_path / "lmeb"


# Rankings chosen so every metric is computable by hand:
#   q1: d_a at rank 1            -> ndcg 1, r5 1, r10 1
#   q2: d_b at rank 2, d_c at 7  -> ndcg (1/log2(3) + 1/log2(8)) / (1 + 1/log2(3)), r5 0.5, r10 1
#   q3: d_d at rank 12           -> ndcg 0, r5 0, r10 0
#   k1: d_k at rank 4            -> ndcg 1/log2(5), r5 1, r10 1
RANKED = {
    "temporal_reasoning": {
        "q1": ["d_a"] + [f"x{i}" for i in range(11)],
        "q2": ["x0", "d_b", "x1", "x2", "x3", "x4", "d_c"] + [f"y{i}" for i in range(5)],
        "q3": [f"z{i}" for i in range(11)] + ["d_d"],
    },
    "knowledge_update": {"k1": ["x0", "x1", "x2", "d_k"] + [f"w{i}" for i in range(8)]},
}
Q2_NDCG = (1 / math.log2(3) + 1 / math.log2(8)) / (1 + 1 / math.log2(3))
EXPECT = {
    "temporal_reasoning": {"ndcg10": (1 + Q2_NDCG + 0) / 3 * 100, "r5": (1 + 0.5 + 0) / 3 * 100, "r10": (1 + 1 + 0) / 3 * 100},
    "knowledge_update": {"ndcg10": 1 / math.log2(5) * 100, "r5": 100.0, "r10": 100.0},
}


def _write_arm(arm_dir: Path, regime: str, header: dict, ranked=RANKED, recorded=None):
    d = arm_dir / regime
    d.mkdir(parents=True, exist_ok=True)
    subtasks = recorded or {
        qtype: round(EXPECT[qtype]["ndcg10"], 2) for qtype in ranked
    }
    (d / "LongMemEval.json").write_text(
        json.dumps({"task": "LongMemEval", "mean_ndcg_at_10": 0.0, "subtasks": subtasks}),
        encoding="utf-8",
    )
    with (d / "rankings.jsonl").open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"header": True, **header}) + "\n")
        for qtype, by_q in ranked.items():
            for qid, ids in by_q.items():
                fh.write(json.dumps({
                    "task": "LongMemEval", "subtask": qtype, "query_id": qid,
                    "returned_ids": ids[:20], "filtered_ids": ids[:20], "rows": [],
                }) + "\n")
    return d


@pytest.fixture
def qrels(lmeb_dir):
    mod = _load()
    task = lmeb_dir / "eval_data" / "Dialogue" / "LongMemEval"
    return mod, {d.name: mod.load_qrels(d / "qrels.tsv") for d in sorted(task.iterdir())}


def test_metrics_match_hand_computed_values(tmp_path, qrels):
    mod, by_type = qrels
    d = _write_arm(tmp_path / "arm", "limit10", LIMIT10)
    scored = mod.score_arm(d, "limit10", by_type)
    for qtype, want in EXPECT.items():
        for metric, value in want.items():
            assert scored[qtype][metric] == pytest.approx(value, abs=1e-9), (qtype, metric)
    assert scored["temporal_reasoning"]["n"] == 3
    assert scored["knowledge_update"]["n"] == 1
    # The macro mean is over types, not over queries.
    assert scored["macro mean"]["r5"] == pytest.approx(
        (EXPECT["temporal_reasoning"]["r5"] + EXPECT["knowledge_update"]["r5"]) / 2
    )


def test_refuses_a_dump_from_the_other_regime(tmp_path, qrels):
    mod, by_type = qrels
    # A limit=10 dump filed under `full`: the header says recall_limit=10.
    d = _write_arm(tmp_path / "arm", "full", LIMIT10)
    with pytest.raises(SystemExit, match="claims regime `full`.*recall_limit=10"):
        mod.score_arm(d, "full", by_type)


def test_refuses_a_limit10_dump_with_the_gate_off(tmp_path, qrels):
    mod, by_type = qrels
    d = _write_arm(tmp_path / "arm", "limit10", {**LIMIT10, "fused_gate_enabled_effective": False})
    with pytest.raises(SystemExit, match="fused_gate_enabled_effective=False"):
        mod.score_arm(d, "limit10", by_type)


def test_refuses_a_header_that_never_recorded_the_gate(tmp_path, qrels):
    mod, by_type = qrels
    header = dict(LIMIT10)
    del header["autocut_enabled_effective"]
    d = _write_arm(tmp_path / "arm", "limit10", header)
    with pytest.raises(SystemExit, match="does not record `autocut_enabled_effective`"):
        mod.score_arm(d, "limit10", by_type)


def test_full_regime_does_not_constrain_the_gate(tmp_path, qrels):
    """A full-ranking dump taken with the gates on is still full-ranking."""
    mod, by_type = qrels
    d = _write_arm(tmp_path / "arm", "full", {**FULL, "fused_gate_enabled_effective": True})
    assert mod.score_arm(d, "full", by_type)["temporal_reasoning"]["n"] == 3


def test_refuses_a_dump_that_does_not_reproduce_the_record(tmp_path, qrels):
    mod, by_type = qrels
    recorded = {qtype: round(EXPECT[qtype]["ndcg10"], 2) for qtype in RANKED}
    recorded["temporal_reasoning"] += 0.5
    d = _write_arm(tmp_path / "arm", "limit10", LIMIT10, recorded=recorded)
    with pytest.raises(SystemExit, match="not the dump of this measurement"):
        mod.score_arm(d, "limit10", by_type)


def test_refuses_a_dump_without_filtered_ids(tmp_path, qrels):
    mod, by_type = qrels
    d = _write_arm(tmp_path / "arm", "limit10", LIMIT10)
    path = d / "rankings.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[1])
    del rec["filtered_ids"]
    lines[1] = json.dumps(rec)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="no `filtered_ids`"):
        mod.score_arm(d, "limit10", by_type)


def test_render_takes_deltas_against_the_first_arm(tmp_path, qrels):
    mod, by_type = qrels
    a = mod.score_arm(_write_arm(tmp_path / "a", "limit10", LIMIT10), "limit10", by_type)
    worse = {
        "temporal_reasoning": {**RANKED["temporal_reasoning"], "q1": ["x0", "d_a"] + [f"x{i}" for i in range(1, 11)]},
        "knowledge_update": RANKED["knowledge_update"],
    }
    recorded = {
        "temporal_reasoning": round((1 / math.log2(3) + Q2_NDCG + 0) / 3 * 100, 2),
        "knowledge_update": round(EXPECT["knowledge_update"]["ndcg10"], 2),
    }
    b = mod.score_arm(
        _write_arm(tmp_path / "b", "limit10", LIMIT10, ranked=worse, recorded=recorded), "limit10", by_type
    )
    table = mod.render("limit10", [("ref", a), ("cand", b)])
    assert "ΔNDCG@10 vs ref (cand)" in table
    row = next(line for line in table.splitlines() if line.startswith("| `temporal_reasoning`"))
    cells = [c.strip() for c in row.strip("|").split("|")]
    delta = float(cells[-3])
    assert delta == pytest.approx((1 / math.log2(3) - 1) / 3 * 100, abs=0.011)
    assert delta < 0
