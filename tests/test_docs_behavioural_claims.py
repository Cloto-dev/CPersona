"""Locks for behavioural claims the published documentation makes in prose.

Reading the pages against the implementation turned up six defects at once,
four of them already published for several releases, every one of them green
under all three guards this repo runs over its documentation. Those guards read
numbers (check-docs-facts), link and fragment resolution (check-doc-anchors),
and translation freshness (check-i18n-drift). None of them can read a sentence
about behaviour, which is where all six lived.

The answer is not a fourth guard that tries to read prose. It is to route each
claim to a mechanism that can already settle it: a behavioural test when the
claim describes observable behaviour, a structural gate when it asserts an
absence over every path (tests/test_structural_gates.py, Gate 14), and
check-docs-facts when it is a constant or a schema-derived count.

This file is the first of those lanes. Each test names the page and quotes the
claim, so a failure says which sentence to go and read — the sentence is the
contract, and the test is downstream of it. When a claim is deliberately
changed, the page and the test move together.
"""

import pytest
import pytest_asyncio

from cpersona import admin_handlers, memory_handlers
from cpersona.database import get_db

AGENT_A = "docs-claim-a"
AGENT_B = "docs-claim-b"


@pytest_asyncio.fixture
async def clean_db():
    db = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()
    return db


async def _seed(db, agent_id, contents):
    for index, content in enumerate(contents):
        await db.execute(
            "INSERT INTO memories (agent_id, content, timestamp, created_at) "
            "VALUES (?, ?, '', ?)",
            (agent_id, content, f"2026-08-{20 - index:02d} 00:00:00"),
        )
    await db.commit()


# --------------------------------------------------------------------------------------
# docs/architecture.md, "Isolation axes":
#
#   "Omitting the axis is the opposite case, and it is deliberate: a cross-agent
#    scan. The listing tools take it that way — a `list_memories` call with no
#    `agent_id` returns rows belonging to every agent in the database."
#
# The page said the opposite until Phase 6: it described omission as failing
# closed. It does not — do_list_memories maps a falsy agent_id to `or None`,
# which drops the predicate. The asymmetry is intentional and is what the
# maintenance and dashboard paths rely on, so it needs a lock in BOTH
# directions: a future reader who mistakes the `or None` for a fail-open bug and
# "fixes" it would silently break the documented behaviour and every caller of
# it, while a reader who widens the bound axis would break hard isolation.
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_listing_without_an_agent_id_scans_every_agent(clean_db):
    """The documented cross-agent scan: no agent_id means no filter."""
    await _seed(clean_db, AGENT_A, ["from a"])
    await _seed(clean_db, AGENT_B, ["from b"])

    result = await admin_handlers.do_list_memories("", 10)

    returned = {m["content"] for m in result["memories"]}
    assert returned == {"from a", "from b"}, (
        "docs/architecture.md ('Isolation axes') states that list_memories with no "
        "agent_id returns rows belonging to every agent. It returned "
        f"{sorted(returned)}. If the narrowing is intended, the page is now wrong — "
        "fix the sentence and this test together."
    )


@pytest.mark.asyncio
async def test_listing_with_an_agent_id_never_crosses_agents(clean_db):
    """The other half of the same table: a bound agent_id is hard isolation, no union.

    Without this, the test above passes just as well against a build that
    ignores agent_id entirely — a cross-agent scan and a broken filter look
    identical from one direction.
    """
    await _seed(clean_db, AGENT_A, ["from a"])
    await _seed(clean_db, AGENT_B, ["from b"])

    result = await admin_handlers.do_list_memories(AGENT_A, 10)

    returned = {m["content"] for m in result["memories"]}
    assert returned == {"from a"}, (
        "docs/architecture.md ('Isolation axes') states that agents never share rows "
        f"when agent_id is bound. Listing for {AGENT_A} returned {sorted(returned)}."
    )


# --------------------------------------------------------------------------------------
# skills/cpersona-memory/SKILL.md, "Session end" and the mandatory-trigger list:
#
#   "Pass the REAL history — it sets the episode's start/end times from the turns'
#    timestamps, but never reaches the embedding, which comes from `summary` alone."
#
# The page said the opposite until now — that the history "drives timestamps and the
# episode embedding". That version had a running cost rather than a latent one: the
# skill ships inside the wheel and instructs every calling agent, so each archive_episode
# carried a full transcript bought for a retrieval benefit that does not exist. The
# corrected sentence is worth a lock precisely because the wrong one is the intuitive
# guess, and the next person to edit this paragraph will guess it again.
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_episode_embedding_ignores_the_conversation_history(clean_db, fake_embedding_client):
    """Only the summary reaches the encoder; history sets the time span and nothing else."""
    summary = "the session settled on the cheaper of the two designs"
    turns = [
        {"role": "user", "content": "which design do we ship", "timestamp": "2026-08-01T00:00:00"},
        {"role": "assistant", "content": "the cheaper one", "timestamp": "2026-08-02T00:00:00"},
    ]

    await memory_handlers.do_archive_episode(AGENT_A, turns, summary=summary)
    await memory_handlers.do_archive_episode(AGENT_B, [], summary=summary)

    rows = await clean_db.execute_fetchall(
        "SELECT agent_id, embedding, start_time, end_time FROM episodes "
        "WHERE agent_id IN (?, ?) ORDER BY agent_id",
        (AGENT_A, AGENT_B),
    )
    assert len(rows) == 2, f"expected one episode per agent, got {len(rows)}"
    with_history, without_history = rows

    assert with_history[1] == without_history[1], (
        "SKILL.md states that the episode embedding comes from `summary` alone. Archiving "
        "the same summary with and without conversation history produced different "
        "embeddings, so history now reaches the encoder — say so on the page, because the "
        "current wording tells agents the transcript is not worth sending."
    )
    assert with_history[1] is not None, (
        "both embeddings are NULL — the encoder never ran, so this test proves nothing. "
        "Check that the fake embedding client is installed."
    )
    assert (with_history[2], with_history[3]) == ("2026-08-01T00:00:00", "2026-08-02T00:00:00"), (
        "history is documented as the source of the episode's start and end times"
    )
    assert (without_history[2], without_history[3]) == (None, None), (
        "an empty history is documented as leaving the time span unset"
    )


