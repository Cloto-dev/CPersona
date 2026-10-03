"""The OmniMemEval quote curve: building answer inputs from re-run searches, and the registered reading.

No harness, server or model is needed.
"""
import importlib
import json
import sys
from pathlib import Path

import pytest

DIR = Path(__file__).resolve().parents[1] / "benchmarks" / "omnimemeval"
MODULES = ("quote_curve_build", "quote_curve_analyze", "count_curve_analyze")


def _fresh(monkeypatch, name, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    monkeypatch.syspath_prepend(str(DIR))
    for m in MODULES:
        sys.modules.pop(m, None)
    return importlib.import_module(name)


@pytest.fixture(autouse=True)
def _forget_modules():
    yield
    for m in MODULES:
        sys.modules.pop(m, None)


# --------------------------------------------------------------------------
# build


def _harness(tmp_path):
    src = tmp_path / "results/lme/cpersona-lme1"
    src.mkdir(parents=True)
    data = {f"lme_exper_user_lme1_{i}": [{"question": f"q{i}", "search_context": f"published {i}",
                                           "search_duration_ms": 1.0, "status": "success"}] for i in range(500)}
    (src / "cpersona_lme_search_results.json").write_text(json.dumps(data))
    (src / "cpersona_lme_search_status.json").write_text("{}")
    return data


def _search(tmp_path, size, contexts):
    q, t = size.split("/")
    d = tmp_path / "search" / f"q{q}-t{t}"
    d.mkdir(parents=True)
    rows = [{"i": i, "search_context": c, "search_duration_ms": 2.5} for i, c in contexts.items()]
    (d / "search.json").write_text(json.dumps(rows[::-1]))  # order must not matter


def test_build_writes_each_size_by_question(monkeypatch, tmp_path):
    _harness(tmp_path)
    _search(tmp_path, "800/400", {i: f"published {i}" for i in range(500)})
    _search(tmp_path, "400/200", {i: f"short {i}" for i in range(500)})
    B = _fresh(monkeypatch, "quote_curve_build", OMNIMEMEVAL_DIR=tmp_path)
    monkeypatch.setattr(sys, "argv", ["x", str(tmp_path / "search"), "400/200"])
    B.main()
    out = json.loads((tmp_path / "results/lme/cpersona-lme1-q400/cpersona_lme_search_results.json").read_text())
    assert out["lme_exper_user_lme1_7"][0]["search_context"] == "short 7"
    assert out["lme_exper_user_lme1_7"][0]["question"] == "q7"  # the published entry's other fields are kept
    # a number, never a string: the harness's judge stage adds it to the answer's duration
    assert type(out["lme_exper_user_lme1_7"][0]["search_duration_ms"]) is float
    assert out["lme_exper_user_lme1_7"][0]["search_duration_ms"] == 2.5
    assert (tmp_path / "results/lme/cpersona-lme1-q400/.step_2_done").exists()


def test_build_stops_when_the_published_sizes_do_not_reproduce(monkeypatch, tmp_path):
    _harness(tmp_path)
    ctxs = {i: f"published {i}" for i in range(500)}
    ctxs[499] = "changed"
    _search(tmp_path, "800/400", ctxs)
    _search(tmp_path, "400/200", {i: f"short {i}" for i in range(500)})
    B = _fresh(monkeypatch, "quote_curve_build", OMNIMEMEVAL_DIR=tmp_path)
    monkeypatch.setattr(sys, "argv", ["x", str(tmp_path / "search"), "400/200"])
    with pytest.raises(SystemExit, match="do not reproduce"):
        B.main()
    assert not (tmp_path / "results/lme/cpersona-lme1-q400").exists()


def test_build_refuses_a_missing_question_and_an_existing_output(monkeypatch, tmp_path):
    _harness(tmp_path)
    _search(tmp_path, "800/400", {i: f"published {i}" for i in range(500)})
    _search(tmp_path, "400/200", {i: f"short {i}" for i in range(499)})
    B = _fresh(monkeypatch, "quote_curve_build", OMNIMEMEVAL_DIR=tmp_path)
    monkeypatch.setattr(sys, "argv", ["x", str(tmp_path / "search"), "400/200"])
    with pytest.raises(AssertionError, match="missing"):
        B.main()
    (tmp_path / "results/lme/cpersona-lme1-q400").mkdir(parents=True, exist_ok=True)
    _search(tmp_path, "560/280", {i: f"mid {i}" for i in range(500)})
    (tmp_path / "results/lme/cpersona-lme1-q560").mkdir()
    monkeypatch.setattr(sys, "argv", ["x", str(tmp_path / "search"), "560/280"])
    with pytest.raises(SystemExit, match="exists"):
        B.main()


# --------------------------------------------------------------------------
# reading


@pytest.fixture
def A(monkeypatch, tmp_path):
    mod = _fresh(monkeypatch, "quote_curve_analyze", OMNIMEMEVAL_DIR=tmp_path,
                 QUOTE_CURVE_USAGE_DIR=tmp_path, COUNT_CURVE_USAGE_DIR=tmp_path)
    monkeypatch.setattr(sys.modules["count_curve_analyze"], "B", 2_000)  # fewer resamples: the reading, not the interval, is under test
    return mod


def _pt(correct, tokens=100.0, n=200):
    """{question: (correct, category, context tokens)}; correct is a fraction of the questions."""
    k = round(correct * n)
    return {f"u{i}": (i < k, "c", tokens) for i in range(n)}


def test_shorter_quotes_better_only_when_the_whole_interval_is_above_zero(A):
    v = A.read(_pt(0.9), _pt(0.5), _pt(0.9))
    assert v["verdict"] == "shorter quotes better" and v["lo"] > 0
    v = A.read(_pt(0.5), _pt(0.9), _pt(0.9))
    assert v["verdict"] == "fewer items better" and v["hi"] < 0
    v = A.read(_pt(0.7), _pt(0.7), _pt(0.7))
    assert v["verdict"] == "neither claimed" and v["d"] == 0


def test_point_minus_control_not_control_minus_point(A):
    v = A.read(_pt(0.8), _pt(0.6), _pt(0.8))
    assert v["d"] == pytest.approx(20.0)


def test_same_cost_is_within_five_percent_either_way(A):
    assert A.read(_pt(0.7, 105.0), _pt(0.7, 100.0), _pt(0.7))["same_cost"] is True
    assert A.read(_pt(0.7, 95.0), _pt(0.7, 100.0), _pt(0.7))["same_cost"] is True
    assert A.read(_pt(0.7, 106.0), _pt(0.7, 100.0), _pt(0.7))["same_cost"] is False
    assert A.read(_pt(0.7, 94.0), _pt(0.7, 100.0), _pt(0.7))["same_cost"] is False


def test_holds_against_every_item_by_the_count_curve_margin(A):
    assert A.read(_pt(0.80), _pt(0.5), _pt(0.80))["holds"] is True
    # slightly below every item, inside the margin: the interval reaches below 0 but not below -2.0
    v = A.read(_pt(0.999, n=1000), _pt(0.5, n=1000), _pt(1.0, n=1000))
    assert -2.0 <= v["lo_full"] < 0 and v["holds"] is True
    # a point clearly more than two points below every item does not hold
    assert A.read(_pt(0.70), _pt(0.5), _pt(0.80))["holds"] is False
