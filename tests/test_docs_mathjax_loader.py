"""MathJax is fetched only by a page that has an equation.

docs/javascripts/mathjax.js used to sit beside a script tag that loaded 265 KB
of MathJax on every page; eight pages have equations. It now adds that script
itself, on the pages with an arithmatex element, once. The loader is executed
here in node against a stubbed document, the way scripts/check-lang-routing.js
executes the language router.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
LOADER = REPO_ROOT / "docs" / "javascripts" / "mathjax.js"

RUNNER = r"""
const vm = require("vm");
const fs = require("fs");
const [file, hasMath] = process.argv.slice(1);
const appended = [];
const document = {
  querySelector: (sel) => {
    if (sel === ".arithmatex") return hasMath === "1" ? {} : null;
    if (sel.startsWith("script[src=")) return appended.length ? {} : null;
    return null;
  },
  createElement: () => ({}),
  head: { appendChild: (el) => appended.push(el.src) },
};
const window = {};
const sandbox = {
  window, document,
  document$: { subscribe: (fn) => { fn(); fn(); } },  // twice: a second signal must not fetch again
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(file, "utf8"), sandbox);
console.log(JSON.stringify({ appended, configured: !!(window.MathJax && window.MathJax.tex) }));
"""


def _run(has_math: bool) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; the docs workflow runs the JavaScript gates with it")
    out = subprocess.run(
        [node, "-e", RUNNER, str(LOADER), "1" if has_math else "0"],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)


def test_a_page_without_equations_fetches_nothing():
    result = _run(has_math=False)
    assert result == {"appended": [], "configured": True}


def test_a_page_with_equations_fetches_mathjax_once():
    result = _run(has_math=True)
    assert result["configured"] is True
    assert result["appended"] == ["https://unpkg.com/mathjax@3/es5/tex-mml-chtml.js"]


def test_the_theme_no_longer_loads_mathjax_on_every_page():
    text = (REPO_ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    block = text[text.index("\nextra_javascript:\n") :].split("\n\n", 1)[0]
    assert "mathjax@3" not in block and "javascripts/mathjax.js" in block
