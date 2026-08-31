# OAuth support: three routes, measured

**Status: adopted, and both halves are built.** Route (b) — stay a resource server, delegate
issuance to an external identity provider — was chosen, together with shipping the route-independent
half first (§7). That half serves the metadata document and points the 401 at it. The other half
now exists too: a token signed by a listed issuer and minted for exactly this resource resolves to
a principal, and anything else is refused. Everything remains off unless configured.

The question this document left open — how a provider-minted identifier acquires grants — was
settled as **per-client provisioning**, and §8 records the shape that took in the code. The
constraint §9 states — a property of the model, not a gap in it — stood unresolved for a while and
has since been answered without weakening the model: §12 records the per-subject boundary.

The rejected routes are kept rather than deleted. A design record that lists only the choice
leaves the next reader to rediscover why the other two were worse, and one of them looks cheapest
right up until the detail that disqualifies it.

Everything labelled *measured* here was produced by running something — a live discovery
document, a real authorization flow driven end to end, or a mutation applied to working code to
see whether a check was actually load-bearing. Claims taken from a vendor's documentation are
labelled as such and are weaker evidence.

## 1. What the specification asks of a resource server

Against the 2026-07-28 authorization revision, a resource server owes five things. This is the
whole list; it is shorter than it looks from the outside.

1. Implement RFC 9728 protected resource metadata, with at least one entry in
   `authorization_servers`.
2. Make that metadata discoverable, by either route: a `resource_metadata` parameter on the
   `WWW-Authenticate` header of a 401, or the well-known path.
3. Validate the token per OAuth 2.1 §5.2, **including the audience**. A token minted for a
   different resource must not be accepted and must not be forwarded. Invalid or expired means
   401.
4. 401 when authentication is required, 403 when the scope is insufficient, 400 when the request
   is malformed. A 403 should carry `error="insufficient_scope"` along with `scope` and
   `resource_metadata`.
5. Advertise `scope` on the `WWW-Authenticate` header. Do not advertise `offline_access`.

Two obligations that read like ours in the revision's prose are the *client's*: keying stored
credentials by issuing authorization server, and specifying `application_type` during dynamic
registration. Neither lands here.

Dynamic Client Registration (RFC 7591) is deprecated in this revision, superseded by Client ID
Metadata Documents. It survives only for compatibility with authorization servers that predate
CIMD. A new implementation should not be designed around it — but see the next section, because
what the client *prefers* and what it *can do* are different questions, and we measured both.

## 2. What the client actually does

Specification revisions describe what is permitted. They do not tell you which of the permitted
behaviours the client in front of you will choose. We stood up a server that advertised both
registration eras at once and connected the Claude web client to it.

**With both eras advertised, it chose CIMD and never touched registration.** The observed
sequence was a 401 carrying `resource_metadata`, then the protected-resource document, then the
authorization-server document, then `/authorize`. `POST /register` was never called. On the
authorization request:

- `client_id` was an **https URL** — this is what makes it CIMD rather than a registered client.
- PKCE with `S256`, plus `state`.
- **`resource` carried our canonical URI.** The client really does send RFC 8707 resource
  indicators, so audience binding is available to us rather than theoretical.
- **`scope` was exactly the value our 401 had advertised.** Scope design is therefore ours to
  set; the client adopts what the resource server asks for.
- The client's published metadata document declares `token_endpoint_auth_method: "none"` — a
  **public client**, no secret.

**With CIMD not advertised, the same client falls back to dynamic registration.** We repeated the
run with only the older era advertised and `POST /register` arrived — twice. The registration body
asked for `token_endpoint_auth_method: "client_secret_post"`, i.e. a **confidential client**.

Two consequences worth stating plainly, because they are easy to get backwards:

- The client's preference for CIMD is a *preference*, not a *capability limit*. An authorization
  server that advertises only the older era still works.
- **The same client presents a different character depending on which era it discovers** — public
  under CIMD, confidential under registration. An authorization server implementation has to
  handle whichever one it advertises, and must tolerate a repeated registration.

## 3. The three routes

**(a) Become the authorization server.** We serve discovery, authorization, token, and the
user-facing consent leg ourselves.

**(b) Stay a resource server; delegate to an external identity provider.** We verify tokens and
nothing else. The provider handles registration, sign-in, consent, issuance and refresh.

**(c) Put an access proxy in front.** A zero-trust proxy terminates authorization at the edge and
the server behind it is reached only after the proxy is satisfied.

## 4. Comparison

The implementation figures come from building both (a) and (b) against the SDK and running them,
not from reading the SDK.

