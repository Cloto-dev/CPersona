# Repairing a degraded embedding backend

CPersona answers `recall` from three retrieval layers. Two of them — FTS5 and
keyword — live inside the SQLite file and cannot fail on their own. The third,
vector search, reaches an embedding server over HTTP, and it is the one that
degrades silently at the network boundary.

This file is the walkthrough for that single failure: how to tell which
condition you are in, what to say about it, what to propose, and how to prove
the repair worked. It is a separate file because it is only needed when
something is actually wrong — the day-to-day store / recall / archive workflow
in `SKILL.md` never reaches it.

---

## 0. What this file authorizes

Diagnosing and explaining: freely. Everything else: a proposal.

Starting a process, editing an MCP client's configuration file, changing an
environment variable, restarting a server — each is a change to the user's
machine, and the approval rules you already operate under govern them here
exactly as they do anywhere else. Show the exact command or the exact edit, say
what it will do, ask, and wait for an answer.

Nothing about a degraded backend makes that urgent. The memories are intact,
writes keep succeeding, and one of three search layers is off. There is no
state that decays while you wait for the user to reply.

---

## 1. Name the condition before you explain anything

Two conditions degrade recall and they need different conversations. Telling
them apart is the first step, not a detail.

| | **HINT** | **FAULT** |
|---|---|---|
| Configuration | no embedding backend configured (`mode=none`) | a backend is configured (`mode=http` or `mode=api`) |
| What it means | a standing, deliberate configuration | an outage: the configured endpoint did not answer |
| `advisory.severity` | `hint` | `fault` |
| `advisory.evidence` | `mode=none / no embedding backend configured` | names the mode, the endpoint, and the failure |
| Raised after | the first `recall` | two consecutive failed embed calls (one blip is debounced) |
| Right response | offer to connect a backend | find out why it stopped answering |
| Wrong response | treating it as a fault the user must fix now | treating it as a configuration choice |

A `hint` is not a smaller `fault`. The user chose a supported configuration and
may keep it; your job is to make sure they chose it knowingly. A `fault` is
something that used to work and stopped.

---

## 2. The signals, and what each one can and cannot tell you

- **`advisory` on a `recall` response** — the primary surface, and the only one
  that fires on its own. Shape:
  `{degraded, severity, reason, evidence, runbook, advisory_scope}`. The first
  time in a degraded episode it carries the full runbook; afterwards a short
  reminder. `advisory_scope` says whose "already told" state you are seeing:
  `session` when the caller declared a session key, `process` when the state is
  the whole server's and the transport is shared.
- **Running a `recall` is the liveness test — with two caveats.** It embeds the
  query through the real client, so a success clears the degraded state and
  re-arms the full runbook, and a failure feeds the fault counter. There is no
  separate probe to run. The first caveat is that it is not always read-only:
  with `CPERSONA_CONFIDENCE_ENABLED=true` a successful, non-deep recall that is
  not a gate fallback updates `recall_count` and `last_recalled_at` on the rows
  it returns. The flag ships off, so a default install is unaffected — but this
  page tells you to run the test repeatedly, and an install that turned
  confidence on is ranking on state those runs move. It writes nothing while the
  session is paused, which is what keeps the test safe during a no-persist
  window. The second caveat: a single-text embed is cached for five minutes, and a cache hit
  returns the stored vector without a request leaving the process. It no longer
  clears the degraded state — an advisory therefore survives a repeated query —
  but it cannot confirm a repair either. **Test with a query you have not just
  used**, or the answer describes the cache rather than the backend.
- **`embedded` on a `store` response** — `false` means the write landed without
  a vector. The memory is saved and searchable by keyword; the vector is
  repairable later (§6).
- **`check_health(agent_id, fix=true)`** — probes the backend and reports
  `embedding_backend_unreachable` (`warn`) with the failing call's own evidence
  when a configured backend does not answer. No finding means it answered.
- **`check_health(agent_id, fix=false)`** — makes no network call, so it cannot
  test liveness, and says so: `embedding_backend_not_probed` (`info`). A fault
  a recall already latched is still reported. Do not read a quiet `fix=false`
  run as evidence that the backend is up. A `fix=true` run reports the same
  finding when the probe hit the embed cache instead of the endpoint; its
  `reason` field distinguishes `served_from_cache` from `no_probe_on_this_run`.
- **No backend configured is deliberately not a `check_health` finding.** It is
  permanent, it was never confusable with "the server is up", and a finding
  that can never be resolved teaches an operator to skim past the check. That
  state is raised on the recall advisory instead, which is the surface a user
  reads.
- **`get_session_findings()`** — `_meta.server_version` is the version of the
  code answering the request, not of whatever distribution sits beside it. Quote
  that when a version matters.

---

## 3. HINT — no backend is configured

### Say this

- CPersona is working. Memories are being stored and recalled right now.
- No embedding backend is configured, so recall runs on FTS5 + keyword only.
- That configuration is **supported** — it is not a broken install.
- It is **not recommended for normal operation**: recall matches on shared
  words, so a memory phrased differently from the question can be missed, and
  so can older ones.
- Connecting a compatible embedding server is strongly recommended, and it is a
  one-time setup.
- CEmbedding is the reference implementation and the first recommendation; any
  server satisfying the embedding contract is equally supported.

### Then propose one of two paths

**The reference backend.** Both commands run from the same directory; the first
downloads the model once, into `./data/models`:

