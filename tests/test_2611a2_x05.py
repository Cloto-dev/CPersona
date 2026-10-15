"""The store and archive_episode write seam: what reaches the row, and what it answers.

Five defects at the same seam, each pinned on the behaviour a caller sees:

* A lone UTF-16 surrogate -- half of a pair, which a client that slices a
  string by UTF-16 code units sends when it cuts through an emoji -- cannot be
  encoded for SQLite. The bind raised out of store, reconstruct, recall,
  recall_with_context, get_contents and archive_episode, and the tool answered
  with a bare ``{error}`` that has none of the fields the tool documents
  (``result`` for store, ``items`` for reconstruct). Each now refuses in its
  own failure shape and names the field.
* The store tool's legacy ``message`` form stored ``timestamp: ""`` verbatim,
  while its top-level form treats the empty string as omitted. Both now mean
  "now". The library entry point keeps an explicit "" as an undated row, which
  is what an import leaves and what other tests build undated rows with.
* store's dedup checks the scope a recall sees (a bucket write collides with
  an identical global-pool row), but the unique index that backs it up under
  concurrency is exact-bucket. A global-pool copy committed between the probe
  and the insert was invisible to both, so the bucket copy was written beside
  it. The part of the scope the index cannot see is probed again under the
  write lock.
* ``INSERT OR IGNORE`` skips a row that violates NOT NULL as readily as one
  that violates UNIQUE, so a library caller's ``channel=None`` or
  ``timestamp=None`` was dropped and reported as "duplicate (unique index)".
* archive_episode stored the caller's history timestamps without the
  ahead-of-the-clock rule store applies to its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from cpersona import config, database, memory_handlers, server, session
from cpersona.database import get_db

AGENT = "x05.agent"
HALF = "\udfff"  # the low half of a surrogate pair, alone
HIGH = "\ud800"  # the high half, alone


@pytest_asyncio.fixture
async def clean_db(monkeypatch):
    # Fresh lock per test: an asyncio.Lock binds to the first loop that contends it,
    # and the concurrency test below contends it.
    monkeypatch.setattr(database, "_write_lock", asyncio.Lock())
    session.reset_pauses_for_tests()
    db = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()
    yield db
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()


async def _rows(db, agent=AGENT):
    return await db.execute_fetchall(
        "SELECT id, project_id, channel, msg_id, content, timestamp FROM memories"
        " WHERE agent_id = ? ORDER BY id",
        (agent,),
    )


async def _episodes(db, agent=AGENT):
    return await db.execute_fetchall(
        "SELECT id, summary, start_time, end_time FROM episodes WHERE agent_id = ? ORDER BY id",
        (agent,),
    )


# ---------------------------------------------------------------------------
# A lone surrogate is refused in each tool's own failure shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,call",
    [
        ("content", {"content": f"half {HALF} pair"}),
        ("content", {"message": {"content": f"half {HIGH} pair"}}),
        ("id", {"content": "plain text one", "msg_id": f"m{HALF}"}),
        ("channel", {"content": "plain text two", "channel": f"c{HALF}"}),
        ("project_id", {"content": "plain text three", "project_id": f"p{HALF}"}),
        ("timestamp", {"content": "plain text four", "timestamp": f"2026-01-01{HALF}"}),
    ],
)
async def test_store_refuses_a_lone_surrogate_with_a_result(clean_db, field, call):
    res = await server.do_store_boundary(AGENT, **call)
    assert res.get("ok") is False and res.get("result") == "rejected", res
    assert res["reason"].startswith(f"{field} "), res
    assert "surrogate" in res["reason"], res
    assert await _rows(clean_db) == [], "a refused store must leave no row"


@pytest.mark.asyncio
async def test_store_refuses_a_lone_surrogate_in_agent_id(clean_db):
    res = await memory_handlers.do_store(f"a{HALF}", {"content": "plain text five"})
    assert res.get("result") == "rejected" and res["reason"].startswith("agent_id "), res


@pytest.mark.asyncio
async def test_a_whole_surrogate_pair_is_ordinary_text(clean_db):
    """The refusal is for half a pair. A whole one arrives from JSON as one code
    point and is stored like any other character."""
    text = "a whole pair \U0001f600 is fine"
    res = await server.do_store_boundary(AGENT, content=text)
    assert res["result"] == "stored", res
    assert [r[4] for r in await _rows(clean_db)] == [text]


@pytest.mark.asyncio
async def test_reconstruct_refuses_a_lone_surrogate_with_its_collections(clean_db):
    await server.do_store_boundary(AGENT, content="half of the release plan was agreed")
    res = await server.do_reconstruct_boundary(
        AGENT, f"release {HALF}", None, None, None, None, False, "", None, ""
    )
    assert res.get("ok") is False, res
    assert res.get("items") == [] and res.get("returned_count") == 0, res
    assert res["error"].startswith("query ") and "surrogate" in res["error"], res


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["channel", "project_id", "source_id"])
async def test_reconstruct_refuses_a_lone_surrogate_in_a_filter(clean_db, field):
    await server.do_store_boundary(AGENT, content="half of the release plan was agreed")
    args = {"channel": "", "project_id": None, "source_id": ""}
    args[field] = f"x{HALF}"
    res = await server.do_reconstruct_boundary(
        AGENT, "release plan", None, None, None, None, False,
        args["channel"], args["project_id"], args["source_id"],
    )
    assert res.get("ok") is False and res.get("items") == [], res
    assert res["error"].startswith(f"{field} "), res


@pytest.mark.asyncio
async def test_recall_refuses_a_lone_surrogate_with_its_messages(clean_db):
    await server.do_store_boundary(AGENT, content="half of the release plan was agreed")
    res = await server.do_recall_boundary(AGENT, f"release {HALF}", 5, False, "", [], None, "")
    assert res.get("ok") is False and res.get("messages") == [], res
    assert res["error"].startswith("query "), res


@pytest.mark.asyncio
async def test_recall_with_context_passes_the_refusal_on(clean_db):
    """It merges the recall's messages with the caller's context. A refused recall
    has none, and dropping its error would answer as if nothing matched."""
    await server.do_store_boundary(AGENT, content="half of the release plan was agreed")
    res = await server.do_recall_with_context_boundary(
        AGENT, f"release {HALF}", [{"role": "user", "content": "hi"}], 5, "", False, None, ""
    )
    assert res.get("ok") is False and res.get("messages") == [], res
    assert "surrogate" in res["error"], res


@pytest.mark.asyncio
async def test_get_contents_refuses_a_lone_surrogate_agent_id(clean_db):
    stored = await server.do_store_boundary(AGENT, content="a row to fetch")
    res = await memory_handlers.do_get_contents(f"{AGENT}{HALF}", [f"mem:{stored['id']}"])
    assert res.get("ok") is False and res["error"].startswith("agent_id "), res


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,kwargs",
    [
        ("summary", {"summary": f"we cut {HALF} here"}),
        ("keywords", {"summary": "a summary", "keywords": f"k{HALF}"}),
        ("channel", {"summary": "a summary", "channel": f"c{HALF}"}),
        ("timestamp", {"summary": "a summary", "history": [{"role": "user", "content": "x", "timestamp": f"2026{HALF}"}]}),
    ],
)
async def test_archive_episode_refuses_a_lone_surrogate(clean_db, field, kwargs):
    kwargs = {"history": [], **kwargs}
    res = await server.do_archive_episode_boundary(AGENT, **kwargs)
    assert res.get("ok") is False and res.get("episode_id", "missing") is None, res
    assert res["error"].startswith(f"{field} ") and "surrogate" in res["error"], res
    assert await _episodes(clean_db) == []


# ---------------------------------------------------------------------------
# The legacy message form reads an empty timestamp as the flat form does
# ---------------------------------------------------------------------------


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


@pytest.mark.asyncio
async def test_an_empty_legacy_timestamp_means_now_as_in_the_flat_form(clean_db):
    before = datetime.now(timezone.utc)
    legacy = await server.do_store_boundary(AGENT, message={"content": "legacy form", "timestamp": ""})
    flat = await server.do_store_boundary(AGENT, content="flat form", timestamp="")
    after = datetime.now(timezone.utc)
    assert legacy["result"] == "stored" and flat["result"] == "stored", (legacy, flat)
    stamps = {r[4]: r[5] for r in await _rows(clean_db)}
    assert stamps["legacy form"] != "", "an empty stamp was stored verbatim: the row is undated"
    for content in ("legacy form", "flat form"):
        assert before <= _parse(stamps[content]) <= after, (content, stamps)


@pytest.mark.asyncio
async def test_a_supplied_legacy_timestamp_is_still_kept(clean_db):
    await server.do_store_boundary(
        AGENT, message={"content": "kept stamp", "timestamp": "2026-02-03T04:05:06+00:00"}
    )
    assert [r[5] for r in await _rows(clean_db)] == ["2026-02-03T04:05:06+00:00"]


# ---------------------------------------------------------------------------
# The bucket-and-global dedup holds when the global copy lands mid-store
# ---------------------------------------------------------------------------


def _racing(monkeypatch, *, project_id="", channel="", msg_id="", content, source='{}'):
    """Commit a row through its own write before do_store's write seam begins.

    That is the window the concurrency defect lives in: after do_store's dedup
    probe has run on the read connection and before its INSERT. The interloper's
    write completes in its own scope because the write lock is not reentrant.
    """
    real = memory_handlers.transaction
    armed = {"n": 0, "id": None}

    @contextlib.asynccontextmanager
    async def racing_transaction(*args, **kwargs):
        if armed["n"] == 0:
            armed["n"] = 1
            async with real() as racer:
                cur = await racer.execute(
                    "INSERT INTO memories (agent_id, project_id, msg_id, content, source,"
                    " timestamp, metadata, channel) VALUES (?, ?, ?, ?, ?, '2026-01-01T00:00:00+00:00', '{}', ?)",
                    (AGENT, project_id, msg_id, content, source, channel),
                )
                armed["id"] = cur.lastrowid
        async with real(*args, **kwargs) as inner:
            yield inner

    monkeypatch.setattr(memory_handlers, "transaction", racing_transaction)
    return armed


@pytest.mark.asyncio
async def test_a_global_copy_landing_mid_store_answers_a_bucket_write(clean_db, monkeypatch):
    text = "the decision both sessions recorded at once"
    armed = _racing(monkeypatch, content=text)
    res = await memory_handlers.do_store(AGENT, {"content": text}, project_id="X")
    assert armed["n"] == 1, "the race was never armed"
    assert res.get("result") == "skipped" and res.get("reason") == "duplicate content", res
    assert res.get("id") == armed["id"], res
    rows = await _rows(clean_db)
    assert [(r[1], r[2]) for r in rows] == [("", "")], (
        f"recall in X now returns the text twice: {rows}"
    )


@pytest.mark.asyncio
async def test_the_same_holds_on_the_channel_axis(clean_db, monkeypatch):
    text = "a channel copy and an unscoped copy at once"
    armed = _racing(monkeypatch, content=text)
    res = await memory_handlers.do_store(AGENT, {"content": text}, channel="c")
    assert res.get("result") == "skipped" and res.get("id") == armed["id"], res
    assert [(r[1], r[2]) for r in await _rows(clean_db)] == [("", "")]


@pytest.mark.asyncio
async def test_the_same_holds_for_a_message_id(clean_db, monkeypatch):
    armed = _racing(monkeypatch, msg_id="m-1", content="first wording")
    res = await memory_handlers.do_store(
        AGENT, {"id": "m-1", "content": "second wording"}, project_id="X"
    )
    assert res.get("result") == "skipped" and res.get("reason") == "duplicate msg_id", res
    assert res.get("id") == armed["id"], res
    assert [r[4] for r in await _rows(clean_db)] == ["first wording"]


@pytest.mark.asyncio
async def test_a_global_copy_under_another_source_does_not_answer(clean_db, monkeypatch):
    """The late probe makes the decision the early one would have made, no more:
    another principal's copy in the global pool does not answer this write."""
    text = "two principals noted the same thing"
    _racing(monkeypatch, content=text, source=json.dumps({"type": "User", "id": "other", "name": ""}))
    res = await memory_handlers.do_store(
        AGENT, {"content": text, "source": {"type": "User", "id": "me", "name": ""}}, project_id="X"
    )
    assert res.get("result") == "stored", res
    assert sorted(r[1] for r in await _rows(clean_db)) == ["", "X"]


