"""The LongMemEval answer reader: what it shows the reader, and how it grades.

No model is called. The prompts are pinned by hash to the reference
implementation (they were compared character for character against
xiaowu0162/LongMemEval at 9e0b455 when written); a changed prompt is a changed
instrument and must turn these red.
"""
import hashlib
import json
import random
from types import SimpleNamespace

import pytest

from benchmarks import longmemeval_reader as R

TYPES = ["single-session-user", "single-session-assistant", "multi-session",
         "temporal-reasoning", "knowledge-update", "single-session-preference"]
PINNED = {
    "single-session-user": "ed829fb18c2ca738",
    "single-session-assistant": "ed829fb18c2ca738",
    "multi-session": "ed829fb18c2ca738",
    "temporal-reasoning": "ba2d1ea659331699",
    "knowledge-update": "b70d25a7d3c25057",
    "single-session-preference": "db58fecc14be5760",
}
ABSTENTION = "cc7fde39f8ad312e"


def _h(text):
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _ref(qtype="multi-session", abstention=False, question="Q?", answer="A.", date="2023/05/30 (Tue) 10:00"):
    return {"question_id": "x", "question_type": qtype, "question": question, "answer": answer,
            "question_date": date, "abstention": abstention, "evidence": []}


# --------------------------------------------------------------------------
# prompts
# --------------------------------------------------------------------------


@pytest.mark.parametrize("qtype", TYPES)
def test_each_judge_prompt_is_the_reference_one(qtype):
    assert _h(R.judge_prompt(_ref(qtype), "RESP")) == PINNED[qtype]


@pytest.mark.parametrize("qtype", TYPES)
def test_an_abstention_question_is_judged_as_one_whatever_its_type(qtype):
    assert _h(R.judge_prompt(_ref(qtype, abstention=True), "RESP")) == ABSTENTION


def test_the_reader_template_is_the_reference_one():
    assert _h(R.READER_TEMPLATE) == "e427ff913456e51a"


def test_history_is_sorted_by_date_and_formatted_as_the_reference_does():
    sessions = [
        ("2023/05/02 (Tue) 09:00", [{"role": "user", "content": " later "}]),
        ("2023/05/01 (Mon) 09:00", [{"role": "user", "content": "first"},
                                    {"role": "assistant", "content": "reply"}]),
    ]
    assert R.history_string(sessions) == (
        "\n### Session 1:\nSession Date: 2023/05/01 (Mon) 09:00\nSession Content:\n"
        "\n\nuser: first\n\nassistant: reply\n"
        "\n### Session 2:\nSession Date: 2023/05/02 (Tue) 09:00\nSession Content:\n"
        "\n\nuser: later\n"
    )


def test_the_reader_prompt_carries_the_question_date_and_the_question():
    prompt = R.reader_prompt(_ref(question="When?", date="2023/06/01 (Thu) 08:00"), [])
    assert prompt.endswith("Current Date: 2023/06/01 (Thu) 08:00\nQuestion: When?\nAnswer:")


# --------------------------------------------------------------------------
# what the reader is shown
# --------------------------------------------------------------------------


def test_rows_from_another_scene_are_dropped_counted_and_not_backfilled():
    returned = ["scene_3_session_1", "scene_4_session_9", "scene_3_session_2", "scene_3_session_7"]
    rows, dropped = R.rows_for("scene_3_q_0", returned, k=3)
    assert rows == ["scene_3_session_1", "scene_3_session_2"]
    assert dropped == 1


def test_a_scene_prefix_does_not_match_a_longer_scene_id():
    rows, dropped = R.rows_for("scene_3_q_0", ["scene_30_session_1"], k=10)
    assert (rows, dropped) == ([], 1)


def test_the_rankings_header_is_skipped(tmp_path):
    path = tmp_path / "rankings.jsonl"
    path.write_text(json.dumps({"header": True, "recall_limit": 10}) + "\n"
                    + json.dumps({"query_id": "scene_1_q_0", "returned_ids": ["scene_1_session_2"]}) + "\n")
    assert R.load_rankings(path) == {"scene_1_q_0": ["scene_1_session_2"]}


TITLE = "Data time: 09:00 AM on Monday 01 May, 2023 - Session 1"


