"""The OmniMemEval evidence metrics: tracing quotes to records and scoring answer evidence.

No harness, store or model is needed: the functions take a question's records as a dict.
"""
import importlib
import sys
from pathlib import Path

import pytest

DIR = Path(__file__).resolve().parents[1] / "benchmarks" / "omnimemeval"


@pytest.fixture(scope="module")
def E():
    mp = pytest.MonkeyPatch()
    mp.setenv("OMNIMEMEVAL_DIR", "/nonexistent")  # count_curve_build reads it at import
    mp.syspath_prepend(str(DIR))
    try:
        yield importlib.import_module("evidence_metrics")
    finally:
        mp.undo()
        sys.modules.pop("evidence_metrics", None)
        sys.modules.pop("count_curve_build", None)


T0, T1 = "2023-05-20T02:21:00+00:00", "2023-05-21T09:00:00+00:00"
S0 = f"Session date: {T0}\n\nuser: I adopted a cat named Miso.\n\nassistant: Congratulations on Miso!"
S1 = f"Session date: {T1}\n\nuser: My sister lives in Lyon.\n\nassistant: Lyon is lovely in spring."
RECS = {0: (S0, T0), 1: (S1, T1)}


def ctx(*blocks):
    return "Conversation memories:\n\n" + "\n".join(f"[{t}]\n{body}\n" for t, body in blocks)


def test_locate_maps_each_passage_to_its_record_and_span(E):
    spans, items, checks = E.locate(ctx((T1, "My sister lives in Lyon. … Lyon is lovely")), RECS)
    assert items == [{1}]
    s = S1.index("My sister")
    assert spans[1] == [(s, s + len("My sister lives in Lyon.")), (S1.index("Lyon is lovely"),
                                                                    S1.index("Lyon is lovely") + 14)]
    assert checks["quoted_chars_located"] == len("My sister lives in Lyon.") + len("Lyon is lovely")
    assert checks["quoted_chars_unlocated"] == 0


def test_passages_must_appear_in_text_order(E):
    # the second passage precedes the first in the record: not the same record's quote
    _, items, checks = E.locate(ctx((T1, "Lyon is lovely … My sister lives")), RECS)
    assert items == [set()]
    assert checks["quoted_chars_unlocated"] == len("Lyon is lovely") + len("My sister lives")


def test_head_prefers_the_record_at_the_blocks_time_and_reports_ambiguity(E):
    recs = {0: (f"Session date: {T0}\n\nuser: thanks!", T0), 1: (f"Session date: {T1}\n\nuser: thanks!", T1)}
    _, items, checks = E.locate(ctx((T1, "user: thanks!")), recs, frozenset({0}))
    assert items == [{1}]
    assert checks["parts_ambiguous"] == 1
    assert checks["parts_ambiguous_evidence"] == 1
    _, _, checks = E.locate(ctx((T1, "user: thanks!")), recs, frozenset())
    assert checks["parts_ambiguous_evidence"] == 0


def test_an_excerpt_part_is_searched_in_every_record(E):
    _, items, _ = E.locate(ctx((T1, "My sister lives in Lyon.\n…\nI adopted a cat")), RECS)
    assert items == [{0, 1}]


def test_evidence_spans_cover_the_turn_content_without_the_role(E):
    row = {"answer_session_ids": ["b"], "haystack_session_ids": ["a", "b"],
           "haystack_sessions": [[{"role": "user", "content": "I adopted a cat named Miso."}],
                                 [{"role": "user", "content": "My sister lives in Lyon.", "has_answer": True},
                                  {"role": "assistant", "content": "Lyon is lovely in spring."}]]}
    ans, turns, missing = E.evidence_spans(row, RECS)
    assert ans == [1] and missing == 0
    s = S1.index("My sister")
    assert turns == [(1, s, s + len("My sister lives in Lyon."), "user")]


def test_overlap_and_duplicates(E):
    m = E.merged([(0, 10), (5, 12), (20, 25)])
    assert m == [[0, 12], [20, 25]]
    assert E.overlap(m, 10, 22) == 2 + 2
    assert E.overlap(m, 12, 20) == 0  # touching a span's end is not overlapping it
    assert E.overlap(m, 30, 40) == 0  # a span past every quote adds nothing, never a negative


