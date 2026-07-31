# SuperAuditor Standard (v1)

A delivery contract for **findings**: the drift, staleness and integrity
observations a server already computes about its own stored state. The
standard specifies how a server *reports* findings — the seam — and
deliberately says nothing about what a server chooses to detect.

The name encodes the job description: an auditor inspects and reports; it
does not repair, and it does not decide what the operator should do next.

## Status of this document

**v1, extracted from a running implementation.** The pull contract,
severity vocabulary and cap semantics below are the ones shipped in
CScheduler 0.9.0 (2026-07-31) and measured in production before this
document was written. Nothing here is speculative design; where the pilot's
design notes and the shipped code disagreed, the code won.

CPersona is the expected second implementation. Until a second
implementation exists, treat unusual-looking requirements as evidence from
one system rather than as generalized wisdom.

**No shared library.** Implementations MUST NOT be required to link a
common runtime. Consistency is carried by this document plus the
conformance fixtures in `conformance/superauditor/v1/`, so an
implementation in another language is never forced to port another
language's bugs. This mirrors the spec-first approach used for MGP.

Key words MUST, MUST NOT, SHOULD, MAY are used per RFC 2119.

## 1. Motivation

A server that computes findings has to decide when to hand them over. The
cheap default is to attach them to every read — and that is what
CScheduler did for 13 read tools. Measured on real transcripts before the
change (2026-07-31, 97 calls to the carrier tools):

- the findings block was a median of **4,578 characters ≈ 2,773 tokens**
  per response, **43.9%** of the payload it rode on, and up to **99.1%** of
  a small one;
- the only consumer read it **once**, at the end of a session;
- because the block is pushed, unresolved state converts directly into a
  per-call fixed cost: findings nobody acts on are re-billed on every read.

Push delivery also makes findings *ambient*. They arrive unrequested, in
the middle of unrelated work — precisely when they are least actionable.

The fix is not to detect less. It is to separate **detection** (unchanged)
from **delivery** (pull, on demand).

## 2. Scope and non-goals

In scope — the seam:

- the shape of a finding;
- the `severity` vocabulary and how severity is assigned;
- the pull tool, its parameters and its response;
- honest reporting of truncation;
- how findings relate to a caller's isolation filters and session identity;
- how an existing broadcast is retired without breaking its consumers.

Out of scope — named here because scope creep is the specific failure this
document exists to prevent:

- **No execution layer.** Acting on a finding takes judgment; the standard
  delivers, the operator decides. A detector that also acts is a policy
  engine wearing a data layer's clothes.
- **No auto-fix.** Repair tools (e.g. a `check_health(fix=true)`) keep
  their repairs. A SuperAuditor implementation MUST NOT mutate state.
- **No detection catalogue.** What counts as a finding is each server's
  business. CScheduler detects semantic drift in a plan graph; CPersona is
  expected to report storage integrity. Same contract, different contents.
- **No confidence scores.** See §4.
- **No probe-accuracy requirements.** Improving a detector is orthogonal
  work and MUST NOT be smuggled in through this contract.

## 3. The finding object

A finding is a JSON object. Two keys are defined by this standard:

| key | type | requirement |
| --- | --- | --- |
| `kind` | string | MUST. A stable, server-defined identifier for the probe that produced it (e.g. `stale_pending`). |
| `severity` | string | MUST. One of the values in §4. |

All other keys are the payload and are server-defined: the identifiers,
counts, ages or titles a consumer needs in order to act. Consumers MUST
tolerate unknown payload keys.

`kind` vocabulary is **not** standardized. Two implementations sharing a
kind name SHOULD mean the same thing by it, but the standard does not
enumerate kinds and does not reserve names.

## 4. Severity

Exactly three values, ordered:

| severity | meaning |
| --- | --- |
| `critical` | The read contract is broken right now — data a caller has already been given cannot be trusted. |
| `warn` | Two stored facts contradict each other; something is wrong now. |
| `info` | An observation or suggestion; whether to act is a judgment call. |

Assignment rules:

1. **Severity MUST be a property of the `kind`, not of the instance.**
   Implementations MUST assign severity from a static per-kind map. No
   model judgment, no per-finding scoring, no confidence values. A consumer
   must be able to route on severity without re-deriving it.
2. **The map MUST be exhaustive over the probe registry**, enforced by a
   test that fails when a probe kind has no entry. An implementation MAY
   also carry a runtime fallback, and if it does, the fallback MUST be the
   weakest severity (`info`) — an unmapped probe must not be able to
   manufacture an alarm.
3. **A probe whose premise is a lexical match MUST NOT be `warn` or
   `critical`.** Keyword matching over free text produces plausible
   findings that are wrong; in the CScheduler pilot such a probe measured
   0/5 precision against full reads of the flagged records. A finding whose
   evidence is a string match does not get to claim a defect.

Implementations MAY leave a severity unused. CScheduler emits no
`critical`: drift in a plan graph never falsifies a read.

## 5. Pull delivery

### 5.1 The tool

```
get_session_findings(session_key?, per_kind_limit?, include_summary?) -> object
```

The tool MUST be read-only and MUST be safe to call at any time. It is the
consumer's decision when findings are worth paying for; the server MUST NOT
second-guess it by rate-limiting or caching stale results.

| parameter | default | meaning |
| --- | --- | --- |
| `session_key` | absent | Opaque, client-declared session identity (§7). |
| `per_kind_limit` | `5` | Maximum findings returned **per kind**. |
| `include_summary` | `true` | Include the human-readable `summary` rendering. |

`include_summary=false` exists because the prose restates `findings`; a
machine consumer MUST be able to decline paying for it.

### 5.2 The response

```json
{
  "findings":            [ { "kind": "stale_pending", "severity": "info", "task_id": 6, "days_stale": 62 } ],
  "total":               34,
  "counts_by_kind":      { "stale_pending": 20, "active_goal_claims_achievement": 13, "duplicate_pending": 1 },
  "counts_by_severity":  { "info": 33, "warn": 1 },
  "capped_kinds":        ["stale_pending"],
  "per_kind_limit":      20,
  "summary":             "Drift / maintenance findings: …",
  "identity_shared":     true,
  "_meta":               { "server_version": "0.9.0" }
}
```

| key | requirement |
| --- | --- |
| `findings` | MUST. The trimmed set, after `per_kind_limit` is applied. |
| `total` | MUST. The number of findings **returned**. It is NOT the true number that exist — truncation is reported by `capped_kinds`, not by this field. |
| `counts_by_kind` / `counts_by_severity` | MUST. Computed over the returned set, so they always agree with `findings`. |
| `capped_kinds` | MUST. See §6. Present and empty when nothing was capped. |
| `per_kind_limit` | MUST. Echoes the limit actually applied, so a consumer reading a stored response can interpret `capped_kinds` without knowing the request. |
| `summary` | MUST be present when `include_summary` is true, and MUST be rendered from the same trimmed set as `findings` — prose and structure never disagree. |
| `identity_shared` | MUST be present and `true` when §7 applies; SHOULD be omitted otherwise. |
| `_meta.server_version` | MUST. The running instance identifies itself. |

The response MUST NOT carry the broadcast block (§8): this tool *is* the
findings channel, and attaching the push payload would bill it twice.

### 5.3 One detector

When an implementation delivers findings through more than one channel
(e.g. during a broadcast migration), all channels MUST be fed by a single
detection implementation. A probe MUST NOT be able to mean one thing when
pushed and another when pulled.

## 6. Honest caps

`per_kind_limit` truncates, and a naive implementation truncates
*silently*: a kind sitting at exactly the limit is indistinguishable from a
kind with two hundred rows. That is a lie a consumer cannot detect.

- Implementations MUST report, in `capped_kinds`, every kind that had more
  findings available than were returned.
- Implementations MUST determine this by observation, not by inference —
  the reference technique is to probe at `per_kind_limit + 1` and trim the
  extra row back before returning. Concluding "capped" from
  `count == limit` is NOT conforming, because it reports a kind that
  happens to have exactly `limit` findings as truncated.
