"""The dashboard's colour tokens meet WCAG 2.2 contrast in both themes (2.6.16).

Read from app.css itself, so a token changed to a colour that fails is caught here
rather than by eye: text tokens against every surface they sit on (SC 1.4.3, 4.5:1),
and the bars and legend lines that carry information against what is behind them
(SC 1.4.11, 3:1). The network is drawn on a dark ground in both themes, so the
tokens declared for it must be the dark theme's, and its legend lines are checked
against that ground as the canvas draws them.
"""

import colorsys
import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "cpersona" / "dashboard" / "static"
CSS = (STATIC / "app.css").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")


def _block(selector_pattern: str) -> str:
    m = re.search(selector_pattern + r"\s*\{([^}]*)\}", CSS, re.M)
    assert m, selector_pattern
    return m.group(1)


def _tokens(block: str) -> dict[str, str]:
    return dict(re.findall(r"--([a-z-]+):\s*(#[0-9a-fA-F]{6})", block))


LIGHT = _tokens(_block(r"^  :root"))
DARK = _tokens(_block(r"@media \(prefers-color-scheme: dark\) \{\s*:root"))
NETWORK = _tokens(_block(r"\.graph\.whole"))
NIGHT = re.search(r'const NIGHT = "(#[0-9a-f]{6})"', JS).group(1)


def _rgb(h: str) -> tuple[float, ...]:
    return tuple(int(h[i : i + 2], 16) / 255 for i in (1, 3, 5))


def _luminance(rgb: tuple[float, ...]) -> float:
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def ratio(a, b) -> float:
    la, lb = sorted((_luminance(_rgb(a) if isinstance(a, str) else a), _luminance(_rgb(b) if isinstance(b, str) else b)))
    return (lb + 0.05) / (la + 0.05)


def _hue_apart(a: str, b: str) -> float:
    ha, hb = (colorsys.rgb_to_hls(*_rgb(c))[0] * 360 for c in (a, b))
    d = abs(ha - hb) % 360
    return min(d, 360 - d)


def over(colour: str, alpha: float, ground: str) -> tuple[float, ...]:
    """A stroke of ``colour`` at ``alpha`` as it lands on ``ground``."""
    return tuple(alpha * c + (1 - alpha) * g for c, g in zip(_rgb(colour), _rgb(ground)))


TEXT = ("text", "muted", "faint", "accent")
SURFACES = ("base", "recessed", "raised", "accent-soft")


def test_the_tokens_were_read():
    for theme in (LIGHT, DARK, NETWORK):
        assert set(TEXT + SURFACES + ("bar", "mark", "on-accent")) <= set(theme), theme
    assert NIGHT == "#0f131c"


@pytest.mark.parametrize("theme_name", ["light", "dark"])
def test_every_text_colour_reads_on_every_surface(theme_name):
    theme = LIGHT if theme_name == "light" else DARK
    failing = {
        (fg, bg): round(ratio(theme[fg], theme[bg]), 2)
        for fg in TEXT
        for bg in SURFACES
        if ratio(theme[fg], theme[bg]) < 4.5
    }
    assert not failing, failing


@pytest.mark.parametrize("theme_name", ["light", "dark"])
def test_selected_text_marks_and_bars_hold_their_contrast(theme_name):
    theme = LIGHT if theme_name == "light" else DARK
    # A pressed segment is filled with the accent and lettered in on-accent.
    assert ratio(theme["on-accent"], theme["accent"]) >= 4.5
    # A search match keeps the text over it readable, and does not look like the
    # selection: the first palette row is selected and its match was invisible when
    # both used the accent's tint. Their lightness can be close, so they differ in hue.
    assert ratio(theme["text"], theme["mark"]) >= 4.5
    assert _hue_apart(theme["mark"], theme["accent-soft"]) >= 90
    # A bar carries a count: 3:1 against the card it sits on and the track behind it.
    assert ratio(theme["bar"], theme["raised"]) >= 3
    assert ratio(theme["bar"], theme["recessed"]) >= 3


def test_the_network_takes_the_dark_tokens_and_reads_on_its_ground():
    assert {k: DARK[k] for k in NETWORK} == NETWORK
    for fg in ("text", "muted", "faint"):
        assert ratio(NETWORK[fg], NIGHT) >= 4.5, fg


def test_the_legend_draws_each_line_as_the_canvas_does_and_visibly():
    # The canvas: declared relations in pink, every other line in the blue ink.
    assert 'accent = "#ffd9ef"' in JS and 'ink = "170,200,255"' in JS
    ink = "#aac8ff"
    legend = {k: (c, float(a or 1)) for k, c, a in re.findall(
        r"\.k-([a-z]+)(?:, \.k-[a-z]+)? \{ stroke: (#[0-9a-f]{6});(?: stroke-opacity: ([0-9.]+);)? \}", CSS)}
    assert legend["declared"][0] == "#ffd9ef"
    for kind in ("co", "structure"):
        assert legend[kind][0] == ink, kind
    for kind, (colour, alpha) in legend.items():
        assert ratio(over(colour, alpha, NIGHT), NIGHT) >= 3, (kind, colour, alpha)


@pytest.mark.parametrize("theme_name", ["light", "dark"])
def test_the_side_panels_headings_read_on_the_side_panel(theme_name):
    # The group headings and the folder tree's heading are quieter than the views
    # under them, but still text: 4.5:1 on the side panel they sit on. In the light
    # theme there is no room below the faint token, so the label may not be lighter.
    theme = LIGHT if theme_name == "light" else DARK
    assert ratio(theme["label"], theme["recessed"]) >= 4.5
    assert ratio(theme["label"], theme["recessed"]) <= ratio(theme["muted"], theme["recessed"])
    assert re.search(r"\.cnav-h \{[^}]*color: var\(--label\)", CSS)
    assert re.search(r"\.section-label \{[^}]*color: var\(--label\)", CSS)
