"""Regression tests for the authentication boundary and the stdio proxy.

  ACL file mode     The group/world-accessible warning on the ACL file is a
                    judgement of permission bits, and where those bits are not
                    the access control (Windows) the mode os.stat reports is
                    synthesised. The warning fired on every start there and
                    advised a chmod that changes nothing.
  per-subject       A per-subject principal whose call demands every agent was
  sweep advice      always told to scope it with agent_id "@me". For a tool
                    whose all-agents demand is its own (a process-wide switch,
                    unconfined file I/O), "@me" narrows nothing: the retry was
                    denied again with the same advice. The named-agent twin is
                    a file in the export directory that belongs to another
                    agent: the caller has already sent "@me".
  kid-less JWKS     RFC 7517 makes "kid" optional. A key set whose one signing
                    key carries none was reduced to nothing, so a single-key
                    issuer authenticated nobody and the log blamed the issuer.
  issuer log        The once-per-cooldown warning was stamped per issuer, so an
  window            earlier, harmless diagnostic in the same discovery walk
                    suppressed the pairwise-subject refusal on every walk.
  alias ledger      Issuance re-reads the file, mints and replaces it whole. A
                    second server process issuing between that re-read and the
                    rename had its row erased.
  proxy 2xx body    The proxy guarded only the status. A 200 HTML page was
                    written to stdout as several non-JSON lines, and the request
                    it answered never got a JSON-RPC reply. The same holds for
                    an event-stream body's data lines and for a JSON body
                    spread over several lines.
"""

import asyncio
import base64
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time

os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "test_2611a2_x14.db"))
os.environ.setdefault("CPERSONA_EMBEDDING_MODE", "none")

import httpx  # noqa: E402
import jwt  # noqa: E402
import pytest  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

from cpersona import acl, aliases, config, fileperms, oauth, proxy_stdio  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent

ISSUER = "https://auth.example.com"
RESOURCE = "https://memory.example.com/mcp"
OAUTH_CLIENT = f"oauth:{ISSUER}:https://claude.ai/mcp-client"


@pytest.fixture(autouse=True)
def _reset_acl():
    yield
    acl.activate(None)
    acl.activate_ledger(None)


# ---------------------------------------------------------------------------
# The ACL file's mode is judged only where mode bits are the access control
# ---------------------------------------------------------------------------


def _write_acl(tmp_path, payload) -> str:
    path = tmp_path / "acl.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _static_payload():
    return {"clients": [{"client_id": "assistant-a", "token": "tok-a", "grants": {"alpha": "read"}}]}


def _mode_warnings(caplog):
    return [r.getMessage() for r in caplog.records if "group/world-accessible" in r.getMessage()]


def test_the_acl_file_mode_is_not_judged_where_mode_bits_are_not_the_access_control(
    tmp_path, monkeypatch, caplog
):
    """Same rule the database warning and the permissions check already follow.

    The control half proves the gate is the platform question and not a
    warning that was removed: the same widened file still warns where the bits
    are enforced.
    """
    path = _write_acl(tmp_path, _static_payload())
    os.chmod(path, 0o644)

    monkeypatch.setattr(fileperms, "mode_bits_are_enforced", lambda *a, **k: True)
    with caplog.at_level("WARNING"):
        acl.load_config(path)
    assert _mode_warnings(caplog), "control: a widened ACL file warns where bits are enforced"

    caplog.clear()
    monkeypatch.setattr(fileperms, "mode_bits_are_enforced", lambda *a, **k: False)
    with caplog.at_level("WARNING"):
        acl.load_config(path)
    assert _mode_warnings(caplog) == [], "the mode is not the access control here, so it is not a finding"


# ---------------------------------------------------------------------------
# A per-subject sweep is advised "@me" only when "@me" would narrow it
# ---------------------------------------------------------------------------


