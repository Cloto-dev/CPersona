# Declared Session Identity (`session_key`)

Status: proposed for the 2.5.7b1 line. Additive and behavior-preserving —
a caller that sends nothing keeps today's behavior byte for byte.

## 1. The problem: a process is not a session

Under the stdio transport one process serves one client, so process-global
state is session-scoped by construction. Under streamable-HTTP it is not:
the server runs `StreamableHTTPSessionManager(..., stateless=True)`, one
process answers every connected client, and no session survives a request.
`config.shared_transport()` already names that condition and exists because
call sites kept asking it.

Two defects in the bug ledger are the same missing axis seen twice, and both
were repaired only as far as honesty allowed:

- **bug-151** — `pause_persistence` is a bare process-global flag. One
  client's pause silently turns every other connected session's writes into
  no-ops until the TTL elapses. The fix added a `scope: "process"` field to
  the pause, resume and status payloads and corrected the docstrings that
  falsely implied per-session scope. **No per-session state was added**: the
  blast radius is disclosed, not removed.
- **bug-251** — the degraded-recall advisory fires its full "notify the
  user" runbook once per *process*, so during an outage exactly one session
  is told and every other session receives a follow-up to a message it never
  saw. The repair scoped the suppression to the transport instead, and its
  note says why it could go no further: per-session suppression *"needs a
  caller-supplied key"*. `health.py` states the same thing in the future
  tense — `advisory_scope` becomes `session` "the day a caller-supplied key
  reaches this seam".

A third surface already carries the parameter. `get_session_findings`
accepts `session_key` because the SuperAuditor standard §7 requires an
implementation that cannot tell sessions apart to say so; its docstring
admits the key "changes nothing but the honesty flag".

In the deployment this was written for, the client cannot supply that
identity out of band: environment variables are captured when the process
spawns, header values are expanded once at startup, and the transport
session id is not echoed back to the server on subsequent calls. Identity
has to travel as data, in the arguments of the call — which is the same
conclusion CScheduler reached before adding its own `session_key`.

## 2. What this is, and what it must never become

`session_key` is an **opaque, client-declared partition hint**. The server
attaches no meaning to its bytes: it compares them and nothing else.

It is **not authentication**. Any caller can send any string, including one
belonging to another session. It is orthogonal to the per-client capability
work and to OAuth; those decide *whether* a call is allowed, this decides
*which in-process bucket* an allowed call lands in.

It is **not a fourth isolation axis**. `agent_id`, `project_id` and
`channel` select *whose data* a query reads. `session_key` selects *whose
in-process state* a call touches. No row in the database is filtered by it,
and no stored memory becomes reachable or unreachable because of it. This
line is drawn here, at the start, because the obvious next request —
"filter my recall to this session" — would make memory unreadable across
the boundary it exists to cross, and would need a completely different
justification than the two defects above.

## 3. The seam

One resolution point, mirroring the sibling implementation:

```python
def resolve_session_key(arguments: dict) -> tuple[str, bool]:
    """Return (effective_key, declared)."""
    raw = arguments.get("session_key")
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped:
            return stripped, True
    return _transport_fallback_key(), False
```

- A non-empty, non-whitespace string is the effective key, and `declared` is
  true.
- Absent, empty or whitespace-only falls through to the transport fallback
  with `declared` false — byte-for-byte today's behavior for every existing
  caller.
- No length limit, no format validation, no sanitization beyond `strip()`.
  The value is compared, never parsed, never rendered into SQL identifiers,
  never logged as an identity claim.

The fallback is a per-process constant. Under stdio that is already a
session, because the process is one; under streamable-HTTP it is a single
shared bucket that every keyless caller lands in — exactly the state of the
world today. The fallback deliberately does not try to *derive* a session
from the process: under the shared transport the process's own lineage
describes the server, not any of its callers, and a key derived from it
would look like an answer while partitioning nothing.

## 4. What it partitions — the complete list

Measured, not assumed. Every module-level mutable value in the package was
enumerated; these are the two that are process-global *and* session-shaped:

| state | today | with a declared key |
| --- | --- | --- |
| `no_persist` pause flag (`_no_persist_until`) | one flag for the whole process | one entry per key |
| degraded-advisory suppression (`health._advisory_emitted`) | one flag for the whole process | one entry per key |

Everything else that looked like a candidate is not one:

- The vector module's threshold, fused-gate and beta caches are keyed by
  **agent**, and calibration authority likewise. An agent's calibration is
  meant to be shared by that agent's sessions.
- Recall precision and profile state live in the database, per agent.
- The database locks are per process because the database is.