| | (a) authorization server | (b) external provider | (c) access proxy |
| --- | --- | --- | --- |
| Our code, minimal working version | **114 lines** | **65 lines** | none |
| Protocol methods to implement | **9** | **1** | none |
| Record types we must persist | **4** (40 fields) | **0** | none |
| User-facing sign-in and consent | ours to build | provider's | proxy's |
| New runtime dependency | none | **none** | none |
| Operational surface we own | discovery, issuance, refresh, revocation, consent, key material | token verification | proxy configuration |
| Reachable without an external account | yes | no | no |
| Compatible with the other routes | — | — | **mutually exclusive with (a) and (b)** |

**The 114 is a floor, not an estimate.** That version stores state in dictionaries, auto-approves
consent, and mints random strings. A shippable one adds durable storage for four record types, a
consent page with a session behind it, and the whole of CIMD. The 65 is close to complete: what is
missing from it is the choice of provider, not more code. The real asymmetry is much larger than
the ratio of those two numbers.

For comparison, the SDK's own server-side authorization package is about 1,650 lines. Route (a)
is not "write 114 lines"; it is "own the part of that problem the SDK left out", and §5 is the
list.

## 5. What the SDK does, and what it leaves

Only behaviours we provoked and observed are listed as covered. A check nobody triggered is not
evidence of a check.

**Covered** — PKCE verification, redirect URI matching against the registered set, scope
validation against the configured set, client authentication, rejection of the `plain` challenge
method, single-use authorization codes, refresh token rotation.

**Left to us, on any route that issues or verifies tokens:**

- **Audience validation.** The bearer backend checks the verifier's answer and the expiry, and
  nothing else. We removed the audience and issuer checks from our own verifier and re-ran: a
  token minted for a **different resource was accepted with 200**, and the identity was published
  downstream. The specification's "must not accept" is entirely ours to honour. It is honoured in
  `cpersona/oauth.py`, and the same mutation was applied again to the shipped code: with the
  audience comparison disabled, two tests fail and no other. A check whose removal breaks nothing
  is not a check.
- **`scope` on the 401.** The middleware emits `error`, `error_description` and
  `resource_metadata` — no `scope`. The client adopts whatever scope the 401 advertises (§2),
  which makes the parameter a loaded lever: a live connection attempt (2026-08-31) died at the
  issuer with `invalid_scope` because the advertised value named scopes the issuer does not
  define — the user never reached a sign-in page. The default therefore advertises none; a
  server with no scope design has nothing true to say here.

**Left to us, additionally, on route (a):**

- **CIMD, in full.** The SDK's support for client ID metadata documents is entirely client-side:
  the field appears in the shared model and in the client package, and nowhere under the server
  package. As a control, `client_id` appears in seven files there, so the search reaches — the
  absence is real. The consequence is concrete and easy to miss: **an authorization server built
  on the stock SDK does not advertise CIMD, so the client falls back to the deprecated
  registration path.** Getting the non-deprecated path means writing the advertisement, the
  metadata document fetch, the client-id-equals-URL check, redirect URI validation, structural
  validation, caching, and request-forgery care.
- **The issuer parameter (RFC 9207).** Not emitted, and the authorization server metadata does
  not advertise `authorization_response_iss_parameter_supported`. Both are ours to add.
- **Consent.** There is none. `authorize()` is a contract that says "return a URL to redirect
  to"; the page a human sees, the sign-in behind it, the record of the decision and the handler
  that receives the return are all past that return value. The SDK's own documentation says
  implementations will need to define another handler.
- **Durable storage** for authorization codes, access tokens, refresh tokens and clients.

## 6. Route (c), and why "no server changes" is not quite true

We built a disposable zero-trust deployment and connected the client to it. It works: the
unauthenticated request is refused at the edge with a 401 carrying `resource_metadata`, the
metadata resolves, dynamic registration returns 201, `/authorize` redirects into the proxy's
sign-in, and the consent screen renders correctly. Nothing reaches the origin until the proxy is
satisfied.

Three things qualify it:

1. The proxy hands the client an **opaque token**. A server that authenticates by comparing a
   static bearer will reject it. Either the static credential comes off, or the server learns to
   accept the proxy's assertion — so this route still touches the server, just less.
2. The vendor documents the exclusion directly: a deployment that relies on its own OAuth and its
   own `WWW-Authenticate` must not enable the managed variant. **Route (c) cannot coexist with
   (a) or (b)**, which makes it a commitment rather than a stepping stone.