def _activate_per_subject(tmp_path):
    payload = {
        "clients": [
            {
                "client_id": OAUTH_CLIENT,
                "token": None,
                "grants": {"*": "read-write"},
                "per_subject": True,
            }
        ]
    }
    acl.activate(acl.load_config(_write_acl(tmp_path, payload)))
    acl.activate_ledger(aliases.AliasLedger(str(tmp_path / "alias_ledger.json")))


async def _stub_handler(arguments):
    return {"ok": True, "echo": arguments}


async def _call_as_subject(tool, arguments):
    guarded = acl._wrap(tool, _stub_handler)
    token = acl.set_principal(acl.Principal(client_id=OAUTH_CLIENT, issuer=ISSUER, subject="user-1"))
    try:
        return await guarded(arguments)
    finally:
        acl.reset_principal(token)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arguments", "cause"),
    [
        ("pause_persistence", {"session_key": "s1"}, "session_key"),
        ("pause_persistence", {"agent_id": "@me", "session_key": "s1"}, "session_key"),
        ("resume_persistence", {"agent_id": "@me"}, "session_key"),
        ("get_session_findings", {"agent_id": "@me"}, "whole database"),
        ("check_update", {"agent_id": "@me"}, "property of the process"),
        ("export_memories", {"agent_id": "@me", "output_path": "out.jsonl"}, "CPERSONA_EXPORT_DIR is unset"),
        ("check_health", {"agent_id": "@me", "fix": True}, "`checks`"),
    ],
)
async def test_a_per_subject_sweep_no_scope_narrows_is_not_advised_at_me(
    tmp_path, monkeypatch, tool, arguments, cause
):
    """Following the advice was denied again with the same words.

    Each of these demands every agent whatever agent_id says, so the advice
    the boundary gave could be followed to the letter and refused again. The
    refusal must name the real cause and must not offer "@me".
    """
    monkeypatch.setattr(config, "EXPORT_DIR", "")
    _activate_per_subject(tmp_path)

    denied = await _call_as_subject(tool, arguments)

    assert denied["ok"] is False and denied["error"] == "permission_denied"
    assert denied["agent_id"] == acl.WILDCARD
    assert "@me" not in denied["detail"], denied["detail"]
    assert "no agent scope narrows this call" in denied["detail"], denied["detail"]
    assert "per_subject" in denied["detail"], denied["detail"]
    assert cause in denied["detail"], denied["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("import_memories", {"target_agent_id": "@me", "input_path": "alpha.jsonl"}),
        ("export_memories", {"agent_id": "@me", "output_path": "alpha.jsonl"}),
    ],
)
async def test_a_foreign_agent_the_file_names_is_not_advised_at_me(tmp_path, monkeypatch, tool, arguments):
    """The named-agent twin: the file in the export directory belongs to another agent.

    The caller already scoped the call to its own space, and the agent it is
    refused for comes from the file at the path, so "@me" -- the advice it was
    given -- is the argument it already sent.
    """
    exports = tmp_path / "exports"
    exports.mkdir()
    (exports / "alpha.jsonl").write_text(
        json.dumps({"_type": "header", "agent_id": "alpha", "version": 1})
        + "\n"
        + json.dumps({"_type": "memory", "agent_id": "alpha", "content": "x"})
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "EXPORT_DIR", str(exports))
    path_key = "input_path" if "input_path" in arguments else "output_path"
    arguments = {**arguments, path_key: str(exports / arguments[path_key])}
    _activate_per_subject(tmp_path)

    denied = await _call_as_subject(tool, arguments)

    assert denied["ok"] is False and denied["agent_id"] == "alpha", denied
    assert "@me" not in denied["detail"], denied["detail"]
    assert "'alpha'" in denied["detail"] and "file at the path" in denied["detail"], denied["detail"]

    control = await _call_as_subject("store", {"agent_id": "someone", "content": "x"})
    assert control["ok"] is False
    assert '"@me"' in control["detail"], "a foreign agent_id is still told to use its own space"


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [{"query": ""}, {"agent_id": "*", "query": ""}])
async def test_a_per_subject_sweep_a_scope_narrows_still_advises_at_me(tmp_path, arguments):
    """Control: where "@me" is the fix, it is still the advice given."""
    _activate_per_subject(tmp_path)

    denied = await _call_as_subject("recall", arguments)

    assert denied["ok"] is False
    assert 'agent_id "@me"' in denied["detail"], denied["detail"]
    followed = await _call_as_subject("recall", {**arguments, "agent_id": "@me"})
    assert followed["ok"] is True, "and following it works"