- Silent truncation anywhere in the response is forbidden.
- Trimming MUST preserve the detector's ordering: the findings kept for a
  kind are its first `per_kind_limit`. Reordering (by severity, age, or
  anything else) MAY happen before trimming, but the pair
  (detector output, `per_kind_limit`) MUST determine the returned set — the
  fixtures in §9 depend on it.

The pilot measured why this matters: the broadcast, capped at 5 per kind,
showed 11 findings where the pull with a higher limit returned 34.

## 7. Session identity and isolation

**Session identity.** `session_key` is an opaque, client-declared label — a
partition hint, not authentication. Implementations that carry
session-scoped probes (e.g. "records this session touched and left
pending") MUST scope them by the declared key.

Where a deployment cannot distinguish sessions — a shared remote transport
with no key declared — the implementation MUST say so with
`identity_shared: true` rather than guessing. Degrading honestly is
required; degrading silently is not conforming.

**Isolation filters.** Findings MUST NOT be filtered by the caller's
isolation axes (project, agent, tenant, or equivalent). The purpose of the
channel is to surface forgotten state; slicing it by the bucket the caller
happens to be reading would hide exactly the records that were forgotten.
Implementations MUST document this, because it is the opposite of what
every other read in such a server does.

## 8. Coexisting with an existing broadcast

An implementation that already pushes findings onto unrelated responses
MUST NOT be required to break its consumers to conform. The migration
contract:

1. Ship the pull tool first. It is purely additive.
2. Gate the broadcast behind a runtime knob with at least the values
   `all` (existing behavior) and `off` (no push). An intermediate value
   that keeps the push on a single designated response — CScheduler uses
   `context`, its session-start read — is RECOMMENDED, because it lets the
   remaining consumer keep working while every other response is freed.
3. **The default at introduction MUST preserve existing behavior
   byte-for-byte**, proven by test. Operators opt in.
4. An unrecognized knob value MUST fall back to the existing behavior and
   log a warning. A typo in a deployment environment MUST NOT silently
   blind an audit.
5. The broadcast payload SHOULD be left frozen — in particular, an
   implementation SHOULD NOT add `severity` to it. Changing the bytes of
   existing responses for a consumer that does not read the new key
   forfeits the byte-identical default for nothing. The asymmetry between
   the two channels resolves when the broadcast is switched off, not by
   editing it now.

## 9. Conformance

An implementation conforms when it satisfies every MUST above and
demonstrates the following with tests. C1–C5 are verifiable against the
shared fixtures in `conformance/superauditor/v1/`, which are pure functions
of (input findings, `per_kind_limit`) and therefore language-independent.

| id | requirement |
| --- | --- |
| C1 | Trimming: no kind exceeds `per_kind_limit` in `findings`. |
| C2 | `capped_kinds` names every kind that had more available, and only those — including the boundary case of exactly `per_kind_limit`. |
| C3 | `total`, `counts_by_kind` and `counts_by_severity` are computed over the returned set and agree with `findings`. |
| C4 | Severity comes from the static map; the same `kind` always yields the same `severity`. |
| C5 | An unmapped `kind` resolves to `info` (if a fallback exists at all). |
| C6 | The severity map is exhaustive over the probe registry — a new probe with no entry fails a test rather than defaulting silently. |
| C7 | Findings are not filtered by the caller's isolation axes. |
| C8 | Under a shared transport with no declared `session_key`, the response carries `identity_shared: true`. |
| C9 | With a broadcast present: the default knob value reproduces pre-change responses byte-for-byte, and each other value drops the push from exactly the responses it claims. |

An implementation that has no broadcast is exempt from C9.

## 10. Versioning

This document is versioned independently of any implementation. Additive
clarifications increment the minor version; a change that invalidates a
conforming implementation increments the major version and MUST be
accompanied by a migration note. The fixture directory is versioned with
the major version (`conformance/superauditor/v1/`).

Canonical home: this repository, while the standard has fewer than two
implementations. If it is adopted more widely, the canonical home may move
and this document becomes a pointer.
