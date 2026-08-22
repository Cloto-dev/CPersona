#!/usr/bin/env python3
"""Advisory check: are the Japanese doc translations behind their English source?

English is the canonical language of docs/; a `<name>.ja.md` page is a
translation of `<name>.md` and records which revision it translated in a
first-line marker:

    <!-- i18n-source: docs/faq.md@<full commit sha> -->

This script compares each marker against the newest commit that actually
touched the English source. When the English page has moved on, the
translation is stale — readers of /ja/ are being served yesterday's contract.

ADVISORY by default: findings are printed as GitHub Actions ::warning
annotations (visible on the PR without failing it) and the exit code stays 0,
because forcing every English doc edit to carry a same-PR translation is too
heavy for a solo project. Pass --strict to exit 1 on findings instead — the
flag exists so the lane can be flipped to blocking without editing this file.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MARKER = re.compile(r"<!--\s*i18n-source:\s*(\S+)@([0-9a-f]{7,40})\s*-->")

findings: list[tuple[Path, str]] = []


def latest_sha(path: str) -> str:
    out = subprocess.run(
        ["git", "log", "-1", "--format=%H", "--", path],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def main() -> int:
    strict = "--strict" in sys.argv
    for ja in sorted((ROOT / "docs").glob("*.ja.md")):
        rel = ja.relative_to(ROOT)
        en_rel = str(rel).replace(".ja.md", ".md")
        if not (ROOT / en_rel).exists():
            findings.append((rel, f"has no English source ({en_rel} does not exist)"))
            continue
        m = MARKER.search(ja.read_text())
        if not m:
            findings.append(
                (rel, "is missing its i18n-source marker (first line: "
                      f"<!-- i18n-source: {en_rel}@<sha> -->)")
            )
            continue
        marked_path, marked_sha = m.group(1), m.group(2)
        if marked_path != en_rel:
            findings.append(
                (rel, f"marker names {marked_path}, expected {en_rel}")
            )
            continue
        current = latest_sha(en_rel)
        if not current.startswith(marked_sha) and current != marked_sha:
            findings.append(
                (rel,
                 f"translates {en_rel}@{marked_sha[:10]}, but the English page "
                 f"has moved to {current[:10]} — re-sync the translation and "
                 "update the marker")
            )

    if findings:
        for rel, msg in findings:
            # ::warning renders as an annotation on the Actions run / PR files
            # view; the plain line keeps local output readable.
            print(f"::warning file={rel},line=1::stale translation: {rel} {msg}")
            print(f"  - {rel}: {msg}", file=sys.stderr)
        print(f"{len(findings)} stale/unmarked translation(s)", file=sys.stderr)
        return 1 if strict else 0

    print("i18n drift: OK (all translations current)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
