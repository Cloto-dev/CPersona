"""Regenerating the golden must add scenarios, not silently overwrite values.

`scripts/capture-behaviour.py` writes what the matrix observes. Two kinds of
difference are recorded in `behaviour_252` as legitimate -- keys a later version
added that the golden does not hold, and values a later version deliberately
changed -- and the replay test applies both lists before it compares, which is
why the suite is green.

The capture script did not. Running it to add one scenario would have written
138 lines of already-reviewed change into the file along with it, retiring the
evidence those lists exist to keep. It happened once and was worked around by
splicing only the new entries in by hand.

These tests pin the reconciliation that closed it. They exercise it as a pure
function on small inputs rather than by re-running the 78-scenario matrix: the
matrix is already run by the replay test, and the property here is about what
the writer does with an observation, not about what the observation contains.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "tests" / "golden" / "behaviour_252.json"


def _capture():
    path = ROOT / "scripts" / "capture-behaviour.py"
    spec = importlib.util.spec_from_file_location("capture_behaviour_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def capture():
    return _capture()


def test_a_key_added_after_the_golden_is_not_written_into_it(capture):
    """`checks_run` postdates the golden on check_health. Observing it must not
    put it in the file -- the golden's claim is about the code as it was."""
    recorded = {"s": {"result": {"issues": [], "status": "healthy"}}}
    observed = {"s": {"result": {"issues": [], "status": "healthy", "checks_run": ["a", "b"]}}}

    written = capture._reconciled(observed, recorded)

    assert written == recorded, "a post-golden key reached the file"


def test_a_key_the_golden_already_holds_is_kept(capture):
    """The same name in a position the golden DOES record stays under full
    comparison -- deep_check has always emitted `checks_run`. A rule keyed on
    the name alone would delete the recorded one, which is the evidence."""
    recorded = {"s": {"result": {"checks_run": ["a"]}}}
    observed = {"s": {"result": {"checks_run": ["a", "b"]}}}

    written = capture._reconciled(observed, recorded)

    assert written["s"]["result"]["checks_run"] == ["a", "b"], (
        "a recorded key was dropped instead of compared"
    )


def test_a_deliberately_changed_value_keeps_what_the_golden_recorded(capture):
    """The one entry in `_VALUES_CHANGED_SINCE_GOLDEN`. The current code answers
    differently on purpose; the file must still hold what was observed before
    it, because that recording is the evidence the change was reviewed."""
    from behaviour_252 import _VALUES_CHANGED_SINCE_GOLDEN

    sid, path = next(iter(_VALUES_CHANGED_SINCE_GOLDEN))
    assert path == ("result", "issues"), "this test hard-codes the shape of the one entry"

    recorded = {sid: {"result": {"issues": [], "status": "degraded"}}}
    observed = {sid: {"result": {"issues": [{"check": "duplicate_content"}], "status": "degraded"}}}

    written = capture._reconciled(observed, recorded)

    assert written[sid]["result"]["issues"] == [], "the recorded value was overwritten"
    assert written[sid]["result"]["status"] == "degraded", "an unlisted key was touched"


def test_the_same_change_in_another_scenario_is_not_excused(capture):
    """An entry names one scenario and one key path. Nothing else is relaxed --
    otherwise the list would quietly widen every time it was used."""
    recorded = {"other": {"result": {"issues": []}}}
    observed = {"other": {"result": {"issues": [{"check": "duplicate_content"}]}}}

    written = capture._reconciled(observed, recorded)

    assert written["other"]["result"]["issues"] == [{"check": "duplicate_content"}], (
        "a value change was excused for a scenario the list does not name"
    )


def test_a_new_scenario_is_recorded_as_observed(capture):
    """A scenario the golden does not hold has no past to preserve. Dropping its
    post-golden keys would hand it a hole nobody chose."""
    observed = {"brand-new": {"result": {"issues": [], "checks_run": ["a"]}}}

    written = capture._reconciled(observed, {})

    assert written == observed, "a new scenario was reconciled against a past it does not have"


def test_reconciling_the_golden_with_itself_changes_nothing(capture):
    """Idempotence on the real file: whatever the transforms do, re-recording an
    unchanged run must be a no-op, or every regeneration would drift."""
    recorded = json.loads(GOLDEN.read_text(encoding="utf-8"))

    assert capture._reconciled(recorded, recorded) == recorded
