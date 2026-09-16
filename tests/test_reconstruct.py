"""Reconstructive Recall — the eight invariants (docs/RELIABLE_RECALL_2_6.md section 7).

Each invariant the design names has a test here, and the one the design names as
*the* verification has its own fixture discipline: ``top_k`` is fixed and larger
than every count under test, so a mutation that re-couples the two
(``top_k = count`` in ``do_reconstruct``) produces three different pools and
``test_count_alone_does_not_move_the_pool`` turns red. A pool no larger than the
biggest count would be equal under the mutation too — the assertion that the pool
can distinguish the counts is what stops this file from passing vacuously.

The candidate pool is read at the same seam ``tests/test_recall_depth.py`` reads
(``_apply_quality_gate``, the first consumer of the fused list), so the test
answers "what did the retrieval consider", not "what did reconstruct keep".
"""

import os
import tempfile

os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "test_reconstruct.db"))
os.environ.setdefault("CPERSONA_EMBEDDING_MODE", "none")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from cpersona import config  # noqa: E402
from cpersona import memory_handlers as M  # noqa: E402
from cpersona import reconstruct as R  # noqa: E402
from cpersona.database import connection, get_db  # noqa: E402

AGENT = "agent.reconstruct"
QUERY = "rollback"
SEEDED = 30
TOP_K = 25
COUNTS = (1, 3, 5)
# One hour apart, so the adjacency key (60 s) cannot fire and every seeded row is
# its own cluster. Clustering gets its own seeds, per key, below.
STEP_SECONDS = 3600
BASE_HOUR = 3


@pytest.fixture(autouse=True)
def _embeddings(fake_embedding_client):
    """Every test in this file runs on the real store -> embed -> fuse path, offline.

    Without it the recall arms produce rows with no cosine, the adaptive quality
    gate drops all of them, and every assertion here would read zero items —
    passing the ones phrased as upper bounds while measuring nothing. The
    conftest double is deterministic, so invariant 3 still means what it says.
    """
    return fake_embedding_client


@pytest_asyncio.fixture(autouse=True)
async def _fresh_db():
    db = await get_db()
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM episodes")
    await db.commit()
    yield


def _stamp(i: int) -> str:
    """A fixed past instant, ``i`` hours in. Past, so the future-stamp guard is out of play."""
    hour = BASE_HOUR + (i * STEP_SECONDS) // 3600
    day = 1 + hour // 24
    return f"2026-01-{day:02d}T{hour % 24:02d}:00:00+00:00"


async def _seed_unclustered(n: int = SEEDED) -> None:
    """``n`` rows that all answer ``QUERY`` and that no bundling key can join.

    Same source, but an hour apart: the adjacency key needs both. No message id
    and no episode, so msg_id and episode containment cannot fire either. The
    result is ``n`` singleton clusters — the fixture the count tests need, because
    a count can only bite when there are more items available than it allows.
    """
    for i in range(n):
        out = await M.do_store(
            AGENT,
            {
                "content": f"note {i}: rollback of the billing deploy, ticket {1000 + i}",
                "source": {"type": "User", "id": "u-main", "name": "main"},
                "timestamp": _stamp(i),
            },
        )
        assert out["result"] == "stored", out


async def _store_scoped_record(msg_id: str, content: str, stamp: str, project_id: str) -> None:
    """Store a distinct scoped record; cross-project ID equality is not versioning."""
    out = await M.do_store(
        AGENT,
        {
            "content": content,
            "id": msg_id,
            "source": {"type": "User", "id": "u-main", "name": "main"},
            "timestamp": stamp,
        },
        project_id=project_id,
    )
    assert out["result"] == "stored", out


async def _seed_burst() -> None:
    """Three rows from one source, 20 s apart: the adjacency key's own fixture.

    Short and strongly on-query, so they rank above the background corpus and are
    still in the candidate pool when the assertion runs.
    """
    for i, stamp in enumerate(("2026-02-01T09:00:00+00:00", "2026-02-01T09:00:20+00:00", "2026-02-01T09:00:40+00:00")):
        out = await M.do_store(
            AGENT,
            {
                "content": f"rollback turn {i}",
                "source": {"type": "User", "id": "u-burst", "name": "burst"},
                "timestamp": stamp,
            },
        )
        assert out["result"] == "stored", out