# --------------------------------------------------------------------------------------
# skills/cpersona-memory/SKILL.md, tool reference:
#
#   "`channel` on `store` / `recall`, and `source_id` on `recall`"
#
# The table said `source_id` was an argument of both. It is not on `store`, so an agent
# following the row called store(..., source_id=…) and the argument was dropped on the
# floor — a silent no-op, which is the worst shape for a wrong instruction. Per-user
# attribution on a write travels inside `message.source.id`.
#
# Argument names in a shipped instruction file are checkable against the schema the
# server actually advertises, so they should never be settled by reading alone.
# --------------------------------------------------------------------------------------


def test_source_id_is_a_recall_argument_and_not_a_store_argument():
    """The axis table in the shipped skill must not name arguments the tool lacks."""
    from cpersona import server

    tools = {t.name: t for t in server.registry._tools}
    store_args = set(tools["store"].inputSchema.get("properties", {}))
    recall_args = set(tools["recall"].inputSchema.get("properties", {}))

    assert "source_id" not in store_args, (
        "`store` now advertises source_id. The shipped skill tells agents that a write "
        "carries its producer in message.source.id instead — update the tool-reference "
        "table in skills/cpersona-memory/SKILL.md before this becomes the wrong advice."
    )
    assert "source_id" in recall_args, (
        "`recall` no longer advertises source_id, which the shipped skill names as the "
        "per-user read filter."
    )
    assert "channel" in store_args and "channel" in recall_args, (
        "the same table names `channel` on both tools"
    )


# --------------------------------------------------------------------------------------
# docs/tools.md, "Protection and deletion":
#
#   "Delete one memory. Ownership is enforced **only when `agent_id` is passed** — omit
#    it and the delete is unscoped and can remove another agent's row"
#
# The row said "(ownership enforced)" flat, for both delete tools, since the page was
# written. The implementation has always enforced it conditionally: do_delete_memory
# folds `AND agent_id = ?` into the DELETE only when the argument is non-empty, and
# logs `UNSCOPED (no ownership enforcement)` when it is not. That branch is deliberate
# — bug-137 chose to warn while "preserving the injected-trust behavior", the design
# where a trusted kernel injects the scope — so the page moved, not the code.
#
# Pinned in BOTH directions. A test that only checked the scoped call would stay green
# against an implementation that ignored agent_id entirely, and a test that only checked
# the unscoped call would stay green against one that enforced nothing at all. The claim
# is the shape of the pair, so the pair is what the test holds.
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_enforces_ownership_only_when_agent_id_is_passed(clean_db):
    """docs/tools.md promises conditional ownership on delete_memory — both halves."""
    await _seed(clean_db, AGENT_A, ["owned by A"])
    rows = await clean_db.execute_fetchall(
        "SELECT id FROM memories WHERE agent_id = ?", (AGENT_A,)
    )
    victim = rows[0][0]

    refused = await admin_handlers.do_delete_memory(victim, agent_id=AGENT_B)
    assert refused.get("ok") is False, (
        "a delete scoped to another agent succeeded. docs/tools.md promises ownership "
        "is enforced when agent_id is passed; if that is no longer true the row in the "
        "'Protection and deletion' table is now wrong."
    )
    still_there = await clean_db.execute_fetchall(
        "SELECT id FROM memories WHERE id = ?", (victim,)
    )
    assert still_there, "the refused delete removed the row anyway"

    unscoped = await admin_handlers.do_delete_memory(victim)
    assert unscoped.get("ok") is True, (
        "the unscoped delete was refused. That is a safer implementation, but it makes "
        "docs/tools.md wrong in the other direction — the row says omitting agent_id "
        "leaves the delete unscoped. Move the page with the code."
    )
    assert (
        await clean_db.execute_fetchall("SELECT id FROM memories WHERE id = ?", (victim,))
        == []
    )


