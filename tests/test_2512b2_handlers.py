"""Regression tests for the 2.5.12b2 fix pass — the admin/calibration handlers.

Each of these was reproduced live on the b1 tree first and then re-measured with
cpersona/admin_handlers.py stashed alone, so what makes them regression tests is
measured rather than asserted.

The second pass (2026-09-07) added the eight remaining admin_handlers findings --
bug-343, bug-345, bug-346, bug-347, bug-349, bug-350, bug-365 and bug-367 -- to this
module rather than to one of its own, so the suite's module count (and with it the
published quality-assurance figures) does not move for a fix pass.

Three of those arrived with probes that assumed a shape the fix could not take, and were
rewritten as invariants before being adopted; each says so at its own section.

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

import asyncio  # noqa: E402
import base64  # noqa: E402
import glob  # noqa: E402
import inspect  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import re  # noqa: E402
import struct  # noqa: E402
import time  # noqa: E402

import aiosqlite  # noqa: E402
import httpx  # noqa: E402
import numpy as np  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from cpersona import (  # noqa: E402
    acl,
    admin_handlers,
    config,
    database,
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


# ==========================================================================
# bug-343 — the import path stored a non-finite embedding the store seam refuses,
#           and said nothing about any embedding it dropped.
# ==========================================================================
AGENT_343 = "regress.b343"


def _b64_343(values):
    return base64.b64encode(struct.pack(f"<{len(values)}f", *values)).decode()


def _jsonl_343(records):
    path = os.path.join(tempfile.mkdtemp(), "b343.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")
    return path


@pytest_asyncio.fixture
async def clean_343(monkeypatch):
    monkeypatch.setattr(config, "EXPORT_DIR", "")
    session.reset_pauses_for_tests()
    db = await get_db()
    for table in ("memories", "episodes"):
        await db.execute(f"DELETE FROM {table} WHERE agent_id = ?", (AGENT_343,))
    await db.commit()
    yield db
    for table in ("memories", "episodes"):
        await db.execute(f"DELETE FROM {table} WHERE agent_id = ?", (AGENT_343,))
    await db.commit()


def test_the_store_seam_refuses_the_values_the_import_accepted_343():
    """Control: the authoring seam's verdict on exactly these vectors."""
    assert vector.pack_for_storage([float("nan")] * 8) is None
    assert vector.pack_for_storage([float("inf")] * 8) is None
    assert vector.pack_for_storage([0.5] * 8) is not None


@pytest.mark.asyncio
async def test_import_does_not_store_a_non_finite_memory_embedding_343(clean_343):
    path = _jsonl_343(
        [
            {
                "_type": "memory",
                "agent_id": AGENT_343,
                "content": "a restored memory",
                "embedding_b64": _b64_343([float("nan")] * 8),
            }
        ]
    )
    result = await admin.do_import_memories(path, target_agent_id=AGENT_343)
    assert result.get("ok") is True, result

    rows = await clean_343.execute_fetchall(
        "SELECT embedding FROM memories WHERE agent_id = ?", (AGENT_343,)
    )
    bad = [r[0] for r in rows if r[0] is not None and not vector.stored_blob_is_finite(r[0])]
    assert not bad, f"import stored {len(bad)} non-finite embedding blob(s); result={result}"


@pytest.mark.asyncio
async def test_import_does_not_store_a_non_finite_episode_embedding_343(clean_343):
    path = _jsonl_343(
        [
            {
                "_type": "episode",
                "agent_id": AGENT_343,
                "summary": "a restored episode",
                "keywords": ["k"],
                "embedding_b64": _b64_343([float("inf")] * 8),
            }
        ]
    )
    result = await admin.do_import_memories(path, target_agent_id=AGENT_343)
    assert result.get("ok") is True, result

    rows = await clean_343.execute_fetchall(
        "SELECT embedding FROM episodes WHERE agent_id = ?", (AGENT_343,)
    )
    bad = [r[0] for r in rows if r[0] is not None and not vector.stored_blob_is_finite(r[0])]
    assert not bad, f"import stored {len(bad)} non-finite episode embedding blob(s)"


@pytest.mark.asyncio
async def test_import_reports_the_embedding_it_dropped_343(clean_343):
    """The narrower half: a restore that loses every vector it carried used to be
    indistinguishable from one that carried none."""
    path = _jsonl_343(
        [
            {
                "_type": "memory",
                "agent_id": AGENT_343,
                "content": "another restored memory",
                "embedding_b64": _b64_343([float("nan")] * 8),
            }
        ]
    )
    result = await admin.do_import_memories(path, target_agent_id=AGENT_343)
    assert result.get("errors"), f"a dropped embedding produced no error line; {result}"
    assert any("non-finite" in line for line in result["errors"]), result["errors"]


