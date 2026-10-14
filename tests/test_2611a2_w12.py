"""Regression tests: what a reconstruct response hands back must still hold.

  bug-578  A raw-span expand (a head quote cut from the record's start, a severed
           best passage, and the passages a mode's evidence sequence took) carried no
           revision, while every block expand did. get_contents checks a revision only
           when one is given, so after the record was rewritten it served the new text
           under the old offsets instead of refusing.
  bug-582  Under a mode, the cap could trim only the passage sequence. The first item's
           claims list (bounded by max_evidence, not by the cap) was not trimmable, so a
           chat burst bundled into one item made lite refuse with cap_below_minimum and a
           detail blaming the first passage, which was a few dozen tokens long.
"""

import os
import tempfile

os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "test_2611a2_w12.db"))
os.environ.setdefault("CPERSONA_EMBEDDING_MODE", "none")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from cpersona import admin_handlers, blocks, config, session, tokens  # noqa: E402
from cpersona import memory_handlers as M  # noqa: E402
from cpersona import reconstruct as R  # noqa: E402
from cpersona.database import get_db  # noqa: E402

AGENT = "w12-reconstruct"


@pytest_asyncio.fixture(autouse=True)
async def clean_db():
    session.reset_pauses_for_tests()
    db = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks", "entities",
                  "entity_aliases", "entity_mentions", "relations", "record_nodes"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()
    yield db
    session.reset_pauses_for_tests()


async def _filler(n: int = 30) -> None:
    # A background corpus that shares no word with the queries: with a handful of rows
    # the adaptive quality gate is at its strictest and can drop the record under test.
    for i in range(n):
        out = await M.do_store(AGENT, {"content": f"note {i} on topic {i} with words alpha{i} beta{i}"})
        assert out["result"] == "stored", out


async def _stored(content: str) -> int:
    out = await M.do_store(AGENT, {"content": content})
    assert out["result"] == "stored", out
    return out["id"]


def _item(out: dict, ref: str) -> dict:
    assert not out.get("error"), out
    return next(i for i in out["items"] if i["head_ref"] == ref)


async def _assert_stale_span_refused(memory_id: int, old: str, expand: dict) -> None:
    """The expand serves the quoted text while the record is unchanged, and is refused
    as stale once the record is rewritten."""
    assert "span" in expand, expand
    # The digest of the very text the offsets were measured in: a revision of anything
    # else would refuse the unchanged record below as well.
    assert expand.get("revision") == blocks.text_revision(old), expand
    start, end = expand["span"]
    before = await M.do_get_contents(AGENT, [expand])
    assert not before.get("unresolved"), before
    assert [i["content"] for i in before["items"]] == [old[start:end]], before

    new = "COMPLETELY DIFFERENT TEXT the record never said before " * 40
    updated = await admin_handlers.do_update_memory(memory_id, new, agent_id=AGENT)
    assert updated.get("ok") is not False, updated
    after = await M.do_get_contents(AGENT, [expand])
    assert not after.get("items"), f"rewritten record served under the old offsets: {after}"
    assert after.get("unresolved") == [{"ref": expand["ref"], "reason": M.RANGE_STALE_REVISION}], after


# ==========================================================================
# bug-578 — a raw-span expand carries the revision of its text.
# ==========================================================================

# One sentence with no terminator, shorter than a block and longer than the head cap the
# test sets: it divides into one block, so the head quote is the record's start and the
# expand names the whole record.
START_QUERY = "zzarquon nebulite flimsy"
START_TEXT = "zzarquon nebulite flimsy " + " ".join(f"logline{i} value ok" for i in range(30))
START_CAP = 300

# A short opening sentence, then a best passage longer than the head cap: the quote is
# the severed start of that passage.
SEVERED_QUERY = "quorvex bramblewick"
SEVERED_TEXT = (
    "An unrelated opening line. "
    + "quorvex bramblewick " + " ".join(f"segment{i} carries data" for i in range(70))
    + ". A closing remark."
)


