"""Regression tests: crashes on reachable input.

Four inputs that reached an unhandled exception instead of the documented answer:

  bug-511  a deeply nested JSON body from the embedding backend raised
           RecursionError out of the client's failure boundary, so store wrote
           nothing and recall did not fall back to keyword search.
  bug-532  a record ref or id beyond SQLite's signed 64-bit range raised
           OverflowError when bound. On store this happened after the memory had
           committed, so the caller was told the write failed while the row existed
           unlocked; the same id failed declare_associations, get_contents and the
           by-id tools with a bare error.
  bug-577  reconstruct assumed a candidate's source id and type were strings; a
           row stored with an integer id, or a null type next to a string one,
           failed every reconstruct whose candidate pool held it.
  bug-586  a well-formed stamp at the edge of the datetime range whose offset moves
           its UTC instant out of range raised OverflowError out of the shared
           timestamp parser, the format-drift checks and the time-cue window.
"""

import json
import os
import sqlite3
import tempfile
from datetime import datetime

os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "test_2612a1_u13.db"))
os.environ.setdefault("CPERSONA_EMBEDDING_MODE", "none")

import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from cpersona import (  # noqa: E402
    admin_handlers,
    associations,
    checks,
    cue,
    generation,
    memory_handlers,
    reconstruct,
    server,
    session,
    utils,
    vector,
)
from cpersona._vendored_mcp_common.embedding_client import EmbeddingClient  # noqa: E402
from cpersona.database import get_db  # noqa: E402


@pytest_asyncio.fixture
async def db():
    session.reset_pauses_for_tests()
    conn = await get_db()
    for table in ("memories", "episodes", "profiles", "relations", "entity_mentions", "entity_aliases", "entities"):
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()
    return conn


# ==========================================================================
# bug-511 — RecursionError from a deeply nested backend body.
# ==========================================================================

# Far below the 64 MiB response budget, far above the decoder's nesting limit.
_DEPTH = 100_000
_NESTED_EMBED = ('{"embeddings":' + "[" * _DEPTH + "]" * _DEPTH + "}").encode()
_NESTED_PLAIN = ("[" * _DEPTH + "]" * _DEPTH).encode()


def _nested_client(*, embed_status=200, capabilities="404", max_response_bytes=None):
    def handler(request):
        path = request.url.path
        if path.endswith("/capabilities"):
            if capabilities == "nested":
                return httpx.Response(200, content=_NESTED_PLAIN, headers={"content-type": "application/json"})
            return httpx.Response(404)
        if path.endswith("/count_tokens"):
            return httpx.Response(200, content=_NESTED_PLAIN, headers={"content-type": "application/json"})
        if embed_status != 200:
            return httpx.Response(embed_status)
        return httpx.Response(200, content=_NESTED_EMBED, headers={"content-type": "application/json"})

    kwargs = {} if max_response_bytes is None else {"max_response_bytes": max_response_bytes}
    client = EmbeddingClient(mode="http", http_url="http://embedding.invalid/embed", **kwargs)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize("max_response_bytes", [None, 0], ids=["budgeted", "unbudgeted"])
async def test_a_deeply_nested_embed_body_is_a_failed_outcome_not_an_escape(max_response_bytes):
    vectors, outcome = await _nested_client(max_response_bytes=max_response_bytes).embed_with_outcome(["x"])
    assert vectors is None
    assert outcome.attempted is True and outcome.ok is False, outcome
    assert "nest" in (outcome.error or ""), outcome


@pytest.mark.asyncio
async def test_a_deeply_nested_capability_report_is_a_failed_outcome():
    report, outcome = await _nested_client(capabilities="nested").capabilities_with_outcome()
    assert report is None
    assert outcome.attempted is True and outcome.ok is False, outcome


@pytest.mark.asyncio
async def test_a_deeply_nested_token_report_is_a_failed_outcome():
    report, outcome = await _nested_client().count_tokens_with_outcome(["x"])
    assert report is None
    assert outcome.attempted is True and outcome.ok is False, outcome


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["http500", "nested_embed", "nested_capabilities"])
async def test_store_under_a_nested_backend_body_stores_the_row_without_a_vector(db, monkeypatch, fault):
    """The HTTP 500 case is the control: the nested body must degrade the same way."""
    if fault == "http500":
        client = _nested_client(embed_status=500)
    elif fault == "nested_embed":
        client = _nested_client()
    else:
        client = _nested_client(capabilities="nested", embed_status=500)
    generation.reset()
    monkeypatch.setattr(vector, "_embedding_client", client)
    try:
        result = await memory_handlers.do_store("agent.u13.511", {"content": f"nested body probe {fault}"})
    finally:
        generation.reset()
    assert result.get("result") == "stored", result
    rows = await db.execute_fetchall(
        "SELECT embedding FROM memories WHERE agent_id = ?", ("agent.u13.511",)
    )
    assert len(rows) == 1 and rows[0][0] is None, rows


