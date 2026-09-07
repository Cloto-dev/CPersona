"""Regression tests for the 2.5.12b2 comprehensive fix pass — the HIGH findings.

Every defect here was reproduced live on the 2.5.12b1 tree before it was touched:
the registry entry was the starting point, not the evidence. Each test below was
then re-run with the fix reverted and only its own source file stashed, so what
makes it a regression test is measured rather than asserted.

Two of them state an invariant rather than a fix shape, because the fix did not
land where the report assumed it would:

  bug-310  the repair really is cross-agent by design (scoping it would leave the
           UNIQUE index uncreatable), so the guard was sized to the write instead.
           The test admits either answer and refuses only the third: the call
           succeeds AND another agent's rows moved. A second test pins that an
           all-agents grant can still run the repair, so "deny everyone" does not
           pass for a fix.
  bug-315  the index path is handed back to the scan when hydration comes up
           short, rather than made to ask a more expensive question on every
           recall -- the cheap plan tests/test_bug285_probe_bound.py pins is part
           of the contract too.
"""

import os
import tempfile

os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "test_2512b2_high.db"))
os.environ.setdefault("CPERSONA_EMBEDDING_MODE", "none")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

import asyncio  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
import struct  # noqa: E402
import time  # noqa: E402
import tracemalloc  # noqa: E402

import numpy as np  # noqa: E402

from cpersona import (  # noqa: E402
    acl,
    admin_handlers,
    checks,
    config,
    database,
    maintenance_handlers,
    oauth,
    server,
    tasks,
    vector,
    vector_index,
)
from cpersona._vendored_mcp_common.embedding_client import EmbeddingClient  # noqa: E402
from cpersona.database import get_db  # noqa: E402
from test_oauth_verification import (  # noqa: E402
    ISSUER,
    NAMESPACED,
    RESOURCE,
    FakeIdp,
    _make_app,
    _request,
)
from tests.conftest import fake_embed_one  # noqa: E402


# ==========================================================================
# bug-310 — check_health(fix=true) was authorised per-agent and repaired every agent.
# ==========================================================================

ALICE = "probe.c4.alice"
BOB = "probe.c4.bob"


async def _seed_dedup_collisions(db):
    await db.execute("DELETE FROM memories")
    await db.execute("DROP INDEX IF EXISTS idx_memories_dedup_msg_id")
    for agent in (ALICE, BOB):
        for content in ("older", "newer"):
            await db.execute(
                "INSERT INTO memories (agent_id, project_id, msg_id, content, timestamp) "
                "VALUES (?, '', 'm1', ?, '2026-01-01T00:00:00Z')",
                (agent, content),
            )
    await db.commit()


async def _msg_ids(db, agent):
    rows = await db.execute_fetchall(
        "SELECT content, msg_id FROM memories WHERE agent_id = ? ORDER BY id", (agent,)
    )
    return {r[0]: r[1] for r in rows}


@pytest.fixture(autouse=True)
def _deactivate_acl_310():
    yield
    acl.activate(None)


@pytest.fixture(autouse=True)
def _restore_dedup_msg_id_schema():
    """Undo what ``_seed_dedup_collisions`` leaves on the shared database.

    It drops ``idx_memories_dedup_msg_id`` and seeds the duplicate msg_ids that
    make that index unbuildable, both on the one database this process shares
    with every other test module. The rows have to go first: while they are
    there the canonical UNIQUE index cannot be recreated, so the leak is not
    merely a missing index but a state in which nothing downstream can repair it.
    """
    yield
    db = database._db
    if db is None:
        return
    asyncio.run(_reset_dedup_msg_id_schema(db))


async def _reset_dedup_msg_id_schema(db):
    await db.execute("DELETE FROM memories WHERE agent_id IN (?, ?)", (ALICE, BOB))
    await db.commit()
    await checks.check_schema_objects(db, "", fix=True)
    await db.commit()