# ---------------------------------------------------------------------------
# JWKS keys without a kid
# ---------------------------------------------------------------------------


def _b64(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _jwk(private_key, kid=None):
    numbers = private_key.public_key().public_numbers()
    jwk = {"kty": "RSA", "use": "sig", "alg": "RS256", "n": _b64(numbers.n), "e": _b64(numbers.e)}
    if kid is not None:
        jwk["kid"] = kid
    return jwk


def _mint(private_key, kid=None):
    now = int(time.time())
    claims = {
        "sub": "user-1",
        "aud": RESOURCE,
        "iss": ISSUER,
        "exp": now + 300,
        "iat": now,
        "client_id": "https://claude.ai/mcp-client",
        "scope": "cpersona:read",
    }
    headers = {"kid": kid} if kid is not None else {}
    return jwt.encode(claims, private_key, algorithm="RS256", headers=headers)


def _fetch_serving(jwks: dict, *, metadata: dict | None = None, oidc: dict | None = None):
    jwks_url = f"{ISSUER}/oauth2/jwks"

    async def fetch(url):
        if url == f"{ISSUER}/.well-known/oauth-authorization-server":
            doc = {"issuer": ISSUER, "jwks_uri": jwks_url} if metadata is None else metadata
            return 200, json.dumps(doc).encode()
        if url == f"{ISSUER}/.well-known/openid-configuration" and oidc is not None:
            return 200, json.dumps(oidc).encode()
        if url == jwks_url:
            return 200, json.dumps(jwks).encode()
        return 404, b""

    return fetch


@pytest.fixture(scope="module")
def rsa_keys():
    return [rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(2)]


@pytest.mark.asyncio
async def test_a_single_signing_key_without_a_kid_verifies_a_token_without_one(rsa_keys, caplog):
    """RFC 7517 §4.5: "kid" is OPTIONAL, and one key is unambiguous without it."""
    key = rsa_keys[0]
    verifier = oauth.IdpTokenVerifier([ISSUER], RESOURCE, fetch=_fetch_serving({"keys": [_jwk(key)]}))

    with caplog.at_level("WARNING"):
        verified = await verifier.verify_token(_mint(key))

    assert verified is not None, [r.getMessage() for r in caplog.records]
    assert verified.subject == "user-1"
    assert not [r for r in caplog.records if "no usable signing key" in r.getMessage()]


@pytest.mark.asyncio
async def test_a_token_from_another_key_is_still_refused_by_a_kidless_set(rsa_keys):
    """The served key is a key, not a pass: the signature still has to match it."""
    published, other = rsa_keys
    verifier = oauth.IdpTokenVerifier([ISSUER], RESOURCE, fetch=_fetch_serving({"keys": [_jwk(published)]}))

    assert await verifier.verify_token(_mint(other)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("signer", [0, 1])
async def test_several_kidless_keys_are_refused_rather_than_guessed(rsa_keys, caplog, signer):
    """No key can be told from another, so neither order may decide the outcome."""
    jwks = {"keys": [_jwk(rsa_keys[0]), _jwk(rsa_keys[1])]}
    verifier = oauth.IdpTokenVerifier([ISSUER], RESOURCE, fetch=_fetch_serving(jwks))

    with caplog.at_level("WARNING"):
        assert await verifier.verify_token(_mint(rsa_keys[signer])) is None
    assert any("none of them carries a kid" in r.getMessage() for r in caplog.records), [
        r.getMessage() for r in caplog.records
    ]


@pytest.mark.asyncio
async def test_a_kid_named_set_keeps_its_behaviour(rsa_keys):
    """Control: a key set that names its keys is answered by name, as before."""
    key = rsa_keys[0]
    jwks = {"keys": [_jwk(key, kid="k1")]}
    verifier = oauth.IdpTokenVerifier([ISSUER], RESOURCE, fetch=_fetch_serving(jwks))

    assert await verifier.verify_token(_mint(key, kid="k1")) is not None
    assert await verifier.verify_token(_mint(key)) is not None, "one key, so a kid-less token is unambiguous"


# ---------------------------------------------------------------------------
# One diagnostic does not silence another for the same issuer
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.mark.asyncio
async def test_an_earlier_diagnostic_does_not_suppress_the_pairwise_refusal(rsa_keys, caplog):
    """RFC 8414 metadata may omit jwks_uri; OIDC discovery then declares pairwise.

    The first line the walk writes is about the RFC 8414 document. The second,
    the pairwise verdict, is the one an operator needs, and it was dropped by
    the window the first had just stamped -- on every walk, since the refresh
    cooldown and the log window are the same length.
    """
    key = rsa_keys[0]
    fetch = _fetch_serving(
        {"keys": [_jwk(key, kid="k1")]},
        metadata={"issuer": ISSUER},
        oidc={"issuer": ISSUER, "jwks_uri": f"{ISSUER}/oauth2/jwks", "subject_types_supported": ["pairwise"]},
    )
    clock = _Clock()
    verifier = oauth.IdpTokenVerifier([ISSUER], RESOURCE, fetch=fetch, now=clock, require_public_subject=True)

    with caplog.at_level("WARNING"):
        for _ in range(3):
            assert await verifier.verify_token(_mint(key, kid="k1")) is None
            clock.t += oauth.JWKS_MIN_REFETCH_SECONDS + 1

    messages = [r.getMessage() for r in caplog.records]
    pairwise = [m for m in messages if "only pairwise subject identifiers" in m]
    jwks_uri = [m for m in messages if "publishes no https jwks_uri" in m]
    assert pairwise, messages
    assert len(pairwise) == 3, "one per cooldown, each walk"
    assert len(jwks_uri) == 3, "the earlier diagnostic is still written too"


@pytest.mark.asyncio
async def test_the_same_diagnostic_is_still_written_once_per_cooldown(rsa_keys, caplog):
    """Control: keying the window by diagnostic did not unbound it.

    The verdict is reached from an unauthenticated request, so writing it each
    time would hand the log to whoever wants to fill it.
    """
    key = rsa_keys[0]
    fetch = _fetch_serving(
        {"keys": [_jwk(key, kid="k1")]},
        metadata={"issuer": ISSUER, "jwks_uri": f"{ISSUER}/oauth2/jwks", "subject_types_supported": ["pairwise"]},
    )
    clock = _Clock()
    verifier = oauth.IdpTokenVerifier([ISSUER], RESOURCE, fetch=fetch, now=clock, require_public_subject=True)

    with caplog.at_level("WARNING"):
        for step in range(5):
            # The refetch cooldown lets a walk through every other request, and
            # each one stays inside the log window of the first.
            clock.t += (oauth.JWKS_MIN_REFETCH_SECONDS / 5) if step else 0
            verifier._cache[ISSUER].last_attempt = -float("inf")
            assert await verifier.verify_token(_mint(key, kid="k1")) is None

    pairwise = [r for r in caplog.records if "only pairwise subject identifiers" in r.getMessage()]
    assert len(pairwise) == 1, [r.getMessage() for r in caplog.records]


# ---------------------------------------------------------------------------
# The alias ledger's re-read, mint and rename hold across processes
# ---------------------------------------------------------------------------

_SECOND_PROCESS = """
import json, sys
from cpersona import aliases
ledger = aliases.AliasLedger(sys.argv[1])
print("ready", flush=True)
alias, issued = ledger.resolve_or_issue(sys.argv[2], "bob")
print(json.dumps([alias, issued]), flush=True)
"""


def test_a_second_process_issuing_inside_the_window_keeps_its_row(tmp_path, monkeypatch):
    """Process B issues while process A sits between its re-read and its rename.

    Without a lock across the two, B's write lands first and A's whole-file
    rename -- built from a read that predates it -- erases B's row. B has already
    answered its subject with that alias, so after B restarts the subject is
    issued a fresh one and everything it stored is stranded.

    With the lock, B waits for A's rename, re-reads the file A wrote, and adds
    its row to it. The wait below is bounded so the unfixed code (B does not
    wait) and the fixed code (B does) both run to the end; the assertion is on
    what is on disk afterwards.
    """
    path = str(tmp_path / "alias_ledger.json")
    first = aliases.AliasLedger(path)
    real_replace = os.replace
    second: dict = {}

    def replace_with_a_rival_in_the_window(src, dst):
        if dst == path and "proc" not in second:
            proc = subprocess.Popen(
                [sys.executable, "-c", _SECOND_PROCESS, path, ISSUER],
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            second["proc"] = proc
            assert proc.stdout.readline().strip() == "ready", proc.stderr.read()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass  # blocked on the ledger lock until this rename is done
        return real_replace(src, dst)

    monkeypatch.setattr(aliases.os, "replace", replace_with_a_rival_in_the_window)
    alice, alice_issued = first.resolve_or_issue(ISSUER, "alice")
    monkeypatch.setattr(aliases.os, "replace", real_replace)

    out, err = second["proc"].communicate(timeout=30)
    assert second["proc"].returncode == 0, err
    bob, bob_issued = json.loads(out.strip().splitlines()[-1])
    assert alice_issued and bob_issued

    on_disk = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))["aliases"][ISSUER]
    assert on_disk.get("alice") == alice
    assert on_disk.get("bob") == bob, f"the second process's row was erased: {on_disk}"
    assert aliases.AliasLedger(path).peek(ISSUER, "bob") == bob, "and survives a restart"


@pytest.mark.skipif(not fileperms.mode_bits_are_enforced(), reason="mode bits are not the access control here")
def test_the_ledger_lock_file_is_private_and_empty(tmp_path):
    path = str(tmp_path / "alias_ledger.json")
    aliases.AliasLedger(path).resolve_or_issue(ISSUER, "alice")

    st = os.stat(path + ".lock")
    assert st.st_mode & 0o777 == fileperms.PRIVATE_FILE_MODE
    assert st.st_size == 0


@pytest.mark.skipif(os.name != "posix", reason="O_NOFOLLOW is a POSIX flag")
def test_a_link_planted_at_the_lock_path_is_not_followed(tmp_path):
    """Refused like any other write that cannot be made, and the target is untouched."""
    path = str(tmp_path / "alias_ledger.json")
    target = tmp_path / "elsewhere"
    target.write_text("keep", encoding="utf-8")
    os.symlink(target, path + ".lock")

    with pytest.raises(aliases.AliasLedgerError, match="lock"):
        aliases.AliasLedger(path).resolve_or_issue(ISSUER, "alice")
    assert target.read_text(encoding="utf-8") == "keep"
    assert not os.path.exists(path), "nothing was issued without the lock"


# ---------------------------------------------------------------------------
# The proxy forwards only JSON from a 2xx body
# ---------------------------------------------------------------------------

_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _drive_proxy(monkeypatch, handler, lines):
    written: list[str] = []

    class _Thread:
        def __init__(self, target, args, daemon):
            self.queue, self.loop = args

        def start(self):
            for line in lines:
                self.loop.call_soon(self.queue.put_nowait, line)
            self.loop.call_soon(self.queue.put_nowait, None)

    monkeypatch.setattr(proxy_stdio, "AUTH_TOKEN", "")
    monkeypatch.setattr(proxy_stdio, "_write_stdout", written.append)
    monkeypatch.setattr(
        proxy_stdio.httpx,
        "AsyncClient",
        lambda **kwargs: _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs),
    )
    monkeypatch.setattr(proxy_stdio.threading, "Thread", _Thread)
    asyncio.run(asyncio.wait_for(proxy_stdio.main(), timeout=10))
    return written


