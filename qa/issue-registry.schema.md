# Issue registry schema

`qa/issue-registry.json` is the bug ledger. It is not documentation of past work:
`scripts/verify-issues.sh` reads every entry on each CI run and greps the named file
for the named pattern, so an entry that no longer describes the tree fails the build.
That is the point — the registry is a machine-checked claim about the code, and this
file describes the shape of one claim.

This document is named by the registry's own `$schema` field, and the verifier refuses
a registry whose `$schema` is absent, is a URL or an absolute path, or does not resolve
to a file in this repository. A pointer nothing follows is free to be wrong in the one
way that matters, and in a sibling registry it already was.

## File shape

```json
{
  "$schema": "qa/issue-registry.schema.md",
  "description": "...",
  "issues": [ { ... }, { ... } ]
}
```

There is no schema version number at the root. The registry has one producer and one
consumer, both in this repository and versioned together by git, so there is no version
skew for such a field to describe — and a number nothing compares is not a version. The
per-entry `version` field below is unrelated: it says which release a defect was found
in, which is a real fact about the defect.

## Entry fields

| Field | Required | Meaning |
| --- | --- | --- |
| `id` | yes | `bug-NNN`, permanent and never reused. The canonical identifier for the defect everywhere in the tree — code comments, fix markers, test names and registry patterns refer to a defect by this id and no other (`RELEASE_LIFECYCLE_STANDARD` §2.8). |
| `summary` | yes | What is wrong, in enough detail to judge severity without opening the file. Audit reports use severity-initial finding ids (`C-NN`/`H-NN`/`M-NN`/`L-NN`); carrying one here as a prefix records provenance, but it is not an identifier. |
| `severity` | yes | `CRITICAL` / `HIGH` / `MEDIUM` / `LOW`. |
| `discovered` | yes | ISO-8601 timestamp. |
| `version` | yes | The version the defect was found in. |
| `file` | yes | Repo-relative path the pattern is checked against. **Follows the anchor**, not the defect's history — when a fix lands in a different file than the report named, move this with it. |
| `pattern` | yes | A `grep -P` (falling back to `-E`) regular expression. |
| `expected` | yes | `present` or `absent`. See below. |
| `status` | yes | `open`, `fixed`, or `obsolete`. `obsolete` entries are skipped entirely. |
| `category` | no | Free-form kebab-case grouping (`doc-drift`, `test-coverage`, `fail-open-write-path`, …). |
| `commit` | no | The commit that introduced the defect, where known. |
| `fixed_in` | no | The version the fix landed in. |
| `fix_note` | no | What the fix does **and why that fix rather than another**. The rejected alternative is worth more than the diff, which git already has. |
| `fix_sketch` | no | For an open entry: the intended fix, when it is already understood. |
| `note` | no | Anything else — constraints, cross-references to related ids. |
| `routing` | no | Where an open entry is expected to be handled, when that is not this line of work (`"2.6, cross-consumer"`). Advisory only; the verifier does not read it. |

No field may contain a newline or an ASCII unit separator (`\x1f`); the verifier
serialises records with `\x1f` and aborts loudly rather than silently dropping a row.

## `expected`, and how it changes across a fix

`expected` says what the verifier should find, so it encodes which side of the fix the
pattern describes.

- **`present`** — the pattern must match. For an `open` entry this anchors the *defect*
  (proof it is still there). For a `fixed` entry it anchors the *fix* (proof it has not
  been reverted), which is why a `bug-NNN` marker in a comment makes a good pattern.
- **`absent`** — the pattern must NOT match. Used for a `fixed` entry whose fix
  *removed* the offending construct. A file that no longer exists counts as absent.

A missing file with `expected: present` is an `ERROR`. A `present` pattern that no
longer matches is `STALE`. An `absent` pattern that still matches is `UNFIXED`. Any of
the three fails the gate.

**Fixing a registered bug therefore means editing its entry in the same change**: flip
`status`, add `fixed_in` and `fix_note`, and re-anchor `pattern`/`expected`/`file` on
whatever now proves the fix. `AGENTS.md` states this as a rule; the gate enforces it.

## Choosing a pattern

The pattern is the whole mechanism, and two failure modes are easy to walk into:

1. **Not unique.** A string that also occurs elsewhere in the same file can never go
   `absent`. Check with `grep -c` before writing the entry — if the construct you
   removed appears three more times legitimately, anchor on something else the fix
   actually deleted.
2. **Not stable.** Prefer a construct the fix demonstrably changes (a `bug-NNN` marker,
   a renamed helper, a distinctive literal) over incidental formatting or a line the
   next refactor will move for unrelated reasons. Line numbers are never part of a
   pattern.

Escape regex metacharacters (`.` `(` `)` `*` `[`) — the pattern goes to `grep -P`, not
to a literal string comparison.

## What the verifier refuses

Every one of these was once a way for an entry to leave the run with a verdict nothing
produced, while the gate exited 0. CI reads only the exit code, so a green run is the
whole report and a row that was never checked is indistinguishable from a row that
passed. Each is now a named failure:

- a registry that does not parse, or whose root is not an object carrying an `issues` list
- a registry that declares zero entries — an empty ledger verifies nothing
- a `$schema` that is absent, is a URL or absolute path, or resolves to no file here
- a field containing the record separator, a newline or a carriage return
- an empty `pattern`, which `grep -c` matches against every line
- an `expected` that is neither `present` nor `absent`
- a mismatch between records emitted and records read, or between rows counted and the
  four verdict buckets

`--filter` selecting none of a registry that *was* read stays a passing result: that is
a real answer, not an empty one.

## Sibling registries

Other repositories in this project carry registries built on the same idea, with
verifiers grown from a common ancestor. They are **not identical and are not kept in
sync**: each has hardened against defects the others had not hit, and each carries
fields the others do not. Do not copy an entry between them expecting it to validate,
and do not assume a guard present here exists there. Each registry names its own schema
document, and `bug-NNN` ids are local to the repository that assigned them.

## Running it

```bash
bash scripts/verify-issues.sh              # everything
bash scripts/verify-issues.sh --filter open
```

Exit code 0 means every entry still describes the tree. `scripts/verify-issues.sh` is
read-only verification infrastructure: never modify the script to make a check pass.
