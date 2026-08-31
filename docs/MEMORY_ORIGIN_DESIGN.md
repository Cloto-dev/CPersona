# Recorded Access Origin (`origin`)

Status: proposed for a 2.5.x pre-release line. Additive to the tool contract —
no tool grows an argument, and no existing response changes shape. It does add
a database column, so it is not rollback-free by the release standard's
definition and takes the pre-release ladder (`RELEASE_LIFECYCLE_STANDARD.md`
§2.1).

## 1. The problem: `agent_id` does not carry origin

A stored row answers "whose memory is this" with `agent_id`, and "who produced
the content" with `source`. Neither answers "which caller put it here", and the
two are routinely not the same. Four paths in the shipped code produce rows
whose origin is unrecoverable:

- **A shared `agent_id` by design.** More than one operator can be pointed at
  one agent namespace deliberately — that is what makes their histories legible
  to each other. Once they are, the only thing distinguishing the writers is
  `source`, which the writer declares about itself.
- **Unscoped writes.** `do_store` accepts an empty `agent_id` and writes a row
  with `agent_id = ''`. That behavior is a deliberate injected-trust seam, not
  an accident (bug-137), but the row it leaves behind names nobody.
- **`import_memories`.** Imported rows take the importing side's `agent_id` and
  the exporting side's `source` verbatim. Who performed the import, and when,
  is recorded nowhere.
- **Verified subjects that are dropped.** `_oauth_principal` fills a principal
  with the `iss` and `sub` claims it verified. Unless the client row opted into
  per-subject separation (`OAUTH_DESIGN.md` §12), nothing downstream consumes
  the subject, and it ends at the end of the request.

The information exists at the moment of the write — `acl.current_principal()`
returns a `Principal(client_id, issuer, subject)` inside every handler — and is
then discarded. This document is about keeping it.

## 2. What this is, and what it must never become

**It is a measurement, not a claim.** The value is written by the server from
what the server resolved. No tool accepts it as an argument. That single rule
is the whole reason the column is worth adding: a field a caller can write is
already `source`, and a second one would carry no more evidence than the first.

**It is not an isolation axis.** `agent_id`, `project_id` and `channel` select
whose rows a query reads. `origin` selects nothing. No recall is filtered by it,
no row becomes reachable or unreachable because of it. Filtering memory by the
identity that wrote it would make memory unreadable across the boundary it
exists to cross — the same rule `session.py` states for the declared session
key, and for the same reason.

**It is not an authorization input.** Access decisions belong to the ACL layer,
which runs before a handler is reached. A column read after the fact cannot
gate anything, and wiring it into a decision would put policy in two places.

**It is not a person's identity.** What lands in the row is the server-issued
opaque alias, never the raw `sub` claim. See §5.

## 3. Why this is not an extension of `source`

`source` is a JSON column with no schema enforcement, so a sub-key could be
added there with no migration at all. That route was measured and rejected. Two
properties of the existing field defeat it, and both are load-bearing where they
are:

**`source` is caller-written, and the write path preserves unknown keys.**
`normalize_source` exists to fold legacy shapes into the canonical contract
*without inventing anything*, because fabricating a discriminator would falsify
attribution and defeat the anonymous-source detector. It therefore has no key
whitelist anywhere. The reachable path is not the lenient branch for unknown
shapes — it is the canonical fast path taken by every current producer:

```python
# (1) Already canonical — the fast path used by every 2.5.x producer.
raw_type = source.get("type")
if isinstance(raw_type, str) and raw_type in CANONICAL_SOURCE_TYPES:
    return source, False
```

A caller sending `{"type": "Agent", "id": "...", "origin": {...}}` has its dict
returned unchanged and serialized as-is by the write seam. Because `type` is
valid, `check_health(invalid_source_type)` does not flag it either: a forged
origin would not appear anywhere as an anomaly. Closing that would mean adding a
stripping step, ahead of the preserve-verbatim contract, inside the one function
written to leave the caller's value alone.

**`source` is returned by recall.** Every scored row carries it back to the
model. An origin living there would put client identifiers, subject aliases and
session keys into the context window on every recall — paid for in tokens on the
hot path, and read by a model that has no use for them. Keeping it out would
mean stripping the sub-key on the read path.

Those two repairs are the observed/declared split and the admin-only read
surface, written in a worse place. A separate column obtains both by
construction, and the migration it costs is the one shape this schema's
migration ladder has repeatedly done before.

## 4. The seam

One helper resolves the value, called from the write path of `store` and
`archive_episode`:

- **client** — `Principal.client_id`. Under stdio and the local principal this
  is the local client constant; under a provider-issued token it is the
  issuer-namespaced identifier (`oauth:<issuer>:<client_id>`) the ACL layer
  already uses, so a row and a grant row read the same.
- **subject alias** — present only when the principal carries a verified
  subject *and* an alias has been issued for it. Never the raw `sub`.