def _row(**kw):
    r = {"correct": True, "category": "c", "answer_sessions": 2, "sessions_shown": 1, "evidence_turns": 1,
         "turns_touched": 1, "turn_chars": 10, "turn_chars_quoted": 5, "quoted_chars": 100,
         "answer_session_chars_quoted": 20, "dup_chars": 0, "context_tokens": 500}
    r.update(kw)
    return r


def test_summary_averages_per_question_not_pooled(E):
    rows = [_row(answer_sessions=1, sessions_shown=1), _row(answer_sessions=4, sessions_shown=0)]
    s = E.summarise(rows)
    assert s["sessions_shown"] == pytest.approx(50.0)  # pooled would be 1/5 = 20%
    assert s["all_sessions_shown"] == pytest.approx(50.0)
    assert s["per_1k"] == pytest.approx((1 / 500 * 1000 + 0) / 2)


def test_summary_shares_and_cost_per_correct(E):
    rows = [_row(quoted_chars=100, answer_session_chars_quoted=30, dup_chars=10, correct=True),
            _row(quoted_chars=300, answer_session_chars_quoted=10, dup_chars=0, correct=False)]
    s = E.summarise(rows)
    assert s["evidence_share"] == pytest.approx(40 / 400 * 100)
    assert s["dup_share"] == pytest.approx(10 / 400 * 100)
    assert s["per_correct"] == pytest.approx(1000 / 1)
    wrong_only = E.summarise([rows[1]])
    assert wrong_only["per_correct"] != wrong_only["per_correct"]  # nan: no correct answer to divide by


class _Frame:
    """The two things per_question reads from the harness's DataFrame."""

    def __init__(self, rows):
        self.rows = rows

    def iterrows(self):
        return enumerate(self.rows)


def test_per_question_end_to_end(E, tmp_path):
    import json
    import sqlite3

    agent = "lme_exper_user_lme1_0"
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE memories (agent_id TEXT, msg_id TEXT, content TEXT, timestamp TEXT)")
    for j, (text, ts) in RECS.items():
        conn.execute("INSERT INTO memories VALUES (?, ?, ?, ?)", (agent, f"{agent}_lme_exper_session_{j}", text, ts))
    conn.execute("INSERT INTO memories VALUES (?, ?, ?, ?)",
                 ("lme_exper_user_lme1_1", "lme_exper_user_lme1_1_lme_exper_session_0", S1, T1))
    row = {"question_id": "q1", "answer_session_ids": ["b"], "haystack_session_ids": ["a", "b"],
           "haystack_sessions": [[{"role": "user", "content": "I adopted a cat named Miso."},
                                  {"role": "assistant", "content": "Congratulations on Miso!"}],
                                 [{"role": "user", "content": "My sister lives in Lyon.", "has_answer": True},
                                  {"role": "assistant", "content": "Lyon is lovely in spring."}]]}
    passage = "My sister lives in Lyon."
    context = ctx((T0, "I adopted a cat"), (T1, passage), (T1, passage))
    run = tmp_path / "results/lme/run"
    run.mkdir(parents=True)
    (run / "cpersona_lme_search_results.json").write_text(json.dumps({agent: [{"search_context": context}]}))
    (run / "cpersona_lme_judged.json").write_text(json.dumps({agent: [{
        "llm_judgments": {"judgment_1": False}, "category": "multi-session", "nlp_metrics": {"context_tokens": 40}}]}))
    rows, checks = E.per_question(tmp_path, "run", _Frame([row]), conn)
    assert checks["records"] == 2 and checks["sessions"] == 2  # another agent's record is not counted
    assert checks["evidence_turns_unlocated"] == 0
    r = rows[0]
    assert (r["answer_sessions"], r["sessions_shown"], r["evidence_turns"], r["turns_touched"]) == (1, 1, 1, 1)
    assert r["turn_chars"] == r["turn_chars_quoted"] == len(passage)
    assert r["quoted_chars"] == len("I adopted a cat") + 2 * len(passage)
    assert r["answer_session_chars_quoted"] == len(passage)  # merged: the repeat is not counted twice
    assert r["dup_chars"] == len(passage)
    assert r["first_item"] == 2 and r["items"] == 3
    assert r["correct"] is False and r["category"] == "multi-session" and r["context_tokens"] == 40
    assert r["evidence_turn_roles"] == r["touched_roles"] == ["user"]