3. The consent nonce we observed lives about five minutes. Paired with a slow sign-in factor —
   an emailed code, say — a first connection can expire before the person finishes. That is a
   direct hit on the reach this whole effort exists to buy.

## 7. The part that does not depend on the choice

The resource-server half — RFC 9728 metadata, and a 401 that carries `resource_metadata` and
`scope` — is required by **all three** routes. It is additive: it changes no existing caller's
behaviour, because today every request without a valid credential is already refused.

It also fixes a symptom we can already explain. Before this half shipped the server implemented
neither discovery mechanism, so a specification-conformant client found nothing and fell through
to the last step of its registration ladder: asking the human to type in a client id. That screen
is not a rejection of our credentials and not a client defect. It is the correct behaviour for a
client that was given nothing to discover.

This half can ship before the route is chosen, and it makes the failure legible either way.

**How it is turned on.** Three environment variables, all unset by default — with none of them
set the responses are byte-identical to before, which is what makes shipping ahead of the route
choice safe:

| Variable | What it does |
|----------|--------------|
| `CPERSONA_OAUTH_RESOURCE` | The canonical resource identifier this server publishes and expects back. Discovery stays off while it is empty |
| `CPERSONA_OAUTH_AUTHORIZATION_SERVERS` | Whitespace- or comma-separated issuer URLs. Discovery stays off while none is listed |
| `CPERSONA_OAUTH_SCOPES` | The scope advertised on the 401 (default empty — advertise only scopes the issuer defines; one it does not define ends every authorization at `invalid_scope`) |
| `CPERSONA_OAUTH_JWKS_URI` | Where the issuer's signing keys are, for a provider whose metadata this server cannot read. Normally discovered; ignored with more than one issuer |

The first two enable verification as well, because a door that opens for nobody is the same failure
as a door nobody can find. Verification additionally requires ACL mode — §8 says why, and what the
server does when it is missing.

Defaults and the surrounding settings: [Configuration](configuration.md). Naming them here
matters more than it looks — an operator hitting exactly the failure above reads this section to
find out what to do about it, and a feature whose switch is unwritten is a feature that shipped
without shipping.

## 8. Additive design, and one place where "additive" stops being true

Authorization here consumes exactly one thing: a principal carrying a client identifier. The
enforcement layer never reads a header — verified by reading every reference, and then by driving
it. So a new identity provider is a **new producer of that principal**, and nothing downstream
changes.

We built the composed resolver and ran it: about **15 lines**, trying the existing credential
table first, then the static credential, then the token verifier. With OAuth enabled, callers
using either existing credential resolve exactly as before; an OAuth token resolves to its client;
a token for a foreign audience and an unknown credential are both refused. The ordering is chosen
for cost and attack surface rather than correctness — the local comparisons are cheap and cannot
be fooled by a remote token, and the token parse is the only step that reads attacker-controlled
input, so it goes last.

**Where it stops.** The identity seam is drop-in; the grant table is not. An identifier the grant
table has never seen resolves to an empty grant set, so it authenticates and is then refused every
scoped tool. We drove the real guard to confirm it rather than inferring it from the lookup. This
is correct fail-closed behaviour, but it means enabling OAuth is not purely additive in operation:
someone has to decide how a provider-minted client identifier acquires grants — a default, a
mapping rule, or per-caller provisioning. **That decision belongs to whichever route is chosen and
should not be discovered during implementation.**

**It was decided: per-caller provisioning.** A default grant would hand capability to whoever
reaches the issuer, and an issuer-wide rule would make "has an account there" mean "may read this
memory" — both put the authorization decision somewhere nobody wrote it down. So an operator adds a
row, and three consequences follow that are worth stating rather than discovering:

- **The identifier is namespaced by its issuer**, as `oauth:<issuer>:<client_id>`. A bare client id
  only means something relative to the authorization server that minted it: used plain it can
  collide with a statically configured client, and after a provider swap a row written for the old
  provider would silently authorize a same-named client at the new one. The namespace also puts the
  provenance of every row and every denial on the page an operator is reading.
- **The row carries no credential.** `"token": null` declares a principal a resolver asserts rather
  than one a caller presents (docs/ACL_DESIGN.md). Writing a placeholder string instead would create
  a live static bearer nobody intended — measured, and the reason the null form exists.
- **Verification refuses to run without a grant table.** With no ACL file there is nothing to
  provision against, so a verified token would authenticate with no enforcement behind it and reach
  every tool. Rather than degrade quietly the server logs that verification is off and keeps serving
  discovery: a client still finds the issuer and is then refused, which is a true answer.

