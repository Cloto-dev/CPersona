"""The call shapes the policy block writes out match the tools the server declares.

Since v6 the block says its call shapes cover everyday use, so an agent on a
client that has to look a tool up before reading its schema (Codex's code mode
looks every tool up through ALL_TOOLS) can call it without the lookup. That only
saves the lookup if the shape is right: a shape naming an argument the tool does
not take, or leaving out one it requires, sends the agent to a failed call and
then to the lookup anyway. So every `tool(...)` the block writes for a CPersona
tool must name only arguments that tool declares, and must name every argument
it requires.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from cpersona import server

ROOT = Path(__file__).resolve().parent.parent
BLOCK = re.compile(r"<!-- BEGIN cpersona-policy v\d+[^>]*-->\n.*?<!-- END cpersona-policy -->", re.S)
TOOLS = {t.name: t.inputSchema for t in server.registry._tools}
CALL = re.compile(r"`(" + "|".join(sorted(TOOLS, key=len, reverse=True)) + r")\(([^`]*)\)`")


def _shapes():
    text = (ROOT / "skills" / "cpersona-memory" / "SKILL.md").read_text(encoding="utf-8")
    found = BLOCK.findall(text)
    assert len(found) == 1
    block = found[0]
    shapes = []
    for name, args in CALL.findall(block):
        names = []
        for part in re.split(r",\s*(?![^\[\]{}]*[\]}])", args):
            part = part.strip()
            if part:
                names.append(part.split("=", 1)[0].strip())
        shapes.append((name, names))
    return shapes


def test_the_block_writes_call_shapes():
    # Guards the parametrised tests below against passing vacuously on a block
    # whose shapes the pattern no longer finds.
    assert {name for name, _ in _shapes()} >= {"reconstruct", "store", "update_memory", "archive_episode", "get_contents"}


@pytest.mark.parametrize("name,args", _shapes())
def test_every_argument_a_shape_names_is_declared(name, args):
    declared = set(TOOLS[name].get("properties", {}))
    unknown = [a for a in args if a not in declared]
    assert not unknown, f"the policy block calls {name}({', '.join(args)}) but {name} declares no {unknown}"


@pytest.mark.parametrize("name,args", _shapes())
def test_every_required_argument_is_in_the_shape(name, args):
    missing = [a for a in TOOLS[name].get("required", []) if a not in args]
    assert not missing, f"the policy block calls {name}({', '.join(args)}) without the required {missing}"
