"""Site search splits a query where the script changes.

The search index segments Japanese text into words, but a query is split only
by the `separator` in mkdocs.yml. Without a split at the script change,
"埋め込みサーバー" was one term and matched nothing, while the same words with
a space between them matched 39 pages. The separator is a JavaScript regular
expression; the constructs it uses (character classes, fixed-width lookbehind
and lookahead) mean the same in Python, so it is checked here with `re`.
"""

from __future__ import annotations

import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _separator() -> re.Pattern[str]:
    text = (REPO_ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    m = re.search(r"^\s+separator:\s*'(?P<sep>[^']+)'\s*$", text, re.M)
    assert m, "mkdocs.yml: the search plugin has no separator"
    # YAML single quotes keep backslashes, so ぀ reaches the browser as a regex escape; Python's re reads it the same way.
    return re.compile(m.group("sep"))


def _split(query: str) -> list[str]:
    return [t for t in _separator().split(query) if t]


@pytest.mark.parametrize(
    "query, terms",
    [
        ("埋め込みサーバー", ["埋め込み", "サーバー"]),
        ("品質ゲート", ["品質", "ゲート"]),
        ("サーバーの設定", ["サーバー", "の設定"]),
        ("MCPサーバー", ["MCP", "サーバー"]),
        ("v2.6の変更", ["v2.6", "の変更"]),
        ("embedding server", ["embedding", "server"]),
        ("read-only", ["read", "only"]),
        ("記憶", ["記憶"]),
        ("インストール", ["インストール"]),
    ],
)
def test_queries_split_where_the_script_changes(query, terms):
    assert _split(query) == terms
