"""bug-187: an unattributed memory is not a broken one (2.5.3, an earlier decision).

``store`` records an omitted or null ``source`` as the anonymous ``{}``. That is
the write path's own normalisation and a supported way to say "producer
unknown", but ``check_invalid_source_type`` counted it as a type defect. The
check carries ``warn``, ``warn`` decides the single ``status`` verdict, and
``fix=true`` cannot clear it because ``normalize_source`` rightly refuses to
invent a discriminator — so the verdict was ``degraded``, permanently, for a DB
that had done nothing wrong.

Both directions are pinned here. A test that only proved anonymous rows are
clean would be satisfied by deleting the detector.
"""

import json

import pytest
import pytest_asyncio

from cpersona import checks, maintenance_handlers
from cpersona._vendored_mcp_common import no_persist
from cpersona.database import get_db

AGENT = "bug187-agent"


@pytest_asyncio.fixture
async def db():
    no_persist.resume()
    conn = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()
    return conn


async def _insert(conn, source: str | None, content: str):
    await conn.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp) VALUES (?, ?, ?, ?)",
        (AGENT, content, source, "2026-07-29T00:00:00+00:00"),
    )
    await conn.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["{}", "{ }", "null"])
async def test_anonymous_source_is_not_an_invalid_type(db, source):
    """The blessed anonymous shapes produce no finding at all."""
    await _insert(db, source, f"anonymous via {source}")

    assert await checks.check_invalid_source_type(db, AGENT, fix=False) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source",
    [
        '{"type":"assistant"}',
        '{"type":"migration","id":"x"}',
        '{"id":"x","name":"someone"}',
        '"claude-code"',
    ],
)
async def test_genuinely_wrong_source_type_is_still_reported(db, source):
    """The other direction: excluding {} must not blunt the detector.

    Each of these claimed a producer and got the contract wrong — an object with
    keys but no recognised type, or a bare string. Dropping these would turn the
    bug-187 fix into a deleted check.
    """
    await _insert(db, source, f"invalid via {source}")

    found = await checks.check_invalid_source_type(db, AGENT, fix=False)

    assert len(found) == 1
    assert found[0]["type"] == "invalid_source_type"
    assert found[0]["count"] == 1


@pytest.mark.asyncio
async def test_fixer_processes_exactly_the_rows_the_check_counted(db):
    """The predicate has two callers and they must not drift (bug-195 class).

    The reported ledger is where a drift shows: the check promises (bug-139)
    that ``mapped + unmapped + locked`` reconciles with ``count``. A fixer that
    selects rows the COUNT excluded inflates ``unmapped`` and drives ``locked``
    negative, so assert the reconciliation rather than the SQL text — an earlier
    version of this test compared the two predicate strings, and a mutation that
    reverted the fixer's SELECT to the old clause sailed straight through it.
    """
    for i, source in enumerate(["{}", "null", '{"type":"assistant","id":"a1"}']):
        await _insert(db, source, f"row {i}")

    found = await checks.check_invalid_source_type(db, AGENT, fix=True)

    issue = found[0]
    assert issue["count"] == 1, "the two anonymous rows are not type defects"
    assert issue["mapped"] == 1
    assert issue["unmapped"] == 0, "an anonymous row reaching the fixer is a drift"
    assert issue["locked"] == 0
    assert issue["mapped"] + issue["unmapped"] + issue["locked"] == issue["count"]


@pytest.mark.asyncio
async def test_predicate_helper_is_the_single_source_for_both_callers(db):
    """Cheap structural pin beside the behavioural one above.

    Kept because the behavioural test can only see drift that changes a number;
    this one fails the moment a call site stops using the shared helper.

    Stated as "every read of memories in this function is gated by the helper"
    rather than as a fixed count of two. The literal 2 broke the moment 2.5.4
    added a third reader (the independent locked COUNT) — a change that made
    the invariant MORE true while failing the test that guards it. A pin whose
    failure mode is "someone did the right thing" gets edited under pressure,
    and the edit is where the real coverage is lost.
    """
    import inspect

    source = inspect.getsource(checks.check_invalid_source_type)

    assert source.count("invalid_source_type_where(canonical_types)") == source.count(
        "FROM memories"
    ), "a read of memories in this function does not go through the shared predicate"
    assert "json_extract(source, '$.type') NOT IN" not in source, (
        "the predicate was re-inlined at a call site instead of using the helper"
    )


