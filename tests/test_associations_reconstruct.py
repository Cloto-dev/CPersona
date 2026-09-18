"""Associative memory read by reconstructive recall (docs/ASSOCIATIVE_MEMORY_DESIGN.md §3, §5).

Stage 1 hands the declared names of the entities a query mentions to the lexical
arm only; stage 2 bundles two candidates a declared record -> record relation
joins; stage 3 walks entity -> entity relations from an item's candidates to the
records that mention what it reached, and adds them as evidence inside the item.
Each invariant of §5 that this step can break has a test here; §5.8 (no
declaration outlives its endpoints) is held by the triggers and tested in
tests/test_associations_schema.py.

Every walk test first asserts that the records it expects the walk to add are NOT
candidates -- otherwise a walk that does nothing would pass, because retrieval
would have put them in the item anyway.
"""

import pytest
import pytest_asyncio

from cpersona import associations, scope_stats, server, session
from cpersona import memory_handlers as M
from cpersona import reconstruct as R
from cpersona.database import get_db

AGENT = "assoc-reconstruct"
OTHER = "assoc-reconstruct-other"
QUERY = "rollback"
BACKGROUND = 40


@pytest.fixture(autouse=True)
def _embeddings(fake_embedding_client):
    return fake_embedding_client


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
    # A background corpus that shares no word with any query below. With a handful
    # of rows the adaptive quality gate is at its strictest and drops the very
    # candidates these tests walk from; forty rows relax it the way a real store does.
    for i in range(BACKGROUND):
        out = await M.do_store(AGENT, {"content": f"ambient telemetry sample {i}", "timestamp": _stamp(200 + i)})
        assert out["result"] == "stored", out
    yield db
    session.reset_pauses_for_tests()


def _stamp(hour: int) -> str:
    return f"2026-03-{1 + hour // 24:02d}T{hour % 24:02d}:00:00+00:00"


async def _mem(content: str, hour: int, agent: str = AGENT, source: str = "u-main", **kw) -> str:
    out = await M.do_store(
        agent,
        {"content": content, "source": {"type": "User", "id": source, "name": source}, "timestamp": _stamp(hour)},
        **kw,
    )
    assert out["result"] == "stored", out
    return f"mem:{out['id']}"


async def _mention(ref: str, *names: str, agent: str = AGENT, **kw) -> None:
    report = await associations.declare(agent, {"entities": [{"name": n} for n in names]}, anchor_ref=ref, **kw)
    assert not report["dropped"], report


async def _relate(subject: str, predicate: str, obj: str, agent: str = AGENT, declared_at: str = "", **kw) -> int:
    report = await associations.declare(
        agent, {"relations": [{"subject": subject, "predicate": predicate, "object": obj}]}, **kw
    )
    assert not report["dropped"] and report["relations"], report
    rel_id = report["relations"][0]
    if declared_at:
        db = await get_db()
        await db.execute("UPDATE relations SET declared_at = ? WHERE id = ?", (declared_at, rel_id))
        await db.commit()
    return rel_id


async def _reconstruct(query: str = QUERY, **kw) -> dict:
    kw.setdefault("top_k", 20)
    kw.setdefault("trace", True)
    return await R.do_reconstruct(AGENT, query, **kw)


def _claims(item: dict) -> dict[str, dict]:
    return {c["ref"]: c for c in item["claims"]}


async def _seed_chain() -> dict[str, str]:
    """A candidate that mentions Billing, and records one, two and three hops away.

        Billing -owned_by-> Payments -part_of-> Finance -reports_to-> Board

    Only the candidate says "rollback"; the reached records share no word with the
    query, so retrieval cannot put them in the pool.
    """
    refs = {
        "direct": await _mem("rollback of the billing deploy", 0),
        "hop1": await _mem("payments team rotates signing keys weekly", 5),
        "hop2": await _mem("finance closes the quarter on the third", 10),
        "hop3": await _mem("board meets monthly on tuesdays", 15),
    }
    await _mention(refs["direct"], "Billing")
    await _mention(refs["hop1"], "Payments")
    await _mention(refs["hop2"], "Finance")
    await _mention(refs["hop3"], "Board")
    await _relate("Billing", "owned_by", "Payments")
    await _relate("Payments", "part_of", "Finance")
    await _relate("Finance", "reports_to", "Board")
    return refs


