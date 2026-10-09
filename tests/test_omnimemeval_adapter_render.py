"""The OmniMemEval adapter's render: which time line each reconstruct item gets.

The adapter imports the harness and an MCP client; neither is needed to render, so both are stubbed.
"""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "benchmarks" / "omnimemeval" / "cpersona_client.py"


@pytest.fixture
def render(monkeypatch):
    pkg = types.ModuleType("omnimemeval_stub")
    pkg.__path__ = []
    base = types.ModuleType("omnimemeval_stub.base_client")
    base.env_float = lambda *a, **k: 0.0
    base.require_env = lambda *a, **k: ""
    mcp = types.ModuleType("mcp")
    mcp.ClientSession = object
    streamable = types.ModuleType("mcp.client.streamable_http")
    streamable.streamable_http_client = object
    for name, mod in {"omnimemeval_stub": pkg, "omnimemeval_stub.base_client": base, "httpx2": types.ModuleType("httpx2"),
                      "mcp": mcp, "mcp.client": types.ModuleType("mcp.client"),
                      "mcp.client.streamable_http": streamable}.items():
        monkeypatch.setitem(sys.modules, name, mod)
    spec = importlib.util.spec_from_file_location("omnimemeval_stub.cpersona_client", SRC)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.CpersonaClient.render


def test_default_item_takes_the_head_claims_time(render):
    item = {"head_ref": "mem:2", "content": "head", "claims": [{"ref": "mem:1", "as_of": "T1"}, {"ref": "mem:2", "as_of": "T2"}]}
    assert render({"items": [item]}) == ["[T2]\nhead\n"]


def test_lite_item_without_claims_takes_its_own_time(render):
    # lite folds a lone claim into the item: no claims list, the time sits on the item.
    assert render({"items": [{"head_ref": "mem:3", "content": "lite", "as_of": "T3"}]}) == ["[T3]\nlite\n"]


def test_claims_win_over_the_items_own_time(render):
    # The item's own time is the last resort; a claims list keeps deciding when it is present.
    item = {"head_ref": "mem:9", "content": "c", "as_of": "ITEM", "claims": [{"ref": "mem:4", "as_of": "CLAIM"}]}
    assert render({"items": [item]}) == ["[CLAIM]\nc\n"]


def test_no_time_anywhere_renders_the_text_alone(render):
    excerpt = {"ref": "mem:5", "content": "more"}
    assert render({"items": [{"content": "only", "excerpts": [excerpt]}]}) == ["only\n…\nmore\n"]