@pytest.mark.asyncio
async def test_a_global_write_is_not_answered_by_a_bucket_copy(clean_db, monkeypatch):
    """A global write probes the global pool only (a bucket copy must not hide the
    row from every other bucket), late as well as early."""
    text = "a bucket copy and then a global one"
    _racing(monkeypatch, project_id="X", content=text)
    res = await memory_handlers.do_store(AGENT, {"content": text})
    assert res.get("result") == "stored", res
    assert sorted(r[1] for r in await _rows(clean_db)) == ["", "X"]


@pytest.mark.asyncio
async def test_an_exact_bucket_race_is_still_the_unique_index(clean_db, monkeypatch):
    """The late probe covers only what the exact-bucket index cannot see. A copy in
    the writer's own bucket is still absorbed by the index, as before."""
    text = "an exact-bucket race"
    _racing(monkeypatch, project_id="X", content=text)
    res = await memory_handlers.do_store(AGENT, {"content": text}, project_id="X")
    assert res.get("result") == "skipped" and "unique index" in res.get("reason", ""), res
    assert [r[1] for r in await _rows(clean_db)] == ["X"]


@pytest.mark.asyncio
async def test_concurrent_bucket_and_global_stores_never_insert_after_the_global_copy(clean_db):
    """Real concurrency, no monkeypatching. Either order of commits is allowed --
    a bucket copy committed first is legitimately joined by a global one, as it
    is sequentially -- but a bucket copy must never be inserted after an
    identical global copy already committed, which is what both probes passing
    and both inserts landing looked like."""
    pairs = 30
    texts = [f"concurrent sentence number {i} about the release plan" for i in range(pairs)]

    def pair(i, text):
        bucket = memory_handlers.do_store(AGENT, {"content": text}, project_id="X")
        unscoped = memory_handlers.do_store(AGENT, {"content": text})
        # Both orders of arrival: the global copy first is the order the defect
        # needs, the bucket copy first is the one that must still store both.
        return (unscoped, bucket) if i % 2 == 0 else (bucket, unscoped)

    await asyncio.gather(*(coro for i, text in enumerate(texts) for coro in pair(i, text)))
    late = []
    for text in texts:
        ids = {
            r[0]: r[1]
            for r in await clean_db.execute_fetchall(
                "SELECT project_id, id FROM memories WHERE agent_id = ? AND content = ?", (AGENT, text)
            )
        }
        assert "" in ids, (text, ids)
        if "X" in ids and ids["X"] > ids[""]:
            late.append(text)
    assert late == [], f"{len(late)}/{pairs} bucket copies were inserted after the global copy"