# --------------------------------------------------------------------------------------
# docs/architecture.md, "Isolation axes", and the same sentence duplicated in
# cpersona/isolation.py's module docstring:
#
#   "binding `''` narrows rather than widens: [...] That bucket is a real address, not a
#    value no write produces: `store` accepts an empty `agent_id`, because *required* in
#    a tool schema means present, not non-empty."
#
# Both said the opposite until now — that `''` "matches nothing any write produces", so
# a predicate that forgot to decide the axis failed closed. It does not fail closed in
# that sense: the empty-agent bucket is writable, so `''` selects real rows. What does
# hold is the half worth keeping — those rows belong to no named agent, so the mistake
# still cannot leak across agents.
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_agent_id_is_a_writable_bucket_not_an_impossible_value(clean_db):
    """architecture.md no longer claims `''` matches nothing a write produces."""
    result = await memory_handlers.do_store(agent_id="", message={"content": "empty bucket"})
    assert result.get("result") == "stored", (
        "store now refuses an empty agent_id. If that is intended, architecture.md and "
        "cpersona/isolation.py must go back to describing `''` as unreachable — right "
        "now both say the bucket is a real address."
    )

    rows = await clean_db.execute_fetchall("SELECT agent_id FROM memories WHERE agent_id = ''")
    assert rows, "the write did not land in the empty-agent bucket"

    await _seed(clean_db, AGENT_A, ["owned by A"])
    leaked = await clean_db.execute_fetchall(
        "SELECT id FROM memories WHERE agent_id = '' AND content = 'owned by A'"
    )
    assert leaked == [], (
        "binding '' reached a named agent's row. The narrowing half of the claim is the "
        "one that still holds — it must not become false silently."
    )


# --------------------------------------------------------------------------------------
# skills/cpersona-memory/SKILL.md, "Mandatory triggers" and the shipped policy block:
#
#   "Use `deep=true` when the first pass comes back thin: it halves the quality gate,
#    so weaker matches are admitted. It does not widen the scan window."
#
# Both lines said the opposite until now — "dig past time decay" and "search the full
# history without time decay". Time decay lives in `_compute_confidence`, and BOTH of
# its call sites are behind `CONFIDENCE_ENABLED`, which ships false. So on a default
# install there is no decay to dig past: the only thing `deep` changes is the quality
# gate (`min_score * 0.5`, and the calibrated fused gate likewise halved). "Full
# history" was false in the other direction — retrieval is bounded by the vector scan
# window (`CPERSONA_MAX_MEMORIES`), which `deep` does not touch.
#
# Pinned in both directions, for the reason test #4 above needed it: an assertion that
# `deep` merely returns "at least as much" would also pass on an implementation where
# `deep` did nothing at all, which is the exact claim being corrected.
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deep_relaxes_the_quality_gate_rather_than_a_time_horizon(
    clean_db, fake_embedding_client
):
    """SKILL.md's `deep=true`: weaker matches, not an older horizon."""
    for index in range(40):
        await memory_handlers.do_store(
            AGENT_A, {"content": f"filler row {index} gardening tools soil"}
        )
    await memory_handlers.do_store(AGENT_A, {"content": "quantum tunnelling diodes"})

    shallow = await memory_handlers.do_recall(AGENT_A, "quantum diodes tunnelling", limit=10)
    deep = await memory_handlers.do_recall(
        AGENT_A, "quantum diodes tunnelling", limit=10, deep=True
    )

    shallow_contents = {m["content"] for m in shallow["messages"]}
    deep_contents = {m["content"] for m in deep["messages"]}

    assert not any("confidence" in m for m in deep["messages"]), (
        "a `confidence` block came back on a default install, so time decay is live "
        "after all and SKILL.md's correction ('the gate, not a time horizon') is "
        "itself now wrong. CONFIDENCE_ENABLED ships false — if that changed, both the "
        "SKILL lines and the recall tool description need rewriting together."
    )

    assert deep_contents > shallow_contents, (
        "SKILL.md tells the caller that `deep=true` halves the quality gate so weaker "
        f"matches are admitted. It admitted {len(deep_contents)} rows against the default "
        f"pass's {len(shallow_contents)}. If `deep` no longer relaxes the gate, the "
        "sentence in SKILL.md ('Mandatory triggers', and the shipped policy block) is "
        "now the wrong advice to give an agent whose first pass came back thin."
    )
    admitted = deep_contents - shallow_contents
    assert all("gardening" in c for c in admitted), (
        "the rows `deep` adds should be the weak matches the gate had rejected; it "
        f"added {sorted(admitted)}"
    )



# --------------------------------------------------------------------------------------
# docs/operations.md ("Tuning recall") and docs/configuration.md, on
# CPERSONA_FUSED_GATE_ENABLED=false:
#
#   "filtering falls back to the pool-size heuristic (`_adaptive_min_score`), which
#    still rejects weak matches — it is a coarser gate, not an open door."
#
# Both pages said "contamination passes through unfiltered", which would make turning
# the gate off a very different decision than it is. `gate` becomes None and
# _apply_quality_gate still applies `effective_min`, so the pool-size floor keeps
# cutting. An operator who read the old sentence would either avoid a knob that is
# safe to reach for, or reach for it expecting a diagnostic firehose and get one row.
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabling_the_fused_gate_leaves_the_pool_size_floor(
    clean_db, fake_embedding_client, monkeypatch
):
    """Turning off the fused gate is a coarser gate, not an open door."""
    from cpersona import config

    for index in range(40):
        await memory_handlers.do_store(
            AGENT_A, {"content": f"filler row {index} gardening tools soil"}
        )
    await memory_handlers.do_store(AGENT_A, {"content": "quantum tunnelling diodes"})

    monkeypatch.setattr(config, "FUSED_GATE_ENABLED", False)
    result = await memory_handlers.do_recall(AGENT_A, "quantum diodes tunnelling", limit=10)

    contents = [m["content"] for m in result["messages"]]
    assert not any("gardening" in c for c in contents), (
        "with CPERSONA_FUSED_GATE_ENABLED=false the weak rows came back, so the pages "
        "should go back to warning that contamination passes unfiltered. Right now "
        "both operations.md and configuration.md tell the operator the pool-size "
        f"heuristic still cuts. Returned: {contents}"
    )


