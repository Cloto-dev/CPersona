"""The Japanese pages join a wrapped line between two Japanese characters, and nothing else.

scripts/docs_ja_lines.py runs as a build hook on the Japanese copies. A line
break inside a paragraph is white space, which between two Japanese characters
is a visible gap in Chrome and Safari. These tests hold both halves of the
contract: the breaks it must remove, and the structure it must leave alone,
since joining two lines that belong to different blocks would change the page.
"""

from __future__ import annotations

import importlib.util
import pathlib
from types import SimpleNamespace

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _hook():
    spec = importlib.util.spec_from_file_location("docs_ja_lines_under_test", REPO_ROOT / "scripts" / "docs_ja_lines.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "source, joined",
    [
        ("Claude を\nはじめとする", "Claude をはじめとする"),
        ("- 課金されます。自前の\n  埋め込みサーバー", "- 課金されます。自前の埋め込みサーバー"),
        ("> README や\n> 同梱 skill", "> README や同梱 skill"),
        ("**記憶**\nを与えます", "**記憶**を与えます"),
        ("詳しくは\n[設定](configuration.md)", "詳しくは[設定](configuration.md)"),
        ("`store` が返す\n`id` は", "`store` が返す\n`id` は"),  # the next line starts with Latin text
        ("決定論的です。較正は  \n次の", "決定論的です。較正は  \n次の"),  # a hard break is kept
    ],
)
def test_what_is_joined(source, joined):
    assert _hook().join_lines(source) == joined


@pytest.mark.parametrize(
    "source",
    [
        "```text\n日本語の\nコード\n```",  # inside a fence
        "## 見出し\n本文",  # after a heading
        "> 引用の\n本文",  # out of a blockquote
        "> > 二重の\n> 一重",  # into a shallower blockquote
        "項目の\n- 次の項目",  # before a list item
        "_強調_\nの後",  # an underscore: Python-Markdown would no longer close the emphasis
        "表の前の\n| 列 | 列 |",  # before a table row
        "English line\n日本語の行",  # after a line ending in Latin text
    ],
)
def test_what_is_left_alone(source):
    assert _hook().join_lines(source) == source


def test_only_japanese_pages_are_joined():
    hook = _hook()
    text = "日本語の\n行"
    ja = SimpleNamespace(file=SimpleNamespace(src_uri="getting-started.ja.md"))
    en = SimpleNamespace(file=SimpleNamespace(src_uri="getting-started.md"))
    assert hook.on_page_markdown(text, page=ja) == "日本語の行"
    assert hook.on_page_markdown(text, page=en) == text


def test_the_hook_is_registered():
    assert "  - scripts/docs_ja_lines.py\n" in (REPO_ROOT / "mkdocs.yml").read_text(encoding="utf-8")
