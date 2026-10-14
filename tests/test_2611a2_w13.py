"""A mode's token cap under a per-subject client counts what the ACL guard echoes.

The guard resolves "@me" to the subject's alias and echoes it back as
`resolved_agent_id` (and `alias_issued` on the first issuance). Those fields are part
of the JSON the caller receives, so `used_tokens` must count them and the response
must stay within `cap` with them included.
"""

import json

import pytest

from cpersona import acl, aliases, memory_handlers, reconstruct, server, tokens
from tests.test_reconstruct_evidence_sequence import RECORDS, SHORT, sized  # noqa: F401 (fixture)
from tests.test_reconstruct_modes import QUERY

ISSUER = "https://auth.example.com"
OAUTH_CLIENT = f"oauth:{ISSUER}:https://claude.ai/mcp-client"
ARGS = {"agent_id": "@me", "query": QUERY, "deep": True}


@pytest.fixture
def per_subject(tmp_path):
    path = tmp_path / "acl.json"
    path.write_text(
        json.dumps(
            {
                "clients": [
                    {
                        "client_id": OAUTH_CLIENT,
                        "token": None,
                        "grants": {"*": "read-write"},
                        "per_subject": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    acl.activate(acl.load_config(str(path)))
    ledger = aliases.AliasLedger(str(tmp_path / "alias_ledger.json"))
    acl.activate_ledger(ledger)
    try:
        yield ledger
    finally:
        acl.activate(None)
        acl.activate_ledger(None)


async def _as_subject(subject: str, arguments: dict) -> dict:
    token = acl.set_principal(acl.Principal(client_id=OAUTH_CLIENT, issuer=ISSUER, subject=subject))
    try:
        return await server.registry._handlers["reconstruct"](arguments)
    finally:
        acl.reset_principal(token)


def _wire(out: dict) -> int:
    # The serialization the MCP layer sends.
    return tokens.count(json.dumps(out, ensure_ascii=False))


async def _store_subject_corpus(tmp, ledger) -> str:
    alias, _ = ledger.resolve_or_issue(ISSUER, "user-1")
    for text in RECORDS + SHORT:
        await memory_handlers.do_store(alias, {"content": text})
    await tmp.drain()
    return alias


@pytest.mark.usefixtures("blocks_off", "sized")
@pytest.mark.asyncio
async def test_used_tokens_counts_the_alias_echo(fake_embedding_client, per_subject):
    from tests.test_reconstruct_filled_quote import _TempDB

    async with _TempDB() as tmp:
        alias = await _store_subject_corpus(tmp, per_subject)

        # A subject's first call: the alias is issued, and both echo fields come back.
        first = await _as_subject("user-2", {"agent_id": "@me", "query": QUERY, "mode": "lite"})
        assert first["resolved_agent_id"].startswith(aliases.ALIAS_PREFIX) and first["alias_issued"] is True
        assert first["used_tokens"] == _wire(first), "used_tokens must count alias_issued too"

        for mode in ("lite", "pro"):
            out = await _as_subject("user-1", {**ARGS, "mode": mode})
            assert out["resolved_agent_id"] == alias and out["items"]
            assert out["used_tokens"] == _wire(out), f"{mode}: used_tokens must count the echoed alias"


@pytest.mark.usefixtures("blocks_off", "sized")
@pytest.mark.asyncio
async def test_the_cap_holds_with_the_alias_echo_included(fake_embedding_client, per_subject, monkeypatch):
    from tests.test_reconstruct_filled_quote import _TempDB

    async with _TempDB() as tmp:
        await _store_subject_corpus(tmp, per_subject)
        # A cap one token below what the whole payload sends: the response must shrink
        # to fit it, echo included, rather than come back one echo's worth over.
        whole = await _as_subject("user-1", {**ARGS, "mode": "pro"})
        sent = _wire(whole)
        assert sent < reconstruct.MODE_CAPS["pro"], "the fixture's payload must fit pro whole"
        assert sent >= 1000, "a cap of the same digit count keeps the comparison exact"
        monkeypatch.setitem(reconstruct.MODE_CAPS, "pro", sent - 1)
        capped = await _as_subject("user-1", {**ARGS, "mode": "pro"})
    assert capped.get("ok") is not False and capped["items"]
    assert capped["cap"] == sent - 1
    assert _wire(capped) <= capped["cap"], "the JSON sent must stay within the cap"