def _assert_not_candidates(out: dict, *refs: str) -> None:
    pool = set(out["trace"]["candidate_refs"])
    assert pool, "the fixture produced no candidates at all"
    assert not pool & set(refs), f"{sorted(pool & set(refs))} are candidates; the walk would be untested"


# --------------------------------------------------------------------------
# Stage 3 — the bounded walk; invariants 5 (bounded) and 6 (provenance)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_walk_adds_reached_records_as_evidence_with_predicate_and_hops():
    refs = await _seed_chain()
    out = await _reconstruct(count=1, max_hops=2)
    _assert_not_candidates(out, refs["hop1"], refs["hop2"], refs["hop3"])
    item = out["items"][0]
    assert item["head_ref"] == refs["direct"]
    claims = _claims(item)
    assert claims[refs["hop1"]]["why"] == "relation:owned_by" and claims[refs["hop1"]]["hops"] == 1
    assert claims[refs["hop2"]]["why"] == "relation:part_of" and claims[refs["hop2"]]["hops"] == 2
    assert refs["hop3"] not in claims, "the walk followed a relation past max_hops"
    assert "hops" not in claims[refs["direct"]], "a candidate is not something the walk reached"
    # The reached records are quoted like any other retained claim.
    assert {e["ref"] for e in item.get("excerpts", [])} == {refs["hop1"], refs["hop2"]}


@pytest.mark.asyncio
async def test_the_hop_bound_is_reported_when_it_left_a_relation_unfollowed():
    refs = await _seed_chain()
    cut = await _reconstruct(count=1, max_hops=2)
    assert "max_hops" in cut["bounds"].get("omitted", [])
    whole = await _reconstruct(count=1, max_hops=3)
    assert _claims(whole["items"][0])[refs["hop3"]]["hops"] == 3
    assert "max_hops" not in whole["bounds"].get("omitted", [])
    none = await _reconstruct(count=1, max_hops=0)
    assert set(_claims(none["items"][0])) == {refs["direct"]}
    assert "max_hops" in none["bounds"].get("omitted", [])


@pytest.mark.asyncio
async def test_relations_are_followed_in_both_directions():
    """From Payments the walk reaches Billing's record through the same relation."""
    payments = await _mem("rollback of the payments gateway", 0)
    billing = await _mem("billing invoices go out on the first", 5)
    await _mention(payments, "Payments")
    await _mention(billing, "Billing")
    await _relate("Billing", "owned_by", "Payments")
    out = await _reconstruct(count=1, max_hops=1)
    _assert_not_candidates(out, billing)
    claims = _claims(out["items"][0])
    assert billing in claims, "the walk did not follow the relation from its object"
    assert claims[billing]["why"] == "relation:owned_by"


@pytest.mark.asyncio
async def test_sharing_an_entity_is_not_a_relation():
    """A record that mentions the candidate's own entity is not evidence: no relation led there."""
    direct = await _mem("rollback of the billing deploy", 0)
    same = await _mem("billing invoices go out on the first", 5)
    await _mention(direct, "Billing")
    await _mention(same, "Billing")
    out = await _reconstruct(count=1, max_hops=2)
    _assert_not_candidates(out, same)
    assert set(_claims(out["items"][0])) == {direct}


