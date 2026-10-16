"""The mutation-diff lane derives its per-mutant timeout from the suite it timed.

cosmic-ray records a test command it stopped at the timeout as KILLED. A
surviving mutant runs the whole suite, so with a fixed limit the suite grows
into, survivors are counted as caught -- and the baseline, which ran under the
same fixed limit, failed outright once the suite passed it, so the lane stopped
measuring anything. The lane now times the unmutated suite once (that run is
also the baseline), runs every cosmic-ray step under a timeout of three times
that time with the configured value as a floor, and counts the mutants the clock
stopped apart from the ones a test failed on.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _load_lane():
    spec = importlib.util.spec_from_file_location("mutation_diff_timeout", REPO / "scripts" / "mutation-diff.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lane = _load_lane()
CONFIG = REPO / "cosmic-ray.toml"


def _toml(path: Path) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def test_the_timeout_is_three_times_the_measured_suite_above_the_floor():
    assert lane.mutant_timeout(120.0, 100.0) == 300.0
    assert lane.mutant_timeout(120.0, 41.0) == 123.0
    assert lane.mutant_timeout(120.0, 40.0) == 120.0
    assert lane.mutant_timeout(120.0, 2.0) == 120.0, "a short suite is not cut off by start-up noise"


def test_the_floor_is_the_value_the_config_names():
    assert lane.config_timeout(CONFIG) == _toml(CONFIG)["cosmic-ray"]["timeout"]


def test_the_effective_config_carries_the_timeout_and_changes_nothing_else(tmp_path):
    base = lane.config_git_branch(CONFIG)
    out = lane.effective_config(CONFIG, base, tmp_path, timeout=363.0)
    assert out != CONFIG
    original, effective = _toml(CONFIG), _toml(out)
    assert effective["cosmic-ray"]["timeout"] == 363.0
    original["cosmic-ray"].pop("timeout")
    effective["cosmic-ray"].pop("timeout")
    assert effective == original


def test_a_new_base_and_a_timeout_are_both_rewritten(tmp_path):
    out = lane.effective_config(CONFIG, "origin/some-base", tmp_path, timeout=300.0)
    effective = _toml(out)
    assert effective["cosmic-ray"]["timeout"] == 300.0
    assert effective["cosmic-ray"]["filters"]["git-filter"]["branch"] == "origin/some-base"


def test_with_nothing_to_rewrite_the_config_is_used_as_is(tmp_path):
    assert lane.effective_config(CONFIG, lane.config_git_branch(CONFIG), tmp_path) == CONFIG


def test_a_config_whose_timeout_cannot_be_found_is_refused(tmp_path):
    config = tmp_path / "cosmic-ray.toml"
    config.write_text(CONFIG.read_text().replace("\ntimeout = ", "\n# timeout = "))
    with pytest.raises(SystemExit, match="could not rewrite timeout"):
        lane.effective_config(config, lane.config_git_branch(config), tmp_path, timeout=300.0)


def _session_rows(tmp_path, rows):
    """rows: (line, worker, test, output) — one mutant each, on cpersona/x.py."""
    path = tmp_path / "session"
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE mutation_specs (job_id TEXT, module_path TEXT, "
        "start_pos_row INTEGER, operator_name TEXT, definition_name TEXT)"
    )
    con.execute("CREATE TABLE work_results (job_id TEXT, worker_outcome TEXT, test_outcome TEXT, output TEXT)")
    for i, (line, worker, test, output) in enumerate(rows):
        con.execute("INSERT INTO mutation_specs VALUES (?,?,?,?,?)", (str(i), "cpersona/x.py", line, "op/A", "f"))
        con.execute("INSERT INTO work_results VALUES (?,?,?,?)", (str(i), worker, test, output))
    con.commit()
    con.close()
    return path


def test_a_mutant_the_clock_stopped_is_counted_apart_from_one_a_test_failed_on(tmp_path):
    rows = [
        (1, "NORMAL", "KILLED", lane.TIMEOUT_OUTPUT),
        (2, "NORMAL", "KILLED", "FAILED tests/test_x.py::test_y"),
        (3, "NORMAL", "SURVIVED", ""),
    ]
    result = lane.classify(_session_rows(tmp_path, rows), REPO, {})
    assert result["counts"]["killed"] == 2, "cosmic-ray's verdict is kept as it is"
    assert result["killed_by_timeout"] == 1


def test_the_summary_says_how_many_kills_were_the_clock(tmp_path):
    report = {
        "status": "ok",
        "in_scope": 3,
        "counts": {"killed": 2, "survived": 1},
        "survival_rate": 1 / 3,
        "killed_by_timeout": 2,
        "timeout_seconds": 363.0,
        "survivors": [],
    }
    out = tmp_path / "summary.md"
    lane.write_summary(report, str(out))
    assert "2 of the killed mutants were stopped at the 363.0s limit" in out.read_text(encoding="utf-8")

    quiet = tmp_path / "quiet.md"
    lane.write_summary({**report, "killed_by_timeout": 0}, str(quiet))
    assert "killed by the timeout" not in quiet.read_text(encoding="utf-8")


def _config_running(tmp_path, code: str) -> Path:
    config = tmp_path / "cosmic-ray.toml"
    command = f"{sys.executable} -c '{code}'"
    config.write_text(
        "\n".join(
            line if not line.startswith("test-command") else f'test-command = "{command}"'
            for line in CONFIG.read_text().splitlines()
        )
    )
    return config


def test_the_unmutated_run_is_bounded_by_the_limit_it_is_given(tmp_path):
    seconds, note = lane.measure_mutant_cost(_config_running(tmp_path, "import time; time.sleep(5)"), limit_seconds=0.5)
    assert seconds is None
    assert "did not finish within" in note


def test_a_passing_unmutated_run_is_priced(tmp_path):
    seconds, note = lane.measure_mutant_cost(_config_running(tmp_path, "pass"), limit_seconds=60)
    assert seconds is not None and seconds < 60
    assert note.startswith("measured")


def _main(monkeypatch, tmp_path, measured):
    """Run main() with the git and cosmic-ray edges replaced; return (report, calls)."""
    calls: list[list[str]] = []
    limits: list[float | None] = []

    def fake_measure(config_path, limit_seconds=None):
        limits.append(limit_seconds)
        return measured

    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(lane, "production_diff_files", lambda base: ["cpersona/x.py"])
    monkeypatch.setattr(lane, "measure_mutant_cost", fake_measure)
    monkeypatch.setattr(lane, "run", lambda cmd, **kw: calls.append(list(cmd)))
    monkeypatch.setattr(lane, "count_pending", lambda session: 0)
    monkeypatch.setattr(lane, "apply_limit", lambda session, limit, seed: None)
    monkeypatch.setattr(
        lane,
        "classify",
        lambda session, repo, active: {
            "counts": {"not_executed": 0},
            "in_scope": 0,
            "survival_rate": None,
            "killed_by_timeout": 0,
            "line_coverage": {"mutable_lines": 0, "measured_lines": 0, "ratio": None},
            "survivors": [],
        },
    )
    out = tmp_path / "report.json"
    rc = lane.main(
        [
            "--base", lane.config_git_branch(CONFIG),
            "--json-out", str(out),
            "--session-dir", str(tmp_path / "s"),
            "--waivers", str(tmp_path / "no-waivers.json"),
        ]
    )
    assert rc == 0
    return json.loads(out.read_text(encoding="utf-8")), calls, limits


def test_a_red_unmutated_suite_stops_the_run_before_cosmic_ray_starts(monkeypatch, tmp_path):
    report, calls, _ = _main(monkeypatch, tmp_path, (None, "baseline `pytest` exited 1 in 3s"))
    assert report["status"] == "baseline-failed"
    assert "exited 1" in report["reason"]
    assert calls == []


def test_every_cosmic_ray_step_runs_under_the_derived_timeout(monkeypatch, tmp_path):
    report, calls, limits = _main(monkeypatch, tmp_path, (100.0, "measured `pytest` at 100s"))
    assert report["status"] == "ok"
    assert report["timeout_seconds"] == 300.0
    assert limits == [900], "the unmutated run is bounded by the budget"
    assert not any("baseline" in cmd for cmd in calls), "the timed run is the baseline; the suite runs unmutated once"

    steps = {cmd[0] if cmd[0] != "cosmic-ray" else f"cosmic-ray {cmd[1]}": cmd for cmd in calls}
    assert set(steps) == {"cosmic-ray init", "cr-filter-git", "cr-filter-pragma", "cr-filter-operators", "cosmic-ray exec"}
    configs = {
        "cosmic-ray init": steps["cosmic-ray init"][3],
        "cr-filter-git": steps["cr-filter-git"][2],
        "cr-filter-operators": steps["cr-filter-operators"][2],
        "cosmic-ray exec": steps["cosmic-ray exec"][2],
    }
    for step, path in configs.items():
        assert _toml(Path(path))["cosmic-ray"]["timeout"] == 300.0, step