# ==========================================================================
# bug-345 — the merge copy passes materialised the whole source table, inside the
#           transaction, so one read's resident set equalled the corpus under the
#           process-wide write lock.
#
# The probe's byte bound was rewritten (2026-09-07): it derived the corpus size from
# the embedding width alone, which sits below the true weight of a page once the
# content column is counted, and so failed a correctly paged reader. The bound here is
# derived from what the copy pass actually read.
# ==========================================================================
SRC_345 = "regress.b345.src"
DST_345 = "regress.b345.dst"
ROWS_345 = 1200
EMB_FLOATS_345 = 1024
PAGE_345 = 500  # the bound the export path in the same file uses


@pytest_asyncio.fixture
async def corpus_345():
    session.reset_pauses_for_tests()
    db = await get_db()
    for agent in (SRC_345, DST_345):
        await db.execute("DELETE FROM memories WHERE agent_id = ?", (agent,))
        await db.execute("DELETE FROM episodes WHERE agent_id = ?", (agent,))
    await db.commit()
    blob = os.urandom(EMB_FLOATS_345 * 4)
    for i in range(ROWS_345):
        await db.execute(
            "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp,"
            " created_at, embedding) VALUES (?, '', '', ?, '{}', ?, ?, ?)",
            (
                SRC_345,
                f"source memory {i} " + "x" * 200,
                "2026-03-01T00:00:00+00:00",
                "2026-03-01 00:00:00",
                blob,
            ),
        )
    await db.commit()
    yield db
    for agent in (SRC_345, DST_345):
        await db.execute("DELETE FROM memories WHERE agent_id = ?", (agent,))
        await db.execute("DELETE FROM episodes WHERE agent_id = ?", (agent,))
    await db.commit()


@pytest.fixture
def record_fetchall_345(monkeypatch):
    """Record (rows returned, bytes resident, write lock held) for every fetchall."""
    seen = []
    original = aiosqlite.Connection.execute_fetchall

    def _resident_bytes(rows):
        return sum(
            len(value)
            for row in rows
            for value in row
            if isinstance(value, (bytes, str))
        )

    async def spy(self, sql, parameters=None, *a, **kw):
        rows = await original(self, sql, parameters, *a, **kw)
        seen.append(
            {
                "rows": len(rows),
                "bytes": _resident_bytes(rows),
                "write_lock_held": database._write_lock.locked(),
                "sql": " ".join(sql.split()),
            }
        )
        return rows

    monkeypatch.setattr(aiosqlite.Connection, "execute_fetchall", spy)
    return seen


@pytest.mark.asyncio
async def test_the_read_instrument_reports_what_it_is_asked_to_345(corpus_345, record_fetchall_345):
    """Instrument self-test: the spy sees a read it is shown, counts its rows, and
    reports the write lock correctly outside `transaction()`."""
    rows = await corpus_345.execute_fetchall(
        "SELECT id, embedding FROM memories WHERE agent_id = ? LIMIT 100", (SRC_345,)
    )
    assert len(rows) == 100
    seen = [r for r in record_fetchall_345 if "LIMIT 100" in r["sql"]]
    assert len(seen) == 1 and seen[0]["rows"] == 100, record_fetchall_345
    assert seen[0]["write_lock_held"] is False
    assert seen[0]["bytes"] >= 100 * EMB_FLOATS_345 * 4


@pytest.mark.asyncio
async def test_merge_reads_the_source_corpus_in_bounded_chunks_345(corpus_345, record_fetchall_345):
    result = await admin.do_merge_memories(SRC_345, DST_345)
    assert result["ok"] is True and result["merged_memories"] == ROWS_345, result

    reads = [r for r in record_fetchall_345 if "FROM memories" in r["sql"]]
    assert reads, f"no memories read recorded: {[r['sql'][:60] for r in record_fetchall_345]}"
    unbounded = [{k: v for k, v in r.items() if k != "sql"} for r in reads if r["rows"] > PAGE_345]
    assert not unbounded, (
        "one statement returned more of the source table than the bound the export path "
        f"in the same file uses ({PAGE_345}): {unbounded}"
    )


