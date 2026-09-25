"""The time cue and the loop's basic form (docs/RECALL_PROCESS_DESIGN.md §2).

The invariants of §2.6 are the point of these tests: without a cue nothing changes;
with one, the rows that pass the quality gate are the same, no row moves up more
than L places, at most one seat is added, and isolation is never widened.
"""
import random
from datetime import datetime, timedelta, timezone

import pytest

from cpersona import config, cue, memory_handlers, server
from cpersona.database import get_db

AGENT = "agent.recall-cue"
OTHER = "agent.recall-cue-other"
NOW = datetime.now(timezone.utc)


def _ts(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


async def _seed(rows, agent=AGENT):
    db = await get_db()
    for table in ("memories", "episodes", "profiles"):
        await db.execute(f"DELETE FROM {table} WHERE agent_id = ?", (agent,))
    await db.commit()
    refs = []
    for content, days_ago in rows:
        out = await memory_handlers.do_store(
            agent, {"content": content, "source": {"System": "test"}, "timestamp": _ts(days_ago)}
        )
        refs.append(f"mem:{out['id']}")
    return refs


# Twelve records that all match the query, one per ten days, the newest first.
CORPUS = [(f"harbor lighthouse keeper logbook entry {i}", 10 * i + 1) for i in range(12)]
QUERY = "harbor lighthouse keeper logbook"


def _refs(out):
    return [m["ref"] for m in out["messages"] if "ref" in m]


# --- reading the cue ------------------------------------------------------------------


@pytest.mark.parametrize("raw, message", [
    ("last week", "must be an object"),
    ({"after": "2026-08-01"}, "confidence is required"),
    ({"after": "2026-08-01", "confidence": "certain"}, "confidence is required"),
    ({"confidence": "sure"}, "either after/before or ago"),
    ({"after": "2026-08-01", "ago": "long_ago", "confidence": "sure"}, "either after/before or ago"),
    ({"after": "2026-08-31", "before": "2026-08-01", "confidence": "sure"}, "later than"),
    ({"after": "August", "confidence": "sure"}, "not a date"),
    ({"ago": {"unit": "years", "value": 1}, "confidence": "sure"}, "unit must be"),
    ({"ago": {"unit": "days", "value": -1}, "confidence": "sure"}, "whole number"),
    ({"ago": {"unit": "days", "value": True}, "confidence": "sure"}, "whole number"),
    ({"ago": "recently", "confidence": "sure"}, "long_ago"),
    ({"after": "2026-08-01", "confidence": "sure", "where": "home"}, "unknown fields"),
])
def test_a_cue_that_cannot_be_read_is_refused_with_the_reason(raw, message):
    with pytest.raises(cue.TimeCueError, match=message):
        cue.parse(raw)


def test_no_cue_is_none():
    assert cue.parse(None) is None and cue.parse({}) is None


def test_a_date_names_the_whole_day_and_the_margins_widen_both_sides():
    tc = cue.parse({"after": "2026-08-01", "before": "2026-08-10", "confidence": "sure"})
    utc = timezone.utc
    span = (None, None)
    assert cue.period(tc, "sure", NOW, span) == (datetime(2026, 8, 1, tzinfo=utc), datetime(2026, 8, 11, tzinfo=utc))
    # Ten days long: likely adds five on each side, vague ten.
    assert cue.period(tc, "likely", NOW, span) == (datetime(2026, 7, 27, tzinfo=utc), datetime(2026, 8, 16, tzinfo=utc))
    assert cue.period(tc, "vague", NOW, span) == (datetime(2026, 7, 22, tzinfo=utc), datetime(2026, 8, 21, tzinfo=utc))


def test_relative_cues_are_read_from_now_and_long_ago_from_the_scope():
    now = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
    three_weeks = cue.parse({"ago": {"unit": "weeks", "value": 3}, "confidence": "sure"})
    assert cue.period(three_weeks, "sure", now, (None, None)) == (
        now - timedelta(weeks=3, days=3.5), now - timedelta(weeks=3) + timedelta(days=3.5))
    today = cue.parse({"ago": {"unit": "days", "value": 0}, "confidence": "sure"})
    assert cue.period(today, "sure", now, (None, None)) == (now - timedelta(hours=12), now)
    oldest, newest = now - timedelta(days=90), now
    long_ago = cue.parse({"ago": "long_ago", "confidence": "sure"})
    assert cue.period(long_ago, "sure", now, (oldest, newest)) == (oldest, oldest + timedelta(days=30))
    assert cue.period(long_ago, "sure", now, (None, None)) is None
    open_start = cue.parse({"before": "2026-06-30", "confidence": "sure"})
    assert cue.period(open_start, "sure", now, (oldest, newest))[0] == oldest


# --- the bounded move -----------------------------------------------------------------


def test_no_row_moves_up_more_than_l_places_however_many_move():
    rng = random.Random(7)
    for _ in range(2000):
        n = rng.randint(1, 40)
        rows = [{"id": i} for i in range(n)]
        cued = rng.sample(range(n), rng.randint(0, n))
        cue_rank = {i: c for c, i in enumerate(rng.sample(cued, len(cued)))}
        bound = rng.choice([1, 2, 3])
        ordered, moves = cue.lift(rows, cue_rank, bound, lambda r: r["id"])
        assert sorted(r["id"] for r in ordered) == list(range(n))
        new = {r["id"]: q for q, r in enumerate(ordered)}
        for i in range(n):
            assert i - new[i] <= bound  # up by at most the bound
            if i not in cue_rank:
                assert new[i] >= i  # a row the cue did not find never moves up
        uncued = [r["id"] for r in ordered if r["id"] not in cue_rank]
        assert uncued == sorted(uncued)  # and the rest keep their order
        assert {m["row"]["id"] for m in moves} == {i for i in range(n) if new[i] != i}


def test_the_move_is_the_key_the_design_gives():
    rows = [{"id": i} for i in range(8)]
    # p = 5, c = 0, L = 3: key 2, tied with row 2; the tie goes to the row the cue
    # found, so the cue arm's first row moves exactly L places.
    ordered, _ = cue.lift(rows, {5: 0}, 3, lambda r: r["id"])
    assert [r["id"] for r in ordered] == [0, 1, 5, 2, 3, 4, 6, 7]
    for bound in (1, 2, 3):
        ordered, _ = cue.lift(rows, {5: 0}, bound, lambda r: r["id"])
        assert [r["id"] for r in ordered].index(5) == 5 - bound
    # Two found rows at once: row 4 (key 1) takes the tie with row 1; row 5 at a
    # worse cue rank (key 3.49) passes only row 4's old place.
    ordered, _ = cue.lift(rows, {4: 0, 5: 60}, 3, lambda r: r["id"])
    assert [r["id"] for r in ordered] == [0, 4, 1, 2, 3, 5, 6, 7]
    # A worse cue rank moves it less: c = 60 halves the bonus to 1.5.
    ordered, _ = cue.lift(rows, {5: 60}, 3, lambda r: r["id"])
    assert [r["id"] for r in ordered] == [0, 1, 2, 3, 5, 4, 6, 7]


# --- the recall -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_without_a_cue_the_recall_is_unchanged(fake_embedding_client):
    await _seed(CORPUS)
    plain = await memory_handlers.do_recall(AGENT, QUERY, limit=5)
    assert await memory_handlers.do_recall(AGENT, QUERY, limit=5, time_cue=None) == plain
    assert await memory_handlers.do_recall(AGENT, QUERY, limit=5, time_cue={}) == plain
    assert "time_cue" not in plain


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["rrf", "rsf"])
async def test_a_cue_changes_order_not_admission(fake_embedding_client, monkeypatch, mode):
    await _seed(CORPUS)
    monkeypatch.setattr(memory_handlers, "RECALL_MODE", mode)
    # The period of the three oldest records, which the recall ranks last.
    time_cue = {"after": _ts(125)[:10], "before": _ts(95)[:10], "confidence": "sure"}
    plain = await memory_handlers.do_recall(AGENT, QUERY, limit=12, trace=True)
    cued = await memory_handlers.do_recall(AGENT, QUERY, limit=12, trace=True, time_cue=time_cue)

    def decisions(out):
        return sorted((d["ref"], d["passed"]) for d in out["trace"]["gate"]["decisions"])

    assert decisions(cued) == decisions(plain)  # the gate decided the same
    assert cued["trace"]["policy"]["process"] == "cued-v0"
    before = [row["ref"] for row in plain["trace"]["order"]["before_cut"]]
    after = [row["ref"] for row in cued["trace"]["order"]["before_cut"]]
    assert sorted(before) == sorted(after) and before != after
    for ref in after:
        assert before.index(ref) - after.index(ref) <= cue.LIFT["sure"]
    lifted = {m["ref"] for m in cued["trace"]["cue"]["lifted"] if m["to"] < m["from"]}
    assert lifted and lifted <= {row["ref"] for row in cued["trace"]["arms"]["cue"]}
    assert cued["time_cue"]["moved"] == len(cued["trace"]["cue"]["lifted"])
    by_ref = {m["ref"]: m for m in cued["messages"]}
    assert all("cue_rank" in by_ref[ref]["match_reason"] for ref in lifted)