def _structured_versions(msg_id: str, contents: tuple[str, str], stamps: tuple[str, str]) -> tuple[dict, str, str]:
    """Exercise version rules on same-namespace candidates from a legacy snapshot.

    Normal stores enforce unique (agent, project, msg_id); they cannot append a
    version. The previous fixture used different projects to bypass that rule,
    incorrectly treating unrelated storage identities as a version history.
    Keep the version/order/conflict assertions at the stage that supports them,
    while real-store namespace separation is covered in test_reconstruct_review.
    """
    rows = [R._Candidate({"ref": f"mem:{i + 1}", "id": msg_id, "content": content,
                          "timestamp": stamp}, rank=i)
            for i, (content, stamp) in enumerate(zip(contents, stamps))]
    for row in rows:
        row.context = ("same-project", "")
    union = R.bundle(rows, {})
    assert union.find(0) == union.find(1), "same-namespace versions must bundle"
    why = {row.ref: union.why[i] for i, row in enumerate(rows)}
    item, truncated = R.structure(rows, why, {}, 40)
    assert truncated is False
    return {"items": [item]}, rows[0].ref, rows[1].ref


def _only_item_with(result: dict, reason: str) -> dict:
    """The one item formed by ``reason``. Exactly one, or the fixture is not what it claims."""
    items = [i for i in result["items"] if i["independence_reason"] == reason]
    assert len(items) == 1, [i["independence_reason"] for i in result["items"]]
    return items[0]


def _spy_pool(monkeypatch) -> list[set]:
    """Capture the candidate ids the fusion produced, before the quality gate."""
    seen: list[set] = []
    real = M._apply_quality_gate

    def spy(results, *args, **kwargs):
        seen.append({r["id"] for r in results if isinstance(r.get("id"), int) and r["id"] > 0})
        return real(results, *args, **kwargs)

    monkeypatch.setattr(M, "_apply_quality_gate", spy)
    return seen


# --------------------------------------------------------------------------
# Invariant 7 — count and breadth are decoupled. The named verification.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_count_alone_does_not_move_the_pool(monkeypatch):
    """Same query, same bounds, three counts, one candidate id set."""
    await _seed_unclustered()
    pools = _spy_pool(monkeypatch)
    returned = []
    for count in COUNTS:
        out = await R.do_reconstruct(AGENT, QUERY, count=count, top_k=TOP_K)
        returned.append(out["returned_count"])
    assert len(pools) == len(COUNTS)
    # The fixture must be able to show a difference: a pool no larger than the
    # biggest count would be equal under the re-coupled mutation too.
    assert len(pools[0]) > max(COUNTS), f"pool {len(pools[0])} cannot distinguish the counts"
    assert pools[0] == pools[1] == pools[2], [len(p) for p in pools]
    # The count still bounds what comes back; it just no longer bounds what was seen.
    assert returned == list(COUNTS), returned


@pytest.mark.asyncio
async def test_top_k_is_what_moves_the_pool(monkeypatch):
    """The other direction: breadth is a real knob, so the test above is not vacuous."""
    await _seed_unclustered()
    pools = _spy_pool(monkeypatch)
    for depth in (5, 20):
        await R.do_reconstruct(AGENT, QUERY, count=1, top_k=depth)
    assert len(pools[0]) < len(pools[1]), [len(p) for p in pools]


# --------------------------------------------------------------------------
# The Reconstruction Window
# --------------------------------------------------------------------------


def test_count_window_arithmetic(monkeypatch):
    """base = forced ?? requested ?? default; effective = min(base, max)."""
    monkeypatch.setattr(config, "RECONSTRUCT_DEFAULT_COUNT", 1)
    monkeypatch.setattr(config, "RECONSTRUCT_MAX_COUNT", 10)
    monkeypatch.setattr(config, "RECONSTRUCT_FORCED_COUNT", None)

    effective, policy = R.resolve_count(None)
    assert (effective, policy["source"], policy["clamped"]) == (1, "server_default", False)

    effective, policy = R.resolve_count(4)
    assert (effective, policy["source"], policy["clamped"]) == (4, "caller", False)

    # A clamp is reported, never silent.
    effective, policy = R.resolve_count(99)
    assert (effective, policy["source"], policy["clamped"]) == (10, "caller", True)

    monkeypatch.setattr(config, "RECONSTRUCT_FORCED_COUNT", 2)
    effective, policy = R.resolve_count(7)
    assert (effective, policy["source"], policy["clamped"]) == (2, "operator_forced", False)


