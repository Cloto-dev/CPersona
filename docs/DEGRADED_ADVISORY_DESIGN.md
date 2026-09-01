# Dense-Degraded Runtime Detection + Advisory Context Injection

**Status**: implemented. Route B shipped on the 2.4.x line; Route A replaced it — the
evidence now comes from the embedding call that failed, and the probe is gone (§6).
**Decision**: project owner + claude-code, 2026-06-28 (handoff: CPersona memory `id 1165`, `agent_id=claude-web`, `project_id=cloto`)
**Scope**: surgical patch — no SCHEMA change, no new tool. A new response field + a process-level health state + one env var.

---

## 1. Motivation

The bundled skill runs a **setup-time** self-check. That is a snapshot: it
proves the embedding backend was reachable *at install*. It cannot catch embedding that
**drifts into a degraded state afterwards** — the process dies, the DB is copied to another
machine, a port changes, or a startup race leaves `mode=http` pointing at nothing.

In all of those cases CPersona keeps answering `recall`, silently degraded to FTS-only.
"Still running but degraded" is a **reputation liability**, especially for the casual /
vibe-coder audience the SKILL is meant to serve ("just build me a CPersona"). This design
is the **runtime guard that pairs with the SKILL's install gate** — it flips the silent
failure into a self-reported one.

The problem is already acknowledged in the code:

```python
# config.py:14
# silently off (recall degraded to FTS-only) — bug-001.
EMBEDDING_MODE = os.environ.get("CPERSONA_EMBEDDING_MODE") or os.environ.get("EMBEDDING_MODE", "none")
```

`bug-001` was the env-key fix (the static, install-time half). This design is its
**runtime successor**: the same trap list, shared between the install gate (SKILL) and the
runtime guard (here).

---

## 2. Current code: where degraded is swallowed

Investigation against `master` (`v2.4.32`, `48e2cef`).

### 2.1 The core swallow — `EmbeddingClient.embed()`

`_vendored_mcp_common/embedding_client.py:102-135`:

```python
async def embed(self, texts):
    if self.mode == "none" or not self._client:
        return None                              # (a) FTS-only by configuration
    ...
    try:
        if self.mode == "http":
            result = await self._embed_via_http(texts)
        ...
    except (httpx.RequestError, httpx.HTTPStatusError, ValueError, KeyError) as e:
        logger.warning(...)
        return None                              # (b) http reachable-but-down, swallowed
```

**Both (a) and (b) collapse to `None`.** The caller cannot tell "embedding is intentionally
off" from "embedding is configured but the endpoint is dead". Disambiguating these two is
the heart of the feature.

### 2.2 The secondary swallow — remote vector search

`vector.py:205`:

```python
except Exception as e:
    logger.warning("Remote vector search failed, falling back to local: %s", e)
```

Same shape: a real outage is logged and silently downgraded.

### 2.3 The advisory landing site — `do_recall`

`memory_handlers.py:702` `do_recall(...)` returns a single structure at `:825`:

```python
return {"messages": messages}
```

The advisory attaches here as a sibling field. `test_do_recall_response.py` already
regression-tests this response contract, so the field has test coverage to extend.

### 2.4 The state-storage precedent

A **process-level module state** already exists in this codebase: the no-persist toggle,
and the per-agent `dict`s in `vector.py` (`_agent_thresholds`, `_agent_fused_gates`).
The health state is placed the same way — a module singleton, reset on restart.

---

## 3. Confirmed spec (9 points, from handoff `id 1165`)

