#!/usr/bin/env python3
"""Blocking check: is every page and every nav label accounted for in Japanese?

`check-i18n-drift.py` answers "is this translation current?", and it can only
ask that about pages that *have* a translation: it walks `docs/*.ja.md`. A new
English page has no `.ja.md`, so it is not walked, and an untranslated page is
not a finding there — English fallback is a deliberate design, not a defect.

The consequence is the gap this file closes. Adding a page to the site takes
three steps, and until now only the third had a detector:

  1. write `<page>.ja.md`                     — nothing reported its absence
  2. add the nav label to `nav_translations`  — nothing reported its absence
  3. carry `{ #english-slug }` on the headings — check-doc-anchors.py

Measured on this repository: `docs/quality-assurance.md` shipped with neither 1
nor 2, and every gate stayed green. The Japanese site served one page of English
prose under an English nav label sitting between 設定リファレンス and 設計ドキュメント,
which is exactly the "the reader cannot tell" failure the drift gate was made
blocking to prevent.

So this gate does not require a translation. It requires that the *absence* of
one be declared:

    docs/.i18n-english-only     one path per line, `#` comments allowed

A page listed there is English-only on purpose and passes. A page that is
neither translated nor listed fails, with the two ways to fix it in the message.
That keeps English-only pages possible while closing the silent path — the
distinction this repository already draws for mutation waivers and the bug
ledger: an exception is fine, an unrecorded exception is not.

The nav half is checked the same way, against `mkdocs.yml`: every leaf label
under `nav:` must have an entry in `nav_translations`, because a missing entry
does not fail the build — it renders the English string into the Japanese nav.

BLOCKING under --strict (how CI runs it): findings print as ::error annotations
and the exit code is 1. Without the flag they print as ::warning and the exit
stays 0, which is the mode for working on a page before its translation exists.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
ALLOWLIST = DOCS / ".i18n-english-only"
MKDOCS = ROOT / "mkdocs.yml"

# A nav entry is `  - Label: path.md` (leaf) or `  - Label:` (section heading).
# Both need a translated label; only the first also needs a translated page,
# which the page half of this check reaches through the filesystem instead.
NAV_LEAF = re.compile(r"^\s*-\s+(?P<label>[^:]+):\s*(?P<path>\S+\.md)\s*$")
NAV_SECTION = re.compile(r"^\s*-\s+(?P<label>[^:]+):\s*$")
NAV_TRANSLATION = re.compile(r"^\s+(?P<label>[^:#]+):\s*(?P<value>.+?)\s*$")


def read_allowlist() -> set[str]:
    if not ALLOWLIST.exists():
        return set()
    entries = set()
    for line in ALLOWLIST.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            entries.add(line)
    return entries


def nav_labels() -> tuple[list[str], int]:
    """Leaf and section labels under `nav:`, and the line `nav:` starts on.

    Parsed rather than loaded with a YAML reader on purpose: mkdocs.yml carries
    `!!python/name:` tags that a plain safe_load refuses, and pulling in the
    loader that accepts them to read six strings would make this check depend on
    the build environment it is supposed to be able to run ahead of.
    """
    lines = MKDOCS.read_text().splitlines()
    labels: list[str] = []
    start = -1
    for i, line in enumerate(lines):
        if line.startswith("nav:"):
            start = i
            continue
        if start < 0:
            continue
        if line and not line[0].isspace():
            break  # dedented to a new top-level key: nav is over
        for pattern in (NAV_LEAF, NAV_SECTION):
            m = pattern.match(line)
            if m:
                labels.append(m.group("label").strip())
                break
    return labels, start


def nav_translations() -> set[str]:
    """The labels `nav_translations:` provides, by indentation, not by YAML."""
    lines = MKDOCS.read_text().splitlines()
    out: set[str] = set()
    base = None
    for line in lines:
        if line.strip().startswith("nav_translations:"):
            base = len(line) - len(line.lstrip())
            continue
        if base is None:
            continue
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= base:
            break  # dedented out of the mapping
        m = NAV_TRANSLATION.match(line)
        if m:
            out.add(m.group("label").strip())
    return out


def main() -> int:
    strict = "--strict" in sys.argv
    findings: list[tuple[str, str]] = []

    allowed = read_allowlist()
    seen_allowed: set[str] = set()

    for en in sorted(DOCS.glob("*.md")):
        if en.name.endswith(".ja.md"):
            continue
        rel = str(en.relative_to(ROOT))
        ja = en.parent / (en.name[: -len(".md")] + ".ja.md")
        if rel in allowed:
            seen_allowed.add(rel)
            # A waiver that outlived its reason is the failure mode of every
            # waiver list: the page got translated, the line stayed, and from
            # then on the list says something about the tree that is not true.
            # Left unreported it also disarms the check — delete the
            # translation later and the stale line silently permits it.
            if ja.exists():
                findings.append(
                    (
                        rel,
                        f"is declared English-only in docs/.i18n-english-only, but "
                        f"{ja.relative_to(ROOT)} exists — drop the line, the page is "
                        "translated.",
                    )
                )
            continue
        if not ja.exists():
            findings.append(
                (
                    rel,
                    f"has no Japanese translation and is not declared English-only. "
                    f"Either add {ja.relative_to(ROOT)}, or add a line "
                    f"`{rel}` to docs/.i18n-english-only saying why it stays English.",
                )
            )

    # An allowlist entry for a page that no longer exists is a stale waiver, and
    # a waiver nobody can see expiring is how a list stops describing the tree.
    for entry in sorted(allowed - seen_allowed):
        findings.append(
            (
                "docs/.i18n-english-only",
                f"lists {entry}, which is not an untranslated English page "
                "(it was translated, renamed or removed) — drop the line.",
            )
        )

    labels, nav_start = nav_labels()
    if nav_start < 0:
        findings.append(("mkdocs.yml", "has no `nav:` block — this check cannot see the nav."))
    else:
        provided = nav_translations()
        for label in labels:
            if label not in provided:
                findings.append(
                    (
                        "mkdocs.yml",
                        f"nav label {label!r} has no entry under nav_translations, "
                        "so the Japanese nav renders it in English. Add "
                        f"`{label}: <日本語>` there.",
                    )
                )

    if findings:
        level = "error" if strict else "warning"
        for where, msg in findings:
            print(f"::{level} file={where},line=1::i18n coverage: {where} {msg}")
            print(f"  - {where}: {msg}", file=sys.stderr)
        print(f"{len(findings)} i18n coverage finding(s)", file=sys.stderr)
        return 1 if strict else 0

    print(
        f"i18n coverage: OK ({len(labels)} nav labels translated, "
        f"{len(allowed)} page(s) declared English-only)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
