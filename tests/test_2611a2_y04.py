"""Reconstruct's bundling boundaries, role derivation, relation walk and query cue terms.

Each test pins a written rule that a one-character regression would break while
every other test stayed green:

* bundling: an episode's span includes its own start, two rows exactly the
  adjacency window apart are one burst, and at one instant a memory is listed
  before an episode;
* roles: `supersedes` points one version forward, rows at the same instant do
  not supersede each other, and `supports` needs a memory and an episode in the
  same (project, channel);
* the walk: when two relations reach one entity in the same hop the more
  recently declared one is the reason given, and a reached entity with more
  records than the graph read holds is reported as `max_evidence` reached;
* cue terms: a term two visible entities share names both, spans that only touch
  do not overlap, and the expansion-term bound cuts and counts what it cuts.
"""

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from cpersona import associations, config, session
from cpersona import memory_handlers as M
from cpersona import reconstruct as R
from cpersona.database import get_db

AGENT = "y04-reconstruct-walk"
QUERY = "rollback"
CONTEXT = ("p", "")
_TABLES = ("memories", "episodes", "profiles", "pending_memory_tasks", "entities",
           "entity_aliases", "entity_mentions", "relations", "record_nodes")


@pytest_asyncio.fixture
async def db(fake_embedding_client):
    session.reset_pauses_for_tests()
    conn = await get_db()
    for table in _TABLES:
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()
    yield conn
    session.reset_pauses_for_tests()


def _stamp(hour: int) -> str:
    return f"2026-03-{1 + hour // 24:02d}T{hour % 24:02d}:00:00+00:00"


@pytest_asyncio.fixture
async def corpus(db):
    # A background corpus that shares no word with the query: with only a handful of
    # rows the adaptive quality gate is at its strictest and drops the very candidate
    # the walk starts from.
    for i in range(40):
        out = await M.do_store(AGENT, {"content": f"ambient telemetry sample {i}", "timestamp": _stamp(200 + i)})
        assert out["result"] == "stored", out
    return db


# --------------------------------------------------------------------------
# Unit-level fixtures: candidates as the four stages read them
# --------------------------------------------------------------------------

T0 = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _mem(n: int, at: datetime, *, rank: int = 0, msg_id: str = "", context=CONTEXT,
         source: str = "u-1") -> R._Candidate:
    row = R._Candidate({"ref": f"mem:{n}", "id": msg_id, "content": f"row {n}", "timestamp": _iso(at),
                        "source": {"type": "User", "id": source}}, rank=rank)
    row.context = context
    return row


def _ep(n: int, at: datetime, *, rank: int = 0, context=CONTEXT) -> R._Candidate:
    row = R._Candidate({"ref": f"ep:{n}", "content": f"episode {n}", "timestamp": _iso(at)}, rank=rank)
    row.context = context
    return row


def _one_item(rows, spans=None, links=()) -> dict:
    spans = spans or {}
    uf = R.bundle(rows, spans, links)
    assert len({uf.find(i) for i in range(len(rows))}) == 1, "the fixture must form one cluster"
    why = {rows[i].ref: key for i, key in uf.why.items()}
    item, dropped = R.structure(rows, why, spans, 40, links=links)
    assert dropped == 0
    return item


def _roles(item: dict) -> dict[str, list]:
    return {c["ref"]: c.get("roles", []) for c in item["claims"]}


# --------------------------------------------------------------------------
# Bundling boundaries
# --------------------------------------------------------------------------


def test_a_memory_stamped_at_an_episodes_start_belongs_to_it():
    """The span is inclusive: a memory written at the first message's instant is
    inside the conversation the episode covers, and supports it."""
    episode = _ep(1, T0, rank=0)
    memory = _mem(2, T0, rank=1, source="")  # no source: only the episode key can join them
    spans = {episode.row_id: (T0, T0 + timedelta(hours=1))}
    uf = R.bundle([episode, memory], spans)
    assert uf.find(0) == uf.find(1), "a memory at the episode's start was left outside it"
    assert uf.why[1] == "cluster:episode"
    item = _one_item([episode, memory], spans)
    assert _roles(item)[memory.ref] == [{"ref": episode.ref, "role": "supports"}]


def test_two_rows_exactly_the_adjacency_window_apart_are_one_burst(monkeypatch):
    """`gap <= window`: the window's own length is still one conversational moment;
    one second more is not."""
    monkeypatch.setattr(config, "RECONSTRUCT_ADJACENCY_SECONDS", 60)
    together = [_mem(1, T0), _mem(2, T0 + timedelta(seconds=60), rank=1)]
    uf = R.bundle(together, {})
    assert uf.find(0) == uf.find(1), "rows exactly one window apart were split"
    assert uf.why[1] == "cluster:adjacent"
    apart = [_mem(1, T0), _mem(2, T0 + timedelta(seconds=61), rank=1)]
    uf = R.bundle(apart, {})
    assert uf.find(0) != uf.find(1)


