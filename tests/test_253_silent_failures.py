"""bug-188 / bug-189: two silent failures in the 2.5.3 pre-existing sweep.

Neither raises, neither logs, and both report success while losing data — the
class the sweep exists to close.

- bug-188: an unbounded profile write. Whitespace overwrote a good profile and
  reported it as an update; an oversized profile was stored verbatim and then
  injected into every recall response.
- bug-189: the calibration sidecar was a snapshot of process state, so
  calibrating one agent dropped every other agent's threshold from the file.

The bug-189 follow-up is in the same file because it is the same write: the first
fix carried the stored per-agent maps forward unconditionally, which fixed the
loss and broke the deletions — a cleared precision override came back on the next
restart, and the global axes were still overwritten with env defaults. The write
path now keeps a record of which entries THIS process is authoritative for (it
loaded them, measured them, or removed them) and carries only the rest, so
"absent because deleted" and "absent because unknown" stop looking alike.
"""

import json

import pytest
import pytest_asyncio

from cpersona import session
from cpersona import admin_handlers, config, vector
from cpersona._vendored_mcp_common.embedding_client import EmbeddingClient
from cpersona.config import MAX_PROFILE_LENGTH
from cpersona.database import get_db
from cpersona.utils import SCORING_VERSION

AGENT = "sweep-agent"


@pytest_asyncio.fixture
async def db():
    session.reset_pauses_for_tests()
    conn = await get_db()
    for table in ("memories", "episodes", "profiles"):
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()
    return conn


async def _profile_of(conn, agent_id: str) -> str | None:
    rows = await conn.execute_fetchall(
        "SELECT content FROM profiles WHERE agent_id = ?", (agent_id,)
    )
    return rows[0][0] if rows else None


# ---------------------------------------------------------------------------
# bug-188 — the profile write path is bounded like every other text path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("blank", ["   ", "\n\n", "\t "])
async def test_whitespace_profile_does_not_destroy_the_stored_one(db, blank):
    """The defect in one assertion: this used to blank the profile and say 1."""
    await admin_handlers.do_update_profile(AGENT, "a genuinely useful profile")

    result = await admin_handlers.do_update_profile(AGENT, blank)

    assert result["profiles_updated"] == 0
    assert result.get("reason")
    assert await _profile_of(db, AGENT) == "a genuinely useful profile"


@pytest.mark.asyncio
async def test_empty_profile_says_why_it_did_nothing(db):
    result = await admin_handlers.do_update_profile(AGENT, "")

    assert result == {"ok": True, "profiles_updated": 0, "reason": "empty profile"}
    assert await _profile_of(db, AGENT) is None


@pytest.mark.asyncio
async def test_oversized_profile_is_truncated_and_says_so(db):
    """Unbounded here meant unbounded in every recall response downstream.

    The cap is pinned exactly, and to the LEADING characters. ``<=`` accepted any
    amount of destruction as long as it was destruction: an off-by-a-lot cut
    (``profile[:100]``, throwing away 99.5% of the text) satisfies it, and so does
    a cut that keeps the tail. The paired read-path test
    (``test_253_followups.test_fix_truncates_to_the_cap``) pins ``==``; a write
    path held to a weaker rule than the repair that cleans up after it cannot be
    trusted to be the reason the repair finds nothing.
    """
    result = await admin_handlers.do_update_profile(AGENT, "x" * (MAX_PROFILE_LENGTH + 5_000))

    assert result["profiles_updated"] == 1
    assert result.get("truncated") is True
    stored = await _profile_of(db, AGENT)
    assert len(stored) == MAX_PROFILE_LENGTH
    assert stored == "x" * MAX_PROFILE_LENGTH


@pytest.mark.asyncio
async def test_truncation_keeps_the_start_of_the_profile(db):
    """The cut must drop the tail, not the head — a profile's first line is its point."""
    head = "Prefers terse answers. Works in Rust and Python.\n"
    result = await admin_handlers.do_update_profile(
        AGENT, head + "z" * (MAX_PROFILE_LENGTH + 5_000)
    )

    assert result.get("truncated") is True
    stored = await _profile_of(db, AGENT)
    assert stored.startswith(head)
    assert len(stored) == MAX_PROFILE_LENGTH


