"""A Claude Code UserPromptSubmit hook that reminds the agent to look in CPersona before it answers.

An agent does not always search its memory before answering, and the reminder
in an instructions file is read once and then competes with everything else.
A hook fires on every prompt. This one adds a short note to the context: call
CPersona's ``reconstruct`` tool for this request (with the prompt as a suggested
query), and confirm "there is no such record" with an exact search, because a
recall score cannot say it (docs/behavior-contracts.md §12).

Standard library only. Register it in ``.claude/settings.json``:

    {
      "hooks": {
        "UserPromptSubmit": [
          {"hooks": [{"type": "command",
                      "command": "python \\"$CLAUDE_PROJECT_DIR/.claude/hooks/prompt_hook.py\\"",
                      "timeout": 5}]}
        ]
      }
    }

Two things break silently on Windows, and this file handles both:

- **Encoding.** Python's standard streams use the console code page there
  (cp932 on Japanese Windows), while Claude Code sends and reads UTF-8. A hook
  that reads ``sys.stdin`` as text fails on a Japanese prompt and adds nothing.
  This one reads and writes bytes and does the UTF-8 itself.
- **Paths.** A relative path to the script stops resolving when the session's
  working directory is a subfolder. ``$CLAUDE_PROJECT_DIR`` is the project root.
"""

import json
import sys

EXCERPT_CHARS = 200


def note_for(prompt: str) -> str | None:
    """The context to add for this prompt, or None to add nothing."""
    text = " ".join(prompt.split())
    if not text or text.startswith("/"):
        return None  # empty, or a slash command
    excerpt = text[:EXCERPT_CHARS] + ("…" if len(text) > EXCERPT_CHARS else "")
    return (
        "cpersona: if answering this needs anything from earlier sessions, call the cpersona "
        f'`reconstruct` tool before you answer, with a query for this request (suggested: "{excerpt}"). '
        "Before you say that a record does not exist, confirm it with an exact search for a term "
        "the record would contain; a recall score cannot tell you."
    )


def main() -> int:
    raw = sys.stdin.buffer.read()
    try:
        prompt = json.loads(raw.decode("utf-8")).get("prompt", "")
    except (UnicodeDecodeError, ValueError, AttributeError):
        return 0  # never block the prompt over a hook that could not read it
    note = note_for(prompt if isinstance(prompt, str) else "")
    if note is None:
        return 0
    out = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": note}}
    sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False).encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
