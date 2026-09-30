"""examples/claude-code-hooks/prompt_hook.py: the hook in docs/operations.md does what the page says.

A production user's hook read a Japanese prompt as text on Windows, where Python's standard streams use the
console code page, and added nothing for five days. These tests run the example with its standard streams set to
cp932 (PYTHONIOENCODING), which is what that console gave it, on every platform — and first show that the naive
way of reading does break under that setting, so the example's pass means something.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "examples" / "claude-code-hooks" / "prompt_hook.py"
PROMPT = "指輪の商品にギミックを足せないか、前に検討した記録はある？"


def _run(args: list[str], payload: bytes, encoding: str | None = "cp932") -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    if encoding:
        env["PYTHONIOENCODING"] = encoding
    return subprocess.run([sys.executable, *args], input=payload, capture_output=True, env=env, timeout=30)


def _payload(prompt) -> bytes:
    return json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": prompt}, ensure_ascii=False).encode("utf-8")


def test_the_trap_is_real_reading_stdin_as_text_under_cp932_breaks_a_japanese_prompt():
    naive = "import json, sys; p = json.load(sys.stdin)['prompt']; sys.stdout.buffer.write(p.encode('utf-8'))"
    run = _run(["-c", naive], _payload(PROMPT))
    assert run.returncode != 0 or run.stdout.decode("utf-8", "replace") != PROMPT


def test_a_japanese_prompt_survives_a_cp932_console():
    run = _run([str(HOOK)], _payload(PROMPT))
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout.decode("utf-8"))
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    note = out["hookSpecificOutput"]["additionalContext"]
    assert f'(suggested: "{PROMPT}")' in note
    assert "`reconstruct`" in note and "exact search" in note


def test_a_long_prompt_is_cut_to_the_excerpt():
    run = _run([str(HOOK)], _payload("あ" * 500))
    note = json.loads(run.stdout.decode("utf-8"))["hookSpecificOutput"]["additionalContext"]
    assert '"' + "あ" * 200 + '…"' in note


def test_slash_commands_and_empty_prompts_add_nothing():
    for prompt in ("/clear", "   ", ""):
        run = _run([str(HOOK)], _payload(prompt))
        assert run.returncode == 0 and run.stdout == b"", prompt


def test_input_it_cannot_read_never_blocks_the_prompt():
    for payload in (b"not json", "{\"prompt\": 3}".encode(), b"\xff\xfe"):
        run = _run([str(HOOK)], payload)
        assert run.returncode == 0 and run.stdout == b"", payload
