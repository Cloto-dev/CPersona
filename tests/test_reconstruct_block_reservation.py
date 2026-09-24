"""reconstruct and the block reservation: docs/BLOCK_REACH_DESIGN.md §5, docs/RELIABLE_RECALL_2_6.md §7.

recall holds a fixed number of places for records only the block arm reached and
returns them beside the rows the gate admitted. reconstruct reads the same recall,
at its own candidate depth, and used to rank those rows with everything else: a
reserved row has no gate score, so it sits at the bottom of the pool and a window
of `count` items never reached it. What is pinned here is that reconstruct gives
the reservation the same shape recall does — places of its own, beside the window,
displacing nothing the window holds.
"""

import pytest

import test_blocks_retrieval as reach
from cpersona import blocks, config, memory_handlers, reconstruct

AGENT = reach.AGENT
QUERY = reach.TAIL_SUBJECT


@pytest.fixture
def building(monkeypatch, fake_embedding_client):
    monkeypatch.setattr(config, "BLOCK_BUILD_ENABLED", True)
    return fake_embedding_client


@pytest.fixture
def reading(monkeypatch, building):
    monkeypatch.setattr(config, "BLOCK_RETRIEVAL_ENABLED", True)
    return building


@pytest.fixture
def lexical_off(monkeypatch):
    # As in test_blocks_retrieval: the lexical arm reaches the tail by keyword, so
    # with it on these tests would pass with the block arm removed.
    monkeypatch.setattr(memory_handlers, "FTS_ENABLED", False)


def _heads(out):
    return [item["head_ref"] for item in out["items"]]


def _reserved(out):
    return [item["head_ref"] for item in out["items"] if item.get("admission") == "reservation"]


def _ranked(out):
    return [item["head_ref"] for item in out["items"] if item.get("admission") != "reservation"]


async def _long_and_echo(tmp):
    """The long record holds the answer past its own vector's reach; the echo says
    only the subject. Returns their refs as (long, echo)."""
    long_id, echo_id = await reach._store(tmp, [reach.LONG_RECORD, reach.ECHO_RECORD])
    return f"mem:{long_id}", f"mem:{echo_id}"


# --------------------------------------------------------------------------
# the defect: a reserved row never reached a window the gate had filled
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recall_reserves_the_tail_for_the_same_query(reading, lexical_off):
    """The precondition, so the reconstruct tests below cannot pass vacuously:
    the recall reconstruct reads does carry the tail, in a reserved place."""
    async with reach._TempDB() as tmp:
        long_ref, _ = await _long_and_echo(tmp)
        out = await memory_handlers.do_recall(AGENT, QUERY, limit=20)
        reserved = [m["ref"] for m in out["messages"] if m.get("match_reason", {}).get("admission") == "reservation"]
        assert reserved == [long_ref]


@pytest.mark.asyncio
async def test_a_full_window_still_returns_the_reserved_record(reading, lexical_off):
    async with reach._TempDB() as tmp:
        long_ref, echo_ref = await _long_and_echo(tmp)
        out = await reconstruct.do_reconstruct(AGENT, QUERY, count=1)
        assert _ranked(out) == [echo_ref]
        assert _reserved(out) == [long_ref], "the record only the block arm reached did not come back"
        assert out["reserved_count"] == 1


@pytest.mark.asyncio
async def test_a_reserved_record_never_takes_a_place_in_the_window(reading, lexical_off):
    """With room in the window, a reserved row used to be ranked as an ordinary
    item. It is a reservation whether or not the window is full, so a caller can
    always tell which items the gate admitted."""
    async with reach._TempDB() as tmp:
        long_ref, echo_ref = await _long_and_echo(tmp)
        out = await reconstruct.do_reconstruct(AGENT, QUERY, count=5)
        assert _ranked(out) == [echo_ref]
        assert _reserved(out) == [long_ref]


