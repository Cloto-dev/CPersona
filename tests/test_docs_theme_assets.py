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


def _arc_extent(svg: str) -> tuple[float, float, float, float]:
    """Ink extent (left, right, top, bottom) of the mark: one stroked arc, butt caps.

    The C is "M x1 y1 A r r 0 1 0 x2 y2" stroked at width w: a large arc through
    the left of its circle. The circle's centre follows from the two end points;
    the ink reaches r + w/2 out on the left, top and bottom, and on the right only
    as far as the outer corner of the cap at each end, which lies on the radius.
    """
    import math

    m = re.search(
        r'd="M([\d.]+) ([\d.]+)A([\d.]+) [\d.]+ 0 1 0 ([\d.]+) ([\d.]+)"[^>]*stroke-width="([\d.]+)"', svg
    )
    assert m, "the mark is no longer a single stroked arc; update this check"
    x1, y1, r, x2, y2, w = map(float, m.groups())
    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
    half = math.dist((x1, y1), (x2, y2)) / 2
    off = math.sqrt(r * r - half * half)
    # sweep 0 with the large arc: the centre is on the side that puts the arc through the left
    ux, uy = (y2 - y1) / (2 * half), -(x2 - x1) / (2 * half)
    cx, cy = mx - ux * off, my - uy * off
    if cx > mx:
        cx, cy = mx + ux * off, my + uy * off
    outer = r + w / 2
    caps = [(cx + outer * (x - cx) / r, cy + outer * (y - cy) / r) for x, y in ((x1, y1), (x2, y2))]
    return cx - outer, max(x for x, _ in caps), cy - outer, cy + outer


def test_the_mark_is_centred():
    for name in ("logo.svg", "favicon.svg"):
        svg = (DOCS / "assets" / "cpersona" / name).read_text(encoding="utf-8")
        size = float(re.search(r'viewBox="0 0 ([\d.]+) [\d.]+"', svg).group(1))
        left, right, top, bottom = _arc_extent(svg)
        assert abs((left + right) / 2 - size / 2) < 0.1, f"{name}: the C sits {size / 2 - (left + right) / 2:.2f} units left of centre"
        assert abs((top + bottom) / 2 - size / 2) < 0.1, f"{name}: the C is off centre vertically"
