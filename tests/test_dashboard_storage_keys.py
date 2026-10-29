"""The page keeps each thing it remembers in the browser under a key of its own.

2.6.15 kept the chosen layout under "cp.layout", which is also where the network
keeps where its points were left: drawing a network overwrote the choice, and
choosing a layout discarded the points. These tests read the page's script and
pin that no key is written both as JSON and as a plain value, and that a choice
an earlier version left under the old key is still read.
"""

from __future__ import annotations

import re
from collections import defaultdict

from cpersona.dashboard import web

SCRIPT = web._static("app.js").decode()


def _constants(text: str) -> dict[str, str]:
    """`const NAME = "value"`, including names declared together after a comma."""
    return dict(re.findall(r'\b([A-Z][A-Z_]*)\s*=\s*"([^"]*)"', text))


def _writes(text: str) -> dict[str, list[str]]:
    """Each storage key, resolved through the constants, to the values written to it."""
    names = _constants(text)
    writes: dict[str, list[str]] = defaultdict(list)
    for key, value in re.findall(r"localStorage\.setItem\(\s*([^,]+?)\s*,\s*(.+?)\);", text):
        resolved = key.strip('"') if key.startswith('"') else names.get(key, key)
        writes[resolved].append(value.strip())
    return writes


def test_no_key_holds_both_json_and_a_plain_value():
    writes = _writes(SCRIPT)
    # The page writes a JSON key and plain keys; without both the check has no subject.
    assert any(all(v.startswith("JSON.stringify(") for v in vs) for vs in writes.values()), writes
    assert any(not any(v.startswith("JSON.stringify(") for v in vs) for vs in writes.values()), writes
    shared = {
        key: values
        for key, values in writes.items()
        if {v.startswith("JSON.stringify(") for v in values} == {True, False}
    }
    assert shared == {}, shared


def test_the_layout_choice_and_the_network_places_use_different_keys():
    names = _constants(SCRIPT)
    assert names["LAYOUT_MODE_KEY"] != names["LAYOUT_KEY"]
    assert "localStorage.setItem(LAYOUT_MODE_KEY, l)" in SCRIPT


def test_a_tabs_choice_left_under_the_old_key_is_carried_over():
    names = _constants(SCRIPT)
    old = re.search(r'localStorage\.getItem\("([^"]+)"\) === "tabs"', SCRIPT)
    assert old and old.group(1) == names["LAYOUT_KEY"] == "cp.layout"
    assert "localStorage.setItem(LAYOUT_MODE_KEY, chosen)" in SCRIPT