@pytest.mark.asyncio
async def test_ordinary_profile_is_stored_unchanged(db):
    """The paired direction — bounding must not mangle a normal write."""
    text = "Prefers terse answers.\n\nWorks in Rust and Python."

    result = await admin_handlers.do_update_profile(AGENT, text)

    assert result["profiles_updated"] == 1
    assert "truncated" not in result
    assert await _profile_of(db, AGENT) == text


# ---------------------------------------------------------------------------
# bug-188, second call site — the import path writes profiles too
#
# The original fix bounded do_update_profile and described the change as putting
# both writers through store's seam. _import_profile_record was not touched, so
# every defect above stayed reachable through import: a whitespace profile in a
# file overwrote a good one and the run reported profile_updated: true. These
# tests are the write-path tests above, aimed at the other door.
# ---------------------------------------------------------------------------


def _import_file(tmp_path, monkeypatch, *records) -> str:
    """Write a JSONL import file inside a confined EXPORT_DIR and return its path."""
    export_root = tmp_path / "confined"
    export_root.mkdir(exist_ok=True)
    monkeypatch.setattr(config, "EXPORT_DIR", str(export_root))
    path = export_root / "import.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return str(path)


def _profile_record(content: str) -> dict:
    return {"_type": "profile", "agent_id": AGENT, "user_id": "", "content": content}


@pytest.mark.asyncio
@pytest.mark.parametrize("blank", ["   ", "\n\n", "\t "])
async def test_whitespace_profile_in_an_import_does_not_destroy_the_stored_one(
    db, tmp_path, monkeypatch, blank
):
    """The defect, through the door the fix did not cover: ok:true, profile gone."""
    await admin_handlers.do_update_profile(AGENT, "a genuinely useful profile")
    path = _import_file(tmp_path, monkeypatch, _profile_record(blank))

    result = await admin_handlers.do_import_memories(path)

    assert result["ok"] is True
    assert result["profile_updated"] is False
    assert await _profile_of(db, AGENT) == "a genuinely useful profile"
    assert any("Line 1" in e and "kept" in e for e in result["errors"]), result.get("errors")


@pytest.mark.asyncio
async def test_oversized_profile_in_an_import_is_truncated_to_the_cap(db, tmp_path, monkeypatch):
    """Pinned to the cap exactly and to the leading characters, like the write path.

    ``<=`` would accept any amount of destruction: a cut to 100 characters, or one
    that keeps the tail, both satisfy it. The import path is where content from a
    foreign DB arrives, so it is the last place to hold to a weaker rule than the
    writer next to it.
    """
    head = "Prefers terse answers.\n"
    path = _import_file(
        tmp_path, monkeypatch, _profile_record(head + "z" * (MAX_PROFILE_LENGTH + 5_000))
    )

    result = await admin_handlers.do_import_memories(path)

    assert result["profile_updated"] is True
    stored = await _profile_of(db, AGENT)
    assert len(stored) == MAX_PROFILE_LENGTH
    assert stored.startswith(head)
    assert any("truncated" in e for e in result["errors"]), result.get("errors")


@pytest.mark.asyncio
async def test_ordinary_profile_import_is_stored_unchanged(db, tmp_path, monkeypatch):
    """The paired direction — bounding must not mangle a legitimate restore."""
    text = "Prefers terse answers.\n\nWorks in Rust and Python."
    path = _import_file(tmp_path, monkeypatch, _profile_record(text))

    result = await admin_handlers.do_import_memories(path)

    assert result["profile_updated"] is True
    assert "errors" not in result
    assert await _profile_of(db, AGENT) == text


