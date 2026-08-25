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