# --------------------------------------------------------------------------------------
# docs/operations.md ("Tuning recall") and docs/configuration.md, on
# CPERSONA_AUTOCUT_MIN_RESULTS:
#
#   "autocut fires on similarity-scale signals: under confidence scoring, or on a
#    homogeneous raw-cosine list (which is what `cascade` produces, confidence on or
#    off) [...] so under the default configuration this knob does nothing — but it is
#    the fusion mode that decides that, not the confidence flag."
#
# Both pages said autocut was "only meaningful with confidence scoring enabled". The
# conclusion they drew from it — inert by default — is right, but the reason was not,
# and an operator debugging a truncated `cascade` recall would have ruled out the
# knob that was doing the truncating. Pinned on both sides for that reason: the
# firing case (cascade, confidence off) and the inert case (rrf, the default) are
# different halves of the sentence and can break independently.
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_autocut_keys_on_the_fusion_mode_not_the_confidence_flag(
    clean_db, fake_embedding_client, monkeypatch
):
    """Autocut fires on a raw-cosine list with confidence off, and not under rrf."""
    # Cosine gaps wide enough to trip the heuristic (1.0 / .84 / .73 / .56, so a
    # relative gap of .17 against the .15 floor). The same list under `rrf` scores
    # .0328 / .0323 / .0317 / .0313 — no gap the heuristic would act on — which is
    # the difference the two halves below are measuring.
    for content in (
        "alpha beta",
        "alpha beta zeta",
        "alpha beta zeta eta",
        "alpha omicron pi rho sigma tau",
    ):
        await memory_handlers.do_store(AGENT_A, {"content": content})

    # Pinned rather than asserted. do_recall reads the flag through the name
    # memory_handlers bound at import, and the suite's ambient config.CONFIDENCE_ENABLED
    # does not track it: another module sets CPERSONA_CONFIDENCE_ENABLED in the process
    # env and a third reloads config, so the config attribute flips True mid-run while
    # the recall path keeps running confidence-off. Reading the ambient value here would
    # make this test's outcome depend on collection order. That the SHIPPED default is
    # confidence-off is pinned separately, on an observable response, by
    # test_deep_relaxes_the_quality_gate_rather_than_a_time_horizon.
    monkeypatch.setattr(memory_handlers, "CONFIDENCE_ENABLED", False)

    monkeypatch.setattr(memory_handlers, "RECALL_MODE", "cascade")
    monkeypatch.setattr(memory_handlers, "AUTOCUT_ENABLED", True)
    cut = await memory_handlers.do_recall(AGENT_A, "alpha beta", limit=10)
    monkeypatch.setattr(memory_handlers, "AUTOCUT_ENABLED", False)
    uncut = await memory_handlers.do_recall(AGENT_A, "alpha beta", limit=10)

    assert len(cut["messages"]) < len(uncut["messages"]), (
        "under `cascade` with confidence off, autocut did not cut "
        f"({len(cut['messages'])} vs {len(uncut['messages'])} rows). Both pages now say "
        "it fires on a homogeneous raw-cosine list regardless of the confidence flag; "
        "if that stopped being true they should go back to naming confidence as the "
        "condition."
    )

    monkeypatch.setattr(memory_handlers, "RECALL_MODE", "rrf")
    monkeypatch.setattr(memory_handlers, "AUTOCUT_ENABLED", True)
    rrf_cut = await memory_handlers.do_recall(AGENT_A, "alpha beta", limit=10)
    monkeypatch.setattr(memory_handlers, "AUTOCUT_ENABLED", False)
    rrf_uncut = await memory_handlers.do_recall(AGENT_A, "alpha beta", limit=10)

    assert len(rrf_cut["messages"]) == len(rrf_uncut["messages"]), (
        "autocut cut a rank-fusion list. Under the default `rrf` both pages promise "
        "the knob does nothing, and bug-013 made that so deliberately — rank-fusion "
        "gaps encode retriever overlap, not relevance breaks. Note what this half can "
        "and cannot see: it catches the branch precedence collapsing (cosine applied "
        "to an rrf-ordered list, which is bug-013 itself), because these rows carry "
        "both signals. It would NOT catch autocut being rewired to cut on the rrf "
        "score, whose gaps on this fixture are far below the ratio floor — that "
        "regression needs a corpus wide enough for rank fusion to spread, which is "
        "not what this file is for."
    )