@pytest.mark.asyncio
async def test_import_preview_reports_the_same_profile_decision_as_a_real_run(
    db, tmp_path, monkeypatch
):
    """bug-056/070's contract: a preview's counts must equal the run it previews.

    A guard added on the write side only would make dry_run claim an update that
    the real run then refuses — the preview lying in the safe direction is still
    the preview lying.
    """
    await admin_handlers.do_update_profile(AGENT, "a genuinely useful profile")
    path = _import_file(tmp_path, monkeypatch, _profile_record("   "))

    preview = await admin_handlers.do_import_memories(path, dry_run=True)
    real = await admin_handlers.do_import_memories(path)

    assert preview["profile_updated"] == real["profile_updated"] is False
    assert preview["errors"] == real["errors"]
    assert await _profile_of(db, AGENT) == "a genuinely useful profile"


# ---------------------------------------------------------------------------
# bug-189 — calibrating one agent must not delete the others from the sidecar
# ---------------------------------------------------------------------------


def _reset_calibration_process_state():
    """Clear every module-level calibration global, ownership claims included.

    All of it is process-wide, and the claims are the half that is easy to forget:
    an agent left marked as owned by an earlier test makes a later carry drop its
    stored entry, which is silent and looks like the bug under test.
    """
    vector._agent_thresholds.clear()
    vector._agent_fused_gates.clear()
    vector._agent_betas.clear()
    vector._global_fused_gate = None
    vector._fused_gate_signal = None
    vector._reset_calibration_authority()


@pytest.fixture
def sidecar(tmp_path, monkeypatch):
    """Point the sidecar at a temp path and seed it with two agents.

    ``config.VECTOR_MIN_SIMILARITY`` is saved and put back because a restore moves
    it: it is the recall floor of every agent without a per-agent override, so
    leaking a calibrated value out of this module changes what LATER test modules
    retrieve (which is how this fixture earned the two lines).
    """
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "cpersona.db"))
    path = str(tmp_path / "cpersona.db") + ".calibration.json"
    saved_threshold = config.VECTOR_MIN_SIMILARITY
    _reset_calibration_process_state()
    with open(path, "w") as fh:
        json.dump(_seed_payload(), fh)
    yield path
    _reset_calibration_process_state()
    config.VECTOR_MIN_SIMILARITY = saved_threshold


def _seed_payload(**overrides) -> dict:
    """A complete, current-fingerprint sidecar payload."""
    payload = {
        "embedding_dim": 1024,
        "embedding_model": "bge-m3",
        "scoring_version": SCORING_VERSION,
        "global_threshold": 0.55,
        "agent_thresholds": {"alice": 0.61, "bob": 0.58},
        "global_fused_gate": None,
        "agent_fused_gates": {"alice": 0.40, "bob": 0.33},
        "fused_gate_signal": "confidence",
        "agent_betas": {"alice": 1.5},
        "calibrated_at": "2026-07-01T00:00:00+00:00",
    }
    payload.update(overrides)
    return payload


def test_an_unknown_calibration_axis_is_rejected_loudly():
    """A mistyped axis has to raise, because its silent failure is undetectable.

    ``_calibration_authority["agent_thresholdz"]`` would mean "nothing is ever
    owned on that axis", i.e. every deletion carried back forever, with no error
    and no wrong-looking value anywhere near the typo.
    """
    with pytest.raises(KeyError):
        vector._claim_agent_calibration("alice", "agent_thresholdz")
    with pytest.raises(KeyError):
        vector._claim_global_calibration("globl_threshold")


def test_stored_entries_are_carried_forward(sidecar):
    carried = admin_handlers._stored_agent_maps_to_carry(1024)

    assert carried["agent_thresholds"] == {"alice": 0.61, "bob": 0.58}
    assert carried["agent_fused_gates"] == {"alice": 0.40, "bob": 0.33}
    assert carried["agent_betas"] == {"alice": 1.5}