def test_at_one_instant_a_memory_is_listed_before_an_episode():
    """The claims' total order puts a statement before the context that covers it
    when time cannot separate them. The episode is the more relevant row here, so
    relevance alone would list it first."""
    episode = _ep(1, T0, rank=0)
    memory = _mem(2, T0, rank=1, source="")
    # The span starts earlier than both rows, so this order does not hang on the
    # inclusive-start rule tested above.
    spans = {episode.row_id: (T0 - timedelta(minutes=30), T0 + timedelta(hours=1))}
    item = _one_item([episode, memory], spans)
    assert [c["ref"] for c in item["claims"]] == [memory.ref, episode.ref]


# --------------------------------------------------------------------------
# Role derivation
# --------------------------------------------------------------------------


def test_a_three_version_record_reads_as_two_one_hop_edges():
    """Each version points at its immediate successor, never past it to the newest."""
    v1 = _mem(1, T0, msg_id="ticket-3", rank=2, source="")
    v2 = _mem(2, T0 + timedelta(days=1), msg_id="ticket-3", rank=1, source="")
    v3 = _mem(3, T0 + timedelta(days=2), msg_id="ticket-3", rank=0, source="")
    item = _one_item([v1, v2, v3])
    roles = _roles(item)
    assert roles[v1.ref] == [{"ref": v2.ref, "role": "supersedes"}]
    assert roles[v2.ref] == [{"ref": v3.ref, "role": "supersedes"}]
    assert roles[v3.ref] == []


def test_rows_at_the_same_instant_do_not_supersede_each_other():
    """Version order cannot separate them: they are a conflict, and neither is
    the other's successor."""
    a = _mem(1, T0, msg_id="ticket-9", rank=0, source="")
    b = _mem(2, T0, msg_id="ticket-9", rank=1, source="")
    item = _one_item([a, b])
    assert item["conflicts"] == [
        {"refs": [a.ref, b.ref], "reason": "same_msg_id_same_timestamp", "msg_id": "ticket-9"}
    ]
    assert all("roles" not in c for c in item["claims"]), item["claims"]


def test_a_memory_and_an_episode_in_different_contexts_have_no_supports_role():
    """A declared record relation can put a memory and an episode of different
    channels in one item; the memory's time falling inside the episode's span
    does not make the episode its support across contexts."""
    episode = _ep(1, T0, rank=0, context=("p", "two"))
    memory = _mem(2, T0 + timedelta(minutes=10), rank=1, context=("p", "one"), source="")
    spans = {episode.row_id: (T0, T0 + timedelta(hours=1))}
    links = [(memory.ref, "mentions", episode.ref)]  # not a role word: it adds no role itself
    item = _one_item([episode, memory], spans, links)
    assert item["independence_reason"] == "cluster:relation"
    assert _roles(item) == {episode.ref: [], memory.ref: []}
    # The same pair in one context is the support the guard withholds above.
    episode.context = memory.context = ("p", "one")
    item = _one_item([episode, memory], spans, links)
    assert _roles(item)[memory.ref] == [{"ref": episode.ref, "role": "supports"}]


# --------------------------------------------------------------------------
# The relation walk, through reconstruct
# --------------------------------------------------------------------------


async def _store(content: str, hour: int) -> str:
    out = await M.do_store(
        AGENT, {"content": content, "source": {"type": "User", "id": "u-main", "name": "u"}, "timestamp": _stamp(hour)}
    )
    assert out["result"] == "stored", out
    return f"mem:{out['id']}"


async def _mention(ref: str, *names: str) -> None:
    report = await associations.declare(AGENT, {"entities": [{"name": n} for n in names]}, anchor_ref=ref)
    assert not report["dropped"], report


async def _relate(subject: str, predicate: str, obj: str, declared_at: str) -> None:
    report = await associations.declare(
        AGENT, {"relations": [{"subject": subject, "predicate": predicate, "object": obj}]}
    )
    assert not report["dropped"] and report["relations"], report
    conn = await get_db()
    await conn.execute("UPDATE relations SET declared_at = ? WHERE id = ?", (declared_at, report["relations"][0]))
    await conn.commit()


async def _reconstruct(**kw) -> dict:
    return await R.do_reconstruct(AGENT, QUERY, top_k=100, trace=True, **kw)


def _assert_not_candidates(out: dict, *refs: str) -> None:
    pool = set(out["trace"]["candidate_refs"])
    assert pool, "the fixture produced no candidates at all"
    assert not pool & set(refs), f"{sorted(pool & set(refs))} are candidates; the walk would be untested"


