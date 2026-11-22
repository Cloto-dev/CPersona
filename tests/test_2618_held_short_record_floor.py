"""bug-580: under a mode a held item with a short record joins the floor of whole records.

`whole_order` quotes every record no longer than a quote first, whole, in item order,
and under `lite` (and `pro`'s first 10 items) the floor takes held items too. A held
record that is short is therefore quoted ahead of the passages of the window's longer
records, and inside lite's 2,800 characters it can leave out a window item that the
call would otherwise return. The behaviour is kept: it is the order the registered
tests of 2.6.9 and 2.6.13 measured, and held items after every window passage would
leave lite almost nothing for the keyword seat. The contract now says so
(docs/RELIABLE_RECALL_2_6.md section 7); this pins the case the audit reproduced, and
the call without a mode, whose held items still come last.
"""
import pytest

from cpersona import memory_handlers as M
from cpersona import reconstruct as R
from tests.test_keyword_seats import AGENT, STRONG, _fresh, _gate, _insert, _pack_of  # noqa: F401

# A record of one long sentence holding the question's words: its first passage is long.
LONG = [
    f"apples orchard harvest notes record {i} " + " ".join(f"orchard{i}w{j}" for j in range(220))
    for i in range(5)
]
# Short enough to be quoted whole (under 800 characters), found by the keyword arms only.
HELD = "apples " + " ".join(f"zq{j}" for j in range(150))


async def _store(monkeypatch):
    for i, text in enumerate(LONG):
        await _insert(text, _pack_of(STRONG), f"2026-01-01T00:00:0{i}Z")
    await _insert(HELD, _pack_of("completely different unrelated content xxxx"), "2026-01-01T00:00:09Z")
    _gate("rrf", 0.05)
    monkeypatch.setattr(M, "RECALL_MODE", "rrf")


def _split(out):
    window = [it["content"] for it in out["items"] if it.get("admission") != "reservation"]
    held = [it["content"] for it in out["items"] if it.get("admission") == "reservation"]
    return window, held


@pytest.mark.asyncio
async def test_under_lite_a_short_held_record_is_quoted_whole_ahead_of_a_window_item(monkeypatch):
    await _store(monkeypatch)
    assert len(HELD) < 800

    with_seat = await R.do_reconstruct(AGENT, "apples orchard", count=3, mode="lite")
    monkeypatch.setattr(R, "MODE_KEYWORD_SEATS", 0)
    without_seat = await R.do_reconstruct(AGENT, "apples orchard", count=3, mode="lite")

    window, held = _split(with_seat)
    window_alone, held_alone = _split(without_seat)
    assert held_alone == [] and len(window_alone) == 2, without_seat["items"]
    assert held == [HELD], "the short held record was not quoted whole"
    # The floor put it ahead of the second window item's passage, which no longer fits.
    assert len(window) == 1, with_seat["items"]
    assert with_seat.get("shortfall_reason") == "budget_exhausted"


@pytest.mark.asyncio
async def test_without_a_mode_held_items_come_after_the_window(monkeypatch):
    await _store(monkeypatch)

    out = await R.do_reconstruct(AGENT, "apples orchard", count=3)

    window, held = _split(out)
    assert len(window) == 3 and held == [HELD], out["items"]
    kinds = [it.get("admission") for it in out["items"]]
    assert kinds == [None, None, None, "reservation"], kinds