@pytest.mark.asyncio
async def test_the_evidence_cut_follows_hops_then_recency_then_record_id():
    """One written order decides which reached records an item keeps.

    Hop 1 through a relation declared in January (x1), hop 1 through one declared
    in March (y1, y2), hop 2 through the newest relation of all (z1). With room for
    three reached records: the March relation's two, lower id first, then January's.
    The hop-2 record is newest-declared and still last.
    """
    direct = await _mem("rollback of the billing deploy", 0)
    x1 = await _mem("ledger export runs nightly", 1)
    y1 = await _mem("payments team rotates signing keys", 2)
    y2 = await _mem("payments dashboards moved to grafana", 3)
    z1 = await _mem("finance closes the quarter", 4)
    await _mention(direct, "Billing")
    await _mention(x1, "Ledger")
    await _mention(y1, "Payments")
    await _mention(y2, "Payments")
    await _mention(z1, "Finance")
    await _relate("Billing", "feeds", "Ledger", declared_at="2026-01-01T00:00:00+00:00")
    await _relate("Billing", "owned_by", "Payments", declared_at="2026-03-01T00:00:00+00:00")
    await _relate("Payments", "part_of", "Finance", declared_at="2026-05-01T00:00:00+00:00")

    whole = await _reconstruct(count=1, max_hops=2)
    _assert_not_candidates(whole, x1, y1, y2, z1)
    assert set(_claims(whole["items"][0])) == {direct, x1, y1, y2, z1}

    out = await _reconstruct(count=1, max_hops=2, max_evidence=4)
    item = out["items"][0]
    assert set(_claims(item)) == {direct, y1, y2, x1}
    assert item["claims_omitted"] == 1
    assert "max_evidence" in out["bounds"]["omitted"]
    # Excerpts carry the reached rows in the walk's order.
    assert [e["ref"] for e in item["excerpts"]] == [y1, y2, x1]

    tight = await _reconstruct(count=1, max_hops=2, max_evidence=2)
    assert set(_claims(tight["items"][0])) == {direct, y1}


@pytest.mark.asyncio
async def test_a_reached_record_is_not_repeated_in_a_later_item():
    """Two items reach the same record; the earlier item keeps it, the later does not repeat it."""
    first = await _mem("rollback rollback rollback of billing", 0)
    second = await _mem("rollback of the payments gateway", 5)
    shared = await _mem("finance closes the quarter", 10)
    await _mention(first, "Billing")
    await _mention(second, "Payments")
    await _mention(shared, "Finance")
    await _relate("Billing", "reports_to", "Finance")
    await _relate("Payments", "reports_to", "Finance")
    out = await _reconstruct(count=2, max_hops=1)
    _assert_not_candidates(out, shared)
    assert len(out["items"]) == 2
    holders = [item["head_ref"] for item in out["items"] if shared in _claims(item)]
    assert holders == [out["items"][0]["head_ref"]]


# --------------------------------------------------------------------------
# Invariant 3 — association never creates or reorders an item
# --------------------------------------------------------------------------


async def _seed_many_items() -> list[str]:
    refs = [await _mem(f"rollback note {i} for service {i}", 6 * i) for i in range(6)]
    others = [await _mem(f"unrelated fact number {i} about ops", 6 * i + 2) for i in range(6)]
    return refs + others


@pytest.mark.asyncio
async def test_entity_relations_leave_item_heads_and_order_unchanged():
    refs = await _seed_many_items()
    before = await _reconstruct(count=6, max_hops=2)
    heads_before = [item["head_ref"] for item in before["items"]]
    assert len(heads_before) >= 3, "the fixture must produce several items"
    # Every candidate mentions an entity related to another whose records are not candidates.
    for i, ref in enumerate(refs[:6]):
        await _mention(ref, f"Service{i}")
        await _mention(refs[6 + i], f"Owner{i}")
        await _relate(f"Service{i}", "owned_by", f"Owner{i}")
    after = await _reconstruct(count=6, max_hops=2)
    assert [item["head_ref"] for item in after["items"]] == heads_before
    reached = [c for item in after["items"] for c in item["claims"] if "hops" in c]
    assert reached, "the graph added no evidence, so the comparison proved nothing"
    assert after["trace"]["candidate_refs"] == before["trace"]["candidate_refs"]


@pytest.mark.asyncio
async def test_a_record_relation_merges_items_without_reordering_the_rest():
    await _seed_many_items()
    before = await _reconstruct(count=6)
    heads_before = [item["head_ref"] for item in before["items"]]
    assert len(heads_before) >= 3
    # Join the second item's head to the third's with a declared relation.
    await _relate(heads_before[2], "corrects", heads_before[1])
    after = await _reconstruct(count=6)
    heads_after = [item["head_ref"] for item in after["items"]]
    assert heads_before[2] not in heads_after, "the joined candidate is still its own item"
    # The rest keep their order; the freed slot is taken by the next item, at the end.
    assert heads_after[: len(heads_before) - 1] == [h for h in heads_before if h != heads_before[2]]
    merged = after["items"][1]
    assert merged["independence_reason"] == "cluster:relation"
    claims = _claims(merged)
    assert claims[heads_before[1]]["roles"] == [{"ref": heads_before[2], "role": "corrects"}]
    assert claims[heads_before[2]]["why"] == "relation:corrects"


