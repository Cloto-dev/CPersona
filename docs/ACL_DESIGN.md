# Per-Client Capability / ACL Design

**Status**: APPROVED — maintainer ruling 2026-08-22 resolved every §9 decision
point as proposed (D4 in its amended read-write form). This document is the
implementation contract; deviations found during implementation go back to §9.
**Scope**: server-side hard enforcement of per-client, per-agent read/write
capability. OAuth-based identity is a separate line that plugs into the seam
defined here (§3.1) and is deliberately out of scope.

---

## 1. Problem

Authentication today is a single `CPERSONA_AUTH_TOKEN`: every client that holds
it can call every tool against every `agent_id`, because `agent_id` is an
ordinary tool argument the caller picks freely. There is no way to wire a
second client as "read-only, and only for these agents" — that intent can only
be expressed as prose in the client's own instructions, which is honor-based
and unenforced. The server must be able to say no.

Target model (example): `client_A = {alpha: read-write, beta: read}`,
`client_B = {beta: read-write}` — enforced at the server, regardless of what
any client sends.

## 2. Trust model

**Defends against**: an over-privileged or misbehaving *authenticated* client —
wrong `agent_id` by bug or prompt injection, writes from a client wired with
read-only intent, blast-radius confinement per client.

**Does not defend against**: network-layer exposure (unchanged: bind guard +
bearer auth), a leaked token of a fully-granted client, or a hostile local
process on the stdio transport (§5.4). Identity *proof* beyond shared secrets
is the OAuth line's job, not this one's.

## 3. Core model

```
request ──► IdentityResolver ──► client_id ──► ACL[(client_id, agent_scope)] ──► allow / deny
```

- **Principal** (`client_id`): an opaque string naming a client. Resolved from
  the request by an identity resolver; the rest of the system never sees
  credentials, only the `client_id`.
- **Grant**: `(client_id, agent_pattern) → none | read | read-write`, where
  `agent_pattern` is an exact `agent_id` or the wildcard `"*"`.
- **Permission lattice**: `none < read < read-write`. `read-write ⊃ read`.
- **Effective permission** for `(client, agent)` = the exact-match grant if one
  exists, else the client's `"*"` grant, else `none`. Exact beats wildcard even
  when it grants *less* — the operator who wrote `{"*": "read", "noisy": "none"}`
  meant the exception.

### 3.1 The identity seam (the load-bearing abstraction)

Enforcement consumes only `client_id`. How a `client_id` is established is a
pluggable resolver behind a narrow interface:

```python
class IdentityResolver(Protocol):
    def resolve(self, request) -> Principal | None:
        """Return the authenticated principal, or None for 401."""
```

- **v1 resolver — named static tokens** (§4): bearer token → `client_id` map.
- **Future resolver — OAuth**: token introspection / JWT validation resolving
  to the *same* `client_id` namespace. Drop-in: the grant table, the
  enforcement layer, and every test below it are untouched.

**Hard constraint**: nothing outside the resolver may assume identity came
from a static token. No bearer-specific fields on `Principal`, no reads of
`Authorization` headers past the resolver. This constraint is what makes the
OAuth line additive instead of a rework.

## 4. Configuration

Authoritative store = **a config file**, not the database. Rationale: the DB
has a bootstrap problem (who inserts the first admin credential into a store
you need credentials to reach), the grant set is small and operator-authored,
and file + restart matches how every other CPersona knob works. The DB schema
also stays untouched (a 2.5.x line invariant).

```jsonc
// CPERSONA_ACL_FILE=/path/to/acl.json
{
  "clients": [
    {
      "client_id": "assistant-a",
      "token": "${CPERSONA_TOKEN_ASSISTANT_A}",   // literal or ${ENV} reference
      "grants": { "alpha": "read-write", "*": "read" }
    },
    {
      "client_id": "importer",
      "token": "s3cr3t-literal-also-allowed",
      "grants": { "beta": "read-write" }
    }
  ]
}
```