def test_an_entry_this_process_owns_is_not_carried(sidecar):
    """Ownership is what lets an absence mean "deleted" instead of "never seen".

    The version of this test that shipped with the fix asserted a dict literal it
    built itself (``{**carried, **{"alice": 0.70}}``) and never called the
    implementation, so it verified a property of Python rather than of CPersona and
    passed against any carry policy whatsoever, the blanket carry that resurrects
    deletions included. This calls the helper and pins the rule the write path
    actually needs: the axis this process owns loses its stored entry, the axes it
    does not own keep theirs — the per-axis split ``ensure_calibrated_on_startup``
    depends on when it preloads betas without their thresholds (bug-184).
    """
    vector._claim_agent_calibration("alice", "agent_betas")

    carried = admin_handlers._stored_agent_maps_to_carry(1024)

    assert carried["agent_betas"] == {}, "alice's beta is ours; its absence is a deletion"
    assert carried["agent_thresholds"] == {"alice": 0.61, "bob": 0.58}
    assert carried["agent_fused_gates"] == {"alice": 0.40, "bob": 0.33}


def test_a_restore_makes_this_process_the_owner(sidecar):
    """The other half of the ownership rule: loading counts, not just writing.

    A process that restored the sidecar has read all of it, so its dicts hold
    everything the file holds and carrying anything back is at best a no-op — and
    at worst the thing that resurrects an entry removed later in the same process.
    Ownership by loading is what makes a removal durable regardless of WHICH code
    removed it; without it, only removals that remember to claim for themselves
    survive a restart, which is a property nobody can see from the removing code.
    """
    admin_handlers._restore_calibration_state(admin_handlers._load_calibration_state())

    carried = admin_handlers._stored_calibration_to_carry(1024)

    assert carried == {"agent_thresholds": {}, "agent_fused_gates": {}, "agent_betas": {}}, (
        "a restored process carried stored entries it already holds in memory"
    )


def test_stored_globals_are_carried_forward(sidecar):
    """The global axes are the same loss, one scope wider.

    They live in ``config`` / ``vector`` module state, so a process that never
    restored holds the ENV DEFAULTS for them — and writes those over the measured
    values the moment it calibrates a single agent. ``_get_vector_threshold``
    falls back to ``config.VECTOR_MIN_SIMILARITY`` for every agent without an
    override, so this one reaches further than any per-agent entry.
    """
    carried = admin_handlers._stored_calibration_to_carry(1024)

    assert carried["global_threshold"] == 0.55
    assert carried["fused_gate_signal"] == "confidence"
    assert carried["global_fused_gate"] is None
    assert "global_fused_gate" in carried, "a stored None is a value, not an absent key"


def test_a_global_axis_this_process_owns_is_not_carried(sidecar):
    vector._claim_global_calibration("global_threshold")

    carried = admin_handlers._stored_calibration_to_carry(1024)

    assert "global_threshold" not in carried, "measured here; the caller's live value wins"
    assert carried["fused_gate_signal"] == "confidence"


@pytest.mark.parametrize(
    "field, value",
    [("embedding_dim", 768), ("scoring_version", "some-older-scoring")],
)
def test_stale_measurements_are_not_carried(sidecar, field, value):
    """bug-184 declines to RESTORE these; the write path must not smuggle them back.

    A dimension change means different vectors, a scoring change means a
    different quantity. Either way the stored numbers describe something the
    runtime no longer produces, and carrying them forward would launder them
    into a sidecar that looks freshly measured.
    """
    state = json.load(open(sidecar))
    state[field] = value
    with open(sidecar, "w") as fh:
        json.dump(state, fh)

    assert admin_handlers._stored_agent_maps_to_carry(1024) == {}
    assert admin_handlers._stored_calibration_to_carry(1024) == {}, (
        "the global axes are measurements too — a stale global_threshold carried "
        "into a fresh-looking sidecar is the same laundering, one scope wider"
    )


def test_no_sidecar_yet_carries_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "fresh.db"))

    assert admin_handlers._stored_agent_maps_to_carry(1024) == {}


