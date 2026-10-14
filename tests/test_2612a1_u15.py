"""Bounds on the work one reconstruct call does.

* ``max_evidence`` has a ceiling. It is the per-entity read limit of the relation walk
  and the number of claims an item keeps, and structure() is quadratic in those claims,
  so without a ceiling one call could read and rank every record a reached entity is
  mentioned by. It shares traverse's per-entity ceiling, the same read, the way
  ``max_hops`` shares traverse's hop ceiling (bug-459).
* The evidence sequence of a mode call stays near-linear in the blocks of a head
  record. A record of sentence terminators divides into one block per character, and
  the passage, cost and cut steps each re-measured the record at every step, so one
  stored record kept the event loop busy for seconds to minutes.
"""

import asyncio
import gc
import random
import time

import pytest
import pytest_asyncio

from cpersona import associations, config, excerpts, server, session
from cpersona import memory_handlers as M
from cpersona import reconstruct as R
from cpersona.database import get_db

AGENT = "u15-bounds"
QUERY = "rollback"
CEILING = associations.TRAVERSE_MAX_LIMIT


@pytest_asyncio.fixture(autouse=True)
async def clean_db():
    session.reset_pauses_for_tests()
    db = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks", "entities",
                  "entity_aliases", "entity_mentions", "relations", "record_nodes"):
        await db.execute(f"DELETE FROM {table}")
    await db.execute(
        "DELETE FROM sqlite_sequence WHERE name IN "
        "('memories','episodes','profiles','pending_memory_tasks','entities','relations')"
    )
    await db.commit()
    yield db
    session.reset_pauses_for_tests()


async def _hub(mentioning: int) -> str:
    """A seed record mentions Alpha, Alpha is part of Hub, and ``mentioning`` records
    that share no word with the query mention Hub. Returns the seed's ref."""
    # A background corpus that shares no word with the query: with a handful of rows
    # the adaptive quality gate is at its strictest and drops the seed itself.
    for i in range(40):
        out = await M.do_store(AGENT, {"content": f"ambient telemetry sample {i}"})
        assert out["result"] == "stored", out
    seed = await M.do_store(AGENT, {"content": "rollback plan for the release",
                                    "timestamp": "2026-03-01T00:00:00+00:00"})
    assert seed["result"] == "stored", seed
    report = await associations.declare(AGENT, {"entities": [{"name": "Alpha"}]}, anchor_ref=f"mem:{seed['id']}")
    assert not report["dropped"], report
    report = await associations.declare(
        AGENT, {"relations": [{"subject": "Alpha", "predicate": "part_of", "object": "Hub"}]}
    )
    assert not report["dropped"] and report["relations"], report
    for i in range(mentioning):
        out = await M.do_store(AGENT, {
            "content": f"hub note number {i} about deployment {i * 7}",
            "timestamp": f"2026-04-01T{(i // 60) % 24:02d}:{i % 60:02d}:00+00:00",
        })
        assert out["result"] == "stored", out
        report = await associations.declare(AGENT, {"entities": [{"name": "Hub"}]}, anchor_ref=f"mem:{out['id']}")
        assert not report["dropped"], report
    return f"mem:{seed['id']}"


def _walked(item: dict) -> int:
    return sum(1 for claim in item["claims"] if "hops" in claim)


# --- max_evidence has a ceiling ---------------------------------------------------------


@pytest.mark.asyncio
async def test_max_evidence_shares_traverse_s_per_entity_ceiling():
    """A caller-chosen max_evidence no longer lifts the walk's read bound: the item
    keeps at most the ceiling's claims, and says the bound was lowered to it."""
    seed = await _hub(CEILING + 20)
    huge = await R.do_reconstruct(AGENT, QUERY, count=1, max_evidence=1_000_000)
    item = huge["items"][0]
    assert item["head_ref"] == seed, item
    assert _walked(item) > 0, "the walk reached nothing, so the bound was never exercised"
    assert len(item["claims"]) <= CEILING, len(item["claims"])
    assert huge["bounds"]["max_evidence"] == CEILING, huge.get("bounds")
    assert huge["items"] == (await R.do_reconstruct(AGENT, QUERY, count=1, max_evidence=CEILING))["items"]


@pytest.mark.asyncio
async def test_a_lowered_evidence_bound_is_reported_even_when_nothing_else_is():
    """The compact response drops `bounds` when the call was served as asked. A lowered
    max_evidence was not, so bounds stays and states the value applied."""
    await _hub(3)  # nothing is cut or reached at the ceiling
    kw = {"count": 1, "top_k": 100}  # deeper than the store, so top_k is never reached
    asked = await R.do_reconstruct(AGENT, QUERY, max_evidence=CEILING, **kw)
    assert "bounds" not in asked, asked.get("bounds")
    lowered = await R.do_reconstruct(AGENT, QUERY, max_evidence=1_000_000, **kw)
    assert lowered.get("bounds", {}).get("max_evidence") == CEILING, lowered.get("bounds")
    assert lowered["items"] == asked["items"]


@pytest.mark.asyncio
async def test_a_max_evidence_past_the_sqlite_integer_range_is_served():
    """2**63 reached the walk's SQL LIMIT as per_entity + 1 and raised OverflowError
    out of the tool, losing the documented items shape."""
    await _hub(3)
    out = await server.do_reconstruct_boundary(AGENT, QUERY, 1, None, None, 2 ** 63, False, "", None, "")
    assert "error" not in out, out
    assert out["items"] and out["bounds"]["max_evidence"] == CEILING, out