The identity a token resolves to comes from its `client_id` claim, or `azp` where the provider sends
that instead. A token carrying neither is **refused rather than falling back to `sub`**: the
principal is a client identifier by contract (§9), and putting a subject in that field would let two
different kinds of identity share one namespace, so a grant row's meaning would depend on which
claim the provider happened to send.

## 9. A constraint that outlives the route choice

The principal carries a client identifier and nothing else.

Under CIMD the client id is a fixed URL, and under registration it is one value per registration.
Either way, **every end user of that client collapses into a single identifier.** The token does
carry a subject, and the verifier does surface it — the seam drops it.

Per-client authorization therefore cannot separate end users of a multi-tenant client. That is
worth stating in this document rather than in an implementation ticket, because the motivation for
adding OAuth at all is reach: many people, most of whom will arrive through exactly one such
client. Reach and per-client authorization are in tension, and the tension is structural rather
than a gap in the current implementation.

When this section was first written it proposed not resolving the tension, only not being
surprised by it. It has since been resolved: §12 describes the per-subject boundary, which keeps
the constraint above intact — the principal's *client* identity is still a client identifier and
nothing else — and separates people with a second verified field beside it rather than by mixing
two kinds of identity into one namespace.

## 10. Out of scope

- ~~Separating end users of the same client from one another (§9).~~ Since resolved — §12.
- Binding accounts to the memory agent identity.
- Migrating existing callers off their current credentials. They keep working; that is the point
  of §8.
- **Enforcing the token's `scope`.** §1 lists a 403 carrying `insufficient_scope` among a resource
  server's obligations, and this implementation does not emit one. Authorization is the grant
  table's job; reading the scope as a second, independent permission model would give one question
  two answers with no rule for which wins when they disagree. The scope on the 401 remains the
  lever §2 measured — it is what tells the client what to ask the issuer for — and the token's
  scopes are carried on the verified identity, so the second model can be added later without
  changing what is verified.
- Any provider selection. The candidate comparison is a separate exercise, and its findings are
  documentation-level until one candidate is exercised with a real token against the verifier
  described in §8.

## 11. The decision, and the question it left open

**Route (b).** It implements one protocol method instead of nine, persists nothing, builds no
consent surface, and adds no dependency. The cost is an external service in the authentication
path, which is a real cost: an outage there authenticates nobody.

Route (a) was rejected on the shape of what the SDK leaves rather than on the 114 lines. Owning an
authorization server here means owning CIMD outright, and the stock SDK quietly delivers the
deprecated registration path instead (§5) — so the honest version of route (a) is larger than the
version anyone estimates from the method count.

Route (c) was rejected on exclusivity rather than on cost. It is the least code, but the vendor
documents that it cannot coexist with a deployment serving its own OAuth (§6), so choosing it
closes the door on the other two. A route that cannot be undone is a poor first move in a design
this young.

**The route-independent half ships first** (§7). It is additive, it is required by whichever route
had won, and it replaces a failure nobody can read with one that explains itself. It has since
shipped; §7 names the three variables that enable it.

**Settled: a provider-minted client identifier acquires grants by per-caller provisioning** (§8).
A default or an issuer-wide rule would place the authorization decision where nobody wrote it. The
cost is the one this section warned about and does not remove: an identifier with no grants
authenticates successfully and is then refused everything, which is correct and looks exactly like a
bug. What was added is the sentence that tells them apart — a denial for a client the grant table
has never seen now says so, rather than reading identically to a permission that was deliberately
withheld.

## 12. Per-subject separation: the boundary, the ledger, and `@me`

§9 named the constraint: every end user of a multi-tenant client collapses into one client
identifier, so one grant row means "everyone who can sign in to that tenant shares one memory".
That is a quiet form of over-grant — no symptom, correct-looking behaviour, and exactly the wrong
default for the deployment OAuth was added for. This section is the resolution. Three decisions
carry it, and each was chosen against a concrete alternative.

**The principal is structured, not renamed.** `Principal` now carries `(client_id, issuer,
subject)`. The subject is the verified `sub` claim — the one identifier that is stable for a
person, and only as the pair `(issuer, subject)` (OIDC Core §5.7). The alternative was to fold the
subject into the agent namespace (`user-<sub>` as an `agent_id`), and it was rejected for the same
reason §8 refuses to fall back from `client_id` to `sub`: two kinds of identity in one namespace
make every row's meaning depend on which kind happened to arrive. Static resolvers leave both new
fields empty — nothing vouched for a person there, so nothing claims one.

