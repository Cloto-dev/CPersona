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