def test_purge_still_removes_an_agent(sidecar):
    """The merge must not resurrect what a purge deliberately dropped.

    _purge_agent_calibration rewrites the payload from the file it just read, and
    it must keep doing exactly that — carrying stored entries in there would make
    dropping an agent impossible.
    """
    import inspect

    source = inspect.getsource(admin_handlers._purge_agent_calibration)

    assert "_stored_agent_maps_to_carry" not in source


# ---------------------------------------------------------------------------
# bug-189 follow-up — the same write, end to end through the real tools
#
# Everything above tests the carry helper in isolation. These drive
# set_recall_precision / calibrate_threshold against a real corpus and read the
# file that survives a restart, because the defect and its regression both live
# in the seam BETWEEN the helper and the call site: the first fix's helper was
# correct and its call site turned every deletion into a no-op.
# ---------------------------------------------------------------------------


async def _seed_embeddings(db, agent_id: str, count: int = 15, dim: int = 8) -> None:
    """Insert *count* rows with deterministic, non-degenerate embeddings.

    Mirrors ``test_threshold_calibration._seed_embeddings``: calibration needs a
    similarity distribution with a real spread, not a corpus of near-duplicates.
    """
    for i in range(count):
        vec = [float((i + j) % 5) - 2.0 for j in range(dim)]
        await db.execute(
            "INSERT INTO memories (agent_id, content, timestamp, embedding) VALUES (?, ?, ?, ?)",
            (agent_id, f"memory {i}", "2026-05-14T00:00:00Z", EmbeddingClient.pack_embedding(vec)),
        )
    await db.commit()


@pytest_asyncio.fixture
async def live(db, tmp_path, monkeypatch):
    """A process holding NO calibration state, with a sidecar of its own.

    That is the bug-189 starting condition: the startup restore did not run, so
    every calibration global is the env default and the file on disk is the only
    place the real numbers exist.
    """
    path = str(tmp_path / "live.calibration.json")
    monkeypatch.setattr(admin_handlers, "_calibration_sidecar_path", lambda: path)
    saved_threshold = config.VECTOR_MIN_SIMILARITY
    config.VECTOR_MIN_SIMILARITY = 0.3
    _reset_calibration_process_state()
    yield path
    _reset_calibration_process_state()
    config.VECTOR_MIN_SIMILARITY = saved_threshold


def _write_sidecar(path: str, *, embedding_dim: int = 8, **overrides) -> None:
    with open(path, "w") as fh:
        json.dump(_seed_payload(embedding_dim=embedding_dim, **overrides), fh)


def _stored(path: str) -> dict:
    with open(path) as fh:
        return json.load(fh)


@pytest.mark.asyncio
async def test_clearing_a_precision_override_removes_it_from_the_sidecar(db, live):
    """``set_recall_precision(agent, "")`` answered ``cleared: true`` and did not clear.

    The clear is expressed by popping the beta out of ``vector._agent_betas`` and
    letting the recalibration persist process state; a carry that overlays the
    stored beta back on top of that hole makes the tool a no-op with a success
    response, and the override returns at the next restart with nothing logged.
    Restored state here, which is the ordinary production shape.
    """
    await _seed_embeddings(db, "cleared-agent")
    _write_sidecar(live, agent_betas={"cleared-agent": 2.0, "bystander": 1.5})
    admin_handlers._restore_calibration_state(admin_handlers._load_calibration_state())

    result = await admin_handlers.do_set_recall_precision("cleared-agent")

    assert result["ok"] is True and result["cleared"] is True
    assert vector._agent_betas.get("cleared-agent") is None
    assert _stored(live)["agent_betas"] == {"bystander": 1.5}, (
        "the cleared override is back in the file; the next restart restores it and "
        "the agent keeps recalling at a precision the operator switched off"
    )


