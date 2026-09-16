"""Measurement failures must not turn into successful zero-cost observations."""
import copy
import json
from types import SimpleNamespace

import pytest

from benchmarks import reconstruct_count_qa as qa


def raw_result():
    return {"subtype": "success", "is_error": False,
            "usage": {"input_tokens": 11, "cache_creation_input_tokens": 23,
                      "cache_read_input_tokens": 37, "output_tokens": 5},
            "duration_ms": 90, "duration_api_ms": 70, "total_cost_usd": 0.01,
            "modelUsage": {"claude-opus-5": {"inputTokens": 11,
                            "cacheCreationInputTokens": 23, "cacheReadInputTokens": 37,
                            "outputTokens": 5}}, "num_turns": 1}


def test_cache_tokens_count_as_input():
    measured = qa.metrics(raw_result(), 120)
    assert measured["total_input_tokens"] == 71
    assert measured["output_tokens"] == 5
    assert (measured["wall_ms"], measured["cli_ms"], measured["api_ms"]) == (120, 90, 70)


@pytest.mark.parametrize("key", ["input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"])
def test_missing_usage_is_not_zero(key):
    raw = raw_result()
    model_key = {"input_tokens": "inputTokens", "cache_creation_input_tokens": "cacheCreationInputTokens",
                 "cache_read_input_tokens": "cacheReadInputTokens", "output_tokens": "outputTokens"}[key]
    raw["modelUsage"]["claude-opus-5"][model_key] = 0
    del raw["usage"][key]
    with pytest.raises(KeyError):
        qa.metrics(raw, 120)


def test_failed_or_fallback_call_is_rejected():
    for patch in ({"subtype": "error_max_budget_usd"}, {"is_error": True},
                  {"modelUsage": {"other-model": {}}}):
        raw = raw_result() | patch
        with pytest.raises(ValueError):
            qa.metrics(raw, 120)


def test_auxiliary_usage_is_separate_from_answer_model():
    raw = raw_result()
    raw["modelUsage"]["auxiliary"] = {"inputTokens": 8, "cacheCreationInputTokens": 0,
                                      "cacheReadInputTokens": 2, "outputTokens": 3}
    measured = qa.metrics(raw, 120)
    assert measured["total_input_tokens"] == 71
    assert measured["all_model_input_tokens"] == 81
    assert measured["all_model_output_tokens"] == 8
    assert measured["auxiliary_models"] == ["auxiliary"]
    raw["modelUsage"]["claude-opus-5"]["outputTokens"] = 4
    with pytest.raises(ValueError, match="Primary model usage"):
        qa.metrics(raw, 120)


def test_judge_requires_boolean_not_yes_string():
    with pytest.raises(ValueError):
        qa.validate_output({"correct": "yes", "reason": "match"}, "judge")
    assert qa.validate_output({"correct": False, "reason": "partial"}, "judge")["correct"] is False


def test_judge_has_no_arm_label_and_abstention_has_own_rubric():
    reference = {"question": "q", "answer": "a", "abstention": True,
                 "question_type": "multi-session", "count": 32}
    packet = qa.judge_packet(reference, {"answer": "insufficient evidence", "refs": []})
    assert set(packet) == {"question", "reference_answer", "candidate_answer", "rubric"}
    assert "cannot be answered" in packet["rubric"]
    changed = copy.deepcopy(reference)
    changed["count"] = 1
    assert packet == qa.judge_packet(changed, {"answer": "insufficient evidence"})


def test_packet_identity_ignores_key_order_but_not_content():
    assert qa.packet_hash({"a": 1, "b": 2}) == qa.packet_hash({"b": 2, "a": 1})
    assert qa.packet_hash({"a": 1}) != qa.packet_hash({"a": 2})


@pytest.mark.parametrize("effort", ["medium", "high"])
def test_requested_effort_reaches_cli_and_result(tmp_path, monkeypatch, effort):
    observed = []
    raw = raw_result() | {"structured_output": {"correct": True, "reason": "matches"}}

    def fake_run(command, **kwargs):
        observed.append(command)
        return SimpleNamespace(returncode=0, stdout=json.dumps(raw), stderr="")

    monkeypatch.setattr(qa.subprocess, "run", fake_run)
    result = qa.invoke({"question": "q"}, "judge", tmp_path / "call", effort=effort)
    assert observed[0][observed[0].index("--effort") + 1] == effort
    assert result["effort"] == effort
    assert "--safe-mode" in observed[0]


def test_invalid_effort_never_calls_model(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid effort reached model")
    monkeypatch.setattr(qa.subprocess, "run", forbidden)
    with pytest.raises(ValueError, match="effort"):
        qa.invoke({}, "reader", tmp_path / "call", effort="mid")