# --------------------------------------------------------------------------
# Stage 2 — bundling and roles
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_predicate_outside_the_vocabulary_bundles_but_is_not_a_role():
    a = await _mem("rollback plan for billing", 0)
    b = await _mem("rollback checklist for billing", 5)
    split = await _reconstruct(count=5)
    assert {item["head_ref"] for item in split["items"]} >= {a, b}
    await _relate(b, "see_also", a)
    out = await _reconstruct(count=5)
    item = next(i for i in out["items"] if a in _claims(i))
    claims = _claims(item)
    assert b in claims and item["independence_reason"] == "cluster:relation"
    assert claims[a]["why"] == claims[b]["why"] == "relation:see_also"
    assert "roles" not in claims[a] and "roles" not in claims[b]


@pytest.mark.asyncio
async def test_a_shared_entity_does_not_bundle():
    a = await _mem("rollback plan for billing", 0)
    b = await _mem("rollback checklist for billing", 5)
    await _mention(a, "Billing")
    await _mention(b, "Billing")
    out = await _reconstruct(count=5, max_hops=2)
    heads = {item["head_ref"] for item in out["items"]}
    assert {a, b} <= heads


@pytest.mark.asyncio
async def test_each_role_word_is_emitted_in_the_vocabulary_direction():
    """The subject of the declared relation is the ref; the object is the claim."""
    pair: list[str] = []
    for role in R.ROLE_VOCABULARY:
        if pair:
            db = await get_db()
            await db.execute("DELETE FROM memories WHERE id IN (?, ?)", [int(r.split(":")[1]) for r in pair])
            await db.commit()
            scope_stats.clear()
        old = await _mem(f"rollback plan {role} one", 0)
        new = await _mem(f"rollback plan {role} two", 5)
        pair = [old, new]
        await _relate(new, role, old)
        out = await _reconstruct(count=5)
        item = next(i for i in out["items"] if old in _claims(i))
        claims = _claims(item)
        assert claims[old].get("roles") == [{"ref": new, "role": role}], (role, claims)
        assert "roles" not in claims[new], role


# --------------------------------------------------------------------------
# Invariant 7 — isolation
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_another_agents_graph_is_never_followed():
    direct = await _mem("rollback of the billing deploy", 0)
    await _mention(direct, "Billing")
    theirs = await _mem("payments team rotates signing keys", 5, agent=OTHER)
    await _mention(theirs, "Payments", agent=OTHER)
    await _mention(await _mem("billing for them", 6, agent=OTHER), "Billing", agent=OTHER)
    await _relate("Billing", "owned_by", "Payments", agent=OTHER)
    out = await _reconstruct(count=1, max_hops=2)
    assert set(_claims(out["items"][0])) == {direct}
    assert "bounds" not in out or "omitted" not in out["bounds"]


