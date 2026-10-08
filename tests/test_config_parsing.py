"""Regression tests for environment-variable parsing in config."""

import importlib

from cpersona import config


def test_invalid_numeric_env_values_fall_back_to_defaults(monkeypatch):
    try:
        with monkeypatch.context() as env:
            env.setenv("CPERSONA_MAX_MEMORIES", "not-a-number")
            env.setenv("CPERSONA_COSINE_FLOOR", "oops")

            reloaded = importlib.reload(config)

            assert reloaded.MAX_MEMORIES == 10000
            assert reloaded.COSINE_FLOOR == 0.20
    finally:
        importlib.reload(config)


def test_valid_numeric_env_override_still_works(monkeypatch):
    try:
        with monkeypatch.context() as env:
            env.setenv("CPERSONA_MAX_MEMORIES", "55")

            reloaded = importlib.reload(config)

            assert reloaded.MAX_MEMORIES == 55
    finally:
        importlib.reload(config)


def test_oauth_scopes_default_advertises_nothing(monkeypatch):
    """The shipped default must stay empty.

    The 401's scope is adopted by the client verbatim and forwarded to the
    authorization server, which refuses any scope it does not define with
    invalid_scope — before the user reaches a sign-in page (measured live,
    2026-08-31). This server does not enforce scopes, so a non-empty default
    advertises a value no issuer defines and breaks every connection.
    """
    try:
        with monkeypatch.context() as env:
            env.delenv("CPERSONA_OAUTH_SCOPES", raising=False)

            reloaded = importlib.reload(config)

            assert reloaded.OAUTH_SCOPES == ""
    finally:
        importlib.reload(config)


# ---------------------------------------------------------------------------
# bug-321 — the precision setting was the one door that accepted a bad value.
# ---------------------------------------------------------------------------
#
# It was read with no trim and no membership check and consumed through a dict
# lookup whose default is the balanced weight, so a trailing space and an
# unrecognised word both resolved to balanced with no setting-specific warning --
# and the readback handler re-derives the label from the applied weight, so the
# response could not distinguish an operator who asked for balanced from one whose
# strict setting was discarded. The same enum is refused loudly at the tool
# surface. The module already owned a parser for this shape.


def _reloaded_precision(monkeypatch, raw):
    with monkeypatch.context() as env:
        env.setenv("CPERSONA_RECALL_PRECISION", raw)
        reloaded = importlib.reload(config)
        return reloaded.RECALL_PRECISION, reloaded.FUSED_GATE_BETA


def test_a_precision_with_surrounding_space_is_still_that_precision(monkeypatch):
    try:
        assert _reloaded_precision(monkeypatch, "strict ") == ("strict", 2.0)
        assert _reloaded_precision(monkeypatch, " lenient") == ("lenient", 0.5)
    finally:
        importlib.reload(config)


def test_an_unrecognised_precision_says_so_and_falls_back(monkeypatch, caplog):
    try:
        with caplog.at_level("WARNING"):
            level, beta = _reloaded_precision(monkeypatch, "high")
        assert (level, beta) == ("balanced", 1.0)
        assert any("CPERSONA_RECALL_PRECISION" in r.getMessage() for r in caplog.records), (
            "an unreadable precision fell back silently, which is the defect: the "
            "readback cannot tell it apart from an operator who asked for balanced"
        )
    finally:
        importlib.reload(config)


def test_the_three_legal_precisions_still_map_to_their_weights(monkeypatch):
    try:
        assert _reloaded_precision(monkeypatch, "strict") == ("strict", 2.0)
        assert _reloaded_precision(monkeypatch, "balanced") == ("balanced", 1.0)
        assert _reloaded_precision(monkeypatch, "lenient") == ("lenient", 0.5)
    finally:
        importlib.reload(config)


def _reload_with(monkeypatch, **env):
    for name in ("CPERSONA_BLOCK_BUILD_ENABLED", "CPERSONA_BLOCK_RETRIEVAL_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return importlib.reload(config)


def test_block_reach_is_on_by_default(monkeypatch):
    try:
        with monkeypatch.context() as env:
            reloaded = _reload_with(env)
            assert reloaded.BLOCK_BUILD_ENABLED is True
            assert reloaded.BLOCK_RETRIEVAL_ENABLED is True
            reloaded.validate_block_gates()
    finally:
        importlib.reload(config)


def test_block_index_file_is_on_by_default(monkeypatch):
    """On unless a deployment says false: a registered run found recall faster
    with every answer unchanged (results-recall-latency-block-index-file.md)."""
    try:
        with monkeypatch.context() as env:
            env.delenv("CPERSONA_BLOCK_INDEX", raising=False)
            assert _reload_with(env).BLOCK_INDEX_ENABLED is True
            env.setenv("CPERSONA_BLOCK_INDEX", "false")
            assert importlib.reload(config).BLOCK_INDEX_ENABLED is False
    finally:
        importlib.reload(config)


def test_turning_construction_off_alone_turns_the_reader_off_too(monkeypatch):
    """The one switch a deployment reaches for to skip the backfill must not
    become a startup error because the reader it did not set defaults to on."""
    try:
        with monkeypatch.context() as env:
            reloaded = _reload_with(env, CPERSONA_BLOCK_BUILD_ENABLED="false")
            assert reloaded.BLOCK_BUILD_ENABLED is False
            assert reloaded.BLOCK_RETRIEVAL_ENABLED is False
            reloaded.validate_block_gates()
    finally:
        importlib.reload(config)


def test_an_explicit_reader_without_construction_is_still_refused(monkeypatch):
    try:
        with monkeypatch.context() as env:
            reloaded = _reload_with(
                env, CPERSONA_BLOCK_BUILD_ENABLED="false", CPERSONA_BLOCK_RETRIEVAL_ENABLED="true"
            )
            assert reloaded.BLOCK_RETRIEVAL_ENABLED is True
            try:
                reloaded.validate_block_gates()
            except ValueError as exc:
                assert "no block index to read" in str(exc)
            else:
                raise AssertionError("an explicit reader with no index started")
    finally:
        importlib.reload(config)


def test_the_reader_can_be_switched_off_while_construction_stays_on(monkeypatch):
    try:
        with monkeypatch.context() as env:
            reloaded = _reload_with(env, CPERSONA_BLOCK_RETRIEVAL_ENABLED="false")
            assert reloaded.BLOCK_BUILD_ENABLED is True
            assert reloaded.BLOCK_RETRIEVAL_ENABLED is False
    finally:
        importlib.reload(config)
