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
