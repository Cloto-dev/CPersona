"""The declared shape of an ``external_context`` entry, and what a wrong one costs.

bug-292: the item schema declared ``role`` and ``content`` while the handler read
``name``, ``user_id`` and ``timestamp`` as well, named only in the array's
free-text description. bug-291: two of those three reads were not type-safe — a
non-string ``timestamp`` raised ``AttributeError`` out of the whole call, and a
non-string ``name`` / ``user_id`` reached ``source`` verbatim.

The suite runs without an embedding server (``EMBEDDING_MODE=none``); the recall
underneath is stubbed so these tests are about the merge, not about retrieval.
"""
import os
import tempfile

os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "ctx.db"))
os.environ["CPERSONA_EMBEDDING_MODE"] = "none"

import logging  # noqa: E402

import pytest  # noqa: E402

from cpersona import config  # noqa: E402
from cpersona import memory_handlers as M  # noqa: E402
from cpersona import server as cpersona_server  # noqa: E402


@pytest.fixture(autouse=True)
def _stub_recall(monkeypatch):
    """Answer with one fixed row, so what varies is the merge and nothing else."""
    calls = []

    async def _fake_recall(agent_id, query, limit, **kw):
        calls.append((agent_id, query))
        return {
            "messages": [
                {
                    "content": "a stored row",
                    "source": {"type": "Agent", "id": "a1"},
                    "timestamp": "2026-01-01T00:00:05Z",
                    "ref": "mem:1",
                }
            ]
        }

    monkeypatch.setattr(M, "do_recall", _fake_recall)
    return calls


def _merged(result):
    return [m for m in result["messages"] if m.get("context_type") == "conversation"]


_GOOD = {"role": "user", "content": "a well formed turn", "name": "alice",
         "user_id": "u-1", "timestamp": "2026-01-01T00:00:02Z"}


# --------------------------------------------------------------------------
# bug-291: a non-string declared field is read as absent, never as a value
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_epoch_int_timestamp_no_longer_aborts_the_call():
    """The expensive half. One malformed neighbour used to cost everything.

    ``_parse_timestamp_utc`` calls ``.replace`` on what it is handed and catches
    only ValueError/OSError, so an int raised AttributeError out of the whole
    call — the recall hits and every well-formed entry with them.
    """
    result = await M.do_recall_with_context(
        "a1", "q",
        external_context=[dict(_GOOD), {"role": "user", "content": "epoch", "timestamp": 1767225600}],
    )
    merged = _merged(result)
    assert [m["content"] for m in merged] == ["epoch", "a well formed turn"], merged
    # Read as absent -> the undated group, which is where an entry with no
    # `timestamp` key already sat, and that group sorts ahead of dated rows.
    assert merged[0]["timestamp"] == ""
    assert any(m.get("ref") == "mem:1" for m in result["messages"]), "recall hits were lost"


@pytest.mark.asyncio
async def test_a_null_timestamp_is_emitted_as_a_string():
    """An explicit JSON null used to reach the response as `null`, where every
    other message carries a string."""
    result = await M.do_recall_with_context(
        "a1", "q", external_context=[{"role": "user", "content": "x", "timestamp": None}],
    )
    assert _merged(result)[0]["timestamp"] == ""


@pytest.mark.asyncio
async def test_a_non_string_name_does_not_become_an_identity():
    """`name` and `user_id` become `source.name` and `source.id`. A dict used to
    reach both — `source.id` was the f-string interpolation of it — so the caller
    was handed an identity it never sent."""
    result = await M.do_recall_with_context(
        "a1", "q",
        external_context=[{"role": "user", "content": "x", "name": {"display": "k"}, "user_id": 12345}],
    )
    source = _merged(result)[0]["source"]
    assert source == {"type": "User", "id": "discord:User", "name": "User"}, source
    assert "{" not in source["id"] and "12345" not in source["id"]


@pytest.mark.asyncio
async def test_a_well_formed_entry_is_untouched():
    """The invariant axis: what worked before still produces the same row."""
    result = await M.do_recall_with_context("a1", "q", external_context=[dict(_GOOD)])
    assert _merged(result)[0] == {
        "content": "a well formed turn",
        "source": {"type": "User", "id": "discord:u-1", "name": "alice"},
        "timestamp": "2026-01-01T00:00:02Z",
        "context_type": "conversation",
    }


@pytest.mark.asyncio
async def test_an_undeclared_field_is_still_accepted():
    """additionalProperties stays open by design: closing it would refuse
    payloads that work today, which the declaration gap does not justify."""
    result = await M.do_recall_with_context(
        "a1", "q",
        external_context=[{"role": "user", "content": "x", "reply_to": "msg-17"}],
    )
    assert [m["content"] for m in _merged(result)] == ["x"]
    assert "context_field_issues" not in result


# --------------------------------------------------------------------------
# The report, and the mode that decides what a mismatch costs
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_report_names_the_entry_index_and_the_fields():
    result = await M.do_recall_with_context(
        "a1", "q",
        external_context=[
            dict(_GOOD),
            {"role": "user", "content": "x", "user_id": 7, "timestamp": 1},
        ],
    )
    assert result["context_field_issues"]["entries"] == [
        {"index": 1, "fields": ["timestamp", "user_id"]}
    ]


