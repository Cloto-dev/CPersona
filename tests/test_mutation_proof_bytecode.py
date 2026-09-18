"""The mutation harness never lets a run import the bytecode of the previous text.

Python trusts a cached .pyc while the source's size and whole-second mtime
match what it recorded. A one-token mutant keeps the size, and a targeted run
can finish inside a second, so without care the next run imports the previous
text: a mutant is judged by code it did not contain. The tests below pin the
worst case deterministically -- same size, and the mtime set back to the one
the cache recorded -- and ask a fresh interpreter what it imported.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _harness():
    path = ROOT / "scripts" / "mutation-proof.py"
    spec = importlib.util.spec_from_file_location("mutation_proof_bytecode", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


H = _harness()


def _imported_value(directory: Path) -> str:
    """What a fresh interpreter sees when it imports the module, writing bytecode as usual."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONDONTWRITEBYTECODE"}
    out = subprocess.run(
        [sys.executable, "-c", "import target; print(target.VALUE)"],
        cwd=directory, env=env, capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def _mutant() -> object:
    return H.Mutation(id="MB", target="t", file="target.py", find="VALUE = 1", replace="VALUE = 2",
                      breaks="x", expect="e")


def test_a_same_size_mutant_in_the_same_second_is_the_code_that_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(H, "REPO", tmp_path)
    source = tmp_path / "target.py"
    source.write_text("VALUE = 1\n")
    assert _imported_value(tmp_path) == "1"  # caches the original's bytecode
    recorded = source.stat().st_mtime

    original = H.apply_mutation(_mutant())
    os.utime(source, (recorded, recorded))  # the write landed in the same second
    assert source.stat().st_size == len(original)
    assert _imported_value(tmp_path) == "2", "the run imported the original's cached bytecode"


def test_the_restored_original_is_the_code_that_runs_after_a_mutant(tmp_path, monkeypatch):
    monkeypatch.setattr(H, "REPO", tmp_path)
    source = tmp_path / "target.py"
    source.write_text("VALUE = 1\n")
    mutant = _mutant()
    original = H.apply_mutation(mutant)
    assert _imported_value(tmp_path) == "2"  # caches the mutant's bytecode
    recorded = source.stat().st_mtime

    H.restore_mutation(mutant, original)
    os.utime(source, (recorded, recorded))
    assert source.read_text() == original
    assert _imported_value(tmp_path) == "1", "the run after the restore imported the mutant's cached bytecode"


def test_forgetting_covers_every_interpreter_and_optimisation_level(tmp_path):
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    names = ["target.cpython-311.pyc", "target.cpython-313.pyc", "target.cpython-313.opt-1.pyc"]
    for name in names:
        (cache / name).write_bytes(b"")
    (cache / "other.cpython-313.pyc").write_bytes(b"")
    H.forget_bytecode(tmp_path / "target.py")
    assert sorted(p.name for p in cache.iterdir()) == ["other.cpython-313.pyc"]
