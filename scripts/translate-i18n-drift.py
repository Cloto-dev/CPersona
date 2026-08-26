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
    translate-i18n-drift.py [--dry-run] [--model MODEL] [--effort LEVEL]

Runs through the Claude Code CLI rather than the API directly, so the work is
billed to a Claude subscription instead of per-token API usage. That is the
only reason for the indirection — `--json-schema` gives the same validated
object the API's structured outputs give, and the CLI additionally re-prompts
on a schema mismatch before giving up.

Requires `claude` on PATH, authenticated. In CI that means a
CLAUDE_CODE_OAUTH_TOKEN secret (`claude setup-token` mints one, valid a year);
locally it means an ordinary login. Exits non-zero if any stale page could not
be updated, so a CI step fails loudly rather than pushing a partial re-sync.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECKER = ROOT / "scripts" / "check-i18n-drift.py"
MARKER = re.compile(r"<!--\s*i18n-source:\s*(\S+)@blob:([0-9a-f]{40})\s*-->")

# Measured before choosing, on one fixed edit (a paragraph gaining a hedge, a
# negation, an inline identifier and a link), four conditions, three runs each.
# Every run passed the mechanical checks — applied cleanly, kept the heading ids,
# the marker, the code spans and the link target — so the choice came down to two
# things the checks cannot see:
#
#   Fidelity. Two Sonnet runs rendered "locking does not exempt a row from the
#   gate" as "does not spare it from EXCLUSION from the gate", which reads as the
#   opposite: excluded rather than evaluated. Both Opus settings got it right in
#   every run. That error survives every guard in this repository — valid
#   Markdown, correct marker, working links — so only a reader catches it, which
#   is exactly the load this tool is supposed to lighten.
#
#   Anchor size. Sonnet's search anchors averaged 153-182 characters against
#   Opus's 40-49, consistently across all runs. A longer anchor is echoed into
#   the replacement, so it widens the area that gets rewritten — against the
#   whole point of a search/replace design.
#
# Effort `low` was not a cost decision: `medium` bought no fidelity (both were
# perfect), produced slightly longer anchors, and ran ~50% slower. Reported cost
# is not comparable across the conditions — whichever ran first absorbed the
# cache write — and is not the reason for either choice.
#
# The measurement's bound: one page, one shape of change, three runs. Deletions
# were not covered, and neither were table edits or multi-site changes.
DEFAULT_MODEL = "claude-opus-5"
DEFAULT_EFFORT = "low"

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
page leaves them in English.
* Keep the page's line width. These files hard-wrap prose at roughly the width \
already in use, so re-wrap the whole paragraph you touch rather than leaving one \
long line: a paragraph that reflows into one line makes the reviewer's diff show \
the entire paragraph as changed, which hides the sentence that actually moved.\
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


def _checker_failed(proc: subprocess.CompletedProcess, why: str) -> str:
    """What to say when the checker did not answer — both streams, named.

    Same rule as the CLI failure below: a report that omits the stream the
    reason happened to land on is a report the reader cannot act on. The
    checker writes its findings to stdout and its tracebacks to stderr, and
    which one carries the explanation depends on how it failed.
    """
    return (
        f"check-i18n-drift.py {why} (exit {proc.returncode}).\n"
        f"  stderr: {proc.stderr.strip()[:800] or '(empty)'}\n"
        f"  stdout: {proc.stdout.strip()[:400] or '(empty)'}"
    )