@pytest.mark.asyncio
async def test_the_materialisation_does_not_happen_under_the_write_lock_345(
    corpus_345, record_fetchall_345
):
    result = await admin.do_merge_memories(SRC_345, DST_345)
    assert result["ok"] is True, result

    big_under_lock = [
        {k: v for k, v in r.items() if k != "sql"}
        for r in record_fetchall_345
        if r["write_lock_held"] and r["rows"] >= ROWS_345 and "FROM memories" in r["sql"]
    ]
    assert not big_under_lock, (
        "the whole source table was materialised while the process-wide write lock was "
        f"held, blocking every other writer for its duration: {big_under_lock}"
    )


@pytest.mark.asyncio
async def test_one_merge_read_does_not_hold_the_whole_corpus_resident_345(
    corpus_345, record_fetchall_345
):
    result = await admin.do_merge_memories(SRC_345, DST_345)
    assert result["ok"] is True, result

    copy_reads = [
        r
        for r in record_fetchall_345
        if "FROM memories WHERE agent_id" in r["sql"] and r["rows"] > 1
    ]
    assert copy_reads, (
        "no multi-row read of the source was recorded; the test measures nothing: "
        f"{[(r['sql'][:70], r['rows']) for r in record_fetchall_345]}"
    )
    # Every copy-pass read together is one traversal of the source, however it was split,
    # so a reader paging at PAGE can hold at most PAGE/ROWS of it whatever a row weighs.
    traversal_bytes = sum(r["bytes"] for r in copy_reads)
    biggest = max(copy_reads, key=lambda r: r["bytes"])
    assert biggest["bytes"] <= traversal_bytes * PAGE_345 / ROWS_345 * 1.05, (
        f"a single merge read held {biggest['bytes'] / 1e6:.2f} MB resident "
        f"({biggest['rows']} rows) out of a {traversal_bytes / 1e6:.2f} MB traversal, "
        f"write_lock_held={biggest['write_lock_held']}"
    )


# ==========================================================================
# bug-346 — a move-mode merge deleted the source rows and left their vectors indexed
#           under the source namespace.
# ==========================================================================
SRC_346 = "regress.b346.src"
DST_346 = "regress.b346.dst"


class _Response346:
    status_code = 200

    def json(self):
        return {}


class _RecordingHttp346:
    def __init__(self):
        self.posts = []

    async def post(self, url, json=None, **kwargs):  # noqa: A002
        self.posts.append((url.rsplit("/", 1)[-1], json))
        return _Response346()


@pytest.fixture
def remote_346(monkeypatch):
    http = _RecordingHttp346()

    class _Client:
        _http_url = "http://fake/embed"
        _client = http
        mode = "remote"

        async def embed(self, texts):
            return [[0.1] * 8 for _ in texts]

    monkeypatch.setattr(vector, "VECTOR_SEARCH_MODE", "remote")
    monkeypatch.setattr(admin_handlers, "VECTOR_SEARCH_MODE", "remote")
    monkeypatch.setattr(vector, "_embedding_client", _Client())
    return http


@pytest_asyncio.fixture
async def corpus_346():
    session.reset_pauses_for_tests()
    db = await get_db()
    for agent in (SRC_346, DST_346):
        for table in ("memories", "episodes", "profiles"):
            await db.execute(f"DELETE FROM {table} WHERE agent_id = ?", (agent,))
    await db.commit()
    for i in range(2):
        await db.execute(
            "INSERT INTO memories (agent_id, project_id, channel, content, source, timestamp,"
            " created_at) VALUES (?, '', '', ?, '{}', ?, ?)",
            (SRC_346, f"a source memory {i}", "2026-03-01T00:00:00+00:00", "2026-03-01 00:00:00"),
        )
    # The ordinary case: BOTH agents have a profile, so the source profile is skipped by
    # the copy pass and stays behind -- which is what makes the whole-namespace purge
    # (the only source-side cleanup there was) inapplicable.
    for agent, body in ((SRC_346, "source profile"), (DST_346, "target profile")):
        await db.execute(
            "INSERT INTO profiles (agent_id, user_id, content, updated_at)"
            " VALUES (?, '', ?, datetime('now'))",
            (agent, body),
        )
    await db.commit()
    yield db
    for agent in (SRC_346, DST_346):
        for table in ("memories", "episodes", "profiles"):
            await db.execute(f"DELETE FROM {table} WHERE agent_id = ?", (agent,))
    await db.commit()


