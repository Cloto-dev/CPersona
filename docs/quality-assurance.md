# Quality Assurance

This page describes how a cpersona release is gated. It is written for someone
deciding whether to trust the server with a corpus they care about, and for
contributors who want to know which checks their change will meet.

If you are here to *run* the checks before opening a pull request, the short
version is in
[CONTRIBUTING § The gates](https://github.com/Cloto-dev/cpersona/blob/master/CONTRIBUTING.md#the-gates).

## Audit-gated releases

Before a release is cut, the codebase goes through comprehensive multi-agent
audit rounds: independent finders per dimension, each finding then verified
adversarially from several lenses so that a plausible-but-wrong report does not
survive into a fix. v2.4.39 shipped after three such rounds — 43 fixes, every
one re-verified against the tree it landed on.

An audit produces findings, not fixes. Each survivor becomes a numbered entry
in the bug ledger before anything is edited, so the commit and the ledger agree
on what `bug-NNN` refers to.

## The bug ledger

Every audited defect lives in
[`qa/issue-registry.json`](https://github.com/Cloto-dev/cpersona/blob/master/qa/issue-registry.json)
with a machine-checkable code pattern: what the defect was, what reproduces it,
and what closed it.

[`scripts/verify-issues.sh`](https://github.com/Cloto-dev/cpersona/blob/master/scripts/verify-issues.sh)
checks the ledger against the tree and fails loudly if a fix marker disappears
or a removed defect returns. It is read-only infrastructure: it verifies the
ledger, and is not edited to make a check pass.

## Structural CI gates

Some invariants cannot be expressed as an ordinary test, because they are
properties of *every* call site rather than of one behaviour. Those are enforced
by AST- and behaviour-level gates in the pytest suite, run on Python 3.11 and
3.13:

- every writer holds the shared write lock;
- agent-scoped SQL carries its isolation predicates;
- identity and dedup probes carry the project and channel axes;
- `check_health` performs no embedding network I/O while holding the lock.

A gate of this kind fails on the call site that forgot the rule, which is what
separates it from a test that happens to cover today's call sites.

## Documented facts are gated too

Hand-written numbers rot. Tool counts, the schema version and
environment-variable defaults stated in the docs are checked against the source
that defines them, so a doc that disagrees with the code fails CI rather than
misleading a reader. Version claims are checked against the release tags. The
Japanese pages are checked against the English content they were translated
from, so a translation cannot silently fall behind its source — and every page
and nav label must either carry a translation or declare that it stays English,
so a new page cannot quietly sit outside the translated site.

The gates live in
[`scripts/`](https://github.com/Cloto-dev/cpersona/tree/master/scripts):
`check-docs-facts.py`, `check-doc-anchors.py`, `check-i18n-drift.py` and
`check-i18n-coverage.py`.

## Mutation proof

A green suite proves the tests ran, not that they would have noticed. The seams
that carry the isolation and locking invariants are mutated deliberately in CI,
and the proof requires that the suite goes red for each mutation. A gate that
stays green under a mutation is reported as a gap in the gate, not as a pass.

## Release lifecycle

The release process itself is specified in
[RELEASE_LIFECYCLE_STANDARD](RELEASE_LIFECYCLE_STANDARD.md) (v1.0), piloted here
as the reference implementation for Cloto-family projects. Which line to run,
and how long a line keeps receiving fixes, is
[SUPPORT.md](https://github.com/Cloto-dev/cpersona/blob/master/SUPPORT.md).

## By the numbers

- **~21,450 LOC** Python across focused modules, plus a 3,660-line vendored MCP
  common snapshot
- **~1,579 test functions** across ~129 test modules — ~2,010 cases once the
  behavioural matrix is parametrised (~46,285 LOC, more test code than server
  code), including the structural-enforcement gates above
- **Schema v13** (auto-migrating)
- **MIT License**

These counts are approximations on purpose, and are themselves gated: they are
re-measured from the tree in CI and fail once they drift far enough to mislead.
