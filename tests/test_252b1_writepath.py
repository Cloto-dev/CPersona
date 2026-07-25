"""2.5.2b1 write-path and context-filter findings (CSC #361 items C12 / C13 / (6)).

Three defects that share one shape: a write path (or a filter) that was less
careful than its sibling, with nothing in the response admitting it.

- C12: ``content`` has been capped since 2.1; its JSON sidecars (``source`` /
  ``metadata``) and the episode's text fields never were, so one call could
  park an unbounded blob per row and ship it to the embedding backend.
- C13: every ``external_context`` entry filters recall, but only user /
  assistant entries are merged into the response — so a system entry could
  suppress a memory invisibly.
- (6): the ``/index`` push on the WRITE hot path inherited the embed client's
  30s default while every sibling remote call named a short deadline.
"""

import json

import pytest
import pytest_asyncio

from cpersona import memory_handlers, vector
from cpersona._vendored_mcp_common import no_persist
from cpersona.config import MAX_CONTENT_LENGTH, MAX_METADATA_LENGTH, REMOTE_INDEX_TIMEOUT_SECS
from cpersona.database import get_db


@pytest_asyncio.fixture
async def clean_db():
    no_persist.resume()
    db = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await db.execute(f"DELETE FROM {table}")
    await db.execute(
        "DELETE FROM sqlite_sequence WHERE name IN "
        "('memories','episodes','profiles','pending_memory_tasks')"
    )
    await db.commit()
    return db


# ---------------------------------------------------------------------------
# C12: the JSON sidecars are bounded, and the bound refuses rather than truncates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oversized_metadata_is_rejected_and_writes_nothing(clean_db):
    res = await memory_handlers.do_store(
        "c12", {"content": "ordinary", "metadata": {"blob": "x" * (MAX_METADATA_LENGTH + 100)}}
    )
    assert res["result"] == "rejected" and res["ok"] is False, res
    assert "metadata too large" in res["reason"], res
    rows = await clean_db.execute_fetchall("SELECT COUNT(*) FROM memories WHERE agent_id = 'c12'")
    assert rows[0][0] == 0, "a rejected write must leave no row"


@pytest.mark.asyncio
async def test_oversized_source_is_rejected(clean_db):
    res = await memory_handlers.do_store(
        "c12",
        {
            "content": "ordinary",
            "source": {"type": "Agent", "id": "a", "name": "n" * (MAX_METADATA_LENGTH + 100)},
        },
    )
    assert res["result"] == "rejected", res
    assert "source too large" in res["reason"], res


@pytest.mark.asyncio
async def test_metadata_at_the_cap_still_stores(clean_db):
    """The bound refuses only what exceeds it — an honest producer never meets
    the cap, and the boundary itself must not become an off-by-one refusal."""
    payload = {"blob": "x" * 100}
    res = await memory_handlers.do_store("c12", {"content": "fits", "metadata": payload})
    assert res["result"] == "stored", res
    row = await clean_db.execute_fetchall("SELECT metadata FROM memories WHERE id = ?", (res["id"],))
    assert json.loads(row[0][0]) == payload


@pytest.mark.asyncio
async def test_episode_summary_is_capped_and_reported(clean_db):
    """Episode text is prose, so it follows the memory rule: truncate to the
    content cap and say so — the same `truncated` signal do_store gives."""
    long_summary = "S" * (MAX_CONTENT_LENGTH + 500)
    res = await memory_handlers.do_archive_episode("c12", [], summary=long_summary)
    assert res["ok"] is True and res.get("truncated") is True, res
    row = await clean_db.execute_fetchall(
        "SELECT summary FROM episodes WHERE id = ?", (res["episode_id"],)
    )
    assert len(row[0][0]) == MAX_CONTENT_LENGTH


@pytest.mark.asyncio
async def test_episode_keywords_are_capped_too(clean_db):
    """bug-170: the C12 cap covers both episode text fields, but only `summary`
    had a test — deleting the keywords sanitize call left the suite green."""
    res = await memory_handlers.do_archive_episode(
        "c12", [], summary="short", keywords="K" * (MAX_CONTENT_LENGTH + 300)
    )
    assert res["ok"] is True and res.get("truncated") is True, res
    row = await clean_db.execute_fetchall(
        "SELECT keywords FROM episodes WHERE id = ?", (res["episode_id"],)
    )
    assert len(row[0][0]) == MAX_CONTENT_LENGTH


