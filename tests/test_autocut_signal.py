"""Regression tests for bug-013 — autocut applied gap detection to rank-fusion scores.

RRF/RSF scores decay hyperbolically by construction (1/(k+rank) sums), so their
"gaps" encode retriever overlap, not relevance breaks: on a homogeneous 18k-doc
corpus autocut cut a full recall down to 2 rows (LMEB MemBench
multi_session_assistant scored exactly 0.0). The fix keys autocut on
similarity-scale signals only:

- fusion-ordered results (_rrf_score / _rsf_score, no confidence) are returned
  whole — contamination control there is the fused quality gate's job;
- cascade results (cosine-ordered) and confidence-sorted results keep the
  Weaviate-style gap cut unchanged.
"""

import os
import tempfile


_tmpdir = tempfile.mkdtemp()
os.environ["CPERSONA_DB_PATH"] = os.path.join(_tmpdir, "test_autocut.db")
os.environ["CPERSONA_EMBEDDING_MODE"] = "none"

from cpersona.memory_handlers import _autocut  # noqa: E402


def _rows(key: str, scores: list[float]) -> list[dict]:
    return [{key: s, "content": f"doc{i}"} for i, s in enumerate(scores)]


def test_rrf_flat_distribution_is_not_cut():
    """The bug-013 shape: two dual-retriever hits (~2/61) tower over thousands
    of single-retriever rows (~1/61) — a 50% artificial gap. Must not cut."""
    scores = [0.0328, 0.0325] + [0.0164 - i * 1e-6 for i in range(100)]
    rows = _rows("_rrf_score", scores)
    assert _autocut(rows) == rows


def test_rsf_ordering_is_not_cut():
    """RSF min-max pins the tail to 0.0, manufacturing a full-scale gap."""
    rows = _rows("_rsf_score", [1.0, 0.9, 0.5, 0.0])
    assert _autocut(rows) == rows


def test_cascade_cosine_gap_still_cuts():
    """Cosine-ordered results keep the original gap-cut behaviour."""
    rows = _rows("_cosine", [0.92, 0.90, 0.88, 0.30, 0.28])
    cut = _autocut(rows)
    assert len(cut) == 3
    assert [r["content"] for r in cut] == ["doc0", "doc1", "doc2"]


def test_cascade_mixed_signal_is_not_cut():
    """bug-018: cascade concatenates a vector stage (rows carry _cosine) with
    episode/profile/keyword stages (no _cosine). Confidence off, no fusion score,
    so autocut falls to the cosine branch — but the non-vector rows have no
    signal. It must return the list whole rather than scoring them 0 and cutting
    every non-vector hit at the vector->non-vector boundary."""
    rows = [
        {"_cosine": 0.71, "content": "vec0"},
        {"_cosine": 0.68, "content": "vec1"},
        {"content": "episode0"},   # no _cosine
        {"content": "[Profile] p"},  # no _cosine
        {"content": "keyword0"},   # no _cosine
    ]
    assert _autocut(rows) == rows


def test_confidence_sorted_gap_still_cuts():
    """Confidence-sorted results (CONFIDENCE_ENABLED) keep the gap cut, even
    when fusion scores are also present on the rows."""
    rows = [
        {"_confidence_score": s, "_rrf_score": 0.03 - i * 0.001, "content": f"doc{i}"}
        for i, s in enumerate([0.95, 0.93, 0.91, 0.20, 0.18])
    ]
    cut = _autocut(rows)
    assert len(cut) == 3


def test_small_result_sets_returned_whole():
    """The v2.4.25 floor is unchanged."""
    rows = _rows("_cosine", [0.9, 0.2])
    assert _autocut(rows) == rows
