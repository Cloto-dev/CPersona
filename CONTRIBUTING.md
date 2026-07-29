# Contributing

Thanks for looking. cpersona is a small project with a single maintainer, so
this document exists to save you from guessing what the gates are — not to put
a process between you and a fix.

Bug reports and questions do not need any of this: see
[SUPPORT.md](SUPPORT.md). Security issues have their own channel and must not
go in a public issue — see [SECURITY.md](SECURITY.md).

## Before you write code

Open an issue first if the change touches any of these. Not because they are
unwelcome, but because they have constraints that are cheaper to discuss than
to discover in review:

- **The database schema.** The schema is preserved across a release line, so a
  migration is a release-planning decision, not a patch.
- **MCP tool contracts** — tool names, argument shapes, or response shapes.
  Agents branch on these, so a change has to be staged through the
  pre-release ladder described in [SUPPORT.md](SUPPORT.md).
- **New tools.** The tool count is already above what directory reviewers
  consider focused; adding one needs a reason that consolidation cannot serve.

Everything else — bug fixes, tests, documentation, internal refactors — you can
just send.

## Development setup

Python 3.11 or newer (see `requires-python` in `pyproject.toml`) and
[uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/Cloto-dev/cpersona.git
cd cpersona
uv sync --frozen
```

No embedding server is needed to develop or to run the suite: the tests are
hermetic and bounded, and the embedding path is exercised through fakes.

## The gates

Run these three before opening a PR. CI runs the same checks, so a green local
run is a good predictor of a green PR:

```bash
uvx ruff@0.15.21 check .      # lint — pinned version, same as CI
uv run --frozen pytest        # the test suite
bash scripts/verify-issues.sh # the bug ledger must still verify
```

CI additionally builds the wheel and imports it from a clean environment, and
runs a mutation proof over the seams that carry it. If one of those fails on
your PR and you cannot reproduce it locally, say so in the PR — it is more
likely a gap in the gate than something you did.

## The bug ledger

`qa/issue-registry.json` is the record of every known defect: what it was, the
pattern that reproduces it, and what closed it. Two rules:

- **Number the bug before you fix it.** Add the entry first, then write the
  fix, so the commit and the ledger agree on what `bug-NNN` refers to. Fixing
  first and numbering afterwards is how entries end up pointing at the wrong
  change.
- **`scripts/verify-issues.sh` is read-only infrastructure.** It verifies the
  ledger against the tree; do not edit it to make a check pass.

When you fix a registered bug, update its entry (`status`, `fix_note`, and the
`pattern`/`expected` fields if the reproduction moved) as part of the same
change, then make `verify-issues.sh` pass.

## Commits and PRs

- Write commit messages and code comments in **English**. This is a public
  repository and English is its working language.
- Keep your own authorship. Do not set the author to the project identity.
- One reviewable change per PR. If a fix and a refactor are separable, they are
  two PRs.
- Tests belong with the change. A bug fix without a test that fails before it
  is a fix that can come back.

## Release lines

Which line your change lands on, what a pre-release means, and how a line is
promoted are all defined in [SUPPORT.md](SUPPORT.md), which is the operative
instance of [docs/RELEASE_LIFECYCLE_STANDARD.md](docs/RELEASE_LIFECYCLE_STANDARD.md).
You do not need to pick a version — the maintainer does that at release time.