def test_a_count_configuration_that_cannot_hold_is_a_startup_error(monkeypatch):
    """Section 7: a default or forced value above the maximum refuses to start."""
    monkeypatch.setattr(config, "RECONSTRUCT_MAX_COUNT", 5)
    monkeypatch.setattr(config, "RECONSTRUCT_FORCED_COUNT", None)
    monkeypatch.setattr(config, "RECONSTRUCT_DEFAULT_COUNT", 6)
    with pytest.raises(ValueError, match="DEFAULT_COUNT"):
        config.validate_reconstruct_counts()

    monkeypatch.setattr(config, "RECONSTRUCT_DEFAULT_COUNT", 1)
    monkeypatch.setattr(config, "RECONSTRUCT_FORCED_COUNT", 6)
    with pytest.raises(ValueError, match="FORCED_COUNT"):
        config.validate_reconstruct_counts()

    monkeypatch.setattr(config, "RECONSTRUCT_FORCED_COUNT", 5)
    config.validate_reconstruct_counts()  # the boundary itself is a valid configuration


@pytest.mark.asyncio
async def test_a_short_return_is_normal_and_carries_a_reason(monkeypatch):
    await _seed_unclustered()
    # A window wider than the corpus has clusters to fill it with. The maximum is
    # lifted for this test because the shipped one (10) is below the cluster count,
    # so the window would never be the binding constraint.
    monkeypatch.setattr(config, "RECONSTRUCT_MAX_COUNT", 100)
    out = await R.do_reconstruct(AGENT, QUERY, count=50, top_k=TOP_K)
    assert 0 < out["returned_count"] < out["effective_count"] == 50, out["returned_count"]
    assert out["shortfall_reason"] == R.SHORTFALL_EXHAUSTED_CANDIDATES

    out = await R.do_reconstruct(AGENT, "nothing here matches this phrase at all", count=5, top_k=TOP_K)
    assert out["returned_count"] == 0
    assert out["shortfall_reason"] in (
        R.SHORTFALL_NO_RELEVANT_EVIDENCE,
        R.SHORTFALL_BELOW_QUALITY_THRESHOLD,
    )
    # A full window says nothing about a shortfall.
    out = await R.do_reconstruct(AGENT, QUERY, count=3, top_k=TOP_K)
    assert out["returned_count"] == 3 and "shortfall_reason" not in out


# --------------------------------------------------------------------------
# Invariants 1-6, 8
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_1_stored_rows_are_not_modified():
    """A read path. Content, timestamps and message ids are exactly as stored."""
    await _seed_unclustered()
    async with connection() as db:
        cursor = await db.execute("SELECT id, content, timestamp, msg_id FROM memories ORDER BY id")
        before = await cursor.fetchall()
    await R.do_reconstruct(AGENT, QUERY, count=5, top_k=TOP_K)
    async with connection() as db:
        cursor = await db.execute("SELECT id, content, timestamp, msg_id FROM memories ORDER BY id")
        after = await cursor.fetchall()
    assert before == after


@pytest.mark.asyncio
async def test_2_content_is_a_quotation():
    """No model is called, so every item's content is a prefix of a stored row."""
    await _seed_unclustered()
    async with connection() as db:
        cursor = await db.execute("SELECT content FROM memories")
        stored = {row[0] for row in await cursor.fetchall()}
    out = await R.do_reconstruct(AGENT, QUERY, count=5, top_k=TOP_K)
    assert out["items"]
    for item in out["items"]:
        assert any(s.startswith(item["content"]) for s in stored), item["content"]


@pytest.mark.asyncio
async def test_3_determinism():
    """Same database state, same query, same bounds, same output."""
    await _seed_unclustered()
    first = await R.do_reconstruct(AGENT, QUERY, count=5, top_k=TOP_K)
    second = await R.do_reconstruct(AGENT, QUERY, count=5, top_k=TOP_K)
    assert first == second


