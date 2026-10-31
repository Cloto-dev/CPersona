"""The dashboard's Japanese words are the ones chosen in 2.6.16.

Labels that read as literal translations or coinages were replaced: an entity is
エンティティ, the tree is フォルダ, and a count is not split from its counter word
across two lines. The Japanese pages and the Japanese docs that describe them are
read here, so a retired word put back in either is caught.
"""

from __future__ import annotations

import re
from pathlib import Path

from cpersona.dashboard import web

SCRIPT = web._static("app.js").decode()
JA = SCRIPT[SCRIPT.index("\n  ja: {") : SCRIPT.index("\n};", SCRIPT.index("\n  ja: {"))]
DOCS = Path(__file__).resolve().parent.parent / "docs"

# Each retired word, with the word that replaced it. The counts' labels are retired
# only as labels of their own (under a number on another line); "3 件の記憶" in one
# string keeps the counter beside its number and stays.
RETIRED = {
    "実体": "エンティティ",
    "たどる": "フォルダ",
    "月間記憶グラフ": "月別の記憶数",
    "原記憶": "元のデータ (JSON)",
    '"件の記憶"': '"記憶"',
    '"件のエピソード"': '"エピソード"',
    '"件の実体"': '"エンティティ"',
}


def test_the_japanese_strings_were_found():
    assert 'overview: "概要"' in JA and "\n  en: {" not in JA


def test_no_retired_word_is_shown():
    found = {old: new for old, new in RETIRED.items() if old in JA}
    assert not found, found


def test_the_replacements_are_used():
    assert 'entities: "エンティティ"' in JA
    assert 'browse: "フォルダ"' in JA
    assert 'byMonth: "月別の記憶数"' in JA
    assert 'raw: "元のデータ (JSON)"' in JA
    assert re.search(
        r'nMemories: "記憶", nEpisodes: "エピソード", nEntities: "エンティティ"', JA
    )


def test_the_japanese_docs_call_an_entity_by_the_same_word():
    found = {
        p.name: p.read_text(encoding="utf-8").count("実体")
        for p in DOCS.glob("*.ja.md")
    }
    assert not {k: v for k, v in found.items() if v}, found
