"""reconstruct's read side: currency, what is read, what is computed, what is reported.

* The node and block sets a quotation reads are judged current by the backend
  identity as it is now, not as some earlier write last learned it: a backend
  redeployed under the same URL with another model of the same width must not leave
  the read paths treating the old model's vectors as comparable. The block arm's
  examined rows are the same question, still open (see the xfail below).
* A head quoted from its own block set never reads node embeddings: under the
  default head cap only the node ends serve it (block currency), never the vectors.
* record_passages does not compute a governing range for a block that already lies
  inside a range taken: that range always overlaps it and is thrown away.
* lite's own budget is the server's default for that mode, not a caller's request.
* A mode call refused for its cap still delivers the per-session notices the
  candidate recall marked as told.
"""

import contextlib

import pytest

from cpersona import blocks, config, generation, memory_handlers, reconstruct, update_check
from cpersona.isolation import isolation_where
from tests.conftest import fake_embed_one
from tests.test_generation_key import FINGERPRINT, LONG, OTHER, Backend, _identity, _TempDB
from tests.test_reconstruct_evidence_sequence import RECORDS, SHORT, sized  # noqa: F401 (fixture)

AGENT = "agent.x09"


def _candidate(kind: str, row_id: int) -> reconstruct._Candidate:
    return reconstruct._Candidate({"ref": f"{kind}:{row_id}", "content": ""}, rank=0)


@pytest.fixture
def building(monkeypatch, fake_embedding_client):
    monkeypatch.setattr(config, "BLOCK_BUILD_ENABLED", True)
    monkeypatch.setattr(config, "BLOCK_RETRIEVAL_ENABLED", True)
    return fake_embedding_client


async def _stored_under(tmp, client, monkeypatch, text: str) -> int:
    """A record whose derived sets were built while the backend was FINGERPRINT."""
    original = Backend(_identity(FINGERPRINT))
    monkeypatch.setattr(client, "capabilities_with_outcome", original.capabilities_with_outcome, raising=False)
    await generation.refresh()
    assert generation.fingerprint() == FINGERPRINT
    row = await memory_handlers.do_store(AGENT, {"content": text})
    await tmp.queue._drain(*_drain_modules())
    return row["id"]


def _drain_modules():
    from cpersona import admin_handlers, nodes

    return admin_handlers, memory_handlers, nodes


def _redeploy(client, monkeypatch) -> Backend:
    """The backend behind the same URL now runs another model; nothing is written after."""
    monkeypatch.setattr(generation, "REFRESH_INTERVAL_SECONDS", 0)
    redeployed = Backend(_identity(OTHER))
    monkeypatch.setattr(client, "capabilities_with_outcome", redeployed.capabilities_with_outcome, raising=False)
    return redeployed


# --- bug-700: the read side judges currency by the backend as it is now ---------------


@pytest.mark.asyncio
async def test_a_node_set_from_before_a_redeploy_is_not_current_to_the_quotation_reader(building, monkeypatch):
    building.token_window = 16
    async with _TempDB() as tmp:
        row_id = await _stored_under(tmp, building, monkeypatch, LONG * 40)
        ref = f"mem:{row_id}"
        before, stale_before = await reconstruct._current_node_sets(AGENT, [_candidate("mem", row_id)])
        assert ref in before and not stale_before, "the fixture must build a current node set"

        redeployed = _redeploy(building, monkeypatch)
        after, stale_after = await reconstruct._current_node_sets(AGENT, [_candidate("mem", row_id)])

    assert ref not in after, "nodes the old model embedded were offered for quotation"
    assert ref in stale_after
    assert redeployed.asked >= 1, "the reader judged currency without asking the backend"


@pytest.mark.asyncio
async def test_a_block_set_from_before_a_redeploy_is_not_current_to_the_quotation_reader(building, monkeypatch):
    async with _TempDB() as tmp:
        row_id = await _stored_under(tmp, building, monkeypatch, LONG)
        ref = f"mem:{row_id}"
        assert ref in await reconstruct._current_block_sets(AGENT, [_candidate("mem", row_id)]), (
            "the fixture must build a current block set"
        )

        _redeploy(building, monkeypatch)
        after = await reconstruct._current_block_sets(AGENT, [_candidate("mem", row_id)])

    assert after == {}, "blocks the old model embedded were offered for quotation"


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "open: blocks.search judges block currency without asking the backend, and asking there "
        "would break test_trace_embedding_model's pinned rule that an untraced recall never asks"
    ),
)
@pytest.mark.asyncio
async def test_the_block_arm_does_not_examine_blocks_from_before_a_redeploy(building, monkeypatch):
    from cpersona import database

    async with _TempDB() as tmp:
        row_id = await _stored_under(tmp, building, monkeypatch, LONG)
        db = await database.get_db()
        query = fake_embed_one("paragraph 3 about budget headcount procurement")
        iso = isolation_where(agent_id=AGENT)
        before = await blocks.search(db, query, iso)
        assert row_id in {hit.parent_id for hit in before}, "the fixture must reach the record by its blocks"

        _redeploy(building, monkeypatch)
        after = await blocks.search(db, query, iso)

    assert after == [], "the block arm ranked the new model's query against the old model's blocks"


