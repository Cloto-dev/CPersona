"""Stage 2 of declared session identity: the no-persist pause becomes per-session.

Authority: ``docs/SESSION_IDENTITY_DESIGN.md`` §5 (stage 2) and §7 (lifetime).

Stage 1 keyed the degraded-recall advisory and *disclosed* who armed the pause;
the pause itself was still one process-global flag, so any client could silence
every other connected session's writes. Stage 2 keys the pause too. Two claims
carry this file, and every test below holds one of them:

- **Isolation.** A declared caller's pause reaches that caller's writes and no
  others'. Not authentication (a caller can send any key, including another
  session's) — a partition of process-local state, nothing more.
- **Preservation.** A caller that declares nothing behaves exactly as it did
  before the parameter existed, because the keyless bucket *is* the old global:
  one entry, shared, reported as ``scope: "process"``.

Two of these tests are not about behaviour but about the seam staying honest:
``test_ttl_validation_matches_vendored`` pins the argument rules this module
copied from the vendored ``no_persist``, and
``test_skipped_response_carries_this_sessions_ttl`` pins the one substitution
this module performs on the vendored response builder. Both fail loudly on a
re-vendor that changes what they mirror, which is the only reason copying was
acceptable. The structural half — "no second implementation of the pause switch
survives anywhere in the package" — lives with the other source-scanning gates,
in ``test_structural_gates.py`` (Gate 17).
"""

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from cpersona import admin_handlers, memory_handlers, server, session, tasks
from cpersona._vendored_mcp_common import no_persist
from cpersona.database import get_db

A = "sess-A"
B = "sess-B"


@pytest.fixture(autouse=True)
def _clean_pauses():
    session.reset_pauses_for_tests()
    yield
    session.reset_pauses_for_tests()


@pytest_asyncio.fixture
async def clean_db():
    db = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()
    return db


def _msg(content: str) -> dict:
    return {"id": "", "content": content, "source": {"User": "u"}}


def _persisted(response: dict) -> bool:
    """``persisted`` is the field the tool contract tells callers to branch on.

    Absent on a normal write (the key is added only by the skipped-response
    builder), so its absence is the success signal and ``False`` is the skip.
    """
    return response.get("persisted") is not False


