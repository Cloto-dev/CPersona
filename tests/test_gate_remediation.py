"""Regression tests for the 2.5.2 gate-remediation pair (bug-183, bug-184).

Both defects are consequences of the b2 cosine backfill (bug-155), from opposite
sides of the quality gate:

- bug-184 (calibration side): the gate's operating point is measured ON a score
  distribution. The sidecar fingerprinted the embedding but not the SCORING
  function, so a threshold calibrated before the backfill was restored verbatim
  afterwards and then gated a quantity it was never measured against.
  ``utils.SCORING_VERSION`` closes that, and ``ensure_calibrated_on_startup``
  treats a mismatch (absence included — that IS the pre-b2 sidecar) exactly like
  an embedding-dimension change.

- bug-183 (recall side): the backfill moved FTS/keyword-only rows off
  ``_compute_confidence``'s cosine-less branch, which was an upper bound on the
  cosine branch and therefore a guaranteed gate pass. A query whose every hit is
  lexical-but-semantically-distant then returns NOTHING where it used to return
  the exact match, and the caller cannot tell that from "no such memory".

The bug-183 tests deliberately pin the fix's two boundaries as well as the fix:

- a row that carries a NATIVE cosine is NOT rescued (its verdict is not one the
  backfill changed — widening the rescue to the whole pre-gate list turns a
  nonsense query into "here is your nearest below-gate neighbour", which
  ``test_audit_2500b3.test_empty_query_recall_bypasses_unscored_volume_gate``
  and the ``recall-no-hits`` golden both refuse);
- a MIXED result keeps the drop. That is designed behaviour pending the 2.6.0
  membership-preserving scoring redesign, so a later change that starts demoting
  weak rows into every ranked set fails a test instead of sliding in unremarked.

Fixture style follows the closest sibling, ``test_bug155_cosine_backfill.py``:
memories are inserted directly (which fires the FTS triggers exactly as do_store
does) because an FTS-only row requires a stored blob that is DISJOINT from its
own content — a property ``do_store`` cannot produce, since it embeds the
content it stores. The embedding is conftest's, so these rows sit in the same
vector space as the rest of the suite.
"""

from __future__ import annotations

import json
import os

import pytest
import pytest_asyncio

from conftest import fake_embed_one

from cpersona import admin_handlers, checks, config, memory_handlers as M, vector
from cpersona._vendored_mcp_common.embedding_client import EmbeddingClient
from cpersona.database import get_db
from cpersona.utils import SCORING_VERSION

AGENT = "agent.gate-remediation"


def _pack_of(text: str) -> bytes:
    return EmbeddingClient.pack_embedding(fake_embed_one(text))


def _reset_calibration_globals() -> None:
    """Clear every module-level calibration global.

    They are process-wide, and the recall assertions below depend on the gate
    being the pool-size heuristic rather than a calibrated operating point some
    earlier test module left behind (the same leak
    ``test_threshold_calibration._reset_module_state`` guards against).
    """
    vector._agent_thresholds.clear()
    vector._agent_fused_gates.clear()
    vector._global_fused_gate = None
    vector._fused_gate_signal = None
    vector._agent_betas.clear()
    config.VECTOR_MIN_SIMILARITY = 0.3


