#!/usr/bin/env python3
"""Keep site search, and the link to the website, in the language of the page.

The English and Japanese pages are built together, so the theme writes one
search index holding both, and every page searches all of it. Measured on the
published site (2026-10-12): searching "Claude Code" from a Japanese page put
the English Operations Runbook first, and an English page listed Japanese
results among its first six. The website tab (mkdocs.yml nav) has one address
for both languages, so the Japanese pages sent a Japanese reader to the
English website.

This build hook does both at the end of the build:

  * on_post_build splits search/search_index.json into the English entries
    (left in that file) and the Japanese ones (search/search_index.ja.json),
    with the same configuration in both. overrides/partials/search-language.js
    points the theme's request for the index at the Japanese file on a
    Japanese page. It runs last (priority -100), after the search plugin has
    written the index and the i18n plugin has finished with it.
  * on_post_page gives the website link on a Japanese page the website's
    Japanese address.
"""

from __future__ import annotations

import json
import pathlib

JA_INDEX = "search_index.ja.json"
WEBSITE = 'href="https://cpersona.cloto.dev/"'
WEBSITE_JA = 'href="https://cpersona.cloto.dev/ja/"'


def _is_ja(location: str) -> bool:
    return location == "ja/" or location.startswith(("ja/", "ja#"))


def split_index(site_dir: str | pathlib.Path) -> tuple[int, int] | None:
    """Write the English and Japanese indexes; return their entry counts, or None without an index."""
    path = pathlib.Path(site_dir) / "search" / "search_index.json"
    if not path.is_file():
        return None
    index = json.loads(path.read_text(encoding="utf-8"))
    docs = index.get("docs", [])
    ja = [doc for doc in docs if _is_ja(doc.get("location", ""))]
    en = [doc for doc in docs if not _is_ja(doc.get("location", ""))]
    for target, entries in ((path, en), (path.parent / JA_INDEX, ja)):
        target.write_text(json.dumps({**index, "docs": entries}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return len(en), len(ja)


def on_post_build(config, **_kwargs) -> None:
    split_index(config["site_dir"])


# What mkdocs.plugins.event_priority(-100) sets, written out so that the tests, which run without mkdocs
# installed, can import this module.
on_post_build.mkdocs_priority = -100


def on_post_page(output: str, page, **_kwargs) -> str:
    if page.file.src_uri.endswith(".ja.md"):
        return output.replace(WEBSITE, WEBSITE_JA)
    return output
