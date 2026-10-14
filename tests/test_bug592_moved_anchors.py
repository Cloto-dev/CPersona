"""The fixes bug-001 and bug-002 registered, pinned by behaviour (bug-592).

Both registry entries named the modules' pre-package root paths, which no longer
exist; the verifier counts an absent pattern in a missing file as fixed, so the
two entries passed whatever the code did. They are re-anchored on the package
modules, and each fix gets a test that fails when it is reverted.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from cpersona import memory_handlers as M

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _config_under(env: dict) -> dict:
    """The embedding settings a fresh process reads from ``env`` alone."""
    clean = {k: v for k, v in os.environ.items() if "EMBEDDING" not in k}
    clean.update(env)
    clean["PYTHONPATH"] = REPO
    out = subprocess.run(
        [sys.executable, "-c",
         "import json; from cpersona import config; "
         "print(json.dumps({'mode': config.EMBEDDING_MODE, 'url': config.EMBEDDING_URL}))"],
        capture_output=True, text=True, env=clean, timeout=60, check=True,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_the_generic_embedding_names_configure_embeddings():
    """bug-001: a catalog install sets EMBEDDING_MODE / EMBEDDING_HTTP_URL, not the prefixed names."""
    got = _config_under({"EMBEDDING_MODE": "http", "EMBEDDING_HTTP_URL": "http://127.0.0.1:9/embed"})
    assert got == {"mode": "http", "url": "http://127.0.0.1:9/embed"}


def test_the_prefixed_names_still_win():
    got = _config_under({
        "EMBEDDING_MODE": "http", "EMBEDDING_HTTP_URL": "http://127.0.0.1:9/a",
        "CPERSONA_EMBEDDING_MODE": "none", "CPERSONA_EMBEDDING_URL": "http://127.0.0.1:9/b",
    })
    assert got == {"mode": "none", "url": "http://127.0.0.1:9/b"}


def test_a_japanese_query_becomes_trigrams_not_one_phrase():
    """bug-002: a whitespace split left a space-less sentence as one phrase no row contains."""
    assert M._build_fts_query("記憶の検索テスト") == (
        '"記憶の" OR "憶の検" OR "の検索" OR "検索テ" OR "索テス" OR "テスト"'
    )
    assert M._build_fts_query("hello world") == '"hello" OR "world"'
