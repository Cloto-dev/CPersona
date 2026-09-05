"""The verifying half of OAuth: a provider-issued token becomes a principal.

docs/OAUTH_DESIGN.md §8. Discovery — covered in tests/test_oauth_discovery.py —
tells a client where to get a token. Everything here is about what happens when
one arrives.

One property carries most of the weight, and it is the one the SDK does not
provide: **a token minted for a different resource must be refused.** It is a
well-formed, unexpired, correctly-signed token, so it passes every check made
anywhere else in the stack; the audience comparison in ``cpersona/oauth.py`` is
the only thing standing between it and an authenticated session. Deleting that
comparison was measured to make a foreign token succeed, so the test below is
written to fail when it is gone rather than to describe it.

Two structural properties get the same treatment:

* **Verification is refused without a grant table.** A verified identity with no
  enforcement behind it reaches every tool, so the wiring test here asserts the
  absence of a verifier rather than trusting the paragraph that says so.
* **Existing callers keep authenticating unchanged.** "Additive" is a claim about
  the callers who never asked for OAuth, so they are driven through the same
  assembled app with the feature on.

Tokens are minted with a real RSA key and verified through the shipped verifier;
only the network is replaced, by a fetch seam that serves the documents an
authorization server would.
"""

import contextlib
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from cpersona import acl, config, oauth, server

ISSUER = "https://auth.example.com"
OTHER_ISSUER = "https://elsewhere.example.com"
RESOURCE = "https://memory.example.com/mcp"
METADATA_URL = f"{ISSUER}/.well-known/oauth-authorization-server"
JWKS_URL = f"{ISSUER}/oauth2/jwks"
CLIENT = "https://claude.ai/mcp-client"
NAMESPACED = f"oauth:{ISSUER}:{CLIENT}"


# ---------------------------------------------------------------------------
# A stand-in authorization server: real keys, real signatures, no sockets
# ---------------------------------------------------------------------------


class FakeIdp:
    """Serves metadata and a key set, and mints tokens signed by them.

    ``requests`` records every URL fetched, so "no network call was made" is an
    observation rather than an assumption — the test that an unknown issuer is
    refused without reaching out depends on it.
    """

    def __init__(self, issuer=ISSUER, kid="k1"):
        self.issuer = issuer
        self.kid = kid
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.requests: list[str] = []
        self.metadata_status = 200
        self.jwks_status = 200
        self.metadata_issuer = issuer
        self.jwks_uri = f"{issuer}/oauth2/jwks"
        # Extra fields merged into the metadata document — how a test declares
        # e.g. subject_types_supported without a second fake.
        self.metadata_extra: dict = {}

    # -- the fetch seam ----------------------------------------------------

    async def fetch(self, url: str):
        self.requests.append(url)
        if url == f"{self.issuer}/.well-known/oauth-authorization-server":
            if self.metadata_status != 200:
                return self.metadata_status, b""
            body = json.dumps(
                {
                    "issuer": self.metadata_issuer,
                    "jwks_uri": self.jwks_uri,
                    **self.metadata_extra,
                }
            ).encode()
            return 200, body
        if url == self.jwks_uri:
            if self.jwks_status != 200:
                return self.jwks_status, b""
            return 200, json.dumps(self.jwks()).encode()
        return 404, b""

    # -- key material ------------------------------------------------------

    def jwks(self):
        numbers = self._key.public_key().public_numbers()
        import base64

        def b64(value: int) -> str:
            raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

        return {
            "keys": [
                {
                    "kty": "RSA",
                    "use": "sig",
                    "alg": "RS256",
                    "kid": self.kid,
                    "n": b64(numbers.n),
                    "e": b64(numbers.e),
                }
            ]
        }

    def rotate(self, kid="k2"):
        """Replace the signing key, as a provider does on rotation."""
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = kid

    # -- minting -----------------------------------------------------------

    def mint(self, *, aud=RESOURCE, iss=None, client_id=CLIENT, sub="user-1", ttl=300, **extra):
        now = int(time.time())
        claims = {
            "sub": sub,
            "aud": aud,
            "iss": self.issuer if iss is None else iss,
            "exp": now + ttl,
            "iat": now,
            "scope": "cpersona:read cpersona:write",
        }
        if client_id is not None:
            claims["client_id"] = client_id
        claims.update(extra)
        for key in [k for k, v in claims.items() if v is None]:
            del claims[key]
        return jwt.encode(claims, self._key, algorithm="RS256", headers={"kid": self.kid})


