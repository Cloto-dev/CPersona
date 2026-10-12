"""Search and the website link follow the language of the page.

scripts/docs_by_language.py splits the theme's single search index into the
English and Japanese entries and gives the Japanese pages the website's
Japanese address; overrides/partials/search-language.js makes a Japanese page
request the Japanese index. These tests hold each half, and the head script
is executed in node against a stubbed XMLHttpRequest.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import shutil
import subprocess
from types import SimpleNamespace

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "overrides" / "partials" / "search-language.js"


def _hook():
    spec = importlib.util.spec_from_file_location("docs_by_language_under_test", REPO_ROOT / "scripts" / "docs_by_language.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_index_is_split_by_language(tmp_path):
    (tmp_path / "search").mkdir()
    docs = [
        {"location": "", "title": "Home"},
        {"location": "getting-started/#install", "title": "Install"},
        {"location": "japanese-notes/", "title": "An English page whose name starts with ja"},
        {"location": "ja/", "title": "ホーム"},
        {"location": "ja/getting-started/#install", "title": "インストール"},
    ]
    config = {"lang": ["en", "ja"], "separator": "[\\s]+"}
    (tmp_path / "search" / "search_index.json").write_text(json.dumps({"config": config, "docs": docs}), encoding="utf-8")
    assert _hook().split_index(tmp_path) == (3, 2)
    en = json.loads((tmp_path / "search" / "search_index.json").read_text(encoding="utf-8"))
    ja = json.loads((tmp_path / "search" / "search_index.ja.json").read_text(encoding="utf-8"))
    assert [d["title"] for d in en["docs"]] == ["Home", "Install", "An English page whose name starts with ja"]
    assert [d["title"] for d in ja["docs"]] == ["ホーム", "インストール"]
    assert en["config"] == ja["config"] == config


def test_a_build_without_an_index_is_left_alone(tmp_path):
    assert _hook().split_index(tmp_path) is None


def test_only_japanese_pages_get_the_japanese_website():
    hook = _hook()
    html = '<a href="https://cpersona.cloto.dev/" class="md-tabs__link">'
    ja = SimpleNamespace(file=SimpleNamespace(src_uri="index.ja.md"))
    en = SimpleNamespace(file=SimpleNamespace(src_uri="index.md"))
    assert hook.on_post_page(html, page=ja) == html.replace("cpersona.cloto.dev/", "cpersona.cloto.dev/ja/")
    assert hook.on_post_page(html, page=en) == html


def test_the_hook_runs_and_the_page_carries_the_script():
    assert "  - scripts/docs_by_language.py\n" in (REPO_ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    assert '{% include "partials/search-language.js" %}' in (REPO_ROOT / "overrides" / "main.html").read_text(encoding="utf-8")


RUNNER = r"""
const vm = require("vm"), fs = require("fs");
const [file, lang, url] = process.argv.slice(1);
let opened = null;
function XMLHttpRequest() {}
XMLHttpRequest.prototype.open = function (method, u) { opened = u; };
const sandbox = { XMLHttpRequest, document: { documentElement: { getAttribute: () => lang } } };
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(file, "utf8"), sandbox);
new sandbox.XMLHttpRequest().open("GET", url);
console.log(opened);
"""


@pytest.mark.parametrize(
    "lang, url, opened",
    [
        ("ja", "https://example.test/Site/search/search_index.json", "https://example.test/Site/search/search_index.ja.json"),
        ("ja", "https://example.test/Site/2.6/search/search_index.json", "https://example.test/Site/2.6/search/search_index.ja.json"),
        ("en", "https://example.test/Site/search/search_index.json", "https://example.test/Site/search/search_index.json"),
        ("ja", "https://example.test/Site/sitemap.xml", "https://example.test/Site/sitemap.xml"),
    ],
)
def test_a_japanese_page_requests_the_japanese_index(lang, url, opened):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; the docs workflow runs the JavaScript gates with it")
    out = subprocess.run([node, "-e", RUNNER, str(SCRIPT), lang, url], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == opened