# --- bug-701: a head quoted from blocks does not read node embeddings -----------------


class _Spy:
    """The read connection, recording the rows record_nodes handed back."""

    def __init__(self, db):
        self._conn = db
        self.node_rows: list[tuple] = []

    async def execute_fetchall(self, sql, params=()):
        rows = await self._conn.execute_fetchall(sql, params)
        if "FROM record_nodes" in sql:
            self.node_rows.extend(tuple(r) for r in rows)
        return rows

    def __getattr__(self, name):
        return getattr(self._conn, name)


@pytest.mark.asyncio
async def test_a_block_quoted_head_reads_no_node_embeddings(building, monkeypatch):
    building.token_window = 16
    async with _TempDB() as tmp:
        row_id = await _stored_under(tmp, building, monkeypatch, LONG * 40)
        ref = f"mem:{row_id}"
        real = reconstruct.connection
        spies: list[_Spy] = []

        @contextlib.asynccontextmanager
        async def spying():
            async with real() as db:
                spies.append(_Spy(db))
                yield spies[-1]

        monkeypatch.setattr(reconstruct, "connection", spying)
        head = _candidate("mem", row_id)
        node_sets, not_current, block_sets = await reconstruct._current_sets(AGENT, [], [head])
        head_rows = spies[-1].node_rows
        both = await reconstruct._current_sets(AGENT, [head], [head])
        other_rows = spies[-1].node_rows

    assert ref in block_sets, "the head's block set must read as current, judged against its node ends"
    assert node_sets == {}, "a node set was built for a record no claim reads nodes of"
    assert not_current == set(), "a record no claim reads nodes of was judged on its nodes"
    assert head_rows, "the node ends block currency needs were not read"
    read = [value for row in head_rows for value in row if isinstance(value, (bytes, bytearray, memoryview))]
    assert read == [], f"{sum(len(v) for v in read)} bytes of node embeddings read for a block-quoted head"
    # The same record as another claim, whose quote does read nodes, still gets them.
    assert ref in both[0] and ref in both[2]
    assert any(isinstance(v, (bytes, bytearray)) for row in other_rows for v in row)


# --- bug-705: no governing range is computed for a block already inside one ----------


class _Claim:
    ref, kind = "mem:1", "mem"

    def __init__(self, content):
        self.content = content


def _overlaps(start, end, ranges):
    return any(start < e and s < end for s, e in ranges)


@pytest.mark.parametrize(
    "text", ["- item ok\n" * 300, "a list item\nanother, and\n" * 120], ids=["dashes", "clauses"]
)
def test_record_passages_skips_blocks_inside_a_taken_range(monkeypatch, text):
    grams = reconstruct._trigrams("item")
    spans = [(s.start, s.end) for s in blocks.segment(text)]
    real = blocks.context_range
    # The same ranking and rule, every range computed: what the passages must still be.
    rows = [(i, s, e, None) for i, (s, e) in enumerate(spans)]
    expected, taken, needed = [], [], 0
    for row in reconstruct.rank_blocks(text, rows, None, grams, None):
        # Only a block outside every range taken so far can produce a new one.
        needed += not _overlaps(row[1], row[2], taken)
        start, end, _ = real(text, spans, row[0])
        if _overlaps(start, end, taken):
            continue
        taken.append((start, end))
        expected.append((start, min(end, start + 800), None, end if end - start > 800 else None))

    calls: list[tuple[int, int, int]] = []

    def spy(t, s, index, *rest):
        out = real(t, s, index, *rest)
        calls.append((index, out[0], out[1]))
        return out

    monkeypatch.setattr(blocks, "context_range", spy)
    _, basis, passages = reconstruct.record_passages(_Claim(text), None, None, grams, None, 800)

    assert basis == "lexical" and len(spans) > 100
    assert passages == expected, "skipping before the range is computed changed the passages"
    governed: list[tuple[int, int]] = []
    for index, start, end in calls:
        assert not _overlaps(*spans[index], governed), f"block {index} lies inside a taken range"
        if not _overlaps(start, end, governed):
            governed.append((start, end))
    assert len(calls) == needed < len(spans), f"{len(calls)} ranges computed for {len(spans)} blocks"