@pytest.fixture
def idp():
    return FakeIdp()


@pytest.fixture
def oauth_on(monkeypatch):
    monkeypatch.setattr(config, "OAUTH_RESOURCE", RESOURCE)
    monkeypatch.setattr(config, "OAUTH_AUTHORIZATION_SERVERS", ISSUER)
    monkeypatch.setattr(config, "OAUTH_SCOPES", "cpersona:read cpersona:write")
    monkeypatch.setattr(config, "OAUTH_JWKS_URI", "")


@pytest.fixture(autouse=True)
def _deactivate_acl():
    yield
    acl.activate(None)


def _verifier(idp, **kwargs):
    kwargs.setdefault("fetch", idp.fetch)
    return oauth.IdpTokenVerifier(kwargs.pop("issuers", (ISSUER,)), RESOURCE, **kwargs)


# ---------------------------------------------------------------------------
# The whole assembled app, so the wiring is exercised and not only the class
# ---------------------------------------------------------------------------


def _acl_file(tmp_path, clients):
    path = tmp_path / "acl.json"
    path.write_text(json.dumps({"clients": clients}), encoding="utf-8")
    path.chmod(0o600)
    return acl.load_config(str(path))


def _make_app(acl_config=None, auth_token="s3cret"):
    reached = []

    async def endpoint(scope, receive, send):
        reached.append(scope["path"])
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"mcp"})

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        yield

    app = server._build_http_app(auth_token, endpoint, lifespan, acl_config=acl_config)
    return app, reached


async def _request(app, *, token="", path="/mcp", method="POST"):
    messages = []
    headers = [(b"authorization", f"Bearer {token}".encode())] if token else []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 51234),
        "server": ("127.0.0.1", 8402),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    start = next(m for m in messages if m["type"] == "http.response.start")
    return start["status"]


# ---------------------------------------------------------------------------
# 1. The audience check — the one the SDK leaves entirely to us
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_token_for_this_resource_is_accepted(idp):
    verified = await _verifier(idp).verify_token(idp.mint())
    assert verified is not None
    assert verified.client_id == NAMESPACED
    assert verified.subject == "user-1"


@pytest.mark.asyncio
async def test_a_delegation_token_is_refused(idp):
    """RFC 8693 ``act``: the subject named is not the caller (§12).

    Signed by the right issuer, minted for this resource, unexpired — every
    other check passes. Subject identity is consumed by enforcement (the
    per-subject boundary keys a memory space on it), so honoring this token
    would hand the delegate the impersonated subject's alias. Removing the
    ``act`` refusal in cpersona/oauth.py turns this red.
    """
    delegated = idp.mint(act={"sub": "admin-console"})
    assert await _verifier(idp).verify_token(delegated) is None


@pytest.mark.asyncio
async def test_a_pairwise_only_issuer_is_refused_when_subjects_partition(idp):
    """subject_types_supported without "public" fails closed (§12).

    A pairwise subject names a (person, client) pair, not a person, so the
    (issuer, subject) alias key would split one person's memory per client.
    The refusal is issuer-wide: no key is discovered, so no token verifies.
    """
    idp.metadata_extra = {"subject_types_supported": ["pairwise"]}
    verifier = _verifier(idp, require_public_subject=True)
    assert await verifier.verify_token(idp.mint()) is None


@pytest.mark.asyncio
async def test_pairwise_metadata_is_ignored_without_subject_partitioning(idp):
    """The check is gated on per-subject configuration — additive otherwise."""
    idp.metadata_extra = {"subject_types_supported": ["pairwise"]}
    assert await _verifier(idp).verify_token(idp.mint()) is not None


