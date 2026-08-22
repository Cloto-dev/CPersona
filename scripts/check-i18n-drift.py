#!/usr/bin/env python3
"""Blocking check: are the Japanese doc translations behind their English source?

English is the canonical language of docs/; a `<name>.ja.md` page is a
translation of `<name>.md` and records *which content* it translated in a
first-line marker:

    <!-- i18n-source: docs/faq.md@blob:<git blob sha of the English file> -->

The marker names content, not history. An earlier version recorded the commit
that last touched the English page, which cannot survive this repository's
merge style: a PR that edits an English page and its translation together can
only write the *branch* commit into the marker, and squash-merge then gives
that same content a *different* sha on master — so the translation would be
reported stale the moment it landed, every time. Hashing the file sidesteps
rebases, squashes and cherry-picks entirely, because none of them change what
the translator actually read.

The value is the ordinary git blob id, so it can be checked by hand:

    git hash-object docs/faq.md

It is computed here from the file's bytes rather than shelled out, so the
check also works in a tree that is not a git checkout.

BLOCKING under --strict (how CI runs it): findings print as ::error
annotations and the exit code is 1. Without the flag the same findings print
as ::warning and the exit stays 0, which is the mode to use locally while a
translation is still being written.

A page with no `.ja.md` at all is not a finding — untranslated pages fall back
to English by design, and only a page claiming to be a translation can be
stale.
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MARKER = re.compile(r"<!--\s*i18n-source:\s*(\S+)@blob:([0-9a-f]{40})\s*-->")
# Recognised only to explain itself: the pre-content-hash marker format.
LEGACY_MARKER = re.compile(r"<!--\s*i18n-source:\s*(\S+)@(?!blob:)([0-9a-f]{7,40})\s*-->")

findings: list[tuple[Path, str]] = []


def blob_sha(path: Path) -> str:
    """The git blob id of a file: sha1(b"blob <len>\\0" + contents)."""
    data = path.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def main() -> int:
    strict = "--strict" in sys.argv
    for ja in sorted((ROOT / "docs").glob("*.ja.md")):
        rel = ja.relative_to(ROOT)
        en_rel = str(rel).replace(".ja.md", ".md")
        en = ROOT / en_rel
        if not en.exists():
            findings.append((rel, f"has no English source ({en_rel} does not exist)"))
            continue

        text = ja.read_text()
        m = MARKER.search(text)
        if not m:
            if LEGACY_MARKER.search(text):
                findings.append(
                    (rel, "carries the old commit-sha marker; re-stamp it as "
                          f"<!-- i18n-source: {en_rel}@blob:{blob_sha(en)} -->")
                )
            else:
                findings.append(
                    (rel, "is missing its i18n-source marker (first line: "
                          f"<!-- i18n-source: {en_rel}@blob:{blob_sha(en)} -->)")
                )
            continue

        marked_path, marked_sha = m.group(1), m.group(2)
        if marked_path != en_rel:
            findings.append((rel, f"marker names {marked_path}, expected {en_rel}"))
            continue

        current = blob_sha(en)
        if marked_sha != current:
            findings.append(
                (rel,
                 f"translates {en_rel}@blob:{marked_sha[:10]}, but that page's "
                 f"content is now blob:{current[:10]} — re-sync the translation "
                 "and re-stamp the marker")
            )

    if findings:
        level = "error" if strict else "warning"
        for rel, msg in findings:
            # Renders as an annotation on the Actions run / PR files view; the
            # plain line keeps local output readable.
            print(f"::{level} file={rel},line=1::stale translation: {rel} {msg}")
            print(f"  - {rel}: {msg}", file=sys.stderr)
        print(f"{len(findings)} stale/unmarked translation(s)", file=sys.stderr)
        return 1 if strict else 0

    print("i18n drift: OK (all translations current)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
