"""The dashboard is installable as an app (2.6.16): the manifest, a service worker
that keeps nothing, and the icons, served without the token and allowed by the
page's content security policy.

The worker is checked for what it must not do: store a response. A worker that
cached would leave memories in the browser for someone else at the machine to read.
"""

import json
import re

from cpersona.dashboard import web


def test_the_manifest_names_the_app_and_every_icon_it_lists_is_served():
    m = json.loads(web._static("manifest.webmanifest"))
    assert m["name"] == "CPersona Dashboard" and m["display"] == "standalone"
    # Relative, so the remote dashboard under /dashboard/ is its own app.
    assert m["start_url"] == "./" and m["scope"] == "./"
    sizes = {i["sizes"] for i in m["icons"]}
    assert {"192x192", "512x512"} <= sizes
    assert any(i.get("purpose") == "maskable" for i in m["icons"])
    for icon in m["icons"]:
        assert "/" + icon["src"] in web._STATIC, icon["src"]
        assert len(web._static(icon["src"])) > 100


def test_the_install_files_are_static_and_typed():
    types = {path: ctype for path, (_f, ctype) in web._STATIC.items()}
    assert types["/manifest.webmanifest"] == "application/manifest+json"
    assert types["/sw.js"].startswith("text/javascript")
    assert all(types[p] == "image/png" for p in types if p.endswith(".png"))


def test_the_policy_lets_the_page_load_its_manifest_and_worker():
    csp = web._HEADERS["Content-Security-Policy"]
    assert "manifest-src 'self'" in csp and "worker-src 'self'" in csp
    assert "default-src 'none'" in csp


def test_the_worker_stores_nothing():
    sw = web._static("sw.js").decode()
    assert "fetch" in sw
    assert not re.search(r"\bcaches\b|cache\.put|respondWith|indexedDB|localStorage", sw), sw


def test_the_page_links_the_manifest_and_icons_relatively():
    html = web._static("index.html").decode()
    assert '<link rel="manifest" href="manifest.webmanifest">' in html
    assert 'href="/' not in html  # an absolute address would miss the remote /dashboard/ prefix
    assert 'navigator.serviceWorker.register("sw.js")' in web._static("app.js").decode()