# --------------------------------------------------------------------------
# the reservation displaces nothing (§5)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turning_the_arm_on_keeps_every_item_of_the_window(monkeypatch, building, lexical_off):
    async with reach._TempDB() as tmp:
        _, echo_ref = await _long_and_echo(tmp)
        before = await reconstruct.do_reconstruct(AGENT, QUERY, count=1)
        assert _heads(before) == [echo_ref], "the baseline changed, so 'keeps every item' is not what is tested"
        assert "reserved_count" not in before

        monkeypatch.setattr(config, "BLOCK_RETRIEVAL_ENABLED", True)
        after = await reconstruct.do_reconstruct(AGENT, QUERY, count=1)

        assert _ranked(after) == _heads(before), "an item of the window was displaced"
        assert _heads(after)[: len(_heads(before))] == _heads(before), "the reserved item was not placed after the window"


@pytest.mark.asyncio
async def test_the_reservation_is_the_same_fixed_bound(monkeypatch, reading, lexical_off):
    async with reach._TempDB() as tmp:
        await reach._store(
            tmp,
            [
                reach.LONG_RECORD,
                reach.LONG_RECORD.replace("pilot", "trial"),
                reach.LONG_RECORD.replace("pilot", "rollout"),
                reach.ECHO_RECORD,
            ],
        )
        full = await reconstruct.do_reconstruct(AGENT, QUERY, count=1)
        assert len(_reserved(full)) == 2, "the fixture does not fill two places, so the bound below is not exercised"

        monkeypatch.setattr(blocks, "BLOCK_RESERVATION", 1)
        out = await reconstruct.do_reconstruct(AGENT, QUERY, count=1)
        assert len(_reserved(out)) == 1


@pytest.mark.asyncio
async def test_a_count_of_zero_reserves_nothing(reading, lexical_off):
    async with reach._TempDB() as tmp:
        await _long_and_echo(tmp)
        out = await reconstruct.do_reconstruct(AGENT, QUERY, count=0)
        assert out["items"] == []


# --------------------------------------------------------------------------
# what the response says about it
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_short_window_is_reported_by_the_items_the_gate_admitted(reading, lexical_off):
    """One admitted item and one reserved one against a window of two: the window
    is short, and a reserved item does not fill it."""
    async with reach._TempDB() as tmp:
        await _long_and_echo(tmp)
        out = await reconstruct.do_reconstruct(AGENT, QUERY, count=2)
        assert len(out["items"]) == 2 and len(_reserved(out)) == 1
        assert out["shortfall_reason"] == reconstruct.SHORTFALL_EXHAUSTED_CANDIDATES


@pytest.mark.asyncio
async def test_the_default_budget_holds_the_reserved_places_too(reading, lexical_off):
    """The default budget fits one head per item of the window; a reserved item is
    an item, so the default widens by the places filled, and only then."""
    async with reach._TempDB() as tmp:
        await _long_and_echo(tmp)
        count = 5
        head = config.RECONSTRUCT_QUOTE_CHARS
        assert count * head >= config.RECONSTRUCT_DEFAULT_BUDGET, "the window alone must set the default for this test"
        out = await reconstruct.do_reconstruct(AGENT, QUERY, count=count, trace=True)
        assert out["reserved_count"] == 1
        assert out["effective_budget"] == (count + 1) * head


@pytest.mark.asyncio
async def test_without_a_reservation_the_default_budget_is_unchanged(building, lexical_off):
    async with reach._TempDB() as tmp:
        await _long_and_echo(tmp)
        out = await reconstruct.do_reconstruct(AGENT, QUERY, count=5, trace=True)
        assert "reserved_count" not in out
        assert out["effective_budget"] == reconstruct.resolve_budget(None, 5)[0]


@pytest.mark.asyncio
async def test_a_held_item_the_budget_leaves_out_is_reported(reading, lexical_off):
    """A caller's budget is not widened; the held item, being last, is what it cuts
    first, and the response says so rather than returning one item silently."""
    async with reach._TempDB() as tmp:
        _, echo_ref = await _long_and_echo(tmp)
        floor = config.RECALL_PREVIEW_CHARS
        out = await reconstruct.do_reconstruct(AGENT, QUERY, count=1, budget=floor)
        assert _heads(out) == [echo_ref]
        assert out["reserved_omitted"] == 1
        assert "reserved_count" not in out
        assert out["effective_budget"] == floor, "a named budget was widened for the held place"