# --- bug-706 / bug-709: what a lite call reports ---------------------------------------


async def _corpus(tmp):
    for text in RECORDS + SHORT:
        await memory_handlers.do_store(AGENT, {"content": text})
    await tmp.drain()


@pytest.mark.usefixtures("blocks_off", "sized")
@pytest.mark.asyncio
@pytest.mark.parametrize("operator", ["max", "forced"])
async def test_lite_does_not_report_a_budget_the_caller_never_sent(fake_embedding_client, monkeypatch, operator):
    from tests.test_reconstruct_filled_quote import _TempDB as _Filled

    if operator == "max":
        monkeypatch.setattr(config, "RECONSTRUCT_MAX_BUDGET", 2000)
    else:
        monkeypatch.setattr(config, "RECONSTRUCT_FORCED_BUDGET", 1500)
    async with _Filled() as tmp:
        await _corpus(tmp)
        lite = await reconstruct.do_reconstruct(AGENT, "filler2x7 lorem", count=9, deep=True, mode="lite")
        named = await reconstruct.do_reconstruct(AGENT, "filler2x7 lorem", count=9, deep=True, mode="lite", budget=2800)

    assert lite["items"], lite
    policy = lite["budget_policy"]
    assert lite.get("requested_budget") is None, "the response names a budget the caller did not send"
    assert policy["source"] != "caller" and policy["reason"] != "budget_requested", policy
    if operator == "max":
        assert policy == {"source": "server_default", "clamped": True, "reason": "budget_omitted"}
    else:
        assert policy["source"] == "operator_forced"
    # A budget the caller does name is still reported as the caller's.
    assert named["requested_budget"] == 2800
    if operator == "max":
        assert named["budget_policy"] == {"source": "caller", "clamped": True, "reason": "budget_requested"}


@pytest.mark.usefixtures("blocks_off", "sized")
@pytest.mark.asyncio
async def test_lite_spends_the_same_budget_when_it_names_none(fake_embedding_client, monkeypatch):
    """The fix moves who the budget is attributed to, not what it is."""
    from tests.test_reconstruct_filled_quote import _TempDB as _Filled

    async with _Filled() as tmp:
        await _corpus(tmp)
        unnamed = await reconstruct.do_reconstruct(AGENT, "filler2x7 lorem", count=9, deep=True, mode="lite", trace=True)
        named = await reconstruct.do_reconstruct(
            AGENT, "filler2x7 lorem", count=9, deep=True, mode="lite", trace=True, budget=reconstruct.LITE_BUDGET
        )
    assert unnamed["effective_budget"] == named["effective_budget"] == reconstruct.LITE_BUDGET
    assert unnamed["requested_budget"] is None and named["requested_budget"] == reconstruct.LITE_BUDGET
    assert [i["content"] for i in unnamed["items"]] == [i["content"] for i in named["items"]]


@pytest.mark.usefixtures("blocks_off", "sized")
@pytest.mark.asyncio
async def test_a_cap_refusal_delivers_the_notice_the_session_was_marked_told(fake_embedding_client, monkeypatch):
    from tests.test_reconstruct_filled_quote import _TempDB as _Filled

    monkeypatch.setattr(config, "UPDATE_CHECK_ENABLED", True)
    monkeypatch.setattr(update_check, "_verdict", update_check._verdict_dict(
        update_check.STATE_NEWER, "0.0.1", update_check.KIND_NEWER, "0.0.2", None))
    monkeypatch.setattr(update_check, "_told_sessions", {})
    monkeypatch.setattr(update_check, "detect_install", lambda available: {"method": "unknown", "command": None})
    monkeypatch.setattr(update_check, "describe", lambda verdict, install: "Test update available")
    async with _Filled() as tmp:
        await _corpus(tmp)
        monkeypatch.setattr(reconstruct, "MODE_CAPS", {"lite": 40, "pro": 40})
        refused = await reconstruct.do_reconstruct(
            AGENT, "filler2x7 lorem", count=9, deep=True, mode="lite", session_key="x09-cap"
        )
        later = await memory_handlers.do_recall(AGENT, "filler2x7 lorem", 5, session_key="x09-cap")

    assert refused["ok"] is False and refused["error"] == reconstruct.CAP_BELOW_MINIMUM
    assert "update" not in later, "the fixture must show the session was marked told"
    assert refused.get("update", {}).get("available") == "0.0.2", (
        "the refusal dropped the update notice its candidate recall marked as told"
    )
