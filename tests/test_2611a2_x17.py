"""Regression tests for what integrating the LOW-severity fixes added on top of them.

  update check   The one switch that opens an outbound connection reads a value
                 that is neither on nor off as off. Every other on/off setting
                 keeps its default for such a value (bug-656), but this one was
                 off for every value but `true` before, and an operator who wrote
                 `disabled` to stop the request must not start sending it.
  docs facts     docs-facts compares the default of an on/off setting, and of a
                 setting whose default is computed from another one, with the
                 configuration page, instead of failing the row as compared with
                 nothing.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "test_2611a2_x17.db"))
os.environ.setdefault("CPERSONA_EMBEDDING_MODE", "none")

import pytest  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _flags_in_fresh_process(env_overrides: dict[str, str]) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CPERSONA_UPDATE_CHECK")}
    env.pop("CPERSONA_TASK_QUEUE_ENABLED", None)
    env.update(env_overrides)
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "from cpersona import config; "
            "print(config.UPDATE_CHECK_ENABLED, config.TASK_QUEUE_ENABLED)",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
    )
    update, queue = out.stdout.split()
    return {"update": update, "queue": queue, "stderr": out.stderr}


def test_the_update_check_is_on_when_nothing_is_set():
    assert _flags_in_fresh_process({})["update"] == "True"


@pytest.mark.parametrize("value", ["disabled", "never", "none"])
def test_an_unreadable_value_turns_the_update_check_off_and_says_so(value):
    got = _flags_in_fresh_process({"CPERSONA_UPDATE_CHECK": value})
    assert got["update"] == "False"
    assert "CPERSONA_UPDATE_CHECK" in got["stderr"] and "using 'false'" in got["stderr"]


@pytest.mark.parametrize("value, expected", [("1", "True"), ("yes", "True"), ("off", "False"), ("0", "False")])
def test_the_update_check_reads_the_usual_spellings(value, expected):
    assert _flags_in_fresh_process({"CPERSONA_UPDATE_CHECK": value})["update"] == expected


def test_another_switch_keeps_its_default_for_an_unreadable_value():
    # The exception is the update check's alone: a default-on switch without an
    # outbound connection keeps the module rule (bug-656).
    assert _flags_in_fresh_process({"CPERSONA_TASK_QUEUE_ENABLED": "disabled"})["queue"] == "True"


# ---------------------------------------------------------------------------
# docs-facts: on/off and computed defaults are compared
# ---------------------------------------------------------------------------


@pytest.fixture
def cdf():
    spec = importlib.util.spec_from_file_location("check_docs_facts_x17", ROOT / "scripts" / "check-docs-facts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.failures.clear()
    return module


def _fake_package(tmp_path: Path, config_source: str) -> None:
    package = tmp_path / "cpersona"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "config.py").write_text(config_source)


_FAKE_PARSERS = (
    "import os\n"
    "def _parse_int(k, d):\n"
    "    v = os.environ.get(k)\n"
    "    return int(v) if v else d\n"
    "def _parse_bool(k, d, unreadable=None):\n"
    "    v = os.environ.get(k)\n"
    "    return d if not v else v == 'true'\n"
)


def test_an_on_off_default_is_read_from_its_literal(cdf, tmp_path, monkeypatch):
    _fake_package(tmp_path, _FAKE_PARSERS + 'SWITCH = _parse_bool("CPERSONA_SWITCH", False)\n')
    monkeypatch.setattr(cdf, "ROOT", tmp_path)
    # The static shape alone: nothing is left for the measured reader.
    monkeypatch.setattr(cdf, "measured_computed_defaults", lambda parsed: {})
    assert cdf.parsed_env_defaults() == {"CPERSONA_SWITCH": "false"}


def test_a_computed_default_is_measured_with_nothing_set(cdf, tmp_path, monkeypatch):
    _fake_package(
        tmp_path,
        _FAKE_PARSERS
        + 'BASE = _parse_int("CPERSONA_BASE", 800)\n'
        + 'TAIL = max(\n    0, _parse_int("CPERSONA_TAIL", min(400, BASE))\n)\n'
        + 'FOLLOWS = _parse_bool("CPERSONA_FOLLOWS", BASE > 0)\n',
    )
    monkeypatch.setattr(cdf, "ROOT", tmp_path)
    # Values set in this process are not what a reader with nothing set gets.
    monkeypatch.setenv("CPERSONA_BASE", "100")
    monkeypatch.setenv("CPERSONA_TAIL", "7")
    assert cdf.measured_computed_defaults({"CPERSONA_BASE"}) == {
        "CPERSONA_TAIL": "400",
        "CPERSONA_FOLLOWS": "true",
    }
    assert cdf.failures == []


def test_the_configuration_page_states_the_measured_and_on_off_defaults(cdf):
    defaults = cdf.parsed_env_defaults()
    assert defaults["CPERSONA_UPDATE_CHECK"] == "true"
    assert defaults["CPERSONA_RECONSTRUCT_TAIL_QUOTE_CHARS"] == "400"
    assert defaults["CPERSONA_RECONSTRUCT_DEFAULT_BUDGET"] == "4000"
    assert defaults["CPERSONA_RECONSTRUCT_MAX_BUDGET"] == "20000"
    cdf.check_env_tables(defaults)
    assert cdf.failures == []


def test_a_wrong_on_off_default_on_the_page_is_a_failure(cdf):
    defaults = dict(cdf.parsed_env_defaults())
    defaults["CPERSONA_UPDATE_CHECK"] = "false"
    cdf.check_env_tables(defaults)
    assert any("CPERSONA_UPDATE_CHECK" in f and "`true` != package default `false`" in f for f in cdf.failures)