@pytest.mark.asyncio
async def test_a_grant_on_one_agent_does_not_rewrite_another_agents_rows(tmp_path):
    db = await get_db()
    await _seed_dedup_collisions(db)
    before_bob = await _msg_ids(db, BOB)
    assert before_bob == {"older": "m1", "newer": "m1"}, before_bob

    acl.activate(
        acl.AclConfig(
            grants_by_client={"c1": {ALICE: acl.PERM_WRITE}},
            token_entries=(("token-c1", "c1"),),
        )
    )
    guarded = server.registry._handlers["check_health"]
    token = acl.set_principal(acl.Principal("c1"))
    try:
        # Control: the guard IS live and the grant IS narrow -- the same call
        # naming bob is refused. Without this the probe could not tell a real
        # cross-agent write from an ACL that was never active.
        refused = await guarded(
            {"agent_id": BOB, "fix": True, "checks": ["dedup_msg_id_index"]}
        )
        assert refused.get("error") == "permission_denied", (
            f"the guard did not refuse a call on {BOB!r}; the ACL is not active: {refused}"
        )
        result = await guarded(
            {"agent_id": ALICE, "fix": True, "checks": ["dedup_msg_id_index"]}
        )
    finally:
        acl.reset_principal(token)

    # The invariant, stated without assuming which side closes the gap: a
    # client holding read-write on exactly one agent must not end up having
    # rewritten another agent's rows. A refusal satisfies it (the guard sized
    # its demand to the reach of the write); so does a call that ran and left
    # bob alone (the repair narrowed itself). What must never happen is the
    # third case -- the call succeeds AND bob's rows moved.
    after_bob = await _msg_ids(db, BOB)
    if result.get("error") == "permission_denied":
        assert after_bob == before_bob, (
            f"the call was refused and still wrote to {BOB!r}: {before_bob} -> {after_bob}"
        )
    else:
        assert result.get("ok") is not False, f"the call failed for another reason: {result}"
        assert after_bob == before_bob, (
            "check_health(fix=true) authorised only on "
            f"{ALICE!r} rewrote {BOB!r}'s rows: {before_bob} -> {after_bob}"
        )


@pytest.mark.asyncio
async def test_a_wildcard_grant_can_still_run_the_global_repair(tmp_path):
    """The demand must be sized to the write, not merely refused.

    Denying every caller would satisfy the invariant above and destroy the
    check: the index it restores is otherwise permanently uncreatable
    (cpersona/checks.py, check_dedup_msg_id_index). A client that DOES hold
    read-write on every agent must therefore still be able to run it.
    """
    db = await get_db()
    await _seed_dedup_collisions(db)

    acl.activate(
        acl.AclConfig(
            grants_by_client={"c2": {"*": acl.PERM_WRITE}},
            token_entries=(("token-c2", "c2"),),
        )
    )
    guarded = server.registry._handlers["check_health"]
    token = acl.set_principal(acl.Principal("c2"))
    try:
        result = await guarded(
            {"agent_id": ALICE, "fix": True, "checks": ["dedup_msg_id_index"]}
        )
    finally:
        acl.reset_principal(token)

    assert result.get("error") != "permission_denied", (
        f"an all-agents grant was refused the global repair: {result}"
    )
    rows = await db.execute_fetchall(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_memories_dedup_msg_id'"
    )
    assert rows, f"the repair ran but the index it exists to restore is absent: {result}"


# ==========================================================================
# bug-311 — verify_token raised on two legal token shapes instead of refusing.
# ==========================================================================


@pytest.fixture
def idp():
    return FakeIdp()


@pytest.fixture
def oauth_on(monkeypatch):
    monkeypatch.setattr(config, "OAUTH_RESOURCE", RESOURCE)
    monkeypatch.setattr(config, "OAUTH_AUTHORIZATION_SERVERS", ISSUER)
    monkeypatch.setattr(config, "OAUTH_SCOPES", "cpersona:read cpersona:write")
    monkeypatch.setattr(config, "OAUTH_JWKS_URI", "")


@pytest.fixture(autouse=True)
def _deactivate_acl_311():
    yield
    acl.activate(None)


