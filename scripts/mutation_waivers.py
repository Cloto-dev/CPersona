#!/usr/bin/env python3
"""Mutation-diff waiver registry — the L2 triage home for advisory survivors
(an earlier decision / an earlier decision).

A waiver says "this specific survivor is not a real test gap, for this reason"
so it stops counting against the advisory lane's un-waived total. It is the
counterpart to qa/issue-registry.json (verified mechanically by CI), adapted to
the one property mutation survivors need and issues do not: **content-based
identity**. A survivor is NOT pinned by line number — a refactor that moves a
function must not silently re-arm every waiver in it, and an edit to the mutated
line itself MUST invalidate its waiver (the code it was reasoning about changed).

Two hashes, two jobs:
  * code_context_hash — sha256 of the mutated line's whitespace-normalised text.
    The verifier checks it still appears in the file; if not, the code changed
    and the waiver is stale. Needs no AST, so the verifier stays simple.
  * fingerprint — code_context_hash folded together with module / enclosing
    definition / operator. This is the exact key mutation-diff.py matches a live
    survivor against (cosmic-ray hands it the definition name); it survives line
    shifts and dies when the line's content changes.

Enforcement (failing CI on an un-waived survivor) is NOT here — that is L6
(#299). At L2 this registry only annotates the advisory report and is checked
for its own integrity. Approval is human-only: a waiver suppresses a survivor
only when approved_by == 'human'. The AI submits waivers with approved_by=null
(pending) and never self-approves its own — an earlier decision's single-maintainer rule.

CLI:
    python scripts/mutation_waivers.py            # verify the registry (advisory)
    python scripts/mutation_waivers.py --strict   # also fail on expiry / drift (L6 turns this on)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_REGISTRY = REPO / "qa" / "mutation-waivers.json"

# The engine the fingerprints were computed against. A waiver is only trusted
# under the same version — mutant identity can shift across cosmic-ray upgrades.
TOOL_VERSION = "cosmic-ray==8.4.6"

# Triage vocabulary — the single source shared with scripts/mutation-diff.py.
CLASSIFICATIONS = (
    "test_added",           # a new test should kill it (real gap, fix the test)
    "code_changed",         # the mutated line is itself the intended change
    "intentional_behavior", # the mutation describes an allowed behaviour
    "redundant_defense",    # equivalent under a redundant guard (evidence MUST
                            # cite the structural gate / sentinel that holds it)
    "equivalent",           # observably indistinguishable from the original
    "operator_noise",       # the operator is noise for this codebase
    "infrastructure_error", # flaky/order-dependent test, not a real survivor
)

_REQUIRED_FIELDS = (
    "id", "fingerprint", "code_context_hash", "operator", "location",
    "classification", "reason", "evidence", "created", "reverify_by",
    "tool_version", "approved_by",
)


def normalize_line(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def code_context_hash(line_text: str) -> str:
    return hashlib.sha256(normalize_line(line_text).encode()).hexdigest()[:16]


def fingerprint(module_path: str, definition: str | None, operator: str, line_text: str) -> str:
    parts = "\0".join([module_path, definition or "", operator, normalize_line(line_text)])
    return hashlib.sha256(parts.encode()).hexdigest()[:16]


def line_at(repo: Path, module_path: str, line_no: int) -> str | None:
    p = repo / module_path
    if not p.exists():
        return None
    lines = p.read_text(encoding="utf-8").splitlines()
    return lines[line_no - 1] if 1 <= line_no <= len(lines) else None


def survivor_fingerprint(
    repo: Path, module_path: str, definition: str | None, operator: str, line_no: int
) -> tuple[str, str] | tuple[None, None]:
    """(fingerprint, code_context_hash) for a live survivor, or (None, None) if
    the file/line is gone. Computed from the CURRENT source so a stale waiver
    (whose line changed) will not match."""
    line = line_at(repo, module_path, line_no)
    if line is None:
        return None, None
    return fingerprint(module_path, definition, operator, line), code_context_hash(line)


def load_registry(path: Path = DEFAULT_REGISTRY) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _code_present(repo: Path, waiver: dict) -> bool:
    """True if the mutated line's content still exists in its file (verified by
    code_context_hash, no AST needed). False => the code changed => stale."""
    p = repo / waiver.get("location", {}).get("file", "")
    if not p.exists():
        return False
    want = waiver.get("code_context_hash")
    return any(code_context_hash(line) == want for line in p.read_text(encoding="utf-8").splitlines())


def verify(repo: Path, registry: dict, today: date) -> list[dict]:
    """Return a list of problems, each {id, severity, kind, detail}. 'hard' =
    the registry is malformed and should always be fixed (block even at L2);
    'soft' = expiry / drift, advisory until L6 turns on --strict."""
    problems: list[dict] = []
    seen_ids: set[str] = set()
    seen_fps: set[str] = set()
    for w in registry.get("waivers", []):
        wid = w.get("id", "<no-id>")

        def add(kind: str, detail: str, severity: str = "hard") -> None:
            problems.append({"id": wid, "severity": severity, "kind": kind, "detail": detail})

        missing = [f for f in _REQUIRED_FIELDS if f not in w]
        if missing:
            add("missing_field", f"missing {missing}")
            continue  # can't sensibly check the rest

        if wid in seen_ids:
            add("duplicate_id", f"id {wid} used more than once")
        seen_ids.add(wid)
        if w["fingerprint"] in seen_fps:
            add("duplicate_fingerprint", f"fingerprint {w['fingerprint']} waived twice")
        seen_fps.add(w["fingerprint"])

        if w["classification"] not in CLASSIFICATIONS:
            add("bad_classification", f"{w['classification']!r} not in {CLASSIFICATIONS}")
        # No operator-wide / wildcard waivers: a single concrete operator only.
        op = w["operator"]
        if not isinstance(op, str) or not op or "*" in op:
            add("bad_operator", f"operator must be one concrete name, got {op!r}")
        loc = w["location"]
        if not isinstance(loc, dict) or not loc.get("file") or "*" in str(loc.get("file", "")):
            add("wildcard_location", f"location.file must be one concrete path, got {loc!r}")
        if not loc.get("definition"):
            add("wildcard_location", "location.definition is required (no file-wide waivers)")
        # Approval is human-only. null = pending (does not suppress); any other
        # value is a forged approval and is rejected.
        if w["approved_by"] not in (None, "human"):
            add("bad_approval", f"approved_by must be 'human' or null, got {w['approved_by']!r}")

        if w["tool_version"] != TOOL_VERSION:
            add("tool_version_mismatch", f"{w['tool_version']} != {TOOL_VERSION}", severity="soft")
        try:
            expires = date.fromisoformat(w["reverify_by"])
            if expires < today:
                add("expired", f"reverify_by {w['reverify_by']} has passed", severity="soft")
        except (ValueError, TypeError):
            add("bad_date", f"reverify_by not an ISO date: {w['reverify_by']!r}")
        if not _code_present(repo, w):
            add("code_changed", "the mutated line's content is gone from the file", severity="soft")

    return problems


def active_waivers(repo: Path, registry: dict, today: date) -> dict[str, dict]:
    """{fingerprint: waiver} for waivers that currently SUPPRESS a survivor:
    human-approved, unexpired, right tool version, and whose code still exists.
    A pending (approved_by=null) or stale waiver is silently inactive."""
    out: dict[str, dict] = {}
    for w in registry.get("waivers", []):
        if w.get("approved_by") != "human":
            continue
        if w.get("tool_version") != TOOL_VERSION:
            continue
        try:
            if date.fromisoformat(w["reverify_by"]) < today:
                continue
        except (ValueError, TypeError, KeyError):
            continue
        if not _code_present(repo, w):
            continue
        out[w["fingerprint"]] = w
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    parser.add_argument("--repo", default=str(REPO))
    parser.add_argument("--strict", action="store_true", help="also fail on soft problems (expiry / drift); L6 turns this on in CI")
    args = parser.parse_args(argv)

    repo = Path(args.repo)
    registry = load_registry(Path(args.registry))
    problems = verify(repo, registry, date.today())

    hard = [p for p in problems if p["severity"] == "hard"]
    soft = [p for p in problems if p["severity"] == "soft"]
    total = len(registry.get("waivers", []))
    active = len(active_waivers(repo, registry, date.today()))
    print(f"mutation-waivers: {total} waivers ({active} active/suppressing), {len(hard)} hard + {len(soft)} soft problems")
    for p in problems:
        print(f"  [{p['severity']}] {p['id']}: {p['kind']} — {p['detail']}")

    # L2 is advisory: block only on a malformed registry (hard). Expiry/drift
    # (soft) are reported but do not fail until L6 flips --strict.
    return 1 if (hard or (args.strict and soft)) else 0


if __name__ == "__main__":
    sys.exit(main())
