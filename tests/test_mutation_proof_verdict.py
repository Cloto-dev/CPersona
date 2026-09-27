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
