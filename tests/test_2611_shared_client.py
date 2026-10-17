"""Regression tests for the shared client defects that reach CPersona.

The shared client (cpersona/_vendored_mcp_common) is fixed upstream and resynced
here; the upstream tests cover each module. These pin what CPersona itself serves:

  bug-512  an integer argument sent as a JSON float (60.0) passed the tool's input
           schema and was then replaced by the default.
  bug-598  a negative CPERSONA_EMBEDDING_CACHE_SIZE made every successful
           single-text embed raise KeyError; config now floors it at 0.
  bug-599  an ACL token with a trailing newline or space loaded as a row no client
           can present; it is now a startup error naming the client.

Each test states the correct behaviour and was seen to fail on the unfixed code for
the reason named above.
"""

import importlib
import json
import os
import tempfile

os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "test_2611_shared_client.db"))
os.environ.setdefault("CPERSONA_EMBEDDING_MODE", "none")

import pytest  # noqa: E402
from mcp import types  # noqa: E402

from cpersona import acl, config, server  # noqa: E402


async def _call(name: str, arguments: dict) -> dict:
    handler = server.registry.server.request_handlers[types.CallToolRequest]
    request = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name=name, arguments=arguments),
    )
    result = (await handler(request)).root
    return json.loads(result.content[0].text)


# ---------------------------------------------------------------------------
# bug-512 -- an integral float is the integer it spells
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("ttl", [60, 60.0], ids=["int", "float"])
async def test_an_integer_argument_sent_as_a_float_is_not_replaced_by_the_default(ttl):
    session_key = f"bug512-{type(ttl).__name__}"
    try:
        paused = await _call("pause_persistence", {"ttl_seconds": ttl, "session_key": session_key})
    finally:
        await _call("resume_persistence", {"session_key": session_key})

    assert paused["paused"] is True, paused
    assert paused["ttl_seconds"] == 60, f"ttl_seconds={ttl!r} was replaced: {paused}"


@pytest.mark.asyncio
async def test_a_fractional_float_is_still_refused_by_the_schema():
    """Control: 2.5 is not an integer, so the input schema refuses it before the handler."""
    handler = server.registry.server.request_handlers[types.CallToolRequest]
    request = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name="pause_persistence", arguments={"ttl_seconds": 2.5}),
    )
    result = (await handler(request)).root
    assert result.isError is True


# ---------------------------------------------------------------------------
# bug-598 -- a negative embedding cache size
# ---------------------------------------------------------------------------


def test_a_negative_embedding_cache_size_is_floored_at_zero(monkeypatch):
    try:
        with monkeypatch.context() as env:
            env.setenv("CPERSONA_EMBEDDING_CACHE_SIZE", "-1")
            assert importlib.reload(config).EMBEDDING_CACHE_SIZE == 0
            env.setenv("CPERSONA_EMBEDDING_CACHE_SIZE", "0")
            assert importlib.reload(config).EMBEDDING_CACHE_SIZE == 0
            env.setenv("CPERSONA_EMBEDDING_CACHE_SIZE", "32")
            assert importlib.reload(config).EMBEDDING_CACHE_SIZE == 32
    finally:
        importlib.reload(config)


# ---------------------------------------------------------------------------
# bug-599 -- a token no client can present is a startup error
# ---------------------------------------------------------------------------


def _acl_file(tmp_path, token) -> str:
    path = tmp_path / "acl.json"
    path.write_text(json.dumps({"clients": [{"client_id": "agent.client", "token": token, "grants": {"*": "read"}}]}))
    return str(path)


@pytest.mark.parametrize("token", ["secret-token\n", "secret-token ", " secret-token"])
def test_an_acl_token_with_surrounding_whitespace_fails_at_load(tmp_path, token):
    with pytest.raises(acl.AclConfigError, match="whitespace") as caught:
        acl.load_config(_acl_file(tmp_path, token))
    assert "agent.client" in str(caught.value)
    assert "secret-token" not in str(caught.value)


def test_an_acl_token_without_whitespace_still_loads(tmp_path):
    """Control: the same row with the token as a client sends it."""
    loaded = acl.load_config(_acl_file(tmp_path, "secret-token"))
    assert ("secret-token", "agent.client") in loaded.token_entries
