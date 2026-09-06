"""Regression tests for the 2.5.12b2 fix pass — the admin/calibration handlers.

Each of these was reproduced live on the b1 tree first and then re-measured with
cpersona/admin_handlers.py stashed alone, so what makes them regression tests is
measured rather than asserted.

Two carry a note about where the fix landed:

  bug-323  the four posts stay non-fatal -- the local change is already committed
           and a failed index is not a failed delete. What is pinned is that the
           shortfall is *said*, at a level that is on.
  bug-395  and bug-396 arrived without probes, so the two tests below were written
           against the measurement in their registry entries rather than adapted
           from one.
"""

import os
import tempfile

os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "test_2512b2_handlers.db"))
os.environ.setdefault("CPERSONA_EMBEDDING_MODE", "none")

import inspect  # noqa: E402
import logging  # noqa: E402
import struct  # noqa: E402
import time  # noqa: E402

import httpx  # noqa: E402
import numpy as np  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from cpersona import (  # noqa: E402
    admin_handlers,
    config,
    server,
    session,
    utils,
    vector,
)
from cpersona import admin_handlers as admin  # noqa: E402
from cpersona._vendored_mcp_common.embedding_client import EmbeddingClient  # noqa: E402
from cpersona.database import connection, get_db, transaction  # noqa: E402

# ==========================================================================
# bug-323 — four remote side-effect posts never read their response.
# ==========================================================================
@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["delete_memory", "update_memory", "delete_agent", "delete_episode"])
@pytest.mark.parametrize("failure", [500, 401, "exception"])
async def test_remote_side_effect_failures_emit_warning(
    monkeypatch, caplog, fake_embedding_client, operation, failure,
):
    session.reset_pauses_for_tests()
    agent = f"probe-c14-{operation}-{failure}"
    table = "episodes" if operation == "delete_episode" else "memories"
    column = "summary" if table == "episodes" else "content"
    fields = "agent_id, summary" if table == "episodes" else "agent_id, content, timestamp"
    values = "?, ?" if table == "episodes" else "?, ?, datetime('now')"
    async with transaction() as db:
        cursor = await db.execute(
            f"INSERT INTO {table} ({fields}) VALUES ({values})",
            (agent, "original probe content"),
        )
        row_id = cursor.lastrowid
    requests = []

    def respond(request):
        requests.append(request)
        if failure == "exception":
            raise httpx.ConnectError("probe connection refused", request=request)
        return httpx.Response(failure, json={"error": "probe remote refusal"})

    monkeypatch.setattr(admin, "VECTOR_SEARCH_MODE", "remote")
    monkeypatch.setattr(fake_embedding_client, "_http_url", "https://probe.invalid/embed")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(fake_embedding_client, "_client", client)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=admin.logger.name):
            if operation == "delete_memory":
                result = await admin.do_delete_memory(row_id, agent)
            elif operation == "update_memory":
                result = await admin.do_update_memory(row_id, "updated probe content", agent)
            elif operation == "delete_agent":
                result = await admin.do_delete_agent_data(agent)
            else:
                result = await admin.do_delete_episode(row_id, agent)
    assert result["ok"] is True, result
    assert len(requests) == 1
    expected = "/index" if operation == "update_memory" else "/purge" if operation == "delete_agent" else "/remove"
    assert requests[0].url.path == expected
    async with connection() as db:
        rows = await db.execute_fetchall(f"SELECT {column} FROM {table} WHERE id = ?", (row_id,))
    assert rows == ([("updated probe content",)] if operation == "update_memory" else [])
    warnings = [r.getMessage() for r in caplog.records if r.name == admin.logger.name and r.levelno >= logging.WARNING]
    assert warnings, {"operation": operation, "remote_failure": failure, "result": result, "local_rows": rows, "warnings": warnings}


# ==========================================================================
# bug-341 — the calibration clock was the only reader without the naive-is-UTC rule.
# ==========================================================================
AGENT_341 = "probe-c69"
NAIVE = "2026-09-06 12:00:00"  # the shape SQLite datetime('now') writes: UTC, no offset
AWARE = "2026-09-06T12:00:00+00:00"  # the same instant, as do_store writes it


@pytest.fixture(autouse=True)
def _jst(monkeypatch):
    """Pin a non-UTC host timezone (the deployment's own: JST, UTC+9)."""
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    time.tzset()
    yield
    time.tzset()


@pytest_asyncio.fixture
async def db():
    session.reset_pauses_for_tests()
    conn = await get_db()
    for table in ("memories", "episodes", "profiles"):
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()
    return conn


