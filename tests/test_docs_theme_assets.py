"""The documentation serves its own typeface and mark, with the typeface's licence beside it.

mkdocs.yml turns the theme's Google Fonts off (`font: false`) and points the
logo and favicon at files under docs/assets/cpersona/. stylesheets/extra.css
loads Hanken Grotesk from the same tree. A path that stops resolving would not
fail the build: the browser falls back to a system face, or shows a broken
image, and every gate stays green. The SIL Open Font License also requires the
licence to travel with the font file.
"""

from __future__ import annotations

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCS = REPO_ROOT / "docs"


def _theme() -> str:
    text = (REPO_ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    return text[text.index("\ntheme:\n") : text.index("\nplugins:\n")]


def test_google_fonts_are_off():
    assert re.search(r"^\s+font:\s*false\s*$", _theme(), re.M), "mkdocs.yml: theme.font must be false"


def test_the_logo_and_favicon_exist():
    theme = _theme()
    for key in ("logo", "favicon"):
        m = re.search(rf"^\s+{key}:\s*(\S+)\s*$", theme, re.M)
        assert m, f"mkdocs.yml: theme.{key} is not set"
        assert (DOCS / m.group(1)).is_file(), f"theme.{key} points at docs/{m.group(1)}, which does not exist"


def test_the_typeface_resolves_and_carries_its_licence():
    css_path = DOCS / "stylesheets" / "extra.css"
    css = css_path.read_text(encoding="utf-8")
    urls = re.findall(r"@font-face\s*\{[^}]*url\(\"?([^\")]+)\"?\)", css)
    assert urls, "extra.css loads no typeface"
    for url in urls:
        font = (css_path.parent / url).resolve()
        assert font.is_file(), f"extra.css loads {url}, which does not exist"
        assert (font.parent / "OFL.txt").is_file(), f"{font.name} is served without its licence (OFL.txt) beside it"


def test_the_palette_takes_the_colours_from_the_stylesheet():
    theme = _theme()
    assert theme.count("primary: custom") == 2 and theme.count("accent: custom") == 2
    css = (DOCS / "stylesheets" / "extra.css").read_text(encoding="utf-8")
    assert "--md-primary-fg-color: #000;" in css