@pytest.mark.asyncio
async def test_metadata_without_subject_types_is_not_refused(idp):
    """Absence is not a pairwise declaration: RFC 8414 metadata lacks the field."""
    verified = await _verifier(idp, require_public_subject=True).verify_token(idp.mint())
    assert verified is not None


@pytest.mark.asyncio
async def test_a_token_for_another_resource_is_refused(idp):
    """The check nothing else in the stack makes.

    This token is signed by the right issuer with a current key and has not
    expired; every check the SDK performs on its own passes. Removing the
    ``audience=`` argument from the decode call in cpersona/oauth.py makes it
    succeed — measured, which is why this test exists in this shape.
    """
    foreign = idp.mint(aud="https://someone-else.example/mcp")
    assert await _verifier(idp).verify_token(foreign) is None


@pytest.mark.asyncio
async def test_a_token_with_no_audience_at_all_is_refused(idp):
    """``require`` is what makes the audience check unskippable.

    A token that simply omits ``aud`` has nothing to compare, so a decoder that
    only compares when the claim is present would accept it.
    """
    assert await _verifier(idp).verify_token(idp.mint(aud=None)) is None


# ---------------------------------------------------------------------------
# 2. The issuer must be one we advertise, and its keys must be its own
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unconfigured_issuer_is_refused_without_any_fetch(idp):
    """Refused from the allow-list, before the network.

    Two things at once: a token from an issuer this server never advertised is
    not accepted, and deciding that costs no outbound request — otherwise an
    unauthenticated caller could aim our fetches by writing an ``iss`` claim.
    """
    stranger = FakeIdp(issuer=OTHER_ISSUER)
    verifier = _verifier(idp)
    assert await verifier.verify_token(stranger.mint(iss=OTHER_ISSUER)) is None
    assert idp.requests == []


@pytest.mark.asyncio
async def test_a_token_signed_by_someone_else_is_refused(idp):
    """Claiming our issuer is not the same as being signed by it."""
    impostor = FakeIdp(issuer=ISSUER)
    assert await _verifier(idp).verify_token(impostor.mint()) is None


@pytest.mark.asyncio
async def test_metadata_that_names_a_different_issuer_is_ignored(idp):
    """RFC 8414 §3.3, and the reason the metadata fetch is safe.

    A document that answers at the issuer's URL but declares someone else is not
    that issuer's metadata, so the key set it points at is not trusted.
    """
    idp.metadata_issuer = "https://attacker.example"
    assert await _verifier(idp).verify_token(idp.mint()) is None


# ---------------------------------------------------------------------------
# 3. The rest of the token's validity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_expired_token_is_refused(idp):
    assert await _verifier(idp).verify_token(idp.mint(ttl=-60)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["sub", "exp", "iss", "aud"])
async def test_a_token_missing_a_required_claim_is_refused(idp, missing):
    """Each required claim, removed one at a time from an otherwise valid token.

    ``iss`` is refused earliest — with no issuer there is no allow-list entry to
    select, so the token never reaches a key. The others are refused by the
    ``require`` option, which is what stops a decoder from silently skipping a
    check whose input is absent.
    """
    claims = {
        "sub": "user-1",
        "aud": RESOURCE,
        "iss": ISSUER,
        "exp": int(time.time()) + 300,
        "client_id": CLIENT,
    }
    del claims[missing]
    token = jwt.encode(claims, idp._key, algorithm="RS256", headers={"kid": idp.kid})
    assert await _verifier(idp).verify_token(token) is None


@pytest.mark.asyncio
async def test_an_unsigned_token_is_refused(idp):
    """``alg: none``: rejected because the algorithm list is explicit."""
    unsigned = jwt.encode(
        {"sub": "u", "aud": RESOURCE, "iss": ISSUER, "exp": int(time.time()) + 300},
        key="",
        algorithm="none",
    )
    assert await _verifier(idp).verify_token(unsigned) is None


