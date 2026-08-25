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
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MARKER = re.compile(r"<!--\s*i18n-source:\s*(\S+)@blob:([0-9a-f]{40})\s*-->")
# Recognised only to explain itself: the pre-content-hash marker format.
LEGACY_MARKER = re.compile(r"<!--\s*i18n-source:\s*(\S+)@(?!blob:)([0-9a-f]{7,40})\s*-->")

findings: list[tuple[Path, str]] = []
# The same findings, in the shape a program needs: --json emits these. Human
# output is a rendering of this list, never the other way round — a caller that
# has to scrape ::error annotations is reading a display format, and display
# formats change (colour, prefixes, wording) without anyone counting that as a
# breaking change.
machine_findings: list[dict] = []


def blob_sha(path: Path) -> str:
    """The git blob id of a file: sha1(b"blob <len>\\0" + contents)."""
    data = path.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def record(
    rel: Path, en_rel: str, reason: str, translated_blob: str | None, current_blob: str | None
) -> None:
    """Add the machine-readable twin of the finding just appended.

    `translated_blob` is the English content this page was translated from, and
    it is the field an automated re-translation needs: with it the updater can
    fetch that exact blob and diff it against the current file, which is what
    makes a targeted edit possible instead of a full re-translation. It is None
    when the page never recorded one.

    `current_blob` is None in the one case where there is nothing to hash: the
    English page named by the translation does not exist. Every finding has to
    reach this list even when it carries no blobs, because the exit code and
    this list are read by different callers — a finding that only reaches the
    human output is invisible to the updater, which then reports nothing to fix
    while the run fails.
    """
    machine_findings.append(
        {
            "translation": str(rel),
            "english": en_rel,
            "reason": reason,
            "translated_blob": translated_blob,
            "current_blob": current_blob,
        }
    )


def main() -> int:
    strict = "--strict" in sys.argv
    as_json = "--json" in sys.argv
    for ja in sorted((ROOT / "docs").glob("*.ja.md")):
        rel = ja.relative_to(ROOT)
        en_rel = str(rel).replace(".ja.md", ".md")
        en = ROOT / en_rel
        if not en.exists():
            findings.append((rel, f"has no English source ({en_rel} does not exist)"))
            # bug-260: this branch used to report to the human output only, so a
            # run could exit non-zero while telling the updater there was
            # nothing to do.
            record(rel, en_rel, "missing_source", None, None)
            continue

        text = ja.read_text()
        m = MARKER.search(text)
        if not m:
            if LEGACY_MARKER.search(text):
                findings.append(
                    (rel, "carries the old commit-sha marker; re-stamp it as "
                          f"<!-- i18n-source: {en_rel}@blob:{blob_sha(en)} -->")
                )
                record(rel, en_rel, "legacy_marker", None, blob_sha(en))
            else:
                findings.append(
                    (rel, "is missing its i18n-source marker (first line: "
                          f"<!-- i18n-source: {en_rel}@blob:{blob_sha(en)} -->)")
                )
                record(rel, en_rel, "missing_marker", None, blob_sha(en))
            continue

        marked_path, marked_sha = m.group(1), m.group(2)
        if marked_path != en_rel:
            findings.append((rel, f"marker names {marked_path}, expected {en_rel}"))
            record(rel, en_rel, "wrong_source", marked_sha, blob_sha(en))
            continue

        current = blob_sha(en)
        if marked_sha != current:
            findings.append(
                (rel,
                 f"translates {en_rel}@blob:{marked_sha[:10]}, but that page's "
                 f"content is now blob:{current[:10]} — re-sync the translation "
                 "and re-stamp the marker")
            )
            record(rel, en_rel, "stale", marked_sha, current)

    if as_json:
        # Exit code still carries the verdict under --strict, so a caller can
        # branch on either. Findings go to stdout alone: annotations on stdout
        # would corrupt the document.
        json.dump({"stale": machine_findings}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 1 if (findings and strict) else 0

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
