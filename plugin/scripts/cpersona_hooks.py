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

The session-start and compact notes also name the agent_id to call with. CPersona scopes
every record to an exact agent_id, and an agent left to guess one picks a different name in
a later session ("claude", then "default"), so its own records stop coming back. The id is
the plugin's agent_id option, which Claude Code exports to hooks as
CLAUDE_PLUGIN_OPTION_AGENT_ID; else CPERSONA_AGENT_ID from the environment, which is how
Codex sets it, having no plugin options; else "default", unless the user's always-loaded
instructions already name one. The per-prompt note names it only when it is set, so that it
never contradicts an id those instructions name.

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

OPTION_ENV = "CLAUDE_PLUGIN_OPTION_AGENT_ID"  # userConfig key agent_id in plugin.json
AGENT_ID_ENV = "CPERSONA_AGENT_ID"
DEFAULT_AGENT_ID = "default"


def configured_agent_id() -> str | None:
    """The agent_id set for this plugin, or None when neither setting names one."""
    for name in (OPTION_ENV, AGENT_ID_ENV):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def agent_id_sentence(agent_id: str | None) -> str:
    if agent_id is not None:
        return f"Use agent_id {json.dumps(agent_id, ensure_ascii=False)} on every cpersona call."
    # A user who followed the setup has an id in CLAUDE.md or AGENTS.md; the default must not override it.
    return (
        f'Use agent_id "{DEFAULT_AGENT_ID}" on every cpersona call, unless your always-loaded '
        "instructions name another one for CPersona."
    )


def session_start_note(agent_id: str | None) -> str:
    return (
        f"cpersona: this agent has persistent memory. {agent_id_sentence(agent_id)} Before your first "
        "substantive action, call the cpersona `reconstruct` tool with a query for what the user is "
        "asking about, and use the decisions, constraints and findings it returns. When the session "
        "produces something worth keeping (a decision, a preference, a finding), `store` it then "
        "rather than at the end: the conversation can be compacted or closed at any time."
    )


def compact_note(agent_id: str | None) -> str:
    return (
        "cpersona: the conversation was just compacted, and the summary above is what is left of it. "
        "Before you continue, call the cpersona `reconstruct` tool for the task in progress to bring "
        f"back its decisions, constraints and open items. {agent_id_sentence(agent_id)} The summary "
        "was written from the conversation and can be wrong (a plan can read as done): check a claim "
        "against the stored records or the files before you store it as a fact."
    )


def prompt_note(prompt: str, agent_id: str | None = None) -> str | None:
    """The note for one prompt, or None to add nothing."""
    text = " ".join(prompt.split())
    if not text or text.startswith("/"):
        return None  # empty, or a slash command
    excerpt = text[:EXCERPT_CHARS] + ("…" if len(text) > EXCERPT_CHARS else "")
    tool = "`reconstruct` tool"
    if agent_id is not None:
        tool += f" (agent_id {json.dumps(agent_id, ensure_ascii=False)})"
    return (
        "cpersona: if answering this needs anything from earlier sessions, call the cpersona "
        f'{tool} before you answer, with a query for this request (suggested: "{excerpt}"). '
        "Before you say that a record does not exist, confirm it with an exact search for a term "
        "the record would contain; a recall score cannot tell you."
    )


def disabled(event: str) -> bool:
    names = {n.strip().lower() for n in os.environ.get("CPERSONA_HOOKS_DISABLE", "").split(",")}
    return event in names or "all" in names


def note_for(event: str, payload: dict) -> tuple[str, str] | None:
    """(hook event name, note) for this event and payload, or None to add nothing."""
    source = payload.get("source") or ""
    agent_id = configured_agent_id()
    if event == "session-start":
        # "" is a host that does not send source; resume keeps the conversation it had, and
        # compact has its own note.
        if source in ("startup", "clear", ""):
            return "SessionStart", session_start_note(agent_id)
        return None
    if event == "compact":
        return ("SessionStart", compact_note(agent_id)) if source == "compact" else None
    if event == "prompt":
        prompt = payload.get("prompt")
        note = prompt_note(prompt, agent_id) if isinstance(prompt, str) else None
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
