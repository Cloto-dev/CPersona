"""The mutation-diff lane samples over its limit instead of running nothing.

Over the cap, the lane used to skip every mutant and still report green, so the
larger the diff, the less of it was measured. It now keeps a deterministic
sample: every changed line gets a mutant before any line gets a second, the
least-used operator goes first within a line, and a seeded hash breaks ties. The
mutants left out are recorded so `cosmic-ray exec` never runs them and the report
counts them apart from the ones a filter removed.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load_lane():
    spec = importlib.util.spec_from_file_location("mutation_diff_sampling", REPO / "scripts" / "mutation-diff.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lane = _load_lane()
SEED = lane.DEFAULT_SEED


def _specs(lines: int, per_line: int, operators=("op/A",)):
    """(job_id, module, line, operator) with `per_line` mutants on each line."""
    out = []
    for line in range(1, lines + 1):
        for k in range(per_line):
            out.append((f"j{line}-{k}", "cpersona/x.py", line, operators[k % len(operators)]))
    return out


def test_everything_runs_when_it_fits():
    specs = _specs(lines=2, per_line=2)
    assert sorted(lane.choose_sample(specs, 4, SEED)) == sorted(j for j, *_ in specs)


def test_a_zero_limit_selects_nothing():
    assert lane.choose_sample(_specs(lines=2, per_line=2), 0, SEED) == []


def test_every_changed_line_gets_a_mutant_before_any_line_gets_a_second():
    specs = _specs(lines=5, per_line=3)
    lines_of = {j: line for j, _, line, _ in specs}

    chosen = lane.choose_sample(specs, 5, SEED)
    assert sorted(lines_of[j] for j in chosen) == [1, 2, 3, 4, 5]

    chosen = lane.choose_sample(specs, 7, SEED)
    counts = [sum(1 for j in chosen if lines_of[j] == line) for line in range(1, 6)]
    assert sorted(counts) == [1, 1, 1, 2, 2]


def test_the_least_used_operator_is_taken_first_within_a_line():
    """A scarce operator is not starved by a common one.

    Eight lines each offer A, A, A, B, and two mutants per line are taken. Taking
    the least-used operator first spends almost every B (all but at most the one
    a tie-break gives to A); choosing by hash alone leaves about half of them
    behind (measured: 3 to 5 of 8 across these seeds).
    """
    specs = [
        (f"j{line}-{k}", "cpersona/x.py", line, op)
        for line in range(1, 9)
        for k, op in enumerate(("op/A", "op/A", "op/A", "op/B"))
    ]
    op_of = {j: op for j, _, _, op in specs}
    for seed in (SEED, "s1", "s2", "s3"):
        ops = [op_of[j] for j in lane.choose_sample(specs, 16, seed)]
        assert ops.count("op/B") >= 7, (seed, ops.count("op/B"))


def test_the_sample_is_deterministic():
    specs = _specs(lines=20, per_line=4, operators=("op/A", "op/B", "op/C"))
    assert lane.choose_sample(specs, 17, SEED) == lane.choose_sample(list(reversed(specs)), 17, SEED)


def _session(tmp_path, n: int, filtered: int = 0) -> Path:
    path = tmp_path / "session"
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE mutation_specs (job_id TEXT, module_path TEXT, "
        "start_pos_row INTEGER, operator_name TEXT, definition_name TEXT)"
    )
    con.execute("CREATE TABLE work_results (job_id TEXT, worker_outcome TEXT, test_outcome TEXT, output TEXT)")
    for i in range(n + filtered):
        con.execute(
            "INSERT INTO mutation_specs VALUES (?,?,?,?,?)",
            (str(i), "cpersona/isolation.py", 1 + i, "core/ReplaceTrueWithFalse", "foo"),
        )
        if i >= n:  # removed by a filter before the lane looked
            con.execute("INSERT INTO work_results VALUES (?, 'SKIPPED', NULL, NULL)", (str(i),))
    con.commit()
    con.close()
    return path


def test_over_the_limit_the_left_out_mutants_are_recorded_and_counted_apart(tmp_path):
    session = _session(tmp_path, n=5, filtered=2)

    sampling = lane.apply_limit(session, 2, SEED)

    assert sampling["in_scope_total"] == 5
    assert sampling["executed"] == 2
    assert len(lane.pending_specs(session)) == 2, "only the sample may be left for cosmic-ray exec"
    result = lane.classify(session, REPO, {})
    assert result["counts"]["not_sampled"] == 3
    assert result["counts"]["skipped_by_filter"] == 2


def test_under_the_limit_nothing_is_left_out(tmp_path):
    session = _session(tmp_path, n=3)
    assert lane.apply_limit(session, 3, SEED) is None
    assert len(lane.pending_specs(session)) == 3


def test_the_summary_says_it_was_a_sample(tmp_path):
    out = tmp_path / "summary.md"
    report = {
        "status": "sampled",
        "reason": "r",
        "base": "origin/master",
        "changed_files": ["cpersona/x.py"],
        "sampling": {"in_scope_total": 740, "executed": 60, "mutable_lines": 312, "rule": "rule", "seed": SEED},
        "line_coverage": {"mutable_lines": 312, "measured_lines": 55, "ratio": 55 / 312},
        "in_scope": 60,
        "counts": {"killed": 50, "survived": 10},
        "survival_rate": 10 / 60,
        "survivors": [],
    }
    lane.write_summary(report, str(out))
    text = out.read_text(encoding="utf-8")
    assert "60 of 740 in-scope mutants executed" in text
    assert "| 60 | 50 | 10 |" in text, "the counts table must still be printed for a sample"
    assert "55 of 312 changed lines that carry a mutant (18%)" in text


def _session_rows(tmp_path, rows):
    """rows: (line, worker, test, output) — one mutant each, on cpersona/x.py."""
    path = tmp_path / "cov-session"
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE mutation_specs (job_id TEXT, module_path TEXT, "
        "start_pos_row INTEGER, operator_name TEXT, definition_name TEXT)"
    )
    con.execute("CREATE TABLE work_results (job_id TEXT, worker_outcome TEXT, test_outcome TEXT, output TEXT)")
    for i, (line, worker, test, output) in enumerate(rows):
        con.execute("INSERT INTO mutation_specs VALUES (?,?,?,?,?)", (str(i), "cpersona/x.py", line, "op/A", "f"))
        if worker is not None:
            con.execute("INSERT INTO work_results VALUES (?,?,?,?)", (str(i), worker, test, output))
    con.commit()
    con.close()
    return path


def test_line_coverage_counts_only_lines_whose_mutant_reached_a_verdict(tmp_path):
    rows = [
        (1, "NORMAL", "KILLED", None),                        # measured
        (1, "SKIPPED", None, lane.NOT_SAMPLED_OUTPUT),        # same line, left out: still measured via the kill
        (2, "NORMAL", "SURVIVED", None),                      # measured
        (3, "SKIPPED", None, lane.NOT_SAMPLED_OUTPUT),        # mutable, not measured
        (4, None, None, None),                                # planned, no result: mutable, not measured
        (5, "EXCEPTION", None, None),                         # ran but no verdict: mutable, not measured
        (6, "SKIPPED", None, None),                           # removed by a filter: never in scope
    ]
    cov = lane.classify(_session_rows(tmp_path, rows), REPO, {})["line_coverage"]
    assert cov == {"mutable_lines": 5, "measured_lines": 2, "ratio": 2 / 5}


def test_line_coverage_is_empty_not_zero_when_nothing_was_in_scope(tmp_path):
    cov = lane.classify(_session_rows(tmp_path, [(1, "SKIPPED", None, None)]), REPO, {})["line_coverage"]
    assert cov == {"mutable_lines": 0, "measured_lines": 0, "ratio": None}