# Six recent exact matches fill every ordinary arm at a count of two; three older
# records match the query only in part, so only a search of their period finds them.
SEAT_CORPUS = [(f"{QUERY} {d}", d) for d in range(1, 7)] + [(f"harbor lighthouse note {i}", 100 + 5 * i) for i in range(3)]


@pytest.mark.asyncio
async def test_a_record_only_the_cue_arm_found_takes_the_one_seat(fake_embedding_client, monkeypatch):
    refs = await _seed(SEAT_CORPUS)
    monkeypatch.setattr(memory_handlers, "RECALL_MODE", "rrf")
    time_cue = {"after": _ts(115)[:10], "before": _ts(95)[:10], "confidence": "sure"}
    plain = await memory_handlers.do_recall(AGENT, QUERY, limit=2)
    cued = await memory_handlers.do_recall(AGENT, QUERY, limit=2, trace=True, time_cue=time_cue)
    cue_only = {row["ref"] for row in cued["trace"]["arms"]["cue"]} - {
        row["ref"] for name, rows in cued["trace"]["arms"].items() if name != "cue" for row in rows}
    assert len(cue_only) >= 2, "the fixture must offer more cue-only records than seats"
    assert set(_refs(plain)) < set(_refs(cued)), "the seat displaced a row"
    seated = [m for m in cued["messages"] if m.get("match_reason", {}).get("signal") == "cue"]
    assert len(seated) == 1 and seated[0]["ref"] in cue_only and seated[0]["ref"] in set(refs[-3:])
    assert seated[0]["match_reason"]["admission"] == "reservation"
    assert cued["time_cue"]["seated"] == 1
    assert [r for r in cued["trace"]["reservation"] if r["kind"] == "cue"] == [{"ref": seated[0]["ref"], "kind": "cue"}]