@pytest.mark.asyncio
async def test_episode_keywords_are_sanitized_too(clean_db):
    """The other half of the same call: annotations are stripped from keywords,
    not just from summary."""
    res = await memory_handlers.do_archive_episode(
        "c12", [], summary="short", keywords="[Memory from foo] real keywords"
    )
    row = await clean_db.execute_fetchall(
        "SELECT keywords FROM episodes WHERE id = ?", (res["episode_id"],)
    )
    assert row[0][0] == "real keywords", row


@pytest.mark.asyncio
async def test_episode_remote_index_receives_the_stored_text(clean_db, monkeypatch):
    """The vector and the row must describe the same text: indexing the
    caller's original after storing a capped copy would make recall score a
    string the database does not contain."""
    pushed: list[dict] = []

    async def fake_upsert(agent_id, items):
        pushed.extend(items)

    monkeypatch.setattr(vector, "remote_index_upsert", fake_upsert)
    res = await memory_handlers.do_archive_episode(
        "c12", [], summary="T" * (MAX_CONTENT_LENGTH + 42)
    )
    row = await clean_db.execute_fetchall(
        "SELECT summary FROM episodes WHERE id = ?", (res["episode_id"],)
    )
    assert pushed and pushed[0]["text"] == row[0][0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "summary",
    ["[Memory from foo]", "   ", "\n\t "],
    ids=["annotation-only", "spaces", "whitespace"],
)
async def test_summary_that_sanitizes_to_empty_is_refused(clean_db, summary):
    """bug-162: the guard read the RAW summary, so text that sanitizes to empty
    cleared it and an empty-summary episode was written answering ok:true — the
    same input do_store refuses with result:'rejected'. The refusal must keep
    bug-006's shape (an explicit failure), not raise out of the handler."""
    res = await memory_handlers.do_archive_episode("b162", [], summary=summary)

    assert res["ok"] is False, res
    assert res["episode_id"] is None, res
    rows = await clean_db.execute_fetchall(
        "SELECT COUNT(*) FROM episodes WHERE agent_id = 'b162'"
    )
    assert rows[0][0] == 0, "a refused archive must leave no row"


@pytest.mark.asyncio
async def test_summary_that_survives_sanitization_still_stores(clean_db):
    """The boundary refuses only what sanitizes away: an annotation carrying
    real prose keeps the prose, and that episode must still be archived."""
    res = await memory_handlers.do_archive_episode(
        "b162", [], summary="[Memory from foo] the actual summary"
    )
    assert res["ok"] is True, res
    row = await clean_db.execute_fetchall(
        "SELECT summary FROM episodes WHERE id = ?", (res["episode_id"],)
    )
    assert row[0][0] == "the actual summary"


@pytest.mark.asyncio
async def test_truncated_reports_a_cut_not_a_long_argument(clean_db):
    """bug-175: the flag was derived from the RAW length, which still counts the
    annotation the sanitizer strips. Content that fits once stripped was reported
    truncated even though the stored text is the caller's text."""
    body = "x" * MAX_CONTENT_LENGTH
    res = await memory_handlers.do_store("b175", {"content": f"[Memory from foo] {body}"})

    assert res["result"] == "stored", res
    assert res.get("truncated") is not True, "nothing was cut — the annotation was"
    row = await clean_db.execute_fetchall(
        "SELECT content FROM memories WHERE id = ?", (res["id"],)
    )
    assert row[0][0] == body, "the stored text is the caller's, in full"


@pytest.mark.asyncio
async def test_truncated_is_still_reported_when_the_cap_really_cuts(clean_db):
    """The positive control: a genuine cut must keep saying so."""
    res = await memory_handlers.do_store("b175", {"content": "y" * (MAX_CONTENT_LENGTH + 50)})
    assert res["result"] == "stored" and res["truncated"] is True, res
    row = await clean_db.execute_fetchall(
        "SELECT content FROM memories WHERE id = ?", (res["id"],)
    )
    assert len(row[0][0]) == MAX_CONTENT_LENGTH


@pytest.mark.asyncio
async def test_episode_truncated_follows_the_same_definition(clean_db):
    """The third copy of the expression lived on the episode path."""
    res = await memory_handlers.do_archive_episode(
        "b175", [], summary=f"[Memory from foo] {'z' * MAX_CONTENT_LENGTH}"
    )
    assert res["ok"] is True and res.get("truncated") is not True, res