Both partitioned values live in process memory. **No table changes, no
migration, no GC job.** This is the substantive difference from the sibling
implementation, which had to add a `session_key` column to three tables and
a schema migration, because the state it partitions is persistent.

## 5. Staging

The parameter is not free (§6), so it is introduced where it pays for itself
first.

### Stage 1 — the advisory, and disclosure for the pause

Tools that accept `session_key`: `recall`, `recall_with_context`,
`pause_persistence`, `resume_persistence`, `persistence_status`.

- **The advisory becomes genuinely per-session.** Suppression is keyed on
  the effective key, so during an outage every session that recalls is told
  once, with the full runbook, and `advisory_scope` answers `session`
  instead of `process` for callers that declared one. bug-251's deferred
  half closes here.
- **The pause discloses its owner.** The pause records the key that armed
  it; `persistence_status` reports whether the caller's own key armed it or
  another session did, and a write skipped by an inherited pause can say so.
  Writes are still globally paused: this is disclosure, not isolation, and
  the doc must not claim otherwise.

### Stage 2 — the pause becomes per-session

Every tool that consults `no_persist.is_paused()` must know its own key,
which means threading `session_key` through the write surface (`store`,
`archive_episode`, `update_memory`, `lock_memory`, `unlock_memory`,
`delete_memory`, `delete_episode`, `delete_agent_data`, `update_profile`,
`calibrate_threshold`, `set_recall_precision`, `migrate_channel_axis`, plus
`export_memories` / `import_memories` / `merge_memories`).

Keyless callers keep sharing one bucket, so a keyless pause continues to
pause every keyless caller — behavior-preserving by construction.

Stage 2 is gated on the measured cost of Stage 1 (§6), not scheduled by
default.

### Out of scope

A session-scoped findings probe — "records this session touched and left
pending", which §7 of the SuperAuditor standard contemplates and the sibling
implementation has — would require recording *which session wrote a row*,
and that is a stored column, a migration and a retention question. It is not
part of this design. Until it exists, `get_session_findings` keeps returning
`identity_shared: true` for keyless remote callers and nothing else changes
there.

## 6. The cost of the parameter itself

Every tool that accepts `session_key` carries its description in the tool
list that clients load on every session. This is a fixed cost paid by every
caller, including those that never declare a key, and it is the reason this
design stages rather than threading everything at once.

The commitment: **measure the tool-list size before and after Stage 1**, and
put the number in the pull request. Stage 2 multiplies that cost by roughly
three (five tools to fifteen or more), and that measurement — not a
preference — decides whether Stage 2 ships.

Mitigation, if the number is unpleasant: one short description referring to
this page, rather than the full explanation repeated per tool.

## 7. Lifetime

Both partitioned values are bounded in-process maps, not persistent rows.

- The pause already carries a TTL and clears lazily; per key, the same TTL
  applies, and an entry disappears when it expires.
- Advisory suppression entries are bounded by an eviction cap, because the
  key space is client-supplied and a client that rotates keys must not grow
  the map without limit. Eviction only forgets that a session was already
  told, so the worst case is a repeated notice — the safe direction.
- A process restart drops both, which is correct: no session survives it.

There is nothing to garbage-collect on disk, and no key ever reaches the
database.

## 8. Degradation contract

A keyless caller on a shared transport is told, not guessed about:
`identity_shared: true` on the affected responses, as `get_session_findings`
already does and as the SuperAuditor standard requires. Degrading honestly
is the requirement; degrading silently is the defect.

Under stdio, `identity_shared` is absent — the process is the session, and
saying otherwise would be false.

## 9. Version placement

The 2.5.7 line is at `2.5.7a2`. This lands as **2.5.7b1**: additive,
behavior-preserving, no schema change, so promoting the line from alpha to
beta to carry it is consistent with the release lifecycle standard, which
permits additive features inside a Current-tier line. The pre-release series
graduates to `2.5.7` final — it does not jump to another number.

## 10. Tests

- Resolution: declared / absent / empty / whitespace-only, each asserting
  both the returned key and the `declared` flag.
- Behavior preservation: an existing keyless call sequence produces the same
  responses before and after, under both transports.
- Advisory: two distinct keys each receive the full runbook during one
  outage; the same key twice receives the short form; a keyless remote pair
  keeps today's transport-scoped behavior.
- Pause disclosure: `persistence_status` distinguishes an own-key pause from
  a foreign-key pause; a write skipped under a foreign pause says so.
- Honesty: keyless remote responses carry `identity_shared: true`; stdio
  responses do not carry it at all.
- Mutation proof: removing the `strip()`, removing the empty-string guard,
  and removing the eviction cap each fail a test that names the defect.
