"""Token verification for the resource-server half of OAuth (docs/OAUTH_DESIGN.md §8).

Discovery — the RFC 9728 document and the ``WWW-Authenticate`` header that points
at it — shipped first and lives in ``server.py``. This module is the other half:
turning a bearer credential minted by an external identity provider into the one
thing enforcement consumes, a ``Principal`` carrying a client identifier.

Three properties carry the weight here, and each exists because removing it was
measured to fail open rather than to fail loudly:

* **Audience validation is ours alone.** The SDK's bearer backend checks what a
  verifier returns and the expiry, and nothing else. With the audience and issuer
  checks removed from the spike's verifier, a token minted for a *different*
  resource was accepted with 200 and its identity was published downstream. The
  specification's "MUST NOT accept" is not delegated to anything; it is the code
  below.
* **The issuer must be one we advertise, and its keys must be its own.** The
  candidate key set is selected from the configured allow-list, so an unknown
  issuer is refused without any network call, and the signature is then verified
  against *that* issuer's keys — an issuer cannot mint a token that claims to
  come from another one.
* **The identity is namespaced by its issuer.** An OAuth ``client_id`` only means
  anything relative to the authorization server that issued it. Used bare it can
  collide with a statically configured client, and swapping providers would let a
  grant row written for the old provider silently authorize a same-named client
  at the new one.

Verification requires ACL mode. Without a grant table there is nothing to
provision against, so every holder of a token for this resource would reach every
tool — the opposite of the per-client provisioning this was adopted with.
``server.py`` enforces that; this module simply never sees a request in the other
mode.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger("cpersona")

#: Asymmetric algorithms only, and named explicitly. PyJWT will not accept an
#: algorithm outside this list, which is what forecloses both ``alg: none`` and
#: the HMAC confusion where a public key is replayed as a shared secret.
ALLOWED_ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384")

#: Claims a token must carry to be considered at all (RFC 9068 §2.2). ``aud`` and
#: ``iss`` are also *verified*, below; requiring them here is what turns a token
#: that simply omits one from "unchecked" into "rejected".
REQUIRED_CLAIMS = ("exp", "aud", "iss", "sub")

#: How long a fetched key set is reused before it is fetched again.
JWKS_TTL_SECONDS = 3600.0

#: Floor between two fetches of the same key set. A token naming a ``kid`` we do
#: not hold is the signal that keys rotated, and refetching is the correct
#: response — but it is also an unauthenticated caller's lever on our outbound
#: requests, so it is rate-limited rather than immediate.
JWKS_MIN_REFETCH_SECONDS = 60.0

#: Ceiling on a fetched document. A key set is a few kilobytes; anything of this
#: size is not one, and reading it into memory on an unauthenticated request is
#: not something to do politely.
MAX_DOCUMENT_BYTES = 256 * 1024

_HTTP_TIMEOUT_SECONDS = 10.0


def namespaced_client_id(issuer: str, client_id: str) -> str:
    """Build the identifier the grant table is keyed by.

    ``oauth:<issuer>:<client_id>``. The shape is part of the adopted design, not
    an implementation detail, for two reasons an operator meets in practice: the
    grant file and every denial name the issuer, so a row's provenance is
    readable; and a provider swap cannot silently hand an old row to a new
    provider's same-named client, because the identifier changed with it.
    """
    return f"oauth:{issuer}:{client_id}"


def _metadata_urls(issuer: str) -> list[str]:
    """Where an authorization server's metadata may be found, in order tried.

    RFC 8414 §3.1 inserts the well-known segment between host and path, so an
    issuer with a path does not simply get the suffix appended; OpenID Connect
    Discovery appends instead. Both spellings are tried because issuers in the
    wild publish one, the other, or both — measured on WorkOS AuthKit, whose
    ``openid-configuration`` document is missing the fields the MCP flow needs
    while ``oauth-authorization-server`` carries them. Only ``jwks_uri`` is read
    here, which both documents do carry.

    The list is deduplicated with order preserved: for a path-less issuer the
    RFC 8414 and OIDC spellings coincide, and fetching the same URL twice would
    turn one unreachable endpoint into two timeouts.
    """
    parts = urlsplit(issuer)
    path = parts.path.rstrip("/")
    origin = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    candidates = [
        f"{origin}/.well-known/oauth-authorization-server{path}",
        f"{origin}/.well-known/openid-configuration{path}",
        f"{origin}{path}/.well-known/openid-configuration",
    ]
    seen, ordered = set(), []
    for url in candidates:
        if url not in seen:
            seen.add(url)
            ordered.append(url)
    return ordered


class _IssuerKeys:
    """Cached signing keys for one issuer, and the state that bounds refetching."""

    __slots__ = ("keys", "fetched_at", "last_attempt", "jwks_uri", "lock")

    def __init__(self) -> None:
        self.keys: dict[str, object] = {}
        self.fetched_at: float = 0.0
        self.last_attempt: float = -math.inf
        self.jwks_uri: str = ""
        self.lock = asyncio.Lock()


class IdpTokenVerifier:
    """Verify a JWT an external identity provider issued *for this resource*.

    ``fetch`` is the single seam this class reaches the network through: it takes
    a URL and returns ``(status, body)``. It is a parameter so the tests drive
    real key rotation, a real outage and a real foreign-audience token without a
    server to stand up — the verification logic under test is then the shipped
    one, not a replica.
    """

    def __init__(
        self,
        issuers,
        audience: str,
        *,
        jwks_uri: str = "",
        fetch=None,
        now=time.monotonic,
        require_public_subject: bool = False,
    ):
        self._issuers = tuple(issuers)
        self._audience = audience
        # Set while per-subject partitioning is configured (docs/OAUTH_DESIGN.md
        # §12). The alias ledger keys on (issuer, subject), which identifies a
        # person only under public subject identifiers; a pairwise issuer hands
        # the same person a different ``sub`` through every client, silently
        # splitting one person's memory per client. When the issuer's metadata
        # declares it cannot mint public subjects, verification fails closed
        # for that issuer rather than partitioning wrongly.
        self._require_public_subject = require_public_subject
        # An operator-supplied key set location, for an issuer whose metadata
        # this server cannot reach. Only meaningful against a single issuer:
        # with several, one URL cannot be the right answer for all of them, and
        # guessing which it belongs to would be worse than ignoring it.
        if jwks_uri and len(self._issuers) != 1:
            logger.warning(
                "CPERSONA_OAUTH_JWKS_URI is set but %d authorization servers are "
                "configured; the override is ignored and each issuer's key set is "
                "discovered from its own metadata",
                len(self._issuers),
            )
            jwks_uri = ""
        self._jwks_uri_override = jwks_uri
        self._fetch = fetch or _http_get
        self._now = now
        self._cache: dict[str, _IssuerKeys] = {issuer: _IssuerKeys() for issuer in self._issuers}
        # -inf, not 0.0, for the reason _IssuerKeys.last_attempt carries it:
        # ``time.monotonic`` is uptime on some platforms, so a 0.0 sentinel puts
        # a freshly started process inside the cooldown and the FIRST outage —
        # the one an operator most needs to see — is the one that goes unlogged.
        # Measured: the test for it passed on a long-running machine and failed
        # on a fresh CI runner.
        self._outage_logged_at: float = -math.inf

    # -- public surface ----------------------------------------------------

    async def verify_token(self, token: str):
        """Return an ``AccessToken`` for a token we issued grants against, else None.

        Every rejection returns ``None`` rather than raising: this runs on an
        unauthenticated request, where the only correct answers are "a verified
        identity" and "401", and an exception escaping here would turn a refusal
        into a 500 that a caller can provoke at will.
        """
        import jwt

        if not token or token.count(".") != 2:
            # Not a JWS at all — the shape every other credential mode also
            # fails on. Rejected before any parsing so a static token that
            # simply did not match costs nothing here.
            return None
        try:
            header = jwt.get_unverified_header(token)
        except Exception:
            return None
        if header.get("alg") not in ALLOWED_ALGORITHMS:
            return None
        kid = header.get("kid") or ""

        issuer = self._candidate_issuer(token)
        if issuer is None:
            return None

        key = await self._signing_key(issuer, kid)
        if key is None:
            return None

        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=list(ALLOWED_ALGORITHMS),
                audience=self._audience,
                issuer=issuer,
                options={"require": list(REQUIRED_CLAIMS)},
            )
        except jwt.PyJWTError as exc:
            # Debug, not warning: a bad token is an ordinary event on a public
            # endpoint, and warning here would let anyone fill the log. The
            # reason is still recorded for an operator who turns debug on while
            # a real client is failing.
            logger.debug("OAuth token rejected: %s", exc)
            return None

        if "act" in claims:
            # RFC 8693 §4.1: ``act`` asserts the presenting party is acting on
            # behalf of the subject — the subject named is not the caller.
            # Subject identity is consumed by enforcement here (the per-subject
            # boundary keys a person's memory space on it, docs/OAUTH_DESIGN.md
            # §12), so honoring a delegation token would hand the delegate the
            # impersonated subject's alias. No deployment of this server has a
            # legitimate issuer minting these; refuse them all.
            logger.warning(
                "OAuth token from %s carries an act (delegation) claim; refusing it "
                "— subject identity is consumed by enforcement, and impersonation "
                "is not supported",
                issuer,
            )
            return None

        client_claim = claims.get("client_id") or claims.get("azp") or ""
        if not client_claim:
            # The principal is a *client* identifier by contract
            # (docs/OAUTH_DESIGN.md §9). Falling back to ``sub`` would put a
            # user identifier in that field instead, so two different kinds of
            # identity would share one namespace and a grant row's meaning would
            # depend on which claim the provider happened to send. Refusing is
            # the legible answer; the log names what is missing.
            logger.warning(
                "OAuth token from %s carries neither client_id nor azp; refusing it "
                "rather than authorizing a subject as if it were a client",
                issuer,
            )
            return None

        from mcp.server.auth.provider import AccessToken

        return AccessToken(
            token=token,
            client_id=namespaced_client_id(issuer, str(client_claim)),
            scopes=(claims.get("scope") or "").split(),
            expires_at=claims["exp"],
            resource=self._audience,
            subject=str(claims["sub"]),
            claims=claims,
        )

    # -- internals ---------------------------------------------------------

    def _candidate_issuer(self, token: str):
        """Pick which configured issuer's keys to try, or None.

        The ``iss`` claim is read here *before* any signature has been checked,
        which is only safe because of what it is used for: choosing a candidate
        from a fixed allow-list. It grants nothing. The signature is then
        verified against that issuer's own keys and ``iss`` is verified again as
        a claim, so a token naming an issuer it was not signed by fails — and an
        issuer this server never advertised is refused without a network call,
        which keeps an unauthenticated caller from steering our outbound
        requests.
        """
        import jwt

        try:
            unverified = jwt.decode(
                token,
                options={"verify_signature": False},
                algorithms=list(ALLOWED_ALGORITHMS),
            )
        except Exception:
            return None
        claimed = unverified.get("iss")
        if isinstance(claimed, str) and claimed in self._cache:
            return claimed
        return None

    async def _signing_key(self, issuer: str, kid: str):
        entry = self._cache[issuer]
        async with entry.lock:
            now = self._now()
            fresh = entry.keys and (now - entry.fetched_at) < JWKS_TTL_SECONDS
            if fresh and kid in entry.keys:
                return entry.keys[kid]
            # Either nothing is cached, the cache aged out, or the token names a
            # key we do not hold — the shape of a rotation. All three want the
            # same thing, and the cooldown is what keeps the third from being a
            # lever on our outbound traffic.
            if (now - entry.last_attempt) >= JWKS_MIN_REFETCH_SECONDS:
                entry.last_attempt = now
                await self._refresh(issuer, entry)
            if kid:
                return entry.keys.get(kid)
            # A token with no ``kid`` is unambiguous only when the issuer
            # publishes exactly one key. Guessing among several would make the
            # outcome depend on dictionary order.
            if len(entry.keys) == 1:
                return next(iter(entry.keys.values()))
            return None

    async def _refresh(self, issuer: str, entry: _IssuerKeys) -> None:
        """Fetch this issuer's key set, leaving the previous one in place on failure.

        An identity provider outage authenticates nobody — that is the cost route
        (b) was chosen with, and it is stated in docs/OAUTH_DESIGN.md §11. What
        this method owes is that the outage be *legible*: the keys already held
        keep working until they age out, and the failure is logged once per
        cooldown rather than once per request, so a log an operator is scanning
        shows the condition without being buried by it.
        """
        jwks_uri = self._jwks_uri_override or entry.jwks_uri
        if not jwks_uri:
            jwks_uri = await self._discover_jwks_uri(issuer)
            if not jwks_uri:
                return
            entry.jwks_uri = jwks_uri

        document = await self._get_json(jwks_uri, what=f"key set for {issuer}")
        if document is None:
            # Forget the location, so the next attempt re-reads the metadata.
            # An issuer that moved its keys would otherwise be unreachable for
            # the life of the process, with the failure looking like an outage.
            entry.jwks_uri = ""
            return
        try:
            import jwt

            key_set = jwt.PyJWKSet.from_dict(document)
        except Exception as exc:
            self._log_outage("key set at %s is not a usable JWKS: %s", jwks_uri, exc)
            return
        keys = {}
        for key in key_set.keys:
            # A key set may legitimately carry keys for other uses (encryption)
            # and other algorithms. Keeping only what could sign a token this
            # server accepts means a rotation that adds an encryption key does
            # not read as "the signing key disappeared".
            if getattr(key, "key_id", None) and (key.public_key_use in (None, "sig")):
                keys[key.key_id] = key.key
        if not keys:
            self._log_outage("key set at %s carries no usable signing key", jwks_uri)
            return
        entry.keys = keys
        entry.fetched_at = self._now()

    async def _discover_jwks_uri(self, issuer: str) -> str:
        for url in _metadata_urls(issuer):
            document = await self._get_json(url, what=f"metadata for {issuer}", quiet=True)
            if document is None:
                continue
            published = document.get("issuer")
            if published != issuer:
                # RFC 8414 §3.3: the issuer a client reads back must be
                # identical to the one it started from, and the response MUST
                # NOT be used otherwise. Here it is also the defence that makes
                # the fetch safe — a document that names someone else is not
                # this issuer's metadata, whatever URL answered.
                self._log_outage(
                    "metadata at %s declares issuer %r, not %r; ignoring it",
                    url,
                    published,
                    issuer,
                )
                continue
            subject_types = document.get("subject_types_supported")
            if (
                self._require_public_subject
                and isinstance(subject_types, list)
                and subject_types
                and "public" not in subject_types
            ):
                # Declared pairwise-only. Fail closed for the whole issuer, not
                # just this metadata spelling: another document that merely
                # omits the field would not make the declaration untrue. A
                # document without the field passes — RFC 8414 metadata does
                # not carry it, and refusing on absence would refuse issuers
                # that are in fact public.
                self._log_outage(
                    "issuer %s supports only pairwise subject identifiers (%r); "
                    "refusing its tokens while per-subject partitioning is "
                    "configured — a pairwise subject names a (person, client) "
                    "pair, not a person, and would split one person's memory "
                    "per client",
                    issuer,
                    subject_types,
                )
                return ""
            jwks_uri = document.get("jwks_uri") or ""
            if not isinstance(jwks_uri, str) or not jwks_uri.startswith("https://"):
                self._log_outage("metadata at %s publishes no https jwks_uri", url)
                continue
            return jwks_uri
        self._log_outage(
            "no authorization server metadata was readable for %s; tokens from it "
            "cannot be verified until it is (tried %s)",
            issuer,
            ", ".join(_metadata_urls(issuer)),
        )
        return ""

    async def _get_json(self, url: str, *, what: str, quiet: bool = False):
        try:
            status, body = await self._fetch(url)
        except Exception as exc:
            if not quiet:
                self._log_outage("could not fetch the %s from %s: %s", what, url, exc)
            return None
        if status != 200:
            if not quiet:
                self._log_outage("fetching the %s from %s returned HTTP %s", what, url, status)
            return None
        if len(body) > MAX_DOCUMENT_BYTES:
            self._log_outage("the %s at %s is larger than %d bytes", what, url, MAX_DOCUMENT_BYTES)
            return None
        try:
            document = json.loads(body)
        except ValueError as exc:
            if not quiet:
                self._log_outage("the %s at %s is not JSON: %s", what, url, exc)
            return None
        return document if isinstance(document, dict) else None

    def _log_outage(self, message: str, *args) -> None:
        """Warn about a provider-side failure at most once per cooldown.

        Per request would let an unauthenticated caller write the log; never
        would make an outage of the thing that authenticates everyone invisible.
        """
        now = self._now()
        if (now - self._outage_logged_at) < JWKS_MIN_REFETCH_SECONDS:
            return
        self._outage_logged_at = now
        logger.warning("OAuth verification degraded: " + message, *args)


async def _http_get(url: str):
    """The default fetch seam: one GET, no redirects, bounded in time.

    Redirects are not followed on purpose. The metadata fetch is guarded by the
    issuer-identity check below, but the key set fetch has no such
    self-identifying field, so a redirect there would be an unverifiable hop to
    another origin. An issuer that genuinely serves its keys from elsewhere
    publishes that location in its metadata, which is followed; an operator whose
    provider needs something else points at the final URL with
    ``CPERSONA_OAUTH_JWKS_URI``.

    bug-297: the body is read as a bounded stream rather than buffered whole.
    ``MAX_DOCUMENT_BYTES`` is checked by the caller on ``len(body)``, which decides
    what gets PARSED and trusted -- but with a buffered read the allocation that
    check exists to bound has already happened, at a size chosen by whoever answers
    at the configured issuer or JWKS URI. That is precisely the party OAuth
    verification exists to survive. Reading incrementally and stopping moves the
    bound from "what we agree to parse" to "what we agree to receive".

    Deliberately stops one byte PAST the cap rather than at it: the caller refuses
    on ``len(body) > MAX_DOCUMENT_BYTES``, so a read that stopped exactly at the cap
    would hand back a silently truncated document for that check to pass and
    ``json.loads`` to reject with a parse error -- the wrong diagnosis for an
    oversized answer. The extra byte is what keeps the existing refusal both
    reachable and correctly worded. Declared Content-Length is not consulted: it may
    be absent or untrue, and the bytes are what has to be bounded either way.
    """
    import httpx

    async with httpx.AsyncClient(
        timeout=_HTTP_TIMEOUT_SECONDS, follow_redirects=False
    ) as client:
        async with client.stream(
            "GET", url, headers={"Accept": "application/json"}
        ) as response:
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_DOCUMENT_BYTES:
                    break
            return response.status_code, bytes(body)
