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
def resolve_session_key(declared: str | None) -> tuple[str, bool]:
    """Return (effective_key, declared)."""
    if isinstance(declared, str):
        stripped = declared.strip()
        if stripped:
            return stripped, True
    return TRANSPORT_KEY, False
```

It takes the value, not the request: this server's tool registry validates and
extracts declared parameters before dispatch, so a handler receives keyword
arguments and never sees an argument dict. (The sibling implementation resolves
from the dict because its dispatch hands one over. Same seam, different host.)

- A non-empty, non-whitespace string is the effective key, and `declared` is
  true.
- Absent, empty or whitespace-only falls through to the transport fallback
  with `declared` false — byte-for-byte today's behavior for every existing
  caller.
- No length limit, no format validation, no sanitization beyond `strip()`.
  The value is compared, never parsed, never rendered into SQL identifiers,
  never logged as an identity claim.

The fallback is a per-process constant (`TRANSPORT_KEY`). Under stdio that is
already a session, because the process is one; under streamable-HTTP it is a
single shared bucket that every keyless caller lands in — exactly the state of
the world today. The fallback deliberately does not try to *derive* a session
from the process: under the shared transport the process's own lineage
describes the server, not any of its callers, and a key derived from it
would look like an answer while partitioning nothing.

A caller may of course declare the literal fallback value and join that bucket.
That is not a hole to close: the key is a hint, and a caller that can send one
key can send another. Nothing is defended by it.

## 4. What it partitions — the complete list

Measured, not assumed. Every module-level mutable value in the package was
enumerated; these are the two that are process-global *and* session-shaped:

| state | today | with a declared key |
| --- | --- | --- |
| the no-persist pause (was `no_persist._no_persist_until`, now `session._pauses`) | one flag for the whole process | one entry per key |
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

**Implemented.** Every tool that consults the pause now knows its own key, so a
session silences its own writes and nobody else's. `session_key` is threaded
through the sixteen write tools that actually consult it (`store`,
`archive_episode`, `update_memory`, `lock_memory`, `unlock_memory`,
`delete_memory`, `delete_episode`, `delete_agent_data`, `update_profile`,
`calibrate_threshold`, `set_recall_precision`, `migrate_channel_axis`,
`import_memories`, `merge_memories`, `check_health`, `deep_check`).

`export_memories` does **not** take the parameter. An earlier draft of this
section listed it, but it consults no pause gate, so the key would have been a
description every client loads for a tool it can never change. The list above is
what the code does, not what the plan said.

Keyless callers keep sharing one bucket, so a keyless pause continues to pause
every keyless caller — behavior-preserving by construction rather than by a
special case, because that bucket is just `TRANSPORT_KEY`.

**One implementation, not two.** The obvious shape — leave the keyless bucket on
the vendored process-global flag and add a map for declared keys — would have
been two implementations of one invariant, and the one nobody exercises is the
one that drifts. Instead `cpersona.session` owns the pause for every bucket, and
the vendored module keeps only what is genuinely shared: the TTL constants and
`make_skipped_response`, whose id-sentinel and action-id nulling (bug-104) are
invariants worth exactly one copy. Two tests hold that line — one fails if a call
to the vendored switch reappears anywhere in the package, one pins this module's
TTL argument handling against the vendored rules it replaced, so a re-vendor that
changes them is caught rather than silently diverged from.

**What the disclosure fields became.** Stage 1 answered "is the pause silencing
me mine, or a parallel session's?" with `pause_owner_known` / `paused_by_self`.
Stage 2 removes the question: a foreign pause cannot silence you, so the fields
would be vacuously true whenever they appeared. They are gone for declared
callers, replaced by `scope: "session"`. A keyless response is unchanged, keys
and all, including `scope: "process"` — which stays true, because every keyless
caller on the process still shares one bucket.

**Queued work.** The task queue drops a queued write if the pause is on, so that
"the user's ephemeral intent overrides queued work that pre-dates it". A queue
row carries no session — recording one is a stored column, and out of scope
below — so the queue keeps an in-process map from task id to the key that
enqueued it, and gates each task on that key. A row that survives a process
restart loses its attribution and falls back to the transport bucket, which is
correct: no session survives a restart either.

### Out of scope

A session-scoped findings probe — "records this session touched and left
pending", which §7 of the SuperAuditor standard contemplates and the sibling
implementation has — would require recording *which session wrote a row*,
and that is a stored column, a migration and a retention question. It is not
part of this design. Until it exists, `get_session_findings` keeps returning
`identity_shared: true` for keyless remote callers and nothing else changes
there.

## 6. The cost of the parameter itself

Every tool that accepts `session_key` carries its description in the tool list
that clients load on every session. This is a fixed cost paid by every caller,
including those that never declare a key, and it is the reason this design staged
rather than threading everything at once.

Measured on the serialized tool list this server advertises, by deleting only the
`session_key` property from the live payload — not by diffing a merge commit
against its parent, which mixes in every unrelated description change that rode
along in the same commit:

| | tools | serialized chars |
| --- | --- | --- |
| no `session_key` at all | 30 | 38,970 |
| after stage 1 | 30 | 41,719 |
| delta | 0 | **+2,749 (+7.1%)** |

The description is written once and shared by every schema that uses it, so the
number above is one shared text, not five copies of a longer one.

Every figure in this section is reproducible with `scripts/measure-tool-list.py`,
which prices a named parameter by deleting it from the live payload. The script
exists because this section promises that a measurement decides the next stage,
and a promise nobody can re-run is one that gets quoted from memory instead.

### What stage 2 was measured against

Stage 2 was gated on this number, and four arms were measured before choosing —
by editing the real payload, not by arithmetic:

| arm | description strategy | serialized chars | vs no-key baseline |
| --- | --- | --- | --- |
| A | full shared text on every newly keyed tool | 50,372 | +29.3% |
| B | a short text on the newly keyed tools | 45,221 | +16.0% |
| C | the short text everywhere, existing uses rewritten | 43,706 | +12.2% |
| **D** | **short text everywhere except `recall` / `recall_with_context`** | **46,113** | **+17.1%** |

The projection that a shorter description was *possible* was not a hypothesis:
`get_session_findings` already shipped one, so the saving could be measured
rather than estimated.

**Arm D is the choice.** The full text's load-bearing clause is "it does NOT
filter stored data (use agent_id / project_id / channel for that)" — §2's line,
stated at the point where a reader is most likely to cross it. `recall` and
`recall_with_context` are where "does this filter which memories I can see?" is
actually asked, so they keep the full text; everywhere else carries a compressed
form that keeps that clause and drops the elaboration around it.

**A correction, because the first projection was wrong.** Arm D was first priced
using the 206-character text `get_session_findings` carries — but that text does
not contain the clause arm D exists to preserve, so arm D had been priced with
arm C's description. The real compressed text is 289 characters per tool against
the full text's 509. Arm D therefore costs more than the first projection said.
It does not change the ranking — every arm is far below A — but the number that
was quoted was measuring the wrong string, and a projection nobody re-measures
after writing the actual words is a guess wearing a table's clothes.

### What stage 2 actually cost

Measured the same way, after the wiring landed: 22 of 30 tools carry the key.

| | serialized chars |
| --- | --- |
| tool list with no `session_key` at all | 39,381 |
| tool list as shipped | 46,113 |
| the parameter | **+6,732 (+17.1%)** |

Per tool: 509 for the two that keep the full text, 289 for the compressed one,
206 for the one `get_session_findings` writes itself.

**+411 characters of that total are not the parameter.** The no-key baseline
itself moved from 38,970 to 39,381, because the three pause tools' own
descriptions had to be rewritten: each of them asserted that the pause is
process-wide and silences every connected session, which is exactly what stage 2
makes false. Leaving them would have been cheaper and would have made the tool
list lie about the tool it describes. It is counted here rather than folded into
the parameter's number, because they are different costs and only one of them is
what the arms were choosing between.

## 7. Lifetime

The partitioned values, and the attribution map that serves them, are bounded
in-process maps — not persistent rows.

- The pause already carries a TTL and clears lazily; per key, the same TTL
  applies, and an entry disappears when it expires.
- Advisory suppression entries are bounded by an eviction cap, because the
  key space is client-supplied and a client that rotates keys must not grow
  the map without limit. Eviction only forgets that a session was already
  told, so the worst case is a repeated notice — the safe direction.
- Pause entries are bounded by a cap too, but **eviction there is not the safe
  direction**: forgetting a pause resumes writes for that session. The two maps
  therefore do not share a policy — **the pause map does not evict at all**. A
  pause, once granted, holds until its TTL or an explicit resume; at the cap the
  *new* request is refused, so the failure lands on the caller making it, who can
  read the answer, instead of on a session that would never learn its guarantee
  had stopped holding. The cap counts live pauses, and decay runs before the
  check, so a full map recovers on its own. Re-arming a key that is already
  paused is never refused: it occupies a slot it already holds.
- The queue's task-to-key attribution is in-process. The queue drops an entry on
  every path it drives itself, and rows also vanish underneath it — agent-data
  deletion, a move-mode merge and the stale-queue repair each delete from the
  table without going through the queue — so a drain pass ends by reconciling the
  map against the rows that still exist. Naming those three deleters instead would
  be correct only until the fourth one is written, and would have a health probe
  and two admin handlers reach into in-process queue state to do it.
- A process restart drops all three, which is correct: no session survives it.
  A queued row that outlives the restart falls back to the transport bucket.

There is nothing to garbage-collect on disk, and no key ever reaches the
database.

## 8. Degradation contract

A keyless caller is told, not guessed about. What it is told depends on what
the response already says, because a keyless response must keep the exact shape
it had — a new key on it would break the preservation §3 promises:

- `get_session_findings` keeps carrying `identity_shared: true` for a keyless
  caller on a shared transport, as the SuperAuditor standard requires. That
  field predates this design.
- The pause trio answers a keyless caller with `scope: "process"`, which states
  the blast radius exactly and is byte-for-byte the response it always gave. A
  declaring caller gets `scope: "session"` instead. Stage 1's ownership fields
  (`pause_owner_known`, `paused_by_self`) are gone: stage 2 made the question
  they answered unaskable, since no other session's pause can reach you.
- A recall response carries the regime in `advisory_scope` — but only inside an
  advisory, which is only present when recall is degraded. A healthy keyless
  recall gains nothing, which is the point.

Under stdio nothing claims a shared identity, because the process is the
session and saying otherwise would be false.

## 9. Version placement

**Stage 1** landed in the **2.5.7 beta series**. The line promoted from alpha to
beta at `2.5.7b1`, which carries the findings pull tool; stage 1 was the next
release in that series, not `b1` itself. Its reasoning was that it is additive,
behavior-preserving and schema-free, which is what the release lifecycle standard
permits inside a Current-tier line (§2.6 — what waits for the next line is a
change that cannot be rolled back).

**Stage 2 cannot borrow that reasoning, and must not pretend to.** It changes
default behavior — a pause that used to silence every connected client now
silences one session — and it removes two response fields that stage 1 shipped.
Under the standard (§2.1) that is exactly the trigger for the pre-release ladder:
not rollback-safe, a change to default behavior, a break in the tool contract.
It therefore opens its own ladder rather than riding one that soaked different
code. The `2.5.7` series had already graduated by the time this was ready, so
stage 2 starts a new series at **`2.5.8a1`** — an alpha, not a beta, because the
point of the first rung is to put a per-session pause in front of real traffic
before anything is called ready.

Two consequences follow, and neither is optional:

- That series graduates to `2.5.8` final and does not jump to another number.
  Whichever pre-release is soaking when the ladder completes is the content that
  becomes final — a graduation that names a version nobody ran is the failure
  this line has already made once.
- Callers documented elsewhere as needing to check for parallel sessions before
  pausing are relying on the old blast radius. That guidance describes the
  behavior stage 2 removes, and it goes stale the moment this ships.

## 10. Tests

- Resolution: declared / absent / empty / whitespace-only, each asserting
  both the returned key and the `declared` flag.
- Behavior preservation: an existing keyless call sequence produces the same
  responses before and after, under both transports — including the absence
  of any field a keyless caller did not have before.
- Advisory: two distinct keys each receive the full runbook during one
  outage; the same key twice receives the short form; a keyless remote pair
  keeps today's transport-scoped behavior.
- Per-session pause: a pause declared by one key skips that key's writes,
  leaves another key's and the keyless bucket's alone, and the reverse for a
  keyless pause. `scope` reads `session` for a declared caller and `process`
  for a keyless one on all three of pause / resume / status.
- TTL: pauses expire per key on an injected clock, not on a slept-through
  wall clock; `resume` reports `was_active` for the caller's own key only.
- Honesty: keyless remote responses carry `identity_shared: true`; stdio
  responses do not carry it at all.
- Copied invariants: the TTL argument rules are asserted to agree with the
  vendored module they were copied from, across bools, zero, negatives, the
  ceiling and non-integers — the test is what keeps the copy honest when the
  vendored module is re-vendored.
- Structural: no call to the vendored pause switch survives anywhere in the
  package outside the module that replaced it. The searched-for strings are
  built by concatenation so the test cannot satisfy its own pattern.
- Queue attribution: a task enqueued under one key is dropped when that key
  pauses and kept when a different key does; an unattributed row is gated on
  the transport bucket; and a row deleted out of band — by the handler that
  really deletes it — leaves no attribution behind after the next drain pass.
- Mutation proof: removing the `strip()`, the empty-string guard, the advisory
  eviction cap, the pause map's refusal at the cap, the per-session branch of the
  pause lookup, the TTL substitution in the skipped-write reason, the queue's
  attribution lookup, and the queue's reconcile pass each fail a test that names
  the defect.