@pytest.mark.asyncio
async def test_a_symmetric_token_is_refused(idp):
    """The public key replayed as an HMAC secret — refused on the algorithm."""
    hmac_token = jwt.encode(
        {"sub": "u", "aud": RESOURCE, "iss": ISSUER, "exp": int(time.time()) + 300},
        key="a-secret-long-enough-to-not-warn-0123456789",
        algorithm="HS256",
        headers={"kid": idp.kid},
    )
    assert await _verifier(idp).verify_token(hmac_token) is None


@pytest.mark.asyncio
async def test_a_token_with_no_client_claim_is_refused(idp, caplog):
    """A subject is not a client (docs/OAUTH_DESIGN.md §9).

    Falling back to ``sub`` would put a user identifier in a field the grant
    table reads as a client identifier, so two kinds of identity would share one
    namespace. Refusing keeps them apart, and says so.
    """
    with caplog.at_level("WARNING", logger="cpersona"):
        assert await _verifier(idp).verify_token(idp.mint(client_id=None)) is None
    assert any("client_id" in r.message or "client_id" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_azp_is_accepted_when_client_id_is_absent(idp):
    verified = await _verifier(idp).verify_token(idp.mint(client_id=None, azp="app-7"))
    assert verified is not None
    assert verified.client_id == f"oauth:{ISSUER}:app-7"


@pytest.mark.asyncio
@pytest.mark.parametrize("garbage", ["", "not-a-jwt", "a.b", "a.b.c", "....", "Bearer x"])
async def test_a_credential_that_is_not_a_jwt_is_refused(idp, garbage):
    assert await _verifier(idp).verify_token(garbage) is None


# ---------------------------------------------------------------------------
# 4. Namespacing: an identifier only means something relative to its issuer
# ---------------------------------------------------------------------------


def test_the_identifier_carries_its_issuer():
    assert oauth.namespaced_client_id(ISSUER, CLIENT) == f"oauth:{ISSUER}:{CLIENT}"


@pytest.mark.asyncio
async def test_the_same_client_id_at_two_issuers_is_two_identities(idp):
    """Why the namespace exists.

    Both providers issue a token for a client called ``shared``. If the grant
    table were keyed by the bare identifier, provisioning one would provision
    the other.
    """
    second = FakeIdp(issuer=OTHER_ISSUER)

    async def fetch(url):
        for source in (idp, second):
            status, body = await source.fetch(url)
            if status == 200:
                return status, body
        return 404, b""

    verifier = oauth.IdpTokenVerifier((ISSUER, OTHER_ISSUER), RESOURCE, fetch=fetch)
    first_id = (await verifier.verify_token(idp.mint(client_id="shared"))).client_id
    second_id = (await verifier.verify_token(second.mint(client_id="shared"))).client_id
    assert first_id != second_id
    assert first_id == f"oauth:{ISSUER}:shared"
    assert second_id == f"oauth:{OTHER_ISSUER}:shared"


# ---------------------------------------------------------------------------
# 5. Key rotation, and the cooldown that bounds it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_rotated_key_is_picked_up(idp):
    clock = [1000.0]
    verifier = _verifier(idp, now=lambda: clock[0])
    assert await verifier.verify_token(idp.mint()) is not None

    idp.rotate("k2")
    clock[0] += oauth.JWKS_MIN_REFETCH_SECONDS + 1
    assert await verifier.verify_token(idp.mint()) is not None


@pytest.mark.asyncio
async def test_an_unknown_kid_cannot_be_used_to_drive_refetching(idp):
    """The cooldown, observed rather than described.

    A token naming a key we do not hold is the rotation signal, so it triggers a
    fetch — which makes it a lever an unauthenticated caller can pull. Within the
    cooldown the second attempt must not reach the network.
    """
    clock = [1000.0]
    verifier = _verifier(idp, now=lambda: clock[0])
    await verifier.verify_token(idp.mint())

    forged = FakeIdp(issuer=ISSUER, kid="unknown-kid")

    # Past the cooldown: the unknown key id is treated as a rotation and one
    # round of fetching happens.
    clock[0] += oauth.JWKS_MIN_REFETCH_SECONDS + 1
    idp.requests.clear()
    await verifier.verify_token(forged.mint())
    first_round = len(idp.requests)
    assert first_round > 0

    # Immediately again, with the clock held: no second round.
    await verifier.verify_token(forged.mint())
    assert len(idp.requests) == first_round


# ---------------------------------------------------------------------------
# 6. A provider outage: legible, and not fatal to keys already held
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keys_already_held_survive_an_outage(idp):
    clock = [1000.0]
    verifier = _verifier(idp, now=lambda: clock[0])
    assert await verifier.verify_token(idp.mint()) is not None

    idp.jwks_status = 503
    clock[0] += oauth.JWKS_MIN_REFETCH_SECONDS + 1
    assert await verifier.verify_token(idp.mint()) is not None


@pytest.mark.asyncio
async def test_a_failed_key_fetch_makes_the_next_attempt_re_read_the_metadata(idp):
    """An issuer that moves its key set must not be unreachable for the process.

    The location is cached, so a failure there has to forget it — otherwise the
    only symptom of a moved key set is an outage that never ends, and nothing
    would ever look at the metadata again.
    """
    clock = [1000.0]
    verifier = _verifier(idp, now=lambda: clock[0])
    idp.jwks_status = 503
    assert await verifier.verify_token(idp.mint()) is None

    idp.jwks_status = 200
    idp.jwks_uri = f"{ISSUER}/keys/moved"
    clock[0] += oauth.JWKS_MIN_REFETCH_SECONDS + 1
    idp.requests.clear()
    assert await verifier.verify_token(idp.mint()) is not None
    assert METADATA_URL in idp.requests
    assert f"{ISSUER}/keys/moved" in idp.requests


@pytest.mark.asyncio
async def test_the_first_token_is_verified_even_at_time_zero(idp):
    """The cooldown must not swallow the very first fetch.

    ``time.monotonic`` is uptime on some platforms, so a "last attempted at
    0.0" sentinel is a real timestamp shortly after boot: the first token to
    arrive would be inside the cooldown and refused, and the server would
    authenticate nobody for the first minute of its life.
    """
    verifier = _verifier(idp, now=lambda: 0.0)
    assert await verifier.verify_token(idp.mint()) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("uptime", [0.0, 5000.0])
async def test_an_outage_before_any_key_is_held_refuses_and_says_so(idp, caplog, uptime):
    """And the first outage is logged however long the process has been up.

    The clock is pinned rather than ambient. ``time.monotonic`` is uptime on
    some platforms, so a rate-limiter whose "last logged" sentinel is 0.0 puts a
    freshly started process inside its own cooldown: the very first outage goes
    unlogged, which is the one an operator most needs. Reading the real clock
    would hide that — measured, on a machine up for days it passed and on a
    fresh CI runner it failed.
    """
    idp.metadata_status = 503
    verifier = _verifier(idp, now=lambda: uptime)
    with caplog.at_level("WARNING", logger="cpersona"):
        assert await verifier.verify_token(idp.mint()) is None
    assert any("OAuth verification degraded" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# 7. Configuration: the key set override
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_jwks_override_skips_metadata_discovery(idp):
    verifier = _verifier(idp, jwks_uri=JWKS_URL)
    assert await verifier.verify_token(idp.mint()) is not None
    assert idp.requests == [JWKS_URL]


@pytest.mark.asyncio
async def test_the_jwks_override_is_ignored_with_several_issuers(idp, caplog):
    with caplog.at_level("WARNING", logger="cpersona"):
        verifier = oauth.IdpTokenVerifier(
            (ISSUER, OTHER_ISSUER), RESOURCE, jwks_uri=JWKS_URL, fetch=idp.fetch
        )
    assert any("ignored" in r.getMessage() for r in caplog.records)
    await verifier.verify_token(idp.mint())
    assert METADATA_URL in idp.requests


def test_every_metadata_spelling_is_tried_once():
    """Both well-known forms, deduplicated for a path-less issuer."""
    assert oauth._metadata_urls(ISSUER) == [
        f"{ISSUER}/.well-known/oauth-authorization-server",
        f"{ISSUER}/.well-known/openid-configuration",
    ]
    with_path = "https://idp.example/tenant"
    assert oauth._metadata_urls(with_path) == [
        "https://idp.example/.well-known/oauth-authorization-server/tenant",
        "https://idp.example/.well-known/openid-configuration/tenant",
        "https://idp.example/tenant/.well-known/openid-configuration",
    ]


# ---------------------------------------------------------------------------
# 8. Wiring: what the assembled server actually does
# ---------------------------------------------------------------------------


def test_no_verifier_is_built_without_a_grant_table(oauth_on, caplog):
    """The fail-open this refuses to become.

    Discovery on, ACL off: a verified token would authenticate with nothing
    behind it to limit what it reaches. The absence is asserted rather than the
    paragraph that promises it, because a later refactor that "simplifies" the
    condition would leave the paragraph intact.
    """
    with caplog.at_level("WARNING", logger="cpersona"):
        assert server._oauth_verifier(server._oauth_discovery(), None) is None
    assert any("verification stays OFF" in r.getMessage() for r in caplog.records)


def test_a_verifier_is_built_with_a_grant_table(oauth_on, tmp_path):
    acl_config = _acl_file(tmp_path, [{"client_id": "a", "token": "t", "grants": {"*": "read"}}])
    verifier = server._oauth_verifier(server._oauth_discovery(), acl_config)
    assert isinstance(verifier, oauth.IdpTokenVerifier)


def test_no_verifier_is_built_while_oauth_is_unset(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OAUTH_RESOURCE", "")
    monkeypatch.setattr(config, "OAUTH_AUTHORIZATION_SERVERS", "")
    acl_config = _acl_file(tmp_path, [{"client_id": "a", "token": "t", "grants": {"*": "read"}}])
    assert server._oauth_verifier(server._oauth_discovery(), acl_config) is None


@pytest.mark.asyncio
async def test_an_oauth_token_reaches_the_tool_surface(oauth_on, monkeypatch, tmp_path, idp):
    monkeypatch.setattr(oauth, "_http_get", idp.fetch)
    acl_config = _acl_file(
        tmp_path,
        [
            {"client_id": "a", "token": "token-a", "grants": {"*": "read"}},
            {"client_id": NAMESPACED, "token": None, "grants": {"*": "read"}},
        ],
    )
    app, reached = _make_app(acl_config=acl_config)
    assert await _request(app, token=idp.mint()) == 200
    assert reached == ["/mcp"]


@pytest.mark.asyncio
async def test_a_foreign_audience_token_is_refused_by_the_assembled_app(
    oauth_on, monkeypatch, tmp_path, idp
):
    """The same check as §1, through the stack that is actually served."""
    monkeypatch.setattr(oauth, "_http_get", idp.fetch)
    acl_config = _acl_file(
        tmp_path, [{"client_id": NAMESPACED, "token": None, "grants": {"*": "read"}}]
    )
    app, reached = _make_app(acl_config=acl_config)
    assert await _request(app, token=idp.mint(aud="https://elsewhere.example/mcp")) == 401
    assert reached == []


@pytest.mark.asyncio
async def test_an_oauth_token_is_refused_while_the_static_token_mode_is_in_use(
    oauth_on, monkeypatch, idp
):
    """No grant table, no verification — end to end.

    The static-credential deployment authenticates by comparing one shared
    secret. A provider-issued token must not become a second way in, because
    there is no grant table to bound what it reaches.
    """
    monkeypatch.setattr(oauth, "_http_get", idp.fetch)
    app, reached = _make_app(acl_config=None, auth_token="s3cret")
    assert await _request(app, token=idp.mint()) == 401
    assert reached == []
    assert await _request(app, token="s3cret") == 200


# ---------------------------------------------------------------------------
# 9. Additive: the callers who never asked for OAuth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_existing_acl_token_still_authenticates_with_oauth_on(
    oauth_on, monkeypatch, tmp_path, idp
):
    monkeypatch.setattr(oauth, "_http_get", idp.fetch)
    acl_config = _acl_file(tmp_path, [{"client_id": "a", "token": "token-a", "grants": {"*": "read"}}])
    app, reached = _make_app(acl_config=acl_config)
    assert await _request(app, token="token-a") == 200
    assert reached == ["/mcp"]
    # The local table answered, so the verifier was never consulted: no fetch.
    assert idp.requests == []


@pytest.mark.asyncio
async def test_an_unknown_credential_is_still_refused_with_oauth_on(
    oauth_on, monkeypatch, tmp_path, idp
):
    monkeypatch.setattr(oauth, "_http_get", idp.fetch)
    acl_config = _acl_file(tmp_path, [{"client_id": "a", "token": "token-a", "grants": {"*": "read"}}])
    app, reached = _make_app(acl_config=acl_config)
    assert await _request(app, token="nope") == 401
    assert reached == []


# ---------------------------------------------------------------------------
# 10. After authentication: an identity the grant table never heard of
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unprovisioned_oauth_client_authenticates_and_is_then_denied(tmp_path):
    """The operational shape of per-client provisioning.

    A provider mints identifiers; the grant table is written by hand. Until
    somebody adds the row, the client connects successfully and every scoped
    tool refuses it — correct, and indistinguishable from a bug without the
    detail the denial now carries.
    """
    acl.activate(_acl_file(tmp_path, [{"client_id": "a", "token": "t", "grants": {"*": "read"}}]))

    async def handler(arguments):
        return {"ok": True}

    token = acl.set_principal(acl.Principal(client_id=NAMESPACED))
    try:
        result = await acl._wrap("recall", handler)({"agent_id": "alpha"})
    finally:
        acl.reset_principal(token)
    assert result["ok"] is False
    assert result["client_id"] == NAMESPACED
    assert "detail" in result


# ---------------------------------------------------------------------------
# bug-297 — the cap bounds what is RECEIVED, not only what is parsed.
#
# ``_get_json`` refuses a document over ``MAX_DOCUMENT_BYTES``, which decides
# what is parsed and trusted. The seam under it used to return
# ``response.content``, so httpx had already buffered the whole body by the time
# that comparison ran: the allocation the cap exists to bound had happened, at a
# size chosen by whoever answers at the configured issuer or JWKS URI — the one
# party OAuth verification exists to survive.
#
# This drives the real ``_http_get`` (the fetch seam every other test in this
# file replaces) against a transport that streams an unbounded body, and asserts
# on the number of bytes it agreed to pull. Asserting the returned length alone
# would not do it: a buffered read that trims afterwards returns the same length
# while having received all of it. The generator counts what it was ASKED for.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_get_stops_receiving_past_the_document_cap(monkeypatch):
    import httpx

    from cpersona import oauth as oauth_module

    served = {"bytes": 0}
    chunk = b"x" * 8192
    # Far more than the cap, and never exhausted by a bounded reader.
    total_available = oauth_module.MAX_DOCUMENT_BYTES * 8

    async def endless():
        # Async: the async client asserts its transport hands back an
        # AsyncByteStream, and a sync generator is not one.
        while served["bytes"] < total_available:
            served["bytes"] += len(chunk)
            yield chunk

    def handler(request):
        return httpx.Response(200, content=endless())

    real_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)

    status, body = await oauth_module._http_get("https://issuer.example/.well-known/openid-configuration")

    assert status == 200
    # Received: bounded by the cap plus at most the chunk that crossed it.
    assert served["bytes"] <= oauth_module.MAX_DOCUMENT_BYTES + len(chunk), (
        f"the fetch pulled {served['bytes']} bytes for a document capped at "
        f"{oauth_module.MAX_DOCUMENT_BYTES}; the cap is not bounding the receive side"
    )
    assert served["bytes"] < total_available, "the whole body was received"
    # Returned: strictly past the cap, so `_get_json`'s `len(body) >` refusal fires
    # and reports an oversized document rather than a JSON parse error on a
    # silently truncated one.
    assert len(body) > oauth_module.MAX_DOCUMENT_BYTES