```bash
uvx --from "cembedding[onnx]" cembedding-download-model --model jina-v5-nano
EMBEDDING_PROVIDER=onnx_jina_v5_nano uvx --from "cembedding[onnx]" cembedding
```

That serves `http://127.0.0.1:8401/embed`. On a PATH install
(`pip install "cembedding[onnx]"`) the same two steps are
`cembedding-download-model --model jina-v5-nano` and `cembedding`; from a source
checkout, `python -m cembedding.download_model --model jina-v5-nano` and
`python -m cembedding`.

**Any conforming backend.** CPersona is embedding-server-agnostic. The server
must implement `POST /embed`, taking `{"texts": ["…"]}` and returning
`{"embeddings": [[float, …], …]}`. Three requirements are easy to miss and each
degrades ranking quietly: embeddings **must be L2-normalized** (similarity is a
raw dot product, so unnormalized vectors bias ranking by magnitude); the
contract is **role-less**, with queries and documents embedded through the same
call and no instruction prefix, which is why prompt-prefix model families
underperform behind it; and CPersona sends **at most 32 texts per request**.

### Point CPersona at it

Both variables belong in the MCP client's `env` block for the `cpersona` server
— the same block that carries the database path — and the client has to be
restarted to pick them up:

- `CPERSONA_EMBEDDING_MODE=http`
- `CPERSONA_EMBEDDING_URL=http://127.0.0.1:8401/embed`

`EMBEDDING_MODE` and `EMBEDDING_HTTP_URL` are accepted as aliases, and the
`CPERSONA_`-prefixed names win when both are set. Check for an existing pair
before editing: setting the alias while the prefixed one is already present
changes nothing, and it looks exactly like a repair that did not take.

### If the user declines

That is a legitimate answer, and `CPERSONA_DEGRADED_ADVISORY=false` records it —
it stops the report, not the degradation. Offer it only after they have heard
what the configuration costs, and say plainly that it does not make running
without a backend the recommended setup.

---

## 4. FAULT — the configured backend is not answering

### Say this

- A backend is configured and is currently unavailable.
- Only the vector layer is affected. Storing and recalling both still work; the
  fallback in force is FTS5 + keyword.
- Writes made while it is down come back `embedded: false`. They are saved, and
  their vectors are repairable once the backend returns (§6).
- Quote `advisory.evidence` verbatim. It is built from the client's own
  configuration and the exception, never from request headers, and the endpoint
  is stripped of any userinfo and query string — so it names the mode, the
  endpoint and the failure without carrying a credential.

### Work through the causes in this order

1. **Read the endpoint in the evidence.** If it is not the endpoint the user
   expects, the configuration is the fault and no amount of restarting the
   server will help.
2. **Is the backend process alive?** It may have exited, or never been started
   in this session.
3. **Is the port reachable from this host?** A backend on another machine, in a
   container, or behind a firewall answers locally and not here.
4. **Did the model ever get downloaded on this machine?** A freshly provisioned
   host — or a database copied to a new one — has the configured URL and no
   model cache behind it.
5. **Did the client's configuration change?** A port moved, or a second
   `CPERSONA_`-prefixed variable was introduced and now takes precedence.

A direct probe separates "the backend is down" from "CPersona cannot reach it",
and it is read-only:

```bash
curl -s -X POST http://127.0.0.1:8401/embed \
  -H 'Content-Type: application/json' -d '{"texts":["ping"]}'
```

An `embeddings` array back means the backend is healthy and the problem is
between it and CPersona.

### Then propose the matching repair

Restart the backend; or correct the URL and restart the MCP client; or download
the model on this machine and start it. One cause, one proposal — and say which
of the five checks led you there, so the user can disagree with the diagnosis
rather than only with the command.

---

## 5. Verify the repair — do not stop at "it started"

Run these in order. The first three prove liveness; only the fourth repairs
what the outage left behind.

1. **`curl` the endpoint** (command above) — the backend answers at all.
2. **Run a `recall` with a query you have not just used** — no `advisory` on the
   response. An embed that reaches the backend clears the degraded state, so this
   is the check that says CPersona itself is satisfied. A repeated query can be
   answered from the five-minute embed cache; that hit clears nothing, so a
   still-present advisory after one would tell you about the cache, not the
   backend.
3. **`store` a fact** — the response carries `embedded: true`.
4. **`check_health(agent_id, fix=true)`** — no `embedding_backend` finding, and
   the rows written during the outage are re-embedded. A `not_probed` finding
   with `reason: served_from_cache` means step 4 asked the cache, not the
   backend; re-run it once the entry expires.

A green step 2 with a skipped step 4 leaves the user with working recall over a
corpus that still has holes in it.

---

## 6. Backfill what was written while it was down

`check_health(agent_id, fix=true)` re-embeds rows whose embedding is NULL —
exactly the rows that came back `embedded: false`. One run re-embeds up to 500
of them, so on a long outage run it until the finding count reaches zero rather
than assuming a single pass caught everything.

---

## 7. What repair cannot reach

Swapping a **different model of the same dimension** behind the same URL is
undetectable and unrepairable by these tools. CPersona fingerprints a backend by
embedding dimension only, so nothing is flagged; the dimension check NULLs blobs
of the wrong length, so nothing is NULLed; and `fix=true` re-embeds only NULL
blobs, so nothing is re-embedded. The corpus is then scored against a model that
did not produce it, and every layer above reports healthy. The remedy is to
treat a model swap as a corpus rebuild, not as a configuration change — see the
Getting Started guide at <https://cloto-dev.github.io/CPersona/getting-started/>.
