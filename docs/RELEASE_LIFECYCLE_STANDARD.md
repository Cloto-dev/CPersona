# Release Lifecycle Standard (v1.1)

A three-tier release lifecycle and support standard for Cloto-family
projects. This document is the **specification**; a repository adopting it
publishes its own operative `SUPPORT.md` (tier table + status) and
`SECURITY.md` derived from the templates here.

## Status of this document

**Pilot.** This standard is piloted in two repositories with complementary
roles: **cpersona** (reference implementation for policy operation and
quality baseline) and **ClotoCore** (reference implementation for
**structural enforcement** — the tier rules are baked into its
update-channel / release-manifest pipeline rather than applied by registry
convention; see its `docs/RELEASE_PIPELINE_DESIGN.md`). Every rule below is
exercised and validated against real releases before family-wide adoption.
Cloto-family **public** repositories adopt the standard incrementally once
the pilot passes its evaluation criteria (§6). Private repositories are
exempt.

Canonical home: this repository, while the pilot runs. If the standard is
adopted family-wide, the canonical home may move to a family-level
repository, and this document will become a pointer to it.

## 1. Tiers

Every release line (e.g. 2.4.x) is in exactly one tier at any time. The
tier attaches to the **line**, not to an individual version.

| Tier | Meaning | Fix policy |
| --- | --- | --- |
| **Stable** | Certified by the maintainer after production soak. Recommended for all users; default distribution channel (e.g. the marketplace pin) serves this line. | Critical bug fixes, data-loss fixes, and security fixes only, backported at the maintainer's discretion. |
| **Current** | The newest release line. Passed the repository's full release gate but not yet production-certified. | All bug fixes land here first; this is where development happens. |
| **Experimental** | Alpha / beta (and, when needed, rc) pre-releases of the next line. Opt-in only; no guarantees. | Fixes ship in the next pre-release. |

Vocabulary note: **Current** follows the Node.js release-phase vocabulary
(the newest supported line, distinct from the production tier) — not the BSD
`-CURRENT` development head, whose role is played by **Experimental** here.
**Experimental** matches React's release-channel usage (opt-in, no
guarantees). The Stable fix gate matches Node.js Maintenance LTS ("critical
bug fixes and security updates") and the Linux kernel stable rules.

## 2. Lifecycle

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

### 2.1 Pre-releases (Experimental)

- Python projects use PEP 440 canonical version strings (`2.5.0a1`,
  `2.5.0b1`, `2.5.0rc1`); git tags match 1:1 (`v2.5.0a1`). Non-Python
  projects use their ecosystem's pre-release notation (e.g. semver
  `-alpha.1`) with the same stage semantics.
- The installer-level opt-in property MUST hold: a plain install never
  resolves to a pre-release (pip excludes pre-releases without `--pre`;
  other ecosystems use pre-release flags / dist-tags to the same effect).
- The `rc` stage is optional; alpha → beta → final is the default ladder.

### 2.2 Release gate (entry into Current)

Each repository defines its own gate, which MUST at minimum include its full
test suite and lint. In cpersona the gate is: pytest suite (including the
structural gates), ruff, issue-registry verification (`verify-issues.sh`),
and comprehensive multi-agent audits for substantial batches.

### 2.3 Certification (promotion to Stable)

An explicit, event-based maintainer decision — **no fixed clock**.
Guideline: several weeks of production soak with no new critical or
high-severity defects. The certification date is recorded in the repo's
`SUPPORT.md` status table; it also starts the superseded line's grace
window. Each adopting repository names its soak environment (for cpersona:
the production ClotoCore deployment).

### 2.4 Grace window

When a successor line is certified Stable, the superseded line keeps its
Stable fix policy for **30 days from the certification date**, then reaches
EOL.

- The clock anchors on the certification event; patch releases inside the
  window do NOT reset it.
- Fixes for issues accepted within the window may ship after it closes.
- If a transition requires a database schema or data migration (cpersona
  line transitions so far preserve the DB schema and MCP tool contract),
  the maintainer SHOULD extend the window before certifying the successor.

### 2.5 EOL

