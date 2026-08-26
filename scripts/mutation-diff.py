#!/usr/bin/env python3
"""Advisory mutation-diff lane (L1).

NON-BLOCKING. This scopes a cosmic-ray mutation run to the production lines a PR
actually changed, classifies the outcomes, writes a JSON artifact plus a
human-readable summary, and ALWAYS exits 0. The lane exists to MEASURE, not to
gate: its numbers (survivor counts, noise rate, added CI time) are the input to
the later, deliberate blocking-flip decision (#299 / L6). Turning it into a hard
gate before those measurements exist would be exactly the "institutionalise a
number nobody validated" failure this goal was created to avoid.

Three mutation surfaces coexist in this repo; keep them distinct:
  * scripts/mutation-proof.py   — hand-authored sentinel mutants pinning the
    refactor-critical invariants (the L5 lane; always blocking).
  * tests/test_equivalence_252.py — the behaviour-golden differential (gated by
    the ordinary `test` job).
  * THIS lane                   — automatically generated mutants over the PR
    diff; advisory only.

What this lane deliberately does NOT do:
  * It is not a mutation-adequacy score. Survivors are recorded, never required
    to be killed. Triage vocabulary and waivers are L2 (#295).
  * It does not catch test-only regressions. A diff that only touches tests
    produces zero production mutants and is reported as "skipped (no production
    diff)". Detecting assertion-weakening / test-deletion is the test-only
    lane's job (#296 / L3), structurally invisible here — by design, not by
    oversight.

Ground truth for the cosmic-ray plumbing was verified against 8.4.6 on
2026-07-24 (config schema, the `[work_item, result]` dump shape, the
worker/test outcome taxonomy, and that generated mutants actually reach
cpersona's tested code — 21/55 killed on isolation.py in a live run).

Usage:
    uv run --group dev --group mutation python scripts/mutation-diff.py
    uv run ... python scripts/mutation-diff.py --base origin/master \\
        --json-out mutation-diff-report.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import tomllib
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutation_waivers  # noqa: E402  — sibling script, not an installable package

REPO = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO / "cosmic-ray.toml"

# git pathspecs for the production diff scan. In DEFAULT (non-:(glob)) pathspec
# syntax `*` matches across `/`, so `cpersona/*.py` is recursive and reaches
# top-level modules too — `cpersona/**/*.py` would (wrongly) require a
# subdirectory and miss cpersona/isolation.py while matching vendored files.
# The excludes mirror cosmic-ray.toml so a vendored-only PR reads as "no
# production diff" instead of scoping mutants cosmic-ray would then skip.
INCLUDE_PATHSPEC = "cpersona/*.py"
EXCLUDED_PATHSPECS = (
    ":(exclude)cpersona/_vendored_mcp_common/*",
    ":(exclude)cpersona/skills/*",
)

# Triage vocabulary for survivors — single source in mutation_waivers so the
# producer (this lane) and the waiver registry cannot drift apart. An un-waived
# survivor leaves here with classification=None; a waived one carries its
# waiver's classification.
SURVIVOR_CLASSIFICATIONS = mutation_waivers.CLASSIFICATIONS

ENGINE_VERSION = "8.4.6"


def run(cmd: list[str], *, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    """Run a subprocess from the repo root. cosmic-ray tools are console scripts
    on PATH inside the uv env this script is launched from."""
    return subprocess.run(
        cmd,
        cwd=REPO,
        check=check,
        text=True,
        capture_output=capture,
    )


def resolve_base(args_base: str | None, config_branch: str) -> str:
    """Precedence: --base > $MUTATION_DIFF_BASE > the config's git-filter branch.
    The value returned is used BOTH for the diff scan and the git filter, so the
    two can never disagree."""
    return args_base or os.environ.get("MUTATION_DIFF_BASE") or config_branch


def config_git_branch(config_path: Path) -> str:
    with config_path.open("rb") as fh:
        cfg = tomllib.load(fh)
    return (
        cfg.get("cosmic-ray", {})
        .get("filters", {})
        .get("git-filter", {})
        .get("branch", "master")
    )


def effective_config(config_path: Path, base: str, session_dir: Path) -> Path:
    """cr-filter-git reads the base branch ONLY from the config. When the runtime
    base differs from the file's value, emit a copy with just that one line
    rewritten so the filter diffs against the same ref this script scanned. The
    rewrite is a single controlled substitution on our own file — not a TOML
    round-trip — so it cannot perturb any other field."""
    if base == config_git_branch(config_path):
        return config_path
    text = config_path.read_text()
    pattern = re.compile(
        r'(\[cosmic-ray\.filters\.git-filter\](?:[^\[]*?)\bbranch\s*=\s*)"[^"]*"',
        re.DOTALL,
    )
    new_text, n = pattern.subn(rf'\1"{base}"', text)
    if n != 1:
        # Fail loud rather than silently diff against the wrong ref.
        raise SystemExit(
            f"could not rewrite git-filter branch in {config_path} "
            f"(expected exactly one match, got {n})"
        )
    out = session_dir / "cosmic-ray.effective.toml"
    out.write_text(new_text)
    return out


def production_diff_files(base: str) -> list[str]:
    """Changed *.py under cpersona/, minus the vendored/skills exclusions."""
    cp = run(
        ["git", "diff", "--name-only", base, "--", INCLUDE_PATHSPEC, *EXCLUDED_PATHSPECS],
        capture=True,
    )
    return [line for line in cp.stdout.splitlines() if line.strip()]


def _read_session(session: Path) -> list[tuple]:
    """(module_path, line, operator, function, worker_outcome, test_outcome) per
    mutant, read straight from the cosmic-ray session SQLite.

    We bypass `cosmic-ray dump` on purpose: in 8.4.6 it raises AttributeError on
    any filter-skipped result (its test_outcome is None), and the git filter
    always produces thousands of those, so dump is unusable for this lane.
    Reading the db directly is also a single query instead of parsing N JSON
    lines. Outcomes are stored as the StrEnum *names* (e.g. 'SKIPPED',
    'KILLED'); callers upper-case before comparing so the parser is immune to
    whether a build stores the name or the value."""
    con = sqlite3.connect(str(session))
    try:
        return con.execute(
            "SELECT ms.module_path, ms.start_pos_row, ms.operator_name, "
            "       ms.definition_name, wr.worker_outcome, wr.test_outcome "
            "FROM mutation_specs ms "
            "LEFT JOIN work_results wr ON wr.job_id = ms.job_id"
        ).fetchall()
    finally:
        con.close()


def count_pending(session: Path) -> int:
    """In-scope mutants awaiting exec = mutants with no result row yet.
    Filter-skipped mutants already carry a (SKIPPED) result row and so are
    excluded here."""
    con = sqlite3.connect(str(session))
    try:
        (n,) = con.execute(
            "SELECT COUNT(*) FROM work_items wi "
            "LEFT JOIN work_results wr ON wr.job_id = wi.job_id "
            "WHERE wr.job_id IS NULL"
        ).fetchone()
        return int(n)
    finally:
        con.close()


def classify(session: Path, repo: Path, active: dict[str, dict]) -> dict:
    counts = {
        "skipped_by_filter": 0,
        "killed": 0,
        "survived": 0,
        "survived_waived": 0,
        "survived_unwaived": 0,
        "incompetent": 0,
        "worker_error": 0,
        "not_executed": 0,
    }
    survivors: list[dict] = []
    for module_path, line, operator, function, worker, test in _read_session(session):
        w = (worker or "").upper()
        t = (test or "").upper()
        if worker is None:
            counts["not_executed"] += 1
        elif w == "SKIPPED":
            counts["skipped_by_filter"] += 1
        elif w != "NORMAL":
            # exception / abnormal / no-test — an unusable run, NOT a kill.
            counts["worker_error"] += 1
        elif t == "KILLED":
            counts["killed"] += 1
        elif t == "SURVIVED":
            counts["survived"] += 1
            # Match the live survivor against approved waivers by content
            # fingerprint (L2, #295). A waived survivor carries its waiver's
            # classification; an un-waived one is the actionable set that L6
            # (#299) will eventually gate on.
            line_text = mutation_waivers.line_at(repo, module_path, line)
            fp = (
                mutation_waivers.fingerprint(module_path, function, operator, line_text)
                if line_text is not None
                else None
            )
            waiver = active.get(fp) if fp else None
            counts["survived_waived" if waiver else "survived_unwaived"] += 1
            survivors.append(
                {
                    "file": module_path,
                    "line": line,
                    "operator": operator,
                    "function": function,
                    "fingerprint": fp,
                    "waived": waiver is not None,
                    "waiver_id": waiver["id"] if waiver else None,
                    "classification": waiver["classification"] if waiver else None,
                }
            )
        elif t == "INCOMPETENT":
            counts["incompetent"] += 1
        else:  # pragma: no cover - defensive
            counts["worker_error"] += 1
    valid = counts["killed"] + counts["survived"]
    survival_rate = (counts["survived"] / valid) if valid else None
    return {
        "counts": counts,
        "in_scope": valid + counts["incompetent"] + counts["worker_error"],
        "survival_rate": survival_rate,
        "survivors": survivors,
    }


def write_summary(report: dict, path: str | None) -> None:
    """Append a markdown summary to $GITHUB_STEP_SUMMARY (or the given path)."""
    if not path:
        return
    lines = ["## Mutation-diff (advisory)", ""]
    status = report["status"]
    if status != "ok":
        lines += [f"**status:** `{status}` — {report.get('reason', '')}", ""]
    if report.get("changed_files"):
        lines += [f"**base:** `{report['base']}` · **changed files:** {len(report['changed_files'])}", ""]
    if status in ("ok", "capped"):
        c = report.get("counts", {})
        rate = report.get("survival_rate")
        rate_s = f"{rate:.1%}" if isinstance(rate, float) else "n/a"
        lines += [
            "| generated | killed | survived | survived (un-waived) | incompetent | worker-err | survival-rate |",
            "|---|---|---|---|---|---|---|",
            f"| {report.get('in_scope', 0)} | {c.get('killed', 0)} | {c.get('survived', 0)} "
            f"| {c.get('survived_unwaived', c.get('survived', 0))} | {c.get('incompetent', 0)} "
            f"| {c.get('worker_error', 0)} | {rate_s} |",
            "",
        ]
        wv = report.get("waivers", {})
        if wv:
            lines += [f"_waivers: {wv.get('active', 0)} active of {wv.get('registry_waivers', 0)} in registry_", ""]
        # Un-waived survivors first — those are the actionable set L6 will gate on.
        survivors = sorted(report.get("survivors", []), key=lambda s: s.get("waived", False))
        if survivors:
            lines += ["<details><summary>Survivors (advisory — triage at L2)</summary>", ""]
            lines += ["| file | line | operator | function | waived |", "|---|---|---|---|---|"]
            for s in survivors[:50]:
                mark = f"✅ {s.get('waiver_id')}" if s.get("waived") else "—"
                lines.append(f"| `{s['file']}` | {s['line']} | `{s['operator']}` | {s['function'] or ''} | {mark} |")
            if len(survivors) > 50:
                lines.append(f"| … | | | | _{len(survivors) - 50} more_ |")
            lines += ["", "</details>"]
    lines += ["", "_This lane never fails the build; it measures. Blocking is a later decision (#299)._"]
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def emit(report: dict, json_out: str | None, summary_path: str | None) -> None:
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if json_out:
        Path(json_out).write_text(text + "\n", encoding="utf-8")
    print(text)
    write_summary(report, summary_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="Diff base ref (default: $MUTATION_DIFF_BASE or config git-filter branch)")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--json-out", default=os.environ.get("MUTATION_DIFF_JSON"))
    parser.add_argument(
        "--cap",
        type=int,
        default=int(os.environ.get("MUTATION_DIFF_CAP", "60")),
        help="Max in-scope mutants to execute; above this the run is reported as capped and NOT executed (bounds CI time).",
    )
    parser.add_argument(
        "--session-dir",
        default=os.environ.get("MUTATION_DIFF_SESSION_DIR", str(REPO / ".mutation-diff")),
        help="Scratch dir for the cosmic-ray session (kept off the *.sqlite name so a local db-guard hook does not flag it).",
    )
    parser.add_argument("--waivers", default=str(mutation_waivers.DEFAULT_REGISTRY))
    args = parser.parse_args(argv)

    config_path = Path(args.config)
    base = resolve_base(args.base, config_git_branch(config_path))
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")

    # Approved waivers that currently suppress a survivor (L2, #295). Missing
    # registry -> no waivers; this lane must never fall over because the file is
    # absent.
    waiver_path = Path(args.waivers)
    if waiver_path.exists():
        registry = mutation_waivers.load_registry(waiver_path)
        active = mutation_waivers.active_waivers(REPO, registry, date.today())
    else:
        registry, active = {"waivers": []}, {}

    session_dir = Path(args.session_dir)
    session_dir.mkdir(parents=True, exist_ok=True)
    session = session_dir / "session"

    base_report = {
        "lane": "mutation-diff-advisory",
        "task": "advisory mutation-diff lane (L1)",
        "base": base,
        "cap": args.cap,
        "tool": {"engine": "cosmic-ray", "version": ENGINE_VERSION},
        "classification_vocabulary": list(SURVIVOR_CLASSIFICATIONS),
        "waivers": {"registry_waivers": len(registry.get("waivers", [])), "active": len(active)},
    }

    changed = production_diff_files(base)
    base_report["changed_files"] = changed
    if not changed:
        emit(
            {**base_report, "status": "skipped", "reason": "no production diff under cpersona/ (test-only or vendored changes are the L3 lane's job)"},
            args.json_out,
            summary_path,
        )
        return 0

    # Baseline: the whole run is meaningless if the unmutated suite is red. Flag
    # loudly, but still exit 0 — this lane never blocks.
    baseline = run(["cosmic-ray", "baseline", str(config_path)], check=False)
    if baseline.returncode != 0:
        emit(
            {**base_report, "status": "baseline-failed", "reason": "unmutated suite did not pass; mutation results would be uninterpretable"},
            args.json_out,
            summary_path,
        )
        return 0

    run(["cosmic-ray", "init", "--force", str(config_path), str(session)])
    run(["cr-filter-git", "--config", str(effective_config(config_path, base, session_dir)), str(session)])
    run(["cr-filter-pragma", str(session)])
    # operators-filter is a no-op while exclude-operators is empty; run it anyway
    # so enabling exclusions later needs no wiring change. Note the positional
    # arg order (session then config) — unlike cr-filter-git's --config flag.
    run(["cr-filter-operators", str(session), str(config_path)], check=False)

    pending = count_pending(session)
    if pending > args.cap:
        emit(
            {
                **base_report,
                "status": "capped",
                "reason": f"{pending} in-scope mutants exceed cap={args.cap}; not executed to bound CI time",
                "in_scope": pending,
                "counts": {},
                "survivors": [],
            },
            args.json_out,
            summary_path,
        )
        return 0

    run(["cosmic-ray", "exec", str(config_path), str(session)])
    result = classify(session, REPO, active)
    emit({**base_report, "status": "ok", **result}, args.json_out, summary_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
