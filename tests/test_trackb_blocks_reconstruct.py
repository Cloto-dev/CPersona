"""The Track B runner's 2.6 arms: blocks built by the package's own builder, and the reconstruct tool scored.

store_corpus inserts rows directly, so the store path that would queue a block build never runs; the runner builds
the blocks itself (``build_corpus_blocks``) through ``blocks.prepare_blocks`` / ``blocks.write_blocks``. What is
tested here is that it writes the package's own division of every stored record and nothing of a page it could not
finish, and that a reconstruct response is scored on its items' head claims in item order while the evidence count
reads every claim.
"""
from __future__ import annotations

import hashlib
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


runner = _load("benchmark_trackb_lmeb_blocks", "benchmark_trackb_lmeb.py")

LONG = (
    "The first paragraph names the train that left at noon.\n\n"
    "The second paragraph says the ticket was bought the day before.\n\n"
    "The third paragraph says the seat was by the window, not the aisle."
)
CORPUS = [
    {"id": "long", "title": "", "text": LONG},
    {"id": "short", "title": "", "text": "One sentence only"},
]


class _Encoder:
    """Deterministic 16-dimension vectors from the text's digest; every text gets a distinct one."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def encode(self, texts, normalize_embeddings=True, show_progress_bar=False, **kw):
        self.calls.append(list(texts))
        out = np.zeros((len(texts), 16), dtype=np.float32)
        for i, t in enumerate(texts):
            digest = hashlib.sha256(t.encode()).digest()
            out[i] = np.frombuffer(digest[:16], dtype=np.uint8).astype(np.float32) - 127.5
            out[i] /= np.linalg.norm(out[i])
        return out


async def _clear():
    from cpersona.database import get_db

    db = await get_db()
    await db.execute("DELETE FROM memories WHERE agent_id = ?", (runner.AGENT_ID,))
    await db.commit()


async def _blocks_by_doc() -> dict[str, list[tuple[int, int]]]:
    from cpersona.database import get_db

    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT m.msg_id, b.start_char, b.end_char FROM record_blocks b "
        "JOIN memories m ON b.parent_kind = 'mem' AND b.parent_id = m.id "
        "WHERE m.agent_id = ? ORDER BY m.msg_id, b.block_index",
        (runner.AGENT_ID,),
    )
    out: dict[str, list[tuple[int, int]]] = {}
    for msg_id, start, end in rows:
        out.setdefault(msg_id, []).append((start, end))
    return out


async def _vector_rows() -> int:
    from cpersona.database import get_db

    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT COUNT(*) FROM record_block_vectors v JOIN memories m "
        "ON v.parent_kind = 'mem' AND v.parent_id = m.id WHERE m.agent_id = ?",
        (runner.AGENT_ID,),
    )
    return rows[0][0]


@pytest.mark.asyncio
async def test_blocks_are_the_packages_own_division_of_each_stored_record(monkeypatch):
    # The long record gets one block row (and one vector row) per span of blocks.segment, at the span's own
    # offsets; the single-span record gets none, as the package's builder declines it. page=1 makes each record
    # its own page, so staging is dropped and refilled between them.
    from cpersona import blocks, vector

    client = runner.LookupEmbeddingClient()
    monkeypatch.setattr(vector, "_embedding_client", client)
    encoder = _Encoder()
    await _clear()
    try:
        await runner.store_corpus(None, client, encoder, CORPUS)
        stats = await runner.build_corpus_blocks(client, encoder, page=1)
        got = await _blocks_by_doc()
        vectors = await _vector_rows()
        counted = await runner.count_block_rows()
    finally:
        await _clear()

    expected = [(s.start, s.end) for s in blocks.segment(LONG)]
    assert len(expected) > 1, "the fixture must divide, or it tests nothing"
    assert got == {"long": expected}, got
    assert vectors == len(expected)
    assert counted == len(expected)
    assert stats["records"] == 2
    assert stats["records_with_blocks"] == 1
    assert stats["block_rows"] == len(expected)
    # Each page encodes exactly the block texts of its own multi-span records: the long record's spans, once.
    block_calls = [c for c in encoder.calls if c != [CORPUS[0]["text"], CORPUS[1]["text"]]]
    assert block_calls == [[LONG[a:b] for a, b in expected]], block_calls
    assert client._staged == {}


SECOND = LONG.replace("train", "ferry").replace("seat", "cabin")


@pytest.mark.asyncio
async def test_a_block_without_a_vector_stops_the_build_and_keeps_nothing_of_its_page(monkeypatch):
    # Two long records on one page; the client receives the first one's vectors only. The first record's blocks
    # are written, the second cannot be embedded and raises, and the page is rolled back -- so the first record's
    # rows are gone too, and the run stops instead of measuring a partial index.
    from cpersona import blocks, vector

    client = runner.LookupEmbeddingClient()
    real_stage = client.stage
    first_texts = {LONG[s.start : s.end] for s in blocks.segment(LONG)}

    def _stage_first_only(texts, vecs):
        keep = [i for i, t in enumerate(texts) if t in first_texts]
        real_stage([texts[i] for i in keep], vecs[keep])

    monkeypatch.setattr(client, "stage", _stage_first_only)
    monkeypatch.setattr(vector, "_embedding_client", client)
    encoder = _Encoder()
    await _clear()
    try:
        await runner.store_corpus(None, client, encoder, [CORPUS[0], {"id": "second", "title": "", "text": SECOND}])
        with pytest.raises(RuntimeError):
            await runner.build_corpus_blocks(client, encoder)
        from cpersona.database import get_db

        await (await get_db()).commit()  # a stray open write would be published here
        got = await _blocks_by_doc()
    finally:
        await _clear()
    assert got == {}


MSG = {3: "d3", 5: "d5", 7: "d7"}


def _ref(kind: str, row_id: int) -> str:
    return f"{kind}:{row_id}"


def test_reconstruct_is_scored_on_head_claims_in_item_order():
    response = {
        "items": [
            {"head_ref": _ref("mem", 5), "claims": [{"ref": _ref("mem", 5)}, {"ref": _ref("mem", 3)}]},
            {"head_ref": _ref("mem", 7), "admission": "reservation", "claims": [{"ref": _ref("mem", 7)}, {"ref": _ref("mem", 3)}]},
        ]
    }
    ranked, evidence = runner.reconstruct_doc_ids(response, MSG)
    assert ranked == ["d5", "d7"]
    assert evidence == ["d5", "d3", "d7"]


def test_a_ref_the_corpus_cannot_hold_is_refused():
    with pytest.raises(ValueError):
        runner.reconstruct_doc_ids({"items": [{"head_ref": _ref("ep", 1), "claims": []}]}, MSG)
    with pytest.raises(KeyError):
        runner.reconstruct_doc_ids({"items": [{"head_ref": _ref("mem", 9), "claims": []}]}, MSG)


def test_any_relevant_reads_the_grade_not_the_presence():
    assert runner.any_relevant(["a"], {"a": 0, "b": 1}) is False
    assert runner.any_relevant(["c", "b"], {"a": 0, "b": 1}) is True
    assert runner.any_relevant(["b"], None) is False


def _subtask(tmp_path):
    (tmp_path / "queries.jsonl").write_text(
        json.dumps({"id": "s_1_q0", "text": "which seat?"}) + "\n"
        + json.dumps({"id": "s_1_q1", "text": "no answer here"}) + "\n"
    )
    # q1 has only a zero-graded row: compute_ndcg skips it, and so must the evidence count.
    (tmp_path / "qrels.tsv").write_text("s_1_q0\td3\t1\ns_1_q1\td5\t0\n")
    return {"name": "t", "queries": str(tmp_path / "queries.jsonl"), "qrels": str(tmp_path / "qrels.tsv"),
            "candidates": ""}


class _Client:
    def preload(self, texts, vectors):
        pass


@pytest.mark.asyncio
async def test_reconstruct_run_scores_heads_and_counts_every_claim(tmp_path):
    # The relevant record d3 is a claim of the first item but no item's head: NDCG@10 is 0, and the evidence count
    # still sees it.
    class _Server:
        async def do_reconstruct(self, agent_id, query, **kw):
            return {"items": [{"head_ref": _ref("mem", 5), "claims": [{"ref": _ref("mem", 5)}, {"ref": _ref("mem", 3)}]}]}

    sink: dict = {}
    ndcg = await runner.run_subtask(_Server(), _Client(), _Encoder(), _subtask(tmp_path), 3, tool="reconstruct",
                                    msg_id_of=MSG, evidence_sink=sink)
    assert ndcg == 0.0
    assert sink["t"] == {"queries": 1, "any_relevant": 1, "any_relevant_rate": 100.0, "mean_carried": 2.0}


@pytest.mark.asyncio
async def test_recall_evidence_counts_the_rows_past_the_first_ten(tmp_path):
    # do_recall returns most relevant LAST. Ten window rows and then one reserved row holding the relevant record:
    # it sits at rank 11, outside NDCG@10, and inside what the response carried.
    window = [f"w{i}" for i in range(10)]

    class _Server:
        async def do_recall(self, **kw):
            ranked = [*window, "d3"]
            return {"messages": [{"id": d} for d in reversed(ranked)]}

    sink: dict = {}
    ndcg = await runner.run_subtask(_Server(), _Client(), _Encoder(), _subtask(tmp_path), 3, recall_limit=10,
                                    evidence_sink=sink)
    assert ndcg == 0.0
    assert sink["t"]["any_relevant"] == 1
    assert sink["t"]["mean_carried"] == 11.0