@pytest.mark.asyncio
async def test_the_recorder_sees_the_single_row_delete_path_346(corpus_346, remote_346):
    """Instrument control: the path that DOES clean up is visible to this recorder."""
    rows = await corpus_346.execute_fetchall(
        "SELECT id FROM memories WHERE agent_id = ?", (SRC_346,)
    )
    victim = rows[0][0]
    result = await admin.do_delete_memory(victim, agent_id=SRC_346)
    assert result["ok"] is True, result
    removes = [(ep, body) for ep, body in remote_346.posts if ep == "remove"]
    assert removes == [
        ("remove", {"namespace": f"cpersona:{SRC_346}", "ids": [f"mem:{victim}"]})
    ], remote_346.posts


@pytest.mark.asyncio
async def test_move_removes_the_moved_vectors_from_the_source_namespace_346(
    corpus_346, remote_346
):
    src_ids = [
        r[0]
        for r in await corpus_346.execute_fetchall(
            "SELECT id FROM memories WHERE agent_id = ? ORDER BY id", (SRC_346,)
        )
    ]
    result = await admin.do_merge_memories(SRC_346, DST_346, mode="move")
    assert result["ok"] is True and result["merged_memories"] == 2, result

    left = await corpus_346.execute_fetchall(
        "SELECT COUNT(*) FROM memories WHERE agent_id = ?", (SRC_346,)
    )
    assert left[0][0] == 0, "the source rows were not deleted, so there is nothing to orphan"

    src_ns = f"cpersona:{SRC_346}"
    cleaned = set()
    purged = False
    for endpoint, body in remote_346.posts:
        if body.get("namespace") != src_ns:
            continue
        if endpoint == "purge":
            purged = True
        elif endpoint == "remove":
            cleaned.update(body.get("ids") or [])

    orphaned = [f"mem:{i}" for i in src_ids if not purged and f"mem:{i}" not in cleaned]
    assert not orphaned, (
        f"the moved rows were deleted from SQLite but their vectors stay indexed under "
        f"{src_ns}: {orphaned}; the whole merge produced {remote_346.posts}"
    )


# ==========================================================================
# bug-347 — an unreadable calibration sidecar was indistinguishable from an absent
#           one, so the boot called it a fresh install and replaced it uncopied.
#
# The last test was rewritten as an invariant (2026-09-07). The probe asserted the
# override is still IN the sidecar afterwards, which assumes a fix that cannot exist:
# the file does not parse, so nothing can be carried out of it. What the code owes the
# operator is the bytes.
# ==========================================================================
AGENT_347 = "regress.b347"


def _sidecar_347():
    return admin_handlers._calibration_sidecar_path()


def _clean_sidecars_347():
    for path in [_sidecar_347(), *glob.glob(glob.escape(_sidecar_347()) + ".before-*")]:
        try:
            os.remove(path)
        except OSError:
            pass


def _reset_calibration_state_347():
    vector._agent_thresholds.clear()
    config.VECTOR_MIN_SIMILARITY = 0.3
    vector._agent_fused_gates.clear()
    vector._global_fused_gate = None
    vector._fused_gate_signal = None
    vector._agent_betas.clear()


async def _seed_347(db, count=15, dim=8):
    for i in range(count):
        vec = [float((i + j) % 5) - 2.0 for j in range(dim)]
        await db.execute(
            "INSERT INTO memories (agent_id, content, timestamp, embedding) VALUES (?, ?, ?, ?)",
            (AGENT_347, f"memory {i}", "2026-05-14T00:00:00Z", EmbeddingClient.pack_embedding(vec)),
        )
    await db.commit()


@pytest_asyncio.fixture
async def clean_347():
    db = await get_db()
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM episodes")
    await db.commit()
    _reset_calibration_state_347()
    _clean_sidecars_347()
    yield db
    await db.execute("DELETE FROM memories")
    await db.execute("DELETE FROM episodes")
    await db.commit()
    _reset_calibration_state_347()
    _clean_sidecars_347()


def _write_sidecar_347(*, scoring_version, embedding_dim=8):
    admin_handlers._save_calibration_state(
        embedding_dim=embedding_dim,
        embedding_model="bge-m3",
        global_threshold=0.61,
        agent_thresholds={AGENT_347: 0.58},
        global_fused_gate=0.45,
        fused_gate_signal="confidence",
        agent_betas={AGENT_347: 2.0},
        scoring_version=scoring_version,
    )


def _truncate_sidecar_347():
    with open(_sidecar_347(), "rb+") as fh:
        fh.truncate(os.path.getsize(_sidecar_347()) - 1)