def _verifier(idp):
    return oauth.IdpTokenVerifier((ISSUER,), RESOURCE, fetch=idp.fetch)


@pytest.mark.asyncio
async def test_array_scope_claim_does_not_raise(idp):
    """RFC 9068 only RECOMMENDS the space-delimited string; an array must be
    refused (None) or folded, never raised: verify_token's contract says every
    rejection returns None (cpersona/oauth.py:186-192)."""
    token = idp.mint(scope=["cpersona:read", "cpersona:write"])
    try:
        result = await _verifier(idp).verify_token(token)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"verify_token raised {type(exc).__name__}: {exc}")
    assert result is None or list(result.scopes) == ["cpersona:read", "cpersona:write"]


@pytest.mark.asyncio
async def test_fractional_exp_claim_does_not_raise(idp):
    """RFC 7519 NumericDate permits a fractional seconds portion; the SDK's
    AccessToken.expires_at is `int | None` (cpersona/oauth.py:270)."""
    token = idp.mint(exp=time.time() + 300.5)
    try:
        result = await _verifier(idp).verify_token(token)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"verify_token raised {type(exc).__name__}: {exc}")
    assert result is None or isinstance(result.expires_at, int)


@pytest.mark.asyncio
async def test_array_scope_through_the_assembled_app(oauth_on, monkeypatch, tmp_path, idp):
    """Through the shipped ASGI stack: the answer must be an HTTP status, not an
    exception escaping BearerTokenMiddleware (cpersona/server.py:2432-2449)."""
    monkeypatch.setattr(oauth, "_http_get", idp.fetch)
    path = tmp_path / "acl.json"
    path.write_text(
        json.dumps({"clients": [{"client_id": NAMESPACED, "token": None, "grants": {"*": "read"}}]}),
        encoding="utf-8",
    )
    path.chmod(0o600)
    acl_config = acl.load_config(str(path))
    app, reached = _make_app(acl_config=acl_config)
    token = idp.mint(scope=["cpersona:read"])
    try:
        status = await _request(app, token=token)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"ESCAPED THE APP: {type(exc).__name__}: {exc} (reached={reached})")
    assert status in (200, 401), f"unexpected status {status}"


# ==========================================================================
# bug-312 — one non-finite blob calibrated a NaN threshold and persisted it.
# ==========================================================================


AGENT_312 = "probe-nan"
DIM_312 = 8


def _remove_sidecar():
    try:
        os.remove(admin_handlers._calibration_sidecar_path())
    except OSError:
        pass


def _reset():
    vector._agent_thresholds.clear()
    vector._agent_fused_gates.clear()
    vector._global_fused_gate = None
    vector._fused_gate_signal = None
    vector._agent_betas.clear()
    config.VECTOR_MIN_SIMILARITY = 0.3


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    db = await get_db()
    await db.execute("DELETE FROM memories")
    await db.commit()
    _reset()
    _remove_sidecar()
    yield
    _reset()
    _remove_sidecar()


async def _seed_nan_corpus(db, finite=11, nan_rows=1):
    for i in range(finite):
        vec = [float((i + j) % 5) - 2.0 for j in range(DIM_312)]
        await db.execute(
            "INSERT INTO memories (agent_id, content, timestamp, embedding) VALUES (?,?,?,?)",
            (AGENT_312, f"memory {i}", "2026-05-14T00:00:00Z", EmbeddingClient.pack_embedding(vec)),
        )
    for i in range(nan_rows):
        blob = struct.pack(f"<{DIM_312}f", *([float("nan")] * DIM_312))
        await db.execute(
            "INSERT INTO memories (agent_id, content, timestamp, embedding) VALUES (?,?,?,?)",
            (AGENT_312, f"poison {i}", "2026-05-14T00:00:00Z", blob),
        )
    await db.commit()