@pytest.mark.usefixtures("blocks_off")
@pytest.mark.parametrize("mode", [None, "lite"])
@pytest.mark.asyncio
async def test_start_quote_expand_is_refused_after_rewrite(fake_embedding_client, monkeypatch, mode):
    monkeypatch.setattr(config, "RECONSTRUCT_QUOTE_CHARS", START_CAP)
    assert START_CAP < len(START_TEXT) <= blocks.MAX_BLOCK_CHARS and "." not in START_TEXT
    memory_id = await _stored(START_TEXT)
    await _filler()
    out = await R.do_reconstruct(AGENT, START_QUERY, count=3, mode=mode, trace=True)
    item = _item(out, f"mem:{memory_id}")
    assert item["quote_basis"] == "start" and item["expand"]["span"] == [0, len(START_TEXT)], item
    await _assert_stale_span_refused(memory_id, START_TEXT, item["expand"])


@pytest.mark.usefixtures("blocks_off")
@pytest.mark.asyncio
async def test_severed_passage_expand_is_refused_after_rewrite(fake_embedding_client):
    memory_id = await _stored(SEVERED_TEXT)
    await _filler()
    out = await R.do_reconstruct(AGENT, SEVERED_QUERY, count=3, trace=True)
    item = _item(out, f"mem:{memory_id}")
    assert item["quote_basis"] == "lexical" and item.get("context_incomplete") is True, item
    assert item["expand"]["span"][0] > 0, item
    await _assert_stale_span_refused(memory_id, SEVERED_TEXT, item["expand"])


@pytest.mark.usefixtures("blocks_off")
@pytest.mark.parametrize("mode", ["lite", "pro"])
@pytest.mark.asyncio
async def test_sequence_quote_expand_is_refused_after_rewrite(fake_embedding_client, mode):
    memory_id = await _stored(SEVERED_TEXT)
    await _filler()
    out = await R.do_reconstruct(AGENT, SEVERED_QUERY, count=3, mode=mode)
    item = _item(out, f"mem:{memory_id}")
    assert item.get("context_incomplete") is True, item
    await _assert_stale_span_refused(memory_id, SEVERED_TEXT, item["expand"])


def test_every_raw_span_quote_helper_attaches_the_revision():
    """The three helpers that build a raw-span expand, called directly: the revision is
    the text's own, on every path, so a fourth path cannot be added without one."""

    class _Claim:
        ref = "mem:1"
        content = START_TEXT

    claim = _Claim()
    cap = 200
    start = R._filled_quote(claim, None, None, set(), cap)
    assert start["quote_basis"] == "start", start
    assert start["expand"] == {"ref": "mem:1", "span": [0, len(START_TEXT)],
                               "revision": blocks.text_revision(START_TEXT)}

    claim.content = SEVERED_TEXT
    severed = R._filled_quote(claim, None, None, R._trigrams(SEVERED_QUERY), cap)
    assert severed.get("context_incomplete") is True, severed
    assert severed["expand"]["revision"] == blocks.text_revision(SEVERED_TEXT), severed

    text, basis, passages = R.record_passages(claim, None, None, R._trigrams(SEVERED_QUERY), None, cap)
    cut = next(j for j, p in enumerate(passages) if p[3] is not None)
    quoted = R.sequence_quote(claim, text, basis, passages, [cut])
    assert quoted["expand"] == {"ref": "mem:1", "span": [passages[cut][0], passages[cut][3]],
                                "revision": blocks.text_revision(text)}


# ==========================================================================
# bug-582 — a mode trims the claims list before it refuses.
# ==========================================================================

BURST_LINE = "The harbor budget plan was discussed at the meeting and the next meeting is on Friday."
BURST_QUERY = "harbor budget plan meeting"


async def _burst(turns: int = 100) -> None:
    # One source, half a second apart: the turns bundle into one item whose claims list
    # is bounded by max_evidence, not by any cap.
    for i in range(turns):
        ms = i * 500
        out = await M.do_store(AGENT, {
            "content": f"turn {i}: " + BURST_LINE,
            "timestamp": f"2026-10-02T09:00:{ms // 1000:02d}.{ms % 1000:03d}000+00:00",
            "source": {"type": "User", "id": "u1", "name": "u"},
        })
        assert out["result"] == "stored", out


