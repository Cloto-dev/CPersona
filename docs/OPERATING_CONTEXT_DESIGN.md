# Server-Served Operating Context (Global Config + MCP Instructions Distribution)

**Status**: design draft (target: 2.5.1, direct release — no pre-release ladder)
**Decision**: project owner, 2026-07-16 (design discussion immediately after the 2.5.0b1
release; CSC Goal #171 / Task #246)
**Scope**: additive — no DB schema change, one new read tool, one sidecar config file.
The 2.5.x line declaration in `SUPPORT.md` ("DB schema and MCP tool contract are
preserved, rollback-free") is maintained.

---

## 1. Motivation

CPersona's operating doctrine — which `project_id` values exist, what `agent_id`
conventions apply, how `recall` should be used (limits, `exclude_contents`, session-start
discipline) — currently lives in **client-side documents**: the operator's `CLAUDE.md`
files, per-repo instructions, and skill texts. This has two structural problems:

1. **Divergence at the source.** Every agent/environment pair (Claude Code, ClotoCore
   kernel agents, claude.ai remote connector, future clients) carries its own copy of the
   doctrine, and the copies drift. A new environment silently starts with *no* doctrine.
2. **Operator-side sprawl.** The doctrine competes for space in `CLAUDE.md`, which is
   already under a slimming discipline (token fixed-cost per session).

The server is the single component every client, by definition, connects to. MCP provides
a purpose-built distribution channel: the `instructions` field of the `initialize`
response. This design makes CPersona serve its own operating context — **deterministic
distribution, explicitly distinguished from probabilistic compliance**.

This generalizes the "shaping/limiting belongs to the boundary layer" principle
(CSC Task #190): rules that can be *validated* are enforced server-side (Hard layer);
rules that can only be *stated* are distributed server-side (Soft layer).

## 2. Two-layer architecture

| Layer | What | Mechanism | Guarantee |
| --- | --- | --- | --- |
| **Soft** | Behavioral doctrine (recall discipline, agent_id conventions, session habits) | `initialize` → `instructions` injection + `get_operating_context` detail tool | Deterministic **distribution**; compliance stays probabilistic |
| **Hard** | Machine-checkable rules (valid `project_id` set, `@auto` default resolution) | Server-side validation on tool calls | Deterministic **enforcement** (mode-gated) |

## 3. Sidecar configuration file (no DB schema)

Precedent: CScheduler's `~/.cscheduler/bindings.json`. The operating context is a
**file owned by the operator**, not DB rows — this is the first-choice design because it
keeps the 2.5.x rollback-free declaration intact and makes governance trivial (§7).

- **Path**: `~/.cpersona/operating-context.toml`, overridable via
  `CPERSONA_OPERATING_CONTEXT_PATH`. Kill switch: `CPERSONA_OPERATING_CONTEXT=off`.
- **Format**: TOML, parsed with stdlib `tomllib` (Python ≥3.11, zero new dependencies).
  TOML is chosen over JSON for multiline doctrine text blocks.
- **Absent file → feature fully dormant.** No instructions, no validation, no behavior
  change whatsoever. Existing deployments are untouched.
- **Invalid file → non-fatal.** The server boots with the feature off, logs a warning,
  and `check_health` reports a finding (§8). A config typo must never take memory down.
- **Reload**: lazy, mtime-based. The Hard layer and `get_operating_context` pick up
  operator edits live; the `instructions` text is naturally frozen per connection
  (MCP sends it only at `initialize`), so clients see updates on reconnect.

### 3.1 Schema

```toml
# ~/.cpersona/operating-context.toml
version = 1                       # file-format contract version
context_revision = "2026-07-16.1" # operator-owned label, echoed in all surfaces

[instructions]
# Compact canonical, injected verbatim via MCP initialize (§4). Keep small.
summary = """
CPersona operating context (rev 2026-07-16.1).
agent_id: 'claude-code' for Claude Code sessions, 'agent.<name>' for kernel agents.
project_id registry: "" (global), "cycia-mc". Pass "@auto" to resolve your default.
recall: limit<=5 outside session-start; use exclude_contents for known content.
Details: call get_operating_context.
"""

[registry]
project_ids = ["", "cycia-mc"]    # the valid project_id set
enforce = "warn"                  # "off" | "warn" | "reject"

[defaults]
# agent_id -> project_id; used ONLY to resolve the "@auto" sentinel (§5.2)
"claude-code" = ""

[[doctrine]]
name = "recall-discipline"
body = """
...full doctrine text served on demand by get_operating_context...
"""

[[doctrine]]
name = "agent-id-conventions"
body = """..."""
```

## 4. Soft layer: `initialize` instructions

The official MCP Python SDK already carries the field end-to-end:
`Server(name, instructions=...)` → `create_initialization_options()` →
`InitializeResult.instructions` (verified in the vendored SDK,
`mcp/server/lowlevel/server.py:142,188`). The only cpersona change is threading it
through the vendored `ToolRegistry.__init__` (currently `Server(server_name)` with no
instructions, `_vendored_mcp_common/mcp_utils.py:73`).

Composition rule: `instructions = [instructions.summary]` verbatim, prefixed with
nothing. **The summary is the compact canonical; details are opt-in** via
`get_operating_context` (preview-tier structure, same token fixed-cost discipline as
CSC `get_active_context` / recall preview tiers).

Size discipline: the summary SHOULD stay ≤ 1,500 characters; `check_health` warns above
3,000 (§8). The instructions text is a per-session fixed cost on every connected client —
treat it like CLAUDE.md budget, not like a doc.

### 4.1 Client propagation matrix (measured 2026-07-16)

| Client | `instructions` handling | Status |
| --- | --- | --- |
| Claude Code (stdio + remote MCP) | Injects into system context as "MCP Server Instructions" | **Confirmed working** (observed live with another server's instructions in a real session) |
| ClotoCore kernel | **Dropped.** `initialize()` extracts only `capabilities.mgp` and `capabilities.logging`; the result is logged and discarded (`crates/core/src/managers/mcp_client.rs`, `initialize()`) | **Gap confirmed** — kernel-side work item, tracked in ClotoCore (out of scope here). Until it lands, kernel agents get the Hard layer only |
| claude.ai remote connector | Untested | **To measure** during implementation; if the connector strips instructions, Claude Code local stdio still covers the primary environment |

The kernel gap does not block 2.5.1: the Hard layer (§5) enforces the machine-checkable
subset for kernel agents regardless, and the Soft layer degrades to exactly today's
status quo.

## 5. Hard layer: registry validation + `@auto` sentinel

Both mechanisms are **strictly additive** to the existing project_id semantics.
The invariant table:

| Caller passes | Behavior (unchanged unless noted) |
| --- | --- |
| omitted / `None` | Reads: no filter. Writes: `""` global pool. **Unchanged.** |
| `""` | Global pool / global-only filter. **Unchanged, always valid.** |
| explicit `"X"` | **New**: validated against `registry.project_ids` per `enforce` mode. Value itself is never rewritten — explicit args are never overridden |
| `"@auto"` | **New**: opt-in sentinel, resolved via `[defaults]` by the call's `agent_id` |

### 5.1 Registry validation

Applied on tools that accept `project_id` (write: `store`, `archive_episode`, `update_memory`;
read: `recall`, `recall_with_context`, `list_*`). Modes:

- `off` — no checks (registry is documentation only).
- `warn` (default) — unknown id is accepted; the response carries
  `operating_context_warning: "project_id 'X' not in registry (rev ...)"`. Advisory-first,
  same philosophy as degraded-advisory: report, don't break.
- `reject` — unknown id on **writes** returns an error naming the registry and revision.
  Reads still warn rather than reject (a bad read filter loses nothing; a bad write
  pollutes a bucket — the asymmetry mirrors the actual damage).

Rationale for `warn` default: the registry file is new; a stale registry must not brick
writes. Operators who want the fence escalate to `reject` deliberately.

### 5.2 `@auto` sentinel

- Resolution: `defaults[agent_id]` → that project_id. No mapping → resolves to `""` and
  the response carries an `operating_context_warning` (in `reject` mode: error instead).
- The resolved value is echoed as `resolved_project_id` in the response — the caller can
  always see what actually happened (transparency over silence).
- `@auto` is literal and opt-in. A caller that never sends it is never affected; explicit
  values are never rewritten; the resolved value is then registry-validated like an
  explicit value.

## 6. Tool surface: `get_operating_context` (24 → 25 tools)

Read-only. Arguments:

- (no args) → `{ context_revision, instructions_summary, registry: {project_ids, enforce},
  defaults, doctrine_sections: [names only], _meta }` — the preview tier.
- `section: "recall-discipline"` → that section's full body.

No write tool (§7). Additive to the MCP tool contract (new tool only, no signature
changes to existing tools — response-field additions in §5 are additive fields, which the
2.5.x line declaration treats as preserved-contract, same as `persisted`/`degraded`
precedents).

## 7. Governance: the write path is the filesystem

**There is no MCP write tool for the operating context in 2.5.1.** The sidecar is edited
by the operator through the OS, full stop. This is the strongest available gate against
the contamination path (a compromised or confused agent talking the server into
rewriting the doctrine that all other agents will then receive):

- MCP surface: read-only (`get_operating_context`).
- Write surface: file permissions — operator-owned, same trust level as editing
  `CLAUDE.md` or `bindings.json`.
- Kill switch: `CPERSONA_OPERATING_CONTEXT=off` (env, i.e. also operator-owned).

If a future version wants agent-mediated edits (e.g. "register this new project_id"),
that lands as a separate design with an explicit approval mechanism — deliberately out of
scope here.

## 8. Health integration

Two additive checks in the `check_health` registry (v2.4.37 registry architecture):

- `operating_context_parse` — sidecar present but unparseable / schema-invalid
  (severity: warn; `fix=false` — the fix is a human editing the file).
- `operating_context_size` — instructions summary > 3,000 chars (severity: info;
  fixed-cost discipline).

## 9. Testing plan

Hermetic (tmp-dir sidecar + env override), no live backend needed:

1. Absent / `off` / invalid sidecar → feature dormant, zero behavior deltas (regression
   guard over the full existing suite).
2. Instructions threading: `ToolRegistry(instructions=...)` lands in
   `create_initialization_options()`.
3. Registry modes: off/warn/reject × read/write × known/unknown/`""` project_id.
4. `@auto`: mapped, unmapped, explicit-value-never-rewritten, `resolved_project_id` echo.
5. mtime reload: edit sidecar mid-session → next call sees the new registry.
6. Health checks fire on the two conditions in §8.

## 10. Version position & release path

- **2.5.1, direct release** (owner ruling 2026-07-16): additive feature, no schema/contract
  break → no pre-release ladder. The 2.5.0 a/b ladder was the caution for *breaking*
  internal stabilization, not a general rule. Requires RELEASE_LIFECYCLE_STANDARD v1.2
  (in-line feature cycle + ladder trigger criteria) — companion revision, same batch.
- Tag/release only **after 2.5.0 final**; design and branch implementation proceed in
  parallel with the b1 soak. Note: shipping 2.5.1 during the soak effectively restarts
  the 2.5.x Stable-certification clock (certification is then taken with 2.5.1 included).

## 11. Open questions

1. claude.ai remote connector propagation (§4.1) — measure; result does not change the
   design, only the coverage claim.
2. ClotoCore kernel `instructions` support — file as a ClotoCore issue/goal; decide
   whether it injects into the agent system prompt globally or per-agent.
3. Should `[defaults]` support a wildcard key (`"*"`)? Deferred until a concrete need —
   YAGNI, and a wildcard weakens the explicitness of `@auto`.
