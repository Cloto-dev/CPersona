"""Locks for the gate that keeps one benchmark table from disagreeing with itself.

CPersona's OmniMemEval rows are published twice — in the README, where a visitor
decides what the memory buys and costs, and in the results document beside the run
record and the rules they were judged by. Two copies of one measurement is the shape
that rots: the second copy is the one nobody remembers to update, and a benchmark
that quietly disagrees with itself is worse than one nobody published.

`check_benchmark_tables_agree` in scripts/check-docs-facts.py is what stops that.
These are its failure paths, asserted through the gate's own failure list, because
a gate whose red has never been observed is a gate observed only green.

The same file's `check_calibrate_default` is here too (bug-360). It is a different
claim, but it is the same script, the same failure list and the same question: can
this check still find its subject, or has it become a pass that costs nothing?
"""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

HEADER = (
    "| Backend | Deployment | SS-User | SS-Asst | SS-Pref | Temp. Reas | Multi-S | Know. Upd | Overall | Context Tokens |\n"
    "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |\n"
)
MINILM = "| CPersona 2.6.3a1 | local | 90.00 | 78.57 | 86.67 | 85.71 | 67.67 | 85.90 | 80.80 | 2,354.6 |\n"
BGE = "| CPersona v1.2 (2.6.4a1) | local | 91.43 | 80.36 | 90.00 | 83.46 | 71.43 | 84.62 | 81.60 | 1,786.7 |\n"


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
    drifted = BGE.replace("| 81.60 |", "| 82.40 |")
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
    padded = "|  CPersona v1.2 (2.6.4a1)  |  local  | 91.43 | 80.36 | 90.00 | 83.46 | 71.43 | 84.62 | 81.60 |  1,786.7  |\n"
    assert gate(HEADER + BGE, HEADER + padded) == []


def test_a_shorter_comparison_row_is_not_read_as_a_table_row(gate):
    """The results document also names CPersona in a five-column comparison table.

    Read as a row of the same table, that line would be a second row for one
    version: a measurement looking like two, and a red gate over a page that agrees.
    """
    comparison = (
        "| Backend | Deployment | Overall | Context Tokens | note |\n| --- | --- | ---: | ---: | --- |\n"
        "| CPersona v1.2 (2.6.4a1) | local | 81.60 | 1,786.7 | — |\n"
    )
    assert gate(HEADER + MINILM + BGE, HEADER + MINILM + BGE + "\n" + comparison) == []


def test_the_real_pages_agree():
    """And the repository's own two tables agree right now."""
    module = _load()
    module.failures.clear()
    module.check_benchmark_tables_agree()
    assert module.failures == []


# ---------------------------------------------------------------------------
# bug-412 — a duplicate row was discarded before the comparison ran.
# ---------------------------------------------------------------------------


def test_two_rows_for_one_model_in_one_table_is_caught(gate):
    """The stale-measurement shape: an old row above, the current one below.

    Keyed by model, the last row silently won and the two tables then compared
    equal -- so a document publishing two different numbers for one model passed
    the gate that exists to keep the published numbers from disagreeing.
    """
    stale = BGE.replace("| 81.60 |", "| 79.20 |")
    assert gate(HEADER + stale + BGE, HEADER + BGE) != []


def test_the_duplicate_is_caught_even_when_both_tables_carry_it(gate):
    """Both sides duplicating the row is the case a key-by-key compare cannot see."""
    stale = BGE.replace("| 81.60 |", "| 79.20 |")
    assert gate(HEADER + stale + BGE, HEADER + stale + BGE) != []


# ---------------------------------------------------------------------------
# bug-360 — the calibrate-default check passed when it matched nothing.
# ---------------------------------------------------------------------------
#
# It reported a mismatch only where its pattern matched, and never asked whether
# it had matched at all. Reword the documented sentence, or edit the expression,
# and the check becomes a silent pass over the claim it guards -- with nothing
# else in the suite covering it, so the vacuity would not surface elsewhere.


@pytest.fixture
def calibrate_gate(tmp_path, monkeypatch):
    module = _load()

    def run(*texts: str) -> list[str]:
        docs = []
        for i, text in enumerate(texts):
            doc = tmp_path / f"doc{i}.md"
            doc.write_text(text)
            docs.append(doc)
        monkeypatch.setattr(module, "ROOT", tmp_path)
        monkeypatch.setattr(module, "DOC_FILES", tuple(docs))
        module.failures.clear()
        module.check_calibrate_default("separation")
        return list(module.failures)

    return run


def test_the_documented_default_matching_the_code_passes(calibrate_gate):
    assert calibrate_gate("calibrate_threshold uses it by default (`separation`).") == []


def test_a_documented_default_that_drifted_is_caught(calibrate_gate):
    assert calibrate_gate("calibrate_threshold uses it by default (`percentile`).") != []


def test_a_check_that_matches_nothing_fails_instead_of_passing(calibrate_gate):
    """The vacuity: no document states the claim any more, and the gate went green."""
    assert calibrate_gate("no claim about the default lives here now") != []


def test_the_japanese_spelling_still_satisfies_the_check(calibrate_gate):
    """Both spellings count toward the same subject; only one page need carry it."""
    assert calibrate_gate("no claim here", "\u65e2\u5b9a (`separation`) \u3067\u3059\u3002") == []


def test_the_real_documents_still_state_the_default():
    """And the repository's own pages carry the claim right now."""
    module = _load()
    module.failures.clear()
    module.check_calibrate_default("separation")
    assert module.failures == []