def _fixture(tmp_path, long_text=False):
    """A two-scene LMEB directory and a rankings dump."""
    lmeb = tmp_path / "lmeb"
    for sub in R.LMEB_TYPES:
        (lmeb / sub).mkdir(parents=True)
        (lmeb / sub / "queries.jsonl").write_text("")
    (lmeb / "multi_session" / "queries.jsonl").write_text(
        json.dumps({"id": "scene_1_q_0", "text": "What did I buy?"}) + "\n")
    user = ("I bought a red kettle. " * 40) if long_text else "I bought a red kettle."
    (lmeb / "corpus.jsonl").write_text(
        json.dumps({"id": "scene_1_session_1", "text": user, "title": TITLE}) + "\n"
        + json.dumps({"id": "scene_1_session_2", "text": "An earlier session.",
                      "title": "Data time: 11:30 PM on Sunday 30 April, 2023 - Session 2"}) + "\n")
    rankings = tmp_path / "rankings.jsonl"
    rankings.write_text(json.dumps({"header": True}) + "\n" + json.dumps(
        {"query_id": "scene_1_q_0",
         "returned_ids": ["scene_2_session_5", "scene_1_session_1", "scene_1_session_2"]}) + "\n")
    turns = [{"role": "user", "content": user},
             {"role": "assistant", "content": "Nice kettle.", "has_answer": True}]
    oracle = {"question_id": "q1", "question_type": "multi-session", "question": "What did I buy?",
              "answer": "a red kettle", "question_date": "2023/05/30 (Tue) 10:00",
              "haystack_dates": ["2023/05/01 (Mon) 09:00"], "haystack_sessions": [turns],
              "haystack_session_ids": ["answer_1"]}
    oracle_path = tmp_path / "oracle.json"
    oracle_path.write_text(json.dumps([oracle]))
    args = SimpleNamespace(mode="rankings", rankings=rankings, arms={"expanded", "preview"},
                           lmeb_dir=lmeb, limit=0, seed=1)
    return args, R.load_references(oracle_path), R.load_queries(lmeb)


def _content(prompt, n):
    return prompt.split("Session Content:\n")[n].split("\n### Session", 1)[0].split("\n\n\nCurrent Date", 1)[0]


def test_the_expanded_arm_shows_each_stored_record_in_full_and_nothing_it_did_not_store(tmp_path):
    args, refs, queries = _fixture(tmp_path, long_text=True)
    items, stats = R.build_items(args, refs, queries)
    expanded = [i for i in items if i["arm"] == "expanded"][0]
    assert expanded["rows"] == ["scene_1_session_1", "scene_1_session_2"]
    assert stats["cross_scene_rows_dropped"] == 1
    # Sorted by the titles' times: session 2 is the earlier one.
    assert _content(expanded["prompt"], 1).strip() == "Data time: 11:30 PM on Sunday 30 April, 2023 - Session 2 An earlier session."
    assert _content(expanded["prompt"], 2).strip() == (TITLE + " " + "I bought a red kettle. " * 40).strip()
    assert "Nice kettle." not in expanded["prompt"], "the memory never stored the assistant turn"


def test_the_preview_arm_shows_the_stored_text_cut_to_the_preview_length(tmp_path):
    args, refs, queries = _fixture(tmp_path, long_text=True)
    items, _ = R.build_items(args, refs, queries)
    preview = [i for i in items if i["arm"] == "preview"][0]
    assert _content(preview["prompt"], 2).strip("\n") == (TITLE + " " + "I bought a red kettle. " * 40)[:R.PREVIEW_CHARS]


def test_the_session_date_is_the_titles_time_in_the_reference_format():
    assert R.session_date("Data time: 03:22 AM on Monday 29 May, 2023 - Session 390") == "2023/05/29 (Mon) 03:22"
    assert R.session_date("Data time: 12:04 AM on Saturday 20 May, 2023 - Session 1") == "2023/05/20 (Sat) 00:04"


def test_a_title_without_a_time_is_an_error_not_an_undated_session():
    with pytest.raises(ValueError, match="No time in title"):
        R.session_date("Session 1")


def _oracle_refs(n=6):
    refs, queries = {}, {}
    for i in range(n):
        text = f"question {i}?"
        refs[R.normalized(text)] = {**_ref(TYPES[i % 3], question=text),
                                    "evidence": [(f"2023/05/0{i + 1} (Mon) 09:00",
                                                  [{"role": "user", "content": f"marker-{i}"}])]}
        queries[f"scene_{i}_q_0"] = (TYPES[i % 3], text)
    return refs, queries


def test_the_oracle_context_is_the_questions_own_evidence():
    refs, queries = _oracle_refs()
    items, _ = R.build_items(SimpleNamespace(mode="oracle", limit=0, seed=1), refs, queries)
    for item in items:
        i = item["qid"].split("_")[1]
        assert f"marker-{i}" in item["prompt"]


def test_the_shuffled_context_is_never_the_questions_own():
    refs, queries = _oracle_refs()
    items, _ = R.build_items(SimpleNamespace(mode="shuffled", limit=0, seed=1), refs, queries)
    for item in items:
        i = item["qid"].split("_")[1]
        assert f"marker-{i}" not in item["prompt"] and "marker-" in item["prompt"]


def test_the_empty_context_shows_no_history():
    refs, queries = _oracle_refs()
    items, _ = R.build_items(SimpleNamespace(mode="empty", limit=0, seed=1), refs, queries)
    assert all("### Session" not in item["prompt"] for item in items)