# ==========================================================================
# bug-532 — a ref or id beyond SQLite's 64-bit range.
# ==========================================================================

HUGE = 99999999999999999999  # > 2**63 - 1
HUGE_REF = f"mem:{HUGE}"


@pytest.mark.asyncio
async def test_store_with_an_oversized_ref_reports_it_and_keeps_the_rest_of_the_call(db):
    out = await server.do_store_boundary(
        "agent.u13.532",
        content="melon contract signed with ACME",
        lock=True,
        associations={
            "entities": [{"name": "ACME"}],
            "relations": [{"subject": HUGE_REF, "predicate": "signed", "object": "ACME"}],
        },
    )
    assert out.get("result") == "stored", out
    assert isinstance(out.get("id"), int) and out["id"] > 0, out
    assert out.get("locked") is True, out
    report = out["associations"]
    assert [e["name"] for e in report["entities"]] == ["ACME"], report
    assert {"item": "relations[0].subject", "reason": f"{HUGE_REF} is not a record of this agent"} in report[
        "dropped"
    ], report
    # The oversized ref is not registered as an entity named after it.
    names = [r[0] for r in await db.execute_fetchall("SELECT name FROM entities")]
    assert names == ["ACME"], names
    locked = await db.execute_fetchall("SELECT locked FROM memories WHERE id = ?", (out["id"],))
    assert locked == [(1,)], locked


@pytest.mark.asyncio
async def test_declare_associations_drops_an_oversized_anchor_and_retract_id(db):
    out = await server.do_declare_associations_boundary(
        "agent.u13.532",
        associations={"entities": [{"name": "Alpha"}]},
        anchor_ref=HUGE_REF,
        retract={"relations": [HUGE, -HUGE], "mentions": [{"entity": HUGE, "ref": "mem:1"}]},
    )
    assert "error" not in out, out
    dropped = json.dumps(out)
    assert "anchor_ref" in dropped and "retract.relations[0]" in dropped and "retract.relations[1]" in dropped
    assert "retract.mentions[0]" in dropped, out


@pytest.mark.asyncio
async def test_get_contents_reports_an_oversized_ref_as_missing_and_serves_the_rest(db):
    stored = await memory_handlers.do_store("agent.u13.532", {"content": "a row that get_contents should serve"})
    ref = f"mem:{stored['id']}"
    out = await memory_handlers.do_get_contents("agent.u13.532", [ref, HUGE_REF, f"ep:{HUGE}"])
    assert [item["ref"] for item in out.get("items", [])] == [ref], out
    assert set(out.get("missing", [])) == {HUGE_REF, f"ep:{HUGE}"}, out


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call",
    [
        lambda i: admin_handlers.do_delete_memory(i, agent_id="agent.u13.532"),
        lambda i: admin_handlers.do_update_memory(i, "new text", agent_id="agent.u13.532"),
        lambda i: admin_handlers.do_lock_memory(i, agent_id="agent.u13.532"),
        lambda i: admin_handlers.do_unlock_memory(i, agent_id="agent.u13.532"),
        lambda i: admin_handlers.do_delete_episode(i, agent_id="agent.u13.532"),
        lambda i: admin_handlers.do_delete_memory(i),
    ],
    ids=["delete_memory", "update_memory", "lock_memory", "unlock_memory", "delete_episode", "delete_unscoped"],
)
@pytest.mark.parametrize("row_id", [2**63, -(2**63) - 1])
async def test_by_id_tools_answer_not_found_for_an_id_sqlite_cannot_hold(db, call, row_id):
    out = await call(row_id)
    assert "error" in out, out
    assert "not found" in out["error"].lower(), out


