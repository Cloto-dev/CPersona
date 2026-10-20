"""plugin/: the agent plugin is shaped the way both hosts load it, and its hooks say what the docs say.

The hooks run on a reader's machine, under a host we do not control, so the tests run them
the way a host does: as a subprocess, with the payload on stdin as UTF-8 bytes and the
standard streams set to cp932 (PYTHONIOENCODING), which is what a Japanese Windows console
gives a Python process. A production user's hook read a Japanese prompt as text there and
added nothing for five days; the first test shows that the naive way of reading does break
under that setting, so the hooks' passes mean something.

The commands in hooks.json are run through a shell too, because a correct script behind a
command the shell cannot run is the same silence.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugin"
HOOK = PLUGIN / "scripts" / "cpersona_hooks.py"
PROMPT = "指輪の商品にギミックを足せないか、前に検討した記録はある？"


def _env(encoding: str | None = "cp932", **extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8", "CPERSONA_HOOKS_DISABLE")}
    if encoding:
        env["PYTHONIOENCODING"] = encoding
    env.update(extra)
    return env


def _run(args: list[str], payload: bytes, **extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], input=payload, capture_output=True, env=_env(**extra), timeout=30)


def _payload(**fields) -> bytes:
    return json.dumps(fields, ensure_ascii=False).encode("utf-8")


def _note(run: subprocess.CompletedProcess, event: str) -> str:
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout.decode("utf-8"))
    assert out["hookSpecificOutput"]["hookEventName"] == event
    return out["hookSpecificOutput"]["additionalContext"]


# --- the hooks ---------------------------------------------------------------------------


def test_the_trap_is_real_reading_stdin_as_text_under_cp932_breaks_a_japanese_prompt():
    naive = "import json, sys; p = json.load(sys.stdin)['prompt']; sys.stdout.buffer.write(p.encode('utf-8'))"
    run = _run(["-c", naive], _payload(prompt=PROMPT))
    assert run.returncode != 0 or run.stdout.decode("utf-8", "replace") != PROMPT


def test_a_japanese_prompt_survives_a_cp932_console():
    note = _note(_run([str(HOOK), "prompt"], _payload(hook_event_name="UserPromptSubmit", prompt=PROMPT)), "UserPromptSubmit")
    assert f'(suggested: "{PROMPT}")' in note
    assert "`reconstruct`" in note and "exact search" in note


def test_a_long_prompt_is_cut_to_the_excerpt():
    note = _note(_run([str(HOOK), "prompt"], _payload(prompt="あ" * 500)), "UserPromptSubmit")
    assert '"' + "あ" * 200 + '…"' in note


def test_slash_commands_and_empty_prompts_add_nothing():
    for prompt in ("/clear", "   ", ""):
        run = _run([str(HOOK), "prompt"], _payload(prompt=prompt))
        assert run.returncode == 0 and run.stdout == b"", prompt


def test_session_start_asks_for_reconstruct_first_and_store_as_it_goes():
    for source in ("startup", "clear"):
        note = _note(_run([str(HOOK), "session-start"], _payload(hook_event_name="SessionStart", source=source)), "SessionStart")
        assert "`reconstruct`" in note and "`store`" in note and "first substantive action" in note, source


def test_session_start_is_silent_on_resume_and_leaves_compact_to_its_own_note():
    for source in ("resume", "compact"):
        run = _run([str(HOOK), "session-start"], _payload(hook_event_name="SessionStart", source=source))
        assert run.returncode == 0 and run.stdout == b"", source


def test_a_host_that_sends_no_source_still_gets_the_session_start_note():
    _note(_run([str(HOOK), "session-start"], _payload(hook_event_name="SessionStart")), "SessionStart")


def test_after_compaction_the_note_restores_the_task_and_marks_the_summary_as_derived():
    note = _note(_run([str(HOOK), "compact"], _payload(hook_event_name="SessionStart", source="compact")), "SessionStart")
    assert "compacted" in note and "`reconstruct`" in note
    assert "can be wrong" in note and "before you store it as a fact" in note


def test_the_compact_note_fires_only_after_a_compaction():
    for source in ("startup", "clear", "resume", ""):
        run = _run([str(HOOK), "compact"], _payload(hook_event_name="SessionStart", source=source))
        assert run.returncode == 0 and run.stdout == b"", source


def test_input_it_cannot_read_never_blocks_the_host():
    for event in ("session-start", "prompt", "compact"):
        for payload in (b"not json", b"[1, 2]", b"\xff\xfe", b""):
            run = _run([str(HOOK), event], payload)
            assert run.returncode == 0 and run.stdout == b"", (event, payload)


def test_a_prompt_that_is_not_text_adds_nothing():
    for prompt in (3, None, ["a"], {"text": PROMPT}):
        run = _run([str(HOOK), "prompt"], _payload(prompt=prompt))
        assert run.returncode == 0 and run.stdout == b"", prompt


def test_an_unknown_or_missing_event_name_adds_nothing():
    for args in ([str(HOOK)], [str(HOOK), "pre-compact"]):
        run = _run(args, _payload(source="startup", prompt=PROMPT))
        assert run.returncode == 0 and run.stdout == b"", args


def test_a_disabled_hook_is_silent_and_the_others_are_not():
    off = {"CPERSONA_HOOKS_DISABLE": "prompt, compact"}
    run = _run([str(HOOK), "prompt"], _payload(prompt=PROMPT), **off)
    assert run.returncode == 0 and run.stdout == b""
    run = _run([str(HOOK), "compact"], _payload(source="compact"), **off)
    assert run.returncode == 0 and run.stdout == b""
    _note(_run([str(HOOK), "session-start"], _payload(source="startup"), **off), "SessionStart")
    run = _run([str(HOOK), "session-start"], _payload(source="startup"), CPERSONA_HOOKS_DISABLE="all")
    assert run.returncode == 0 and run.stdout == b""


@pytest.mark.parametrize(
    ("event", "payload", "limit"),
    [
        ("session-start", {"source": "startup"}, 450),
        ("compact", {"source": "compact"}, 450),
        ("prompt", {"prompt": "あ" * 500}, 550),
    ],
)
def test_each_note_stays_within_the_size_the_docs_state(event, payload, limit):
    """docs/getting-started.md states what each hook adds; every reader pays it, so it is pinned."""
    run = _run([str(HOOK), event], _payload(**payload))
    note = json.loads(run.stdout.decode("utf-8"))["hookSpecificOutput"]["additionalContext"]
    assert len(note) <= limit, (event, len(note))


# --- the plugin's shape ------------------------------------------------------------------


def _hooks() -> dict:
    return json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]


def test_hooks_json_wires_each_event_to_its_note_and_nothing_else():
    hooks = _hooks()
    assert set(hooks) == {"SessionStart", "UserPromptSubmit"}, "a PreCompact hook cannot reach the agent on either host"
    wired = {}
    for event, groups in hooks.items():
        for group in groups:
            for handler in group["hooks"]:
                name = handler["command"].split("cpersona_hooks.py\" ")[1].split(" ")[0]
                wired[(event, group.get("matcher", ""))] = name
    assert wired == {
        ("SessionStart", "startup|clear"): "session-start",
        ("SessionStart", "compact"): "compact",
        ("UserPromptSubmit", ""): "prompt",
    }


def test_every_command_names_the_script_through_the_plugin_root():
    for groups in _hooks().values():
        for group in groups:
            for handler in group["hooks"]:
                assert handler["type"] == "command"
                assert handler["command"].count('"${CLAUDE_PLUGIN_ROOT}/scripts/cpersona_hooks.py"') == 2, handler
                assert handler["command"].startswith("python3 ") and " || python " in handler["command"], handler
    assert HOOK.is_file()


def test_the_plugin_bundles_no_server_and_no_executables():
    # The server is connected separately: a bundled one would rename every tool to
    # mcp__plugin_cpersona_cpersona__* and show existing users the same server twice.
    assert not (PLUGIN / ".mcp.json").exists()
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert "mcpServers" not in manifest
    # claude.ai and Cowork do not install a plugin with a top-level bin/.
    assert not (PLUGIN / "bin").exists()
    assert (PLUGIN / "skills" / "cpersona-memory" / "SKILL.md").is_file()


def test_the_plugin_version_is_the_package_version():
    """plugin.json is a hand-written version surface, like server.json: a bump that misses it
    ships a plugin that hosts cache under the previous version and never offer as an update."""
    import cpersona

    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest.get("version"), "plugin.json carries no version — the comparison would pass vacuously"
    assert manifest["version"] == cpersona.__version__, (manifest["version"], cpersona.__version__)
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    # The manifest's version wins over an entry's, so a second copy in the marketplace could only go stale.
    assert all("version" not in entry for entry in market["plugins"])


def test_the_marketplace_lists_the_plugin_by_the_manifest_name():
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    (entry,) = market["plugins"]
    assert entry["source"] == "./plugin" and (ROOT / entry["source"]).resolve() == PLUGIN
    assert entry["name"] == manifest["name"] == "cpersona"


# --- the commands, through a shell -------------------------------------------------------


def _bash() -> str | None:
    return shutil.which("bash")


def _command(event: str, matcher: str = "") -> str:
    for group in _hooks()[event]:
        if group.get("matcher", "") == matcher:
            return group["hooks"][0]["command"]
    raise AssertionError((event, matcher))


@pytest.mark.skipif(_bash() is None, reason="no bash on this runner")
def test_the_command_runs_through_a_shell_with_the_plugin_root_set():
    run = subprocess.run(
        [_bash(), "-c", _command("UserPromptSubmit")],
        input=_payload(prompt=PROMPT),
        capture_output=True,
        env=_env(CLAUDE_PLUGIN_ROOT=PLUGIN.as_posix()),
        timeout=30,
    )
    assert f'(suggested: "{PROMPT}")' in _note(run, "UserPromptSubmit")


@pytest.mark.skipif(sys.platform == "win32" or _bash() is None, reason="needs a POSIX PATH to stage")
def test_the_command_falls_back_to_python_when_python3_is_missing(tmp_path):
    # Windows installs from python.org provide `python` and no `python3`; macOS provides
    # `python3` and often no `python`. A PATH with only `python` must still get the note.
    (tmp_path / "python").symlink_to(sys.executable)
    run = subprocess.run(
        [_bash(), "-c", _command("SessionStart", "startup|clear")],
        input=_payload(source="startup"),
        capture_output=True,
        env=_env(CLAUDE_PLUGIN_ROOT=PLUGIN.as_posix(), PATH=str(tmp_path)),
        timeout=30,
    )
    _note(run, "SessionStart")
    assert shutil.which("python3", path=str(tmp_path)) is None
