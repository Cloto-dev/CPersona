#!/usr/bin/env python3
"""Re-sync stale Japanese translations from the English diff that made them stale.

`check-i18n-drift.py` blocks a PR whose English edit shipped without its
translation. This is the other half: it takes each stale page and applies the
*same* change to the Japanese side, so the author is reviewing a translation
rather than writing one.

The unit of work is a diff, not a page. Every stale translation records the
English blob it was translated from, so the exact text the translator read is
recoverable; diffing that against the current file gives the change and nothing
else. A full re-translation would be easier to write and much worse to review —
the Japanese diff would cover the whole page, and the sentence whose meaning
actually moved would be indistinguishable from the thousand lines that merely
got rephrased.

That principle is enforced mechanically rather than requested politely: the
model returns search/replace pairs, and this script applies them itself, so
untouched prose is untouched by construction. A search string that does not
appear **exactly once** is a failure, not a fuzzy match — the same discipline a
scripted edit to source would use, and for the same reason (a short anchor that
matches twice silently edits the wrong paragraph).

What this does NOT do — deliberately:

  * It does not merge, and it does not approve. A machine cannot check that a
    translation still says what the English says: no gate here reads meaning,
    so a fidelity error (a negation dropped, a MUST softened to a SHOULD)
    passes every check this repository runs. What is automated is the first
    draft, not the review.
  * It does not touch a page whose marker is missing, legacy, or points at the
    wrong source. Those mean nobody recorded what was translated, so there is
    no diff to apply — a human decides what the page should say.

Usage:
    translate-i18n-drift.py [--dry-run] [--model MODEL]

Requires ANTHROPIC_API_KEY and the `anthropic` package. Exits non-zero if any
stale page could not be updated, so a CI step fails loudly rather than pushing
a partial re-sync.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECKER = ROOT / "scripts" / "check-i18n-drift.py"
MARKER = re.compile(r"<!--\s*i18n-source:\s*(\S+)@blob:([0-9a-f]{40})\s*-->")

DEFAULT_MODEL = "claude-opus-5"

SYSTEM = """\
You maintain the Japanese translations of a documentation site whose English \
pages are canonical. An English page changed; your job is to make the same \
change on the Japanese page, and nothing else.

You are given the unified diff of the English page and the full current text of \
the Japanese page. Return the edits to the Japanese page as search/replace \
pairs. Each `search` string must appear EXACTLY ONCE in the Japanese page — \
include enough surrounding text to make it unique. Return an empty list if the \
English change needs no Japanese change (a typo fix in a code sample, say).

Rules that outrank fluency:

* Translate the meaning, not the words. If the English gained a hedge \
("usually", "in the default configuration"), the Japanese must gain it too — \
those are the words a reader acts on.
* Preserve every explicit heading id: a heading written `## Retrieval \
{ #retrieval }` keeps `{ #retrieval }` verbatim. Links elsewhere in the site \
point at those ids, and dropping one breaks them silently.
* Preserve code, identifiers, environment variable names, file paths, tool \
names, and link targets exactly. Translate prose and link text.
* Do not touch the `i18n-source` marker comment. It is re-stamped mechanically \
after your edits apply.
* Do not restructure, re-order, or improve untouched Japanese text. A section \
the English diff did not touch must not appear in your edits at all.
* Match the surrounding register: this documentation uses だ/である-adjacent \
technical prose with English technical terms left in English where the existing \
page leaves them in English.\
"""

EDITS_SCHEMA = {
    "type": "object",
    "properties": {
        "edits": {
            "type": "array",
            "description": "Search/replace pairs to apply to the Japanese page, in order.",
            "items": {
                "type": "object",
                "properties": {
                    "search": {
                        "type": "string",
                        "description": "Text to find, appearing exactly once in the Japanese page.",
                    },
                    "replace": {
                        "type": "string",
                        "description": "Text to put in its place. Empty string deletes.",
                    },
                    "why": {
                        "type": "string",
                        "description": "The English change this edit mirrors, in one clause.",
                    },
                },
                "required": ["search", "replace", "why"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["edits"],
    "additionalProperties": False,
}


def stale_pages() -> list[dict]:
    """Ask the checker what is stale. Its --json output is the contract."""
    proc = subprocess.run(
        [sys.executable, str(CHECKER), "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode not in (0, 1):
        raise SystemExit(f"check-i18n-drift.py failed:\n{proc.stderr}")
    return json.loads(proc.stdout)["stale"]


def english_at(blob: str) -> str | None:
    """The English text as the translator read it, or None if it is unreachable.

    A shallow clone or an aggressive gc can leave the blob absent. That is a
    reason to stop, not to fall back to translating the whole page: the fallback
    would produce exactly the unreviewable full-page diff this exists to avoid.
    """
    proc = subprocess.run(
        ["git", "cat-file", "blob", blob], cwd=ROOT, capture_output=True, text=True
    )
    return proc.stdout if proc.returncode == 0 else None


def english_diff(before: str, after: str, path: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=6,
        )
    )


def request_edits(client, model: str, diff: str, japanese: str, page: str) -> list[dict]:
    prompt = (
        f"The English page `{page}` changed as follows:\n\n"
        f"```diff\n{diff}\n```\n\n"
        "Here is the current Japanese translation in full:\n\n"
        f"```markdown\n{japanese}\n```\n\n"
        "Return the search/replace edits that apply the same change to the Japanese page."
    )
    with client.messages.stream(
        model=model,
        max_tokens=32000,
        system=SYSTEM,
        output_config={
            "effort": "high",
            "format": {"type": "json_schema", "schema": EDITS_SCHEMA},
        },
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        message = stream.get_final_message()

    if message.stop_reason == "refusal":
        raise RuntimeError(f"the model declined to translate {page}")
    if message.stop_reason == "max_tokens":
        raise RuntimeError(
            f"the reply for {page} hit max_tokens — the diff is too large for one pass"
        )
    text = next((b.text for b in message.content if b.type == "text"), "")
    return json.loads(text)["edits"]


def apply_edits(japanese: str, edits: list[dict], page: str) -> str:
    """Apply each edit, refusing anything that is not an exact single match.

    Refusing a 0-match edit is obvious. Refusing a 2-match edit is the one that
    matters: a short anchor that appears twice would silently rewrite whichever
    paragraph came first, and both the tests and the doc guards would stay green
    because the file is still valid Markdown that still translates the right page.
    """
    for edit in edits:
        count = japanese.count(edit["search"])
        if count != 1:
            raise RuntimeError(
                f"{page}: the edit for {edit['why']!r} anchors on text appearing "
                f"{count} times; an edit is applied only on an exact single match"
            )
        japanese = japanese.replace(edit["search"], edit["replace"])
    return japanese


def restamp(japanese: str, english_rel: str, sha: str, page: str) -> str:
    marker = f"<!-- i18n-source: {english_rel}@blob:{sha} -->"
    if not MARKER.search(japanese):
        raise RuntimeError(f"{page}: the i18n-source marker went missing during the edit")
    return MARKER.sub(marker, japanese, count=1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()

    pages = stale_pages()
    if not pages:
        print("i18n drift: nothing stale — no translation needed")
        return 0

    skipped = [p for p in pages if p["reason"] != "stale"]
    for page in skipped:
        print(
            f"::warning file={page['translation']},line=1::{page['translation']} "
            f"cannot be re-synced automatically ({page['reason']}): nothing records what "
            "was translated, so there is no diff to apply. Fix the marker by hand.",
        )

    work = [p for p in pages if p["reason"] == "stale"]
    if not work:
        return 1 if skipped else 0

    client = None
    if not args.dry_run:
        import anthropic  # noqa: PLC0415 — only needed on the writing path

        client = anthropic.Anthropic()

    failures = list(skipped)
    for page in work:
        translation = ROOT / page["translation"]
        english = ROOT / page["english"]
        before = english_at(page["translated_blob"])
        if before is None:
            print(
                f"::warning file={page['translation']},line=1::the English content this "
                f"page was translated from (blob {page['translated_blob'][:10]}) is not in "
                "this clone, so the change cannot be isolated. Fetch full history "
                "(fetch-depth: 0) or re-sync this page by hand.",
            )
            failures.append(page)
            continue

        diff = english_diff(before, english.read_text(), page["english"])
        if args.dry_run:
            print(f"{page['translation']}: {len(diff.splitlines())} diff lines to mirror")
            continue

        try:
            edits = request_edits(client, args.model, diff, translation.read_text(), page["english"])
            updated = apply_edits(translation.read_text(), edits, page["translation"])
            updated = restamp(updated, page["english"], page["current_blob"], page["translation"])
        except (RuntimeError, json.JSONDecodeError) as exc:
            print(f"::error file={page['translation']},line=1::{exc}")
            failures.append(page)
            continue

        translation.write_text(updated)
        print(f"{page['translation']}: applied {len(edits)} edit(s)")
        for edit in edits:
            print(f"  - {edit['why']}")

    if failures:
        print(
            f"\n{len(failures)} page(s) still stale — a human has to finish them.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