def _assert_json_frames(written):
    """Every write is one line the client's reader can parse as a JSON-RPC frame."""
    for frame in written:
        assert "\n" not in frame, f"a frame spans several lines on stdout: {frame!r}"
        try:
            parsed = json.loads(frame)
        except ValueError:
            pytest.fail(f"a non-JSON frame was written to stdout: {frame!r}")
        assert isinstance(parsed, (dict, list)), f"not a JSON-RPC message or batch: {frame!r}"


def _request(req_id):
    return json.dumps({"jsonrpc": "2.0", "id": req_id, "method": "tools/call"}).encode()


def test_a_2xx_html_page_is_answered_as_an_error_not_written_to_stdout(monkeypatch):
    """An SSO sign-in page or a SPA fallback answers 200 with HTML."""

    def handler(request):
        return httpx.Response(
            200, text="<html>\n<body>Sign in</body>\n</html>", headers={"content-type": "text/html"}
        )

    written = _drive_proxy(monkeypatch, handler, [_request(7)])

    _assert_json_frames(written)
    assert len(written) == 1, written
    reply = json.loads(written[0])
    assert reply["id"] == 7 and reply["error"]["code"] == -32000, reply
    assert "HTTP 200" in reply["error"]["message"]


def test_a_2xx_json_body_spread_over_lines_is_written_as_one_line(monkeypatch):
    """Line breaks between JSON tokens carry no meaning, but they split the frame."""

    def handler(request):
        req_id = json.loads(request.content)["id"]
        body = json.dumps({"jsonrpc": "2.0", "id": req_id, "result": {"text": "a\nb"}}, indent=2)
        return httpx.Response(200, text=body, headers={"content-type": "application/json"})

    written = _drive_proxy(monkeypatch, handler, [_request(3)])

    _assert_json_frames(written)
    assert len(written) == 1, written
    assert json.loads(written[0]) == {"jsonrpc": "2.0", "id": 3, "result": {"text": "a\nb"}}