def stale_pages() -> list[dict]:
    """Ask the checker what is stale. Its --json output is the contract.

    Exit 1 is ambiguous here and cannot be made otherwise: it is what the
    checker returns for "drift found" under --strict, and it is also what
    Python returns for an uncaught exception. Reading the verdict off the code
    alone therefore mistakes a crashed checker for a clean run with drift, and
    the empty stdout that follows reaches json.loads as "" — which raises a
    JSONDecodeError naming column 1 and nothing else, while the traceback that
    said why is still sitting unread in stderr.

    So the output decides, not the code: a verdict is always a JSON document
    (bug-261).
    """
    proc = subprocess.run(
        [sys.executable, str(CHECKER), "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode not in (0, 1):
        raise SystemExit(_checker_failed(proc, "failed"))
    if not proc.stdout.strip():
        raise SystemExit(_checker_failed(proc, "produced no output, so it did not run to a verdict"))
    try:
        return json.loads(proc.stdout)["stale"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise SystemExit(
            _checker_failed(proc, f"did not answer with the expected document ({exc})")
        ) from exc


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


def extract_edits(payload: dict, page: str) -> list[dict]:
    """Pull the validated edits out of a Claude Code result, or explain the failure.

    Two things are checked, not one. `subtype == "success"` alone is not enough:
    a run can finish successfully and still carry no `structured_output`, which
    the CLI documents as a case to treat as a failure. It matters here more than
    most places, because the thing that would sail through is an *empty* edit
    list — and an empty list is also the legitimate answer when the English
    change needs no Japanese change (a typo in a code sample). Conflating the two
    would re-stamp the marker on a page nobody translated, turning the drift
    check green over a translation that never happened.
    """
    subtype = payload.get("subtype")
    if subtype != "success":
        detail = payload.get("result") or payload.get("api_error_status") or ""
        if subtype == "error_max_structured_output_retries":
            raise RuntimeError(
                f"{page}: no valid edit list after the CLI's retries — the diff is "
                f"probably too large or too tangled for one pass. {detail}"
            )
        raise RuntimeError(f"{page}: the run ended as {subtype!r}. {detail}")

    structured = payload.get("structured_output")
    if structured is None:
        raise RuntimeError(
            f"{page}: the run succeeded without producing a structured result. That is "
            "not an empty edit list — it is no answer at all, and re-stamping the marker "
            "on it would mark an untranslated page as current."
        )
    return structured["edits"]


def request_edits(model: str, effort: str, diff: str, japanese: str, page: str) -> list[dict]:
    """Ask Claude Code for the edits, with as little else in the context as possible.

    Three flags do real work beyond the obvious:

    * ``--tools ""`` — the job is a transformation of text that is already in the
      prompt, so a tool call could only reach for something that is not the
      question. It also removes the whole class of "the page said to run this".
      Not ``--allowed-tools ""``, which was used here first and is a permission
      filter: a denied tool is still a tool whose schema was sent, and the CLI
      reported 26 of them loaded under it. Replaying one real page edit through
      this function cost 26,550 input tokens that way and 8,248 with the tools
      actually gone.

      Read those as input_tokens + cache_creation + cache_read. The figure this
      comment used to carry was cache-write alone, which reported 3,073 for a run
      that was in fact carrying the whole roster: it arrived as a cache hit, and
      a cache hit is cheaper but not absent. A metric that drops a term cannot
      show a cost that lives in that term.

      The count is a property of the CLI build, and the version measured here is
      not the version this workflow pins, so treat 26 as the shape rather than
      the constant. The flag empties the roster either way.
    * ``--setting-sources ""`` and a scratch cwd — a run inside the repository
      loads its CLAUDE.md and settings into every call.
    * ``--system-prompt`` (not ``--append-``) — replaces the default rather than
      adding to it, for the same reason.
    """
    prompt = (
        f"The English page `{page}` changed as follows:\n\n"
        f"```diff\n{diff}\n```\n\n"
        "Here is the current Japanese translation in full:\n\n"
        f"```markdown\n{japanese}\n```\n\n"
        "Return the search/replace edits that apply the same change to the Japanese page."
    )
    with tempfile.TemporaryDirectory() as scratch:
        proc = subprocess.run(
            [
                "claude",
                "-p",
                prompt,
                "--model",
                model,
                "--effort",
                effort,
                "--json-schema",
                json.dumps(EDITS_SCHEMA),
                "--output-format",
                "json",
                "--system-prompt",
                SYSTEM,
                "--setting-sources",
                "",
                "--tools",
                "",
            ],
            cwd=scratch,
            capture_output=True,
            text=True,
        )
    if proc.returncode != 0:
        # Both streams, because the CLI does not consistently use stderr for its
        # failures: an auth or startup problem can arrive on stdout with stderr
        # empty, and reporting only stderr turns that into "exited 1" with no
        # reason attached — a failure that tells the reader nothing is barely
        # better than a silent one.
        detail = proc.stdout.strip()
        try:
            failed = json.loads(detail)
        except json.JSONDecodeError:
            detail = detail[:600] or "(empty)"
        else:
            # The CLI reports a failed run as a normal result document, so the
            # reason is a field inside it rather than a message on a stream.
            # Truncating the raw JSON cuts that field off, which is how the first
            # CI failure arrived with "api_error" and nothing else.
            detail = (
                f"terminal_reason={failed.get('terminal_reason')} "
                f"api_error_status={failed.get('api_error_status')} "
                f"result={str(failed.get('result'))[:700]}"
            )
        raise RuntimeError(
            f"{page}: claude exited {proc.returncode}.\n"
            f"  stderr: {proc.stderr.strip()[:400] or '(empty)'}\n"
            f"  {detail}"
        )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{page}: could not read the CLI's response as JSON ({exc}). "
            f"First 200 characters: {proc.stdout[:200]!r}"
        ) from exc
    return extract_edits(payload, page)


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


def skip_advice(page: dict) -> str:
    """Why this page cannot be re-synced automatically, and what to do instead.

    Two different causes reach here and they take different fixes, so they are
    not given one message. A missing / legacy / wrong-source marker means the
    page never recorded what it translated: the English text is fine and the
    marker is the thing to repair. A missing source means the English page
    itself is gone, and no marker edit brings it back — telling that author to
    "fix the marker by hand" is advice that cannot work.
    """
    if page["reason"] == "missing_source":
        return (
            f"it translates {page['english']}, which does not exist. The English page was "
            "deleted or renamed, so there is no current text to re-sync against: delete "
            "this translation, or point it at the page that replaced it."
        )
    return (
        "nothing records what was translated, so there is no diff to apply. "
        "Fix the marker by hand."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--effort", default=DEFAULT_EFFORT, help="low | medium | high | xhigh | max")
    args = parser.parse_args()

    pages = stale_pages()
    if not pages:
        print("i18n drift: nothing stale — no translation needed")
        return 0

    skipped = [p for p in pages if p["reason"] != "stale"]
    for page in skipped:
        print(
            f"::warning file={page['translation']},line=1::{page['translation']} "
            f"cannot be re-synced automatically ({page['reason']}): {skip_advice(page)}",
        )

    work = [p for p in pages if p["reason"] == "stale"]
    if not work:
        return 1 if skipped else 0

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
            edits = request_edits(
                args.model, args.effort, diff, translation.read_text(), page["english"]
            )
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