@pytest.mark.asyncio
async def test_a_row_the_gate_refused_does_not_come_back_through_the_seat(fake_embedding_client, monkeypatch):
    await _seed(CORPUS)
    monkeypatch.setattr(memory_handlers, "RECALL_MODE", "rrf")
    monkeypatch.setattr(config, "FUSED_GATE_ENABLED", False)
    monkeypatch.setattr(memory_handlers, "_adaptive_min_score", lambda count: 10.0)
    time_cue = {"after": _ts(125)[:10], "before": _ts(95)[:10], "confidence": "sure"}
    out = await memory_handlers.do_recall(AGENT, QUERY, limit=12, trace=True, time_cue=time_cue)
    refused = {d["ref"] for d in out["trace"]["gate"]["decisions"] if not d["passed"]}
    assert {row["ref"] for row in out["trace"]["arms"]["cue"]} <= refused, "every cue row must be one the gate refused"
    assert out["trace"]["arms"]["cue"], "the cue arm must find rows for the seat to refuse"
    assert out["time_cue"]["seated"] == 0
    assert not any(m.get("match_reason", {}).get("signal") == "cue" for m in out["messages"])


@pytest.mark.asyncio
@pytest.mark.parametrize("fts", [True, False])
async def test_the_cue_arm_searches_only_the_period(fake_embedding_client, monkeypatch, fts):
    """Both halves of the cue arm: with keyword search off, the vector half alone."""
    refs = await _seed(CORPUS)
    monkeypatch.setattr(memory_handlers, "FTS_ENABLED", fts)
    # Days 45 to 75 hold the records at 51, 61 and 71 days.
    time_cue = {"after": _ts(75)[:10], "before": _ts(46)[:10], "confidence": "sure"}
    out = await memory_handlers.do_recall(AGENT, QUERY, limit=12, trace=True, time_cue=time_cue)
    in_period = {refs[5], refs[6], refs[7]}
    assert {row["ref"] for row in out["trace"]["arms"]["cue"]} == in_period