# ---------------------------------------------------------------------------
# A NOT NULL value is never reported as a duplicate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_none_channel_is_the_default_channel(clean_db):
    res = await memory_handlers.do_store(AGENT, {"content": "first fact"}, channel=None)
    assert res.get("result") == "stored", res
    assert [(r[2], r[4]) for r in await _rows(clean_db)] == [("", "first fact")]


@pytest.mark.asyncio
async def test_a_none_timestamp_is_an_omitted_timestamp(clean_db):
    before = datetime.now(timezone.utc)
    res = await memory_handlers.do_store(AGENT, {"content": "second fact", "timestamp": None})
    assert res.get("result") == "stored", res
    (row,) = await _rows(clean_db)
    assert before <= _parse(row[5]) <= datetime.now(timezone.utc), row


@pytest.mark.asyncio
async def test_a_none_message_id_is_no_message_id(clean_db):
    res = await memory_handlers.do_store(AGENT, {"content": "third fact", "id": None})
    assert res.get("result") == "stored", res
    assert [(r[3], r[4]) for r in await _rows(clean_db)] == [("", "third fact")]


@pytest.mark.asyncio
async def test_archive_episode_reads_a_none_channel_as_the_default(clean_db):
    """The episode INSERT carries no OR IGNORE, so the same None raised a NOT NULL
    failure out of the call instead of answering."""
    res = await memory_handlers.do_archive_episode(AGENT, [], summary="an episode", channel=None)
    assert res.get("ok") is True and res.get("episode_id"), res
    rows = await clean_db.execute_fetchall("SELECT channel FROM episodes WHERE agent_id = ?", (AGENT,))
    assert [r[0] for r in rows] == [""], rows


