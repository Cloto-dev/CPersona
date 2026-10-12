"""The project notice: fetched only on the published site, placed by width, and only ever shown as text.

docs/javascripts/notice.js asks the project's website for a notice, and that request is how the
documentation's page views are counted. So it must not run on previews or local builds (they would count
or fail), must say which page and language it is, must send no cookie, and must put what it receives into
the page as text: the notice comes over the network, so a heading carrying markup stays a heading, and a
link that is not https: or mailto: is not shown at all. It is executed here in node against a stubbed
document.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "docs" / "javascripts" / "notice.js"

RUNNER = r"""
const vm = require("vm"), fs = require("fs");
const [file, optionsJson] = process.argv.slice(1);
const o = JSON.parse(optionsJson);
const fetched = [];
const el = (name) => ({ name, children: [], attrs: {}, appendChild(c) { c.parentNode = this; this.children.push(c); }, setAttribute(k, v) { this.attrs[k] = v; } });
const toc = el("toc"), article = el("article");
const document = {
  documentElement: { getAttribute: () => o.lang },
  querySelector: (s) => (s.includes("md-sidebar--secondary") ? (o.noToc ? null : toc) : s.includes("md-content__inner") ? article : null),
  createElement: (tag) => el(tag),
};
const window = {
  fetch: (url, init) => { fetched.push({ url, init }); return o.fail ? Promise.reject(new Error("offline")) : Promise.resolve({ ok: o.status !== 500, json: () => Promise.resolve(o.reply) }); },
  matchMedia: (q) => ({ matches: !!o.wide, media: q, addEventListener() {} }),
};
const sandbox = { window, document, location: { hostname: o.host, pathname: o.path }, fetch: window.fetch, encodeURIComponent };
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(file, "utf8"), sandbox);
setTimeout(() => {
  const box = [...toc.children, ...article.children][0];
  const describe = (b) => b && { in: b.parentNode.name, cls: b.className, aria: b.attrs["aria-label"], parts: b.children.map((c) => ({ tag: c.name, cls: c.className, text: c.textContent, href: c.href })) };
  console.log(JSON.stringify({ fetched, box: box ? describe(box) : null }));
}, 20);
"""

REPLY = {
    "label": "From the CPersona project",
    "notice": {
        "id": "sponsors",
        "heading": "Support CPersona's development",
        "text": "CPersona is free, open-source software.",
        "link": "Sponsor on GitHub",
        "href": "https://github.com/sponsors/Cloto-dev",
    },
}
PUBLISHED = "cloto-dev.github.io"


def _run(**options) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; the docs workflow runs the JavaScript gates with it")
    base = {"host": PUBLISHED, "path": "/CPersona/getting-started/", "lang": "en", "wide": True, "reply": REPLY}
    out = subprocess.run([node, "-e", RUNNER, str(SCRIPT), json.dumps({**base, **options})], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_the_published_site_asks_for_its_page_and_language_without_cookies():
    result = _run(path="/CPersona/ja/getting-started/", lang="ja")
    assert result["fetched"] == [
        {
            "url": "https://cpersona.cloto.dev/api/notice?page=%2FCPersona%2Fja%2Fgetting-started%2F&lang=ja",
            "init": {"credentials": "omit", "cache": "no-store"},
        }
    ]


@pytest.mark.parametrize("host", ["localhost", "cpersona-docs.example.test", "cloto-dev.github.io.example.test"])
def test_previews_and_local_builds_neither_fetch_nor_count(host):
    result = _run(host=host)
    assert result == {"fetched": [], "box": None}


def test_wide_screens_put_it_under_the_contents_and_narrow_ones_at_the_end():
    wide = _run(wide=True)
    narrow = _run(wide=False)
    no_toc = _run(wide=True, noToc=True)
    assert wide["box"]["in"] == "toc" and narrow["box"]["in"] == "article" and no_toc["box"]["in"] == "article"
    assert [p["text"] for p in wide["box"]["parts"]] == [
        "From the CPersona project",
        "Support CPersona's development",
        "CPersona is free, open-source software.",
        "Sponsor on GitHub",
    ]
    assert wide["box"]["parts"][3]["href"] == "https://github.com/sponsors/Cloto-dev"
    assert wide["box"]["aria"] == "From the CPersona project"


def test_what_comes_over_the_network_stays_text():
    hostile = {"label": "x", "notice": {**REPLY["notice"], "heading": '<img src=x onerror="alert(1)">'}}
    result = _run(reply=hostile)
    heading = result["box"]["parts"][1]
    assert heading["text"] == '<img src=x onerror="alert(1)">' and heading["tag"] == "p"


@pytest.mark.parametrize("href", ["javascript:alert(1)", "http://example.test/", "//example.test/", ""])
def test_a_link_that_is_not_https_or_mailto_shows_nothing(href):
    result = _run(reply={"label": "x", "notice": {**REPLY["notice"], "href": href}})
    assert result["box"] is None


@pytest.mark.parametrize("options", [{"fail": True}, {"status": 500}, {"reply": None}, {"reply": {"label": "x"}}])
def test_no_answer_shows_nothing(options):
    assert _run(**options)["box"] is None


def test_the_theme_loads_the_script():
    text = (REPO_ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    block = text[text.index("\nextra_javascript:\n") :].split("\n\n", 1)[0]
    assert "  - javascripts/notice.js" in block