class _Clock:
    """A movable clock for the pause module. Injected, never slept on.

    A TTL test that sleeps either takes as long as the window it is testing or
    shrinks the window until it races the machine; moving the clock does neither
    and can cross the boundary exactly.
    """

    def __init__(self, start: datetime | None = None):
        self.now = start or datetime(2027, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(session, "_now", c)
    return c


# ---------------------------------------------------------------------------
# 1 — isolation: a pause reaches its own session's writes and nobody else's
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_declared_pause_skips_only_that_sessions_writes(clean_db):
    """The defect stage 2 closes: one client silencing every other client."""
    await server.do_pause_persistence(ttl_seconds=120, session_key=A)

    mine = await memory_handlers.do_store("agent-1", _msg("A writes"), session_key=A)
    theirs = await memory_handlers.do_store("agent-1", _msg("B writes"), session_key=B)
    keyless = await memory_handlers.do_store("agent-1", _msg("nobody writes"))

    assert not _persisted(mine), "the paused session's own write was persisted"
    assert _persisted(theirs), "a parallel session's write was silenced by a pause it never armed"
    assert _persisted(keyless), "a keyless write was silenced by a declared session's pause"

    rows = await clean_db.execute_fetchall("SELECT content FROM memories ORDER BY id")
    assert [r[0] for r in rows] == ["B writes", "nobody writes"]


@pytest.mark.asyncio
async def test_a_keyless_pause_skips_only_keyless_writes(clean_db):
    """The keyless bucket is one bucket, not a wildcard over every session."""
    await server.do_pause_persistence(ttl_seconds=120)

    keyless = await memory_handlers.do_store("agent-1", _msg("shared bucket"))
    mine = await memory_handlers.do_store("agent-1", _msg("A writes"), session_key=A)
    theirs = await memory_handlers.do_store("agent-1", _msg("B writes"), session_key=B)

    assert not _persisted(keyless), "the keyless bucket did not honour its own pause"
    assert _persisted(mine) and _persisted(theirs), (
        "a keyless pause silenced declared sessions — the stage-1 blast radius, unchanged"
    )

    rows = await clean_db.execute_fetchall("SELECT content FROM memories ORDER BY id")
    assert [r[0] for r in rows] == ["A writes", "B writes"]


@pytest.mark.asyncio
async def test_isolation_holds_across_the_write_surface(clean_db):
    """Not just store: every gated handler must consult the CALLER's bucket.

    One handler left on the shared bucket is the whole defect back in one tool,
    and it would pass any test that only exercised do_store.
    """
    cur = await clean_db.execute(
        "INSERT INTO memories (agent_id, content, timestamp) VALUES (?, ?, ?)",
        ("agent-1", "seed", "2027-01-01T00:00:00Z"),
    )
    await clean_db.commit()
    mem_id = cur.lastrowid

    await server.do_pause_persistence(ttl_seconds=120, session_key=A)

    calls = {
        "update_profile": lambda key: admin_handlers.do_update_profile(
            "agent-1", "text", session_key=key
        ),
        "update_memory": lambda key: admin_handlers.do_update_memory(
            mem_id, "edited", session_key=key
        ),
        "lock_memory": lambda key: admin_handlers.do_lock_memory(mem_id, session_key=key),
        "unlock_memory": lambda key: admin_handlers.do_unlock_memory(mem_id, session_key=key),
        "delete_memory": lambda key: admin_handlers.do_delete_memory(mem_id, session_key=key),
        "delete_agent_data": lambda key: admin_handlers.do_delete_agent_data(
            "agent-x", session_key=key
        ),
        "delete_episode": lambda key: admin_handlers.do_delete_episode(1, session_key=key),
        "set_recall_precision": lambda key: admin_handlers.do_set_recall_precision(
            "agent-1", "strict", session_key=key
        ),
        "archive_episode": lambda key: memory_handlers.do_archive_episode(
            "agent-1", [], summary="s", session_key=key
        ),
        "archive_episode_or_queue": lambda key: server.do_archive_episode_or_queue(
            "agent-1", [], summary="s", session_key=key
        ),
        "update_profile_or_queue": lambda key: server.do_update_profile_or_queue(
            "agent-1", "text", session_key=key
        ),
    }

    silenced_for_the_owner = [name for name, call in calls.items() if _persisted(await call(A))]
    assert not silenced_for_the_owner, (
        f"these tools ignored the caller's own pause and wrote anyway: {silenced_for_the_owner}"
    )

    leaked_to_b = [name for name, call in calls.items() if not _persisted(await call(B))]
    assert not leaked_to_b, (
        "these tools consulted a bucket other than the caller's, so one session's pause "
        f"still silences another's writes: {leaked_to_b}"
    )


@pytest.mark.asyncio
async def test_maintenance_fix_downgrade_follows_the_caller(clean_db):
    """check_health / deep_check downgrade fix=True rather than refusing outright.

    Same isolation question in a different response shape: the downgrade must
    key on the caller, or a parallel session silently loses its repairs.
    """
    await server.do_pause_persistence(ttl_seconds=120, session_key=A)

    for handler in (
        lambda key: admin_handlers.do_calibrate_threshold("agent-1", session_key=key),
    ):
        assert not _persisted(await handler(A))
        assert _persisted(await handler(B))

    mine = await server.do_check_health("agent-1", fix=True, session_key=A)
    theirs = await server.do_check_health("agent-1", fix=True, session_key=B)
    assert mine["repairs_skipped"] is True
    assert "repairs_skipped" not in theirs, "a foreign pause downgraded this caller's repairs"

    mine_deep = await server.do_deep_check("agent-1", fix=True, session_key=A)
    theirs_deep = await server.do_deep_check("agent-1", fix=True, session_key=B)
    assert mine_deep["repairs_skipped"] is True
    assert "repairs_skipped" not in theirs_deep

    # migrate_channel_axis is gated differently — forced to dry_run — but on the
    # same axis.
    mine_migrate = await server.do_migrate_channel_axis(dry_run=False, session_key=A)
    theirs_migrate = await server.do_migrate_channel_axis(dry_run=False, session_key=B)
    assert mine_migrate["dry_run"] is True
    assert theirs_migrate["dry_run"] is False


@pytest.mark.asyncio
async def test_recall_count_bump_follows_the_callers_pause(clean_db, monkeypatch):
    """bug-038's gate, re-keyed: the ranking write is a write like any other."""
    monkeypatch.setattr(memory_handlers, "CONFIDENCE_ENABLED", True)
    await memory_handlers.do_store("agent-rc", _msg("raspberry jam recipe"))
    await server.do_pause_persistence(ttl_seconds=120, session_key=A)

    async def _count() -> int:
        rows = await clean_db.execute_fetchall(
            "SELECT recall_count FROM memories WHERE agent_id = 'agent-rc'"
        )
        return rows[0][0]

    before = await _count()
    await memory_handlers.do_recall("agent-rc", "raspberry", 10, session_key=A)
    assert await _count() == before, "the paused session's recall still moved ranking state"

    await memory_handlers.do_recall("agent-rc", "raspberry", 10, session_key=B)
    assert await _count() > before, "a parallel session's recall was gated on a foreign pause"


# ---------------------------------------------------------------------------
# 2 — preservation: the keyless response is what it was before the parameter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_keyless_sequence_returns_the_pre_change_shape(clean_db):
    """A keyless pause → write → status round trip, field for field.

    Written as exact key sets, not spot checks: the failure this guards against
    is an ADDED key (stage 1's ownership fields were exactly that), which a
    subset assertion cannot see.
    """
    paused = await server.do_pause_persistence(ttl_seconds=1800)
    assert set(paused) == {"paused", "expires_at", "ttl_seconds", "scope"}
    assert paused["paused"] is True
    assert paused["ttl_seconds"] == 1800
    assert paused["scope"] == "process"

    write = await memory_handlers.do_store("agent-1", _msg("skipped"))
    assert set(write) == {"ok", "result", "id", "embedded", "persisted", "dry_run", "reason"}
    assert (write["ok"], write["result"], write["id"]) == (True, "skipped", "no-persist")
    assert write["persisted"] is False and write["dry_run"] is True

    status = await server.do_persistence_status()
    assert set(status) == {"paused", "expires_at", "ttl_remaining_seconds", "scope"}
    assert status["paused"] is True
    assert status["scope"] == "process"

    resumed = await server.do_resume_persistence()
    assert set(resumed) == {"paused", "was_active", "scope"}
    assert (resumed["paused"], resumed["was_active"], resumed["scope"]) == (False, True, "process")

    for response in (paused, write, status, resumed):
        assert "pause_owner_known" not in response
        assert "paused_by_self" not in response


@pytest.mark.asyncio
async def test_a_declared_response_carries_no_extra_keys_either():
    """The declared payload is the keyless payload with one field's value changed."""
    paused = await server.do_pause_persistence(ttl_seconds=60, session_key=A)
    status = await server.do_persistence_status(session_key=A)
    resumed = await server.do_resume_persistence(session_key=A)

    assert set(paused) == {"paused", "expires_at", "ttl_seconds", "scope"}
    assert set(status) == {"paused", "expires_at", "ttl_remaining_seconds", "scope"}
    assert set(resumed) == {"paused", "was_active", "scope"}


# ---------------------------------------------------------------------------
# 3 — the scope field states the blast radius, and states it correctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("declared,expected", [(A, "session"), (None, "process")])
async def test_scope_is_reported_on_all_three_controls(declared, expected):
    kwargs = {} if declared is None else {"session_key": declared}
    assert (await server.do_pause_persistence(ttl_seconds=60, **kwargs))["scope"] == expected
    assert (await server.do_persistence_status(**kwargs))["scope"] == expected
    assert (await server.do_resume_persistence(**kwargs))["scope"] == expected


def test_whitespace_only_is_scoped_as_the_shared_bucket():
    """The resolution rule reaches the scope field: "  " is not a session."""
    key, declared = session.resolve_session_key("   ")
    assert session.pause_for(key, declared, 60)["scope"] == "process"


# ---------------------------------------------------------------------------
# 4 — TTL, per key
# ---------------------------------------------------------------------------


def test_a_pause_expires_on_its_own_key_only(clock):
    session.pause_for(A, True, 60)
    session.pause_for(B, True, 600)

    clock.advance(59)
    assert session.is_paused_for(A) and session.is_paused_for(B)

    clock.advance(1)  # exactly at A's deadline: `now >= deadline` expires it
    assert not session.is_paused_for(A), "A's TTL did not elapse at its deadline"
    assert session.is_paused_for(B), "B's pause expired on A's clock"

    clock.advance(540)
    assert not session.is_paused_for(B)


def test_status_ttl_remaining_counts_down_for_the_caller(clock):
    session.pause_for(A, True, 300)
    assert session.pause_status_for(A, True)["ttl_remaining_seconds"] == 300
    clock.advance(120)
    assert session.pause_status_for(A, True)["ttl_remaining_seconds"] == 180
    assert session.pause_status_for(B, True) == {
        "paused": False,
        "expires_at": None,
        "ttl_remaining_seconds": None,
        "scope": "session",
    }


def test_resume_reports_was_active_for_the_callers_key_only(clock):
    session.pause_for(A, True, 60)

    assert session.resume_for(B, True)["was_active"] is False, (
        "B was told it cleared a pause it neither armed nor could see"
    )
    assert session.is_paused_for(A), "B's resume cleared A's pause"

    assert session.resume_for(A, True)["was_active"] is True
    assert not session.is_paused_for(A)
    assert session.resume_for(A, True)["was_active"] is False, "a redundant resume claimed a clear"


def test_an_expired_pause_reports_was_active_false(clock):
    """Decay runs before the report, so an elapsed TTL is not a clear."""
    session.pause_for(A, True, 60)
    clock.advance(61)
    assert session.resume_for(A, True)["was_active"] is False


def test_re_arming_replaces_the_ttl_rather_than_stacking(clock):
    session.pause_for(A, True, 600)
    session.pause_for(A, True, 60)
    clock.advance(61)
    assert not session.is_paused_for(A), "the second pause stacked instead of replacing"


# ---------------------------------------------------------------------------
# 5 — the map is bounded, and the cap is what bounds it
# ---------------------------------------------------------------------------


def _fill_the_pause_map(soonest_ttl=60):
    """A full map: one entry near its deadline, the rest far from theirs."""
    session.pause_for("soonest", True, soonest_ttl)
    for n in range(session._MAX_PAUSED_SESSIONS - 1):
        session.pause_for(f"s-{n}", True, 3600)
    assert len(session._pauses) == session._MAX_PAUSED_SESSIONS


def test_the_pause_map_is_capped_and_refuses_rather_than_revoking():
    """bug-269: a granted pause holds until its TTL or a resume. Nothing revokes it.

    The map used to evict the nearest deadline to make room. That lifted a pause on
    behalf of a *different* caller arming one of its own, with no signal reaching the
    session that lost it — `store` kept answering like any successful write. The
    nearest-deadline rule also selected against ordinary use: a caller on the default
    TTL is nearer its deadline than one holding the maximum, so the entry dropped was
    the one least likely to belong to the key rotator the cap exists for.
    """
    _fill_the_pause_map()
    before = dict(session._pauses)

    with pytest.raises(session.PauseCapacityError) as refused:
        session.pause_for("one-too-many", True, 3600)

    assert session._pauses == before, "a refused pause still disturbed the map"
    assert session.is_paused_for("soonest"), "the nearest deadline was revoked anyway"
    assert not session.is_paused_for("one-too-many")
    assert len(session._pauses) == session._MAX_PAUSED_SESSIONS, (
        "the map grew past the cap — a client rotating keys can now exhaust memory"
    )
    message = str(refused.value)
    assert "NOT paused" in message, "the refusal has to say the pause did not happen"
    assert "resume_persistence" in message, "and name the way out"


@pytest.mark.asyncio
async def test_the_refusal_reaches_the_caller_as_a_failed_tool_response():
    """The refusal is only worth anything if it survives the handler.

    ``PauseCapacityError`` subclasses ``ValueError`` precisely so it rides the channel
    ``do_pause_persistence`` already turns into ``{ok: False, error: ...}``. A caller
    that branches on ``ok`` must not read this as a pause it now holds.
    """
    _fill_the_pause_map()

    out = await server.do_pause_persistence(ttl_seconds=3600, session_key="one-too-many")

    assert out["ok"] is False
    assert "NOT paused" in out["error"]
    assert out.get("paused") is not True
    assert not session.is_paused_for("one-too-many")


def test_an_expired_entry_frees_its_slot_so_a_full_map_recovers():
    """The cap counts live pauses, not keys ever seen — decay runs before the check.

    Without this the first 256 keys a process ever saw would own the pause surface for
    the life of the process, which is a denial the fix would have introduced.
    """
    _fill_the_pause_map(soonest_ttl=1)
    session._pauses["soonest"] = session._now() - timedelta(seconds=1)

    session.pause_for("one-too-many", True, 3600)

    assert session.is_paused_for("one-too-many")
    assert not session.is_paused_for("soonest"), "the expired entry should be gone"
    assert len(session._pauses) == session._MAX_PAUSED_SESSIONS


def test_re_arming_an_existing_key_at_the_cap_is_neither_refused_nor_costly():
    """Refreshing a pause is not a new session, so it must not cost another one."""
    for n in range(session._MAX_PAUSED_SESSIONS):
        session.pause_for(f"s-{n}", True, 3600)
    session.pause_for("s-0", True, 7200)
    assert len(session._pauses) == session._MAX_PAUSED_SESSIONS
    assert all(session.is_paused_for(f"s-{n}") for n in range(session._MAX_PAUSED_SESSIONS))


# ---------------------------------------------------------------------------
# 6 — the copied invariant stays honest across a re-vendor
# ---------------------------------------------------------------------------


_TTL_CASES = [True, 0, -1, 1, 1800, no_persist.MAX_TTL_SECONDS + 1, "60", None, 1.5]


def _outcome(call) -> object:
    """Run a TTL acceptor and reduce it to comparable evidence."""
    try:
        return ("ok", call())
    except ValueError:
        return ("ValueError", None)


@pytest.mark.parametrize("ttl", _TTL_CASES, ids=[repr(t) for t in _TTL_CASES])
def test_ttl_validation_matches_vendored(ttl):
    """``session._validate_ttl`` is a copy of the vendored rules; this is the pin.

    ``session`` no longer calls ``no_persist.pause``, so nothing else would notice
    a re-vendor that tightened the type check, moved the ceiling, or started
    accepting ``bool``. Both sides are exercised on the same table: the vendored
    module's answer is read out of the returned ``ttl_seconds``, which is the
    clamped value it actually used.
    """
    mine = _outcome(lambda: session._validate_ttl(ttl))
    try:
        theirs = ("ok", no_persist.pause(ttl_seconds=ttl)["ttl_seconds"])
    except ValueError:
        theirs = ("ValueError", None)
    finally:
        no_persist.resume()

    assert mine == theirs, (
        f"ttl_seconds={ttl!r}: session._validate_ttl says {mine}, the vendored "
        f"no_persist.pause says {theirs}. The copy has drifted from its source — "
        "re-read the vendored rules and update _validate_ttl, do not relax this test."
    )


def test_the_ttl_table_covers_both_verdicts():
    """A table that only produced one outcome would agree with anything."""
    verdicts = {_outcome(lambda t=t: session._validate_ttl(t))[0] for t in _TTL_CASES}
    assert verdicts == {"ok", "ValueError"}


# ---------------------------------------------------------------------------
# 7 — the skipped response names THIS session's TTL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skipped_response_carries_this_sessions_ttl(clock, clean_db):
    """The vendored builder reads a global this package no longer arms.

    Left alone it renders "TTL unknown" on every skipped write — true-ish,
    useless, and silent. ``session.make_skipped_response`` substitutes the
    caller's own remaining TTL; this asserts the substitution fired.
    """
    session.pause_for(A, True, 1800)
    clock.advance(120)

    skipped = await memory_handlers.do_store("agent-1", _msg("x"), session_key=A)
    assert "TTL unknown" not in skipped["reason"], (
        "the substitution did not fire — every skipped write now reports an unknown TTL"
    )
    assert "(28m left)" in skipped["reason"], skipped["reason"]

    session.pause_for(B, True, 45)
    b_skipped = await memory_handlers.do_store("agent-1", _msg("y"), session_key=B)
    assert "(45s left)" in b_skipped["reason"], (
        f"B was told A's TTL, not its own: {b_skipped['reason']}"
    )


def test_the_substituted_reason_is_otherwise_the_vendored_one(clock):
    """Only the parenthetical changes; the shape stays the vendored module's.

    The id sentinel and the nulled action-id keys (bug-104) are invariants with
    exactly one implementation, and this is what says so.
    """
    session.pause_for(A, True, 1800)
    body = {"ok": True, "id": 7, "deleted_id": 7}
    mine = session.make_skipped_response(dict(body), "delete_memory", A)
    theirs = no_persist.make_skipped_response(dict(body), "delete_memory")

    assert mine["id"] == "no-persist" == theirs["id"]
    assert mine["deleted_id"] is None is theirs["deleted_id"]
    assert (mine["persisted"], mine["dry_run"]) == (False, True)
    assert mine["reason"] == theirs["reason"].replace("(TTL unknown)", "(30m left)")


def test_an_unknown_key_still_gets_a_usable_reason():
    """No pause for this key (a race with expiry) — the label must not crash."""
    body = session.make_skipped_response({"ok": True}, "store", "never-paused")
    assert "TTL unknown" in body["reason"]


# ---------------------------------------------------------------------------
# 9 — queued work is judged by the pause of the session that enqueued it
#
# (8 is the structural gate; it lives in test_structural_gates.py.)
# ---------------------------------------------------------------------------


async def _drain_once(queue) -> None:
    queue._running = True
    try:
        await queue._drain(admin_handlers, memory_handlers)
    finally:
        queue._running = False


@pytest.mark.asyncio
async def test_a_queued_task_is_dropped_by_its_own_sessions_pause(clean_db, monkeypatch):
    ran: list[str] = []

    async def spy_update_profile(agent_id, payload, session_key=""):
        ran.append(agent_id)
        return {"ok": True, "profiles_updated": 1}

    monkeypatch.setattr(admin_handlers, "do_update_profile", spy_update_profile)

    queue = tasks.MemoryTaskQueue()
    await queue.enqueue("update_profile", "agent-A", [{"content": "x"}], session_key=A)
    session.pause_for(A, True, 120)

    await _drain_once(queue)

    assert ran == [], "queued work outlived the pause of the session that queued it"
    rows = await clean_db.execute_fetchall("SELECT COUNT(*) FROM pending_memory_tasks")
    assert rows[0][0] == 0, "the dropped task was left in the queue to be retried later"


@pytest.mark.asyncio
async def test_a_queued_task_survives_another_sessions_pause(clean_db, monkeypatch):
    """The half a single global flag got wrong: B's pause discarding A's work."""
    ran: list[str] = []

    async def spy_update_profile(agent_id, payload, session_key=""):
        ran.append(agent_id)
        return {"ok": True, "profiles_updated": 1}

    monkeypatch.setattr(admin_handlers, "do_update_profile", spy_update_profile)

    queue = tasks.MemoryTaskQueue()
    await queue.enqueue("update_profile", "agent-A", [{"content": "x"}], session_key=A)
    session.pause_for(B, True, 120)
    session.pause_for(session.TRANSPORT_KEY, False, 120)

    await _drain_once(queue)

    assert ran == ["agent-A"], "a pause armed by another session discarded this task"


@pytest.mark.asyncio
async def test_an_unattributed_task_is_gated_on_the_transport_bucket(clean_db, monkeypatch):
    """A row that survived a restart has no session left; the shared bucket owns it."""
    ran: list[str] = []

    async def spy_update_profile(agent_id, payload, session_key=""):
        ran.append(agent_id)
        return {"ok": True, "profiles_updated": 1}

    monkeypatch.setattr(admin_handlers, "do_update_profile", spy_update_profile)

    queue = tasks.MemoryTaskQueue()
    await queue.enqueue("update_profile", "agent-orphan", [{"content": "x"}], session_key=A)
    queue._task_sessions.clear()  # what a process restart leaves behind
    session.pause_for(A, True, 120)

    await _drain_once(queue)
    assert ran == ["agent-orphan"], (
        "an unattributed row was still charged to the session that no longer exists"
    )

    await queue.enqueue("update_profile", "agent-orphan", [{"content": "x"}], session_key=A)
    queue._task_sessions.clear()
    session.pause_for(session.TRANSPORT_KEY, False, 120)

    await _drain_once(queue)
    assert ran == ["agent-orphan"], "the transport bucket's pause did not reach an orphan row"


@pytest.mark.asyncio
async def test_attribution_does_not_outlive_the_row(clean_db, monkeypatch):
    """The map has no owner but the queue, so every exit from the queue clears it."""

    async def spy_update_profile(agent_id, payload, session_key=""):
        return {"ok": True, "profiles_updated": 1}

    monkeypatch.setattr(admin_handlers, "do_update_profile", spy_update_profile)

    queue = tasks.MemoryTaskQueue()
    completed = await queue.enqueue("update_profile", "agent-A", [{"content": "x"}], session_key=A)
    assert queue._task_sessions[completed] == A
    await _drain_once(queue)
    assert completed not in queue._task_sessions, "a completed task kept its attribution"

    dropped = await queue.enqueue("update_profile", "agent-A", [{"content": "x"}], session_key=A)
    session.pause_for(A, True, 120)
    await _drain_once(queue)
    assert dropped not in queue._task_sessions, "a dropped task kept its attribution"


def test_the_literal_transport_key_is_not_a_declared_session():
    """Sending ``TRANSPORT_KEY`` joins the shared bucket, so it is reported as one.

    The module docstring says any caller may send any string, and names this one
    explicitly. Resolving it as *declared* would make every consumer of the flag
    describe the shared bucket as private: the pause would answer
    ``scope: "session"`` while silencing every keyless caller — who could also
    clear it — and the advisory would key its suppression per-session on state
    the whole process shares. The bucket decides, not the caller's intent.
    """
    assert session.resolve_session_key(session.TRANSPORT_KEY) == (session.TRANSPORT_KEY, False)
    assert session.resolve_session_key(f"  {session.TRANSPORT_KEY}  ") == (
        session.TRANSPORT_KEY,
        False,
    )
    # The scope it reports is the one a keyless caller gets, not "session".
    armed = session.pause_for(*session.resolve_session_key(session.TRANSPORT_KEY), 120)
    try:
        assert armed["scope"] == "process"
        keyless_key, keyless_declared = session.resolve_session_key(None)
        assert session.is_paused_for(keyless_key), "the literal armed a different bucket"
        assert session.pause_status_for(keyless_key, keyless_declared)["scope"] == "process"
    finally:
        session.resume_for(session.TRANSPORT_KEY, False)


@pytest.mark.asyncio
async def test_a_rolled_back_insert_keeps_its_attribution(clean_db, monkeypatch):
    """A transaction that rolls back must not strand the retry in the shared bucket.

    The task-row DELETE and the episode INSERT share one transaction, so an
    insert that raises rolls the row back into the queue to be retried. A dict
    mutation does not roll back with it: dropping the attribution inside that
    block would hand the retry to the keyless bucket, and a session that armed a
    no-persist pause in the meantime would have its episode written anyway —
    the exact write the pause exists to stop.
    """
    inserts = {"n": 0}
    attributions: list[str] = []

    async def fake_prepare(agent_id, payload, summary=""):
        return {"agent_id": agent_id}

    async def flaky_insert(db, row):
        inserts["n"] += 1
        if inserts["n"] == 1:
            # The session goes no-persist while its queued work is in flight —
            # the window the retry path exists to survive.
            session.pause_for(A, True, 120)
            raise RuntimeError("transient insert failure")

    monkeypatch.setattr(memory_handlers, "_prepare_episode_row", fake_prepare)
    monkeypatch.setattr(memory_handlers, "_insert_episode_row", flaky_insert)

    queue = tasks.MemoryTaskQueue()
    task_id = await queue.enqueue("archive_episode", "agent-A", [{"content": "x"}], session_key=A)
    assert queue._task_sessions[task_id] == A

    # _drain re-fetches until the queue is empty, so the retry happens inside this call.
    original_session_for = queue._session_for
    queue._session_for = lambda tid: attributions.append(original_session_for(tid)) or attributions[-1]
    await _drain_once(queue)

    assert attributions[0] == A, "the first attempt was not charged to the enqueuing session"
    assert len(attributions) > 1, "the failed insert did not leave the row queued for retry"
    assert attributions[1] == A, (
        "the rolled-back transaction dropped the attribution, so the retry was judged "
        "against the shared keyless bucket instead of the session that queued it"
    )
    assert inserts["n"] == 1, (
        "the retry wrote the episode despite the pause armed by the session that "
        "queued the work"
    )
    rows = await clean_db.execute_fetchall("SELECT COUNT(*) FROM pending_memory_tasks")
    assert rows[0][0] == 0, "the task the pause dropped was left in the queue"


def test_the_attribution_map_is_bounded():
    """Bounded for the same reason the pause map is, and by its own cap."""
    queue = tasks.MemoryTaskQueue()
    for task_id in range(queue._MAX_ATTRIBUTED_TASKS + 50):
        queue._remember_session(task_id, f"s-{task_id}")
    assert len(queue._task_sessions) == queue._MAX_ATTRIBUTED_TASKS
    # FIFO: the lowest ids are the rows nearest the head, so they go first.
    assert queue._session_for(0) == session.TRANSPORT_KEY
    assert queue._session_for(queue._MAX_ATTRIBUTED_TASKS + 49) == (
        f"s-{queue._MAX_ATTRIBUTED_TASKS + 49}"
    )


def test_the_transport_key_is_not_stored_as_an_entry():
    """The fallback is what an absent entry means; storing it would spend the cap."""
    queue = tasks.MemoryTaskQueue()
    queue._remember_session(1, session.TRANSPORT_KEY)
    assert queue._task_sessions == {}
    assert queue._session_for(1) == session.TRANSPORT_KEY


@pytest.mark.asyncio
async def test_export_is_not_gated_by_a_pause(clean_db, tmp_path):
    """`export_memories` runs under a pause, and takes no session_key.

    Not an omission from the threading: a pause means "do not write to my
    memory", and an export writes a file the caller asked for and not one row.
    Gating it would return a fabricated tally that hides what a real export
    would have produced — the failure bug-048 and bug-079 corrected on the merge
    and import previews. A key here would decide nothing, so it is not offered.

    This test is what makes that a decision rather than an accident: adding a
    pause gate, or the parameter, turns it red.
    """
    await memory_handlers.do_store("agent-1", _msg("written before the pause"))
    await server.do_pause_persistence(ttl_seconds=120, session_key=A)
    await server.do_pause_persistence(ttl_seconds=120)  # the keyless bucket too

    out = tmp_path / "export.jsonl"
    result = await admin_handlers.do_export_memories("agent-1", str(out))

    assert not result.get("error"), result
    assert result.get("persisted") is not False, "export was treated as a skipped write"
    assert out.exists(), "no file was produced while a pause was armed"
    assert "written before the pause" in out.read_text()

    import inspect

    assert "session_key" not in inspect.signature(admin_handlers.do_export_memories).parameters, (
        "export_memories grew a session_key; it consults no pause, so the key would decide "
        "nothing and would cost every client the description"
    )