@pytest.mark.asyncio
async def test_a_none_agent_id_is_refused_not_skipped(clean_db):
    res = await memory_handlers.do_store(None, {"content": "fourth fact"})
    assert res.get("ok") is False and res.get("result") == "rejected", res
    assert "agent_id" in res["reason"], res


# ---------------------------------------------------------------------------
# archive_episode applies the ahead-of-the-clock rule to the stamps it stores
# ---------------------------------------------------------------------------


def _ahead(seconds: float) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=config.FUTURE_TIMESTAMP_SKEW_SECONDS + seconds)
    ).isoformat()


def _history(*stamps: str) -> list[dict]:
    return [{"role": "user", "content": f"turn {i}", "timestamp": s} for i, s in enumerate(stamps)]


@pytest.mark.asyncio
async def test_reject_refuses_an_episode_stamped_ahead_of_the_clock(clean_db, monkeypatch, caplog):
    monkeypatch.setattr(config, "FUTURE_TIMESTAMP_MODE", "reject")
    with caplog.at_level(logging.WARNING):
        res = await server.do_archive_episode_boundary(
            AGENT, _history("2026-01-01T00:00:00+00:00", "2099-01-01T00:00:00Z"), "a summary"
        )
    assert res.get("ok") is False and res.get("episode_id", "missing") is None, res
    assert "2099-01-01T00:00:00Z" in res["error"] and "ahead of this clock" in res["error"], res
    assert "CPERSONA_FUTURE_TIMESTAMP_MODE" in caplog.text
    assert await _episodes(clean_db) == [], "a refused archive must leave no episode"