**Separation is a restrictive boundary, not an additive grant.** A client row may declare
`"per_subject": true`. From that client, each signed-in subject reaches **only its own alias
space**; every other agent name is refused *before* the grant table is consulted, so an explicit
deny beats every allow, the `"*"` wildcard included. The alternative — modelling subjects as extra
grant rows — fails open twice: an operator who writes `"*": "read-write"` for convenience has
silently granted cross-subject reach, and a subject nobody wrote a row for is indistinguishable
from a configuration gap. A boundary that subtracts cannot be widened by generosity elsewhere in
the file. The flag is only accepted on a resolver-asserted row (`"token": null`): a static token
authenticates a client, not a person, so the flag there would be policy that can never apply, and
the loader refuses it at startup rather than letting it sit inert.

**Subjects get opaque aliases, issued by this server.** The name a subject's memory lives under is
not the raw `sub` but a short opaque alias (`u-` + hex), and the `(issuer, subject) → alias` map is
persisted in the **alias ledger** — a server-writable JSON file beside the database
(`CPERSONA_ALIAS_LEDGER_FILE` to relocate). Baking the raw subject into `agent_id` was rejected
because every provider event that re-issues subjects — a migration, a custom-domain move, a switch
to pairwise identifiers — would orphan the data keyed by the old values with no seam to repair it.
With the ledger, each of those events is an edit to one file: pointing two `(issuer, subject)` rows
at one alias is manual account linking, and it is the operator's escape hatch. The ledger is
deliberately a different file from the grant table, with different writers: `acl.json` is
operator-written policy the server never touches; the ledger is server-written state the operator
may edit.

**Issuance is automatic.** The first call a new subject scopes to itself mints an alias, records it
durably, logs it, and reports it (`alias_issued: true` in the response). The invitation-shaped
alternative — require an operator to pre-create each subject — was rejected because it reproduces,
per person, exactly the failure §8 documented for clients: authenticate successfully, then be
refused everything, which is correct and looks exactly like a bug. The written `per_subject` flag
is still the opt-in (no row, no partitioning — the §8 philosophy intact), and the *entry* gate
belongs where accounts are managed: the identity provider's tenant settings decide who can sign in
at all. A mint that cannot be made durable refuses the request instead of proceeding — an alias
that authorized a write but evaporates on restart would strand that write behind a fresh alias.

**`@me` names the caller's own space.** A subject cannot know its alias before the server has
issued one, so the sentinel `@me` (same idiom as the existing `@auto`) resolves to it. The
evaluation order is fixed: **resolve, then ACL, then query** — the sentinel is rewritten before any
demand is computed, so no grant is ever evaluated against the literal, and the response echoes
`resolved_agent_id` (the alias, never the raw subject) so what was reached is visible and
addressable. Four refusals keep the sentinel from widening anything:

- A client without the boundary — the stdio principal, every static-token client, an OAuth client
  whose row lacks the flag — is refused `@me` outright. Falling back to the client identity would
  let the sentinel quietly mean "my client", the namespace mixing this design exists to avoid.
- A token carrying an `act` claim (RFC 8693 delegation) is refused at verification: the subject it
  names is not the caller, and honoring it would hand the delegate the impersonated subject's
  alias.
- At startup with `per_subject` configured, a database already using the reserved names — the
  literal `@me`, or any agent under the `u-` prefix — refuses to serve: a stored `@me` row could
  never be addressed again, and a pre-existing `u-` agent is indistinguishable from an issued
  alias. Deployments that never opt in keep every name they have.
- An issuer whose metadata declares only pairwise subject identifiers is refused while
  `per_subject` is configured: a pairwise `sub` names a (person, client) pair, not a person, so the
  ledger key would silently split one person's memory per client. Absence of the field is not a
  declaration — RFC 8414 metadata does not carry it — and `CPERSONA_OAUTH_JWKS_URI` bypasses
  metadata entirely, so setting both earns a startup warning that the check cannot run.

**What this deliberately does not do.** No new database axis: the boundary is enforced in one
place (the ACL guard) and the alias is an ordinary `agent_id` everywhere below it, so the memory
schema, the migration story and every query stay untouched. Routing subjects to separate databases,
an owner/tenant column, and an identity-link table were all considered and deferred: each buys
isolation this boundary already provides, at the cost of touching the storage layer — the wrong
trade for the current line, and re-evaluatable when the schema is next open anyway. Enabling
partitioning for one client changes nothing for any other row in the file; a deployment with no
`per_subject` row runs byte-for-byte as before.
