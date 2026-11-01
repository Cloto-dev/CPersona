"""The dashboard's colour tokens meet WCAG 2.2 contrast in both themes (2.6.16).

Read from app.css itself, so a token changed to a colour that fails is caught here
rather than by eye: text tokens against every surface they sit on (SC 1.4.3, 4.5:1),
and the bars and legend lines that carry information against what is behind them
(SC 1.4.11, 3:1). The memory network follows the theme (2.6.17): its canvas reads
the --net-* tokens, so its ground, marks, names and legend lines are checked per
theme against that ground, as the canvas and the legend draw them.
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
# The --net-* tokens the canvas reads; each must be declared in both themes.
NET_DRAWN = ("ground", "dot", "dot-bold", "on", "ink", "accent", "label", "label-strong", "hub", "hub-ink")
SURFACES = ("base", "recessed", "raised", "accent-soft")


def test_the_tokens_were_read():
    for theme in (LIGHT, DARK):
        assert set(TEXT + SURFACES + ("bar", "mark", "on-accent")) <= set(theme), theme
        assert {f"net-{k}" for k in NET_DRAWN} <= set(theme), theme
    assert DARK["net-ground"] == "#0f131c" and LIGHT["net-ground"] == LIGHT["base"]


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


def test_the_canvas_draws_with_the_net_tokens():
    # Every colour the network draws comes from the theme's tokens, none fixed in the script.
    drawn = re.search(r"const NET = Object\.fromEntries\(\[([^\]]*)\]", JS).group(1)
    assert set(re.findall(r'"([a-z-]+)"', drawn)) == set(NET_DRAWN)
    start = JS.index("function startGraph(")
    graph = JS[start : JS.index("\nfunction ", start + 1)]
    assert not re.findall(r'"#[0-9a-fA-F]{3,6}"', graph), re.findall(r'"#[0-9a-fA-F]{3,6}"', graph)


@pytest.mark.parametrize("theme_name", ["light", "dark"])
def test_the_network_reads_on_its_ground(theme_name):
    theme = LIGHT if theme_name == "light" else DARK
    ground = theme["net-ground"]
    # The heading, hints and panels over the canvas use the page's text tokens.
    for fg in ("text", "muted", "faint", "net-label", "net-label-strong", "net-on"):
        assert ratio(theme[fg], ground) >= 4.5, fg
    # Memories and agents are marks that carry information: 3:1.
    for mark in ("net-dot", "net-dot-bold", "net-hub", "net-accent"):
        assert ratio(theme[mark], ground) >= 3, mark


@pytest.mark.parametrize("theme_name", ["light", "dark"])
def test_the_legend_draws_each_line_as_the_canvas_does_and_visibly(theme_name):
    theme = LIGHT if theme_name == "light" else DARK
    ground = theme["net-ground"]
    legend = {k: (c, float(a or 1)) for ks, c, a in re.findall(
        r"((?:\.k-[a-z]+(?:, )?)+) \{ stroke: var\(--([a-z-]+)\);(?: stroke-opacity: ([0-9.]+);)? \}", CSS)
        for k in re.findall(r"\.k-([a-z]+)", ks)}
    # Declared relations in the canvas's accent; the lines drawn in its ink use the ink.
    assert legend["declared"][0] == "net-accent"
    for kind in ("co", "part", "structure"):
        assert legend[kind][0] == "net-ink", kind
    for kind, (token, alpha) in legend.items():
        assert ratio(over(theme[token], alpha, ground), ground) >= 3, (kind, token, alpha)


@pytest.mark.parametrize("theme_name", ["light", "dark"])
def test_the_similarity_swatch_reads_on_the_panel(theme_name):
    # The settings panel's swatch for similar lines runs between two hues; both ends
    # hold 3:1 on the panel it sits on.
    theme = LIGHT if theme_name == "light" else DARK
    for end in ("net-grad-a", "net-grad-b"):
        assert ratio(theme[end], theme["raised"]) >= 3, end


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