@pytest.mark.asyncio
async def test_a_process_that_set_an_override_can_also_clear_it(db, live):
    """Set then clear, with no restore — the two directions in one process.

    The set must NOT drop the bystander (that is bug-189) and the clear must NOT
    keep the agent (that is what the first fix broke). One carry policy cannot
    satisfy both; only knowing which entries this process owns can.
    """
    await _seed_embeddings(db, "sole-agent")
    _write_sidecar(live, agent_betas={"bystander": 1.5})

    assert (await admin_handlers.do_set_recall_precision("sole-agent", "strict"))["ok"] is True
    assert _stored(live)["agent_betas"] == {"sole-agent": 2.0, "bystander": 1.5}

    assert (await admin_handlers.do_set_recall_precision("sole-agent"))["cleared"] is True
    assert _stored(live)["agent_betas"] == {"bystander": 1.5}


@pytest.mark.asyncio
async def test_a_failed_precision_set_gives_the_ownership_back(db, live):
    """A call that was told it failed must not have taken ownership on the way out.

    ``set_recall_precision`` claims the beta axis before recalibrating, because the
    recalibration is what persists the change. When the recalibration cannot run
    (here: an agent with no embeddings at all) the override is rolled back — and
    the claim has to roll back with it. Left behind, it teaches the NEXT
    calibration that this process's silence about the agent is a deletion, so a
    stored override disappears from the file because an unrelated call failed.
    """
    await _seed_embeddings(db, "measured")
    _write_sidecar(live, agent_betas={"tiny": 1.5})

    failed = await admin_handlers.do_set_recall_precision("tiny", "strict")
    assert failed["ok"] is False
    assert "tiny" not in vector._agent_betas  # rolled back (bug-096/149)

    assert (await admin_handlers.do_calibrate_threshold("measured"))["ok"] is True

    assert _stored(live)["agent_betas"] == {"tiny": 1.5}, (
        "a failed set_recall_precision deleted a stored override it never applied"
    )


@pytest.mark.asyncio
async def test_calibrating_one_agent_keeps_every_other_agents_numbers(db, live):
    """bug-189 itself, through the tool: one agent's calibration is not a file rewrite.

    Also pins the per-axis split. The calibrated agent's THRESHOLD is replaced by
    the fresh measurement, but its gate (not measurable without an embedding
    backend) and its beta (policy this call never touched) keep their stored
    values — an ownership claim that covered the whole agent instead of the axis
    would delete both and call it a calibration.
    """
    await _seed_embeddings(db, "measured")
    _write_sidecar(
        live,
        agent_thresholds={"measured": 0.61, "untouched": 0.58},
        agent_fused_gates={"measured": 0.40, "untouched": 0.33},
        agent_betas={"measured": 1.5, "untouched": 2.0},
    )

    result = await admin_handlers.do_calibrate_threshold("measured")
    assert result["ok"] is True

    state = _stored(live)
    assert state["agent_thresholds"]["untouched"] == 0.58, (
        "an agent nobody touched lost its threshold and now falls back to the "
        "global default — silently wrong recall breadth, no error anywhere"
    )
    assert state["agent_fused_gates"]["untouched"] == 0.33
    assert state["agent_betas"]["untouched"] == 2.0
    assert state["agent_thresholds"]["measured"] == result["new_threshold"]
    assert state["agent_fused_gates"]["measured"] == 0.40
    assert state["agent_betas"]["measured"] == 1.5


@pytest.mark.asyncio
async def test_a_per_agent_calibration_keeps_the_stored_global_values(db, live):
    """The half of bug-189 the first fix left open, and the wider half.

    Calibrating ONE agent in a process that never restored used to write that
    process's env defaults over the global threshold, the global fused gate and
    the gate signal. Nothing measures a global fused gate at all, so for that one
    the loss is permanent.
    """
    await _seed_embeddings(db, "measured")
    _write_sidecar(live, global_threshold=0.55, global_fused_gate=0.31)
    assert config.VECTOR_MIN_SIMILARITY != 0.55, "the process must be holding the default"

    assert (await admin_handlers.do_calibrate_threshold("measured"))["ok"] is True

    state = _stored(live)
    assert state["global_threshold"] == 0.55, (
        "every agent without a per-agent override recalls at the global threshold, "
        "so this one reset the recall floor of the whole deployment"
    )
    assert state["global_fused_gate"] == 0.31
    assert state["fused_gate_signal"] == "confidence"