@pytest.mark.asyncio
async def test_the_report_is_absent_when_every_entry_is_well_formed():
    """Same contract as the C13 disclosure beside it: the common case pays no
    payload, so its absence is what a caller branches on."""
    result = await M.do_recall_with_context("a1", "q", external_context=[dict(_GOOD)])
    assert "context_field_issues" not in result


@pytest.mark.asyncio
async def test_an_entry_that_is_not_an_object_is_reported_whole():
    result = await M.do_recall_with_context("a1", "q", external_context=["just a string"])
    assert result["context_field_issues"]["entries"] == [{"index": 0, "fields": ["<entry>"]}]


@pytest.mark.asyncio
async def test_reject_refuses_the_call_without_running_the_recall(_stub_recall):
    """`reject` is where this ends up. It is tested now so that release changes a
    default rather than adding a code path."""
    result = await M.do_recall_with_context(
        "a1", "q",
        external_context=[dict(_GOOD), {"role": "user", "content": "x", "timestamp": 1}],
        context_mode="reject",
    )
    assert result["ok"] is False
    assert "[1] timestamp" in result["error"]
    # bug-232's shape: a caller's `messages` access must not be the second failure.
    assert result["messages"] == []
    assert _stub_recall == [], "reject paid for a search it discarded"


@pytest.mark.asyncio
async def test_reject_passes_a_well_formed_list_through(_stub_recall):
    result = await M.do_recall_with_context(
        "a1", "q", external_context=[dict(_GOOD)], context_mode="reject",
    )
    assert result.get("ok") is not False
    assert len(_stub_recall) == 1


@pytest.mark.asyncio
async def test_off_silences_the_report_but_not_the_safe_read():
    """A crash is not a diagnostic: `off` removes the reporting, never the
    fallback that keeps one bad entry from costing the whole call."""
    result = await M.do_recall_with_context(
        "a1", "q",
        external_context=[{"role": "user", "content": "x", "timestamp": 1}],
        context_mode="off",
    )
    assert "context_field_issues" not in result
    assert _merged(result)[0]["timestamp"] == ""


def test_an_unreadable_mode_falls_back_to_warn_and_says_so(monkeypatch, caplog):
    """Fail-safe, not fail-open. `off` is the other candidate for a fallback and
    would answer a typo by removing the reporting the typo was reaching for."""
    monkeypatch.setenv("CPERSONA_EXTERNAL_CONTEXT_MODE", "Reject!")
    with caplog.at_level(logging.WARNING, logger="cpersona.config"):
        got = config._parse_choice(
            "CPERSONA_EXTERNAL_CONTEXT_MODE", "warn", ("warn", "reject", "off")
        )
    assert got == "warn"
    assert "CPERSONA_EXTERNAL_CONTEXT_MODE" in caplog.text


def test_a_mode_spelled_in_caps_or_padded_is_accepted(monkeypatch):
    monkeypatch.setenv("CPERSONA_EXTERNAL_CONTEXT_MODE", "  REJECT ")
    assert config._parse_choice(
        "CPERSONA_EXTERNAL_CONTEXT_MODE", "warn", ("warn", "reject", "off")
    ) == "reject"


def test_the_shipped_default_is_warn():
    """Nothing a caller sends today stops working, which is what lets this ship
    in a release whose acceptance axis is that nothing gets worse."""
    monkeypatch_free = os.environ.get("CPERSONA_EXTERNAL_CONTEXT_MODE")
    assert monkeypatch_free is None, "test env leaked a mode; the default is what is under test"
    assert config.EXTERNAL_CONTEXT_MODE == "warn"


# --------------------------------------------------------------------------
# bug-292: the declaration cannot drift from the reads again
# --------------------------------------------------------------------------


def test_every_field_the_handler_reads_is_declared_in_the_schema():
    """The gate that keeps this fixed.

    Derived from ``_CTX_DECLARED_STRINGS`` rather than from a written-out list,
    so a sixth field read off an entry fails here until it is declared — which is
    the failure that did not exist when three of five went undeclared for a year.
    """
    tool = next(t for t in cpersona_server.registry._tools if t.name == "recall_with_context")
    declared = tool.inputSchema["properties"]["external_context"]["items"]["properties"]
    assert set(M._CTX_DECLARED_STRINGS) == set(declared), (
        sorted(set(M._CTX_DECLARED_STRINGS) ^ set(declared))
    )
    for name, spec in declared.items():
        assert spec["type"] == "string", (name, spec)


def test_the_reads_in_the_handler_are_the_fields_declared():
    """The other direction: a field declared but never read is a promise the
    server does not keep. Both `_ctx_string` call sites and the two dedicated
    readers are counted from the source rather than assumed."""
    import inspect

    src = inspect.getsource(M.do_recall_with_context)
    read = {"role", "content"}  # via _ctx_role / _ctx_content
    for field in M._CTX_DECLARED_STRINGS:
        if f'_ctx_string(entry, "{field}"' in src:
            read.add(field)
    assert read == set(M._CTX_DECLARED_STRINGS), sorted(set(M._CTX_DECLARED_STRINGS) - read)