@pytest.mark.asyncio
async def test_4_bounds_are_declared_and_a_cut_is_reported():
    await _seed_unclustered()
    # A depth the retrieval fills is a possible cut, and the tool cannot tell a
    # full pool from a cut one — so it says so rather than implying it saw
    # everything. 10 is below what this corpus returns; the depth is the binding
    # constraint.
    out = await R.do_reconstruct(AGENT, QUERY, count=1, top_k=10, max_hops=2, max_evidence=40)
    assert out["bounds"] == {"top_k": 10, "max_hops": 2, "max_evidence": 40, "truncated": True}

    # A depth the corpus cannot fill is not a cut by the depth. (What recall's own
    # quality gate drops is recall's to report, and it is why this returns fewer
    # rows than the depth allows rather than exactly SEEDED.)
    out = await R.do_reconstruct(AGENT, QUERY, count=1, top_k=SEEDED + 50, max_evidence=40)
    assert out["bounds"]["truncated"] is False


@pytest.mark.asyncio
async def test_4b_evidence_cut_sets_truncated():
    """A cluster with more evidence than the bound reports the cut."""
    await _seed_unclustered()  # a corpus, so the adaptive quality gate is not the subject
    await _seed_burst()
    out = await R.do_reconstruct(AGENT, QUERY, count=1, top_k=TOP_K, max_evidence=2)
    assert out["items"][0]["independence_reason"] == "cluster:adjacent"
    assert len(out["items"][0]["evidence"]) == 2
    assert out["bounds"]["truncated"] is True


@pytest.mark.asyncio
async def test_5_every_element_says_why():
    await _seed_unclustered()
    out = await R.do_reconstruct(AGENT, QUERY, count=5, top_k=TOP_K)
    assert set(out["count_policy"]) == {"source", "clamped", "reason"}
    for item in out["items"]:
        assert item["independence_reason"]
        assert item["evidence"] and all(e["why"] for e in item["evidence"])
        assert all(set(e) == {"ref", "why"} for e in item["evidence"])


@pytest.mark.asyncio
async def test_6_the_recall_contract_is_untouched():
    """reconstruct adds no key to a recall response and changes none of its values."""
    await _seed_unclustered()
    before = await M.do_recall(AGENT, QUERY, limit=10)
    await R.do_reconstruct(AGENT, QUERY, count=5, top_k=TOP_K)
    after = await M.do_recall(AGENT, QUERY, limit=10)
    assert set(before) == set(after) == {"messages"}
    assert [m["ref"] for m in before["messages"]] == [m["ref"] for m in after["messages"]]


@pytest.mark.asyncio
async def test_8_no_padding():
    """One cluster is one item; a row never appears as the head of two items."""
    await _seed_unclustered()
    out = await R.do_reconstruct(AGENT, QUERY, count=5, top_k=TOP_K)
    heads = [item["head_ref"] for item in out["items"]]
    assert len(heads) == len(set(heads))
    all_refs = [e["ref"] for item in out["items"] for e in item["evidence"]]
    assert len(all_refs) == len(set(all_refs)), "a row was counted into two items"


# --------------------------------------------------------------------------
# Stage 2 keys and stage 4 roles
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_msg_id_bundles_and_derives_supersedes():
    """Two versions of one record: one item, newest at the head, chained by role."""
    out, old, new = _structured_versions(
        "ticket-42", ("rollback pending", "rollback shipped"),
        ("2026-03-01T10:00:00+00:00", "2026-03-02T10:00:00+00:00"),
    )
    item = _only_item_with(out, "cluster:msg_id")
    # Newest first — the head is the current statement.
    assert [c["ref"] for c in item["claims"]] == [new, old]
    assert item["content"] == "rollback shipped"
    # The role hangs off the row that was superseded and points at its successor:
    # `role` names what the REFERENCED row is to this claim.
    assert item["claims"][0]["roles"] == []
    assert item["claims"][1]["roles"] == [{"ref": new, "role": "supersedes"}]
    assert item["timeline"] == [
        {"at": "2026-03-01T10:00:00+00:00", "ref": old},
        {"at": "2026-03-02T10:00:00+00:00", "ref": new},
    ]