@pytest.mark.asyncio
async def test_health_status_is_healthy_for_an_unattributed_corpus(db):
    """The verdict, end to end — this is what an operator actually reads."""
    for i in range(3):
        await _insert(db, "{}", f"unattributed memory {i}")

    health = await maintenance_handlers.do_check_health(agent_id=AGENT, fix=False)

    types = [issue["type"] for issue in health["issues"]]
    assert "invalid_source_type" not in types
    assert health["status"] == "healthy", health["issues"]


@pytest.mark.asyncio
async def test_health_status_still_degrades_on_a_real_type_defect(db):
    """And the paired direction at the same altitude."""
    await _insert(db, '{"type":"assistant"}', "legacy lowercase type")

    health = await maintenance_handlers.do_check_health(agent_id=AGENT, fix=False)

    assert "invalid_source_type" in [issue["type"] for issue in health["issues"]]
    assert health["status"] == "degraded"


@pytest.mark.asyncio
async def test_fix_leaves_anonymous_rows_untouched(db):
    """fix=true must not reach for the rows it stopped reporting."""
    await _insert(db, "{}", "anonymous")
    await _insert(db, '{"type":"assistant","id":"a1"}', "mappable legacy")

    await checks.check_invalid_source_type(db, AGENT, fix=True)

    stored = {
        content: source
        for content, source in await db.execute_fetchall(
            "SELECT content, source FROM memories WHERE agent_id = ?", (AGENT,)
        )
    }
    assert stored["anonymous"] == "{}"
    assert json.loads(stored["mappable legacy"])["type"] == "Agent"


# ---------------------------------------------------------------------------
# 2.5.4 — the class bug-187 left open: a warn no fix can clear
#
# bug-187 excluded the anonymous shapes, which stopped ONE route to a permanent
# `degraded`. The route stayed open for everything else the mapper refuses, and
# the store schema has no `required` on `source`, so a conforming client can
# reach it: {"id":"discord:1","name":"bob"} is accepted, counted, and unmappable.
# Measured on the production corpus as 495 rows that no number of fix runs
# clears. Severity now follows what a fix run would actually do.
# ---------------------------------------------------------------------------


UNREPAIRABLE = ['"claude-code"', '{"id":"discord:1","name":"bob"}', '{"type":123}', '{"type":null}']


@pytest.mark.asyncio
@pytest.mark.parametrize("source", UNREPAIRABLE)
async def test_an_unrepairable_defect_reports_but_stops_gating(db, source):
    """Still counted, still listed — no longer holding the verdict down.

    The row is a real contract violation and the check keeps saying so. What it
    stops doing is claiming an action is available: canonicalising this means
    deciding what the producer was, which is a migration someone authorises.
    """
    await _insert(db, source, f"unrepairable via {source}")

    found = await checks.check_invalid_source_type(db, AGENT, fix=False)

    assert found[0]["count"] == 1, "the defect must still be reported"
    assert found[0]["severity"] == "info"
    assert found[0]["needs_human_review"] is True
    assert "migration" in found[0]["hint"]


@pytest.mark.asyncio
async def test_a_repairable_defect_still_warns(db):
    """The paired direction. A downgrade that applied to everything would just
    be the detector switched off."""
    await _insert(db, '{"type":"assistant"}', "mappable legacy")

    found = await checks.check_invalid_source_type(db, AGENT, fix=False)

    assert found[0].get("severity") is None, "no override: the registry default (warn) stands"
    assert "needs_human_review" not in found[0]