@pytest.mark.asyncio
async def test_warn_stores_the_episode_and_reports_the_stamp(clean_db, caplog):
    stamp = _ahead(3600)
    with caplog.at_level(logging.WARNING):
        res = await server.do_archive_episode_boundary(
            AGENT, _history("2026-01-01T00:00:00+00:00", stamp), "a summary"
        )
    assert res.get("ok") is True and res.get("episode_id"), res
    issue = res.get("timestamp_ahead_of_clock")
    assert issue is not None and issue["timestamp"] == stamp, res
    assert issue["allowance_seconds"] == config.FUTURE_TIMESTAMP_SKEW_SECONDS
    assert "CPERSONA_FUTURE_TIMESTAMP_MODE=reject" in caplog.text
    assert [r[3] for r in await _episodes(clean_db)] == [stamp], "warn stores the episode as given"


@pytest.mark.asyncio
async def test_a_julian_day_stamp_is_judged_as_sqlite_reads_it(clean_db, monkeypatch):
    """A history whose stamps only SQLite reads has no parsed end; the ends are
    the stored strings, and they are judged as store judges them."""
    monkeypatch.setattr(config, "FUTURE_TIMESTAMP_MODE", "reject")
    res = await server.do_archive_episode_boundary(AGENT, _history("2500000"), "a summary")
    assert res.get("ok") is False and "ahead of this clock" in res.get("error", ""), res


@pytest.mark.asyncio
async def test_off_stores_an_episode_silently(clean_db, monkeypatch, caplog):
    monkeypatch.setattr(config, "FUTURE_TIMESTAMP_MODE", "off")
    with caplog.at_level(logging.WARNING):
        res = await server.do_archive_episode_boundary(AGENT, _history("2099-01-01T00:00:00Z"), "a summary")
    assert res.get("ok") is True and "timestamp_ahead_of_clock" not in res, res
    assert "ahead of this clock" not in caplog.text


@pytest.mark.asyncio
async def test_an_ordinary_episode_carries_no_report(clean_db, monkeypatch):
    monkeypatch.setattr(config, "FUTURE_TIMESTAMP_MODE", "reject")
    res = await server.do_archive_episode_boundary(
        AGENT, _history("2026-01-01T00:00:00+00:00", "2026-01-01T01:00:00+00:00"), "a summary"
    )
    assert res.get("ok") is True and "timestamp_ahead_of_clock" not in res, res
    res = await server.do_archive_episode_boundary(AGENT, [], "no history at all")
    assert res.get("ok") is True and "timestamp_ahead_of_clock" not in res, res