@pytest.mark.asyncio
async def test_an_empty_period_is_widened_once(fake_embedding_client):
    await _seed(CORPUS)
    # Days 3 to 8 hold nothing (records sit at 1, 11, 21, ...); widened by half its
    # length on each side it reaches day 1 and day 11.
    time_cue = {"after": _ts(8)[:10], "before": _ts(3)[:10], "confidence": "sure"}
    out = await memory_handlers.do_recall(AGENT, QUERY, limit=5, trace=True, time_cue=time_cue)
    stages = out["trace"]["stages"]
    assert [s["confidence"] for s in stages] == ["sure", "likely"]
    assert stages[0]["found"] == 0 and stages[0]["next"] == "widen to the likely margin"
    assert stages[1]["found"] > 0 and stages[1]["searched"] == "the cue period only"
    assert out["trace"]["suspected"] == [{"stage": 0, "code": "CANDIDATE_MISS",
                                         "reason": "the cue period holds no candidate"}]
    assert out["time_cue"]["revised"] is True and out["time_cue"]["confidence"] == "likely"


@pytest.mark.asyncio
async def test_a_vague_cue_that_finds_nothing_stops(fake_embedding_client):
    await _seed(CORPUS)
    time_cue = {"after": "2020-01-01", "before": "2020-01-02", "confidence": "vague"}
    out = await memory_handlers.do_recall(AGENT, QUERY, limit=5, trace=True, time_cue=time_cue)
    plain = await memory_handlers.do_recall(AGENT, QUERY, limit=5)
    assert len(out["trace"]["stages"]) == 1
    assert out["trace"]["stages"][0]["next"].startswith("stop: no wider period")
    assert _refs(out) == _refs(plain)


@pytest.mark.asyncio
async def test_the_revision_stops_at_the_time_limit(fake_embedding_client, monkeypatch):
    await _seed(CORPUS)
    monkeypatch.setattr(config, "RECALL_CUE_TIME_LIMIT_MS", 0)
    time_cue = {"after": _ts(8)[:10], "before": _ts(3)[:10], "confidence": "sure"}
    out = await memory_handlers.do_recall(AGENT, QUERY, limit=5, trace=True, time_cue=time_cue)
    assert [s["next"] for s in out["trace"]["stages"]] == ["stop: time limit"]


@pytest.mark.asyncio
async def test_the_cue_never_widens_isolation(fake_embedding_client):
    # Another agent holds records in the cue's period; this agent holds one elsewhere.
    other_refs = set(await _seed(CORPUS, agent=OTHER))
    await _seed([("harbor lighthouse keeper logbook of this agent", 300)])
    time_cue = {"after": _ts(125)[:10], "before": _ts(95)[:10], "confidence": "vague"}
    out = await memory_handlers.do_recall(AGENT, QUERY, limit=5, trace=True, time_cue=time_cue)
    assert out["messages"], "this agent's own record must still be found"
    assert not other_refs & set(_refs(out))
    assert not other_refs & {row["ref"] for rows in out["trace"]["arms"].values() for row in rows}


@pytest.mark.asyncio
async def test_the_tool_refuses_a_cue_it_cannot_read(fake_embedding_client):
    await _seed(CORPUS)
    out = await server.do_recall_boundary(
        AGENT, QUERY, 5, False, "", [], None, "", time_cue={"after": "2026-08-01"}
    )
    assert out["messages"] == [] and "confidence is required" in out["error"]


@pytest.mark.asyncio
async def test_reconstruct_passes_the_cue_to_its_recall(fake_embedding_client):
    from cpersona import reconstruct

    await _seed(CORPUS)
    time_cue = {"after": _ts(125)[:10], "before": _ts(95)[:10], "confidence": "likely"}
    out = await reconstruct.do_reconstruct(AGENT, QUERY, count=3, trace=True, time_cue=time_cue)
    assert out["trace"]["recall"]["policy"]["process"] == "cued-v0"
    assert out["time_cue"]["confidence"] == "likely"
    assert "time_cue" not in await reconstruct.do_reconstruct(AGENT, QUERY, count=3)
    refused = await server.do_reconstruct_boundary(
        AGENT, QUERY, 3, None, None, None, False, "", None, "", time_cue={"ago": "yesterday", "confidence": "sure"}
    )
    assert refused["items"] == [] and "long_ago" in refused["error"]


@pytest.mark.asyncio
async def test_an_empty_query_searches_the_period_by_recency(fake_embedding_client):
    refs = await _seed(CORPUS)
    time_cue = {"after": _ts(75)[:10], "before": _ts(46)[:10], "confidence": "sure"}
    out = await memory_handlers.do_recall(AGENT, "", limit=3, trace=True, time_cue=time_cue)
    assert [row["ref"] for row in out["trace"]["arms"]["cue"]] == [refs[5], refs[6], refs[7]]
    assert out["trace"]["stages"][0]["found"] == 3
