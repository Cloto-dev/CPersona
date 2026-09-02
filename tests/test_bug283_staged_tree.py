"""bug-283: the mutation harness refuses a tree with *staged* edits too.

`tree_is_clean` guards the harness's whole claim — that its results describe the
release candidate. It read only `git diff --name-only`, which reports the
unstaged set, so a target file that was edited and then `git add`ed passed the
guard: mutants were applied over code nobody is shipping, and every seam was
reported pinned. The failure is quiet in the green direction.

The guard is exercised through a stubbed `run` rather than by staging a file in
the real working tree: staging here would disturb whatever the session is
actually working on, and the predicate's whole content is which git queries it
asks. Each case below asserts the answer for one arrangement of the two sets.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _harness():
    path = ROOT / "scripts" / "mutation-proof.py"
    spec = importlib.util.spec_from_file_location("mutation_proof_bug283", path)
    module = importlib.util.module_from_spec(spec)
    # The harness declares dataclasses under `from __future__ import
    # annotations`, and resolving those annotations looks the module up by name.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _stub_git(monkeypatch, module, *, unstaged: list[str], staged: list[str]):
    """Answer the two name-only queries; fail loudly on anything else."""

    def fake_run(cmd, **kw):
        if cmd[:3] == ["git", "diff", "--name-only"]:
            out = "\n".join(unstaged)
        elif cmd[:4] == ["git", "diff", "--cached", "--name-only"]:
            out = "\n".join(staged)
        else:  # pragma: no cover - reached only if the guard changes shape
            raise AssertionError(f"unexpected command: {cmd}")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=out)

    monkeypatch.setattr(module, "run", fake_run)


TARGET = "cpersona/memory_handlers.py"


@pytest.mark.parametrize(
    "unstaged, staged, clean",
    [
        ([], [], True),  # nothing pending anywhere
        ([TARGET], [], False),  # the case the guard already caught
        ([], [TARGET], False),  # bug-283: staged edits count
        ([], ["docs/operations.md"], True),  # untargeted files are not this run's business
    ],
)
def test_guard_sees_both_sets(monkeypatch, unstaged, staged, clean):
    module = _harness()
    _stub_git(monkeypatch, module, unstaged=unstaged, staged=staged)
    assert module.tree_is_clean({TARGET}) is clean


def test_the_guard_actually_asks_for_the_staged_set(monkeypatch):
    """Pin the query, not just the verdict.

    Without this, a guard that returned False for every input would satisfy the
    case above while telling us nothing about what it measured.
    """
    module = _harness()
    asked: list[list[str]] = []

    def recording_run(cmd, **kw):
        asked.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="")

    monkeypatch.setattr(module, "run", recording_run)
    assert module.tree_is_clean({TARGET}) is True
    assert ["git", "diff", "--cached", "--name-only"] in asked, (
        "the guard never asked git for the staged set, so a staged edit to a "
        f"target file would read as a clean tree; it ran {asked}"
    )