# --------------------------------------------------------------------------------------
# docs/behavior-contracts.md §8, and the same claim in docs/architecture.md ("Retrieval",
# stage 2):
#
#   "The rescue path exists only under confidence scoring. The rows it returns are marked
#    by the same backfill that runs when `CPERSONA_CONFIDENCE_ENABLED` is on, so in the
#    default configuration `gate_fallback` can never appear: an all-below-gate recall
#    simply returns nothing."
#
# This is a reachability claim about a marker a caller is told to branch on, and it is the
# kind that rots quietly: the rescue would still look correct in review if someone moved
# the backfill out from behind the flag, and the only symptom would be a marker the page
# says is unreachable turning up in production responses.
#
# Both halves are pinned because they fail independently. A test that only asserted the
# default-off half would also pass on an implementation where the rescue was deleted
# outright — which is the opposite defect, and it would mean §8 documents a marker that
# no configuration can produce.
#
# The gate is forced rather than fitted. `_adaptive_min_score` is monkeypatched above
# every branch's scale (confidence / rsf / cosine are 0-1; the rrf branch compares against
# min_score * RRF_MAX_SCALE), so "every candidate fell below the quality gate" holds by
# construction instead of depending on a corpus that happens to score low. The per-agent
# vector threshold is raised for the same reason: it keeps the identifier row out of the
# vector arm, so it arrives through FTS alone and cosine-less, which is the row the
# backfill exists to move — the "identifier/hash lookup" case §8 names.
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_fallback_is_unreachable_with_confidence_off(
    clean_db, fake_embedding_client, monkeypatch
):
    """§8's reachability claim, pinned on both sides of the confidence flag."""
    from cpersona import vector

    identifier = "a3f9c2deadbeef"
    for index in range(12):
        await memory_handlers.do_store(
            AGENT_A, {"content": f"gardening notes {index} soil water sun tools"}
        )
    # Lexically an exact hit, semantically far: one rare token diluted among twenty, which
    # puts its cosine (~0.22 under the deterministic test embedder) well under the vector
    # arm's floor below and leaves the margin insensitive to small scoring changes.
    await memory_handlers.do_store(
        AGENT_A,
        {
            "content": "deploy log soil water sun tools note rollback done shipped "
            f"staging queue drain retry batch worker timer flush {identifier}"
        },
    )

    # A calibrated agent whose vector arm demands a close match; the identifier row is
    # below it, so only FTS finds it.
    monkeypatch.setitem(vector._agent_thresholds, AGENT_A, 0.9)
    # An impossible gate: above every branch's scale, so nothing survives it.
    monkeypatch.setattr(memory_handlers, "_adaptive_min_score", lambda count: 5.0)

    monkeypatch.setattr(memory_handlers, "CONFIDENCE_ENABLED", False)
    default = await memory_handlers.do_recall(AGENT_A, identifier, limit=10)

    assert default["messages"] == [], (
        "an all-below-gate recall returned rows under the shipped configuration. "
        f"behavior-contracts.md §8 and architecture.md both promise an empty response "
        f"there. Got: {[m['content'] for m in default['messages']]}"
    )
    assert "gate_fallback" not in default, (
        "`gate_fallback` appeared with CPERSONA_CONFIDENCE_ENABLED off. Both pages tell "
        "the reader the marker is unreachable in the default configuration, so a caller "
        "who never enabled confidence has no branch for it — either the backfill left "
        "the flag, or the rescue stopped keying on it. The pages and the two tool "
        "descriptions in server.py have to move with that change."
    )

    monkeypatch.setattr(memory_handlers, "CONFIDENCE_ENABLED", True)
    rescued = await memory_handlers.do_recall(AGENT_A, identifier, limit=10)

    assert rescued.get("gate_fallback") is True, (
        "with confidence scoring on, the same all-below-gate recall did not raise "
        "`gate_fallback`. §8 documents a rescue path that exists for exactly this case "
        "(an identifier whose exact match is semantically distant); if it no longer "
        "fires, §8 describes a marker no configuration produces and the section should "
        f"go, not be left as advice. Response keys: {sorted(rescued)}"
    )
    assert any(identifier in m["content"] for m in rescued["messages"]), (
        "the rescue fired but did not return the exact lexical match it exists to keep "
        f"visible. Returned: {[m['content'] for m in rescued['messages']]}"
    )


# --------------------------------------------------------------------------------------
# docs/architecture.md, "The three memory types", on the profile row:
#
#   "Appended to recall responses **when the scope holds at least 50 rows** (memories and
#    episodes together — the pool the gate governs; below that the gate drops it) [...]"
#
# The page said "at least 50 memories" until this test was written, and measurement moved
# the sentence: 49 memories plus one episode injects the profile. That is not an accident
# to be tidied away in the doc's favour — bug-216 made the gate count the pool it actually
# governs (memories + episodes over the recall's isolation scope) precisely because an
# episodes-only agent had every row blocked by a threshold computed over an empty
# `memories` table. The old sentence would have an operator with 40 memories and 15
# episodes conclude their profile is being dropped while it is being injected.
#
# Pinned on both sides of the boundary, plus the episode contribution, because the three
# fail independently: an off-by-one in the comparison moves only the first two, and
# scoping the count back to `memories` alone moves only the third.
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_profile_injection_needs_fifty_rows_counting_episodes(
    clean_db, fake_embedding_client
):
    """The profile gate's threshold, and what it counts as a row."""
    from cpersona import admin_handlers

    async def _recall_with(memories: int, episodes: int) -> list[str]:
        for table in ("memories", "episodes", "profiles"):
            await clean_db.execute(f"DELETE FROM {table}")
        await clean_db.commit()
        await admin_handlers.do_update_profile(AGENT_A, "operator prefers metric units")
        for index in range(memories):
            await memory_handlers.do_store(
                AGENT_A, {"content": f"gardening notes {index} soil water sun"}
            )
        for index in range(episodes):
            await memory_handlers.do_archive_episode(
                AGENT_A, [], summary=f"a session about soil {index}", keywords="soil"
            )
        # A query no row answers: the scored channels come back empty, so what survives
        # is the profile row alone and the gate's verdict on it is the whole result.
        result = await memory_handlers.do_recall(AGENT_A, "zzqxunrelatedtoken", limit=10)
        return [m["content"] for m in result["messages"]]

    below = await _recall_with(memories=49, episodes=0)
    assert below == [], (
        "the profile row was injected into a 49-row scope. architecture.md tells the "
        f"reader the gate drops it below 50, and behavior-contracts.md §7 sends anyone "
        f"who needs guaranteed presence to deterministic injection instead. Got: {below}"
    )

    at_threshold = await _recall_with(memories=50, episodes=0)
    assert any(c.startswith("[Profile]") for c in at_threshold), (
        "the profile row did not appear at exactly 50 rows, so either the threshold "
        f"moved or profile injection stopped working. architecture.md names 50. Got: "
        f"{at_threshold}"
    )

    with_episode = await _recall_with(memories=49, episodes=1)
    assert any(c.startswith("[Profile]") for c in with_episode), (
        "49 memories plus one episode did not clear the gate, so the pool is being "
        "counted over `memories` alone again. bug-216 scoped it to memories + episodes "
        "so an episodes-only agent is not gated by a threshold computed over an empty "
        "table, and architecture.md says 'rows (memories and episodes together)'. If "
        f"the count is deliberately narrowing, that sentence moves with it. Got: "
        f"{with_episode}"
    )


