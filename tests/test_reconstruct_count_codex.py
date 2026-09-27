"""Detect understated usage and successful-looking failures in Codex measurements."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks import reconstruct_count_codex as qa


def events():
    return [{"type": "turn.completed", "usage": {
        "input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 12,
        "reasoning_output_tokens": 8,
    }}]


def test_cached_input_is_subset():
    result = qa.metrics(events(), 35)
    assert result["total_input_tokens"] == 100
    assert result["uncached_input_tokens"] == 40
    assert result["output_tokens"] == 12
    assert result["reasoning_output_tokens"] == 8


@pytest.mark.parametrize("key", ["input_tokens", "cached_input_tokens", "output_tokens"])
def test_missing_usage_fails(key):
    value = events()
    del value[0]["usage"][key]
    with pytest.raises(KeyError):
        qa.metrics(value, 1)


def test_invalid_usage_fails():
    for key, number in (("input_tokens", True), ("cached_input_tokens", 101),
                        ("output_tokens", -1), ("reasoning_output_tokens", 13)):
        value = events()
        value[0]["usage"][key] = number
        with pytest.raises(ValueError):
            qa.metrics(value, 1)


def test_errors_retries_and_tool_use_fail():
    for value in ([], events() * 2,
                  events() + [{"type": "turn.failed"}],
                  events() + [{"type": "error"}],
                  events() + [{"type": "item.completed", "item": {"type": "command_execution"}}]):
        with pytest.raises(ValueError):
            qa.metrics(value, 1)


def test_invocation_pins_model_effort_and_chatgpt_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-only-not-a-key")
    observed = []

    def fake_run(command, **kwargs):
        observed.append(command)
        assert "OPENAI_API_KEY" not in kwargs["env"]
        assert Path(kwargs["cwd"]) != tmp_path
        kwargs["stdout"].write("\n".join(json.dumps(e) for e in events()))
        Path(command[command.index("--output-last-message") + 1]).write_text(
            json.dumps({"correct": True, "reason": "matches"}))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(qa.subprocess, "run", fake_run)
    result = qa.invoke({"question": "q"}, "judge", tmp_path / "call")
    command = observed[0]
    assert command[command.index("--model") + 1] == "gpt-5.6-luna"
    assert 'model_reasoning_effort="high"' in command
    assert 'forced_login_method="chatgpt"' in command
    assert "--ignore-user-config" in command
    assert result["effort"] == "high"
    assert result["metrics"]["total_input_tokens"] == 100
    with pytest.raises(FileExistsError):
        qa.invoke({}, "judge", tmp_path / "call")
    assert len(observed) == 1


def test_nonzero_exit_never_becomes_success(tmp_path, monkeypatch):
    monkeypatch.setattr(qa.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1))
    with pytest.raises(RuntimeError, match="exited 1"):
        qa.invoke({}, "judge", tmp_path / "call")
    assert not (tmp_path / "call" / "result.json").exists()


def test_the_executable_can_be_pointed_past_a_wrapper(tmp_path, monkeypatch):
    observed = []

    def fake_run(command, **kwargs):
        observed.append(command)
        kwargs["stdout"].write("\n".join(json.dumps(e) for e in events()))
        Path(command[command.index("--output-last-message") + 1]).write_text(
            json.dumps({"correct": True, "reason": "matches"}))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(qa.subprocess, "run", fake_run)
    monkeypatch.setattr(qa, "CODEX_BIN", "/opt/real/codex")
    qa.invoke({"question": "q"}, "judge", tmp_path / "call")
    assert observed[0][0] == "/opt/real/codex"
