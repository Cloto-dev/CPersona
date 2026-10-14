"""Regression tests: declared associations.

  bug-533  declare_associations bounded its declare lists at MAX_ITEMS but not its
           retract lists: every retract item ran its own DELETE inside one write
           transaction, so a single call could hold the process-wide write lock for
           as long as its payload was long. The excess is now reported in `dropped`
           and never deleted, as a declaration's excess is.
  bug-534  the relation walk loaded the records of an entity only when it lay at
           least one relation away from EVERY chosen item's mentions. An entity that
           one item reaches through a relation but another item mentions got no
           records, so raising `count` (which brings the other item into the window)
           silently removed walked evidence from an item already in it.
"""

import pytest
import pytest_asyncio

from cpersona import associations, server, session
from cpersona import memory_handlers as M
from cpersona import reconstruct as R
from cpersona.database import get_db

AGENT = "w03-assoc"
BOUND = associations.MAX_ITEMS


@pytest_asyncio.fixture
async def db():
    session.reset_pauses_for_tests()
    conn = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks", "entities",
                  "entity_aliases", "entity_mentions", "relations", "record_nodes"):
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()
    return conn


def _stamp(h: int) -> str:
    return f"2026-03-{1 + h // 24:02d}T{h % 24:02d}:00:00+00:00"


async def _mem(content: str, h: int) -> str:
    out = await M.do_store(AGENT, {"content": content, "source": {"type": "User", "id": "u", "name": "u"},
                                   "timestamp": _stamp(h)})
    assert out["result"] == "stored", out
    return f"mem:{out['id']}"


# ---------------------------------------------------------------------------------
# bug-533 -- retract is bounded like a declaration
# ---------------------------------------------------------------------------------


async def _relations(n: int) -> list[int]:
    ids: list[int] = []
    for start in range(0, n, BOUND):
        rels = [{"subject": f"S{i}", "predicate": "links", "object": f"O{i}"} for i in range(start, min(n, start + BOUND))]
        report = await associations.declare(AGENT, {"relations": rels})
        assert not report["dropped"], report
        ids.extend(report["relations"])
    assert len(set(ids)) == n
    return ids


@pytest.mark.asyncio
async def test_retract_relations_past_the_bound_are_reported_and_not_deleted(db):
    ids = await _relations(BOUND + 2)
    out = await server.do_declare_associations_boundary(AGENT, retract={"relations": ids})
    assert out["retracted"]["relations"] == BOUND, out["retracted"]
    excess = [d for d in out["dropped"] if d["item"].startswith("retract.relations")]
    assert [d["item"] for d in excess] == [f"retract.relations[{i}]" for i in (BOUND, BOUND + 1)], excess
    assert all(f"{BOUND}-item bound" in d["reason"] for d in excess), excess
    left = [r[0] for r in await db.execute_fetchall(
        "SELECT id FROM relations WHERE agent_id = ? ORDER BY id", (AGENT,))]
    assert left == sorted(ids[BOUND:]), left


@pytest.mark.asyncio
async def test_retract_mentions_past_the_bound_are_reported_and_not_deleted(db):
    anchor = await _mem("anchor for many mentions", 0)
    entity_ids: list[int] = []
    for start in range(0, BOUND + 2, BOUND):
        names = [{"name": f"E{i}"} for i in range(start, min(BOUND + 2, start + BOUND))]
        report = await associations.declare(AGENT, {"entities": names}, anchor_ref=anchor)
        assert not report["dropped"], report
        entity_ids.extend(e["id"] for e in report["entities"])
    pairs = [{"entity": e, "ref": anchor} for e in entity_ids]
    out = await server.do_declare_associations_boundary(AGENT, retract={"mentions": pairs})
    assert out["retracted"]["mentions"] == BOUND, out["retracted"]
    excess = [d["item"] for d in out["dropped"] if d["item"].startswith("retract.mentions")]
    assert excess == [f"retract.mentions[{i}]" for i in (BOUND, BOUND + 1)], out["dropped"]
    left = sorted(r[0] for r in await db.execute_fetchall(
        "SELECT entity_id FROM entity_mentions WHERE ref = ?", (anchor,)))
    assert left == sorted(entity_ids[BOUND:]), left


@pytest.mark.asyncio
async def test_a_long_retract_list_issues_no_delete_past_the_bound(db, monkeypatch):
    """The cost of a retract is bounded by MAX_ITEMS, not by the length of the payload."""
    deletes = 0
    real = associations.transaction

    class _Counting:
        def __init__(self, conn):
            self._conn = conn

        async def execute(self, sql, *args):
            nonlocal deletes
            if sql.lstrip().upper().startswith("DELETE"):
                deletes += 1
            return await self._conn.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    class _Wrap:
        async def __aenter__(self):
            self._cm = real()
            return _Counting(await self._cm.__aenter__())

        async def __aexit__(self, *exc):
            return await self._cm.__aexit__(*exc)

    monkeypatch.setattr(associations, "transaction", lambda: _Wrap())
    n = 5000
    report = await associations.retract(
        AGENT, relations=list(range(1, n + 1)), mentions=[{"entity": i, "ref": "mem:1"} for i in range(1, n + 1)]
    )
    assert deletes == 2 * BOUND, deletes
    assert len(report["dropped"]) == 2 * (n - BOUND), len(report["dropped"])