# --------------------------------------------------------------------------------------
# docs/tools.md, on check_health:
#
#   "Some checks are report-only by design — isolation-axis hygiene among them, because
#    which spelling of an axis is canonical is an operator's call, not a repair"
#
# `fix=true` is the one argument in this API that rewrites rows, and this sentence is the
# only thing telling an operator that one of the checks it runs will not act on what it
# reports. The failure it guards against is silent and destructive in the direction that
# matters: a well-meant "repair" that folds `cycia-mc` into `cyciamc` moves rows across a
# hard γ-isolation boundary, and the operator's evidence that it did not happen is a
# sentence on a page.
#
# Pinned in both directions. Asserting only that the rows are unchanged would also pass if
# the check stopped detecting drift at all, which reads as "clean" and would leave the
# split buckets in place with nothing reporting them.
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_axis_hygiene_reports_naming_drift_without_repairing_it(clean_db):
    """check_health(fix=True) reports axis drift and leaves the spellings alone."""
    from cpersona import maintenance_handlers

    for project_id in ("cycia-mc", "cyciamc"):
        await clean_db.execute(
            "INSERT INTO memories (agent_id, content, timestamp, created_at, project_id) "
            "VALUES (?, ?, '', '2026-08-20 00:00:00', ?)",
            (AGENT_A, f"a note filed under {project_id}", project_id),
        )
    await clean_db.commit()

    result = await maintenance_handlers.do_check_health(
        agent_id=AGENT_A, fix=True, checks=["axis_hygiene"]
    )

    reported = [i for i in result["issues"] if i["type"] == "project_id_naming_drift"]
    assert reported, (
        "two spellings of one project_id went unreported by a check whose whole job is "
        "to surface them. tools.md describes axis hygiene as report-only, which is only "
        f"worth saying if it reports. Issues: {result['issues']}"
    )

    rows = await clean_db.execute_fetchall(
        "SELECT project_id FROM memories WHERE agent_id = ? ORDER BY project_id", (AGENT_A,)
    )
    assert [r[0] for r in rows] == ["cycia-mc", "cyciamc"], (
        "check_health(fix=True) rewrote a project_id. tools.md promises the operator "
        "that axis hygiene is report-only because canonicalising a spelling is their "
        "call — and project_id is a hard isolation axis, so folding one bucket into "
        f"another moves rows across a boundary no recall crosses back. Rows now: {rows}"
    )


# --------------------------------------------------------------------------------------
# docs/architecture.md, "Retrieval":
#
#   "**Three retrievers** feed the fusion step: vector search, FTS5 over memories, and
#    FTS5 over episodes. [...] A **keyword (`LIKE`) pass is not a fourth retriever.** It
#    sits inside the memories channel as a fallback and runs only when FTS is disabled or
#    its `MATCH` returns nothing — so it never merges alongside the FTS memories
#    retriever, it stands in for it."
#
# The diagram on that page is drawn from this claim, and the arithmetic downstream of it
# is a caller's: rank fusion gives a row one vote per channel it wins, so a fourth channel
# over the same table would double-count every lexical hit and quietly re-weight the whole
# merge toward it. This is also the shape bug-155 and bug-215 were both about.
#
# The observable is the row's `_bm25`: the FTS branch carries a score, the LIKE branch
# carries None (the "uniform 1.0" vote _minmax_norm documents). One call to the memories
# channel returning one branch's rows is what "stands in for it" means, so the test pins
# the call count and the branch together — a spy that only counted calls would pass on an
# implementation that merged both result sets inside the one call.
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_keyword_pass_stands_in_for_fts_rather_than_joining_it(
    clean_db, fake_embedding_client, monkeypatch
):
    """Three retrievers feed fusion; LIKE substitutes inside one of them."""
    calls: dict[str, int] = {}
    memories_rows: list[list[dict]] = []

    def _spy(name, fn, record=False):
        async def wrapper(*args, **kwargs):
            calls[name] = calls.get(name, 0) + 1
            rows = await fn(*args, **kwargs)
            if record:
                memories_rows.append(rows)
            return rows

        return wrapper

    monkeypatch.setattr(
        memory_handlers, "_search_vector", _spy("vector", memory_handlers._search_vector)
    )
    monkeypatch.setattr(
        memory_handlers,
        "_search_episodes_fts",
        _spy("episodes_fts", memory_handlers._search_episodes_fts),
    )
    monkeypatch.setattr(
        memory_handlers,
        "_search_memories_keyword",
        _spy("memories", memory_handlers._search_memories_keyword, record=True),
    )

    await memory_handlers.do_store(AGENT_A, {"content": "the deployment runbook for staging"})
    await memory_handlers.do_store(AGENT_A, {"content": "an unrelated note about soil"})

    await memory_handlers.do_recall(AGENT_A, "deployment runbook", limit=10)

    assert calls == {"vector": 1, "episodes_fts": 1, "memories": 1}, (
        "the fusion step was fed by something other than exactly three retriever calls. "
        "architecture.md names three and draws its pipeline diagram from that count; a "
        "fourth channel over `memories` would give every lexical hit a second rank-fusion "
        f"vote. Calls: {calls}"
    )
    assert memories_rows[-1] and all(r["_bm25"] is not None for r in memories_rows[-1]), (
        "the memories channel returned LIKE-branch rows (no bm25) for a query FTS can "
        "answer. The page says the LIKE pass runs only when MATCH returns nothing; if it "
        f"now runs first or alongside, bm25 ranking is gone from that channel. Rows: "
        f"{memories_rows[-1]}"
    )

    # A two-character query: no trigram term can be formed, so _build_fts_query returns ""
    # and the LIKE branch is the only thing that can answer — the substitution the page
    # describes, exercised from the same call site.
    memories_rows.clear()
    calls.clear()
    await memory_handlers.do_store(AGENT_A, {"content": "release ok"})
    await memory_handlers.do_recall(AGENT_A, "ok", limit=10)

    assert calls.get("memories") == 1, (
        f"the memories channel was not called exactly once on the fallback path: {calls}"
    )
    assert memories_rows[-1] and all(r["_bm25"] is None for r in memories_rows[-1]), (
        "a query with no usable trigram term did not fall through to the LIKE pass. That "
        "fallback is what keeps short and CJK queries answerable at all (bug-215), and "
        f"architecture.md documents it as the substitution branch. Rows: {memories_rows[-1]}"
    )