@pytest.mark.asyncio
async def test_prepare_still_raises_for_the_queue_drain(clean_db):
    """The drain (tasks.py) routes a summary-less payload to retry/discard by
    catching this ValueError, so the shared seam must keep raising even though
    do_archive_episode now refuses earlier."""
    with pytest.raises(ValueError):
        await memory_handlers._prepare_episode_row("b162", [], summary="[Memory from foo]")


@pytest.mark.asyncio
async def test_id_hydration_crosses_its_chunk_boundary(clean_db, monkeypatch):
    """bug-171: the batching added for bug-158 exists to stay under SQLite's
    bound-variable ceiling, but no test ever produced more ids than one chunk —
    restricting the loop to its first chunk left the suite green. Shrinking the
    chunk reaches the multi-statement path without seeding 500 rows."""
    for i in range(5):
        await memory_handlers.do_store("b171", {"content": f"row {i}"})
    rows = await clean_db.execute_fetchall(
        "SELECT id FROM memories WHERE agent_id = 'b171' ORDER BY id"
    )
    ids = [r[0] for r in rows]
    assert len(ids) == 5

    monkeypatch.setattr(vector, "_ID_FETCH_CHUNK", 2)
    fetched = await vector._fetch_rows_by_id(
        clean_db,
        "SELECT id, content FROM memories WHERE id IN ({ph}) AND agent_id = ?",
        ids,
        ("b171",),
    )
    assert sorted(fetched) == ids, "every id must survive the chunk boundary"
    assert {r[1] for r in fetched.values()} == {f"row {i}" for i in range(5)}


@pytest.mark.asyncio
async def test_id_hydration_keeps_duplicate_ids_to_one_row(clean_db, monkeypatch):
    """The map is keyed by id, so a repeated remote hit must not multiply the
    statements it takes to resolve it."""
    await memory_handlers.do_store("b171dup", {"content": "only row"})
    rows = await clean_db.execute_fetchall(
        "SELECT id FROM memories WHERE agent_id = 'b171dup'"
    )
    only = rows[0][0]

    monkeypatch.setattr(vector, "_ID_FETCH_CHUNK", 2)
    fetched = await vector._fetch_rows_by_id(
        clean_db,
        "SELECT id, content FROM memories WHERE id IN ({ph}) AND agent_id = ?",
        [only, only, only],
        ("b171dup",),
    )
    assert list(fetched) == [only], fetched


# ---------------------------------------------------------------------------
# C13: a context entry that filters recall without appearing in the response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_system_context_entry_that_suppresses_a_memory_is_disclosed(clean_db):
    """The suppression itself is correct — the caller already holds that text.
    What was wrong is that nothing said so, and the entry is not in `messages`
    either, so the memory just vanished."""
    await memory_handlers.do_store(
        "c13", {"content": "API key rotation policy is 90 days", "source": {"type": "Agent"}}
    )
    ctx = [
        {
            "role": "system",
            "content": "API key rotation policy is 90 days",
            "timestamp": "2026-07-01T09:59:00+00:00",
        }
    ]
    res = await memory_handlers.do_recall_with_context("c13", query="", external_context=ctx)

    assert res["messages"] == [], "baseline: the entry filters the memory out"
    disclosed = res.get("context_filter_only")
    assert disclosed is not None, "the suppression must not be silent"
    assert disclosed["roles"] == ["system"]


@pytest.mark.asyncio
async def test_conversational_context_alone_adds_no_disclosure(clean_db):
    """user / assistant entries ARE merged into messages, so their filtering is
    self-evident; the disclosure would be pure payload."""
    ctx = [
        {"role": "user", "content": "hello", "timestamp": "2026-07-01T09:00:00+00:00"},
        {"role": "assistant", "content": "hi", "timestamp": "2026-07-01T09:00:01+00:00"},
    ]
    res = await memory_handlers.do_recall_with_context("c13", query="", external_context=ctx)
    assert "context_filter_only" not in res, res
    assert len(res["messages"]) == 2


@pytest.mark.asyncio
async def test_entry_without_a_role_is_disclosed_as_unset(clean_db):
    ctx = [{"content": "orphan note", "timestamp": "2026-07-01T09:00:00+00:00"}]
    res = await memory_handlers.do_recall_with_context("c13", query="", external_context=ctx)
    assert res["context_filter_only"]["roles"] == ["(unset)"], res