@pytest.mark.asyncio
async def test_one_unrepairable_row_does_not_mask_a_repairable_one(db):
    """Mixed corpus: an action exists, so the verdict must keep saying so."""
    await _insert(db, '"claude-code"', "unrepairable")
    await _insert(db, '{"type":"assistant"}', "mappable legacy")

    found = await checks.check_invalid_source_type(db, AGENT, fix=False)

    assert found[0]["count"] == 2
    assert found[0].get("severity") is None
    assert "needs_human_review" not in found[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corpus",
    [
        ['"claude-code"'],
        ['{"id":"x","name":"y"}', '"claude-code"'],
        ['{"type":"assistant"}'],
        ['{"type":"assistant"}', '"claude-code"'],
        ['{"type":"ai"}', '{"type":"session"}'],
    ],
)
async def test_severity_is_warn_exactly_when_a_fix_would_change_something(db, corpus):
    """The rule, as a property rather than a set of examples.

    Whatever the shapes, the two must agree: the check gates if and only if
    running the repair would rewrite at least one row. Anything else is either
    a verdict that never clears or an action the operator is not told about.
    """
    for i, source in enumerate(corpus):
        await _insert(db, source, f"row {i}")

    reported = (await checks.check_invalid_source_type(db, AGENT, fix=False))[0]
    repaired = (await checks.check_invalid_source_type(db, AGENT, fix=True))[0]

    gates = reported.get("severity") != "info"
    assert gates is (repaired["mapped"] > 0), (
        f"severity says gates={gates}, but the fix run mapped {repaired['mapped']} rows"
    )


@pytest.mark.asyncio
async def test_offenders_that_are_all_locked_say_so(db):
    """A repairable row the fixer may not touch is not an available action.

    bug-098 keeps locked rows out of every fixer, so a corpus whose only
    offenders are locked converges no better than an unmappable one — but the
    operator's next step is different, and the hint has to say which it is.
    """
    await _insert(db, '{"type":"assistant"}', "locked legacy")
    await db.execute("UPDATE memories SET locked = 1 WHERE agent_id = ?", (AGENT,))
    await db.commit()

    found = await checks.check_invalid_source_type(db, AGENT, fix=False)

    assert found[0]["count"] == 1
    assert found[0]["severity"] == "info"
    assert "unlock" in found[0]["hint"]


@pytest.mark.asyncio
async def test_locked_is_counted_not_inferred_from_the_sample(db, monkeypatch):
    """`locked = count - len(rows)` was arithmetic, not a measurement.

    It agreed with reality only while the SELECT saw every unlocked offender.
    Cap the classification and the subtraction invents a locked row that does
    not exist — and it was that same subtraction that made
    `mapped + unmapped + locked == count` an identity no mutation could break.
    """
    monkeypatch.setattr(checks, "INVALID_SOURCE_CLASSIFY_CAP", 1)
    for i in range(2):
        await _insert(db, '{"type":"assistant"}', f"mappable {i}")

    found = (await checks.check_invalid_source_type(db, AGENT, fix=True))[0]

    assert found["count"] == 2
    assert found["locked"] == 0, "no row here is locked; subtraction would say 1"
    assert found["classified"] == 1, "the sample was capped and the report says so"
    assert found.get("severity") is None, "an incomplete sample must not downgrade itself"


@pytest.mark.asyncio
async def test_unattributed_rows_are_visible_somewhere(db):
    """bug-187 removed {} from invalid_source_type and left it in no check at all.

    The docstring said the exclusion did not make it invisible; measured, it did
    — a corpus of {} rows produced only missing_profile and null_embedding.
    """
    for i in range(3):
        await _insert(db, "{}", f"unattributed {i}")
    await _insert(db, json.dumps({"type": "User", "id": "", "name": ""}), "sentinel")

    found = await checks.check_anonymous_source(db, AGENT, fix=False)

    assert found[0]["count"] == 4, "both shapes mean 'no producer' and count as one number"
    assert found[0]["unattributed"] == 3
    assert "deep_check" in found[0]["hint"], "the recoverable half keeps its hint"
