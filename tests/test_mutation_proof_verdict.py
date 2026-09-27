"""The mutation harness runs a mutant's own test files before the full suite.

The shortcut must only ever shorten a verdict the full suite would reach. A red
subset is a red suite, so a failing targeted run may decide CAUGHT; every other
outcome has to fall through to the full suite: a green subset (the `tests` list
went stale), a run that collected nothing, and any equivalent mutant, which is
only correct if it survives the WHOLE suite.

The runner is a stub that records which runs were asked for, so each case pins
both the verdict and the runs that produced it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _harness():
    path = ROOT / "scripts" / "mutation-proof.py"
    spec = importlib.util.spec_from_file_location("mutation_proof_verdict", path)
    module = importlib.util.module_from_spec(spec)
    # Dataclass annotations are resolved through the module's registered name.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


H = _harness()


def _mutant(**overrides):
    fields = dict(id="MX", target="t", file="f.py", find="a", replace="b", breaks="x", expect="e",
                  tests=("tests/test_one.py",))
    fields.update(overrides)
    return H.Mutation(**fields)


def _runner(targeted_code: int, full_code: int):
    calls: list[list[str]] = []

    def run_tests(paths):
        calls.append(list(paths))
        return targeted_code if paths else full_code

    return run_tests, calls


def test_a_red_targeted_run_decides_caught_without_the_full_suite():
    run_tests, calls = _runner(targeted_code=1, full_code=0)
    assert H.verdict(_mutant(), run_tests) == (True, "targeted")
    assert calls == [["tests/test_one.py"]]


def test_a_green_targeted_run_falls_through_to_the_full_suite():
    run_tests, calls = _runner(targeted_code=0, full_code=1)
    assert H.verdict(_mutant(), run_tests) == (True, "full")
    assert calls == [["tests/test_one.py"], []]


def test_a_mutant_green_everywhere_survives():
    run_tests, calls = _runner(targeted_code=0, full_code=0)
    assert H.verdict(_mutant(), run_tests) == (False, "full")
    assert calls == [["tests/test_one.py"], []]


@pytest.mark.parametrize("code", [3, 4, 5])
def test_a_targeted_run_that_says_nothing_about_the_mutant_is_not_caught(code):
    """No tests collected, internal and usage errors must not become CAUGHT."""
    run_tests, calls = _runner(targeted_code=code, full_code=0)
    assert H.verdict(_mutant(), run_tests) == (False, "full")
    assert calls == [["tests/test_one.py"], []]


def test_an_interrupted_targeted_run_counts_as_caught():
    """A mutant that breaks an import interrupts collection (exit 2)."""
    run_tests, _ = _runner(targeted_code=2, full_code=0)
    assert H.verdict(_mutant(), run_tests) == (True, "targeted")


def test_an_equivalent_mutant_never_takes_the_shortcut():
    run_tests, calls = _runner(targeted_code=1, full_code=0)
    assert H.verdict(_mutant(equivalent=True), run_tests) == (False, "full")
    assert calls == [[]]


def test_a_mutant_without_test_files_runs_the_full_suite_only():
    run_tests, calls = _runner(targeted_code=1, full_code=1)
    assert H.verdict(_mutant(tests=()), run_tests) == (True, "full")
    assert calls == [[]]


def test_every_listed_test_file_exists():
    """A misspelt path collects nothing and would silently cost a full run."""
    assert H.missing_test_files(H.MUTATIONS) == []
    assert H.missing_test_files([_mutant(tests=("tests/no_such_file.py",))]) == ["MX: tests/no_such_file.py"]


def test_every_behavioural_mutant_names_its_test_files():
    unnamed = [m.id for m in H.MUTATIONS if not m.equivalent and not m.tests]
    assert unnamed == []


# A red full run decides only when it reproduces. The scripted runner answers each
# call in turn and records the paths and deselections it was asked for.

FLAKY = "tests/test_other.py::test_sometimes"


def _scripted(*answers):
    calls: list[tuple[list[str], tuple[str, ...]]] = []
    queue = list(answers)

    def run_tests(paths, deselect=()):
        calls.append((list(paths), tuple(deselect)))
        return queue.pop(0)

    return run_tests, calls


def test_a_failure_that_reproduces_with_the_mutant_decides_caught():
    run_tests, calls = _scripted(H.Run(1, (FLAKY,)), H.Run(1, (FLAKY,)))
    report: list = []
    assert H.verdict(_mutant(equivalent=True), run_tests, report) == (True, "full")
    assert calls == [([], ()), ([FLAKY], ())]
    assert report == [("failed", (FLAKY,))]


def test_a_failure_that_does_not_reproduce_is_set_aside_and_the_rest_decides():
    """The equivalent mutant this was written for: one flaky test read as OVER-PINNED."""
    run_tests, calls = _scripted(H.Run(1, (FLAKY,)), H.Run(0), H.Run(0))
    report: list = []
    assert H.verdict(_mutant(equivalent=True), run_tests, report) == (False, "full")
    assert calls == [([], ()), ([FLAKY], ()), ([], (FLAKY,))]
    assert report == [("failed", (FLAKY,)), ("flaky", (FLAKY,))]


def test_a_flaky_failure_does_not_hide_a_behavioural_survivor():
    """The dangerous direction: a flake must not turn a surviving mutant into CAUGHT."""
    run_tests, calls = _scripted(H.Run(0), H.Run(1, (FLAKY,)), H.Run(0), H.Run(0))
    assert H.verdict(_mutant(), run_tests) == (False, "full")
    assert calls[-1] == ([], (FLAKY,))


def test_the_rest_of_the_suite_still_catches_after_a_flake_is_set_aside():
    real = "tests/test_one.py::test_the_pin"
    run_tests, _ = _scripted(H.Run(1, (FLAKY,)), H.Run(0), H.Run(1, (real,)))
    report: list = []
    assert H.verdict(_mutant(equivalent=True), run_tests, report) == (True, "full")
    assert report[-1] == ("failed", (real,))


def test_a_red_run_that_names_no_test_stays_caught_without_a_rerun():
    run_tests, calls = _scripted(H.Run(1))
    assert H.verdict(_mutant(tests=()), run_tests) == (True, "full")
    assert calls == [([], ())]


@pytest.mark.parametrize("code", [2, 3, 4, 5])
def test_a_rerun_that_cannot_answer_keeps_the_conservative_verdict(code):
    """A node id the rerun cannot collect or run says nothing: the mutant stays caught."""
    run_tests, calls = _scripted(H.Run(1, (FLAKY,)), H.Run(code))
    assert H.verdict(_mutant(tests=()), run_tests) == (True, "full")
    assert len(calls) == 2


def test_failed_node_ids_reads_the_short_summary():
    output = "\n".join([
        "..........F",
        "=========================== short test summary info ============================",
        "FAILED tests/test_a.py::test_x - AssertionError: 1 == 2",
        "FAILED tests/test_b.py::test_y[m1 - recall cue] - assert False",
        "ERROR tests/test_c.py::test_z",
        "SKIPPED [1] tests/test_d.py:3: reason",
        "1 failed, 10 passed in 0.10s",
    ])
    assert H.failed_node_ids(output) == (
        "tests/test_a.py::test_x",
        "tests/test_b.py::test_y[m1 - recall cue]",
        "tests/test_c.py::test_z",
    )