@pytest.mark.asyncio
async def test_rows_that_cross_agents_are_not_read_even_when_they_exist():
    """`declare` cannot create a cross-agent row, so the read filters are the second guard.

    Crafted directly: another agent's relation between THIS agent's entities, and
    another agent's entity mentioning THIS agent's candidate. A read that dropped
    its agent filter would follow both.
    """
    direct = await _mem("rollback of the billing deploy", 0)
    reached = await _mem("payments team rotates signing keys", 5)
    theirs = await _mem("ledger export runs nightly", 6)
    await _mention(direct, "Billing")
    await _mention(reached, "Payments")
    await _mention(theirs, "Ledger")
    db = await get_db()
    ids = dict(await db.execute_fetchall("SELECT name, id FROM entities WHERE agent_id = ?", (AGENT,)))
    await db.execute(
        "INSERT INTO relations (agent_id, project_id, channel, subject_kind, subject_id, predicate, "
        "object_kind, object_id, anchor_ref, declared_by, declared_at) "
        "VALUES (?, '', '', 'entity', ?, 'owned_by', 'entity', ?, '', 'agent', '2026-03-01T00:00:00+00:00')",
        (OTHER, ids["Billing"], ids["Payments"]),
    )
    cur = await db.execute(
        "INSERT INTO entities (agent_id, project_id, channel, name, normalized, declared_by, created_at) "
        "VALUES (?, '', '', 'Stray', 'stray', 'agent', '2026-03-01T00:00:00+00:00')",
        (OTHER,),
    )
    await db.execute(
        "INSERT INTO entity_mentions (entity_id, ref, declared_by, created_at) VALUES (?, ?, 'agent', '')",
        (cur.lastrowid, direct),
    )
    await db.execute(
        "INSERT INTO relations (agent_id, project_id, channel, subject_kind, subject_id, predicate, "
        "object_kind, object_id, anchor_ref, declared_by, declared_at) "
        "VALUES (?, '', '', 'entity', ?, 'feeds', 'entity', ?, '', 'agent', '2026-03-01T00:00:00+00:00')",
        (OTHER, cur.lastrowid, ids["Ledger"]),
    )
    await db.commit()
    out = await _reconstruct(count=1, max_hops=2)
    _assert_not_candidates(out, reached, theirs)
    assert set(_claims(out["items"][0])) == {direct}


@pytest.mark.asyncio
async def test_a_relation_in_a_project_is_followed_only_where_the_project_is_read():
    direct = await _mem("rollback of the billing deploy", 0)
    reached = await _mem("payments team rotates signing keys", 5)
    await _mention(direct, "Billing")
    await _mention(reached, "Payments")
    await _relate("Billing", "owned_by", "Payments", project_id="p1")
    global_only = await _reconstruct(count=1, max_hops=1, project_id="")
    assert reached not in _claims(global_only["items"][0])
    in_project = await _reconstruct(count=1, max_hops=1, project_id="p1")
    assert reached in _claims(in_project["items"][0])
    unfiltered = await _reconstruct(count=1, max_hops=1)
    assert reached in _claims(unfiltered["items"][0])


@pytest.mark.asyncio
async def test_a_record_the_call_could_not_read_is_not_reached():
    direct = await _mem("rollback of the billing deploy", 0)
    elsewhere = await _mem("payments team rotates signing keys", 5, project_id="p2")
    other_user = await _mem("payments dashboards moved", 6, source="u-other")
    await _mention(direct, "Billing")
    await _mention(other_user, "Payments")  # registers the global entity first
    await _mention(elsewhere, "Payments", project_id="p2")  # ...so this resolves to it
    await _relate("Billing", "owned_by", "Payments")
    everywhere = await _reconstruct(count=1, max_hops=1)
    assert {elsewhere, other_user} <= set(_claims(everywhere["items"][0])), "the fixture reaches neither"
    scoped = await _reconstruct(count=1, max_hops=1, project_id="p1")
    assert elsewhere not in _claims(scoped["items"][0])
    assert other_user in _claims(scoped["items"][0])
    by_source = await _reconstruct(count=1, max_hops=1, source_id="u-main")
    assert other_user not in _claims(by_source["items"][0])


# --------------------------------------------------------------------------
# Stage 1 — declared names reach the lexical arm only
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_alias_in_the_query_adds_the_lexical_vote_for_the_record_that_used_the_name():
    """The alias gives the record a lexical vote it could not get from the query's words.

    The target shares one short word with the query ("ui", below the trigram
    index), so only the vector arm votes for it and a dozen rows that say "ui" at
    least as well outrank it. The declared alias lets the lexical arm find
    "mizeye", and the target becomes the most relevant item.

    What this does NOT do: bring in a record neither arm would otherwise vote for.
    A row found by one arm alone is below recall's quality gate (the policy that
    keeps weak lexical rows out), and the gate is recall's, unchanged here.
    """
    target = await _mem("mizeye ui shipped", 0)
    for i in range(12):
        await _mem(f"ui note {i}", i + 1)
    plain = await _reconstruct("ミズアイ ui", count=1, top_k=5)
    assert plain["items"][0]["head_ref"] != target, "the target already leads; the test proves nothing"
    await associations.declare(AGENT, {"entities": [{"name": "MizEye", "aliases": ["ミズアイ"]}]})
    out = await _reconstruct("ミズアイ ui", count=1, top_k=5)
    assert out["items"][0]["head_ref"] == target
    assert out["trace"]["cues"] == {"entities": ["MizEye"], "terms": ["MizEye"]}