@pytest.mark.asyncio
async def test_episode_containment_bundles_and_derives_supports():
    await _seed_unclustered()
    # On-query enough to clear the adaptive quality gate alongside the episode
    # summary: a row the gate drops cannot demonstrate a bundle, and the whole
    # point of this test is the pair. Nothing about recall is monkeypatched — the
    # bundle is read off the pool the shipped read path actually returns.
    await M.do_store(
        AGENT,
        {
            "content": "rollback",
            "source": {"type": "User", "id": "u-ep", "name": "ep"},
            "timestamp": "2026-04-01T12:00:00+00:00",
        },
    )
    out = await M.do_archive_episode(
        AGENT,
        [
            {"role": "user", "content": "start the rollback", "timestamp": "2026-04-01T11:00:00+00:00"},
            {"role": "assistant", "content": "done", "timestamp": "2026-04-01T13:00:00+00:00"},
        ],
        summary="rollback of the billing deploy, start to finish",
        keywords="rollback billing",
    )
    assert out.get("episode_id"), out
    res = await R.do_reconstruct(AGENT, QUERY, count=5, top_k=TOP_K)
    items = [i for i in res["items"] if i["independence_reason"] == "cluster:episode"]
    assert items, res
    item = items[0]
    refs = {c["ref"] for c in item["claims"]}
    assert any(r.startswith("ep:") for r in refs) and any(r.startswith("mem:") for r in refs)
    memory_claim = next(c for c in item["claims"] if c["ref"].startswith("mem:"))
    assert [r["role"] for r in memory_claim["roles"]] == ["supports"]
    assert memory_claim["roles"][0]["ref"].startswith("ep:")


@pytest.mark.asyncio
async def test_source_alone_does_not_bundle():
    """The adjacency key needs both halves; source alone would fold the whole pool."""
    await _seed_unclustered()  # one source, an hour apart
    out = await R.do_reconstruct(AGENT, QUERY, count=5, top_k=TOP_K)
    assert out["returned_count"] == 5
    assert all(i["independence_reason"] == "singleton" for i in out["items"]), [
        i["independence_reason"] for i in out["items"]
    ]


@pytest.mark.asyncio
async def test_a_conflict_the_keys_cannot_fold_is_shown_inside_one_item():
    """Same message id, same instant: version order cannot separate them (invariant 8)."""
    out, a, b = _structured_versions(
        "ticket-99", ("rollback variant A", "rollback variant B"),
        ("2026-05-01T10:00:00+00:00", "2026-05-01T10:00:00+00:00"),
    )
    item = _only_item_with(out, "cluster:msg_id")
    assert item["conflicts"] == [
        {"refs": sorted([a, b]), "reason": "same_msg_id_same_timestamp", "msg_id": "ticket-99"}
    ]
    # Both survive inside the one item; neither is dropped and neither is merged.
    assert {c["ref"] for c in item["claims"]} == {a, b}


# --------------------------------------------------------------------------
# The role vocabulary is a contract
# --------------------------------------------------------------------------


def test_role_vocabulary_is_fixed_and_v0_fills_part_of_it():
    assert R.ROLE_VOCABULARY == (
        "supports",
        "supersedes",
        "corrects",
        "qualifies",
        "contradicts",
        "temporal_predecessor",
    )
    assert R.V0_DERIVED_ROLES < set(R.ROLE_VOCABULARY), "v0 must not invent a role outside the vocabulary"


@pytest.mark.asyncio
async def test_only_v0_roles_are_emitted():
    """A reader written against the vocabulary sees nothing outside it, and nothing v0 cannot justify."""
    await _seed_unclustered()
    await _seed_burst()
    await _store_scoped_record("ticket-7", "rollback v1", "2026-06-01T10:00:00+00:00", "p1")
    await _store_scoped_record("ticket-7", "rollback v2", "2026-06-02T10:00:00+00:00", "")
    out = await R.do_reconstruct(AGENT, QUERY, count=10, top_k=TOP_K, project_id="p1")
    emitted = {r["role"] for item in out["items"] for c in item["claims"] for r in c["roles"]}
    assert emitted <= R.V0_DERIVED_ROLES, emitted


@pytest.mark.asyncio
async def test_stage_3_is_the_identity_and_says_so():
    """The hop bound is declared and reported before the walk follows any edge."""
    clusters = [[0, 1], [2]]
    out, truncated = R.walk(clusters, max_hops=2)
    assert out == clusters and truncated is False