# --------------------------------------------------------------------------------------
# docs/architecture.md, "Retrieval", on the two bounds around fusion:
#
#   "The [episode boundary penalty] works on the other side: it multiplies the *already
#    fused* score of memories older than the most recent `archive_episode`, before the
#    gate runs."
#
# Order is the whole claim here, and under the default `rrf` mode it is load-bearing
# rather than descriptive: rank fusion is ordinal, so a penalty applied to a channel's
# raw scores *before* the merge would leave every rank — and therefore every fused score —
# exactly where it was. The penalty exists at all only because it lands after the merge.
# "Before the gate" is the second half: the gate compares the penalised value, so an old
# row can be dropped outright rather than merely demoted.
#
# Measured at the gate's own input, which is the one place both halves are visible at
# once. Comparing the response rows instead would show a reordering but could not tell a
# post-fusion penalty apart from a pre-fusion one that happened to reorder — and could not
# see the value the gate keyed on at all.
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_episode_penalty_scales_the_fused_score_before_the_gate(
    clean_db, fake_embedding_client, monkeypatch
):
    """The penalty multiplies `_rrf_score`, and the gate sees the multiplied value."""
    gate_input: list[list[tuple[str, float | None]]] = []
    original_gate = memory_handlers._apply_quality_gate

    def spy(results, *args, **kwargs):
        gate_input.append([(r.get("content", ""), r.get("_rrf_score")) for r in results])
        return original_gate(results, *args, **kwargs)

    monkeypatch.setattr(memory_handlers, "_apply_quality_gate", spy)

    await clean_db.execute(
        "INSERT INTO memories (agent_id, content, timestamp, created_at) "
        "VALUES (?, 'the staging deployment runbook', '2026-08-01 00:00:00', "
        "'2026-08-01 00:00:00')",
        (AGENT_A,),
    )
    await clean_db.execute(
        "INSERT INTO episodes (agent_id, summary, keywords, created_at) "
        "VALUES (?, 'a prior session', '', '2026-08-20 00:00:00')",
        (AGENT_A,),
    )
    await clean_db.execute(
        "INSERT INTO memories (agent_id, content, timestamp, created_at) "
        "VALUES (?, 'the staging deployment checklist', '2026-08-21 00:00:00', "
        "'2026-08-21 00:00:00')",
        (AGENT_A,),
    )
    await clean_db.commit()

    monkeypatch.setattr(memory_handlers, "EPISODE_PENALTY_ENABLED", False)
    await memory_handlers.do_recall(AGENT_A, "staging deployment", limit=10)
    unpenalised = dict(gate_input[-1])

    monkeypatch.setattr(memory_handlers, "EPISODE_PENALTY_ENABLED", True)
    await memory_handlers.do_recall(AGENT_A, "staging deployment", limit=10)
    penalised = dict(gate_input[-1])

    old, recent = "the staging deployment runbook", "the staging deployment checklist"
    assert unpenalised.get(old) and unpenalised.get(recent), (
        f"both rows should reach the gate with a fused score: {unpenalised}"
    )
    assert penalised[old] < unpenalised[old], (
        "the row written before the last archive_episode reached the gate with its fused "
        "score untouched. Under `rrf` the merge is ordinal, so a penalty applied to a "
        "channel's raw scores before fusion cannot change anything — if this stopped "
        "biting, the penalty is being applied on the wrong side of the merge and "
        f"architecture.md's 'already fused' is wrong. {unpenalised[old]} → {penalised[old]}"
    )
    assert penalised[recent] == unpenalised[recent], (
        "a memory written after the boundary was penalised too. The penalty's charter is "
        "to weaken CROSS-session rows so current-session signals take precedence; "
        f"penalising both is a uniform rescale, which is a no-op. {unpenalised[recent]} → "
        f"{penalised[recent]}"
    )