@pytest.mark.asyncio
@pytest.mark.parametrize("newer", ["audits", "feeds"])
async def test_the_newer_of_two_converging_relations_is_the_reason(corpus, newer):
    """Billing -feeds-> Ledger and Payments -audits-> Ledger both reach Ledger at hop 1.

    The candidate mentions Billing and Payments, so both edges are walked in the
    same step; the more recently declared one is why Ledger's record is evidence.
    Both declaration orders are run, so neither "first seen wins" nor "last seen
    wins" passes for the recency rule.
    """
    direct = await _store("rollback of the billing deploy", 0)
    ledger = await _store("ledger export runs nightly", 5)
    await _mention(direct, "Billing", "Payments")
    await _mention(ledger, "Ledger")
    older = "2026-01-01T00:00:00+00:00"
    newest = "2026-05-01T00:00:00+00:00"
    await _relate("Billing", "feeds", "Ledger", newest if newer == "feeds" else older)
    await _relate("Payments", "audits", "Ledger", newest if newer == "audits" else older)
    out = await _reconstruct(count=1, max_hops=1)
    _assert_not_candidates(out, ledger)
    claims = {c["ref"]: c for c in out["items"][0]["claims"]}
    assert claims[direct].get("hops") is None
    assert claims[ledger]["why"] == f"relation:{newer}"
    assert claims[ledger]["hops"] == 1


async def _ledger_records(n: int) -> tuple[str, list[str]]:
    direct = await _store("rollback of the billing deploy", 0)
    await _mention(direct, "Billing")
    reached = [await _store(f"ledger entry number {i} reconciled", 5 + i) for i in range(n)]
    for ref in reached:
        await _mention(ref, "Ledger")
    await _relate("Billing", "feeds", "Ledger", "2026-02-01T00:00:00+00:00")
    return direct, reached


@pytest.mark.asyncio
async def test_a_reached_entity_with_more_records_than_the_read_holds_reports_the_evidence_bound(corpus):
    """Ledger has three records and the read holds two: what lay beyond is not
    known, so the response says the evidence bound was reached."""
    _, reached = await _ledger_records(3)
    out = await _reconstruct(count=1, max_hops=1, max_evidence=2)
    _assert_not_candidates(out, *reached)
    assert "max_evidence" in out["bounds"].get("reached", []), out["bounds"]


@pytest.mark.asyncio
async def test_a_reached_entity_whose_records_all_fit_does_not_report_the_evidence_bound(corpus):
    """Exactly as many records as the read holds: the walk saw all of them."""
    _, reached = await _ledger_records(2)
    out = await _reconstruct(count=1, max_hops=1, max_evidence=2)
    _assert_not_candidates(out, *reached)
    assert "max_evidence" not in out["bounds"].get("reached", []), out["bounds"]


# --------------------------------------------------------------------------
# Stage 1 cue terms
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_name_two_visible_entities_share_names_both(db):
    """The global entity and the project's own entity of one name are both named:
    the second may reuse the span the first claimed for the same term."""
    report = await associations.declare(AGENT, {"entities": [{"name": "Atlas", "aliases": ["atlas-db"]}]})
    assert not report["dropped"], report
    report = await associations.declare(
        AGENT, {"entities": [{"name": "Atlas", "aliases": ["atlas-svc"]}]}, project_id="p1"
    )
    assert not report["dropped"], report
    terms, found = await associations.query_terms(AGENT, "atlas rollback", project_id="p1")
    assert found["entities"] == ["Atlas", "Atlas"]
    assert sorted(terms) == ["atlas-db", "atlas-svc"]


@pytest.mark.asyncio
async def test_spans_that_only_touch_do_not_overlap(db):
    """`foobar` names both Foo and Bar: the spans meet at one boundary and share no
    character, so the second term is not inside the first one's span."""
    report = await associations.declare(AGENT, {"entities": [
        {"name": "Foo", "aliases": ["foo service"]},
        {"name": "Bar", "aliases": ["bar service"]},
    ]})
    assert not report["dropped"], report
    terms, found = await associations.query_terms(AGENT, "foobar")
    assert sorted(found["entities"]) == ["Bar", "Foo"]
    assert sorted(terms) == ["bar service", "foo service"]


async def _two_entities_with_aliases(each: int) -> list[str]:
    entities = [
        {"name": name, "aliases": [f"{name.lower()} alias {i:02d}" for i in range(each)]}
        for name in ("Alpha", "Beta")
    ]
    report = await associations.declare(AGENT, {"entities": entities})
    assert not report["dropped"], report
    # Longest matched term first (alpha before beta), each entity's aliases in order.
    return [a for e in entities for a in e["aliases"]]


@pytest.mark.asyncio
async def test_the_expansion_term_bound_cuts_and_counts_the_rest(db):
    expected = await _two_entities_with_aliases(20)
    assert len(expected) == 40 > associations.MAX_EXPANSION_TERMS == 32
    terms, found = await associations.query_terms(AGENT, "alpha and beta")
    assert found["entities"] == ["Alpha", "Beta"]
    assert terms == expected[: associations.MAX_EXPANSION_TERMS]
    assert found["terms"] == terms
    assert found["terms_omitted"] == len(expected) - associations.MAX_EXPANSION_TERMS


@pytest.mark.asyncio
async def test_exactly_the_expansion_term_bound_is_not_a_cut(db):
    expected = await _two_entities_with_aliases(16)
    assert len(expected) == associations.MAX_EXPANSION_TERMS
    terms, found = await associations.query_terms(AGENT, "alpha and beta")
    assert terms == expected
    assert "terms_omitted" not in found