def test_naive_timestamps_are_read_as_utc():
    admin_naive = admin_handlers._parse_ts_seconds(NAIVE)
    admin_aware = admin_handlers._parse_ts_seconds(AWARE)
    utils_naive = utils._parse_timestamp_utc(NAIVE).timestamp()

    assert admin_naive == utils_naive, (
        f"admin_handlers._parse_ts_seconds({NAIVE!r}) = {admin_naive} but the package "
        f"parser (utils._parse_timestamp_utc, utils.py:153-171) says {utils_naive}: a "
        f"{utils_naive - admin_naive:.0f}s shift, the host's UTC offset"
    )
    assert admin_naive == admin_aware, (
        f"the same instant spelled two ways parses {admin_aware - admin_naive:.0f}s apart "
        "(admin_handlers.py:664-669)"
    )


@pytest.mark.asyncio
async def test_temporal_adjacency_window_does_not_depend_on_host_timezone(db):
    """The consumer at admin_handlers.py:729 compares the two spellings inside a
    30-minute window (config.CALIBRATE_TEMPORAL_WINDOW_MIN)."""
    blob = struct.pack("<8f", *[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    blob2 = struct.pack("<8f", *[0.9, 0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    # 60 seconds apart in real time: unambiguously the same session.
    await db.execute(
        "INSERT INTO memories (agent_id, content, timestamp, embedding) VALUES (?,?,?,?)",
        (AGENT_341, "first", AWARE, blob),
    )
    await db.execute(
        "INSERT INTO memories (agent_id, content, timestamp, embedding) VALUES (?,?,?,?)",
        (AGENT_341, "second", "2026-09-06 12:01:00", blob2),
    )
    await db.commit()

    sims = await admin_handlers._temporal_adjacency_sims(db, AGENT_341, 100, 30.0)

    assert len(sims) == 1, (
        "two memories 60 seconds apart -- one stamped with an offset (as do_store writes "
        "it) and one naive (as SQLite datetime('now') and episodes.created_at write it) -- "
        f"produced {len(sims)} same-session pairs instead of 1. On a UTC+9 host the naive "
        "stamp is read 32400s earlier (admin_handlers.py:664-669), so the pair falls "
        "outside the 1800s window and is scored as unrelated"
    )


# ==========================================================================
# bug-342 — set_recall_precision asked calibration about the wrong session bucket.
# ==========================================================================
AGENT_342 = "probe-c70"


def _reset_calibration_process_state():
    vector._agent_thresholds.clear()
    vector._agent_fused_gates.clear()
    vector._agent_betas.clear()
    vector._global_fused_gate = None
    vector._fused_gate_signal = None
    vector._reset_calibration_authority()


@pytest.fixture
def sidecar_342(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "cpersona.db"))
    saved_threshold = config.VECTOR_MIN_SIMILARITY
    _reset_calibration_process_state()
    session.reset_pauses_for_tests()
    yield str(tmp_path / "cpersona.db") + ".calibration.json"
    _reset_calibration_process_state()
    session.reset_pauses_for_tests()
    config.VECTOR_MIN_SIMILARITY = saved_threshold


@pytest_asyncio.fixture
async def corpus_342():
    """14 embedded memories: enough for the >=10 floor in _sample_embeddings."""
    conn = await get_db()
    for table in ("memories", "episodes", "profiles"):
        await conn.execute(f"DELETE FROM {table}")
    rng = np.random.default_rng(7)
    for i in range(14):
        v = rng.standard_normal(8).astype(np.float32)
        v /= float(np.linalg.norm(v))
        await conn.execute(
            "INSERT INTO memories (agent_id, content, timestamp, embedding) VALUES (?,?,?,?)",
            (AGENT_342, f"memory number {i}", f"2026-09-06T12:{i:02d}:00+00:00",
             struct.pack("<8f", *v.tolist())),
        )
    await conn.commit()
    return conn


@pytest.mark.asyncio
async def test_control_nothing_paused(sidecar_342, corpus_342):
    result = await admin_handlers.do_set_recall_precision(AGENT_342, "strict", session_key="s1")
    assert result["ok"] is True
    assert result["calibrate"]["new_threshold"] is not None, result
    assert AGENT_342 in vector._agent_thresholds
    assert os.path.exists(sidecar_342), "the control call wrote the sidecar_342"


@pytest.mark.asyncio
async def test_a_foreign_keyless_pause_does_not_suppress_a_declared_callers_calibration(
    sidecar_342, corpus_342
):
    # Another client (a benchmark) pauses without declaring a key: the shared
    # keyless bucket, reported as scope: "process".
    armed = await server.do_pause_persistence(ttl_seconds=300)
    assert armed["scope"] == "process"

    # This caller declares its own key and is NOT paused.
    assert session.is_paused_for("s1") is False
    result = await admin_handlers.do_set_recall_precision(AGENT_342, "strict", session_key="s1")

    assert result["ok"] is True and result["beta"] == 2.0
    assert result["calibrate"]["new_threshold"] is not None, (
        "the inner do_calibrate_threshold ran in the keyless bucket "
        "(admin_handlers.py:1656 omits session_key) and returned its no-persist "
        f"skeleton, but the outer response reports success: {result!r}. It carries no "
        "persisted:false marker, so the caller cannot tell the recalibration did not happen"
    )
    assert AGENT_342 in vector._agent_thresholds, (
        "no per-agent threshold was applied even though the caller was not paused"
    )
    assert os.path.exists(sidecar_342), (
        f"the sidecar_342 at {sidecar_342} was never written: the setting is gone on the next "
        f"restart while the beta sits claimed in process memory "
        f"(vector._agent_betas={dict(vector._agent_betas)!r}, "
        f"claimed={AGENT_342 in vector._calibration_authority['agent_betas']})"
    )


# ==========================================================================
# bug-344 — the startup width probe and the persisted width were different quantities.
# ==========================================================================
AGENT_344 = "probe.c73"


def _remove_sidecar():
    for path in (
        admin_handlers._calibration_sidecar_path(),
        *__import__("glob").glob(admin_handlers._calibration_sidecar_path() + ".before-*"),
    ):
        try:
            os.remove(path)
        except OSError:
            pass


def _reset_module_state():
    vector._agent_thresholds.clear()
    config.VECTOR_MIN_SIMILARITY = 0.3
    vector._agent_fused_gates.clear()
    vector._global_fused_gate = None
    vector._fused_gate_signal = None
    vector._agent_betas.clear()


async def _seed(db, agent_id, count, dim, content_prefix):
    for i in range(count):
        vec = [float((i + j) % 5) - 2.0 for j in range(dim)]
        await db.execute(
            "INSERT INTO memories (agent_id, content, timestamp, embedding) VALUES (?, ?, ?, ?)",
            (agent_id, f"{content_prefix} {i}", "2026-05-14T00:00:00Z",
             EmbeddingClient.pack_embedding(vec)),
        )
    await db.commit()


@pytest_asyncio.fixture(autouse=True)
async def clean_344():
    db = await get_db()
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM episodes")
    await db.commit()
    _reset_module_state()
    _remove_sidecar()
    yield db
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM episodes")
    await db.commit()
    _reset_module_state()
    _remove_sidecar()


@pytest.mark.asyncio
async def test_homogeneous_corpus_settles_after_one_recalibration(clean_344):
    """Control: with one width throughout, the probe and the persisted value are
    the same number, so the second boot restores instead of recalibrating."""
    await _seed(clean_344, AGENT_344, 20, 8, "current")
    admin_handlers._save_calibration_state(
        embedding_dim=4, embedding_model="old-model",
        global_threshold=0.99, agent_thresholds={AGENT_344: 0.99},
        global_fused_gate=0.45, fused_gate_signal="confidence",
    )
    first = await admin_handlers.ensure_calibrated_on_startup(
        auto_calibrate=False, on_model_change=True
    )
    assert first["action"] == "recalibrated", first
    second = await admin_handlers.ensure_calibrated_on_startup(
        auto_calibrate=False, on_model_change=True
    )
    assert second["action"] != "recalibrated", second


@pytest.mark.asyncio
async def test_mixed_dimension_corpus_stops_recalibrating_after_the_first_boot(clean_344):
    """The defect: 3 legacy 16-wide rows (oldest, so lowest rowids) beside 20
    current 8-wide rows. Recalibration writes the modal 8; the boot probe keeps
    reading 16, so `dim_changed` never clears."""
    await _seed(clean_344, AGENT_344, 3, 16, "legacy")
    await _seed(clean_344, AGENT_344, 20, 8, "current")

    live = await admin_handlers._corpus_embedding_dim()

    boots = []
    for _ in range(4):
        status = await admin_handlers.ensure_calibrated_on_startup(
            auto_calibrate=False, on_model_change=True
        )
        boots.append((status["action"], status.get("dim_changed")))
    stored = admin_handlers._load_calibration_state()["embedding_dim"]

    # Boot 1 is the legitimate one (no sidecar yet). Every boot after it is on an
    # unchanged corpus and an unchanged model.
    assert all(a != "recalibrated" for a, _ in boots[1:]), (
        f"the boot probe reported dim {live} while calibration persisted "
        f"embedding_dim {stored}, so an unchanged corpus recalibrates on every boot: "
        f"{boots}"
    )


@pytest.mark.asyncio
async def test_the_probe_and_the_persisted_value_are_the_same_quantity(clean_344):
    """The same claim stated directly on the two functions, so the mechanism is
    visible without the boot guard in between."""
    await _seed(clean_344, AGENT_344, 3, 16, "legacy")
    await _seed(clean_344, AGENT_344, 20, 8, "current")

    live = await admin_handlers._corpus_embedding_dim()
    result = await admin_handlers.do_calibrate_threshold(AGENT_344)
    assert result["ok"] is True, result

    assert live == result["embedding_dim"], (
        f"_corpus_embedding_dim (LIMIT 1, admin_handlers.py:1061) answered {live} while "
        f"do_calibrate_threshold persisted the modal {result['embedding_dim']} "
        "(admin_handlers.py:1433); ensure_calibrated_on_startup compares these two"
    )


# ==========================================================================
# bug-396 — the positive proxy kept the lexicographically largest stamps.
# ==========================================================================


@pytest.mark.asyncio
async def test_the_adjacency_window_is_ordered_by_the_instant_not_the_text():
    """`timestamp` is caller-supplied TEXT, so one client stamping an offset and
    another stamping UTC fill the same column. Ordering by bytes kept an older row
    and dropped a newer one; ordering by the parsed instant cannot."""
    agent = "b2.396"
    db = await get_db()
    await db.execute("DELETE FROM memories WHERE agent_id = ?", (agent,))
    # 09:00+09:00 is 00:00Z -- the OLDEST of the three, but the largest as text.
    stamps = [
        ("2026-09-06T09:00:00+09:00", b"\x00" * 32),
        ("2026-09-06T01:00:00+00:00", b"\x01" * 32),
        ("2026-09-06T02:00:00+00:00", b"\x02" * 32),
    ]
    # Content is unique per (agent, project, channel); vary the axis the test does
    # NOT measure so the rows are distinguishable without touching the ordering.
    for n, (ts, blob) in enumerate(stamps):
        await db.execute(
            "INSERT INTO memories (agent_id, project_id, content, timestamp, embedding) "
            "VALUES (?, '', ?, ?, ?)",
            (agent, f"probe row {n}", ts, blob),
        )
    await db.commit()

    iso_clause = "AND agent_id = ?"
    kept = await db.execute_fetchall(
        f"SELECT timestamp FROM memories WHERE embedding IS NOT NULL {iso_clause} "
        "ORDER BY datetime(timestamp) DESC LIMIT 2",
        (agent,),
    )
    kept_stamps = [r[0] for r in kept]
    # Control: the byte order really does disagree here, so the test is not
    # measuring a corpus where both orderings happen to agree.
    by_text = await db.execute_fetchall(
        f"SELECT timestamp FROM memories WHERE embedding IS NOT NULL {iso_clause} "
        "ORDER BY timestamp DESC LIMIT 2",
        (agent,),
    )
    assert [r[0] for r in by_text] != kept_stamps, (
        "the fixture no longer distinguishes byte order from chronological order"
    )
    assert "2026-09-06T09:00:00+09:00" not in kept_stamps, (
        f"the chronologically oldest row survived a newest-first cut: {kept_stamps}"
    )
    assert set(kept_stamps) == {"2026-09-06T02:00:00+00:00", "2026-09-06T01:00:00+00:00"}, kept_stamps

    # And the shipped query is the one that was measured, not a copy of it.
    source = inspect.getsource(admin_handlers._temporal_adjacency_sims)
    assert "ORDER BY datetime(timestamp) DESC" in source, source


# ==========================================================================
# bug-395 — the two calibration populations voted on their width separately.
# ==========================================================================


def test_the_positive_proxy_is_drawn_at_the_width_the_null_settled_on():
    """One threshold sweep is handed both populations, so they have to describe the
    same model. The proxy sampler now takes the width rather than voting again."""
    sig = inspect.signature(admin_handlers._temporal_adjacency_sims)
    assert "target_dim" in sig.parameters, sig

    call_site = inspect.getsource(admin_handlers.do_calibrate_threshold)
    assert "target_dim=int(vecs.shape[1])" in call_site, (
        "the proxy is still drawn without the width the null sample settled on"
    )
    body = inspect.getsource(admin_handlers._temporal_adjacency_sims)
    assert "if target_dim is None:" in body, (
        "a supplied width must replace the independent vote, not sit beside it"
    )


def test_the_modal_width_is_a_property_of_the_corpus_not_of_the_scan():
    """Counter.most_common breaks a tie by insertion order, i.e. by row order, so
    the same corpus could answer differently between the samplers and the probe."""
    from collections import Counter

    tie_a = Counter({8: 5, 16: 5})
    tie_b = Counter()
    tie_b[16] = 5
    tie_b[8] = 5
    assert admin_handlers._modal_width(tie_a) == admin_handlers._modal_width(tie_b)
    assert admin_handlers._modal_width(Counter({8: 20, 16: 3})) == 8
    assert admin_handlers._modal_width({64: 1, 32: 9}) == 32
    # The insertion-order rule really does disagree on the tie, so the assertion
    # above is not passing for free.
    assert tie_a.most_common(1)[0][0] != tie_b.most_common(1)[0][0]
