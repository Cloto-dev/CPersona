"""Platform portability: a CRLF checkout of the vocabulary, and a non-UTF-8 stdout.

Both are Windows defaults. Git for Windows checks text files out with CRLF line
endings (core.autocrlf=true), and a piped stdout on Windows encodes with the ANSI
code page (cp1252, cp932) unless UTF-8 mode is on.
"""

import asyncio
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from cpersona import proxy_stdio, tokens

REPO = Path(__file__).resolve().parents[1]


# --- the vocabulary under a CRLF checkout -------------------------------------------


def test_a_crlf_checkout_of_the_vocabulary_still_loads_and_counts(tmp_path, monkeypatch):
    lf = tokens.VOCAB_PATH.read_bytes()
    assert b"\r" not in lf
    crlf = tmp_path / "cl100k_base.tiktoken"
    crlf.write_bytes(lf.replace(b"\n", b"\r\n"))
    monkeypatch.setattr(tokens, "VOCAB_PATH", crlf)
    monkeypatch.setattr(tokens, "_encoding", None)

    # Same reference counts as the LF vocabulary (tests/test_reconstruct_modes.py).
    assert tokens.count("ℝ∑ emoji 😀 and ID item:4821 node:77") == 15
    assert tokens.count("20261009114255938115") == 7


def test_a_crlf_vocabulary_that_is_also_altered_is_still_refused(tmp_path, monkeypatch):
    # Normalising line endings must not turn the digest into a check of line endings only.
    altered = tokens.VOCAB_PATH.read_bytes().replace(b"IQ== 0", b"IQ== 9", 1).replace(b"\n", b"\r\n")
    path = tmp_path / "cl100k_base.tiktoken"
    path.write_bytes(altered)
    monkeypatch.setattr(tokens, "VOCAB_PATH", path)
    monkeypatch.setattr(tokens, "_encoding", None)
    with pytest.raises(tokens.VocabularyError):
        tokens.count("x")


def test_git_never_converts_the_vocabulary_line_endings():
    if shutil.which("git") is None or not (REPO / ".git").exists():
        pytest.skip("not a git checkout")
    out = subprocess.run(
        ["git", "-C", str(REPO), "check-attr", "text", "eol", "--", "cpersona/vocab/cl100k_base.tiktoken"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    # "-text" (unset) is what keeps core.autocrlf from rewriting the file on checkout.
    assert "text: unset" in out, out


# --- the stdio proxy under a non-UTF-8 stdout ---------------------------------------


def _drive_writer(monkeypatch, encoding):
    raw = io.BytesIO()
    stdout = io.TextIOWrapper(raw, encoding=encoding, errors="strict", newline="\n")
    monkeypatch.setattr(sys, "stdout", stdout)
    jp = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "日本語 memory"}]}},
        ensure_ascii=False,
    )
    plain = json.dumps({"jsonrpc": "2.0", "id": 2, "result": {}})

    async def go():
        q = asyncio.Queue()
        task = asyncio.create_task(proxy_stdio._stdout_writer(q))
        q.put_nowait([jp])
        q.put_nowait([plain])
        q.put_nowait(None)
        await task

    asyncio.run(go())
    stdout.flush()
    return raw.getvalue()


@pytest.mark.parametrize("encoding", ["cp1252", "cp932", "utf-8"])
def test_the_proxy_writes_utf8_whatever_the_console_encoding(monkeypatch, encoding):
    data = _drive_writer(monkeypatch, encoding)
    # The MCP client reads stdout as UTF-8: every response must arrive, intact.
    lines = [json.loads(line) for line in data.decode("utf-8").splitlines()]
    assert [m["id"] for m in lines] == [1, 2]
    assert lines[0]["result"]["content"][0]["text"] == "日本語 memory"