@pytest.mark.asyncio
async def test_retract_lists_that_are_not_lists_are_reported(db):
    report = await associations.retract(AGENT, relations={"a": 1}, mentions="mem:1")
    items = {d["item"]: d["reason"] for d in report["dropped"]}
    assert items == {"retract.relations": "must be a list", "retract.mentions": "must be a list"}, report


# ---------------------------------------------------------------------------------
# bug-534 -- an item's walked evidence does not depend on which other items are chosen
# ---------------------------------------------------------------------------------


async def _billing_payments():
    """a mentions Billing, b and y mention Payments; Billing owned_by Payments.

    y is the evidence a reaches through the relation; it is not a retrieval
    candidate (no query term), and b mentions the entity it is reached through.
    """
    for i in range(40):
        await M.do_store(AGENT, {"content": f"ambient telemetry sample {i}", "timestamp": _stamp(200 + i)})
    a = await _mem("rollback of the billing deploy", 0)
    b = await _mem("rollback of the payments gateway", 1)
    y = await _mem("payments team rotates signing keys weekly", 5)
    ids = {}
    for ref, name in ((a, "Billing"), (b, "Payments"), (y, "Payments")):
        rep = await associations.declare(AGENT, {"entities": [{"name": name}]}, anchor_ref=ref)
        assert not rep["dropped"], rep
        ids[name] = rep["entities"][0]["id"]
    rep = await associations.declare(
        AGENT, {"relations": [{"subject": "Billing", "predicate": "owned_by", "object": "Payments"}]}
    )
    assert rep["relations"], rep
    return a, b, y, ids


@pytest.mark.asyncio
async def test_the_loader_reads_records_of_an_entity_another_start_mentions(db):
    a, b, y, ids = await _billing_payments()
    graph = await associations.walk_graph(AGENT, [a, b], [a, b], max_hops=1, per_entity=40)
    assert graph.records.get(ids["Payments"]) == [y], graph.records
    assert y in graph.rows


@pytest.mark.asyncio
async def test_the_loader_marks_the_cut_of_an_entity_another_start_mentions(db):
    a, b, y, ids = await _billing_payments()
    extra = await _mem("payments on call handbook", 6)
    rep = await associations.declare(AGENT, {"entities": [{"name": "Payments"}]}, anchor_ref=extra)
    assert not rep["dropped"], rep
    graph = await associations.walk_graph(AGENT, [a, b], [a, b], max_hops=1, per_entity=1)
    assert ids["Payments"] in graph.records_cut, graph.records_cut


@pytest.mark.asyncio
async def test_the_loader_follows_each_start_to_the_hop_bound(db):
    """a mentions Alpha, b mentions Gamma, Alpha - Beta - Gamma: a reaches Gamma at hop 2."""
    a = await _mem("alpha note", 0)
    b = await _mem("gamma note", 1)
    y = await _mem("gamma evidence", 2)
    ids = {}
    for ref, name in ((a, "Alpha"), (b, "Gamma"), (y, "Gamma")):
        rep = await associations.declare(AGENT, {"entities": [{"name": name}]}, anchor_ref=ref)
        ids[name] = rep["entities"][0]["id"]
    rep = await associations.declare(AGENT, {"relations": [
        {"subject": "Alpha", "predicate": "near", "object": "Beta"},
        {"subject": "Beta", "predicate": "near", "object": "Gamma"},
    ]})
    assert len(rep["relations"]) == 2, rep
    two_hops = await associations.walk_graph(AGENT, [a, b], [a, b], max_hops=2, per_entity=40)
    assert two_hops.records.get(ids["Gamma"]) == [y], two_hops.records
    walked, _ = R.walk([[0], [1]], [R._Candidate({"ref": ref, "content": "x"}, rank=i) for i, ref in enumerate((a, b))],
                       two_hops, 2)
    assert walked[0] == [(y, "relation:near", 2)], walked


@pytest.mark.asyncio
async def test_raising_count_keeps_the_walked_evidence_of_an_earlier_item(db, fake_embedding_client):
    a, b, y, _ = await _billing_payments()
    one = await R.do_reconstruct(AGENT, "rollback", count=1, max_hops=1, top_k=20, trace=True)
    two = await R.do_reconstruct(AGENT, "rollback", count=2, max_hops=1, top_k=20, trace=True)
    pool = set(one["trace"]["candidate_refs"])
    assert a in pool and b in pool and y not in pool, pool

    def walked(item):
        return [(c["ref"], c.get("why"), c.get("hops")) for c in item["claims"] if c["ref"] != item["head_ref"]]

    assert one["items"][0]["head_ref"] == a and two["items"][0]["head_ref"] == a
    assert walked(one["items"][0]) == [(y, "relation:owned_by", 1)], one["items"][0]
    assert walked(two["items"][0]) == walked(one["items"][0]), two["items"]