@pytest.mark.asyncio
async def test_a_nan_embedding_does_not_produce_a_nan_threshold():
    """cpersona/admin_handlers.py:707 gates on byte width only, so a NaN blob
    reaches the similarity matrix (line 1302) and `max(raw, floor)` at line 799
    returns NaN because `floor > nan` is False. A threshold that is not a real
    number must never be applied."""
    db = await get_db()
    await _seed_nan_corpus(db)
    result = await admin_handlers.do_calibrate_threshold(AGENT_312)
    new = result.get("new_threshold")
    assert result["ok"] is False or (new is not None and math.isfinite(new)), (
        f"calibration reported ok={result['ok']} with new_threshold={new!r}; full result={result}"
    )


@pytest.mark.asyncio
async def test_a_nan_threshold_is_not_installed_in_the_live_table():
    """cpersona/vector._agent_thresholds feeds _get_vector_threshold, which the
    recall scan compares every row against."""
    db = await get_db()
    await _seed_nan_corpus(db)
    await admin_handlers.do_calibrate_threshold(AGENT_312)
    live = vector._agent_thresholds.get(AGENT_312)
    assert live is None or math.isfinite(live), f"live threshold for {AGENT_312} is {live!r}"


@pytest.mark.asyncio
async def test_a_nan_threshold_is_not_persisted_to_the_sidecar():
    """Persisted state survives restart: json.dump defaults to allow_nan=True,
    so the NaN is written as the bare token NaN (cpersona/admin_handlers.py:958-963)."""
    db = await get_db()
    await _seed_nan_corpus(db)
    result = await admin_handlers.do_calibrate_threshold(AGENT_312)
    path = admin_handlers._calibration_sidecar_path()
    raw = ""
    if os.path.exists(path):
        with open(path) as fh:
            raw = fh.read()
    assert "NaN" not in raw, (
        f"sidecar persisted a NaN threshold (sidecar_persisted={result.get('sidecar_persisted')}): {raw}"
    )


# ==========================================================================
# bug-313 — the sample ceiling bounded an allocation an order of magnitude small.
# ==========================================================================


AGENT_313 = "c75"
N = 700
DIM_313 = 32
# config.py's ceiling justification, re-measured for bug-313 over the whole
# computation (product + upper triangle + threshold sweep): 29.0 bytes per null
# pair, flat from n=700 to n=10,000, i.e. 363 MB at the n=5,000 ceiling.
DOCUMENTED_BYTES_PER_PAIR = 29.0
# The handler also reads rows, hydrates blobs and builds a response, and none of
# that scales with pairs. Measured at ~2.3 MB for this corpus; 4 MB leaves room
# without hiding the defect -- a return of the (256, pairs) broadcast would add
# 256 B/pair, which is 63 MB here and swamps any fixed-cost allowance.
HANDLER_FIXED_BYTES = 4_000_000


@pytest_asyncio.fixture
async def seeded_db():
    db = await get_db()
    await db.execute("DELETE FROM memories")
    await db.commit()

    rng = np.random.default_rng(1234)
    for i in range(N):
        vec = rng.standard_normal(DIM_313)
        vec = vec / np.linalg.norm(vec)
        await db.execute(
            "INSERT INTO memories (agent_id, content, timestamp, embedding) VALUES (?, ?, ?, ?)",
            (AGENT_313, f"probe row {i}", "", EmbeddingClient.pack_embedding([float(x) for x in vec])),
        )
    await db.commit()
    vector._agent_thresholds.clear()
    yield db
    await db.execute("DELETE FROM memories")
    await db.commit()
    vector._agent_thresholds.clear()
    try:
        os.remove(admin_handlers._calibration_sidecar_path())
    except OSError:
        pass


