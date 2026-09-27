"""Scene isolation in the Track B runner (``--isolate_scenes``).

LongMemEval pools about 500 scenes into one store. Isolated, each scene's history is stored in its own channel and a
query recalls inside its own scene: the haystack a caller of ``recall`` has over their own memory. Pooled, there is one
channel and exact-duplicate sessions collapse, as they do in production. Both halves are tested: where a record is
stored, and where a query looks.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "benchmarks"


def _load(name: str, filename: str):
    if str(BENCH) not in sys.path:
        sys.path.insert(0, str(BENCH))
    spec = importlib.util.spec_from_file_location(name, BENCH / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


runner = _load("benchmark_trackb_lmeb_scene_isolation", "benchmark_trackb_lmeb.py")

# Scene id = the first two underscore parts of an id (runner.get_scene_id). The same session text sits in two scenes.
CORPUS = [
    {"id": "sceneA_1_d0", "title": "", "text": "a session both scenes contain"},
    {"id": "sceneA_1_d1", "title": "", "text": "a session only scene A contains"},
    {"id": "sceneB_2_d0", "title": "", "text": "a session both scenes contain"},
]


class _Encoder:
    """Deterministic unit vectors (sum of code points picks the axis), enough for the store path."""

    def encode(self, texts, normalize_embeddings=True, show_progress_bar=False, **kw):
        out = np.zeros((len(texts), 8), dtype=np.float32)
        for i, t in enumerate(texts):
            out[i, sum(map(ord, t)) % 8] = 1.0
        return out


async def _rows():
    from cpersona.database import get_db

    db = await get_db()
    async with db.execute(
        "SELECT msg_id, channel FROM memories WHERE agent_id = ? ORDER BY msg_id", (runner.AGENT_ID,)
    ) as cur:
        return {r[0]: r[1] for r in await cur.fetchall()}


async def _clear():
    from cpersona.database import get_db

    db = await get_db()
    await db.execute("DELETE FROM memories WHERE agent_id = ?", (runner.AGENT_ID,))
    await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("isolate", [True, False])
async def test_store_puts_each_scene_in_its_own_channel(isolate):
    # Isolated: every record carries its scene as the channel, and a session two scenes share is stored once per
    # scene. Pooled: one channel, and the shared session collapses to its first copy (the shipped dedup).
    await _clear()
    try:
        await runner.store_corpus(None, None, _Encoder(), CORPUS, isolate_scenes=isolate)
        got = await _rows()
    finally:
        await _clear()
    if isolate:
        assert got == {"sceneA_1_d0": "sceneA_1", "sceneA_1_d1": "sceneA_1", "sceneB_2_d0": "sceneB_2"}, got
    else:
        assert got == {"sceneA_1_d0": "", "sceneA_1_d1": ""}, got


@pytest.mark.asyncio
async def test_a_query_recalls_inside_its_own_scene(tmp_path):
    # Isolated: every recall of the query passes the query's scene as the channel. Pooled: no channel is passed.
    calls = []

    class _Server:
        async def do_recall(self, **kw):
            calls.append(kw)
            return {"messages": []}

    (tmp_path / "queries.jsonl").write_text(json.dumps({"id": "sceneB_2_q0", "text": "which session?"}) + "\n")
    (tmp_path / "qrels.tsv").write_text("sceneB_2_q0\tsceneB_2_d0\t1\n")
    sub = {"name": "t", "queries": str(tmp_path / "queries.jsonl"), "qrels": str(tmp_path / "qrels.tsv"),
           "candidates": ""}
    class _Client:
        def preload(self, texts, vectors):
            pass

    await runner.run_subtask(_Server(), _Client(), _Encoder(), sub, 3, recall_limit=10, isolate_scenes=True)
    assert calls and all(c.get("channel") == "sceneB_2" for c in calls), calls
    calls.clear()
    await runner.run_subtask(_Server(), _Client(), _Encoder(), sub, 3, recall_limit=10, isolate_scenes=False)
    assert calls and all("channel" not in c for c in calls), calls