def _role_targets(item: dict) -> set[str]:
    return {role["ref"] for claim in item.get("claims", []) for role in claim.get("roles", []) if "ref" in role}


@pytest.mark.asyncio
async def test_a_mode_trims_the_claims_rather_than_refusing(fake_embedding_client):
    await _burst()
    plain = await R.do_reconstruct(AGENT, BURST_QUERY, count=3, top_k=100, max_evidence=100)
    first = plain["items"][0]
    total = len(first["claims"])
    assert total >= 90, (total, plain)
    assert tokens.count_json(first["content"]) < 100, first["content"]

    lite = await R.do_reconstruct(AGENT, BURST_QUERY, mode="lite", top_k=100, max_evidence=100)
    assert "error" not in lite, lite
    assert lite["returned_count"] >= 1 and lite["used_tokens"] <= R.MODE_CAPS["lite"], lite
    item = lite["items"][0]
    assert item["head_ref"] == first["head_ref"] and item["content"], item
    kept = [claim["ref"] for claim in item["claims"]]
    assert first["head_ref"] in kept, item
    # The cut is said, and counts what the item was built from, not only what lite cut.
    assert 1 <= len(kept) < total and item["claims_omitted"] == total - len(kept), item
    # A role never names a claim the cut removed.
    assert _role_targets(item) <= set(kept), item

    # A larger cap never keeps fewer claims.
    pro = await R.do_reconstruct(AGENT, BURST_QUERY, mode="pro", top_k=100, max_evidence=100)
    assert "error" not in pro, pro
    assert len(pro["items"][0]["claims"]) >= len(kept), pro


@pytest.mark.asyncio
async def test_a_mode_keeps_as_many_claims_as_fit(fake_embedding_client, monkeypatch):
    """The trim is the largest claims list that fits, not a fixed floor of one claim."""
    await _burst()
    lite = await R.do_reconstruct(AGENT, BURST_QUERY, mode="lite", top_k=100, max_evidence=100)
    assert "error" not in lite, lite
    kept = len(lite["items"][0]["claims"])
    assert kept > 1, lite
    # One more claim does not fit the cap this response was trimmed to.
    one_more = await R.do_reconstruct(AGENT, BURST_QUERY, mode="lite", top_k=100, max_evidence=kept + 1)
    assert "error" not in one_more, one_more
    assert len(one_more["items"][0]["claims"]) == kept, one_more


@pytest.mark.asyncio
async def test_a_refusal_names_what_did_not_fit(fake_embedding_client, monkeypatch):
    await _burst(20)
    # A per-session notice rides only the first response of a session; take it here, so
    # the responses measured below are the same size.
    await R.do_reconstruct(AGENT, BURST_QUERY, mode="lite", top_k=20, max_evidence=1)
    lite = await R.do_reconstruct(AGENT, BURST_QUERY, mode="lite", top_k=20, max_evidence=1)
    assert "error" not in lite, lite
    passage = tokens.count_json(lite["items"][0]["content"])
    smallest = lite["used_tokens"]
    # Far apart, so a cap between them stays between them whatever its own digits cost.
    assert passage + 40 < smallest, (passage, smallest)

    # A cap the passage fits and the smallest response does not: the passage is not to blame.
    monkeypatch.setitem(R.MODE_CAPS, "lite", (passage + smallest) // 2)
    over = await R.do_reconstruct(AGENT, BURST_QUERY, mode="lite", top_k=20, max_evidence=1)
    assert over.get("error") == R.CAP_BELOW_MINIMUM and over["items"] == [], over
    assert "first passage alone" not in over["detail"], over
    assert f"{passage} tokens" in over["detail"] and "smallest response" in over["detail"], over

    # A cap below the passage itself: then it is the passage.
    monkeypatch.setitem(R.MODE_CAPS, "lite", passage - 1)
    under = await R.do_reconstruct(AGENT, BURST_QUERY, mode="lite", top_k=20, max_evidence=1)
    assert under.get("error") == R.CAP_BELOW_MINIMUM, under
    assert f"the first passage alone is {passage} tokens" in under["detail"], under