- `token` accepts a literal or a `${ENV_VAR}` reference (resolved at load; an
  unset variable is a startup error — fail closed, §7).
- Duplicate `client_id` or duplicate resolved token: startup error. Token
  lookup must stay unambiguous.
- Permissions are exactly `"none" | "read" | "read-write"`. Unknown strings:
  startup error.
- File permissions: the loader warns when the file is group/world-readable
  (same posture as the DB file).

### 4.1 Two-stage default (backward compatibility)

| State | Behavior |
| --- | --- |
| `CPERSONA_ACL_FILE` unset (default) | **Legacy mode, byte-for-byte today's behavior**: `CPERSONA_AUTH_TOKEN` single token (or unauthenticated + reachability warning), full capability for every caller. ACL code contributes zero decisions. |
| `CPERSONA_ACL_FILE` set | **ACL mode**: named tokens only; every call is resolved and checked; anything not explicitly granted is denied. |

ACL mode is opt-in and the default behavior is unchanged — the release can ship
additive. In ACL mode, `CPERSONA_AUTH_TOKEN` is **ignored with a startup
warning** if also set (proposed; alternative in §9-D3): one authority for
credentials at a time, and a forgotten legacy token must not survive as a
hidden full-capability backdoor.

## 5. Enforcement

Two layers, both server-side.

### 5.1 Layer 1 — transport authentication (extends `BearerTokenMiddleware`)

Today the middleware compares one token (`hmac.compare_digest`) and forwards.
Change: in ACL mode it resolves the token against the client table —
constant-time comparison against **every** entry (no early exit, no
dict-by-token lookup; the table is small) — and stashes the resolved
`Principal` in a `contextvars.ContextVar` for the dispatch layer. Unknown or
missing token: `401` exactly as today.

Propagation note: the HTTP transport runs
`StreamableHTTPSessionManager(stateless=True)`, so a tool call executes within
the ASGI request's task lineage and the context variable set by the middleware
is visible at dispatch. This is an implementation-detail dependency and gets
its own wiring test (§8) so a future transport change (sessionful mode, task
pools) fails red instead of silently resolving every call to "no principal".

### 5.2 Layer 2 — capability check at tool dispatch

The vendored `ToolRegistry.call_tool` seam stays untouched — it is shared
across servers, and a cross-consumer change is a different (and much more
expensive) release class. Instead, cpersona wraps its own handlers at
registration time in `server.py`: after the 29 `auto_tool` registrations, a
single pass replaces each handler with

```
guard(tool_name, handler):  arguments → resolve scope → check → handler | denial
```

The check reads `(principal, tool_classification[tool_name], agent_scope(arguments))`
and compares against the grant table. Fail-closed default: a tool present in
the registry but missing from the classification table is **denied** in ACL
mode with an explicit "unclassified tool" error — a new tool cannot ship
enforcement-invisible (§8 pins this with an exhaustiveness test).

### 5.3 Error contract

- Transport failures stay HTTP: `401` (bad/missing token) as today.
- Capability denials are **tool errors** (the MCP call succeeds transport-wise
  and returns a structured refusal), because the decision needs tool arguments
  and happens inside dispatch:

```json
{ "ok": false, "error": "permission_denied",
  "tool": "store", "agent_id": "beta", "required": "read-write",
  "client_id": "assistant-a" }
```

`client_id` echoes the caller's own resolved identity (it is not a secret to
itself) so a mis-wired client is diagnosable from its side of the wire. Denials
are logged server-side at WARNING with the same fields (observability; also the
audit seed if a real audit log is ever wanted — deliberately not built now).

### 5.4 stdio transport

The stdio transport has no credential to resolve — the peer is whoever spawned
the process. Proposed: in ACL mode, stdio resolves to the reserved principal
`"local"`; grants for it come from the same file (`"client_id": "local"`, no
`token` field). If `"local"` is absent, stdio calls are denied like any
ungranted principal — an operator who turns ACL mode on states every principal
explicitly, the local one included. Legacy mode: stdio is unrestricted, as
today.