@pytest.mark.asyncio
async def test_a_stale_sidecar_is_backed_up_and_reported_347(clean_347, caplog):
    """Control: the branch one step away preserves the evidence, so what the other
    tests measure is the guard rather than the fixture."""
    await _seed_347(clean_347)
    _write_sidecar_347(scoring_version="some-older-scoring-version")
    with caplog.at_level(logging.DEBUG):
        status = await admin_handlers.ensure_calibrated_on_startup(
            auto_calibrate=False, on_model_change=True
        )
    assert status["action"] == "recalibrated_scoring", status
    assert status["sidecar_backup"], status
    assert glob.glob(glob.escape(_sidecar_347()) + ".before-*")


@pytest.mark.asyncio
async def test_an_unreadable_sidecar_is_not_reported_as_a_fresh_install_347(clean_347):
    await _seed_347(clean_347)
    _write_sidecar_347(scoring_version=utils.SCORING_VERSION)
    _truncate_sidecar_347()
    assert admin_handlers._load_calibration_state() is None

    status = await admin_handlers.ensure_calibrated_on_startup(
        auto_calibrate=False, on_model_change=True
    )
    assert status["action"] != "initial", (
        f"an unreadable sidecar was reported as a first-ever boot: {status}"
    )


@pytest.mark.asyncio
async def test_an_unreadable_sidecar_is_backed_up_before_it_is_replaced_347(clean_347):
    await _seed_347(clean_347)
    _write_sidecar_347(scoring_version=utils.SCORING_VERSION)
    corrupt = open(_sidecar_347(), "rb").read()[:-1]
    with open(_sidecar_347(), "wb") as fh:
        fh.write(corrupt)

    await admin_handlers.ensure_calibrated_on_startup(auto_calibrate=False, on_model_change=True)
    backups = glob.glob(glob.escape(_sidecar_347()) + ".before-*")
    surviving = [b for b in backups if open(b, "rb").read() == corrupt]
    assert surviving, (
        "the unreadable sidecar was replaced with no backup; on disk now: "
        f"{json.loads(open(_sidecar_347()).read())}, backups={backups}"
    )


@pytest.mark.asyncio
async def test_an_unreadable_sidecar_says_so_somewhere_347(clean_347, caplog):
    await _seed_347(clean_347)
    _write_sidecar_347(scoring_version=utils.SCORING_VERSION)
    _truncate_sidecar_347()

    with caplog.at_level(logging.DEBUG):
        status = await admin_handlers.ensure_calibrated_on_startup(
            auto_calibrate=False, on_model_change=True
        )
    said = [
        (r.levelname, r.getMessage())
        for r in caplog.records
        if any(
            word in r.getMessage().lower()
            for word in ("unreadable", "corrupt", "could not read", "could not be read", "parse")
        )
    ]
    assert said, (
        f"no record at any level says the sidecar could not be read; status={status}; "
        f"records={[(r.levelname, r.getMessage()[:90]) for r in caplog.records]}"
    )


@pytest.mark.asyncio
async def test_an_operator_set_precision_override_is_recoverable_347(clean_347):
    """`agent_betas` is a `set_recall_precision` override, not a measurement, so losing
    it silently loses a stated preference."""
    await _seed_347(clean_347)
    _write_sidecar_347(scoring_version=utils.SCORING_VERSION)
    _truncate_sidecar_347()

    await admin_handlers.ensure_calibrated_on_startup(auto_calibrate=False, on_model_change=True)
    on_disk = json.loads(open(_sidecar_347()).read())
    assert on_disk.get("agent_betas", {}).get(AGENT_347) is None, (
        f"the boot parsed an unparseable sidecar; the fixture no longer holds: {on_disk}"
    )
    wanted = re.compile(
        rb'"agent_betas"\s*:\s*\{[^}]*"' + re.escape(AGENT_347).encode() + rb'"\s*:\s*2\.0'
    )
    backups = glob.glob(glob.escape(_sidecar_347()) + ".before-*")
    recoverable = [b for b in backups if wanted.search(open(b, "rb").read())]
    assert recoverable, (
        "the operator's precision override is unrecoverable: it is not in the sidecar "
        f"({on_disk}) and no backup carries it (backups={backups})"
    )


# ==========================================================================
# bug-349 — import and merge deduplicated on content only through the v12 UNIQUE
#           index, whose creation is allowed to fail; the explicit content probe
#           existed on the preview arm alone.
#
# The first test was rewritten as an invariant (2026-09-07). The probe demanded that
# the ladder withhold SCHEMA_VERSION when the CREATE fails; that cannot be the fix,
# because `current` would stay at 11 and every later boot would re-run the v11 step --
# a full FTS rebuild over the corpus -- for as long as the collision lasts. The defect
# is permanence, so the invariant is recoverability.
# ==========================================================================
SHARED_349 = "shared line: the deploy window is Friday"