def test_a_query_without_a_reference_is_refused():
    refs, queries = _oracle_refs(2)
    queries["scene_9_q_0"] = ("multi-session", "a question nobody asked?")
    with pytest.raises(ValueError, match="No reference"):
        R.build_items(SimpleNamespace(mode="oracle", limit=0, seed=1), refs, queries)


def test_the_stratified_subset_keeps_every_type_and_is_reproducible():
    refs, queries = _oracle_refs(30)
    by_qid = {q: refs[R.normalized(t)] for q, (_, t) in queries.items()}
    a = R.stratified(sorted(by_qid), by_qid, 9, random.Random(5))
    b = R.stratified(sorted(by_qid), by_qid, 9, random.Random(5))
    assert a == b
    assert {by_qid[q]["question_type"] for q in a} == set(TYPES[:3])


def test_a_derangement_has_no_fixed_point():
    for seed in range(50):
        p = R.derangement(5, random.Random(seed))
        assert sorted(p) == list(range(5)) and all(i != v for i, v in enumerate(p))


# --------------------------------------------------------------------------
# calls, caching and grading
# --------------------------------------------------------------------------


@pytest.fixture
def fake_codex(monkeypatch):
    calls = []

    def run(prompt, system, schema, out, *, model, effort, timeout=180):
        calls.append((prompt, effort))
        out.mkdir(parents=True)
        value = {"verdict": "yes" if "kettle" in prompt else "no"} if "verdict" in schema["properties"] \
            else {"answer": "a red kettle"}
        return value, {"input_tokens": 10, "output_tokens": 2}

    monkeypatch.setattr(R, "run_isolated", run)
    return calls


def test_a_call_is_made_once_and_then_read_from_the_cache(tmp_path, fake_codex):
    first = R.call("p", "s", R.READER_SCHEMA, tmp_path, effort="low")
    second = R.call("p", "s", R.READER_SCHEMA, tmp_path, effort="low")
    assert first == second and len(fake_codex) == 1


def test_a_repeat_or_another_effort_is_a_new_call(tmp_path, fake_codex):
    R.call("p", "s", R.READER_SCHEMA, tmp_path, effort="low")
    R.call("p", "s", R.READER_SCHEMA, tmp_path, effort="low", rep=1)
    R.call("p", "s", R.READER_SCHEMA, tmp_path, effort="medium")
    assert len(fake_codex) == 3


def test_the_judges_verdict_decides_correctness(tmp_path, fake_codex):
    item = {"qid": "q", "arm": "expanded", "reference": _ref(answer="a red kettle"), "prompt": "p"}
    result = R.answer_and_grade(item, tmp_path, "low", "low")
    assert result["correct"] is True and result["answer"] == "a red kettle"


def test_an_invalid_verdict_is_an_error_not_a_grade(tmp_path, monkeypatch):
    def run(prompt, system, schema, out, *, model, effort, timeout=180):
        out.mkdir(parents=True)
        return ({"verdict": "maybe"} if "verdict" in schema["properties"] else {"answer": "x"}), {}

    monkeypatch.setattr(R, "run_isolated", run)
    item = {"qid": "q", "arm": "expanded", "reference": _ref(), "prompt": "p"}
    with pytest.raises(ValueError, match="Invalid verdict"):
        R.answer_and_grade(item, tmp_path, "low", "low")


def test_the_summary_keeps_abstention_apart_and_counts_every_answer():
    results = [
        {"arm": "expanded", "question_type": "multi-session", "abstention": False, "correct": True},
        {"arm": "expanded", "question_type": "multi-session", "abstention": False, "correct": False},
        {"arm": "expanded", "question_type": "multi-session", "abstention": True, "correct": True},
        {"arm": "preview", "question_type": "temporal-reasoning", "abstention": False, "correct": False},
    ]
    summary = R.summarize(results)
    assert summary["expanded"]["multi-session"] == {"n": 2, "correct": 1, "accuracy": 0.5}
    assert summary["expanded"]["abstention"] == {"n": 1, "correct": 1, "accuracy": 1.0}
    assert summary["expanded"]["overall"] == {"n": 3, "correct": 2, "accuracy": 0.6667}
    assert summary["preview"]["overall"]["accuracy"] == 0.0


def test_the_judge_self_tests_hand_it_the_gold_or_another_questions_answer():
    refs, queries = _oracle_refs(6)
    for i, key in enumerate(sorted(refs)):
        refs[key]["answer"] = f"answer-{key}"
    first = sorted(queries)[0]
    refs[R.normalized(queries[first][1])]["abstention"] = True
    gold, _ = R.build_items(SimpleNamespace(mode="judge_gold", limit=0, seed=1), refs, queries)
    other, _ = R.build_items(SimpleNamespace(mode="judge_other", limit=0, seed=1), refs, queries)
    assert first not in {i["qid"] for i in gold}, "an abstention question has no answer to hand over"
    assert all(i["response"] == i["reference"]["answer"] for i in gold)
    assert all(i["response"] != i["reference"]["answer"] for i in other)
    assert len(gold) == len(other) == 5