| # | Spec | Landing in code |
|---|------|-----------------|
| 1 | Detection by **measurement**, not config-read. Surface `{attempted, ok, error}` at the embedding-client boundary; `do_recall` reads it. | New `health.observe_*` calls at the `embed()` call site in `_search_vector`; `do_recall` reads `health.snapshot()`. |
| 2 | **State machine, 4 states**: `unknown` / `healthy` / `hint` / `fault`. Process-level (reset on restart). | New `health.py` module singleton (mirrors no-persist module-state). |
| 3 | **Severity split**: `hint` = embedding unset (`mode=none`, FTS-only, static → immediate). `fault` = `mode=http` but endpoint unreachable (promote on **2 consecutive** failures; debounce single blips per the CoreML-hang precedent). | `hint` set from `config.EMBEDDING_MODE`; `fault` gated by a consecutive-failure counter. |
| 4 | **Firing by transition**: each `healthy→degraded` first transition emits one **full ~1000-char** template; subsequent recalls during the *same* outage emit a **short ~100-char** reminder; `healthy` is **completely silent**. | `health` records `advisory_emitted_for_current_outage`; `do_recall` chooses full vs short vs none. |
| 5 | **Dynamic evidence** embedded into both full and short payloads (e.g. `mode=http / POST http://127.0.0.1:8401/embed failed: connection refused`). Template = static skeleton, problem = dynamic slot. | `health.evidence` carries the error captured by the embedding call itself. |
| 6 | **Payload = struct** `{degraded, severity, reason, evidence, runbook}`. The agent renders/localizes it (language + tone are the agent's domain). Imperative phrasing ("notify the user: ...") raises relay odds. | `advisory` field value is this struct; rendering left to the client. |
| 7 | **Carrier = the `recall` response advisory field**. MCP cannot push → honest reach is "fault surfaces on the *next* recall". Relay is best-effort and must say so. | New `advisory` key alongside `messages`. |
| 8 | **On by default / env opt-out.** The opt-out records an operator who accepts running without an embedding backend — a supported fallback, not a recommendation. Safe-by-default. | `CPERSONA_DEGRADED_ADVISORY` (default `true`). |
| 9 | **`fault` runbook skeleton**: state + measured evidence / impact (plain) / investigation steps / repair commands / one plain user-facing sentence / opt-out env. | Static template strings in `health.py`. |

---

## 4. Route B (accepted, 2.4.x line) — cpersona-local probe

### 4.1 Why Route B now

`embed()` lives in `_vendored_mcp_common/` — a shared client vendored into CPersona. At the
time, making `embed()` itself surface `{attempted, ok, error}` was judged to require a
release of the upstream package and a change every consumer would have to absorb, which
contradicted the "surgical patch / no new tool / 2.4.x QOL line" framing.

That premise did not survive contact with the change (§6): an additive method left the
existing entry point untouched, so no consumer had to absorb anything.

Route B keeps the change **cpersona-only**: `embed()` is left untouched; CPersona derives
health from (a) `config.EMBEDDING_MODE` for the static `hint` case and (b) **its own
lightweight health-probe** for the `fault` case, capturing the real error string the probe's
own `try/except` sees.

### 4.2 New module — `health.py`

```python
"""Process-level embedding-health state for the degraded-advisory guard.

Module singleton, reset on restart (mirrors the no-persist module-state). Fed by
observations from the recall path; read by do_recall to attach an advisory.
"""

# 4 states (point 2)
UNKNOWN, HEALTHY, HINT, FAULT = "unknown", "healthy", "hint", "fault"

_state = UNKNOWN
_severity = None            # "hint" | "fault"
_reason = None              # short machine reason
_evidence = None           # dynamic: the measured failure, e.g. "POST .../embed: connection refused"
_consecutive_failures = 0   # debounce counter (point 3)
_advisory_emitted = False   # full-vs-short selector (point 4)

FAULT_PROMOTE_THRESHOLD = 2  # consecutive failures before healthy->fault (point 3)
```

Key transitions:

- **`observe_config()`** (called once at do_recall entry): if `EMBEDDING_MODE == "none"`,
  set `HINT` immediately (static, no debounce). Otherwise leave the http path to the probe.
- **`observe_ok()`**: embedding produced a usable vector → `HEALTHY`, reset
  `_consecutive_failures`, clear `_advisory_emitted` (so a re-failure later re-emits the full
  template — point 4 "recovered→re-failed re-arms").
- **`observe_failure(evidence)`**: `mode=http` attempt failed → `_consecutive_failures += 1`;
  promote to `FAULT` only at `>= FAULT_PROMOTE_THRESHOLD` (debounce single blips).

### 4.3 The probe

> **Superseded by §6.** The probe described here no longer exists. It is kept because the
> reason it was needed is the reason the current design looks the way it does.

When `_search_vector` called `embed([query])` and got a falsy result while
`EMBEDDING_MODE != "none"`, CPersona ran `_probe_embedding_health()`:

```python
async def _probe_embedding_health() -> tuple[bool, str | None]:
    """Direct, non-swallowing health POST to the embedding endpoint.

    Returns (ok, error_string). Unlike embed(), this does NOT swallow — it captures
    the actual transport error for the advisory's evidence slot (point 5).
    """
    client = vector._embedding_client
    try:
        resp = await client._client.post(client._http_url, json={...minimal probe...}, timeout=...)
        resp.raise_for_status()
        return True, None
    except Exception as e:
        return False, f"mode=http / POST {client._http_url} failed: {e}"
```

- Probe runs **only on a suspected failure** (embed returned falsy on a non-empty query),
  not on every recall — bounded extra I/O, and the embedding cache already absorbs repeats.
- The probe's captured error is the **dynamic evidence** (point 5).
- Debounce (point 3): two consecutive probe failures promote `HINT`/`HEALTHY`→`FAULT`.

> **The double-I/O and the disagreement it allowed**: the probe was a *separate* POST from
> the real recall-path `embed()` call, so the two could disagree — and the direction that
> mattered was the probe succeeding while the real call failed, because the branch then
> recorded health as OK on a recall that returned nothing. The probe also carried the local
> server's payload shape to `_http_url`, which an api-mode client does not have, so that
> mode could only ever produce "embedding client unavailable" instead of evidence. Both are
> gone with the probe (§6).

### 4.4 `do_recall` integration

At `do_recall` entry, `health.observe_config()`. The recall path feeds `observe_ok()` /
`observe_failure()` from the outcome of the embedding call itself. Before `return {"messages": messages}`:

```python
advisory = health.maybe_advisory()  # None when healthy/opted-out; full or short struct otherwise
if advisory is not None:
    return {"messages": messages, "advisory": advisory}
return {"messages": messages}
```

`maybe_advisory()` returns `None` when `_state == HEALTHY` or the env opt-out is set; a
**full** struct on the first transition of an outage (`not _advisory_emitted`, then sets it);
a **short** struct on subsequent recalls within the same outage.

### 4.5 Advisory payload (point 6)

```jsonc
{
  "degraded": true,
  "severity": "fault",                      // or "hint"
  "reason": "embedding endpoint unreachable",
  "evidence": "mode=http / POST http://127.0.0.1:8401/embed failed: connection refused",
  "runbook": "<full or short text per point 4/9>"
}
```

`runbook` for `fault` (full, point 9 skeleton): state + measured evidence / plain-language
impact / investigation steps (process alive? port? `curl` result? model downloaded?) /
repair commands (start the embedding server / fix URL+port / re-run the setup steps if the
backend must be reinstalled) / one plain user-facing sentence / the opt-out env. Phrased imperatively to raise relay odds (point 6).

### 4.6 Env opt-out (point 8)

```python
DEGRADED_ADVISORY_ENABLED = os.environ.get("CPERSONA_DEGRADED_ADVISORY", "true").lower() == "true"
```

On by default. Opting out silences the advisory for an operator who accepts running
without an embedding backend; it does not make that configuration a recommended one.

### 4.7 Tests

- Extend `test_do_recall_response.py`: (a) `mode=none` → `hint` advisory present; (b)
  `mode=http` + probe fails twice → `fault` advisory with evidence; (c) one blip (single
  failure) → **no** advisory (debounce); (d) `healthy` → no `advisory` key at all; (e)
  full-then-short across two recalls in one outage; (f) recovery clears state and re-arms;
  (g) env opt-out silences everything. Probe is monkeypatched (no live endpoint needed).

---

## 5. Out of scope

- No SCHEMA change, no new MCP tool (response field + env only).
- No push (MCP cannot) — reach is "next recall surfaces it" (point 7), stated honestly.
- bge-m3 mac CoreML hang guard remains best-effort / unverified (handoff open item).

---

## 6. Route A — shipped

The detection is folded into the boundary itself, and the probe (§4.3) is **removed**: the
health state is fed directly from the real recall-path call.

It did not need the breaking change §4.1 assumed. `embed()` keeps its signature and its
return values exactly; an additive `embed_with_outcome()` returns the same value alongside
`{attempted, ok, error}`, and the recall path calls that one. Nothing else had to change,
which is why this landed on the 2.5.x line rather than waiting for a major version.

The outcome is returned to the caller rather than stored on the client, so two concurrent
embeds cannot read each other's result. One case is worth naming: a 2xx response carrying
no embeddings is reported as a failure, because it is one for the caller — and it is
exactly what a separate probe got wrong, since the probe saw the same success code.

**Why the layering is clean (forward-compat)**: Route B's **advisory contract is the stable
interface** — the payload struct `{degraded, severity, reason, evidence, runbook}` and the
`do_recall` `advisory` field do not change. Route A is a **"swap the signal source"**
refactor (probe → embed() result), not a redesign. The user-facing contract is identical;
the evidence is *upgraded* from a separate probe POST to the actual recall-path call,
eliminating the §4.3 double-I/O and the probe-vs-real-call race.

Cross-repo cost to budget for 2.5.0: clotohub-servers `servers/common/` change →
`clotohub-servers-common` bump → re-vendor into CPersona → revalidate other consumers
(CScheduler embedding, etc.).

---

## 7. Implementation notes / corrections (v2.4.33 build)

Refinements discovered while implementing Route B; these supersede the earlier sections
where they conflict.

1. **No `HINT→FAULT` path** (supersedes §4.3 wording). When `EMBEDDING_MODE=="none"`,
   `server.py:959` never constructs the client, so `vector._embedding_client is None` and
   the embed/probe path is never entered. `hint` is therefore detected *solely* by
   `health.observe_config()` at `do_recall` entry, and `fault` only ever promotes from
   `unknown`/`healthy`.
2. **Two advisory return sites** (supersedes the §2.3 "single structure at :825" framing).
   `do_recall_with_context` builds its own return and extracts only `messages` from
   `do_recall`'s result, so it must **forward** `recall_result.get("advisory")` explicitly
   or the advisory is dropped. It must NOT call `maybe_advisory()` again (that would flip
   full→short within one logical recall).
3. **Probe placement** (refines §4.3; **superseded by §6** — the probe and its dedicated
   timeout are gone, and the failure path no longer needs the `health.is_faulted()` gate
   that existed to bound probe I/O). The observation point is still the local embed path in
   `vector.py`, and `health.py` still takes no `vector` import, so the dependency graph is
   unchanged: `config ← health ← vector ← memory_handlers`. Recovery is still observed on
   the embed **success** path.
4. **Remote-search swallow needs no separate hook** (refines §2.2). On
   `VECTOR_SEARCH_MODE=="remote"` a remote failure falls through to the instrumented local
   embed path, so only the local path is wired (production uses local mode).
5. **No tool-schema change** — `_vendored_mcp_common/mcp_utils.py` `json.dumps`es the whole
   handler return dict, so the extra `advisory` key reaches the client for free.

**Files**: `health.py` (new), `vector.py` (probe + observe at the local embed path),
`memory_handlers.py` (`observe_config` at entry; advisory at both return sites), `config.py`
(`CPERSONA_DEGRADED_ADVISORY`), `test_do_recall_response.py` (state-machine units + do_recall
integration + probe units; autouse `health._reset`). Tests: 13/13 green; recall-SQL
regression `test_channel_axis_migration` 7/7 + `test_episode_channel` 10/10 green.

---

## 8. Suppression scope (bug-251)

Supersedes the point-4 firing rule in §3 and the payload field lists in §4.5 and §6.

**The defect.** "The full runbook already fired" is process state
(`health._advisory_emitted`), and the once-per-episode downgrade keys on it. Under stdio
that is the intended rule — one process serves one client session. But
`CPERSONA_TRANSPORT=streamable-http` runs `StreamableHTTPSessionManager(stateless=True)`,
so one process answers every connected client: the first recall of an outage consumed the
full runbook for everybody. Every other session received `FAULT_RUNBOOK_SHORT`, which
carries no `**Notify the user:**` imperative and reads as a follow-up to a message that
session never got. Point 7's honest reach — "fault surfaces on the *next* recall" —
quietly became "on the next recall of one session, once per outage".

Measured through the real transport, two clients in one process during one outage: the
first got 1067 characters with the imperative, the second got 107 without it.

**The rule now.** A `fault` does not downgrade while the process serves several sessions.
An outage is rare and the runbook is the point of the feature, so paying for it on every
recall beats paying silence on every session but one.

A `hint` still downgrades. `mode=none` is permanent, so the exemption would repeat the
full runbook on every recall forever, and running without an embedding backend is a
standing condition rather than an outage to be escalated.

**The payload says which rule is in force.** `advisory_scope` is `"process"` when the
suppression state is shared and `"session"` when the process is the session, so a client
can tell a reminder it never received from a follow-up to one it did. The no-persist
toggle discloses its blast radius the same way; this advisory used to degrade its own
payload silently instead.

**Why not per session.** Nothing at the recall seam identifies a session to key on. The
HTTP mode is stateless, so no session survives a request, and the ACL principal carries
only a client id — two windows sharing one credential are one principal. A caller-supplied
key would give one, at the cost of an argument every client has to pass; `advisory_scope`
is the field that would then start answering `"session"`, with no change of shape.