def test_an_sse_body_of_non_json_data_is_answered_as_an_error(monkeypatch):
    """The event-stream branch is a 2xx body too, and forwarded its data lines unchecked."""

    def handler(request):
        return httpx.Response(
            200, text="data: <html>\n\ndata: <body>Sign in</body>\n\n", headers={"content-type": "text/event-stream"}
        )

    written = _drive_proxy(monkeypatch, handler, [_request(9)])

    _assert_json_frames(written)
    assert len(written) == 1, written
    reply = json.loads(written[0])
    assert reply["id"] == 9 and reply["error"]["code"] == -32000, reply


def test_an_sse_body_keeps_its_messages_and_drops_only_the_junk(monkeypatch, caplog):
    """A reply that did arrive is still owed; only the unparseable line is held back."""

    def handler(request):
        req_id = json.loads(request.content)["id"]
        body = f'data: {{"jsonrpc":"2.0","id":{req_id},"result":{{}}}}\n\ndata: <html>\n\n'
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    with caplog.at_level("WARNING"):
        written = _drive_proxy(monkeypatch, handler, [_request(4)])

    _assert_json_frames(written)
    assert [json.loads(w) for w in written] == [{"jsonrpc": "2.0", "id": 4, "result": {}}]
    assert any("not JSON-RPC messages" in r.getMessage() for r in caplog.records)


def test_a_compact_json_reply_is_forwarded_byte_for_byte(monkeypatch):
    """Control: what the server sends is still exactly what the client reads."""
    body = '{"jsonrpc":"2.0","id":5,"result":{"n":1.0,"s":"\\u00e9"}}'

    def handler(request):
        return httpx.Response(200, text=body, headers={"content-type": "application/json"})

    assert _drive_proxy(monkeypatch, handler, [_request(5)]) == [body]


def test_an_accepted_notification_still_writes_nothing(monkeypatch):
    """Control: a 202 with no body owes the client nothing."""

    def handler(request):
        return httpx.Response(202)

    notification = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode()
    assert _drive_proxy(monkeypatch, handler, [notification]) == []