@pytest.mark.asyncio
async def test_a_failing_associations_rider_does_not_replace_the_store_answer(db, monkeypatch):
    async def busy(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(server.associations_module, "declare", busy)
    out = await server.do_store_boundary(
        "agent.u13.532",
        content="a committed write must be reported as one",
        lock=True,
        associations={"entities": [{"name": "Beta"}]},
    )
    assert out.get("result") == "stored" and isinstance(out.get("id"), int), out
    report = out["associations"]
    assert report["entities"] == [] and report["relations"] == [], report
    assert report["dropped"][0]["item"] == "associations", report
    assert "database is locked" in report["dropped"][0]["reason"], report
    # The lock rider still ran.
    assert out.get("locked") is True, out


@pytest.mark.asyncio
async def test_a_failing_lock_rider_reports_locked_false(db, monkeypatch):
    async def busy(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(server, "do_lock_memory", busy)
    out = await server.do_store_boundary(
        "agent.u13.532", content="a committed write with a failed lock", lock=True
    )
    assert out.get("result") == "stored" and isinstance(out.get("id"), int), out
    assert out.get("locked") is False, out


def test_parse_ref_keeps_in_range_ids_and_refuses_oversized_ones():
    assert associations.parse_ref(f"mem:{2**63 - 1}") == ("mem", 2**63 - 1)
    assert associations.parse_ref(f"mem:{2**63}") is None
    assert associations.parse_ref("mem:0") == ("mem", 0)


# ==========================================================================
# bug-577 — a non-string source id or type on a reconstruct candidate.
# ==========================================================================


async def _store_with_sources(agent, sources):
    for i, source in enumerate(sources):
        out = await server.do_store_boundary(
            agent,
            message={
                "content": f"orchard kiwi note number {i}",
                "source": source,
                "timestamp": f"2026-03-01T10:0{i}:00+00:00",
            },
        )
        assert out.get("result") == "stored", out


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sources",
    [
        [{"type": "User", "id": 12345, "name": "bob"}],
        [{"User": {"id": 42, "name": "n"}}],
        [{"type": None, "id": "discord:1"}, {"type": "User", "id": "discord:1"}],
        [{"type": 7, "id": "discord:2"}, {"type": "User", "id": "discord:2"}],
    ],
    ids=["int_id", "serde_int_id", "null_type_next_to_string", "int_type_next_to_string"],
)
async def test_reconstruct_answers_for_rows_with_non_string_source_fields(db, fake_embedding_client, sources):
    agent = "agent.u13.577"
    await _store_with_sources(agent, sources)
    for query in ("", "orchard kiwi note"):
        out = await reconstruct.do_reconstruct(agent, query)
        assert "error" not in out, out
        quoted = json.dumps(out.get("items", []))
        assert "orchard kiwi note number 0" in quoted, out


def test_candidate_source_fields_are_strings():
    candidate = reconstruct._Candidate(
        {"ref": "mem:1", "content": "x", "source": {"type": 7, "id": 12345}}, 0
    )
    assert candidate.source_id == "12345" and candidate.source_type == "7"
    candidate = reconstruct._Candidate({"ref": "mem:1", "content": "x", "source": {"type": None, "id": None}}, 0)
    assert candidate.source_id == "" and candidate.source_type == ""


# ==========================================================================
# bug-586 — a stamp whose offset carries its UTC instant out of range.
# ==========================================================================

BAD_HI = "9999-12-31T23:59:59-05:00"
BAD_LO = "0001-01-01T00:00:00+01:00"


@pytest.mark.parametrize("stamp", [BAD_HI, BAD_LO])
def test_the_shared_parser_reads_an_out_of_range_instant_as_unparseable(stamp):
    assert utils._parse_timestamp_utc(stamp) is None
    assert utils.future_timestamp_issue(stamp) is None


def test_the_shared_parser_still_reads_the_edges_it_can_represent():
    assert utils._parse_timestamp_utc("9999-12-31T23:59:59+00:00") == datetime.fromisoformat(
        "9999-12-31T23:59:59+00:00"
    )
    assert utils._parse_timestamp_utc("0001-01-01T05:00:00+01:00") == datetime.fromisoformat(
        "0001-01-01T04:00:00+00:00"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stamp", [BAD_HI, BAD_LO])
async def test_store_with_an_out_of_range_instant_answers_a_result(db, stamp):
    out = await memory_handlers.do_store("agent.u13.586", {"content": f"edge stamp {stamp}", "timestamp": stamp})
    assert out.get("result") == "stored", out


@pytest.mark.asyncio
async def test_archive_episode_keeps_the_episode_when_a_history_stamp_is_out_of_range(db):
    out = await memory_handlers.do_archive_episode(
        "agent.u13.586",
        [
            {"role": "user", "content": "hi", "timestamp": BAD_LO},
            {"role": "user", "content": "there", "timestamp": "2026-02-01T00:00:00+00:00"},
        ],
        summary="a summary of an edge stamp session",
    )
    assert out.get("ok") is True, out
    rows = await db.execute_fetchall("SELECT start_time FROM episodes WHERE agent_id = ?", ("agent.u13.586",))
    assert rows == [("2026-02-01T00:00:00+00:00",)], rows


@pytest.mark.asyncio
async def test_recall_with_context_places_an_out_of_range_entry_instead_of_failing(db):
    out = await memory_handlers.do_recall_with_context(
        "agent.u13.586",
        "hello",
        external_context=[
            {"role": "user", "content": "hello edge", "timestamp": BAD_LO},
            {"role": "user", "content": "hello dated", "timestamp": "2026-02-01T00:00:00+00:00"},
        ],
    )
    assert "error" not in out, out
    assert "hello edge" in json.dumps(out) and "hello dated" in json.dumps(out), out


@pytest.mark.asyncio
async def test_import_of_one_out_of_range_memory_stamp_does_not_abort_the_restore(db, tmp_path):
    for i in range(3):
        await memory_handlers.do_store(
            "agent.u13.586src", {"content": f"good memory {i}", "timestamp": f"2026-01-0{i + 1}T00:00:00+00:00"}
        )
    exported = tmp_path / "export.jsonl"
    out = await admin_handlers.do_export_memories("agent.u13.586src", str(exported))
    assert out.get("ok") is True, out
    lines, changed = [], False
    for line in exported.read_text().splitlines():
        record = json.loads(line)
        if not changed and record.get("_type") == "memory":
            record["timestamp"] = BAD_LO
            changed = True
        lines.append(json.dumps(record))
    assert changed, "the export carried no memory record to edit"
    imported = tmp_path / "import.jsonl"
    imported.write_text("\n".join(lines) + "\n")
    out = await admin_handlers.do_import_memories(str(imported), target_agent_id="agent.u13.586dst")
    assert out.get("ok") is True, out
    rows = await db.execute_fetchall("SELECT COUNT(*) FROM memories WHERE agent_id = ?", ("agent.u13.586dst",))
    assert rows == [(3,)], rows


@pytest.mark.asyncio
async def test_timestamp_drift_checks_skip_an_aware_stamp_they_cannot_convert(db):
    await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp) VALUES (?, ?, ?, ?)",
        ("agent.u13.586", "edge row", "{}", BAD_HI),
    )
    await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp) VALUES (?, ?, ?, ?)",
        ("agent.u13.586", "aware row", "{}", "2026-01-01T09:00:00+09:00"),
    )
    await db.execute(
        "INSERT INTO episodes (agent_id, summary, start_time, end_time) VALUES (?, ?, ?, ?)",
        ("agent.u13.586", "edge episode", BAD_LO, "2026-01-01T09:00:00+09:00"),
    )
    await db.commit()
    memory_issues = await checks.check_timestamp_format_drift(db, "agent.u13.586", fix=False)
    episode_issues = await checks.check_episode_timestamp_format_drift(db, "agent.u13.586", fix=False)
    # The convertible aware stamp is still repairable; the edge one is skipped.
    assert memory_issues and memory_issues[0]["repairable"] == 1, memory_issues
    assert episode_issues and episode_issues[0]["repairable"] == 1, episode_issues


@pytest.mark.parametrize(
    "stamp, expected",
    [(BAD_LO, "0001-01-01 00:00:00"), (BAD_HI, "9999-12-31 23:59:59"), ("2026-01-01T09:00:00+09:00", "2026-01-01 00:00:00")],
)
def test_a_cue_bound_past_the_range_is_clipped_to_the_end_it_passed(stamp, expected):
    assert cue.sql_instant(datetime.fromisoformat(stamp)) == expected


@pytest.mark.asyncio
async def test_recall_with_an_out_of_range_time_cue_bound_answers(db):
    await memory_handlers.do_store(
        "agent.u13.586", {"content": "cue window row", "timestamp": "2026-01-01T00:00:00+00:00"}
    )
    out = await memory_handlers.do_recall(
        "agent.u13.586", "cue window row", 5, time_cue={"after": BAD_LO, "confidence": "sure"}
    )
    assert "error" not in out, out