# --------------------------------------------------------------------------
# Ordering — the item list follows relevance, which pins the rank convention
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_items_are_ordered_by_relevance():
    """do_recall emits its ranked list reversed (most relevant LAST).

    reconstruct converts that into a rank before ordering items, and this pins the
    conversion: with every row a singleton, the first item's head must be the row
    recall ranked first — its LAST message. Reading the list the other way round
    turns this red.
    """
    await _seed_unclustered()
    recalled = await M.do_recall(AGENT, QUERY, limit=TOP_K)
    most_relevant = recalled["messages"][-1]["ref"]
    out = await R.do_reconstruct(AGENT, QUERY, count=1, top_k=TOP_K)
    assert out["items"][0]["head_ref"] == most_relevant


# --------------------------------------------------------------------------
# The head claim: latest version of a record, otherwise the most relevant row
# --------------------------------------------------------------------------


def _row(n: int, content: str, stamp: str, rank: int, msg_id: str = "") -> R._Candidate:
    row = R._Candidate({"ref": f"mem:{n}", "id": msg_id, "content": content, "timestamp": stamp,
                        "source": {"type": "User", "id": "u-head"}}, rank=rank)
    row.context = ("same-project", "")
    return row


def _structure(rows: list[R._Candidate], max_evidence: int = 40) -> tuple[dict, bool]:
    uf = R.bundle(rows, {})
    assert len({uf.find(i) for i in range(len(rows))}) == 1, "fixture must form one cluster"
    why = {rows[i].ref: key for i, key in uf.why.items()}
    return R.structure(rows, why, {}, max_evidence)


def test_head_of_distinct_rows_is_the_most_relevant_not_the_newest():
    """A burst of distinct rows: the newest is only the last thing said."""
    relevant = _row(1, "my dog is named Rex", "2026-02-01T09:00:00+00:00", rank=0)
    later = _row(2, "ok thanks, see you", "2026-02-01T09:00:30+00:00", rank=5)
    item, _ = _structure([relevant, later])
    assert item["independence_reason"] == "cluster:adjacent"
    assert item["content"] == "my dog is named Rex"
    assert item["head_ref"] == relevant.ref
    # Claims keep their documented order: newest first.
    assert [c["ref"] for c in item["claims"]] == [later.ref, relevant.ref]


def test_head_of_a_version_chain_is_its_latest_version_even_when_older_ranks_higher():
    old = _row(1, "rollback pending", "2026-03-01T10:00:00+00:00", rank=0, msg_id="ticket-9")
    new = _row(2, "rollback shipped", "2026-03-02T10:00:00+00:00", rank=4, msg_id="ticket-9")
    item, _ = _structure([old, new])
    assert item["content"] == "rollback shipped"
    assert item["head_ref"] == new.ref


def test_version_rule_applies_only_to_the_record_relevance_chose():
    """A newer version of a LESS relevant record does not displace the most relevant row."""
    lead = _row(1, "deploy window is friday", "2026-03-02T10:00:00+00:00", rank=0)
    v1 = _row(2, "rollback pending", "2026-03-02T10:00:20+00:00", rank=1, msg_id="ticket-9")
    v2 = _row(3, "rollback shipped", "2026-03-02T10:00:40+00:00", rank=2, msg_id="ticket-9")
    item, _ = _structure([lead, v1, v2])
    assert item["head_ref"] == lead.ref
    assert item["content"] == "deploy window is friday"


def test_an_evidence_cut_keeps_the_head_then_the_most_relevant_rows():
    relevant = _row(1, "my dog is named Rex", "2026-02-01T09:00:00+00:00", rank=0)
    second = _row(2, "he is a beagle", "2026-02-01T09:00:10+00:00", rank=1)
    newest = _row(3, "ok thanks, see you", "2026-02-01T09:00:30+00:00", rank=7)
    item, truncated = _structure([relevant, second, newest], max_evidence=1)
    assert truncated is True
    assert item["head_ref"] == relevant.ref
    assert [c["ref"] for c in item["claims"]] == [relevant.ref]
    item, _ = _structure([relevant, second, newest], max_evidence=2)
    assert {e["ref"] for e in item["evidence"]} == {relevant.ref, second.ref}
    assert [c["ref"] for c in item["claims"]] == [second.ref, relevant.ref]