@pytest.mark.asyncio
async def test_mixed_type_roles_do_not_abort_the_recall(clean_db):
    """bug-163: the disclosure collects the roles into a set and sorts it, so an
    int role beside a string one raised TypeError and took the entire call down —
    the bug-035 class (a malformed entry turning the tool into an opaque error),
    re-entered through `role`. Report the malformed role instead of dying on it."""
    ctx = [
        {"role": "system", "content": "sys note", "timestamp": "2026-07-01T09:00:00+00:00"},
        {"role": 7, "content": "numeric role", "timestamp": "2026-07-01T09:00:01+00:00"},
    ]
    res = await memory_handlers.do_recall_with_context("b163", query="", external_context=ctx)
    assert res["context_filter_only"]["roles"] == ["7", "system"], res


@pytest.mark.asyncio
async def test_unhashable_role_does_not_abort_the_recall(clean_db):
    """The same coercion covers the other half: a dict/list role is unhashable,
    so it broke while the set was being built, one step before sorted()."""
    ctx = [
        {"role": {"nested": "d"}, "content": "unhashable", "timestamp": "2026-07-01T09:00:00+00:00"},
    ]
    res = await memory_handlers.do_recall_with_context("b163", query="", external_context=ctx)
    assert res["context_filter_only"]["roles"] == ["{'nested': 'd'}"], res


@pytest.mark.asyncio
async def test_a_non_string_role_is_never_read_as_conversational(clean_db):
    """Coercion must not open a second door: the merge path admits only the
    literal strings, so a stringified role stays filter-only and out of messages."""
    ctx = [
        {"role": ["user"], "content": "list role", "timestamp": "2026-07-01T09:00:00+00:00"},
    ]
    res = await memory_handlers.do_recall_with_context("b163", query="", external_context=ctx)
    assert res["messages"] == [], res
    assert res["context_filter_only"]["roles"] == ["['user']"], res


# ---------------------------------------------------------------------------
# (6): the write hot path states its own deadline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_index_push_states_its_timeout(clean_db, monkeypatch):
    """Without an explicit timeout the POST inherits the embed client's 30s
    default, so a hung endpoint blocks every store for 30s — the read path has
    been protected since bug-033 and the write path was not."""
    captured: dict = {}

    class _Resp:
        def raise_for_status(self):
            return None

    class _Client:
        async def post(self, url, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs
            return _Resp()

    class _EmbedClient:
        _http_url = "http://embed.invalid/embed"
        _client = _Client()

        async def embed(self, texts):
            return [None]

        @staticmethod
        def pack_embedding(vec):
            return b""

    monkeypatch.setattr(memory_handlers, "VECTOR_SEARCH_MODE", "remote")
    monkeypatch.setattr(vector, "_embedding_client", _EmbedClient())

    res = await memory_handlers.do_store("c6", {"content": "indexed row"})
    assert res["result"] == "stored", res
    assert captured["url"].endswith("/index")
    assert captured["kwargs"].get("timeout") == REMOTE_INDEX_TIMEOUT_SECS, captured["kwargs"]


# ---------------------------------------------------------------------------
# C23: one failure shape across the tool surface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_representative_failures_all_answer_ok_false(clean_db):
    """Spot-check across three modules — an ownership/lookup failure, a
    validation failure and an argument failure. Before b1 each of these
    returned a bare {'error': ...}, so `resp.get("ok")` was None for all of
    them while a successful call returned True."""
    from cpersona import admin_handlers, memory_handlers

    failures = [
        await admin_handlers.do_delete_memory(999_999, agent_id="c23"),
        await admin_handlers.do_update_memory(999_999, "new text", agent_id="c23"),
        await admin_handlers.do_merge_memories("same", "same"),
        await memory_handlers.do_get_contents("", ["mem:1"]),
        await memory_handlers.do_get_contents("c23", []),
    ]
    for resp in failures:
        assert resp["ok"] is False, resp
        assert resp["error"], resp


@pytest.mark.asyncio
async def test_error_helper_keeps_the_per_tool_fields(clean_db):
    """Unification must not flatten failures that owe their caller more than a
    message — archive_episode's caller keys on episode_id."""
    from cpersona import server

    resp = await server.do_archive_episode_or_queue("c23", [], summary="")
    assert resp["ok"] is False and resp["episode_id"] is None, resp
