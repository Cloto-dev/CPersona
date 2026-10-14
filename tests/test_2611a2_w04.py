"""Block division cost, the backfill's outage guard, and a capability ask that fails.

Three regressions around the block layer:

- the divider is linear in the record's length on text that leaves brackets
  open, closes brackets nothing opened, or holds many code fences — it runs on
  recall's excerpt path and inside reconstruct, on the event loop;
- a backfill run that met an embedding backend that is down raises (so the
  queue retries the same cursor) even when the corpus also holds records that
  divide into one block, which are decided offline and say nothing about the
  backend;
- one ``/capabilities`` ask that fails after a fingerprint was learned leaves
  the keys on that fingerprint, so block and node sets stamped with it stay
  current, and the backend is asked again well before the full interval.
"""

import random
import time

import pytest

from cpersona import blocks, config, generation
from cpersona.isolation import isolation_where
from test_blocks_backfill import ONE, _many, _seed, _TempDB
from test_generation_key import (
    FINGERPRINT,
    OTHER,
    Backend,
    _identity,
    _row_written_under,
)
from test_generation_key import _TempDB as _GenTempDB


# --------------------------------------------------------------------------
# the divider is linear on unbalanced brackets and on fences
# --------------------------------------------------------------------------


def _best_of(text: str, runs: int = 3) -> float:
    best = float("inf")
    for _ in range(runs):
        t0 = time.perf_counter()
        blocks.segment(text)
        best = min(best, time.perf_counter() - t0)
    return best


_PROSE = ("The deploy was rolled back after the canary failed. " * 400)[:16000]


@pytest.mark.parametrize(
    "shape, text",
    [
        ("unclosed openers then prose", "(" * 8000 + "a" * 8000),
        ("unclosed openers then stray closers", "(" * 8000 + "]" * 8000),
        ("many code fences", ("```\n\n" * 3200)[:16000]),
    ],
    ids=["unclosed-openers", "stray-closers", "fences"],
)
def test_division_of_a_pathological_record_costs_what_prose_costs(shape, text):
    """A record of the default maximum length, shaped to defeat the divider, must
    cost the same order as prose of that length. Before, each character scanned
    every pending opener and every boundary scanned every fence: seconds of
    event-loop time for one 16,000-character record."""
    assert len(text) == len(_PROSE)
    t_prose = _best_of(_PROSE)
    t_bad = _best_of(text)
    assert t_bad < max(20 * t_prose, 0.05), (
        f"{shape}: {t_bad * 1000:.0f} ms against {t_prose * 1000:.1f} ms for prose"
    )


def _reference_depth(text: str) -> list[int]:
    """The bracket rule, written the slow and obvious way, as an oracle."""
    pending: list[tuple[str, int]] = []
    unmatched: set[int] = set()
    for i, ch in enumerate(text):
        if ch in blocks._BRACKETS:
            pending.append((blocks._BRACKETS[ch], i))
            continue
        for d in range(len(pending) - 1, -1, -1):
            if pending[d][0] == ch:
                unmatched.update(pos for _, pos in pending[d + 1 :])
                del pending[d:]
                break
    unmatched.update(pos for _, pos in pending)
    depth = [0] * (len(text) + 1)
    stack: list[str] = []
    for i, ch in enumerate(text):
        depth[i] = len(stack)
        if ch in blocks._BRACKETS and i not in unmatched:
            stack.append(blocks._BRACKETS[ch])
        elif stack and ch == stack[-1]:
            stack.pop()
    depth[len(text)] = len(stack)
    return depth


def _reference_cuts(text: str) -> set[int]:
    protected = blocks._fence_ranges(text)
    depth = _reference_depth(text)
    cuts = set()
    for pattern in blocks._BOUNDARIES:
        for m in pattern.finditer(text):
            end = m.end()
            if end <= 0 or end >= len(text):
                continue
            if any(a < end < b for a, b in protected):
                continue
            if depth[end] > 0:
                continue
            cuts.add(end)
    return cuts


def test_the_linear_divider_draws_the_same_cuts_as_the_rule():
    """Speed bought by changing where the divider cuts would be a different
    divider. Random text over the brackets, fences and boundaries the rule reads,
    compared against the rule written out directly."""
    rng = random.Random(20261014)
    alphabet = list("「」『』（）()［］[]【】《》〈〉") + ["a", "b", " ", ".", "。", "\n", "\n\n", "```\n"]
    for _ in range(3000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 60)))
        assert blocks._bracket_depth(text) == _reference_depth(text), repr(text)
        assert blocks._structural_cuts(text) == _reference_cuts(text), repr(text)


