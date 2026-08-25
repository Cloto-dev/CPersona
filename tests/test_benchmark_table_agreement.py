"""Locks for the gate that keeps one benchmark table from disagreeing with itself.

The LMEB Track A/B numbers are published twice — in the README, where a visitor
decides whether the pipeline costs ranking quality, and in `benchmarks/README.md`
beside the harness that produced them. Two copies of one measurement is the shape
that rots: the second copy is the one nobody remembers to update, and a benchmark
that quietly disagrees with itself is worse than one nobody published.

`check_benchmark_tables_agree` in scripts/check-docs-facts.py is what stops that.
These are its failure paths, asserted through the gate's own failure list, because
a gate whose red has never been observed is a gate observed only green.
"""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

HEADER = "| Embedding Model | Params | Dim | Track A (raw) | Track B (cpersona) | Δ |\n|---|---|---|---|---|---|\n"
MINILM = "| all-MiniLM-L6-v2 | 22M | 384 | 43.67 | **50.10** | +6.43 |\n"
BGE = "| bge-m3 | 568M | 1024 | 56.83 | **57.66** | +0.83 |\n"


def _load():
    spec = importlib.util.spec_from_file_location(
        "check_docs_facts", ROOT / "scripts" / "check-docs-facts.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gate(tmp_path, monkeypatch):
    """The check pointed at two temp pages, with a clean failure list each time."""
    module = _load()

    def run(text_a: str, text_b: str) -> list[str]:
        a, b = tmp_path / "a.md", tmp_path / "b.md"
        a.write_text(f"prose\n\n{text_a}\nmore prose\n")
        b.write_text(f"prose\n\n{text_b}\nmore prose\n")
        monkeypatch.setattr(module, "ROOT", tmp_path)
        monkeypatch.setattr(module, "BENCH_TABLE_SOURCES", (a, b))
        module.failures.clear()
        module.check_benchmark_tables_agree()
        return list(module.failures)

    return run


def test_identical_tables_pass(gate):
    assert gate(HEADER + MINILM + BGE, HEADER + MINILM + BGE) == []


def test_a_changed_number_is_caught(gate):
    """The case this gate exists for: one side is updated, the other is not."""
    drifted = BGE.replace("**57.66**", "**58.99**")
    assert gate(HEADER + MINILM + BGE, HEADER + MINILM + drifted) != []


def test_a_row_present_on_only_one_side_is_caught(gate):
    """Adding a model to one table and forgetting the other is the same class."""
    assert gate(HEADER + MINILM + BGE, HEADER + BGE) != []


def test_a_missing_table_fails_rather_than_skipping(gate):
    """A comparison with nothing to compare must not report green.

    This is the failure mode that makes a gate worse than no gate: move or rename
    the table, and a check that treats "no rows" as "nothing to check" goes on
    passing forever over numbers nobody is reading anymore.
    """
    assert gate(HEADER + MINILM + BGE, "no table here at all\n") != []


def test_reformatting_is_free(gate):
    """Rows compare after whitespace is squeezed: alignment is not a fact."""
    padded = "|  bge-m3  |  568M  | 1024 | 56.83 | **57.66** | +0.83 |\n"
    assert gate(HEADER + BGE, HEADER + padded) == []


def test_the_real_pages_agree():
    """And the repository's own two tables agree right now."""
    module = _load()
    module.failures.clear()
    module.check_benchmark_tables_agree()
    assert module.failures == []