async def _boot_349(monkeypatch, dbfile):
    monkeypatch.setattr(database, "DB_PATH", dbfile)
    database._db = None  # orphan-waiver: entry-point reset (tests/test_harness_2500.py:41-49)
    return await database.get_db()


async def _shutdown_349():
    if database._db is not None:
        await database._db.close()
        database._db = None


async def _index_names_349(db) -> set:
    rows = await db.execute_fetchall(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'memories'"
    )
    return {r[0] for r in rows}


async def _legacy_db_with_locked_duplicates_349(monkeypatch, dbfile):
    """A pre-v12 database holding two LOCKED rows with identical
    (agent_id, project_id, channel, content) -- the state the v12 migration cannot
    collapse, because it never deletes a locked row."""
    db = await _boot_349(monkeypatch, dbfile)
    await db.execute("DROP " + "INDEX IF EXISTS idx_memories_dedup_content")
    await db.execute("DELETE FROM schema_version")
    await db.execute("INSERT INTO schema_version (version) VALUES (11)")
    for _ in range(2):
        await db.execute(
            "INSERT INTO memories (agent_id, project_id, channel, content, source, "
            "timestamp, locked) VALUES ('legacy', '', '', ?, '{}', '', 1)",
            (SHARED_349,),
        )
    await db.commit()
    await _shutdown_349()
    return await _boot_349(monkeypatch, dbfile)  # the ladder runs again over that state


@pytest.mark.asyncio
async def test_a_blocked_dedup_index_is_created_once_the_blocker_is_gone_349(tmp_path, monkeypatch):
    saved = database._db
    dbfile = str(tmp_path / "legacy.db")
    try:
        db = await _legacy_db_with_locked_duplicates_349(monkeypatch, dbfile)
        assert "idx_memories_dedup_content" not in await _index_names_349(db), (
            "the fixture no longer reproduces a blocked index"
        )
        stamped = (await db.execute_fetchall("SELECT MAX(version) FROM schema_version"))[0][0]

        # The operator clears the collision the way check_health's duplicate_content
        # repair would: unlock the pair and drop the later row.
        await db.execute("UPDATE memories SET locked = 0 WHERE agent_id = 'legacy'")
        await db.execute(
            "DELETE FROM memories WHERE agent_id = 'legacy' AND id NOT IN "
            "(SELECT MIN(id) FROM memories WHERE agent_id = 'legacy')"
        )
        await db.commit()
        await _shutdown_349()

        db = await _boot_349(monkeypatch, dbfile)
        assert "idx_memories_dedup_content" in await _index_names_349(db), (
            "the dedup index the ladder could not create is still missing after the "
            f"collision was cleared and the server restarted; schema_version={stamped}, "
            f"indexes={sorted(await _index_names_349(db))}"
        )
    finally:
        await _shutdown_349()
        database._db = saved