@pytest.mark.asyncio
async def test_default_calibration_stays_inside_the_documented_allocation_budget(seeded_db):
    assert config.CALIBRATE_METHOD == "separation", "probe assumes the shipped default method"

    tracemalloc.start()
    tracemalloc.reset_peak()
    result = await admin_handlers.do_calibrate_threshold(agent_id=AGENT_313, sample_size=N)
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert result["ok"] is True, result
    pairs = result["num_pairs"]
    assert pairs == N * (N - 1) // 2, (pairs, result["sampled_embeddings"])

    budget = DOCUMENTED_BYTES_PER_PAIR * pairs + HANDLER_FIXED_BYTES
    bytes_per_pair = max(peak - HANDLER_FIXED_BYTES, 0) / pairs
    ceiling_pairs = config.CALIBRATE_MAX_SAMPLE * (config.CALIBRATE_MAX_SAMPLE - 1) // 2
    assert peak <= budget, (
        f"calibration peaked at {peak / 1e6:.1f} MB for {pairs} null pairs "
        f"({bytes_per_pair:.1f} bytes/pair) — the ceiling in config.py:538-547 was sized "
        f"against ~{DOCUMENTED_BYTES_PER_PAIR:.0f} bytes/pair. At the clamp ceiling "
        f"n={config.CALIBRATE_MAX_SAMPLE} ({ceiling_pairs} pairs) that same rate is "
        f"{bytes_per_pair * ceiling_pairs / 1e9:.2f} GB, not the documented 0.36 GB."
    )


# ==========================================================================
# bug-314 — a report-only health run held the write lock for the rest of the run.
# ==========================================================================


AGENT_314 = "c82"


@pytest_asyncio.fixture
async def clean_db():
    db = await get_db()
    await db.execute("DELETE FROM memories")
    await db.execute(
        "INSERT INTO memories (agent_id, content, timestamp) VALUES (?, ?, ?)",
        (AGENT_314, "probe row", "2026-01-01T00:00:00Z"),
    )
    await db.commit()
    yield db
    await db.execute("DELETE FROM memories")
    await db.commit()


@pytest.mark.asyncio
async def test_report_only_health_run_does_not_block_a_concurrent_writer(clean_db, monkeypatch):
    assert config.FTS_ENABLED, "probe needs the FTS integrity-check statement to run"
    assert database.DB_PATH != ":memory:", "probe needs a separate read connection"

    reached = asyncio.Event()
    release = asyncio.Event()
    # sqlite_integrity is registered after fts_integrity (checks.py:2451-2510), so
    # pausing it holds the run open exactly where the claim says the lock is held.
    check = next(c for c in checks.HEALTH_CHECKS if c.name == "sqlite_integrity")
    original = check.runner

    async def gated(db, agent_id, fix, **kwargs):
        reached.set()
        await release.wait()
        return await original(db, agent_id, fix, **kwargs)

    monkeypatch.setattr(check, "runner", gated)

    health = asyncio.create_task(maintenance_handlers.do_check_health(agent_id=AGENT_314, fix=False))
    await asyncio.wait_for(reached.wait(), timeout=20)

    async def _timed(label, coro_factory):
        started = time.monotonic()
        try:
            await coro_factory()
            outcome = "ok"
        except Exception as exc:  # noqa: BLE001 - the observation is the exception itself
            outcome = f"{type(exc).__name__}: {exc}"
        return label, outcome, time.monotonic() - started

    async def _raw_write():
        async with database.transaction() as db:
            await db.execute(
                "INSERT INTO memories (agent_id, content, timestamp) VALUES (?, ?, ?)",
                (AGENT_314, "concurrent writer", "2026-01-01T00:00:01Z"),
            )

    async def _shipped_writer():
        # The background drain's own entry point (tasks.py:93 -> transaction()).
        await tasks.MemoryTaskQueue().enqueue("update_profile", AGENT_314, [{"content": "x"}])

    observations = [
        await _timed("transaction()", _raw_write),
        await _timed("MemoryTaskQueue.enqueue", _shipped_writer),
    ]

    release.set()
    health_result = await health

    failures = [o for o in observations if o[1] != "ok"]
    assert not failures, (
        f"writers were locked out during a report-only check_health run: {observations}; "
        f"the health run itself reported status={health_result.get('status')!r} "
        f"(maintenance_handlers.py:121 holds the write lock taken by the FTS "
        f"integrity-check INSERT at checks.py:881)"
    )


