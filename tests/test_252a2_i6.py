"""Regression tests for the 252-a2 audit item I6 (verify-issues gate).

bug-152: scripts/verify-issues.sh serialized the 7 registry fields joined with '|'
and re-read them with `IFS='|'`. Any field containing a literal pipe shifted
every following field, so bug-012's pattern
``AFTER UPDATE ON (memories|episodes) BEGIN`` was mis-parsed: its verdict
silently evaporated (no branch matched the shifted ``expected`` token) while the
gate still exited 0. The fix switches the record separator to an ASCII unit
separator (\\x1f) in both the Python emitter and the bash reader, asserts no
field contains the separator, and reconciles the emitted vs. parsed record count
so a dropped row fails the gate loudly instead of passing green.

These tests run the actual script (never a reimplementation) so they exercise
the real serialization/parse hand-off.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "verify-issues.sh"
REGISTRY = REPO_ROOT / "qa" / "issue-registry.json"

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def _run(script: Path, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script)],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


def _make_root(tmp_path: Path, issues: list[dict], files: dict[str, str]) -> Path:
    """Build a throwaway project root: a copy of the real script plus a crafted
    registry and any pattern-target files. The script derives its project root
    from its own location, so a copy under tmp/scripts sees tmp/qa/registry."""
    (tmp_path / "scripts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "qa").mkdir(parents=True, exist_ok=True)
    shutil.copy(SCRIPT, tmp_path / "scripts" / "verify-issues.sh")
    (tmp_path / "qa" / "issue-registry.json").write_text(
        json.dumps({"issues": issues}), encoding="utf-8"
    )
    for rel, content in files.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return tmp_path


def _parse_summary(output: str) -> dict[str, int]:
    plain = _strip_ansi(output)
    counts: dict[str, int] = {}
    for key, label in (
        ("total", "Total issues"),
        ("verified", "Verified"),
        ("stale", "Stale"),
        ("fixed", "Fixed"),
        ("errors", "Errors"),
    ):
        m = re.search(rf"^{re.escape(label)}:\s+(\d+)\s*$", plain, re.MULTILINE)
        assert m is not None, f"missing summary line {label!r} in output:\n{plain}"
        counts[key] = int(m.group(1))
    return counts


def test_real_gate_checks_bug012_and_no_row_is_dropped() -> None:
    """The gate must evaluate bug-012 (whose pattern embeds a '|') and its
    Total must equal the sum of the four verdict counters — i.e. no registry
    row silently evaporates. On the pre-fix script bug-012 was dropped, so the
    counters summed to Total-1 and 'bug-012' never appeared."""
    result = _run(SCRIPT, REPO_ROOT)
    assert result.returncode == 0, (
        f"gate exited {result.returncode}\nSTDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}"
    )

    output = result.stdout
    assert "bug-012" in _strip_ansi(output), (
        "bug-012 has no verdict line — the pipe in its pattern shifted the "
        "fields and its check evaporated"
    )

    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    non_obsolete = sum(
        1 for i in registry.get("issues", []) if i.get("status") != "obsolete"
    )

    counts = _parse_summary(output)
    assert counts["total"] == non_obsolete, (
        f"Total={counts['total']} but registry has {non_obsolete} non-obsolete "
        "issues"
    )
    counter_sum = (
        counts["verified"] + counts["stale"] + counts["fixed"] + counts["errors"]
    )
    assert counter_sum == counts["total"], (
        f"verdict counters sum to {counter_sum} but Total={counts['total']} — "
        "a registry row was silently dropped by the field parser"
    )


def test_pipe_bearing_pattern_is_not_dropped(tmp_path: Path) -> None:
    """A single issue whose pattern contains a literal '|' must still receive a
    verdict. Pre-fix, the '|' join shifted its fields (expected became a stray
    token that matched neither 'present' nor 'absent'), so the row was counted
    in Total but produced no verdict — Total (1) exceeded the counter sum (0)."""
    root = _make_root(
        tmp_path,
        issues=[
            {
                "id": "bug-pipe",
                "severity": "MEDIUM",
                "file": "target.py",
                "pattern": "FOO|BAR",
                "expected": "absent",
                "status": "fixed",
                "summary": "pattern with an embedded pipe",
            }
        ],
        files={"target.py": "nothing to see here\n"},
    )
    result = _run(root / "scripts" / "verify-issues.sh", root)
    assert result.returncode == 0, (
        f"exit {result.returncode}\n{result.stdout}\n{result.stderr}"
    )
    assert "bug-pipe" in _strip_ansi(result.stdout), (
        "the pipe-bearing issue produced no verdict — its fields were shifted"
    )
    counts = _parse_summary(result.stdout)
    assert counts["total"] == 1
    counter_sum = (
        counts["verified"] + counts["stale"] + counts["fixed"] + counts["errors"]
    )
    assert counter_sum == 1, "the pipe-bearing row was dropped from the verdicts"


def test_emitter_rejects_separator_char_in_field(tmp_path: Path) -> None:
    """A field carrying the reserved record separator (unit separator) must
    abort the gate non-zero with a FATAL message, not silently corrupt the
    stream. Pre-fix (no assertion, '|' transport) such a field passed through
    and the gate exited 0."""
    root = _make_root(
        tmp_path,
        issues=[
            {
                "id": "bug-us",
                "severity": "LOW",
                "file": "target.py",
                "pattern": "present-marker",
                "expected": "present",
                "status": "fixed",
                "summary": "summary with a \x1f unit separator inside",
            }
        ],
        files={"target.py": "present-marker\n"},
    )
    result = _run(root / "scripts" / "verify-issues.sh", root)
    combined = _strip_ansi(result.stdout + result.stderr)
    assert result.returncode != 0, (
        f"gate exited 0 despite a reserved separator in a field:\n{combined}"
    )
    assert "FATAL" in combined, (
        f"expected a FATAL diagnostic for the corrupt field, got:\n{combined}"
    )