No further fixes. Post-EOL security fixes are at the maintainer's sole
discretion and must not be relied upon.

### 2.6 Initial state (no certified Stable line)

Before a repository's **first** certification event, no Stable line exists.
In that state:

- The default distribution channel MUST serve the **Current** line (i.e. a
  `stable` channel aliases `current` until first certification).
- Consumer-facing surfaces SHOULD state that no line has been certified
  Stable yet (e.g. a "Stable line not yet certified" note in the
  `SUPPORT.md` status table and, where applicable, in update UI).
- The installer opt-in property (§2.1) still holds: the aliased default
  channel never resolves to a pre-release.
- The first certification replaces the alias with a real pin; from then on
  §2.3–§2.5 apply unchanged.

## 3. Required artifacts (per adopting repository)

1. `SUPPORT.md` — operative policy: tier table, lifecycle summary, and the
   repository's **status table** (line / tier / certification-EOL dates).
2. `SECURITY.md` — supported-versions table referencing `SUPPORT.md`, plus
   a private vulnerability-reporting channel.
3. A short README section pointing at both.

cpersona's `SUPPORT.md` / `SECURITY.md` are the reference templates.

## 4. Distribution mapping

- **Marketplace / hub**: the default pin serves the **Stable** line; the pin
  flips on certification, not on release.
- **PyPI / registries**: `latest` naturally resolves to Current's newest
  final release; Experimental stays behind the pre-release flag.
- **GitHub Releases**: pre-releases carry the "Pre-release" flag; the
  "Latest" badge tracks Current.
- **Update manifest / feed** (repositories that ship their own updater): the
  feed exposes one channel per tier, named after the tiers verbatim; the
  default channel is `stable` and its pin flips on certification (§2.3),
  making §2.1 and the marketplace rule above structural rather than
  conventional. Reference implementation: ClotoCore's release pipeline.

## 5. Adoption checklist (for a new repository)

- [ ] Copy `SUPPORT.md` / `SECURITY.md` from the templates; fill the status
      table with the repo's current lines.
- [ ] Define the repo's release gate (§2.2) and soak environment (§2.3).
- [ ] Verify the installer opt-in property for pre-releases (§2.1).
- [ ] Point the default distribution channel at the Stable line (§4).
- [ ] Add the README pointer section.
- [ ] Record adoption in this document's §7 registry.

## 6. Pilot evaluation criteria

The pilot is considered successful — unlocking family-wide adoption — when:

1. One full lifecycle cycle completes in cpersona (2.5.x: Experimental →
   Current → Stable certification; 2.4.x: Grace → EOL) **without the policy
   forcing an ad-hoc decision it cannot express** (any such gap is a
   standard defect: fix the standard, bump its version).
2. The mechanical hooks behave as specified: pip pre-release exclusion,
   hub pin flip on certification, status-table bookkeeping.
3. No consumer-facing confusion incident attributable to the tier
   vocabulary or the grace-window semantics.

Failures do not abort the pilot; they iterate the standard (v1.x) until a
clean cycle passes.

## 7. Adoption registry

| Repository | Standard version | Adopted | Notes |
| --- | --- | --- | --- |
| cpersona | v1.0 | 2026-07-09 | Pilot / reference implementation (policy operation). |
| ClotoCore | v1.1 | 2026-07-12 | Second pilot / reference implementation (structural enforcement via update-channel + signed-manifest pipeline, `docs/RELEASE_PIPELINE_DESIGN.md`). |

## 8. Changelog

- **v1.1 (2026-07-12)** — Initial-state rule for repositories with no
  certified Stable line (§2.6, surfaced by the ClotoCore adoption — a §6
  "standard defect" fixed per its own procedure); update-manifest/feed row
  in the distribution mapping (§4); ClotoCore registered as second pilot
  (structural-enforcement reference).
- **v1.0 (2026-07-09)** — Initial standard, extracted from the cpersona
  policy discussion; vocabulary and rules benchmarked against OSS
  conventions (Node.js release phases, React release channels, Debian
  oldstable / Firefox ESR grace precedents, kernel stable rules, PEP 440).