# --------------------------------------------------------------------------------------
# docs/operations.md, "Backup and restore":
#
#   "1. **Online physical backup** (first choice — safe while the server runs) [...]
#    Both produce a consistent snapshot under concurrent writes."
#
#   "**The `.db` is not the whole instance.** State lives outside it that none of the
#    backup forms above touch: `<CPERSONA_DB_PATH>.calibration.json` [...]"
#
# A backup runbook is only ever tested by the restore nobody rehearsed, so both halves are
# pinned here: the recommended form really does carry a live database across (including
# rows still sitting in the -wal file, which is why the page tells the operator not to
# script a `cp`), and it really does leave the sidecar behind — the sentence that turns a
# silent loss of tuning into a documented step.
#
# What this covers and what it does not: the backup is taken through the same SQLite
# online-backup API the `sqlite3 ... ".backup"` shell command drives, from a second
# connection to the live file, so the mechanism and the concurrency are the real ones. The
# shell wrapper itself is not exercised — this asserts the guarantee the page makes, not
# the spelling of the command.
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_online_backup_carries_the_database_and_leaves_the_sidecar(
    clean_db, fake_embedding_client, tmp_path
):
    """The recommended backup form: complete as to the .db, silent as to the sidecar."""
    import os
    import sqlite3

    from cpersona import admin_handlers, config

    await memory_handlers.do_store(AGENT_A, {"content": "the staging deployment runbook"})
    await memory_handlers.do_store(AGENT_A, {"content": "an unrelated note about soil"})

    # The live file is read off the connection, not off config.DB_PATH: modules in this
    # suite reload `config` after removing CPERSONA_DB_PATH from the environment, so the
    # attribute can point somewhere the open connection does not, and sqlite3.connect on
    # a path that does not exist would silently back up a freshly created empty database.
    # config.DB_PATH is then pinned to the live file for the duration, so the sidecar
    # path this test writes and the database it backs up are the same instance.
    live_db_path = (await clean_db.execute_fetchall("PRAGMA database_list"))[0][2]
    ambient_db_path = config.DB_PATH
    config.DB_PATH = live_db_path
    sidecar = admin_handlers._calibration_sidecar_path()
    previous = None
    if os.path.exists(sidecar):
        with open(sidecar, "rb") as handle:
            previous = handle.read()
    try:
        admin_handlers._save_calibration_state(
            embedding_dim=8,
            embedding_model="fake",
            global_threshold=0.42,
            agent_thresholds={AGENT_A: 0.61},
        )
        assert os.path.exists(sidecar), "the calibration writer did not produce a sidecar"

        destination = tmp_path / "cpersona-backup.db"
        # A second connection to the live file, as the shell command is.
        source = sqlite3.connect(live_db_path)
        target = sqlite3.connect(destination)
        with target:
            source.backup(target)
        source.close()

        rows = target.execute(
            "SELECT content FROM memories WHERE agent_id = ? ORDER BY id", (AGENT_A,)
        ).fetchall()
        assert [r[0] for r in rows] == [
            "the staging deployment runbook",
            "an unrelated note about soil",
        ], (
            "the online backup came back without rows the running server had already "
            "written. operations.md calls this form safe under concurrent writes, and "
            "tells the operator not to script a `cp` precisely because those rows can "
            f"still be sitting in the -wal file. Got: {rows}"
        )
        embedded = target.execute(
            "SELECT COUNT(*) FROM memories WHERE embedding IS NOT NULL AND agent_id = ?",
            (AGENT_A,),
        ).fetchone()[0]
        assert embedded == 2, (
            f"embeddings did not survive the backup ({embedded}/2), so a restore would "
            "come back with no local vector arm and nothing in the runbook says so"
        )
        matched = target.execute(
            "SELECT COUNT(*) FROM memories_fts WHERE memories_fts MATCH 'deployment'"
        ).fetchone()[0]
        assert matched == 1, (
            f"the FTS index did not come across ({matched} matches), so the restored "
            "instance would answer lexical queries with the LIKE fallback alone"
        )
        target.close()

        # What the restored instance can find. Asserted through the loader rather than
        # by looking for a file next to the copy: nothing in production could write one
        # there, so that assertion could never go red, and a green that cannot fail is
        # not a lock. This one fails the moment calibration state stops being
        # DB-adjacent — a fixed per-user path, or a move into the database itself — and
        # either change makes the operations.md bullet naming
        # `<CPERSONA_DB_PATH>.calibration.json` the wrong thing to hand an operator.
        restored_state = None
        try:
            config.DB_PATH = str(destination)
            restored_state = admin_handlers._load_calibration_state()
        finally:
            config.DB_PATH = live_db_path
        assert restored_state is None, (
            "the restored copy came up already holding calibration state, so "
            "operations.md's warning ('state lives outside it that none of the backup "
            "forms above touch') now overstates the loss and its recovery step — copy "
            "the sidecar, or re-apply set_recall_precision — is advice for a problem "
            f"that no longer exists. Loaded: {restored_state}"
        )
    finally:
        config.DB_PATH = ambient_db_path
        if previous is None:
            if os.path.exists(sidecar):
                os.remove(sidecar)
        else:
            with open(sidecar, "wb") as handle:
                handle.write(previous)
