"""A reader who lands on a registration command sees the two edits it needs.

The project website links straight to `getting-started/#register-claude-code`
and `#register-codex`. A reader arriving there sees the command first, so the
command's two assumptions (an absolute database path under `/home/you`, and an
embedding server on 127.0.0.1:8401) have to be stated between the anchor and the
command. Stated earlier on the page, they sit above the landing point; stated
later, they come after a pasted command has already failed.

The note quotes the exact text to change, so it is also checked against the
command below it: if the command stops containing what the note tells the reader
to change, the note is wrong, whichever of the two moved.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from cpersona import config

ROOT = Path(__file__).resolve().parent.parent
PAGES = ["docs/getting-started.md", "docs/getting-started.ja.md"]
# anchor -> the text the note tells the reader to replace in that command
EDITS = {
    "register-claude-code": '"EMBEDDING_MODE":"http"',
    "register-codex": "EMBEDDING_MODE=http",
}


def _landing(text: str, anchor: str) -> tuple[str, str]:
    """Return (the prose between the anchor and the first fence, that fence's body)."""
    start = text.index("{ #" + anchor + " }")
    m = re.compile(r"^```[a-z]*\n(.*?)^```", re.S | re.M).search(text, start)
    assert m, f"no code block follows {{ #{anchor} }}"
    return text[start:m.start()], m.group(1)


def _check(text: str, anchor: str, old: str) -> None:
    note, command = _landing(text, anchor)
    new = old.replace("http", "none")
    assert "/home/you" in note, f"#{anchor}: say before the command that /home/you must be replaced"
    assert f"`{old}`" in note and f"`{new}`" in note, (
        f"#{anchor}: say before the command how to run it without an embedding server ({old} -> {new})"
    )
    assert "/home/you" in command and old in command, (
        f"#{anchor}: the note names text the command below it no longer contains"
    )


@pytest.mark.parametrize("page", PAGES)
@pytest.mark.parametrize("anchor", sorted(EDITS))
def test_the_landing_point_states_the_commands_assumptions(page, anchor):
    _check((ROOT / page).read_text(encoding="utf-8"), anchor, EDITS[anchor])


def test_the_mode_the_note_offers_is_one_the_server_accepts():
    assert "none" in config.SUPPORTED_EMBEDDING_MODES


def _move_note_above(text: str, anchor: str) -> str:
    tag = "{ #" + anchor + " }\n\n"
    start = text.index(tag) + len(tag)
    end = text.index("\n\n```", start)
    note = text[start:end]
    text = text[:start] + text[end + 2:]
    heading = text.rindex("\n\n", 0, text.index(tag)) + 2
    return text[:heading] + note + "\n\n" + text[heading:]


@pytest.mark.parametrize(
    "break_it",
    [
        # the note moved above the anchor
        lambda t: _move_note_above(t, "register-codex"),
        # the command changed under the note
        lambda t: t.replace("--env EMBEDDING_MODE=http", "--env EMBEDDING_MODE=api", 1),
        # the no-server half of the note dropped
        lambda t: t.replace("`EMBEDDING_MODE=none`", "`EMBEDDING_MODE`", 1),
    ],
)
def test_the_check_fails_when_the_landing_point_breaks(break_it):
    """A landing check that passed on a broken page would protect nothing."""
    text = (ROOT / "docs/getting-started.md").read_text(encoding="utf-8")
    broken = break_it(text)
    assert broken != text, "the mutation no longer applies; update it to the current page"
    with pytest.raises(AssertionError):
        _check(broken, "register-codex", EDITS["register-codex"])


@pytest.mark.parametrize(
    "page, website",
    [("docs/getting-started.md", "https://cpersona.cloto.dev/#setup"), ("docs/getting-started.ja.md", "https://cpersona.cloto.dev/ja/#setup")],
)
def test_the_guide_opens_with_the_agent_route(page, website):
    """The website starts readers with one prompt for Claude Code or Codex; the guide it points to says the same first.

    Before this, the guide listed Python 3.11+ and uv as prerequisites and its only agent route was a Claude Code
    skill, while the website said Claude Code or Codex was all a reader needed.
    """
    text = (ROOT / page).read_text(encoding="utf-8")
    first = text[text.index("\n## ") :].split("\n## ", 2)[1]
    assert first.startswith(("Let your agent set it up", "エージェントに任せる { #let-your-agent-set-it-up }")), first[:60]
    assert f"]({website})" in first, f"{page}: the first section does not link the website's setup prompt"
    assert "Claude Code" in first and "Codex" in first, f"{page}: the first section must name both agents"