@pytest.mark.asyncio
async def test_the_vector_arm_sees_the_query_unchanged(monkeypatch):
    await _mem("mizeye shipped the gaze viewer", 0)
    await associations.declare(AGENT, {"entities": [{"name": "MizEye", "aliases": ["ミズアイ", "miz-eye"]}]})
    vector_queries, lexical = [], []
    real_vector, real_keyword, real_fts = M._search_vector, M._search_memories_keyword, M._search_episodes_fts

    async def spy_vector(db, agent_id, query, *a, **kw):
        vector_queries.append(query)
        return await real_vector(db, agent_id, query, *a, **kw)

    async def spy_keyword(db, agent_id, query, *a, **kw):
        lexical.append(("mem", query, kw.get("extra_terms")))
        return await real_keyword(db, agent_id, query, *a, **kw)

    async def spy_fts(db, agent_id, query, *a, **kw):
        lexical.append(("ep", query, kw.get("extra_terms")))
        return await real_fts(db, agent_id, query, *a, **kw)

    monkeypatch.setattr(M, "_search_vector", spy_vector)
    monkeypatch.setattr(M, "_search_memories_keyword", spy_keyword)
    monkeypatch.setattr(M, "_search_episodes_fts", spy_fts)
    await _reconstruct("ミズアイ status", count=1)
    assert vector_queries and set(vector_queries) == {"ミズアイ status"}
    assert {kind for kind, _, _ in lexical} == {"mem", "ep"}
    for _, query, terms in lexical:
        assert query == "ミズアイ status" and terms == ["MizEye", "miz-eye"]


@pytest.mark.asyncio
async def test_without_a_match_retrieval_is_called_exactly_as_before(monkeypatch):
    await _mem("rollback of the billing deploy", 0)
    await associations.declare(AGENT, {"entities": [{"name": "MizEye", "aliases": ["ミズアイ"]}]})
    calls = []
    real = M.do_recall

    async def spy(*a, **kw):
        calls.append(kw)
        return await real(*a, **kw)

    monkeypatch.setattr(M, "do_recall", spy)
    await _reconstruct(count=1)
    assert calls and all("lexical_terms" not in kw for kw in calls)


@pytest.mark.asyncio
async def test_the_longest_declared_term_claims_its_span():
    await associations.declare(AGENT, {"entities": [{"name": "MizEye", "aliases": ["miz eye viewer"]}, {"name": "Eye"}]})
    terms, report = await associations.query_terms(AGENT, "where is the MizEye build")
    assert report["entities"] == ["MizEye"] and terms == ["miz eye viewer"]
    terms, report = await associations.query_terms(AGENT, "mizeye and the eye")
    assert report["entities"] == ["MizEye", "Eye"]


@pytest.mark.asyncio
async def test_cue_terms_follow_the_read_scope():
    await associations.declare(AGENT, {"entities": [{"name": "MizEye", "aliases": ["ミズアイ"]}]}, project_id="p1")
    await associations.declare(OTHER, {"entities": [{"name": "Kirari", "aliases": ["ミズアイ"]}]})
    assert (await associations.query_terms(AGENT, "ミズアイ", project_id=""))[0] == []
    assert (await associations.query_terms(AGENT, "ミズアイ", project_id="p1"))[0] == ["MizEye"]
    assert (await associations.query_terms(AGENT, "ミズアイ"))[0] == ["MizEye"]


# --------------------------------------------------------------------------
# Invariants 1 and 2 — recall is unchanged; a graph that does not apply is a no-op
# --------------------------------------------------------------------------


async def _reset_counters() -> None:
    db = await get_db()
    await db.execute("UPDATE memories SET recall_count = 0, last_recalled_at = NULL")
    await db.commit()


