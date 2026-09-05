"""The policy block shown in Getting Started is the one the skill ships.

Getting Started step 5 shows the always-loaded policy block so that a reader on
Codex, Cursor or VS Code — clients that do not load the skill — can paste it.
The skill remains the source: Gate 10c measures the block there against the
40-line budget its standard sets. Two copies of a 40-line block drift the way
any two copies do, and the reader who pastes the stale one gets a policy the
skill would refuse to write. So the docs copy, in both languages, must be
byte-identical to the skill's.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BLOCK = re.compile(r"<!-- BEGIN cpersona-policy v\d+[^>]*-->\n.*?<!-- END cpersona-policy -->", re.S)


def _block(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    found = BLOCK.findall(text)
    assert len(found) == 1, f"{path.relative_to(ROOT)}: expected exactly one policy block, found {len(found)}"
    return found[0]


@pytest.mark.parametrize("page", ["docs/getting-started.md", "docs/getting-started.ja.md"])
def test_getting_started_shows_the_block_the_skill_ships(page):
    skill = _block(ROOT / "skills" / "cpersona-memory" / "SKILL.md")
    shown = _block(ROOT / page)
    assert shown == skill, (
        f"{page} shows a policy block that differs from the one in "
        "skills/cpersona-memory/SKILL.md. The skill is the source; copy its block "
        "verbatim (markers included) into the page."
    )


def test_the_detector_sees_a_one_character_drift(tmp_path):
    """A mirror check that could not tell the copies apart would pass forever."""
    skill = _block(ROOT / "skills" / "cpersona-memory" / "SKILL.md")
    page = tmp_path / "page.md"
    page.write_text(skill.replace("agent_id", "agent-id", 1), encoding="utf-8")
    assert _block(page) != skill