# --------------------------------------------------------------------------
# the backfill's outage guard is not defeated by single-block records
# --------------------------------------------------------------------------


@pytest.fixture
def enabled(monkeypatch, fake_embedding_client):
    monkeypatch.setattr(config, "BLOCK_BUILD_ENABLED", True)
    return fake_embedding_client


@pytest.mark.asyncio
async def test_an_outage_raises_even_when_the_corpus_holds_a_single_block_record(monkeypatch, enabled):
    """A record that divides into one block is decided offline, so finding one
    says nothing about whether the backend answers. Counting it as success let a
    sweep that met a down backend report the corpus swept and move its cursor
    past every record it failed to build."""
    async with _TempDB() as tmp:
        seeded = await _seed(tmp, monkeypatch, [ONE, _many(1), _many(2)])

        async def broken(texts):
            raise RuntimeError("the embedding server is down")

        monkeypatch.setattr(enabled, "embed", broken)
        with pytest.raises(RuntimeError, match="failed and none were built"):
            await blocks.backfill({})

        assert await tmp.sweeps_queued() == [], "a run that achieved nothing queued a successor"
        for row_id, _ in seeded[1:]:
            assert await tmp.blocks_of(parent_id=row_id) == []


@pytest.mark.asyncio
async def test_a_run_that_built_something_beside_single_block_records_still_succeeds(monkeypatch, enabled):
    """The guard still reads a run that reached the backend as a success."""
    async with _TempDB() as tmp:
        await _seed(tmp, monkeypatch, [ONE, _many(1)])
        report = await blocks.backfill({})
        assert report.startswith("backfill swept the corpus"), report
        assert "built 1 records" in report and "1 needed none" in report, report


# --------------------------------------------------------------------------
# one failed capability ask does not move the keys off a learned fingerprint
# --------------------------------------------------------------------------


@pytest.fixture
def forget():
    generation.reset()
    yield
    generation.reset()


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


@pytest.fixture
def clock(monkeypatch, forget):
    c = _Clock()
    monkeypatch.setattr(generation, "time", c)
    return c


@pytest.mark.asyncio
async def test_a_failed_ask_after_a_fingerprint_keeps_its_block_sets_current(clock):
    async with _GenTempDB():
        from cpersona import database

        db = await database.get_db()
        await generation.refresh(Backend(_identity(FINGERPRINT)))
        await _row_written_under(db, FINGERPRINT, parent_id=1, text_len=12)
        keys = generation.block_keys()
        node_keys = generation.node_keys()
        assert await blocks._blocks_current(db, "mem", 1, 12, keys) is True

        # The interval lapses, and the next ask meets the backend mid-restart.
        clock.now += generation.REFRESH_INTERVAL_SECONDS + 1
        assert await generation.refresh(Backend(None, error="GET /capabilities failed")) == FINGERPRINT

        assert generation.block_keys() == keys
        assert generation.node_keys() == node_keys
        assert generation.fingerprint() == FINGERPRINT
        after = generation.block_keys()
        assert await blocks._blocks_current(db, "mem", 1, 12, after) is True
        examined = {r[1] for r in await blocks._examined(db, isolation_where(agent_id="a"), after)}
        assert examined == {1}, "the block arm stopped examining the fingerprint's rows"


@pytest.mark.asyncio
async def test_an_incomplete_identity_after_a_fingerprint_keeps_the_keys(clock):
    """A backend that answers but cannot complete its identity has not named a
    different one either."""
    await generation.refresh(Backend(_identity(FINGERPRINT)))
    clock.now += generation.REFRESH_INTERVAL_SECONDS + 1
    await generation.refresh(Backend(_identity(None, incomplete=("digests.graph",))))
    assert generation.block_keys()[0] == FINGERPRINT


@pytest.mark.asyncio
async def test_after_a_failed_ask_the_backend_is_asked_again_sooner(clock):
    await generation.refresh(Backend(_identity(FINGERPRINT)))
    clock.now += generation.REFRESH_INTERVAL_SECONDS + 1
    await generation.refresh(Backend(None, error="GET /capabilities failed"))

    # Well inside the full interval, a backend that answers again is asked.
    clock.now += generation.REFRESH_INTERVAL_SECONDS / 2
    back = Backend(_identity(OTHER))
    await generation.refresh(back)
    assert back.asked == 1, "a failed ask was cached for the whole interval"
    # A positive, different report is what moves the key.
    assert generation.block_keys()[0] == OTHER

    # And once answered, the ordinary interval applies again.
    clock.now += 1
    again = Backend(_identity(FINGERPRINT))
    await generation.refresh(again)
    assert again.asked == 0