def test_the_schema_states_the_evidence_ceiling():
    props = next(t for t in server.registry._tools if t.name == "reconstruct").inputSchema["properties"]
    assert props["max_evidence"]["maximum"] == CEILING
    assert props["max_evidence"]["minimum"] == 1


# --- the evidence sequence is near-linear in a record's blocks --------------------------


def _reference_costs(order, records):
    """The step costs as the quote measures them: the whole quote of the record re-joined
    before and after each step."""
    def quoted(ranges):
        joined = excerpts._joined(sorted(ranges))
        return sum(e - s for s, e in joined) + len(excerpts.SEPARATOR) * max(0, len(joined) - 1)

    shown: dict[int, list] = {}
    costs = []
    for i, j, _ in order:
        before = shown.get(i, [])
        after = before + [tuple(records[i][j][:2])]
        costs.append(quoted(after) - quoted(before))
        shown[i] = after
    return costs


def _random_records(rng: random.Random) -> list[list[tuple]]:
    """Records of non-overlapping passages, in a random rank order, many of them touching."""
    records = []
    for _ in range(rng.randint(1, 4)):
        cuts, at = [], 0
        for _ in range(rng.randint(1, 25)):
            at += rng.choice((0, 0, 1, 3))  # a gap of 0 makes the next passage touch
            length = rng.randint(1, 9)
            cuts.append((at, at + length, None, None))
            at += length
        rng.shuffle(cuts)
        records.append(cuts)
    return records


def test_step_costs_are_what_each_step_adds_to_the_quote():
    """The incremental costs equal the quote lengths measured from scratch, and the cut
    at every budget is the longest prefix whose costs fit."""
    rng = random.Random(2612)
    for _ in range(300):
        records = _random_records(rng)
        order = [(i, j, None) for i, passages in enumerate(records) for j in range(len(passages))]
        rng.shuffle(order)
        expected = _reference_costs(order, records)
        assert R.sequence_costs(order, records) == expected
        total = sum(expected)
        for budget in {0, 1, total // 3, total // 2, total - 1, total, total + 5}:
            taken, used = R.cut_sequence(order, records, budget)
            running, n = 0, 0
            for position, cost in enumerate(expected):
                if position and running + cost > budget:
                    break
                running, n = running + cost, n + 1
            want: dict[int, list[int]] = {}
            for i, j, _ in order[:n]:
                want.setdefault(i, []).append(j)
            assert (taken, used) == (want, running), (order, records, budget)


def _terminators(n: int) -> str:
    # One block per character: the divider cuts at every sentence terminator.
    return "kiwi orchard note " + "。" * n


class _Claim:
    def __init__(self, content):
        self.ref, self.kind, self.content = "mem:1", "mem", content


def _best_of(fn, repeat=5) -> float:
    """The fastest of a few runs, with the collector paused, so a ratio of two of them
    measures the work rather than a pause that landed in one."""
    best = float("inf")
    enabled = gc.isenabled()
    gc.disable()
    try:
        for _ in range(repeat):
            t0 = time.perf_counter()
            fn()
            best = min(best, time.perf_counter() - t0)
    finally:
        if enabled:
            gc.enable()
    return best


def _passages(n: int):
    _, _, passages = R.record_passages(_Claim(_terminators(n)), None, None, R._trigrams("kiwi orchard"), None, 800)
    return passages


def test_record_passages_grows_near_linearly_with_the_blocks():
    """Four times the blocks costs about four times the work, not sixteen: a range is
    checked against the better ones already taken by a search, not by a scan of all."""
    assert len(_passages(4000)) > 3000, "the record did not divide per character"
    small = _best_of(lambda: _passages(3000))
    large = _best_of(lambda: _passages(12000))
    assert large < 9 * small, (small, large)


def test_the_sequence_costs_and_cut_grow_near_linearly_with_the_passages():
    """sequence_costs and cut_sequence re-joined a record's whole quote at every step."""
    def run(n):
        records = [[(k, k + 1, None, None) for k in range(n)]]
        order = R.whole_order(records, [False])
        costs = R.sequence_costs(order, records)
        R.cut_sequence(order, records, sum(costs))

    small = _best_of(lambda: run(2000))
    large = _best_of(lambda: run(8000))
    assert large < 9 * small, (small, large)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["lite", "pro"])
async def test_one_long_record_does_not_hold_the_event_loop(mode):
    """The claim's shape end to end: a record of sentence terminators under the content
    cap, quoted by a mode call, while a ticker on the same loop measures the longest gap."""
    text = _terminators(15_900)
    assert len(text) <= config.MAX_CONTENT_LENGTH
    out = await M.do_store(AGENT, {"content": text})
    assert out["result"] == "stored" and not out.get("truncated"), out
    for i in range(5):
        await M.do_store(AGENT, {"content": f"unrelated filler {i}"})

    gaps: list[float] = []
    stop = False

    async def ticker():
        last = time.perf_counter()
        while not stop:
            await asyncio.sleep(0.02)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0.1)
    result = await R.do_reconstruct(AGENT, "kiwi orchard", count=3, mode=mode)
    stop = True
    await task
    assert result.get("items"), result
    assert max(gaps) < 3.0, f"the event loop was held for {max(gaps):.2f}s"
