"""CPersona's agent hooks: tell the agent when to use its memory, at the moments it tends not to.

An agent does not always look in memory before it answers, and an instruction in an
always-loaded file is read once and then competes with everything else. A hook fires at
the moment itself. Each hook here adds one short note to the agent's context and does
nothing else: it never blocks, never talks to the server, and exits 0 on any input it
cannot read, so a broken hook costs a reminder and never a prompt.

One script, with the event named on the command line (plugin/hooks/hooks.json):

    session-start  SessionStart, source startup or clear: look in memory before the first
                   substantive action, and store what is worth keeping as it happens
    prompt         UserPromptSubmit: reconstruct for this request, with the prompt as the
                   suggested query
    compact        SessionStart, source compact: the conversation was just compacted;
                   bring back the task's decisions and constraints, and treat the summary
                   as derived, not as a record

Claude Code and Codex send the same payloads for these events, and Codex loads this
plugin as it is. A PreCompact hook is deliberately absent: neither host lets it put text
in front of the agent, so it cannot ask for anything to be saved before compaction.

Set CPERSONA_HOOKS_DISABLE to a comma-separated list of the names above (or "all") to
silence some of them; the per-prompt note is the one with a cost on every turn.

Two things break silently on Windows, and this file handles both:

- **Encoding.** Python's standard streams use the console code page there (cp932 on
  Japanese Windows), while the host sends and reads UTF-8. A hook that reads
  ``sys.stdin`` as text fails on a Japanese prompt and adds nothing. This one reads and
  writes bytes and does the UTF-8 itself.
- **Paths.** The command in hooks.json names this file through ``${CLAUDE_PLUGIN_ROOT}``,
  never a relative path, which stops resolving when the session's working directory is a
  subfolder.

Standard library only, and no syntax newer than the oldest Python a host may run it with.
"""

from __future__ import annotations

import json
import os
import sys

EXCERPT_CHARS = 200

SESSION_START_NOTE = (
    "cpersona: this agent has persistent memory. Before your first substantive action, call the "
    "cpersona `reconstruct` tool with a query for what the user is asking about, and use the "
    "decisions, constraints and findings it returns. When the session produces something worth "
    "keeping (a decision, a preference, a finding), `store` it then rather than at the end: the "
    "conversation can be compacted or closed at any time."
)

COMPACT_NOTE = (
    "cpersona: the conversation was just compacted, and the summary above is what is left of it. "
    "Before you continue, call the cpersona `reconstruct` tool for the task in progress to bring "
    "back its decisions, constraints and open items. The summary was written from the "
    "conversation and can be wrong (a plan can read as done): check a claim against the stored "
    "records or the files before you store it as a fact."
)


def prompt_note(prompt: str) -> str | None:
    """The note for one prompt, or None to add nothing."""
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


def disabled(event: str) -> bool:
    names = {n.strip().lower() for n in os.environ.get("CPERSONA_HOOKS_DISABLE", "").split(",")}
    return event in names or "all" in names


def note_for(event: str, payload: dict) -> tuple[str, str] | None:
    """(hook event name, note) for this event and payload, or None to add nothing."""
    source = payload.get("source") or ""
    if event == "session-start":
        # "" is a host that does not send source; resume keeps the conversation it had, and
        # compact has its own note.
        if source in ("startup", "clear", ""):
            return "SessionStart", SESSION_START_NOTE
        return None
    if event == "compact":
        return ("SessionStart", COMPACT_NOTE) if source == "compact" else None
    if event == "prompt":
        prompt = payload.get("prompt")
        note = prompt_note(prompt) if isinstance(prompt, str) else None
        return ("UserPromptSubmit", note) if note else None
    return None


def main(argv: list[str]) -> int:
    event = argv[1] if len(argv) > 1 else ""
    if disabled(event):
        return 0
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return 0  # never block the host over a hook that could not read its input
    if not isinstance(payload, dict):
        return 0
    found = note_for(event, payload)
    if found is None:
        return 0
    name, note = found
    out = {"hookSpecificOutput": {"hookEventName": name, "additionalContext": note}}
    sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False).encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