async def _seed_rich() -> list[str]:
    """Versions, a burst, an episode and singletons, so every bundling key fires."""
    refs = [await _mem(f"rollback note {i} for billing", 6 * i) for i in range(4)]
    for i, stamp in enumerate(("2026-03-09T09:00:00+00:00", "2026-03-09T09:00:20+00:00")):
        out = await M.do_store(AGENT, {"content": f"rollback turn {i}", "id": "burst",
                                       "source": {"type": "User", "id": "u-burst", "name": "b"},
                                       "timestamp": stamp})
        refs.append(f"mem:{out['id']}")
    return refs


@pytest.mark.asyncio
async def test_recall_is_unchanged_by_a_populated_graph():
    """The graph is read by reconstruct only.

    The fixture is one where reading it WOULD move recall: the query names
    `invoicing`, an alias of Billing, and the record that says "billing" is voted
    for by the vector arm alone until the alias gives it a lexical vote.
    Reconstruct over the same query shows the graph taking effect there.
    """
    target = await _mem("billing ui export", 0)
    for i in range(12):
        await _mem(f"ui note {i}", i + 1)
    query = "invoicing ui"
    await _reset_counters()
    before = await server.do_recall_boundary(AGENT, query, 10, False, "", [], None, "")
    plain = await _reconstruct(query, count=1, top_k=5)
    await associations.declare(AGENT, {"entities": [{"name": "Billing", "aliases": ["invoicing"]}]}, anchor_ref=target)
    await _relate("Billing", "owned_by", "Payments")
    await _reset_counters()
    after = await server.do_recall_boundary(AGENT, query, 10, False, "", [], None, "")
    assert after == before
    expanded = await _reconstruct(query, count=1, top_k=5)
    assert plain["items"][0]["head_ref"] != target and expanded["items"][0]["head_ref"] == target, (
        "the graph does not move reconstruct on this fixture, so recall's equality proves nothing"
    )


@pytest.mark.asyncio
async def test_a_graph_that_does_not_apply_changes_nothing():
    """An unrelated graph must be byte-identical to an empty one.

    Declared: another agent's full graph over the same names, and entities of this
    agent that neither the query nor any candidate names. A reconstruct that
    consulted the graph unconditionally -- expanded every alias, walked from every
    entity, bundled by any relation -- would differ here.
    """
    await _seed_rich()
    theirs = await _mem("rollback billing for them", 1, agent=OTHER)
    lone = await _mem("kafka lag alarms", 40)
    owners = await _mem("streaming owners list", 41)
    await _reset_counters()
    empty = await _reconstruct("rollback billing", count=5, max_hops=3)
    await _mention(theirs, "Billing", agent=OTHER)
    await associations.declare(OTHER, {"entities": [{"name": "Rollback", "aliases": ["billing"]}]})
    await _relate("Billing", "owned_by", "Payments", agent=OTHER)
    await _mention(lone, "Kafka")
    await _mention(owners, "Streaming")
    await _relate("Kafka", "owned_by", "Streaming")
    await _reset_counters()
    populated = await _reconstruct("rollback billing", count=5, max_hops=3)
    assert populated == empty


def test_fts_terms_are_phrases():
    """A declared term is one phrase: `Miz Eye` must not match a row that only says `Eye`."""
    base = M._build_fts_recall_query("where is it")
    assert M._build_fts_recall_query("where is it", None) == base
    assert M._build_fts_recall_query("where is it", []) == base
    expanded = M._build_fts_recall_query("where is it", ["Miz Eye", "ok", 'say "hi"'])
    assert expanded == base + ' OR "Miz Eye" OR "say ""hi"""'
    assert M._build_fts_recall_query("ab", ["Miz Eye"]) == '"Miz Eye"'


# --------------------------------------------------------------------------
# The walk and its loader, each against its own bound
# --------------------------------------------------------------------------


def _ref(n: int) -> str:
    """A record ref for the pure-walk tests, which need no stored rows."""
    return f"mem:{n}"


def _chain_graph() -> associations.WalkGraph:
    """Entities 1-2-3-4 in a chain, one record per entity; the candidate mentions 1."""
    graph = associations.WalkGraph()
    graph.mentions = {_ref(1): [1]}
    for rel_id, (a, b) in enumerate(((1, 2), (2, 3), (3, 4)), start=1):
        graph.adjacency.setdefault(a, []).append((rel_id, rel_id, b, f"p{rel_id}"))
        graph.adjacency.setdefault(b, []).append((rel_id, rel_id, a, f"p{rel_id}"))
    graph.records = {e: [f"mem:{10 + e}"] for e in (2, 3, 4)}
    return graph