# ==========================================================================
# bug-315 — a deleted indexed row was invisible and made the index answer short.
# ==========================================================================


AGENT_315 = "c130.agent"
TOPIC = "alpha beta gamma"
_SHARED = fake_embed_one(f"{TOPIC} shared")


def _index_paths() -> tuple[str, str]:
    path = vector_index.index_path("memories")
    return path, path + ".tmp"


def _clean_index():
    for path in _index_paths():
        if os.path.exists(path):
            os.unlink(path)


async def _store(db, content, *, created_at, embedding):
    await db.execute(
        "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp,"
        " created_at, embedding) VALUES (?, '', '', ?, ?, ?, ?, ?)",
        (
            AGENT_315, content, '{"type": "User", "id": "user-a"}',
            "2026-03-01T00:00:00+00:00", created_at,
            np.array(embedding, dtype=np.float32).tobytes(),
        ),
    )


@pytest_asyncio.fixture
async def corpus(fake_embedding_client):
    db = await get_db()
    _clean_index()
    await db.execute("DELETE FROM memories")
    # 12 rows, most sharing one embedding so the top-10 window is saturated and a
    # dropped row must be backfilled from the 11th.
    for n in range(12):
        await _store(
            db, f"row {n}",
            created_at=f"2026-03-01 00:00:{n:02d}",
            embedding=_SHARED if n % 3 else fake_embed_one(f"unrelated {n}"),
        )
    await db.commit()
    yield db
    await db.execute("DELETE FROM memories")
    await db.commit()
    _clean_index()


async def _search(db):
    return await vector._search_vector(db, AGENT_315, TOPIC, 10, min_similarity=-1.0)


async def _answer_without_the_index(db):
    """The same query with the index file moved aside — the live scan's answer."""
    keep = {}
    for path in _index_paths():
        if os.path.exists(path):
            keep[path] = open(path, "rb").read()
            os.unlink(path)
    try:
        return await _search(db)
    finally:
        for path, blob in keep.items():
            with open(path, "wb") as fh:
                fh.write(blob)


@pytest.mark.asyncio
async def test_health_check_reports_an_index_holding_a_deleted_row(corpus):
    db = corpus
    assert (await vector_index.build_index(db, "memories"))["built"]
    index = vector_index.load_index("memories")
    rows = await db.execute_fetchall(
        "SELECT id FROM memories WHERE agent_id = ? ORDER BY id", (AGENT_315,)
    )
    victim = rows[0][0]
    assert victim <= index.watermark, "probe needs a row at or below the watermark"

    assert (await admin_handlers.do_delete_memory(victim, agent_id=AGENT_315))["ok"]

    issues = await checks.check_vector_index(db, AGENT_315, False)
    assert issues, (
        f"check_vector_index reported nothing after indexed row {victim} (watermark "
        f"{index.watermark}, {index.count} indexed rows) was deleted; the index still "
        "scores an id the database no longer holds (checks.py:2319-2335 only counts "
        "rows past the watermark)"
    )


@pytest.mark.asyncio
async def test_index_path_returns_the_same_rows_as_the_scan_after_a_delete(corpus):
    db = corpus
    assert (await vector_index.build_index(db, "memories"))["built"]
    index = vector_index.load_index("memories")
    scored = await _search(db)
    assert len(scored) == 10, f"probe needs a saturated top-10 window, got {len(scored)}"

    victim = scored[0]["id"]
    assert victim <= index.watermark
    assert (await admin_handlers.do_delete_memory(victim, agent_id=AGENT_315))["ok"]

    with_index = await _search(db)
    without_index = await _answer_without_the_index(db)

    assert [r["id"] for r in with_index] == [r["id"] for r in without_index], (
        f"index path returned {len(with_index)} rows {[r['id'] for r in with_index]} while "
        f"the live scan returned {len(without_index)} rows "
        f"{[r['id'] for r in without_index]} after indexed row {victim} was deleted"
    )
