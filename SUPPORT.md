# Support Policy

This document defines the release lifecycle and support policy for cpersona.
It is written to be line-agnostic: the same rules apply to every release line
(2.4.x, 2.5.x, ...), so the policy survives line transitions unchanged.

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
- Line transitions so far preserve the database schema and the MCP tool
  contract, which makes rollback and roll-forward free. If a future
  transition ever requires a schema or data migration, the maintainer SHOULD
  extend the grace window before certifying the successor.

### EOL

No further fixes. Security fixes after EOL are at the maintainer's sole
discretion and must not be relied upon.

## Status

| Line | Tier | Notes |
| --- | --- | --- |
| 2.4.x | **Stable** | Certified Stable; the marketplace serves this line by default. Enters Grace 30 days after 2.5.x is certified Stable. |
| 2.5.x | **Current** | Latest release: 2.5.0 (final). Passed the full release gate (test suite, lint, issue-registry verification, audits); all fixes land here. Awaiting production-soak certification to Stable. |

Certification and EOL dates are recorded in this table as they occur.

## Known issues

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

*Last updated: 2026-07-17*