@pytest.mark.asyncio
async def test_import_is_idempotent_without_the_dedup_index_349(tmp_path, monkeypatch):
    """The import tool is advertised idempotentHint."""
    saved = database._db
    session.reset_pauses_for_tests()
    monkeypatch.setattr(config, "EXPORT_DIR", "")
    try:
        db = await _legacy_db_with_locked_duplicates_349(monkeypatch, str(tmp_path / "legacy.db"))
        assert "idx_memories_dedup_content" not in await _index_names_349(db), "precondition"

        export = tmp_path / "one.jsonl"
        export.write_text(
            json.dumps({"_type": "header", "version": "cpersona-export/1.0"})
            + "\n"
            + json.dumps(
                {
                    "_type": "memory",
                    "agent_id": "importee",
                    "project_id": "",
                    "channel": "",
                    "msg_id": "",
                    "content": "a line imported twice",
                    "source": "{}",
                    "timestamp": "",
                    "metadata": "{}",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        first = await admin.do_import_memories(str(export))
        preview = await admin.do_import_memories(str(export), dry_run=True)
        second = await admin.do_import_memories(str(export))
        rows = await db.execute_fetchall(
            "SELECT COUNT(*) FROM memories WHERE agent_id = 'importee'"
        )
        assert rows[0][0] == 1, (
            f"re-importing the same export duplicated the row (count={rows[0][0]}). "
            f"first={first}, preview={preview}, second={second} -- the preview predicted "
            "a skip the real run did not perform"
        )
    finally:
        await _shutdown_349()
        database._db = saved


@pytest.mark.asyncio
async def test_merge_strategy_skip_without_the_dedup_index_349(tmp_path, monkeypatch):
    saved = database._db
    session.reset_pauses_for_tests()
    try:
        db = await _legacy_db_with_locked_duplicates_349(monkeypatch, str(tmp_path / "legacy.db"))
        assert "idx_memories_dedup_content" not in await _index_names_349(db), "precondition"

        for agent in ("src", "dst"):
            await db.execute(
                "INSERT INTO memories (agent_id, project_id, channel, content, source, "
                "timestamp) VALUES (?, '', '', 'shared line', '{}', '')",
                (agent,),
            )
        await db.commit()

        preview = await admin.do_merge_memories("src", "dst", dry_run=True)
        real = await admin.do_merge_memories("src", "dst")
        rows = await db.execute_fetchall(
            "SELECT COUNT(*) FROM memories WHERE agent_id = 'dst' AND content = 'shared line'"
        )
        assert rows[0][0] == 1, (
            f"strategy='skip' copied a duplicate into the target (count={rows[0][0]}). "
            f"preview={preview}, real={real}"
        )
    finally:
        await _shutdown_349()
        database._db = saved


# ==========================================================================
# bug-350 — two concurrent exports to one path shared a temp file named after the
#           process, publishing a torn, mixed-agent backup under ok:true.
# ==========================================================================
ROWS_350 = 700


@pytest_asyncio.fixture
async def corpus_350():
    session.reset_pauses_for_tests()
    db = await get_db()
    for agent in ("regress.b350.A", "regress.b350.B"):
        await db.execute("DELETE FROM memories WHERE agent_id = ?", (agent,))
    await db.commit()
    for agent in ("regress.b350.A", "regress.b350.B"):
        for i in range(ROWS_350):
            await db.execute(
                "INSERT INTO memories (agent_id, content, timestamp) VALUES (?, ?, ?)",
                (agent, f"{agent} row {i} " + "x" * 80, "2026-01-01T00:00:00Z"),
            )
    await db.commit()
    yield db
    for agent in ("regress.b350.A", "regress.b350.B"):
        await db.execute("DELETE FROM memories WHERE agent_id = ?", (agent,))
    await db.commit()


@pytest.mark.asyncio
async def test_two_concurrent_exports_to_one_path_do_not_corrupt_the_backup_350(
    corpus_350, tmp_path, monkeypatch
):
    monkeypatch.setattr(config, "EXPORT_DIR", "")
    out = str(tmp_path / "backup.jsonl")

    results = await asyncio.gather(
        admin.do_export_memories("regress.b350.A", out),
        admin.do_export_memories("regress.b350.B", out),
        return_exceptions=True,
    )
    raised = [r for r in results if isinstance(r, BaseException)]

    agents = set()
    memory_records = 0
    torn = 0
    header = None
    with open(out, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                torn += 1
                continue
            if record.get("_type") == "header":
                header = record
            elif record.get("_type") == "memory":
                memory_records += 1
                agents.add(record.get("agent_id"))

    observed = (
        f"published file: header={header and {k: header[k] for k in ('agent_id', 'memory_count')}}, "
        f"{memory_records} memory records from agents {sorted(agents)}, {torn} torn line(s); "
        f"call results={results}"
    )
    assert not raised, f"an export call raised: {[repr(r) for r in raised]}; {observed}"
    assert torn == 0, f"published backup has {torn} unparseable line(s); {observed}"
    assert header is not None, f"published backup has no header line; {observed}"
    assert agents == {header["agent_id"]}, (
        f"published backup mixes agents {sorted(agents)} while its header claims "
        f"{header['agent_id']!r}; {observed}"
    )
    assert memory_records == header["memory_count"], (
        f"published backup holds {memory_records} memory records but its header declares "
        f"memory_count={header['memory_count']}; {observed}"
    )


# ==========================================================================
# bug-365 — under a pause, import and merge answered requests that could never have
#           succeeded with a success-shaped skipped response.
# ==========================================================================
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["missing", "outside", "directory", "oversized", "same", "mode", "strategy"]
)
async def test_paused_invalid_requests_return_real_errors_365(tmp_path, monkeypatch, case):
    session.reset_pauses_for_tests()
    root = tmp_path / "exports"
    root.mkdir()
    monkeypatch.setattr(config, "EXPORT_DIR", str(root))
    monkeypatch.setattr(config, "MAX_IMPORT_BYTES", 4)
    large = root / "large.jsonl"
    large.write_text("12345")
    calls = {
        "missing": (admin.do_import_memories, (str(root / "absent.jsonl"),), {}),
        "outside": (admin.do_import_memories, (str(tmp_path / "outside.jsonl"),), {}),
        "directory": (admin.do_import_memories, (str(root),), {}),
        "oversized": (admin.do_import_memories, (str(large),), {}),
        "same": (admin.do_merge_memories, ("same", "same"), {}),
        "mode": (admin.do_merge_memories, ("a", "b"), {"mode": "bogus"}),
        "strategy": (admin.do_merge_memories, ("a", "b"), {"strategy": "bogus"}),
    }
    fn, args, kwargs = calls[case]
    control = await fn(*args, **kwargs)
    assert control["ok"] is False, control
    session.pause_for(session.TRANSPORT_KEY, False, 60)
    try:
        paused = await fn(*args, **kwargs)
        preview = await fn(*args, **kwargs, dry_run=True)
    finally:
        session.reset_pauses_for_tests()
    assert preview == control, (preview, control)
    assert paused == control, {"case": case, "unpaused": control, "paused": paused}


# ==========================================================================
# bug-367 — the by-id write handlers told a caller, in their error strings, whether a
#           row it may not see exists and whether it is locked.
# ==========================================================================
@pytest.mark.asyncio
async def test_foreign_ids_and_lock_states_are_indistinguishable_367(tmp_path):
    session.reset_pauses_for_tests()
    db = await get_db()
    await db.execute("DELETE FROM memories WHERE agent_id = 'b367.alpha'")
    ids = []
    for locked in (0, 1):
        cur = await db.execute(
            "INSERT INTO memories (agent_id, content, source, timestamp, locked) "
            "VALUES ('b367.alpha', ?, '{}', '2026-01-01', ?)",
            (f"foreign row {locked}", locked),
        )
        ids.append(cur.lastrowid)
    await db.commit()

    path = tmp_path / "acl.json"
    path.write_text(
        json.dumps(
            {"clients": [{"client_id": "B", "token": "probe-token", "grants": {"beta": "read-write"}}]}
        )
    )
    path.chmod(0o600)
    acl.activate(acl.load_config(str(path)))
    token = acl.set_principal(acl.Principal("B"))
    observed = {}
    try:
        for name in ("delete_memory", "update_memory", "lock_memory", "unlock_memory"):
            handler = getattr(admin_handlers, f"do_{name}")

            async def adapter(arguments, _handler=handler):
                return await _handler(**arguments)

            guarded = acl._wrap(name, adapter)
            responses = []
            for memory_id in [*ids, 999999]:
                arguments = {"memory_id": memory_id, "agent_id": "beta"}
                if name == "update_memory":
                    arguments["content"] = "replacement"
                response = await guarded(arguments)
                assert response["ok"] is False, response
                responses.append(re.sub(r"Memory \d+", "Memory <id>", response["error"]))
            observed[name] = responses
        rows = await db.execute_fetchall(
            "SELECT content, locked FROM memories WHERE agent_id = 'b367.alpha' ORDER BY id"
        )
        assert [tuple(row) for row in rows] == [("foreign row 0", 0), ("foreign row 1", 1)]
        assert all(len(set(errors)) == 1 for errors in observed.values()), observed
    finally:
        acl.reset_principal(token)
        acl.activate(None)
        session.reset_pauses_for_tests()
        await db.execute("DELETE FROM memories WHERE agent_id = 'b367.alpha'")
        await db.commit()


@pytest.mark.asyncio
async def test_an_unscoped_caller_still_gets_the_specific_message_367():
    """The ambiguity is owed to a caller that named an agent. An unscoped call has no
    ownership predicate to hide behind, so collapsing it would remove diagnosis without
    closing any boundary."""
    session.reset_pauses_for_tests()
    db = await get_db()
    await db.execute("DELETE FROM memories WHERE agent_id = 'b367.solo'")
    cur = await db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp, locked) "
        "VALUES ('b367.solo', 'a locked row', '{}', '2026-01-01', 1)"
    )
    locked_id = cur.lastrowid
    await db.commit()
    try:
        assert "locked" in (await admin.do_delete_memory(locked_id))["error"]
        assert "not found" in (await admin.do_delete_memory(999999))["error"]
    finally:
        await db.execute("DELETE FROM memories WHERE agent_id = 'b367.solo'")
        await db.commit()
