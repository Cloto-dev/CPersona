"""bug-476: a deep recall does not apply the age weight.

``deep`` is documented to disable time decay, and the age weight
(``CPERSONA_PRIOR_AGE_RATE``) is a time term of the same family. Before the fix a deep
recall was still reordered by age whenever the rate was above 0. The control is the
same recall without ``deep``: there the same steep weight changes the order, so the
equality under ``deep`` cannot hold because the weight never had anything to move.
"""
import pytest

from cpersona import memory_handlers
from tests.test_260a7_prior_function import AGENT, QUERY, _seed


async def _recall(deep):
    return (await memory_handlers.do_recall(AGENT, QUERY, limit=50, deep=deep))["messages"]


@pytest.mark.asyncio
async def test_a_deep_recall_is_not_reordered_by_age(fake_embedding_client, monkeypatch):
    await _seed()
    monkeypatch.setattr(memory_handlers, "RECALL_MODE", "rrf")
    monkeypatch.setattr(memory_handlers, "CONFIDENCE_ENABLED", False)
    monkeypatch.setattr(memory_handlers, "PRIOR_AGE_FLOOR", 0.0)

    monkeypatch.setattr(memory_handlers, "PRIOR_AGE_RATE", 0.0)
    deep_plain = await _recall(deep=True)
    shallow_plain = await _recall(deep=False)
    monkeypatch.setattr(memory_handlers, "PRIOR_AGE_RATE", 1.0)
    deep_weighted = await _recall(deep=True)
    shallow_weighted = await _recall(deep=False)

    # Control: without deep the steep weight moves the rows, so it is live here.
    assert [m["ref"] for m in shallow_weighted] != [m["ref"] for m in shallow_plain]
    assert all("prior" in m["match_reason"] for m in shallow_weighted)
    # Under deep the weight leaves the order and the reasons as they were.
    assert [m["ref"] for m in deep_weighted] == [m["ref"] for m in deep_plain]
    assert all("prior" not in m["match_reason"] for m in deep_weighted)