@pytest.mark.asyncio
async def test_a_global_calibration_replaces_the_stored_global_threshold(db, live):
    """The paired direction: carrying must not outrank a measurement.

    A carry that always wins would be just as broken in the other direction —
    ``calibrate_threshold(agent_id="")`` would compute a new global threshold,
    report it, and persist the old one.
    """
    await _seed_embeddings(db, "measured")
    _write_sidecar(live, global_threshold=0.55)

    result = await admin_handlers.do_calibrate_threshold("")
    assert result["ok"] is True
    assert result["new_threshold"] != 0.55, "fixture drift: the measurement must differ"

    assert _stored(live)["global_threshold"] == result["new_threshold"]


@pytest.mark.asyncio
async def test_a_stale_sidecar_launders_nothing_through_the_write_path(db, live):
    """bug-184's refusal covers the global axes too (see the helper test above).

    A stale payload is not carried at all, so the freshly measured live values are
    written and the sidecar stops claiming numbers measured on a scoring function
    the runtime no longer runs.
    """
    await _seed_embeddings(db, "measured")
    _write_sidecar(
        live,
        scoring_version="some-older-scoring",
        global_threshold=0.99,
        global_fused_gate=0.9,
    )

    assert (await admin_handlers.do_calibrate_threshold("measured"))["ok"] is True

    state = _stored(live)
    assert state["global_threshold"] == config.VECTOR_MIN_SIMILARITY != 0.99
    assert state["global_fused_gate"] is None
    assert state["scoring_version"] == SCORING_VERSION


async def _seed_recallable(db, agent_id: str) -> None:
    """Insert rows the recall pipeline can actually rank, in two time clusters.

    The fused gate is calibrated by SIMULATED queries, so these rows have to be
    reachable through the real retrieval path: conftest's embedding space (so the
    vector channel ranks them) and real content (so the FTS triggers index them).
    Two clusters 12 hours apart give the calibration both populations it needs —
    same-session neighbours as the positive proxy, the far cluster as the null.
    """
    from conftest import fake_embed_one

    rows = [(f"apples pears cluster one memory {i}", f"2026-05-14T00:{i:02d}:00Z") for i in range(12)]
    rows += [(f"engines pistons cluster two memory {i}", f"2026-05-14T12:{i:02d}:00Z") for i in range(12)]
    for content, ts in rows:
        await db.execute(
            "INSERT INTO memories (agent_id, content, timestamp, embedding) VALUES (?, ?, ?, ?)",
            (agent_id, content, ts, EmbeddingClient.pack_embedding(fake_embed_one(content))),
        )
    await db.commit()