## 6. Tool classification and agent-scope resolution

Classification is an explicit table in cpersona code (`ACL_CLASSIFICATION`),
not derived at runtime from `ToolAnnotations`. Annotations are client-facing
hints; the ACL table is a server-side security decision, and the two are kept
honest against each other by a test (§8) rather than by sharing a source —
where they disagree, the test forces the disagreement to be examined instead
of silently inheriting a hint written for a different purpose.

Survey result (all 29 tools, from `server.py` registrations): two tools carry
**no** `ToolAnnotations` today — `calibrate_threshold` and
`set_recall_precision` (both mutate calibration state; the gap is fixed
alongside the implementation, independent of which way §9-D6 resolves).

| Tool | Capability | Agent scope |
| --- | --- | --- |
| `recall`, `recall_with_context`, `get_contents`, `get_profile`, `list_memories`, `list_episodes`, `get_recall_precision` | read | `agent_id` argument |
| `store`, `update_profile`, `archive_episode`, `update_memory`, `lock_memory`, `unlock_memory`, `delete_memory`, `delete_episode`, `delete_agent_data` | read-write | `agent_id` argument |
| `calibrate_threshold`, `set_recall_precision` | read-write | `agent_id` argument (mutate per-agent calibration state) |
| `check_health`, `deep_check` | read; **read-write when `fix=true`** | `agent_id` argument; empty = every agent → requires the grant on `"*"` |
| `migrate_channel_axis` | read-write | `agent_id` argument; empty = every agent → `"*"` |
| `export_memories` | read-write; **while `CPERSONA_EXPORT_DIR` is unset (the shipped default) the demand escalates to `"*"`** — the path argument is caller-chosen anywhere on the filesystem, so the blast radius is not one agent's data (§9-D4, second amendment from the pre-merge review) | `agent_id` argument |
| `import_memories` | read-write; same `"*"` escalation while `CPERSONA_EXPORT_DIR` is unset | `target_agent_id`; empty = "as recorded in file" → `"*"` |
| `merge_memories` | `copy`: read(source) + read-write(target). `move`: read-write on **both** (move deletes source rows) | `source_agent_id` + `target_agent_id` |
| `pause_persistence`, `resume_persistence` | read-write on `"*"` (process-wide switch affects every agent's writes) | global |
| `persistence_status`, `get_queue_status`, `get_operating_context` | unscoped read: allowed for **any authenticated principal** (no per-agent data; §9-D5) | none |

Resolution rules the table relies on:

- A call whose scope resolves to `"*"` (empty `agent_id` on an all-agents tool)
  is a **sweep**: it touches every agent, the excepted ones included. It is
  satisfied only at the level every grant row allows — the minimum over the
  wildcard grant and every named exception. Holding `read` on three named
  agents does not add up to `read` on `"*"`, and `{"*": "read-write",
  "prod": "none"}` cannot reach `prod` through an all-agents call: the
  operator meant the exception (D6 applied to sweeps; pre-merge review
  refinement — the first cut consulted only the wildcard grant, which let a
  sweep do what the named `"none"` forbade).
- An agent-scope argument that is not a non-empty string (absent, empty, or a
  non-string type) resolves to the wildcard demand — the broadest requirement.
  The guard runs outside the per-tool parameter validation, so it must not
  assume validated shapes (pre-merge review refinement).
- The file-I/O escalation tests only whether `CPERSONA_EXPORT_DIR` is set, not
  how much it contains. **Point it at a service-owned directory**: a broad
  root (`/home`, `/var`) restores the agent-scoped demand while still letting
  a caller choose almost any path inside it, silently re-opening what the
  escalation closed.
- Conditional capability (`fix=true`) is resolved from arguments **before**
  the handler runs; the guard sees the same validated arguments the handler
  would.

## 7. Failure posture

Fail closed, loudly, at the earliest seam:

- Malformed ACL file, unknown permission string, duplicate token/client,
  unresolvable `${ENV}`: **startup error** — the server refuses to serve
  rather than serving with a policy other than the one written.
- Unresolvable principal at dispatch in ACL mode (contextvar empty — a wiring
  regression): deny + ERROR log, never "no principal, no restriction".
- Tool missing from the classification table in ACL mode: deny (§5.2).

## 8. Test strategy

1. **Resolver unit**: token→principal over the table, constant-time path,
   unknown token → `None`, `${ENV}` resolution and its failure.
2. **Wiring** (extends the existing `test_253_middleware_wiring.py` pattern —
   real ASGI requests through the assembled app): 401 unknown token; a granted
   call passing; the *same* call under a lesser grant returning the §5.3 denial
   — pinning middleware → contextvar → guard end-to-end, which is also the
   `stateless=True` propagation proof.
3. **Classification exhaustiveness**: every name in `registry._handlers` is in
   `ACL_CLASSIFICATION`; a registered-but-unclassified tool fails the suite
   (and is denied at runtime — both halves of fail-closed).
4. **Annotations cross-check**: `readOnlyHint=True` ⇔ classification `read`,
   with a reviewed explicit exception list (expected: `check_health` /
   `deep_check`, read-tools with a write mode).
5. **Scope resolution**: merge move/copy grant matrices; empty-`agent_id` →
   `"*"`; `fix=true` escalation.
6. **Legacy-mode equivalence**: with `CPERSONA_ACL_FILE` unset, the guard
   contributes zero decisions — behavior-identical suite run.
7. **stdio principal**: `"local"` granted / absent-denied per §5.4.
8. **Mutation checks** (release-gate discipline): drop the guard wrap → red;
   invert one grant → red; remove one classification row → red.

## 9. Decision points for the maintainer

Proposed defaults are what §§3–7 specify; each is cheap to change pre-implementation.

| # | Decision | Proposed | Alternative |
| --- | --- | --- | --- |
| D1 | Grant store | Config file (§4) | DB table (bootstrap problem; schema change is off the table for this line anyway) |
| D2 | Default policy | Two-stage opt-in (§4.1) | ACL always on with an implicit full-grant default client (more moving parts in legacy deployments for zero gained enforcement) |
| D3 | `CPERSONA_AUTH_TOKEN` in ACL mode | Ignored + startup warning | Auto-map to a built-in full-capability client (keeps one hidden super-token alive — the failure mode this feature exists to remove) |
| D4 | `export_memories` | read-write. The original "read" rationale ("the file write is operator-configured, not caller-chosen") does not survive contact with `_confine_io_path`: with `CPERSONA_EXPORT_DIR` **unset — the shipped default — the caller picks any `..`-free absolute path** (bug-054 lineage), and the existing annotation already declares `destructiveHint=True` | read, valid only when `CPERSONA_EXPORT_DIR` confinement is actually configured — a later, condition-gated relaxation if read-only backup clients turn out to be a real need |
| D5 | Unscoped reads (`persistence_status` etc.) | Any authenticated principal | Require at least one non-`none` grant (denies a fully-revoked-but-still-listed client even status visibility) |
| D6 | Wildcard semantics | Exact-match overrides wildcard, both directions (§3) | Wildcard as floor (`max(exact, "*")`) — simpler but cannot express "read everywhere except none on X" |
| D7 | Ship shape | One alpha rung (auth-surface change; soak the resolver + guard under real traffic) | Direct-to-final (formally allowed: additive, default-off) |

## 10. Non-goals

- **OAuth / identity proof** — separate line, consumes §3.1.
- **project_id / channel granularity** — no consumer today; the grant value is
  a closed enum, so a later `{perm, projects: [...]}` extension is additive.
- **Rate limiting, quotas, audit persistence** — out of scope; §5.3 logging is
  the seed, not the feature.
- **Multi-tenancy beyond agent_id** — the agent axis is the isolation model.
