"""The `cpersona-policy` command: what it prints, what it writes, what it refuses.

This command exists because the policy block has to reach a file the client loads
every session, and a client may refuse to let an agent write into such a file. So
the user runs the install themselves — which makes every byte this command writes
into somebody's always-loaded instructions, and the reason the assertions below
compare exact content rather than checking that something was written.

Nothing here touches the real home directory: each test points HOME at tmp_path,
and every install names its target explicitly unless detection is what is under
test.
"""

from __future__ import annotations

import os
import re
import stat
import sys
import tomllib
from pathlib import Path

import pytest

from cpersona import policy

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "cpersona-memory" / "SKILL.md"


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    """Point the home directory at a scratch tree for every test in this module."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


@pytest.fixture
def block():
    """The shipped block with an id substituted — what a correct install writes."""
    version, text = policy.policy_block("claude-code")
    assert version == "v2" or re.fullmatch(r"v\d+", version), version
    return text


def _run(argv, capsys):
    code = policy.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# --- printing (the default) --------------------------------------------------


def test_print_is_the_default_and_writes_nothing(tmp_path, capsys, _home, block):
    before = sorted(p for p in tmp_path.rglob("*"))
    code, out, err = _run(["--agent-id", "claude-code"], capsys)

    assert code == 0
    assert err == ""
    assert out == block + "\n"
    assert sorted(p for p in tmp_path.rglob("*")) == before


def test_printed_block_is_the_skill_block_with_the_id_substituted(capsys):
    shipped = policy.extract_block(SKILL.read_text(encoding="utf-8"))[1]
    code, out, _ = _run(["--agent-id", "agent.sapphy"], capsys)

    assert code == 0
    assert out == shipped.replace("<AGENT_ID>", "agent.sapphy") + "\n"
    assert "<AGENT_ID>" not in out
    assert 'agent_id="agent.sapphy"' in out


@pytest.mark.parametrize("flag", [["--client", "codex"], ["--target", "x.md"], ["--dry-run"]])
def test_target_flags_without_install_are_a_usage_error(flag, tmp_path, capsys):
    code, out, err = _run(["--agent-id", "claude-code", *flag], capsys)

    assert code == 2
    assert out == ""
    assert flag[0] in err
    assert not (tmp_path / "x.md").exists()


# --- invariant 1: the target does not exist yet ------------------------------


def test_creates_the_file_its_parents_and_ends_with_one_newline(tmp_path, capsys, block):
    target = tmp_path / "nested" / "deeper" / "CLAUDE.md"
    code, out, err = _run(
        ["--agent-id", "claude-code", "--install", "--target", str(target)], capsys
    )

    assert (code, err) == (0, "")
    assert target.read_bytes() == (block + "\n").encode("utf-8")
    assert out == f"installed cpersona-policy v2 into {target} (created)\n"


def test_a_new_file_gets_the_mode_a_plain_write_would_have_given_it(tmp_path, capsys):
    target = tmp_path / "CLAUDE.md"
    _run(["--agent-id", "claude-code", "--install", "--target", str(target)], capsys)

    mask = os.umask(0o022)
    os.umask(mask)
    assert stat.S_IMODE(target.stat().st_mode) == 0o666 & ~mask


# --- invariant 2: the target exists and holds no markers ---------------------


@pytest.mark.parametrize(
    "existing",
    [
        "# My rules\n\nBe brief.",  # no trailing newline at all
        "# My rules\n\nBe brief.\n",  # exactly one
        "# My rules\n\nBe brief.\n\n\n\n",  # several
    ],
    ids=["no-newline", "one-newline", "many-newlines"],
)
def test_appends_after_one_blank_line_whatever_the_file_ended_with(
    existing, tmp_path, capsys, block
):
    target = tmp_path / "CLAUDE.md"
    target.write_bytes(existing.encode("utf-8"))

    code, out, err = _run(
        ["--agent-id", "claude-code", "--install", "--target", str(target)], capsys
    )

    assert (code, err) == (0, "")
    assert target.read_bytes() == ("# My rules\n\nBe brief.\n\n" + block + "\n").encode("utf-8")
    assert out == f"installed cpersona-policy v2 into {target} (appended)\n"


def test_appending_keeps_the_last_crlf_line_intact(tmp_path, capsys, block):
    target = tmp_path / "AGENTS.md"
    target.write_bytes(b"# Rules\r\n\r\nBe brief.\r\n\r\n\r\n")

    code, _, err = _run(["--agent-id", "codex", "--install", "--target", str(target)], capsys)
    installed = policy.policy_block("codex")[1]

    assert (code, err) == (0, "")
    assert target.read_bytes() == b"# Rules\r\n\r\nBe brief.\r\n\r\n" + installed.encode("utf-8") + b"\n"


def test_appending_into_an_empty_file_adds_no_leading_blank_lines(tmp_path, capsys, block):
    target = tmp_path / "CLAUDE.md"
    target.write_bytes(b"")

    code, out, _ = _run(
        ["--agent-id", "claude-code", "--install", "--target", str(target)], capsys
    )

    assert code == 0
    assert target.read_bytes() == (block + "\n").encode("utf-8")
    assert "(appended)" in out


def test_trailing_spaces_on_the_last_line_are_not_stripped(tmp_path, capsys, block):
    target = tmp_path / "CLAUDE.md"
    target.write_bytes(b"Be brief.   \n")

    _run(["--agent-id", "claude-code", "--install", "--target", str(target)], capsys)

    assert target.read_bytes() == ("Be brief.   \n\n" + block + "\n").encode("utf-8")


# --- invariant 3: the target already holds one block ------------------------


V1_BLOCK = (
    "<!-- BEGIN cpersona-policy v1 (managed by the cpersona-memory skill) -->\n"
    "## CPersona memory policy\n"
    "\n"
    "An older, shorter policy nobody should keep.\n"
    "<!-- END cpersona-policy -->"
)


def test_upgrading_an_older_block_leaves_every_other_byte_alone(tmp_path, capsys, block):
    target = tmp_path / "CLAUDE.md"
    before = b"# My rules\r\n\r\nKeep these.\r\n\r\n"
    after = b"\r\n\r\n## After the block\r\nAlso keep these.\r\n"
    target.write_bytes(before + V1_BLOCK.encode("utf-8") + after)

    code, out, err = _run(
        ["--agent-id", "claude-code", "--install", "--target", str(target)], capsys
    )

    assert (code, err) == (0, "")
    assert target.read_bytes() == before + block.encode("utf-8") + after
    assert out == f"installed cpersona-policy v2 into {target} (replaced v1→v2)\n"


def test_a_marker_without_a_version_is_replaced_not_duplicated(tmp_path, capsys, block):
    target = tmp_path / "CLAUDE.md"
    unversioned = "<!-- BEGIN cpersona-policy -->\nstale\n<!-- END cpersona-policy -->"
    target.write_bytes(("head\n\n" + unversioned + "\n\ntail\n").encode("utf-8"))

    code, out, _ = _run(
        ["--agent-id", "claude-code", "--install", "--target", str(target)], capsys
    )

    assert code == 0
    assert target.read_bytes() == ("head\n\n" + block + "\n\ntail\n").encode("utf-8")
    assert "no version" in out
    assert target.read_text(encoding="utf-8").count("BEGIN cpersona-policy") == 1


def test_the_block_is_replaced_in_place_not_moved_to_the_end(tmp_path, capsys, block):
    target = tmp_path / "CLAUDE.md"
    target.write_bytes((V1_BLOCK + "\n\n## Tail heading\n").encode("utf-8"))

    _run(["--agent-id", "claude-code", "--install", "--target", str(target)], capsys)

    assert target.read_bytes() == (block + "\n\n## Tail heading\n").encode("utf-8")


# --- invariant 4: already identical ------------------------------------------


def test_an_identical_block_is_not_rewritten(tmp_path, capsys, block):
    target = tmp_path / "CLAUDE.md"
    body = ("head\n\n" + block + "\n\ntail\n").encode("utf-8")
    target.write_bytes(body)
    os.utime(target, (1_000_000, 1_000_000))
    before = target.stat()

    code, out, err = _run(
        ["--agent-id", "claude-code", "--install", "--target", str(target)], capsys
    )

    assert (code, err) == (0, "")
    assert out == f"cpersona-policy v2 already present in {target} (unchanged)\n"
    assert target.read_bytes() == body
    assert target.stat().st_mtime_ns == before.st_mtime_ns
    assert target.stat().st_ino == before.st_ino


def test_a_one_character_difference_inside_the_block_is_still_a_rewrite(tmp_path, capsys, block):
    target = tmp_path / "CLAUDE.md"
    target.write_bytes(("head\n\n" + block.replace("agent_id", "agent-id", 1) + "\n").encode("utf-8"))
    os.utime(target, (1_000_000, 1_000_000))

    code, out, _ = _run(
        ["--agent-id", "claude-code", "--install", "--target", str(target)], capsys
    )

    assert code == 0
    assert "(replaced" in out
    assert target.read_bytes() == ("head\n\n" + block + "\n").encode("utf-8")
    assert target.stat().st_mtime_ns != 1_000_000 * 10**9


# --- invariant 5: malformed markers ------------------------------------------


@pytest.mark.parametrize(
    "content, expected",
    [
        ("head\n" + V1_BLOCK.split("<!-- END")[0] + "\n", "1 BEGIN and 0 END"),
        ("head\n<!-- END cpersona-policy -->\n", "0 BEGIN and 1 END"),
        ("head\n" + V1_BLOCK + "\n" + V1_BLOCK + "\n", "2 BEGIN and 2 END"),
        ("<!-- BEGIN cpersona-policy v1 -->\n" + V1_BLOCK + "\n", "2 BEGIN and 1 END"),
        (V1_BLOCK + "\n<!-- END cpersona-policy -->\n", "1 BEGIN and 2 END"),
    ],
    ids=["begin-only", "end-only", "two-blocks", "two-begins", "two-ends"],
)
def test_malformed_markers_are_refused_and_nothing_is_written(
    content, expected, tmp_path, capsys
):
    target = tmp_path / "CLAUDE.md"
    target.write_bytes(content.encode("utf-8"))

    code, out, err = _run(
        ["--agent-id", "claude-code", "--install", "--target", str(target)], capsys
    )

    assert code == 2
    assert out == ""
    assert expected in err
    assert target.read_bytes() == content.encode("utf-8")


def test_an_end_marker_before_its_begin_marker_is_refused(tmp_path, capsys):
    target = tmp_path / "CLAUDE.md"
    content = "<!-- END cpersona-policy -->\nbody\n<!-- BEGIN cpersona-policy v1 -->\n"
    target.write_bytes(content.encode("utf-8"))

    code, out, err = _run(
        ["--agent-id", "claude-code", "--install", "--target", str(target)], capsys
    )

    assert (code, out) == (2, "")
    assert "comes before" in err
    assert target.read_bytes() == content.encode("utf-8")


# --- invariant 6: symlinked targets -----------------------------------------


def test_a_symlinked_target_stays_a_symlink_and_its_real_file_changes(tmp_path, capsys, block):
    real = tmp_path / "dotfiles" / "CLAUDE.md"
    real.parent.mkdir()
    real.write_bytes(b"# Tracked in a dotfiles repository\n")
    link = tmp_path / "home" / "CLAUDE.md"
    link.symlink_to(real)

    code, out, err = _run(
        ["--agent-id", "claude-code", "--install", "--target", str(link)], capsys
    )

    assert (code, err) == (0, "")
    assert link.is_symlink()
    assert os.readlink(link) == str(real)
    assert real.read_bytes() == (
        "# Tracked in a dotfiles repository\n\n" + block + "\n"
    ).encode("utf-8")
    assert f"note: {link} is a symlink" in out
    assert str(real) in out


def test_a_symlink_to_a_missing_file_creates_that_file_not_a_regular_target(
    tmp_path, capsys, block
):
    real = tmp_path / "dotfiles" / "CLAUDE.md"
    real.parent.mkdir()
    link = tmp_path / "home" / "CLAUDE.md"
    link.symlink_to(real)

    code, _, err = _run(
        ["--agent-id", "claude-code", "--install", "--target", str(link)], capsys
    )

    assert (code, err) == (0, "")
    assert link.is_symlink()
    assert real.is_file() and not real.is_symlink()
    assert real.read_bytes() == (block + "\n").encode("utf-8")


# --- invariant 7: atomic write, modes preserved -----------------------------


def test_an_existing_files_permission_bits_survive_the_replacement(tmp_path, capsys):
    target = tmp_path / "CLAUDE.md"
    target.write_bytes(b"head\n")
    os.chmod(target, 0o640)

    code, _, err = _run(
        ["--agent-id", "claude-code", "--install", "--target", str(target)], capsys
    )

    assert (code, err) == (0, "")
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_no_temporary_file_is_left_behind(tmp_path, capsys):
    target = tmp_path / "CLAUDE.md"
    _run(["--agent-id", "claude-code", "--install", "--target", str(target)], capsys)

    assert sorted(p.name for p in tmp_path.iterdir() if p.is_file()) == ["CLAUDE.md"]


def test_a_failed_write_leaves_the_old_content_and_no_temporary_file(tmp_path, monkeypatch):
    target = tmp_path / "CLAUDE.md"
    target.write_bytes(b"head\n")

    def boom(src, dst):
        raise OSError("no space left on device")

    monkeypatch.setattr(policy.os, "replace", boom)
    with pytest.raises(OSError):
        policy.write_atomically(target, "replacement\n")

    assert target.read_bytes() == b"head\n"
    assert sorted(p.name for p in tmp_path.iterdir() if p.is_file()) == ["CLAUDE.md"]


def test_a_failed_write_is_reported_as_a_refusal_not_a_traceback(tmp_path, capsys, monkeypatch):
    target = tmp_path / "CLAUDE.md"
    target.write_bytes(b"head\n")

    def boom(src, dst):
        raise OSError("no space left on device")

    monkeypatch.setattr(policy.os, "replace", boom)
    code, out, err = _run(
        ["--agent-id", "claude-code", "--install", "--target", str(target)], capsys
    )

    assert code == 2
    assert out == ""
    assert "cannot write" in err and "no space left on device" in err
    assert "nothing was changed" in err
    assert target.read_bytes() == b"head\n"
    assert sorted(p.name for p in tmp_path.iterdir() if p.is_file()) == ["CLAUDE.md"]


def test_a_directory_where_the_file_should_be_is_refused(tmp_path, capsys):
    target = tmp_path / "CLAUDE.md"
    target.mkdir()

    code, out, err = _run(
        ["--agent-id", "claude-code", "--install", "--target", str(target)], capsys
    )

    assert code == 2
    assert out == ""
    assert "cannot read" in err
    assert target.is_dir() and list(target.iterdir()) == []


# --- invariant 8: bytes outside the markers, line endings included ----------


def test_crlf_outside_the_block_is_not_rewritten_on_a_replacement(tmp_path, capsys, block):
    target = tmp_path / "CLAUDE.md"
    target.write_bytes(b"a\r\nb\r\n\r\n" + V1_BLOCK.encode("utf-8") + b"\r\nc\r\n")

    code, _, err = _run(
        ["--agent-id", "claude-code", "--install", "--target", str(target)], capsys
    )

    raw = target.read_bytes()
    assert (code, err) == (0, "")
    assert raw.startswith(b"a\r\nb\r\n\r\n")
    assert raw.endswith(b"\r\nc\r\n")
    assert raw == b"a\r\nb\r\n\r\n" + block.encode("utf-8") + b"\r\nc\r\n"


def test_non_utf8_content_is_refused_rather_than_mangled(tmp_path, capsys):
    target = tmp_path / "CLAUDE.md"
    target.write_bytes(b"\xff\xfe head\n")

    code, out, err = _run(
        ["--agent-id", "claude-code", "--install", "--target", str(target)], capsys
    )

    assert (code, out) == (2, "")
    assert "not UTF-8" in err
    assert target.read_bytes() == b"\xff\xfe head\n"


# --- invariant 9: --dry-run -------------------------------------------------


def test_dry_run_reports_a_creation_and_writes_nothing(tmp_path, capsys):
    target = tmp_path / "nested" / "CLAUDE.md"
    code, out, err = _run(
        ["--agent-id", "claude-code", "--install", "--dry-run", "--target", str(target)], capsys
    )

    assert (code, err) == (0, "")
    assert out == f"dry-run: would install cpersona-policy v2 into {target} (created)\n"
    assert not target.exists()
    assert not target.parent.exists()


def test_dry_run_reports_a_replacement_and_writes_nothing(tmp_path, capsys):
    target = tmp_path / "CLAUDE.md"
    content = ("head\n\n" + V1_BLOCK + "\n").encode("utf-8")
    target.write_bytes(content)
    os.utime(target, (1_000_000, 1_000_000))

    code, out, err = _run(
        ["--agent-id", "claude-code", "--install", "--dry-run", "--target", str(target)], capsys
    )

    assert (code, err) == (0, "")
    assert out == (
        f"dry-run: would install cpersona-policy v2 into {target} (replaced v1→v2)\n"
    )
    assert target.read_bytes() == content
    assert target.stat().st_mtime_ns == 1_000_000 * 10**9


def test_dry_run_reports_unchanged_when_the_block_is_already_there(tmp_path, capsys, block):
    target = tmp_path / "CLAUDE.md"
    target.write_bytes((block + "\n").encode("utf-8"))

    code, out, _ = _run(
        ["--agent-id", "claude-code", "--install", "--dry-run", "--target", str(target)], capsys
    )

    assert code == 0
    assert out == f"dry-run: cpersona-policy v2 already present in {target} (unchanged)\n"


def test_dry_run_refuses_broken_markers_the_same_way_a_real_run_does(tmp_path, capsys):
    target = tmp_path / "CLAUDE.md"
    content = ("head\n" + V1_BLOCK.split("<!-- END")[0]).encode("utf-8")
    target.write_bytes(content)

    code, out, err = _run(
        ["--agent-id", "claude-code", "--install", "--dry-run", "--target", str(target)], capsys
    )

    assert (code, out) == (2, "")
    assert "1 BEGIN and 0 END" in err
    assert target.read_bytes() == content


# --- choosing the target ----------------------------------------------------


@pytest.mark.parametrize(
    "directory, filename",
    [(".claude", "CLAUDE.md"), (".codex", "AGENTS.md")],
)
def test_one_client_directory_is_detected_without_being_named(
    directory, filename, _home, capsys, block
):
    (_home / directory).mkdir()

    code, out, err = _run(["--agent-id", "claude-code", "--install"], capsys)

    assert (code, err) == (0, "")
    assert (_home / directory / filename).read_bytes() == (block + "\n").encode("utf-8")
    assert str(_home / directory / filename) in out


def test_both_client_directories_present_refuses_to_guess(_home, capsys):
    (_home / ".claude").mkdir()
    (_home / ".codex").mkdir()

    code, out, err = _run(["--agent-id", "claude-code", "--install"], capsys)

    assert (code, out) == (2, "")
    assert "--client {claude-code,codex}" in err
    assert not (_home / ".claude" / "CLAUDE.md").exists()
    assert not (_home / ".codex" / "AGENTS.md").exists()


def test_no_client_directory_present_refuses_to_guess(_home, capsys):
    code, out, err = _run(["--agent-id", "claude-code", "--install"], capsys)

    assert (code, out) == (2, "")
    assert "--client {claude-code,codex}" in err
    assert sorted(p.name for p in _home.iterdir()) == []


def test_a_file_where_a_client_directory_is_expected_is_not_a_client(_home, capsys):
    """Detection asks for a directory: ~/.claude as a regular file detects nothing."""
    (_home / ".claude").write_bytes(b"not a directory\n")
    (_home / ".codex").mkdir()

    code, _, err = _run(["--agent-id", "codex", "--install"], capsys)

    assert (code, err) == (0, "")
    assert (_home / ".codex" / "AGENTS.md").is_file()


@pytest.mark.parametrize(
    "client, relative",
    [("claude-code", ".claude/CLAUDE.md"), ("codex", ".codex/AGENTS.md")],
)
def test_each_client_writes_its_own_user_level_file(client, relative, _home, capsys, block):
    code, _, err = _run(["--agent-id", "claude-code", "--install", "--client", client], capsys)

    assert (code, err) == (0, "")
    assert (_home / relative).read_bytes() == (block + "\n").encode("utf-8")


def test_client_and_target_together_are_a_usage_error(tmp_path, _home, capsys):
    (_home / ".claude").mkdir()
    target = tmp_path / "CLAUDE.md"

    code, out, err = _run(
        [
            "--agent-id",
            "claude-code",
            "--install",
            "--client",
            "claude-code",
            "--target",
            str(target),
        ],
        capsys,
    )

    assert (code, out) == (2, "")
    assert "--client" in err and "--target" in err
    assert not target.exists()
    assert not (_home / ".claude" / "CLAUDE.md").exists()


def test_an_unknown_client_is_rejected_by_the_parser(capsys):
    with pytest.raises(SystemExit) as exit_info:
        policy.main(["--agent-id", "claude-code", "--install", "--client", "cursor"])
    assert exit_info.value.code == 2


def test_a_tilde_in_target_is_expanded_to_the_home_directory(_home, capsys, block):
    code, _, err = _run(
        ["--agent-id", "claude-code", "--install", "--target", "~/project/AGENTS.md"], capsys
    )

    assert (code, err) == (0, "")
    assert (_home / "project" / "AGENTS.md").read_bytes() == (block + "\n").encode("utf-8")


def test_the_client_table_matches_the_documented_one():
    """The two file-based rows of the Getting Started table are the two choices here.

    A mapping that drifts from the page sends the block to a file the client does
    not read, and nothing about the run would look wrong.
    """
    page = (ROOT / "docs" / "getting-started.md").read_text(encoding="utf-8")
    assert "| Claude Code / Claude Desktop | `~/.claude/CLAUDE.md` |" in page
    assert "| Codex CLI | `~/.codex/AGENTS.md` |" in page
    assert policy._CLIENT_FILES == {
        "claude-code": (".claude", "CLAUDE.md"),
        "codex": (".codex", "AGENTS.md"),
    }


# --- the agent id -----------------------------------------------------------


@pytest.mark.parametrize(
    "agent_id",
    [
        'say"hi',
        "back`tick",
        "two words",
        "line\nbreak",
        "",
        "a" * 65,
        ".leading",
        "-leading",
        ":leading",
        "_leading",
        "tag<AGENT_ID>",
        "semi;colon",
        "sla/sh",
        "star*",
        "dollar$sign",
        "back\\slash",
        "quote'single",
    ],
)
def test_a_rejected_agent_id_writes_nothing(agent_id, tmp_path, capsys):
    target = tmp_path / "CLAUDE.md"
    # Written as one token so that an id beginning with '-' reaches the
    # validator rather than being read as another option.
    code, out, err = _run(
        [f"--agent-id={agent_id}", "--install", "--target", str(target)], capsys
    )

    assert code == 2
    assert out == ""
    assert "--agent-id" in err
    assert ". _ : -" in err, "the message must name the characters that are allowed"
    assert not target.exists()


@pytest.mark.parametrize(
    "agent_id",
    ["claude-code", "agent.sapphy", "team:bot_1", "a", "A9", "a" * 64, "x.y:z-1_2"],
)
def test_an_accepted_agent_id_reaches_the_block(agent_id, tmp_path, capsys):
    target = tmp_path / "CLAUDE.md"
    code, _, err = _run(
        ["--agent-id", agent_id, "--install", "--target", str(target)], capsys
    )

    assert (code, err) == (0, "")
    assert f'agent_id="{agent_id}"' in target.read_text(encoding="utf-8")
    assert "<AGENT_ID>" not in target.read_text(encoding="utf-8")


def test_a_rejected_id_is_reported_rather_than_raised(capsys):
    """The refusal is an exit code, not a SystemExit: a caller gets to handle it."""
    assert policy.main(["--agent-id", "bad id"]) == 2
    assert "invalid --agent-id" in capsys.readouterr().err


# --- locating the packaged skill -------------------------------------------


def test_the_lookup_finds_the_skill_beside_the_module(tmp_path):
    """The installed-wheel layout: skills/ is force-included under the package."""
    package = tmp_path / "site-packages" / "cpersona"
    skill = package / "skills" / "cpersona-memory" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("x\n", encoding="utf-8")

    assert policy.skill_path(package) == skill


def test_the_lookup_finds_the_skill_one_level_up(tmp_path):
    """The source-checkout layout: skills/ is a sibling of the package directory."""
    package = tmp_path / "checkout" / "cpersona"
    package.mkdir(parents=True)
    skill = tmp_path / "checkout" / "skills" / "cpersona-memory" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("x\n", encoding="utf-8")

    assert policy.skill_path(package) == skill


def test_the_packaged_layout_wins_over_the_one_above_it(tmp_path):
    """An installed wheel must read its own skill, not one in whatever sits above it."""
    package = tmp_path / "cpersona"
    inner = package / "skills" / "cpersona-memory" / "SKILL.md"
    inner.parent.mkdir(parents=True)
    inner.write_text("inner\n", encoding="utf-8")
    outer = tmp_path / "skills" / "cpersona-memory" / "SKILL.md"
    outer.parent.mkdir(parents=True)
    outer.write_text("outer\n", encoding="utf-8")

    assert policy.skill_path(package).read_text(encoding="utf-8") == "inner\n"


def test_the_lookup_reports_absence_rather_than_a_path_that_is_not_there(tmp_path):
    package = tmp_path / "cpersona"
    package.mkdir()
    assert policy.skill_path(package) is None


def test_a_missing_skill_exits_one(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(policy, "skill_path", lambda module_dir=None: None)
    code, out, err = _run(["--agent-id", "claude-code"], capsys)

    assert (code, out) == (1, "")
    assert "skills/cpersona-memory/SKILL.md" in err


@pytest.mark.parametrize("copies", [0, 2], ids=["none", "two"])
def test_a_skill_without_exactly_one_block_exits_one(copies, tmp_path, monkeypatch, capsys):
    skill = tmp_path / "SKILL.md"
    skill.write_text("prose\n" + (V1_BLOCK + "\n") * copies, encoding="utf-8")
    monkeypatch.setattr(policy, "skill_path", lambda module_dir=None: skill)

    code, out, err = _run(["--agent-id", "claude-code"], capsys)

    assert (code, out) == (1, "")
    assert f"holds {copies} policy blocks" in err


# --- packaging ---------------------------------------------------------------


def test_the_console_script_is_declared(capsys):
    manifest = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = manifest["project"]["scripts"]

    assert scripts["cpersona-policy"] == "cpersona.policy:main"
    # The server's own entry point is what every MCP client launches.
    assert scripts["cpersona"] == "cpersona.server:run"


def test_the_module_is_runnable_as_a_module():
    """`python -m cpersona.policy` is the form for anyone without the script on PATH."""
    import subprocess

    result = subprocess.run(
        [sys.executable, "-m", "cpersona.policy", "--agent-id", "claude-code"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.rstrip("\n") == policy.policy_block("claude-code")[1]