- **transport** — whether the call arrived over stdio or the shared HTTP
  transport. This is the difference between "one process, one client" and "one
  process, everyone", and it is what tells a later reader how much the client
  field is worth.
- **declared session key** — the key the caller sent, if any.

Resolution failures are not errors. A principal that is absent, a subject with
no alias, a transport that cannot be determined: each omits its key. An empty
object is a truthful record of a call that carried no resolvable origin, and it
is also what every pre-existing row holds, so the two are deliberately
indistinguishable — see §7.

## 5. Observed and declared never share a bag

```json
{
  "observed": {
    "client": "oauth:https://auth.example/:client_abc",
    "subject_alias": "u-1a2b3c",
    "transport": "http"
  },
  "declared": { "session_key": "..." },
  "at": "2026-01-01T00:00:00+00:00"
}
```

The nesting is the point. `observed` holds what the server resolved; for the
OAuth path those fields descend from signed claims that were verified before the
handler ran, which is what entitles a reader to treat them as evidence.
`declared` holds what the caller said about itself. The declared session key is
compared, never verified — any caller can send any string, including one
belonging to another session — so it is recorded where its trust level is
legible rather than in the same object as a verified subject.

A flat blob would be smaller and would lose exactly the property the column is
for. Someone auditing a bad row a year from now reads a field; nothing else in
the row tells them which half was measured.

**The alias, not the subject.** The name a subject's memory lives under is
already an opaque server-issued alias, with the `(issuer, subject) → alias` map
in a ledger the operator can edit, precisely so that a provider migration or a
switch to pairwise identifiers is one file to repair rather than orphaned data
(`OAUTH_DESIGN.md` §12). Writing the raw `sub` into a memory row would reproduce
that orphaning per row, in a column no ledger edit can reach — and would put a
person's provider identifier into a surface that gets exported and recalled.

## 6. The read surface

Phase one exposes `origin` on the inspection tools only — the per-row admin
reads and the health surface. It is **not** added to `recall` or
`recall_with_context`.

The asymmetry is deliberate. Adding a field to a recall response later is
additive; removing one is a contract break. Starting where the cost is zero
keeps the choice open, and the operator-facing question this column answers
("which caller wrote this row") is not a question a model answers mid-recall.

For the same reason, no recall filter is proposed. `source_id` already filters
on declared attribution; an `origin`-shaped filter is a different feature with
its own decision to make, and it would sit uncomfortably close to the per-subject
boundary, which is restrictive by construction and must not acquire a second,
additive path through it.

## 7. What this cannot tell you

Stated here rather than discovered later:

- **Under stdio it is nearly constant.** The local principal carries a fixed
  client id and no subject, so a single-user local install records the same two
  fields on every row. The column earns its keep on the shared HTTP transport,
  where one process serves many callers. A deployment that never leaves stdio
  should expect no information from it.
- **It is not retroactive.** Every row written before the migration holds `{}`,
  and so does every row written afterwards by a call with no resolvable origin.
  The column cannot say which — "written before this shipped" and "written by an
  unidentifiable caller" are the same value by design, because inventing a
  distinction would mean writing a claim about rows nobody measured.
- **Import records the importer.** An imported row's origin names who performed
  the import, not who originally wrote the content — that is unknowable, and the
  exporting side's value must not be carried across as though it had been
  measured here. Anything else would make the column a forgery surface, which is
  the failure mode §3 rejected `source` for.

## 8. Schema and compatibility

`memories` and `episodes` each gain `origin TEXT NOT NULL DEFAULT '{}'`, added
by the same `ALTER TABLE ... ADD COLUMN` step every column-adding migration
before it used, and stamped as the next schema version.

Older builds tolerate the column, and this was checked rather than assumed:

- every insert names its columns explicitly, so an added column is never
  positional to an older writer;
- `SELECT *` does not appear in the package, so no reader receives an unexpected
  field;
- a database stamped newer than the running build logs a warning and continues
  (bug-138), so a downgrade degrades rather than refusing to boot.

Rolling back therefore leaves the column in place and unread. That makes the
change rollback-*tolerant*, which is not the same as rollback-free: it is still a
schema migration, and the release standard sends those through the pre-release
ladder regardless of how gentle they look.

## 9. Tests

The claims above are worth only as much as the checks that hold them:

1. A caller that sends an `origin` key inside `source` does not influence the
   stored `origin` column — the forgery path §3 measured, asserted directly.
2. A call with a resolvable principal records the client; a call with none
   records `{}`.
3. A verified subject records its alias and never the raw claim value — asserted
   by matching the alias, and separately by asserting the raw subject string is
   absent from the serialized row.
4. `recall` and `recall_with_context` responses contain no `origin` key, so §6
   is a gate and not a convention.
5. Import writes the importing caller's origin, not the value in the imported
   payload.
6. A database created before the migration opens, migrates, and serves rows
   whose `origin` is `{}`.
