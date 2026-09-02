# Support Policy

This document defines the release lifecycle and support policy for cpersona.
It is written to be line-agnostic: the same rules apply to every release line
(2.4.x, 2.5.x, ...), so the policy survives line transitions unchanged.

**Looking to report a problem?** See [Reporting an issue](#reporting-an-issue)
at the end of this document.

**Looking for how the server behaves or how to run it?** The canonical
documentation site is <https://cloto-dev.github.io/CPersona/> — behavior
contracts, operations runbook, configuration reference, and FAQ.

This policy is the operative instance of the
[Release Lifecycle Standard](docs/RELEASE_LIFECYCLE_STANDARD.md), which is
piloted in this repository as its reference implementation and quality
baseline before wider Cloto-family adoption.

## Release tiers

Every release line is in exactly one tier at any time. The tier attaches to
the line (e.g. 2.4.x), not to an individual version.

| Tier | Meaning | Fix policy |
| --- | --- | --- |
| **Stable** | Certified by the maintainer after production soak. Recommended for all users; the marketplace serves this line by default. | Critical bug fixes, data-loss fixes, and security fixes only (backported at the maintainer's discretion). |
| **Current** | The newest release line. It has passed the full release gate (test suite, lint, issue-registry verification, comprehensive audits) but has not yet earned the production-soak certification. | All bug fixes land here first — this is where development happens. |
| **Experimental** | Alpha / beta (and, when needed, rc) pre-releases of the next line. Opt-in only; no guarantees of any kind. | Fixes ship in the next pre-release. |

Naming note: **Current** follows the Node.js release vocabulary — the newest
supported release line, distinct from the production-recommended tier. It is
*not* the BSD `-CURRENT` (an unstable development head); that role is played
by **Experimental** here.

## Lifecycle

```
X.Y.0aN → X.Y.0bN (→ X.Y.0rcN if needed) → X.Y.0     [Experimental]
                                              │  release gate passed
                                              ▼
                                           Current
                                              │  production soak + maintainer certification
                                              ▼
                                           Stable ──── the previously Stable line enters Grace
                                              │  a successor line is certified Stable
                                              ▼
                                     Grace (30 days) → EOL
```

### Pre-releases (Experimental)

- Version strings use PEP 440 canonical form: `2.5.0a1`, `2.5.0b1`, `2.5.0rc1`.
  Git tags match 1:1 (`v2.5.0a1`).
- pip excludes pre-releases unless explicitly requested (`pip install --pre`),
  so the Experimental tier is opt-in by construction.
- The `rc` stage is optional: it is added only when beta soak surfaces enough
  churn to justify one final gate. Skipping it (alpha → beta → final) is the
  default.

### Promotion to Stable

Promotion is an explicit, event-based maintainer decision — there is no fixed
clock. Guideline: several weeks of production soak with no new critical or
high-severity defects. The certification date is recorded in the Status table
below; it also starts the superseded line's grace window.

### Grace window

When a successor line is certified Stable, the superseded line keeps its
Stable fix policy (critical / data-loss / security only) for **30 days from
the certification date**, then reaches EOL.

- The clock anchors on the certification event and is **not** reset by patch
  releases inside the window.
- Fixes for issues accepted within the window may ship after it closes.
- Line transitions preserve the database schema, so rollback and roll-forward
  are free on the data. They do **not** guarantee the MCP tool contract: 2.5.2
  changes the `store` response shape and drops `check_health`'s `healthy`
  boolean (see the release notes for the migration). Contract changes ship
  through the pre-release ladder, never straight to a final release.
- If a future transition ever requires a schema or data migration, the
  maintainer SHOULD extend the grace window before certifying the successor.

### EOL

No further fixes. Security fixes after EOL are at the maintainer's sole
discretion and must not be relied upon.

## Status

| Line | Tier | Notes |
| --- | --- | --- |
| 2.4.x | **Stable** | Certified Stable; the marketplace serves this line by default. Enters Grace 30 days after 2.5.x is certified Stable. |
| 2.5.x | **Current** | Latest release: 2.5.10. Passed the full release gate (test suite, lint, issue-registry verification, audits); all fixes land here. Awaiting production-soak certification to Stable. |

Certification and EOL dates are recorded in this table as they occur.

## Known issues

- **All released versions through 2.5.4a2 — undated rows outrank every dated row
  under confidence scoring (bug-207, HIGH).** With `CPERSONA_CONFIDENCE_ENABLED=true`
  (not the default), a memory or episode whose timestamp is missing or unparseable
  was scored as written this instant: it took a time-decay of 1.0 that no dated row
  can reach, sorted above every dated row and survived the quality gate that cut
  them. Production impact scales with undated rows — in the reference deployment
  two thirds of episodes had no `start_time`. **Fixed in v2.5.4** (unknown age is
  imputed as the midpoint of the corpus age range and flagged `age_unknown: true`);
  the Stable 2.4.x line is affected and unfixed. Workaround on affected versions:
  leave confidence off (the default), or backfill timestamps
  (`check_health(checks=["missing_episode_start_time"], fix=true)` on 2.5.5+).


- **v2.5.2 and earlier — the HTTP transport starts unauthenticated on a loopback
  bind (bug-198, HIGH).** With `CPERSONA_TRANSPORT=streamable-http` and no
  `CPERSONA_AUTH_TOKEN`, these versions refuse to start only on a *non-loopback*
  bind. A loopback bind starts and logs that it is "bound to loopback 127.0.0.1
  only" — which reads as an all-clear, and is not one. **A loopback bind does not
  bound reachability**: tunnels (cloudflared, ngrok), reverse proxies,
  `kubectl port-forward` and published container ports all forward to
  `127.0.0.1`. If any of those sits in front of the process, every tool is
  callable with no credentials by anyone who can reach the front door, including
  `delete_agent_data` and the file-reading/writing `export_memories` /
  `import_memories`. **If you serve CPersona over HTTP, set
  `CPERSONA_AUTH_TOKEN` now** — the stdio transport is unaffected, and so is any
  HTTP deployment that already sets a token. **Fixed in v2.5.3**, which refuses
  to start without a token wherever it binds; see the
  [remote transport reference](https://cloto-dev.github.io/CPersona/configuration/#remote-http-transport)
  for the migration if you are deliberately running without authentication.
  Details: `qa/issue-registry.json` (bug-198).

- **v2.5.2 and earlier — `check_health` reports `degraded` for memories stored
  without a source (bug-187, MEDIUM).** `store` accepts an omitted or null
  `source` and records it as the anonymous `{}`; that is documented, supported,
  and the shape the write path normalises to. The `invalid_source_type` check
  counts every row whose `source.type` is absent, which includes that anonymous
  shape, and it carries `warn` severity — so the single `status` verdict lands
  on `degraded` and stays there. `check_health(fix=true)` cannot clear it:
  there is no canonical producer type to rewrite `{}` into without inventing
  attribution, so the fixer correctly leaves those rows alone.
  **The data is fine; the verdict is wrong.** Nothing else is affected —
  recall, store, and the remaining checks behave normally, and no repair runs
  against those rows. On v2.5.2 and earlier, read the `issues` list or
  `severity_summary` rather than `status` alone: an `invalid_source_type`
  finding whose count matches your anonymous-source rows is this, not a real
  defect. Attributed writes (`source` with a `type` of `User` / `Agent` /
  `System`) are unaffected. **Fixed in v2.5.3** — the anonymous shapes are no
  longer counted as type defects, so an unattributed corpus reports `healthy`,
  while a source that claims a producer and gets the contract wrong is still
  reported. No schema change and no data is rewritten; upgrading is enough.
  Details: `qa/issue-registry.json` (bug-187).

- **v2.5.3 and earlier — `check_health` reports `degraded` for legacy `source`
  shapes no repair can clear (bug-205, MEDIUM).** The same failure as bug-187,
  one class wider. `store`'s `source` object declares no required key, so
  `{"id":"discord:1","name":"bob"}` is a conforming write; it is counted by
  `invalid_source_type`, and `normalize_source` refuses to guess a producer type
  for it. Bare strings that are not `user` / `assistant` / `ai` — an agent id
  such as `"claude-code"` — behave the same way. Those rows carry `warn`, `warn`
  decides `status`, and no number of `fix=true` runs converges. On a corpus
  carrying legacy attribution this leaves `status` pinned at `degraded`, which
  is where a *new* warning goes to be ignored. **Fixed in v2.5.4** — severity
  now follows whether a fix run would change anything: rows the mapper can
  rewrite still `warn`, rows only a human migration can settle report `info`
  with `needs_human_review: true` and stay in the `issues` list. Nothing is
  hidden and no data is rewritten. On v2.5.3 and earlier, read `issues` rather
  than `status` alone, as for bug-187. Details: `qa/issue-registry.json`
  (bug-205).

- **v2.4.39 and earlier — vector recall scan window too narrow (bug-085,
  HIGH).** The vector retriever ranked only the newest
  `min(MAX_MEMORIES, max(limit * 10, 100))` rows, so a default recall reached
  only the newest **100** memories (at most 500 under the default
  configuration) — anything older was invisible to semantic search on any
  corpus beyond a few hundred rows. **v2.4.38 and v2.4.39 are the most
  affected**: the response-limit clamp introduced in 2.4.38 (itself a correct
  hardening) also capped wide-scan configurations at 1,000 rows, closing the
  only workaround. Discovered when it collapsed LMEB LongMemEval from ~78 to
  38.68. **Fixed in v2.4.40** — the scan window is decoupled from the response
  limit and the `CPERSONA_MAX_MEMORIES` default is raised 500 → 10,000.
  Upgrading is strongly recommended; no schema change is involved. Details:
  `qa/issue-registry.json` (bug-085).

## Reporting an issue

Bugs and feature requests belong in
[GitHub Issues](https://github.com/Cloto-dev/cpersona/issues). Two templates
are provided — [bug report](https://github.com/Cloto-dev/cpersona/issues/new?template=bug_report.yml)
and [feature request](https://github.com/Cloto-dev/cpersona/issues/new?template=feature_request.yml)
— but a plain issue is fine too; the templates exist to save you from guessing
what is useful, not as a barrier.

A few things that make a report easier to act on, none of them required:

- The cpersona version, and whether an embedding server is configured. Recall
  behaviour differs substantially between the two configurations.
- Whether the problem reproduces against a fresh database. This separates data
  state from code paths faster than anything else.
- The output of `check_health` when the symptom involves recall quality or
  missing memories.

Please check [Known issues](#known-issues) first — a match there means the
diagnosis already exists and may already be fixed in a later release.

**Security vulnerabilities are the exception.** Do not open a public issue for
them; follow [SECURITY.md](SECURITY.md) instead.

*Last updated: 2026-08-25*
