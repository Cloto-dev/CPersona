"""Regression tests for the 2.5.2a2 I5 (server/env) fix group.

C10 — the HTTP port and embedding-timeout env reads must route through the
bug-133 warn-and-fall-back-to-default parse instead of a bare int() that
crashes startup on a malformed value.

C2 — the no-persist controls must surface that the pause is a process-global
flag (an HTTP deployment shares one process across every connected session),
so the pause/resume/status responses carry an additive ``scope`` key.
"""

import logging
import os
import tempfile

import pytest

# Override DB path + disable embeddings BEFORE importing server modules so the
# import stays lightweight and self-contained (mirrors tests/test_no_persist.py).
_tmpdir = tempfile.mkdtemp()
os.environ["CPERSONA_DB_PATH"] = os.path.join(_tmpdir, "test_252a2_i5.db")
os.environ["CPERSONA_EMBEDDING_MODE"] = "none"

from cpersona import config  # noqa: E402
from cpersona import server  # noqa: E402
from cpersona._vendored_mcp_common import no_persist  # noqa: E402


# ============================================================
# C10 — malformed numeric env reads warn + fall back (no crash)
# ============================================================


def test_http_port_malformed_falls_back_and_warns(monkeypatch, caplog):
    """A malformed CPERSONA_HTTP_PORT must resolve to the default with a
    warning, not raise a startup-aborting ValueError (bug-133 class)."""
    monkeypatch.setenv("CPERSONA_HTTP_PORT", "80 80")
    with caplog.at_level(logging.WARNING):
        port = server._resolve_http_port()
    assert port == 8402
    assert any("CPERSONA_HTTP_PORT" in rec.getMessage() for rec in caplog.records)


def test_embedding_timeout_malformed_falls_back_and_warns(monkeypatch, caplog):
    """A malformed CPERSONA_EMBEDDING_TIMEOUT_SECS (e.g. a '30s' unit suffix)
    must resolve to the default with a warning, not raise — this read runs for
    BOTH transports whenever embeddings are enabled."""
    monkeypatch.setenv("CPERSONA_EMBEDDING_TIMEOUT_SECS", "30s")
    with caplog.at_level(logging.WARNING):
        timeout = server._resolve_embedding_timeout()
    assert timeout == 30
    assert any("CPERSONA_EMBEDDING_TIMEOUT_SECS" in rec.getMessage() for rec in caplog.records)


def test_http_port_valid_override_preserved(monkeypatch):
    """Behaviour preservation: a well-formed value still overrides the default."""
    monkeypatch.setenv("CPERSONA_HTTP_PORT", "9001")
    assert server._resolve_http_port() == 9001


def test_embedding_timeout_valid_override_preserved(monkeypatch):
    """Behaviour preservation: a well-formed value still overrides the default."""
    monkeypatch.setenv("CPERSONA_EMBEDDING_TIMEOUT_SECS", "45")
    assert server._resolve_embedding_timeout() == 45


def test_http_port_unset_uses_default(monkeypatch):
    monkeypatch.delenv("CPERSONA_HTTP_PORT", raising=False)
    assert server._resolve_http_port() == 8402


def test_embedding_timeout_unset_uses_default(monkeypatch):
    monkeypatch.delenv("CPERSONA_EMBEDDING_TIMEOUT_SECS", raising=False)
    assert server._resolve_embedding_timeout() == 30


def test_public_parse_wrappers_match_private(monkeypatch):
    """The public config.parse_int/parse_float wrappers keep bug-133 semantics."""
    monkeypatch.setenv("CPERSONA_TMP_INT", "oops")
    monkeypatch.setenv("CPERSONA_TMP_FLOAT", "nope")
    assert config.parse_int("CPERSONA_TMP_INT", 7) == 7
    assert config.parse_float("CPERSONA_TMP_FLOAT", 1.5) == 1.5
    monkeypatch.setenv("CPERSONA_TMP_INT", "12")
    assert config.parse_int("CPERSONA_TMP_INT", 7) == 12


# ============================================================
# C2 — no-persist controls expose the process-global scope
# ============================================================


@pytest.fixture(autouse=True)
def _reset_no_persist():
    """Guarantee the process-global no-persist flag is clear around each test."""
    no_persist.resume()
    yield
    no_persist.resume()


@pytest.mark.asyncio
async def test_pause_response_has_process_scope():
    result = await server.do_pause_persistence(ttl_seconds=60)
    assert "scope" in result
    assert result["scope"] == "process"


@pytest.mark.asyncio
async def test_resume_response_has_process_scope():
    result = await server.do_resume_persistence()
    assert "scope" in result
    assert result["scope"] == "process"


@pytest.mark.asyncio
async def test_status_response_has_process_scope():
    result = await server.do_persistence_status()
    assert "scope" in result
    assert result["scope"] == "process"
