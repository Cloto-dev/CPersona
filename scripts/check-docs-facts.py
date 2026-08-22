#!/usr/bin/env python3
"""Verify that measurable facts stated in the docs match the source of truth.

Hand-written numbers rot: the bundled skill said "28 tools / ~5,600 LOC" for
months after both had grown (caught by a production user, 2026-08-21), and the
README's test counts drifted within a day of being measured. This gate makes
the drift visible in CI instead of waiting for a reader to trip over it.

Checked facts and their sources of truth:

  tool count      runtime registry (import cpersona.server, count registered
                  tools) — NOT grep: a grep of `auto_tool(` has already
                  miscounted once (matched a non-registration line)
  schema version  SCHEMA_VERSION literal in cpersona/database.py
  env defaults    static parse of cpersona/config.py, compared against every
                  markdown table row in docs/ that names a `CPERSONA_*` var
  volatile stats  LOC / test-function / test-module / collected-case counts,
                  re-measured here with the same commands the docs cite;
                  docs state them as rounded `~` values and this check allows
                  TOLERANCE relative drift before failing, so routine commits
                  do not churn the docs while real drift still trips the gate

Exit 0 when every claim holds; exit 1 with one line per violation otherwise.
Run from the repository root (CI does): `uv run python scripts/check-docs-facts.py`.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Docs surfaces that claim CURRENT state and therefore must track the code.
# *_DESIGN.md / *_STANDARD.md are point-in-time design records — statements
# like "(24 → 25 tools)" describe the moment a design shipped and are allowed
# to stay as written — so they are deliberately not scanned.
DOC_FILES = [
    ROOT / "README.md",
    ROOT / "skills" / "cpersona-memory" / "SKILL.md",
    ROOT / "docs" / "index.md",
    ROOT / "docs" / "getting-started.md",
    ROOT / "docs" / "architecture.md",
    ROOT / "docs" / "tools.md",
    ROOT / "docs" / "behavior-contracts.md",
    ROOT / "docs" / "operations.md",
    ROOT / "docs" / "configuration.md",
    ROOT / "docs" / "faq.md",
    ROOT / "docs" / "llms.txt",
    # Translations restate the same measurable claims, so they rot the same
    # way. The checks below key on `CPERSONA_*` cells and on the numbers
    # themselves, not on English prose, so a ja page is scanned identically.
    ROOT / "docs" / "index.ja.md",
    ROOT / "docs" / "faq.ja.md",
    ROOT / "docs" / "behavior-contracts.ja.md",
    ROOT / "docs" / "operations.ja.md",
    ROOT / "docs" / "configuration.ja.md",
    ROOT / "docs" / "getting-started.ja.md",
    ROOT / "docs" / "architecture.ja.md",
    ROOT / "docs" / "tools.ja.md",
]

# Relative drift allowed on volatile stats before the gate goes red. 3% keeps
# routine test additions from forcing a doc edit per commit, while an 8% drift
# like the one this gate was built for (13,000 vs 14,129 LOC) still fails.
TOLERANCE = 0.03

failures: list[str] = []


def fail(msg: str) -> None:
    failures.append(msg)


# --- sources of truth -------------------------------------------------------


def measured_tool_count() -> int:
    sys.path.insert(0, str(ROOT))
    import cpersona.server as server  # noqa: PLC0415 — deliberate late import

    return len(server.registry._tools)


def measured_schema_version() -> int:
    text = (ROOT / "cpersona" / "database.py").read_text()
    m = re.search(r"^SCHEMA_VERSION = (\d+)$", text, re.M)
    if not m:
        fail("database.py: SCHEMA_VERSION literal not found — update this script")
        return -1
    return int(m.group(1))


def parsed_env_defaults() -> dict[str, str]:
    """Static parse of config.py → {VAR_NAME: normalized default}.

    Handles the three assignment shapes config.py uses. Vars read through any
    other shape are simply absent here and fall back to an existence-only
    check against the package source.
    """
    text = (ROOT / "cpersona" / "config.py").read_text()
    defaults: dict[str, str] = {}
    for m in re.finditer(r'_parse_int\("(CPERSONA_\w+)",\s*(-?\d+)\)', text):
        defaults[m.group(1)] = m.group(2)
    for m in re.finditer(r'_parse_float\("(CPERSONA_\w+)",\s*(-?[\d.]+)\)', text):
        defaults[m.group(1)] = m.group(2)
    for m in re.finditer(
        r'os\.environ\.get\("(CPERSONA_\w+)"\s*,\s*"([^"]*)"\)(\.lower\(\)\s*==\s*"true")?',
        text,
    ):
        defaults[m.group(1)] = m.group(2)
    return defaults


def measured_volatile_stats() -> dict[str, float]:
    def loc(paths: list[Path]) -> int:
        return sum(len(p.read_text().splitlines()) for p in paths)

    server_loc = loc(sorted((ROOT / "cpersona").glob("*.py")))
    vendored_loc = loc(sorted((ROOT / "cpersona" / "_vendored_mcp_common").rglob("*.py")))
    test_files = sorted((ROOT / "tests").glob("*.py"))
    test_loc = loc(test_files)
    test_funcs = sum(
        len(re.findall(r"\bdef test_", p.read_text())) for p in test_files
    )
    test_modules = len([p for p in test_files if p.name.startswith("test_")])

    collect = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    m = re.search(r"(\d+) tests collected", collect.stdout)
    if not m:
        fail(
            "pytest --collect-only did not report a test count "
            f"(rc={collect.returncode}) — cannot verify the collected-case claim"
        )
        cases = -1
    else:
        cases = int(m.group(1))

    return {
        "server LOC": server_loc,
        "vendored LOC": vendored_loc,
        "test LOC": test_loc,
        "test functions": test_funcs,
        "test modules": test_modules,
        "collected cases": cases,
    }


# --- claims found in the docs ----------------------------------------------


def check_tool_and_schema_claims(tool_count: int, schema_version: int) -> None:
    for doc in DOC_FILES:
        text = doc.read_text()
        rel = doc.relative_to(ROOT)
        # Both spellings, because the translations restate the same count and a
        # detector that only knows the English phrase passes them vacuously —
        # which is indistinguishable from passing them correctly.
        for pattern in (r"\b(\d+) tools\b", r"(\d+) 個のツール"):
            for m in re.finditer(pattern, text):
                if int(m.group(1)) != tool_count:
                    fail(f"{rel}: claims '{m.group(0)}' but the registry serves {tool_count}")
        for m in re.finditer(r"\b[Ss]chema v(\d+)\b", text):
            if int(m.group(1)) != schema_version:
                fail(f"{rel}: claims '{m.group(0)}' but SCHEMA_VERSION is {schema_version}")


def normalize_doc_default(cell: str) -> str | None:
    """Return a comparable default from a markdown table cell, or None if the
    cell does not state a single literal default (prose descriptions etc.)."""
    cell = cell.strip()
    if cell in ("*(unset)*", "—", ""):
        return "<unset>"
    m = re.fullmatch(r"`([^`]*)`", cell)
    if not m:
        return None
    return m.group(1)


def check_env_tables(env_defaults: dict[str, str]) -> None:
    package_source = "\n".join(
        p.read_text() for p in (ROOT / "cpersona").glob("*.py")
    )
    for doc in DOC_FILES:
        rel = doc.relative_to(ROOT)
        for line in doc.read_text().splitlines():
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            var = None
            var_idx = None
            for i, c in enumerate(cells):
                m = re.fullmatch(r"`(CPERSONA_\w+)`", c)
                if m:
                    var, var_idx = m.group(1), i
                    break
            if var is None:
                continue
            # Existence: a documented variable must be read somewhere in the
            # package — a renamed or removed env var must not survive in docs.
            if f'"{var}"' not in package_source and f"'{var}'" not in package_source:
                fail(f"{rel}: table documents `{var}`, which no source file reads")
                continue
            # Default: only checkable for vars the static parse understood and
            # cells that state a single literal.
            if var not in env_defaults or var_idx + 1 >= len(cells):
                continue
            documented = normalize_doc_default(cells[var_idx + 1])
            if documented is None:
                continue
            actual = env_defaults[var]
            if documented == "<unset>":
                continue  # existence-only: parse shapes for unset vars vary
            try:
                if float(documented) == float(actual):
                    continue  # numerically equal (5 vs 5.0)
            except ValueError:
                pass
            if documented != actual:
                fail(
                    f"{rel}: `{var}` documented default `{documented}` != "
                    f"config.py default `{actual}`"
                )


VOLATILE_CLAIMS = {
    # claim regex (group 1 = number) → measured-stat key. These are the only
    # numeric claims docs are allowed to hand-round; each is stated with `~`.
    # The test-FUNCTION count is deliberately absent: Gate 12 in
    # tests/test_structural_gates.py owns that claim (one invariant, one
    # implementation) — it predates this script and is mutation-protected.
    r"~([\d,]+) LOC\*?\* Python": "server LOC",
    r"a ([\d,]+)-line vendored": "vendored LOC",
    r"([\d,]+) test modules": "test modules",
    r"~([\d,]+) cases": "collected cases",
    r"\(~([\d,]+) LOC, more test code": "test LOC",
    r"~([\d,]+) LOC Python across": "server LOC",
}


def check_volatile_claims(stats: dict[str, float]) -> None:
    for doc in DOC_FILES:
        text = doc.read_text()
        rel = doc.relative_to(ROOT)
        for pattern, key in VOLATILE_CLAIMS.items():
            for m in re.finditer(pattern, text):
                claimed = int(m.group(1).replace(",", ""))
                actual = stats[key]
                if actual <= 0:
                    continue  # measurement itself failed and was reported
                drift = abs(actual - claimed) / actual
                if drift > TOLERANCE:
                    fail(
                        f"{rel}: claims {key} ≈ {claimed}, measured {int(actual)} "
                        f"(drift {drift:.1%} > {TOLERANCE:.0%})"
                    )


def main() -> int:
    tool_count = measured_tool_count()
    schema_version = measured_schema_version()
    env_defaults = parsed_env_defaults()
    stats = measured_volatile_stats()

    check_tool_and_schema_claims(tool_count, schema_version)
    check_env_tables(env_defaults)
    check_volatile_claims(stats)

    print(
        f"measured: {tool_count} tools, schema v{schema_version}, "
        + ", ".join(f"{k}={int(v)}" for k, v in stats.items())
        + f", {len(env_defaults)} env defaults parsed"
    )
    if failures:
        print(f"\n{len(failures)} documentation fact(s) out of date:", file=sys.stderr)
        for f_ in failures:
            print(f"  - {f_}", file=sys.stderr)
        return 1
    print("docs facts: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