@pytest_asyncio.fixture(autouse=True)
async def _fresh_state(tmp_path, monkeypatch):
    """Truncate the tables this file writes and redirect the calibration sidecar.

    ``get_db`` is a process-wide singleton and the sidecar path is derived from
    the shared DB_PATH, so both would otherwise leak across modules.
    """
    db = await get_db()
    for table in ("memories", "episodes", "profiles"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()
    _reset_calibration_globals()
    monkeypatch.setattr(
        admin_handlers,
        "_calibration_sidecar_path",
        lambda: os.path.join(str(tmp_path), "sidecar.calibration.json"),
    )
    yield
    _reset_calibration_globals()


# ===========================================================================
# bug-184 — the scoring fingerprint
# ===========================================================================


async def _seed_embeddings(db, agent_id: str, count: int, dim: int = 8) -> None:
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


def _write_sidecar(payload: dict) -> None:
    """Write a sidecar by hand — the only way to produce a payload the current
    ``_save_calibration_state`` cannot emit (i.e. the pre-b2 upgrade case)."""
    with open(admin_handlers._calibration_sidecar_path(), "w") as fh:
        json.dump(payload, fh)


@pytest.mark.asyncio
async def test_calibration_stamps_the_scoring_version_and_still_restores():
    """A real calibration records the runtime scoring fingerprint, and a sidecar
    carrying it restores unchanged — the guard must not have become a permanent
    recalibrate-on-every-boot.
    """
    db = await get_db()
    await _seed_embeddings(db, "agent-stamp", 15, dim=8)

    result = await admin_handlers.do_calibrate_threshold("agent-stamp")
    assert result["ok"] is True

    state = admin_handlers._load_calibration_state()
    assert state is not None
    assert state["scoring_version"] == SCORING_VERSION, (
        f"the sidecar carries {state.get('scoring_version')!r}; the gate it holds was "
        f"measured on {SCORING_VERSION!r} and nothing records that without this key"
    )

    # Re-write it with a complete post-v2.4.27 record (a signal-less sidecar would
    # correctly fall through to the gate-missing branch, which is a different test).
    admin_handlers._save_calibration_state(
        embedding_dim=8,
        embedding_model="bge-m3",
        global_threshold=0.61,
        agent_thresholds={"agent-stamp": 0.58},
        global_fused_gate=0.45,
        fused_gate_signal="confidence",
    )
    status = await admin_handlers.ensure_calibrated_on_startup(
        auto_calibrate=False, on_model_change=True
    )
    assert status["action"] == "restored", status
    assert vector._get_vector_threshold("agent-stamp") == 0.58
    assert config.VECTOR_MIN_SIMILARITY == 0.61


@pytest.mark.asyncio
async def test_startup_guard_forces_recalibration_on_a_pre_b2_sidecar():
    """A sidecar with NO scoring_version is exactly what an upgrade from any
    pre-2.5.2b2 release leaves on disk. It must NOT be restored: its thresholds
    were measured on the cosine-less branch the backfill removed.

    Unfixed, the guard sees a matching embedding_dim and returns
    ``action='restored'`` with the stale 0.99 threshold live — silently
    over-filtering every subsequent recall. AUTO_CALIBRATE is off here, so a
    guard that only recalibrates for auto-calibrating deployments also fails.
    """
    db = await get_db()
    await _seed_embeddings(db, "agent-preb2", 15, dim=8)
    _write_sidecar(
        {
            "embedding_dim": 8,  # unchanged — dim_changed must NOT be what fires
            "embedding_model": "bge-m3",
            "global_threshold": 0.99,
            "agent_thresholds": {"agent-preb2": 0.99},
            "global_fused_gate": 0.9,
            "agent_fused_gates": {},
            "fused_gate_signal": "confidence",
            "agent_betas": {},
            "calibrated_at": "2026-07-01T00:00:00+00:00",
        }
    )

    status = await admin_handlers.ensure_calibrated_on_startup(
        auto_calibrate=False, on_model_change=True
    )

    assert status["action"] == "recalibrated_scoring", (
        f"expected the scoring-staleness trigger; got {status!r}. 'restored' means the "
        f"pre-b2 gate is live against the post-backfill score distribution."
    )
    assert status["scoring_stale"] is True
    assert status["dim_changed"] is False, "the dimension is unchanged — the trigger must be the scoring version"
    assert vector._get_vector_threshold("agent-preb2") != 0.99, (
        "the stale threshold survived; the restore was not skipped"
    )
    assert admin_handlers._load_calibration_state()["scoring_version"] == SCORING_VERSION, (
        "the recalibration did not re-stamp the sidecar, so the next boot repeats this"
    )


@pytest.mark.parametrize(
    "sidecar_scoring_version,auto_calibrate,on_model_change,expected_action",
    [
        # The upgrade boot: a pre-b2 sidecar (no fingerprint) forces recalibration.
        (None, False, True, "recalibrated_scoring"),
        # A perfectly current sidecar, skipped only because AUTO_CALIBRATE is on. Same
        # seam, same wipe — and this one fires on EVERY boot, not just after an upgrade.
        (SCORING_VERSION, True, False, "auto"),
    ],
    ids=["scoring-stale-upgrade-boot", "auto-calibrate-on"],
)
@pytest.mark.asyncio
async def test_forced_recalibration_preserves_the_operator_precision_override(
    sidecar_scoring_version, auto_calibrate, on_model_change, expected_action
):
    """Skipping the restore must not take the operator's knob-3 beta with it.

    ``_restore_calibration_state`` is what normally loads ``agent_betas``. On every path
    that recalibrates WITHOUT restoring, ``vector._agent_betas`` is empty while the
    recalibration runs, so the gate is re-measured at the DEFAULT beta and the empty dict
    is then persisted — the operator's setting is gone from disk after one boot, and
    nothing reports it. Betas are policy INPUTS the measurement consumes, not
    measurements; only thresholds and gates are invalidated by a scoring change.

    Parametrised over the two ways to reach that state deliberately: the invariant is
    "nothing was restored, so reload the policy", and an invariant that held for one
    trigger and not the other would be a coin flip on an unrelated config flag.
    """
    db = await get_db()
    await _seed_embeddings(db, "agent-beta", 15, dim=8)
    sidecar = {
        "embedding_dim": 8,
        "embedding_model": "bge-m3",
        "global_threshold": 0.99,
        "agent_thresholds": {"agent-beta": 0.99},
        "global_fused_gate": 0.9,
        "agent_fused_gates": {},
        "fused_gate_signal": "confidence",
        "agent_betas": {"agent-beta": 2.0},  # a deliberate operator choice
        "calibrated_at": "2026-07-01T00:00:00+00:00",
    }
    if sidecar_scoring_version is not None:
        sidecar["scoring_version"] = sidecar_scoring_version
    _write_sidecar(sidecar)

    status = await admin_handlers.ensure_calibrated_on_startup(
        auto_calibrate=auto_calibrate, on_model_change=on_model_change
    )
    assert status["action"] == expected_action, status

    assert vector._agent_betas.get("agent-beta") == 2.0, (
        f"the operator's precision override was dropped in-process: {vector._agent_betas!r} "
        f"— the gate was then re-measured at the default beta"
    )
    assert admin_handlers._load_calibration_state()["agent_betas"] == {"agent-beta": 2.0}, (
        "the recalibration persisted an empty agent_betas: the setting is now gone from "
        "disk, permanently, after a single upgrade boot"
    )
    # The measurements it replaces must NOT have been preloaded — that staleness is the
    # whole point of bug-184.
    assert vector._get_vector_threshold("agent-beta") != 0.99


@pytest.mark.asyncio
async def test_dropping_an_agent_does_not_launder_a_stale_sidecar():
    """``_purge_agent_calibration`` REWRITES the sidecar (bug-036) without
    measuring anything. If that rewrite stamped the live fingerprint, deleting
    any agent would make a stale sidecar look freshly calibrated and the startup
    guard would restore it — the fingerprint would be defeated by an unrelated
    tool call.
    """
    _write_sidecar(
        {
            "embedding_dim": 8,
            "embedding_model": "bge-m3",
            "global_threshold": 0.99,
            "agent_thresholds": {"agent-doomed": 0.99, "agent-keeper": 0.7},
            "global_fused_gate": 0.9,
            "agent_fused_gates": {},
            "fused_gate_signal": "confidence",
            "agent_betas": {},
            "calibrated_at": "2026-07-01T00:00:00+00:00",
        }
    )

    assert admin_handlers._purge_agent_calibration("agent-doomed") is True

    state = admin_handlers._load_calibration_state()
    assert "agent-doomed" not in state["agent_thresholds"], "the purge itself regressed"
    assert state.get("scoring_version") is None, (
        f"the purge stamped {state.get('scoring_version')!r} onto thresholds it did not "
        f"re-measure; the sidecar now claims a fingerprint it did not earn"
    )


@pytest.mark.asyncio
async def test_deep_check_reports_a_scoring_version_mismatch(fake_embedding_client):
    """``deep_calibration_staleness`` covers the case startup could not repair
    (no embedding client at boot, or a calibration that returned ok=False).

    ``calibrated_at`` is deliberately fresh: age answers a different question, so
    the unfixed check reports ``ok`` for a sidecar that is stale in the way that
    actually matters.
    """
    from datetime import datetime, timezone

    db = await get_db()
    await _seed_embeddings(db, AGENT, 12, dim=8)
    _write_sidecar(
        {
            "embedding_dim": 8,
            "embedding_model": "bge-m3",
            "global_threshold": 0.5,
            "agent_thresholds": {},
            "fused_gate_signal": "confidence",
            "scoring_version": "251-pre-backfill",
            "calibrated_at": datetime.now(timezone.utc).isoformat(),
        }
    )

    result = await checks.deep_calibration_staleness(db, AGENT, fix=False)

    assert result["status"] == "stale_scoring_version", (
        f"got {result!r} — a fresh calibrated_at makes the age check report 'ok' for a "
        f"gate measured on a scoring function this build no longer runs"
    )
    assert result["sidecar_scoring_version"] == "251-pre-backfill"
    assert result["runtime_scoring_version"] == SCORING_VERSION
    # bug-189: the hint must NOT send the operator to calibrate_threshold. This status
    # fires exactly when nothing was restored, and a single-agent calibration then
    # rewrites the whole sidecar from empty in-memory state, dropping every other agent's
    # thresholds and gates. A restart runs the startup guard, which does all agents.
    assert result["hint"] == "restart the server (it recalibrates automatically on boot)"
    assert "run calibrate_threshold" not in result["hint"]


@pytest.mark.asyncio
async def test_scoring_mismatch_outranks_the_age_check(fake_embedding_client):
    """A sidecar can be BOTH old and scoring-stale. The scoring verdict must win:
    'stale' says "re-tune sometime", 'stale_scoring_version' says "the gate is
    measuring the wrong quantity right now" — and only the latter names the two
    versions the operator needs to see. Ordering the age test first would mask it.
    """
    db = await get_db()
    await _seed_embeddings(db, AGENT, 12, dim=8)
    _write_sidecar(
        {
            "embedding_dim": 8,
            "embedding_model": "bge-m3",
            "global_threshold": 0.5,
            "agent_thresholds": {},
            "fused_gate_signal": "confidence",
            "scoring_version": "251-pre-backfill",
            # Older than CALIBRATION_STALE_DAYS (90), so the age branch would also fire.
            "calibrated_at": "2025-01-01T00:00:00+00:00",
        }
    )

    result = await checks.deep_calibration_staleness(db, AGENT, fix=False)

    assert result["status"] == "stale_scoring_version", (
        f"got {result!r} — with the scoring check placed after the age test, a sidecar "
        f"that is both old and scoring-stale reports only 'stale', which hides which "
        f"defect is live and drops both version strings"
    )


# ===========================================================================
# bug-183 — the empty-after-gate rescue
# ===========================================================================


async def _insert_mem(content: str, blob: bytes | None, ts: str = "2026-01-01T00:00:00Z") -> int:
    """Insert one memory. Direct INSERT (not do_store) because an FTS-only row
    needs a blob DISJOINT from its own content; the FTS triggers fire either way.
    See test_bug155_cosine_backfill for the same construction."""
    db = await get_db()
    cur = await db.execute(
        "INSERT INTO memories (agent_id, channel, content, source, timestamp, embedding, created_at) "
        "VALUES (?, '', ?, '{}', ?, ?, ?)",
        (AGENT, content, ts, blob, ts),
    )
    await db.commit()
    return cur.lastrowid


@pytest.mark.asyncio
async def test_gate_emptied_by_the_backfill_falls_back_and_says_so(
    monkeypatch, fake_embedding_client
):
    """Every non-profile hit is lexical-but-distant: the row matches the query in
    FTS, its stored blob is unrelated, so the backfill gives it a near-zero
    cosine and the gate drops it — leaving the caller an empty result that is
    indistinguishable from "no such memory".

    Unfixed: ``messages == []``. Fixed: the row comes back, flagged.
    """
    monkeypatch.setattr(M, "CONFIDENCE_ENABLED", True)
    await _insert_mem(
        content="apples zzz yyy www",
        blob=_pack_of("completely different unrelated content xxxx"),
    )

    # deep=True fixes time_decay / completion / recency at 1.0 so the assertion is
    # about the gate and not about the corpus's age arithmetic.
    out = await M.do_recall(AGENT, "apples", limit=5, deep=True)

    assert [m["content"] for m in out["messages"]] == ["apples zzz yyy www"], (
        f"the only lexical match was gated away and the caller got {out!r} — the empty "
        f"result bug-183 describes"
    )
    assert out.get("gate_fallback") is True, (
        f"the rows were returned without disclosing that they are below the gate: {out!r}"
    )


@pytest.mark.asyncio
async def test_gate_fallback_is_absent_on_an_ordinary_recall(monkeypatch, fake_embedding_client):
    """The flag is ABSENT (not False) whenever the rescue did not fire — every
    recorded golden and every existing consumer depends on the response shape
    being untouched in the common case.
    """
    monkeypatch.setattr(M, "CONFIDENCE_ENABLED", True)
    await _insert_mem(content="apples orchard hit", blob=_pack_of("apples orchard hit"))

    out = await M.do_recall(AGENT, "apples", limit=5, deep=True)

    assert out["messages"], "fixture regression: the strong vector hit did not survive"
    assert "gate_fallback" not in out, (
        f"gate_fallback leaked onto an ordinary recall: {out!r}"
    )


@pytest.mark.asyncio
async def test_mixed_result_keeps_the_drop_and_stays_unflagged(monkeypatch, fake_embedding_client):
    """DESIGNED BEHAVIOUR, pinned on purpose (bug-183 fix_note): with one row
    above the gate and one lexical-only row below it, the weak row is DROPPED,
    not demoted, and nothing is flagged.

    Demoting instead would readmit weak lexical rows to every ranked set — the
    inversion bug-155's backfill exists to close — and would perturb the b2 soak
    on every query. The membership-preserving redesign is 2.6.0 work; until then
    this assertion is what makes that a decision rather than an accident.
    """
    monkeypatch.setattr(M, "CONFIDENCE_ENABLED", True)
    await _insert_mem(content="apples orchard hit", blob=_pack_of("apples orchard hit"))
    await _insert_mem(
        content="apples zzz yyy www",
        blob=_pack_of("completely different unrelated content xxxx"),
        ts="2026-01-01T00:00:01Z",
    )

    out = await M.do_recall(AGENT, "apples", limit=5, deep=True)
    contents = [m["content"] for m in out["messages"]]

    assert "apples orchard hit" in contents, "fixture regression: the strong hit was gated"
    assert "apples zzz yyy www" not in contents, (
        f"the below-gate lexical row was demoted into the result set instead of dropped: "
        f"{contents!r}"
    )
    assert "gate_fallback" not in out, (
        f"the rescue fired on a non-empty result: {out!r}"
    )


@pytest.mark.asyncio
async def test_a_native_cosine_row_is_not_rescued(monkeypatch, fake_embedding_client):
    """The rescue restores the membership the backfill removed — and nothing
    else. A row that reached the gate carrying its OWN cosine was judged on
    grounds b2 did not change, so an empty result stays empty.

    Widening the rescue to the whole pre-gate list breaks exactly this: a query
    with no real answer would start replying with its nearest below-gate
    neighbour, which ``test_audit_2500b3``'s
    ``test_empty_query_recall_bypasses_unscored_volume_gate`` and the
    ``recall-no-hits`` golden both refuse.

    Corpus and query are that audit test's, because its point is the same one:
    "a meaningful single-channel match in a small corpus must still be
    quality-gated". The candidate assertion below keeps this test HONEST — with
    an empty pre-gate list it would pass no matter how wide the rescue is.
    """
    monkeypatch.setattr(M, "CONFIDENCE_ENABLED", True)
    for content in (
        "alpha launch checklist",
        "bravo database migration",
        "charlie customer notes",
        "delta release summary",
    ):
        # Content and blob agree, so the vector channel — not FTS — is what
        # admits a row, and every admitted row carries a native cosine.
        await _insert_mem(content=content, blob=_pack_of(content))

    query = "ocean forest canyon"  # shares no token with any row
    db = await get_db()
    candidates = await vector._search_vector(
        db,
        AGENT,
        query,
        limit=10,
        min_similarity=vector._get_vector_threshold(AGENT) * M.RRF_THRESHOLD_FACTOR,
    )
    assert candidates, (
        "fixture is vacuous: the vector channel admitted nothing, so this test would "
        "pass with the rescue removed entirely"
    )
    assert all(c.get("_cosine") is not None for c in candidates)

    out = await M.do_recall(AGENT, query, limit=10)

    assert out["messages"] == [], (
        f"a below-gate native-cosine row was resurrected by the bug-183 rescue: {out!r}"
    )
    assert "gate_fallback" not in out


async def _insert_profile(content: str) -> None:
    db = await get_db()
    await db.execute(
        "INSERT INTO profiles (agent_id, user_id, content, updated_at) VALUES (?, '', ?, datetime('now'))",
        (AGENT, content),
    )
    await db.commit()


async def _insert_filler(count: int) -> None:
    """Rows that exist only to move ``memory_count`` past the profile gate's 50.

    Their content and blob agree and share no token with any test query, so they never
    reach the fused candidate set — the corpus size is the only thing they contribute.
    """
    for i in range(count):
        text = f"filler note {i} aardvark bookkeeping quarterly"
        await _insert_mem(content=text, blob=_pack_of(text))


@pytest.mark.asyncio
async def test_rescue_survives_autocut_when_a_profile_row_is_present(
    monkeypatch, fake_embedding_client
):
    """The rescue must survive the autocut that runs after it.

    ``_autocut`` cuts at the largest score gap. A profile row scores 1.0 (no cosine, no
    age) and the rescued rows are below-gate by construction, so the gap between them is
    the entire scale: autocut cuts at index 1 and the caller gets a response that says
    ``gate_fallback: true`` and contains nothing but the profile — the exact silence
    bug-183 exists to remove, now wearing a flag that claims otherwise.

    This also pins the rescue TRIGGER: it keys on "no non-profile row survived", not on
    "``results`` is empty". With a profile row passing the gate, ``results`` is non-empty
    and a ``not results`` trigger never fires at all.
    """
    monkeypatch.setattr(M, "CONFIDENCE_ENABLED", True)
    assert M.AUTOCUT_ENABLED, "this test is about the autocut interaction; it must be on"
    await _insert_profile("the operator prefers concise answers")
    await _insert_filler(50)  # memory_count >= 50 so the profile row passes the gate
    # TWO rescued rows, not one: with the profile they make three, and autocut returns
    # any set smaller than AUTOCUT_MIN_RESULTS whole. At two rows this test passes with
    # the cut restored — it would look like a regression test and be none.
    await _insert_mem(
        content="apples zzz yyy www",
        blob=_pack_of("completely different unrelated content xxxx"),
    )
    await _insert_mem(
        content="apples qqq rrr sss",
        blob=_pack_of("another entirely unrelated body of text yyyy"),
        ts="2026-01-01T00:00:01Z",
    )

    out = await M.do_recall(AGENT, "apples", limit=10)
    contents = [m["content"] for m in out["messages"]]

    assert out.get("gate_fallback") is True, (
        f"the rescue did not fire while a profile row occupied the result set: {out!r}"
    )
    assert len(contents) >= M.AUTOCUT_MIN_RESULTS, (
        f"fixture is vacuous: {len(contents)} rows is below AUTOCUT_MIN_RESULTS "
        f"({M.AUTOCUT_MIN_RESULTS}), where autocut declines to cut anything"
    )
    assert any(c.startswith("[Profile]") for c in contents), (
        f"the profile row passed the gate but is missing from the output: {contents!r}"
    )
    assert "apples zzz yyy www" in contents and "apples qqq rrr sss" in contents, (
        f"gate_fallback=true but the rescued rows are gone — autocut cut them away at the "
        f"profile/below-gate score gap: {contents!r}"
    )


@pytest.mark.asyncio
async def test_a_gate_blocked_profile_does_not_ride_along_with_the_rescue(
    monkeypatch, fake_embedding_client
):
    """The profile sentinel keeps the GATE's verdict, in both directions.

    Its rule is corpus size (``memory_count >= 50``), which the rescue has no business
    overturning: on a small corpus the profile was blocked before the rescue existed and
    must stay blocked after it. Rescuing every id=-1 row unconditionally would inject a
    profile into exactly the deployments the volume rule excludes.
    """
    monkeypatch.setattr(M, "CONFIDENCE_ENABLED", True)
    await _insert_profile("the operator prefers concise answers")
    await _insert_mem(  # corpus of 1 -> memory_count < 50 -> profile is gate-blocked
        content="apples zzz yyy www",
        blob=_pack_of("completely different unrelated content xxxx"),
    )

    out = await M.do_recall(AGENT, "apples", limit=10)
    contents = [m["content"] for m in out["messages"]]

    assert out.get("gate_fallback") is True, f"the rescue did not fire: {out!r}"
    assert "apples zzz yyy www" in contents
    assert not any(c.startswith("[Profile]") for c in contents), (
        f"a profile row the volume rule blocked was carried in by the rescue: {contents!r}"
    )


@pytest.mark.asyncio
async def test_the_rescue_returns_only_the_backfilled_rows(monkeypatch, fake_embedding_client):
    """Membership bound, pinned where it is observable: a backfilled row and a
    below-gate NATIVE-cosine row in the same emptied result.

    The trigger fires either way, so a mutation that keeps the trigger and widens the
    membership to the whole pre-gate list survives every other test in this file — the
    two row kinds have to coexist for the difference to show.
    """
    monkeypatch.setattr(M, "CONFIDENCE_ENABLED", True)
    # Native-cosine below-gate row: content and blob agree, and its cosine against the
    # query clears the vector channel's threshold but not the quality gate.
    await _insert_mem(content="delta release summary", blob=_pack_of("delta release summary"))
    # Backfilled row: FTS-matches "ocean", blob packed from disjoint text.
    await _insert_mem(
        content="ocean logs zzz yyy",
        blob=_pack_of("completely different unrelated content xxxx"),
    )

    query = "ocean forest canyon"
    db = await get_db()
    candidates = await vector._search_vector(
        db,
        AGENT,
        query,
        limit=10,
        min_similarity=vector._get_vector_threshold(AGENT) * M.RRF_THRESHOLD_FACTOR,
    )
    assert any(c["content"] == "delta release summary" for c in candidates), (
        "fixture is vacuous: the native-cosine row never entered the candidate set, so a "
        "widened membership would have nothing to wrongly return"
    )

    out = await M.do_recall(AGENT, query, limit=10)
    contents = [m["content"] for m in out["messages"]]

    assert out.get("gate_fallback") is True, f"the rescue did not fire: {out!r}"
    assert contents == ["ocean logs zzz yyy"], (
        f"expected only the backfilled row; got {contents!r}. The native-cosine row was "
        f"gated on grounds b2 never changed and must stay out."
    )


@pytest.mark.asyncio
async def test_rescued_rows_earn_no_ranking_credit(monkeypatch, fake_embedding_client):
    """A rescue must not feed the recall-boost loop.

    ``recall_count`` raises ``_compute_confidence``'s decay floor, so a row that keeps
    being returned BECAUSE nothing else passed would drift upward until it starts passing
    the gate on unrelated queries — the lexical contamination bug-155 closed, rebuilt out
    of its own disclosure path. Non-deep on purpose: ``deep=True`` skips the bump
    entirely, so the other rescue tests here cannot see this.
    """
    monkeypatch.setattr(M, "CONFIDENCE_ENABLED", True)
    row_id = await _insert_mem(
        content="apples zzz yyy www",
        blob=_pack_of("completely different unrelated content xxxx"),
    )
    db = await get_db()
    before = (
        await db.execute_fetchall(
            "SELECT recall_count, last_recalled_at FROM memories WHERE id = ?", (row_id,)
        )
    )[0]

    out = await M.do_recall(AGENT, "apples", limit=5)  # not deep -> the bump path is live
    assert out.get("gate_fallback") is True, f"the rescue did not fire: {out!r}"

    after = (
        await db.execute_fetchall(
            "SELECT recall_count, last_recalled_at FROM memories WHERE id = ?", (row_id,)
        )
    )[0]
    assert after == before, (
        f"a rescued below-gate row accrued rank credit: recall_count/last_recalled_at "
        f"{before!r} -> {after!r}"
    )


@pytest.mark.asyncio
async def test_recall_with_context_forwards_the_flag(monkeypatch, fake_embedding_client):
    """``do_recall_with_context`` delegates retrieval but builds its own response
    dict, so the flag has to be forwarded explicitly (like ``advisory``).
    Otherwise the merged-context caller is the one caller that cannot tell a
    rescued result from an ordinary one.
    """
    monkeypatch.setattr(M, "CONFIDENCE_ENABLED", True)
    await _insert_mem(
        content="apples zzz yyy www",
        blob=_pack_of("completely different unrelated content xxxx"),
    )

    out = await M.do_recall_with_context(
        AGENT,
        "apples",
        external_context=[{"role": "user", "content": "unrelated turn", "name": "u"}],
        limit=5,
        deep=True,
    )

    assert out.get("gate_fallback") is True, (
        f"the rescue was invisible through recall_with_context: {out!r}"
    )