@pytest.mark.parametrize("max_hops, expected, cut", [
    (0, [], True),
    (1, [(_ref(12), "relation:p1", 1)], True),
    (2, [(_ref(12), "relation:p1", 1), (_ref(13), "relation:p2", 2)], True),
    (3, [(_ref(12), "relation:p1", 1), (_ref(13), "relation:p2", 2), (_ref(14), "relation:p3", 3)], False),
])
def test_the_walk_stops_at_its_hop_bound_even_when_the_graph_holds_more(max_hops, expected, cut):
    """The pure walk is bounded by itself, not only by what its loader fetched."""
    rows = [R._Candidate({"ref": _ref(1), "content": "x"}, rank=0)]
    reached, cuts = R.walk([[0]], rows, _chain_graph(), max_hops)
    assert reached == [expected]
    assert (R.BOUND_HOPS in cuts[0]) is cut


@pytest.mark.asyncio
async def test_the_loader_reads_no_further_than_the_hop_bound():
    refs = await _seed_chain()
    graph = await associations.walk_graph(AGENT, [refs["direct"]], [refs["direct"]], max_hops=1, per_entity=40)
    reached = set(graph.rows)
    assert refs["hop1"] in reached and refs["hop2"] not in reached and refs["hop3"] not in reached


def test_the_walk_starts_from_each_items_own_candidates_and_skips_their_own_entities():
    """Item 0 mentions entity 1, item 1 mentions entity 3 (the far end of the chain).

    Each walks from its own mentions, and a record that mentions a start entity
    is not evidence -- sharing an entity is not a relation.
    """
    graph = _chain_graph()
    graph.mentions = {_ref(1): [1], _ref(2): [4]}
    graph.records[1] = [_ref(11)]
    graph.records[4] = [_ref(14)]
    rows = [R._Candidate({"ref": ref, "content": "x"}, rank=i) for i, ref in enumerate((_ref(1), _ref(2)))]
    reached, _ = R.walk([[0], [1]], rows, graph, max_hops=1)
    assert reached == [[(_ref(12), "relation:p1", 1)], [(_ref(13), "relation:p3", 1)]]


@pytest.mark.asyncio
async def test_the_walk_reaches_episodes_where_recall_would_return_them():
    """An episode that mentions a reached entity is evidence like a memory -- quoted,
    with `why` and `hops` -- and, as in recall, only without a source filter or with
    a channel."""
    direct = await _mem("rollback of the billing deploy", 0)
    out = await M.do_archive_episode(
        AGENT, [{"role": "user", "content": "payments sync", "timestamp": "2026-02-01T00:00:00+00:00"}],
        summary="payments team weekly sync", keywords="",
    )
    episode = f"ep:{out['episode_id']}"
    # Archived in February, before every memory here. The episode boundary is the
    # latest episode's archive time, and a boundary of "now" would down-weight the
    # March-dated candidate this walks from until the gate dropped it.
    db = await get_db()
    await db.execute("UPDATE episodes SET created_at = '2026-02-01 00:00:00' WHERE id = ?", (out["episode_id"],))
    await db.commit()
    scope_stats.clear()
    await _mention(direct, "Billing")
    await _mention(episode, "Payments")
    await _relate("Billing", "owned_by", "Payments")
    walked = await _reconstruct(count=1, max_hops=1)
    _assert_not_candidates(walked, episode)
    claim = _claims(walked["items"][0]).get(episode)
    assert claim is not None and claim["why"] == "relation:owned_by" and claim["hops"] == 1
    assert any(e["ref"] == episode and e["content"].startswith("[Episode]") for e in walked["items"][0]["excerpts"])
    by_source = await _reconstruct(count=1, max_hops=1, source_id="u-main")
    assert episode not in _claims(by_source["items"][0])
    with_channel = await _reconstruct(count=1, max_hops=1, source_id="u-main", channel="c")
    assert episode in _claims(with_channel["items"][0])