@pytest.mark.asyncio
async def test_a_measured_gate_owns_the_signal_it_was_measured_for(
    db, live, fake_embedding_client, monkeypatch
):
    """The gate and the signal it was measured on must be persisted together.

    ``fused_gate_signal`` records WHICH score branch the stored gate belongs to,
    and ``_apply_quality_gate`` only applies a gate whose signal matches the live
    one. Carrying the stored signal over a freshly measured gate therefore does
    not just write a stale label: it writes a gate that the next boot restores and
    then never uses, so the calibration silently degrades to the pool-size
    heuristic while the sidecar looks fully calibrated.

    This is also the only test in the suite that reaches the fused-gate success
    branch at all — everything else runs without an embedding client, where
    ``_calibrate_fused_gate`` returns None before measuring anything.
    """
    from cpersona import memory_handlers

    # The gate is calibrated on the branch the runtime gate compares. Confidence is
    # the branch that scores EVERY row, so it is the one that produces a curve from
    # an ordinary corpus. Both spellings are patched on purpose: memory_handlers
    # binds the flag by value at import, admin_handlers reads it off the module.
    monkeypatch.setattr(config, "CONFIDENCE_ENABLED", True)
    monkeypatch.setattr(memory_handlers, "CONFIDENCE_ENABLED", True)
    await _seed_recallable(db, "gated")
    _write_sidecar(
        live,
        embedding_dim=64,
        agent_fused_gates={"gated": 0.40, "untouched": 0.33},
        fused_gate_signal="rrf",
    )

    result = await admin_handlers.do_calibrate_threshold("gated")
    assert result["ok"] is True
    assert result.get("fused_gate"), "the gate branch did not run; this test proves nothing"

    state = _stored(live)
    assert state["fused_gate_signal"] == result["fused_gate"]["signal"], (
        "the sidecar kept the stored signal, so the measured gate is now filed under a "
        "branch it was not measured on — restored on the next boot and never applied"
    )
    assert state["fused_gate_signal"] != "rrf", "fixture drift: the stored signal must differ"
    assert state["agent_fused_gates"]["gated"] == result["fused_gate"]["threshold"]
    assert state["agent_fused_gates"]["untouched"] == 0.33, "the carry still protects the rest"

    carried = admin_handlers._stored_agent_maps_to_carry(64)
    assert carried["agent_fused_gates"] == {"untouched": 0.33}, (
        "the measured gate is this process's to delete; it must not be carried back"
    )


@pytest.mark.asyncio
async def test_an_auto_calibrate_boot_owns_the_betas_it_preloaded(db, live):
    """The boot that loads HALF the sidecar must own exactly that half.

    ``AUTO_CALIBRATE`` skips the restore, so ``ensure_calibrated_on_startup``
    preloads ``agent_betas`` on their own (bug-184: betas are policy inputs, the
    thresholds and gates are the stale measurements it is about to replace).
    Ownership has to follow that split. Claiming nothing leaves the process unable
    to express a later removal of a preloaded beta; claiming the whole agent
    deletes stored thresholds this boot deliberately never looked at.
    """
    await _seed_embeddings(db, "booted")
    _write_sidecar(
        live,
        agent_thresholds={"booted": 0.61, "unrelated": 0.58},
        agent_betas={"booted": 2.0, "unrelated": 1.5},
    )

    status = await admin_handlers.ensure_calibrated_on_startup(
        auto_calibrate=True, on_model_change=False
    )
    assert status["action"] == "auto", status

    carried = admin_handlers._stored_agent_maps_to_carry(8)
    assert carried["agent_betas"] == {}, "the boot holds every stored beta; none is unknown"
    assert carried["agent_thresholds"] == {"unrelated": 0.58}, (
        "the boot preloaded no thresholds, so an agent it never recalibrated is still "
        "only known to the file"
    )


@pytest.mark.asyncio
async def test_a_purge_whose_rewrite_failed_is_not_undone_by_the_next_calibration(
    db, live, monkeypatch
):
    """A purge that could not write still counts as a deletion.

    ``_purge_agent_calibration`` drops the agent from process state and then
    rewrites the file; the rewrite can fail (bug-095 made that visible rather than
    fatal), leaving the agent on disk. The next calibration must not read it back
    out and restore calibration for a corpus that no longer exists — which is
    exactly the bug-036 the purge exists to prevent.
    """
    await _seed_embeddings(db, "measured")
    _write_sidecar(live, agent_thresholds={"doomed": 0.61, "keeper": 0.58})
    real_save = admin_handlers._save_calibration_state
    monkeypatch.setattr(admin_handlers, "_save_calibration_state", lambda *a, **k: False)

    assert admin_handlers._purge_agent_calibration("doomed") is True
    assert "doomed" in _stored(live)["agent_thresholds"], "the failed write is the premise"

    monkeypatch.setattr(admin_handlers, "_save_calibration_state", real_save)
    assert (await admin_handlers.do_calibrate_threshold("measured"))["ok"] is True

    state = _stored(live)
    assert "doomed" not in state["agent_thresholds"]
    assert state["agent_thresholds"]["keeper"] == 0.58
