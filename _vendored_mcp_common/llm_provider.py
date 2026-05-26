"""
Cloto MCP Common: LLM Provider Base
Shared logic for OpenAI-compatible LLM provider MCP servers.
Extracted from deepseek/server.py and cerebras/server.py.

Provides:
- LLM API call via the kernel proxy (MGP S13.4)
- Message building (system prompt, chat messages)
- Response parsing (content extraction, tool-call parsing)
- Common MCP tool definitions and handlers
"""

import asyncio
import contextvars
import json
import os
import platform
import re
import shutil
import sys
from dataclasses import dataclass, field

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolRequest, TextContent

# Per-request flag set by the CallToolRequest wrapper when the incoming
# params include ``_mgp.stream: true``. Read from inside the @server.call_tool()
# handler to branch into the streaming path. Using a ContextVar (vs attributes
# on the server) because multiple concurrent requests may be in flight.
_mgp_stream_requested: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "cloto_mgp_stream_requested", default=False
)

# Maximum number of recent chunks retained per request for MGP §12.9
# gap-retransmission. Dropped chunks result in a gap_unrecoverable notification.
_CHUNK_BUFFER_MAX: int = 100

# Memory injection mode for build_chat_messages().
# "xml_user_prefix" (default): pack recalled memories into a <background_memories>
#   XML block prepended to the user message — prevents topic-drift by making it
#   clear to the LLM that memories are reference material, not active chat turns.
# "chat": legacy behaviour — insert memories as interleaved role=user/assistant
#   chat turns (kept for rollback via CLOTO_MEMORY_INJECTION=chat).
_MEMORY_INJECTION_MODE: str = os.environ.get("CLOTO_MEMORY_INJECTION", "xml_user_prefix")

# ISO 639-1 → English language name. Used by build_system_prompt's Layer 1.5
# when ClotoCore injects metadata["response_language"]. Unknown codes fall
# through to the raw value so any LLM that understands ISO 639-1 still works.
_LANGUAGE_NAMES: dict[str, str] = {
    "ja": "Japanese",
    "en": "English",
    "zh": "Chinese",
    "ko": "Korean",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "pt": "Portuguese",
    "ru": "Russian",
    "it": "Italian",
}


@dataclass
class StreamState:
    """Per-request streaming state used by the MGP interceptor and handler.

    Lives in ``_active_streams[request_id]`` for the lifetime of a
    ``handle_think_with_tools_streaming`` invocation. The handler publishes
    incremental progress; the interceptor reads it to build cancel
    partial_result payloads and to retransmit on gap notifications.
    """

    request_id: int
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    accumulated_text: str = ""
    # Each entry is (index, content_dict). Bounded by _CHUNK_BUFFER_MAX.
    chunk_buffer: list[tuple[int, dict]] = field(default_factory=list)
    last_index: int = -1
    cancelled_reason: str | None = None


# Per-process registry of in-flight streaming requests. Keyed by JSON-RPC
# request_id. Populated by handle_think_with_tools_streaming on entry and
# removed on exit (finally). Read by the MGP interceptor's cancel and gap
# handlers.
_active_streams: dict[int, StreamState] = {}


def _record_chunk(state: StreamState, index: int, content: dict) -> None:
    """Append a chunk to the ring-bounded buffer and update last_index."""
    state.chunk_buffer.append((index, content))
    if len(state.chunk_buffer) > _CHUNK_BUFFER_MAX:
        del state.chunk_buffer[0]
    state.last_index = index


# ============================================================
# Provider Configuration
# ============================================================


def _detect_host_os() -> str:
    """Build a concise OS summary string for the system prompt.

    Examples:
      "Windows 11 (10.0.26200), shell: PowerShell"
      "Linux 6.5.0-44 (Ubuntu 24.04), shell: bash"
      "Darwin 23.5.0 (macOS 14.5), shell: zsh"
    """
    system = platform.system()  # Windows / Linux / Darwin
    release = platform.release()  # 10.0.26200 / 6.5.0-44 / 23.5.0
    version = platform.version()  # full version string

    if system == "Windows":
        # platform.release() returns "11" on modern Python/Win11, or "10" on older.
        # platform.version() returns the full build string e.g. "10.0.26200".
        win_ver = release  # "10" or "11"
        if release == "10":
            # Disambiguate Win10 vs Win11 via build number
            try:
                build = int(version.split(".")[-1]) if version else 0
                if build >= 22000:
                    win_ver = "11"
            except (ValueError, IndexError):
                pass
        os_part = f"Windows {win_ver} ({version})"
    elif system == "Darwin":
        mac_ver = platform.mac_ver()[0]  # e.g. "14.5"
        os_part = f"macOS {mac_ver} (Darwin {release})" if mac_ver else f"Darwin {release}"
    elif system == "Linux":
        # Try freedesktop os-release for distro name
        distro = ""
        for p in ("/etc/os-release", "/usr/lib/os-release"):
            if os.path.isfile(p):
                try:
                    with open(p) as f:
                        for line in f:
                            if line.startswith("PRETTY_NAME="):
                                distro = line.split("=", 1)[1].strip().strip('"')
                                break
                except OSError:
                    pass
                break
        os_part = f"Linux {release} ({distro})" if distro else f"Linux {release}"
    else:
        os_part = f"{system} {release}"

    # Detect default shell
    shell_path = os.environ.get("SHELL") or os.environ.get("COMSPEC") or ""
    shell_name = os.path.basename(shell_path).removesuffix(".exe") if shell_path else "unknown"
    # On Windows, also check for PowerShell availability
    if system == "Windows" and shell_name in ("cmd", "unknown"):
        if shutil.which("pwsh") or shutil.which("powershell"):
            shell_name = "PowerShell"

    return f"{os_part}, shell: {shell_name}"


_HOST_OS_SUMMARY: str = _detect_host_os()


@dataclass
class ProviderConfig:
    """Configuration for an LLM provider server."""

    provider_id: str
    model_id: str
    api_url: str = "http://127.0.0.1:8082/v1/chat/completions"
    request_timeout: int = 120
    supports_tools: bool = True
    display_name: str = ""
    reasoning_think_prefill: bool = False
    # MGP §12: opt-in streaming. When True and the caller sets `_mgp.stream: true`
    # on the tools/call params, handle_think_with_tools emits
    # notifications/mgp.stream.chunk as tokens arrive. Default off to preserve
    # existing one-shot behavior for non-MGP clients.
    supports_streaming: bool = False

    def __post_init__(self):
        if not self.display_name:
            self.display_name = self.provider_id.capitalize()


# ============================================================
# LLM Utilities (ported from crates/shared/src/llm.rs)
# ============================================================


# Models whose name suggests they run in a thinking / reasoning mode (emit
# <think>...</think> blocks, reasoning_content, or need the iter-2 </think>
# prefill workaround). Matched with word-boundary regex below.
#
# Positive hints — any match implies reasoning=True:
#   qwen3 / qwen-3 : Qwen3 series (including 3.5 / 3.6); unified chat+thinking
#   qwq            : Qwen's QwQ reasoning line
#   r1             : DeepSeek-R1 and R1-distill derivatives
#   reasoner       : deepseek-reasoner etc.
#   thinking       : models that self-label (Llama-Thinking etc.)
#   o1- / o3-      : OpenAI reasoning families
_REASONING_HINT_RE = re.compile(
    r"(?:^|[^a-z0-9])(?:qwen-?3|qwq|r1|reasoner|thinking|o[13])(?:[^a-z0-9]|$)",
    re.IGNORECASE,
)
# Negative hint — a model explicitly labelled *-instruct without any reasoning
# hint is treated as non-reasoning (helps Qwen2.5-Instruct, Llama-Instruct).
_INSTRUCT_HINT_RE = re.compile(r"(?:^|[^a-z0-9])instruct(?:[^a-z0-9]|$)", re.IGNORECASE)


def _model_suggests_reasoning(model_id: str) -> bool | None:
    """Heuristic: does this model name indicate a reasoning / thinking model?

    Returns True / False when the name is recognisable, or None when it's
    empty or ambiguous (caller should fall back to the configured default).
    Reasoning hints win over `instruct` so e.g. Qwen3-*-Instruct (dual-mode)
    still comes out True.
    """
    if not model_id:
        return None
    if _REASONING_HINT_RE.search(model_id):
        return True
    if _INSTRUCT_HINT_RE.search(model_id):
        return False
    return None


def model_supports_tools(config: ProviderConfig) -> bool:
    """Check if the configured model supports tool schemas.

    deepseek-reasoner (R1) explicitly does not support tool schemas.
    Providers with supports_tools=False (e.g. Cerebras) always return False.
    """
    if not config.supports_tools:
        return False
    return "reasoner" not in config.model_id


def build_system_prompt(agent: dict, tools: list[dict] | None = None) -> str:
    """Build a 5-layer system prompt for a Cloto agent.

    Layers:
      1. Identity   — agent name + platform intro
      2. Platform   — Cloto local/self-hosted description
      3. Persona    — structured role/expertise/style from metadata.persona
      4. Capabilities — available tools (dynamic), memory, avatar
      5. Behavior   — tool-usage guidance + free-text description
    """
    name = agent.get("name", "Agent")
    description = agent.get("description", "")
    metadata = agent.get("metadata", {})

    lines: list[str] = []

    # --- [1] Identity ---
    lines.append(f"You are {name}, an AI agent running on the Cloto platform.")

    # --- [1.5] Language (operator's preferred response language) ---
    # ClotoCore injects metadata["response_language"] (ISO 639-1) when the
    # global "Inject response language" setting is enabled. Agent description
    # (layer 5) can still override this since it appears later in the prompt.
    response_language = metadata.get("response_language")
    if response_language:
        lang_name = _LANGUAGE_NAMES.get(response_language, response_language)
        lines.append(
            f"Always respond in {lang_name} unless the operator explicitly asks otherwise."
        )

    # --- [2] Platform ---
    lines.append(
        "Cloto is a local, self-hosted AI container system — "
        "all data stays on your operator's hardware and is never sent to external services."
    )
    shell_examples = (
        "e.g. dir, type, findstr, Get-ChildItem"
        if platform.system() == "Windows"
        else "e.g. ls, cat, grep, find"
    )
    lines.append(
        f"Host OS: {_HOST_OS_SUMMARY}. "
        f"When using execute_command, always use commands native to this OS "
        f"({shell_examples})."
    )

    # --- [3] Persona (from metadata.persona JSON) ---
    persona_raw = metadata.get("persona", "")
    if persona_raw:
        try:
            p = json.loads(persona_raw) if isinstance(persona_raw, str) else persona_raw
            if p.get("role"):
                lines.append(f"Your role: {p['role']}")
            if p.get("expertise"):
                exp = p["expertise"]
                if isinstance(exp, list):
                    lines.append(f"Your areas of expertise: {', '.join(exp)}")
                else:
                    lines.append(f"Your areas of expertise: {exp}")
            if p.get("communication_style"):
                lines.append(f"Communication style: {p['communication_style']}")
        except (json.JSONDecodeError, TypeError):
            pass

    # --- [4] Capabilities ---
    if metadata.get("preferred_memory"):
        lines.append("You have persistent memory — you can store and recall past conversations.")

    avatar_desc = metadata.get("avatar_description", "")
    if avatar_desc:
        lines.append(f"Your visual appearance/avatar: {avatar_desc}")

    # Dynamic tool listing — lets the model know exactly what it can do
    if tools:
        tool_lines = []
        for t in tools:
            fn = t.get("function", {})
            tname = fn.get("name", "")
            tdesc = fn.get("description", "")
            if tname:
                short_desc = tdesc.split(".")[0].strip() if tdesc else ""
                tool_lines.append(f"  - {tname}: {short_desc}")
        if tool_lines:
            lines.append("")
            lines.append(f"You have access to {len(tool_lines)} tools:")
            lines.extend(tool_lines)

    # --- [5] Behavior ---
    lines.append("")
    lines.append(
        "When the user's request can be fulfilled by using a tool, "
        "prefer calling the appropriate tool over guessing or explaining "
        "how to do it manually. Execute first, explain after."
    )
    lines.append("If no tool can help, respond honestly based on your knowledge.")
    lines.append(
        "Never state the current time, date, or day of the week without first "
        "verifying it by calling get_current_time. Recalled memories may contain "
        "outdated time references — do not echo or extrapolate from them."
    )
    lines.append(
        "Prefer fast tools. Only use high-latency tools (generate_image, "
        "deep_research, transcribe, analyze_image) when the user explicitly requests them."
    )
    lines.append(
        "Do not call update_profile or archive_episode — "
        "the system handles these automatically in the background."
    )

    if description:
        lines.append("")
        lines.append(description)

    return "\n".join(lines)


def _context_msg_to_role_content(msg: dict) -> tuple[str, str]:
    """Map a context message to (role, content) for the OpenAI messages array."""
    source = msg.get("source", {})
    src_type = source.get("type", "") if isinstance(source, dict) else ""
    content = msg.get("content", "")
    if src_type in ("User",) or "User" in source or "user" in source:
        role = "user"
        ctx_name = source.get("name", "") if isinstance(source, dict) else ""
        if ctx_name and ctx_name not in ("User", ""):
            content = f"[{ctx_name}]: {content}"
    elif src_type in ("Agent",) or "Agent" in source or "agent" in source:
        role = "assistant"
    else:
        role = "system"
    return role, content


def _parse_context_timestamp(ts: str) -> str | None:
    """Parse an ISO-8601 timestamp and format for LLM context display."""
    try:
        from datetime import datetime

        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        # Convert to local timezone for user-friendly display
        local_dt = dt.astimezone()
        return local_dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return None


def build_chat_messages(
    agent: dict,
    message: dict,
    context: list[dict],
    tools: list[dict] | None = None,
) -> list[dict]:
    """Build the standard OpenAI-compatible messages array.

    Returns [system_message, ...context_messages, user_message].
    When tools are provided, the system prompt includes a dynamic tool listing.
    """
    messages = [{"role": "system", "content": build_system_prompt(agent, tools)}]

    # Split context into memory (CPersona recall) and conversation (channel history)
    memory_msgs = [m for m in context if m.get("context_type") != "conversation"]
    conversation_msgs = [m for m in context if m.get("context_type") == "conversation"]

    if memory_msgs and _MEMORY_INJECTION_MODE == "chat":
        # Legacy: insert memories as interleaved chat turns (rollback path).
        messages.append(
            {
                "role": "system",
                "content": (
                    "[The following are recalled memories from past conversations. "
                    "They are NOT recent messages. Time references in them may be outdated.]"
                ),
            }
        )
        for msg in memory_msgs:
            role, content = _context_msg_to_role_content(msg)
            ts = msg.get("timestamp", "")
            if ts and role != "system":
                ts_label = _parse_context_timestamp(ts)
                if ts_label:
                    messages.append(
                        {"role": "system", "content": f"[The following message is from {ts_label}]"}
                    )
            messages.append({"role": role, "content": content})
        messages.append(
            {
                "role": "system",
                "content": "[End of recalled memories.]",
            }
        )
    # xml_user_prefix: memories accumulated into XML block, injected into user message below

    if conversation_msgs:
        messages.append(
            {
                "role": "system",
                "content": (
                    "[Recent messages from this channel for background context only. "
                    "Do NOT continue or repeat these topics unless the user "
                    "explicitly asks about them.]"
                ),
            }
        )
        for msg in conversation_msgs:
            role, content = _context_msg_to_role_content(msg)
            messages.append({"role": role, "content": content})
        messages.append(
            {
                "role": "system",
                "content": (
                    "[END OF CONTEXT. IMPORTANT: The next message is the CURRENT user message. "
                    "Respond ONLY to it. Ignore conversation history unless directly relevant.]"
                ),
            }
        )

    if not memory_msgs and not conversation_msgs and context:
        # Fallback for legacy context without context_type
        for msg in context:
            role, content = _context_msg_to_role_content(msg)
            messages.append({"role": role, "content": content})

    # Inject external message context so the LLM can use origin-specific tools
    msg_metadata = message.get("metadata", {})
    external_source = msg_metadata.get("external_source")
    if external_source:
        context_parts = [f"source: {external_source}"]
        for key in ("external_channel_id", "external_message_id", "external_guild_id"):
            val = msg_metadata.get(key)
            if val:
                # Strip "external_" prefix for readability
                context_parts.append(f"{key.removeprefix('external_')}: {val}")
        sender = msg_metadata.get("external_sender_name")
        if sender:
            context_parts.append(f"sender: {sender}")
        messages.append(
            {
                "role": "system",
                "content": (
                    "[External message context: "
                    + ", ".join(context_parts)
                    + ". Use these IDs if you need to call tools targeting this message.]"
                ),
            }
        )

        # Inject reply reference context if this message is a reply
        ref_raw = msg_metadata.get("external_reference")
        if ref_raw:
            try:
                ref_data = json.loads(ref_raw) if isinstance(ref_raw, str) else ref_raw
                if isinstance(ref_data, dict):
                    ref_author = ref_data.get("author_name", "Unknown")
                    ref_content = ref_data.get("content", "")
                    if ref_content:
                        # Truncate long messages to avoid context bloat
                        if len(ref_content) > 200:
                            ref_content = ref_content[:200] + "..."
                        messages.append(
                            {
                                "role": "system",
                                "content": (
                                    "[This is a reply to a message by "
                                    f'{ref_author}: "{ref_content}"]'
                                ),
                            }
                        )
            except (json.JSONDecodeError, TypeError):
                pass

    # Extract user name from source for multi-user awareness
    source = message.get("source", {})
    user_name = ""
    if isinstance(source, dict) and source.get("type") == "User":
        user_name = source.get("name", "")
    user_content = message.get("content", "")
    if user_name and user_name not in ("User", ""):
        raw_user = f"[{user_name}]: {user_content}"
    else:
        raw_user = user_content

    if memory_msgs and _MEMORY_INJECTION_MODE != "chat":
        # xml_user_prefix: pack memories into a <background_memories> block
        # prepended to the user message so the LLM treats them as reference
        # material rather than active conversation turns (anti-topic-drift).
        xml_lines = [
            "<background_memories>",
            (
                "<!-- Recalled memories from past conversations. NOT part of the "
                "current conversation. Time references may be outdated. -->"
            ),
        ]
        for msg in memory_msgs:
            role, content = _context_msg_to_role_content(msg)
            ts = msg.get("timestamp", "")
            ts_label = _parse_context_timestamp(ts) if ts else None
            prefix = f"[{ts_label}] " if ts_label else ""
            src_label = "[agent] " if role == "assistant" else ""
            xml_lines.append(f"{prefix}{src_label}{content}")
        xml_lines.append("</background_memories>")
        messages.append({"role": "user", "content": "\n".join(xml_lines) + "\n\n" + raw_user})
    else:
        messages.append({"role": "user", "content": raw_user})
    return messages


def _check_api_error(label: str, response_data: dict) -> None:
    """Raise ValueError if the response contains an API error (OpenAI or Cerebras format)."""
    if "error" in response_data:
        error = response_data["error"]
        msg = error.get("message", str(error)) if isinstance(error, dict) else str(error)
        raise ValueError(f"{label} API Error: {msg}")
    if response_data.get("type", "").endswith("error"):
        msg = response_data.get("message", "Unknown error")
        raise ValueError(f"{label} API Error: {msg}")


def parse_chat_content(config: ProviderConfig, response_data: dict) -> str:
    """Extract text content from a chat completions response.

    Ported from llm::parse_chat_content().
    """
    _check_api_error(config.display_name, response_data)

    try:
        return response_data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise ValueError(
            f"Invalid {config.display_name} API response: missing choices[0].message.content: {e}"
        ) from e


_TOOL_CALL_BLOCK_RE = re.compile(r"<tool_call>\s*(.*?)(?:</tool_call>|\Z)", re.DOTALL)
_FUNCTION_TAG_RE = re.compile(r"<function=([^>\s]+)>")
_PARAMETER_TAG_RE = re.compile(r"<parameter=([^>\s]+)>(.*?)</parameter>", re.DOTALL)
_THINK_TAG_RE = re.compile(r"</?think>")


def _extract_tool_calls_from_text(text: str) -> list[dict]:
    """Parse <tool_call>...</tool_call> blocks out of free-form text.

    Handles the emission quirk seen on Qwen3 / DeepSeek-R1 style reasoning models
    where the model writes tool calls as Hermes-style XML (or OpenAI-style JSON)
    inside its `<think>` block instead of via the structured `tool_calls[]`
    channel. Tolerates a trailing unclosed `<tool_call>` (EOS truncation) —
    returns whatever fully-closed parameters are present and drops partials.
    """
    if not text:
        return []
    calls: list[dict] = []
    for idx, match in enumerate(_TOOL_CALL_BLOCK_RE.finditer(text)):
        body = match.group(1).strip()
        if not body:
            continue
        # OpenAI JSON form: {"name":"...","arguments":{...}}
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict) and isinstance(parsed.get("name"), str):
                args = parsed.get("arguments", {})
                if not isinstance(args, dict):
                    args = {}
                calls.append(
                    {
                        "id": f"reasoning_fallback_{idx}",
                        "name": parsed["name"],
                        "arguments": args,
                    }
                )
                continue
        except (json.JSONDecodeError, ValueError):
            pass
        # Hermes XML form: <function=NAME><parameter=KEY>VALUE</parameter>...
        fn_match = _FUNCTION_TAG_RE.search(body)
        if not fn_match:
            continue
        args = {pm.group(1): pm.group(2).strip() for pm in _PARAMETER_TAG_RE.finditer(body)}
        calls.append(
            {
                "id": f"reasoning_fallback_{idx}",
                "name": fn_match.group(1),
                "arguments": args,
            }
        )
    return calls


def _strip_reasoning_artifacts(text: str) -> str:
    """Remove <think>/</think> wrappers and any trailing partial <tool_call>...

    Used as a last-resort surface when the upstream returns only reasoning text
    and no structured content — guarantees the UI sees *something* rather than
    an empty bubble, while hiding the XML tool-call trailer that would otherwise
    leak into the conversation.
    """
    if not text:
        return ""
    cleaned = _THINK_TAG_RE.sub("", text)
    tc_idx = cleaned.find("<tool_call>")
    if tc_idx != -1:
        cleaned = cleaned[:tc_idx]
    return cleaned.strip()


def parse_chat_think_result(config: ProviderConfig, response_data: dict) -> dict:
    """Parse a chat completions response into a ThinkResult.

    Returns either:
      {"type": "final", "content": "..."}
    or:
      {"type": "tool_calls", "assistant_content": "...", "calls": [...]}

    Ported from llm::parse_chat_think_result().
    """
    _check_api_error(config.display_name, response_data)

    try:
        choice = response_data["choices"][0]
    except (KeyError, IndexError, TypeError) as e:
        raise ValueError(f"Invalid API response: missing choices[0]: {e}") from e

    message_obj = choice.get("message", {})
    finish_reason = choice.get("finish_reason", "stop")

    if finish_reason == "tool_calls" or "tool_calls" in message_obj:
        tool_calls_arr = message_obj.get("tool_calls", [])
        calls = []
        for tc in tool_calls_arr:
            tc_id = tc.get("id", "")
            function = tc.get("function", {})
            name = function.get("name", "")
            arguments_str = function.get("arguments", "{}")
            try:
                arguments = json.loads(arguments_str)
            except json.JSONDecodeError:
                arguments = {}

            if tc_id and name:
                calls.append({"id": tc_id, "name": name, "arguments": arguments})

        if calls:
            # Prefer content, fall back to reasoning_content (DeepSeek R1 etc.).
            # Qwen3 frequently returns `"\n\n"` in content while the real prose
            # is in reasoning_content — treat whitespace-only as empty so the
            # fallback kicks in and the kernel doesn't emit a blank-label
            # thinking step.
            raw_content = message_obj.get("content") or ""
            reasoning = message_obj.get("reasoning_content") or ""
            assistant_content = raw_content if raw_content.strip() else (reasoning or None)
            return {
                "type": "tool_calls",
                "assistant_content": assistant_content,
                "calls": calls,
            }

    raw_content = message_obj.get("content") or ""
    reasoning = message_obj.get("reasoning_content") or ""

    # P1: Reasoning-model quirk — tool calls emitted as XML/JSON text inside
    # <think>. Harvest them so the agentic loop can proceed normally.
    fallback_calls = _extract_tool_calls_from_text(reasoning)
    if not fallback_calls:
        fallback_calls = _extract_tool_calls_from_text(raw_content)
    if fallback_calls:
        return {
            "type": "tool_calls",
            "assistant_content": raw_content if raw_content.strip() else (reasoning or None),
            "calls": fallback_calls,
        }

    # P1.b: Empty content but we have reasoning — surface stripped reasoning so
    # the UI never shows a blank bubble.
    content = raw_content
    if not content and reasoning:
        content = _strip_reasoning_artifacts(reasoning)

    if not content:
        try:
            print(
                f"[llm_provider] upstream returned empty content "
                f"provider={config.display_name} finish_reason={finish_reason} "
                f"reasoning_len={len(reasoning)} usage={response_data.get('usage')}",
                file=sys.stderr,
                flush=True,
            )
        except OSError:
            pass

    return {"type": "final", "content": content}


# ============================================================
# LLM API Call
# ============================================================


class LlmApiError(Exception):
    """Structured error from the LLM proxy with an error code."""

    def __init__(self, message: str, code: str = "unknown", status_code: int = 0):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code


def _sanitize_tool_names(tools: list[dict]) -> tuple[list[dict], dict[str, str]]:
    """Replace dots in tool names with underscores for LLM API compatibility.

    Many LLM providers (DeepSeek, OpenAI) require tool names to match
    ^[a-zA-Z0-9_-]+$. MGP tools use dots (e.g. mgp.health.ping).

    Returns (sanitized_tools, reverse_map) where reverse_map maps
    sanitized names back to original names.
    """
    sanitized = []
    reverse_map: dict[str, str] = {}
    for tool in tools:
        fn = tool.get("function", {})
        original_name = fn.get("name", "")
        safe_name = original_name.replace(".", "_")
        if safe_name != original_name:
            reverse_map[safe_name] = original_name
            tool = json.loads(json.dumps(tool))  # deep copy
            tool["function"]["name"] = safe_name
        sanitized.append(tool)
    return sanitized, reverse_map


def _restore_tool_names(response_data: dict, reverse_map: dict[str, str]) -> dict:
    """Restore original tool names (with dots) in LLM response."""
    if not reverse_map:
        return response_data
    try:
        for choice in response_data.get("choices", []):
            for tc in choice.get("message", {}).get("tool_calls", []):
                fn = tc.get("function", {})
                name = fn.get("name", "")
                if name in reverse_map:
                    fn["name"] = reverse_map[name]
    except (KeyError, TypeError):
        pass
    return response_data


async def call_llm_api(
    config: ProviderConfig,
    messages: list[dict],
    tools: list[dict] | None = None,
) -> dict:
    """Send a request via the kernel LLM proxy (MGP S13.4)."""
    body: dict = {
        "model": config.model_id,
        "messages": messages,
        "stream": False,
    }

    reverse_map: dict[str, str] = {}
    if tools and model_supports_tools(config):
        sanitized, reverse_map = _sanitize_tool_names(tools)
        body["tools"] = sanitized

    try:
        async with httpx.AsyncClient(timeout=config.request_timeout) as client:
            response = await client.post(
                config.api_url,
                json=body,
                headers={
                    "X-LLM-Provider": config.provider_id,
                    "Content-Type": "application/json",
                },
            )
    except httpx.ConnectError:
        raise LlmApiError(
            "Cannot connect to LLM proxy. Ensure the kernel is running.",
            "connection_failed",
        )
    except httpx.TimeoutException:
        raise LlmApiError(
            f"LLM request timed out after {config.request_timeout}s.",
            "timeout",
        )

    if response.status_code >= 400:
        # Extract structured error from proxy response
        try:
            err_body = response.json()
            err_obj = err_body.get("error", {})
            msg = err_obj.get("message", f"HTTP {response.status_code}")
            code = err_obj.get("code", "unknown")
        except Exception:
            msg = f"HTTP {response.status_code}"
            code = "unknown"
        raise LlmApiError(msg, code, response.status_code)

    return _restore_tool_names(response.json(), reverse_map)


async def call_llm_api_streaming(
    config: ProviderConfig,
    messages: list[dict],
    tools: list[dict] | None = None,
):
    """Stream an OpenAI-compatible /v1/chat/completions response chunk-by-chunk.

    Yields parsed SSE chunks (dicts). The terminating ``data: [DONE]`` marker
    is consumed internally — callers iterate until the generator completes
    naturally.

    Tool name sanitization (dot → underscore) is applied on the outbound
    request. The kernel agentic loop already restores dot-names on received
    tool calls, so no reverse-map is exposed here.
    """
    body: dict = {
        "model": config.model_id,
        "messages": messages,
        "stream": True,
        # OpenAI-compatible opt-in for receiving the final `usage` block
        # during a streaming response. Without this, LM Studio / OpenAI /
        # vLLM emit chunks but no usage, and downstream features like the
        # Dashboard's ContextUsageBadge have nothing to render.
        "stream_options": {"include_usage": True},
    }

    if tools and model_supports_tools(config):
        sanitized, _ = _sanitize_tool_names(tools)
        body["tools"] = sanitized

    try:
        async with httpx.AsyncClient(timeout=config.request_timeout) as client:
            async with client.stream(
                "POST",
                config.api_url,
                json=body,
                headers={
                    "X-LLM-Provider": config.provider_id,
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                },
            ) as response:
                if response.status_code >= 400:
                    # Drain body to extract proxy error structure
                    body_bytes = await response.aread()
                    try:
                        err_body = json.loads(body_bytes.decode("utf-8", errors="replace"))
                        err_obj = err_body.get("error", {})
                        msg = err_obj.get("message", f"HTTP {response.status_code}")
                        code = err_obj.get("code", "unknown")
                    except Exception:
                        msg = f"HTTP {response.status_code}"
                        code = "unknown"
                    raise LlmApiError(msg, code, response.status_code)

                done_received = False
                async for raw_line in response.aiter_lines():
                    line = raw_line.strip()
                    if not line:
                        continue
                    if not line.startswith("data:"):
                        # Some servers emit comment lines starting with ":" — skip
                        continue
                    payload = line[len("data:") :].strip()
                    if payload == "[DONE]":
                        done_received = True
                        return
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        # Ignore malformed lines; upstream sometimes emits blanks
                        continue
                    yield chunk

                # If the upstream closes the stream without emitting the
                # OpenAI/SSE [DONE] sentinel, the generation was cut off
                # (network reset, proxy timeout, provider crash, etc.).
                # Surfacing this as an error lets the streaming handler
                # flag the response as partial instead of returning the
                # truncated text as if it were complete (MGP §12.5
                # final-authoritative guarantee intact: the final result
                # will carry `_mgp.partial=true`).
                if not done_received:
                    raise LlmApiError(
                        "Upstream closed stream before [DONE] marker (response may be truncated)",
                        "upstream_truncated",
                    )
    except httpx.ConnectError:
        raise LlmApiError(
            "Cannot connect to LLM proxy. Ensure the kernel is running.",
            "connection_failed",
        )
    except httpx.TimeoutException:
        raise LlmApiError(
            f"LLM request timed out after {config.request_timeout}s.",
            "timeout",
        )


# ============================================================
# Common MCP Tool Definitions
# ============================================================

THINK_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "agent": {
            "type": "object",
            "description": "Agent metadata (name, description, metadata)",
        },
        "message": {
            "type": "object",
            "description": "User message with 'content' field",
        },
        "context": {
            "type": "array",
            "description": "Conversation context messages",
            "items": {"type": "object"},
        },
    },
    "required": ["agent", "message", "context"],
}

THINK_WITH_TOOLS_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "agent": {
            "type": "object",
            "description": "Agent metadata (name, description, metadata)",
        },
        "message": {
            "type": "object",
            "description": "User message with 'content' field",
        },
        "context": {
            "type": "array",
            "description": "Conversation context messages",
            "items": {"type": "object"},
        },
        "tools": {
            "type": "array",
            "description": "Available tool schemas (OpenAI format)",
            "items": {"type": "object"},
        },
        "tool_history": {
            "type": "array",
            "description": "Prior tool calls and results",
            "items": {"type": "object"},
        },
    },
    "required": [
        "agent",
        "message",
        "context",
        "tools",
        "tool_history",
    ],
}


# ============================================================
# Common MCP Tool Handlers
# ============================================================


def _error_response(error: Exception) -> list[TextContent]:
    """Build a structured error response for tool handlers."""
    if isinstance(error, LlmApiError):
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "error": error.message,
                        "error_code": error.code,
                    }
                ),
            )
        ]
    import logging

    logging.getLogger(__name__).error("Unexpected error in LLM handler: %s", error, exc_info=True)
    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "error": f"An unexpected error occurred: {type(error).__name__}: {error}",
                    "error_code": "internal",
                }
            ),
        )
    ]


def extract_usage(response_data: dict) -> dict | None:
    """Pull the `usage` block out of an LLM response, if present.

    Returns the raw dict so the kernel can normalize it (it already has to handle
    both OpenAI `prompt_tokens`/`completion_tokens` and Anthropic `input_tokens`/
    `output_tokens` for the mind.claude provider). Returns None when the upstream
    didn't report usage at all, in which case the kernel falls back to its
    pre-flight estimate.
    """
    usage = response_data.get("usage") if isinstance(response_data, dict) else None
    return usage if isinstance(usage, dict) else None


async def handle_think(config: ProviderConfig, arguments: dict) -> list[TextContent]:
    """Handle 'think' tool: simple text generation."""
    try:
        agent = arguments.get("agent", {})
        message = arguments.get("message", {})
        context = arguments.get("context", [])

        messages = build_chat_messages(agent, message, context)
        response_data = await call_llm_api(config, messages)
        content = parse_chat_content(config, response_data)

        payload = {"type": "final", "content": content}
        if (usage := extract_usage(response_data)) is not None:
            payload["usage"] = usage
        return [TextContent(type="text", text=json.dumps(payload))]
    except Exception as e:
        return _error_response(e)


def _build_think_with_tools_messages(
    agent: dict,
    message: dict,
    context: list,
    tools: list,
    tool_history: list,
    config: ProviderConfig,
) -> list[dict]:
    """Build the message array for think_with_tools (shared by sync + streaming paths)."""
    messages = build_chat_messages(agent, message, context, tools=tools)
    # Sanitize dot-names in tool_history for LLM API compatibility
    for entry in tool_history:
        if "tool_calls" in entry:
            entry = json.loads(json.dumps(entry))  # deep copy
            for tc in entry.get("tool_calls", []):
                fn = tc.get("function", {})
                name = fn.get("name", "")
                safe = name.replace(".", "_")
                if safe != name:
                    fn["name"] = safe
        elif entry.get("role") == "tool" and "name" in entry:
            name = entry.get("name", "")
            safe = name.replace(".", "_")
            if safe != name:
                entry = {**entry, "name": safe}
        messages.append(entry)

    # P2: On reasoning models, force-exit the <think> block via an assistant
    # prefill. Prevents the Qwen3 / R1 failure mode where the follow-up tool
    # call is emitted as XML text inside the thinking block.
    #
    # We prefill a full empty block (<think>\n\n</think>\n\n) rather than just
    # the closing tag, because LM Studio / llama.cpp ignore the official
    # chat_template_kwargs.enable_thinking knob (upstream bug), so the template
    # still injects <think> at the start of every assistant turn. A close-only
    # prefill leaves room for the model to re-enter thinking; a full empty
    # block reliably produces 0 reasoning tokens and keeps the completion
    # budget available for the actual answer and tool calls.
    #
    # We also apply the prefill on iteration 1 (not just 2+), because Qwen3.5
    # defaults to thinking-on with no soft-switch like Qwen3's /no_think, so
    # the first generation burns the same budget without the prefill.
    if config.reasoning_think_prefill:
        messages.append({"role": "assistant", "content": "<think>\n\n</think>\n\n"})
    return messages


async def handle_think_with_tools(config: ProviderConfig, arguments: dict) -> list[TextContent]:
    """Handle 'think_with_tools' tool: may return tool calls or final text."""
    try:
        agent = arguments.get("agent", {})
        message = arguments.get("message", {})
        context = arguments.get("context", [])
        tools = arguments.get("tools", [])
        tool_history = arguments.get("tool_history", [])

        messages = _build_think_with_tools_messages(
            agent, message, context, tools, tool_history, config
        )
        response_data = await call_llm_api(config, messages, tools)
        result = parse_chat_think_result(config, response_data)
        if (usage := extract_usage(response_data)) is not None:
            result["usage"] = usage

        return [TextContent(type="text", text=json.dumps(result))]
    except Exception as e:
        return _error_response(e)


async def handle_think_with_tools_streaming(
    config: ProviderConfig, arguments: dict, ctx
) -> list[TextContent]:
    """Streaming variant of ``think_with_tools`` (MGP §12).

    Emits ``notifications/mgp.stream.chunk`` as tokens arrive from the upstream
    LLM and returns the final ``CallToolResult`` containing the complete
    accumulated content (MGP §12.5: final response MUST carry the full result).

    Hardening (Plan 1.5):
    - Registers a :class:`StreamState` in ``_active_streams[request_id]`` so
      the MGP interceptor can observe progress for cancel / gap responses.
    - Polls ``state.cancel_event`` on every iteration; when set, exits the
      loop cleanly and returns ``{"type":"final","content":<partial>,
      "_mgp":{"streamed":True,"chunks_sent":N,"cancelled":True,
      "cancel_reason":<reason>}}``.
    - Keeps a bounded ring buffer (``_CHUNK_BUFFER_MAX`` entries) of emitted
      chunks for gap retransmission.
    - On mid-stream network / timeout errors where at least one chunk has
      been produced, returns a partial result with an ``error`` sub-object
      instead of discarding the accumulated text.

    Parameters
    ----------
    ctx:
        ``RequestContext`` obtained from ``server.request_context`` by the
        caller (decorator). Supplies ``request_id`` (for chunk routing) and
        ``session`` (for the write stream).
    """
    from _vendored_mcp_common.mgp_utils import send_mgp_stream_chunk

    request_id = getattr(ctx, "request_id", 0)
    session = ctx.session

    state = StreamState(request_id=request_id)
    _active_streams[request_id] = state

    try:
        agent = arguments.get("agent", {})
        message = arguments.get("message", {})
        context = arguments.get("context", [])
        tools = arguments.get("tools", [])
        tool_history = arguments.get("tool_history", [])

        messages = _build_think_with_tools_messages(
            agent, message, context, tools, tool_history, config
        )

        tool_calls_buffer: list[dict] = []
        finish_reason: str | None = None
        usage: dict | None = None
        index = 0

        stream_error: Exception | None = None

        try:
            async for chunk in call_llm_api_streaming(config, messages, tools):
                if state.cancel_event.is_set():
                    # Cancellation observed — break out without waiting for [DONE].
                    break

                chunk_usage = extract_usage(chunk)
                if chunk_usage is not None:
                    usage = chunk_usage

                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta") or {}
                if "finish_reason" in choice and choice["finish_reason"]:
                    finish_reason = choice["finish_reason"]

                # Buffer structured tool_calls (no streaming to client — they are
                # inherently partial and only the final assembled form is useful).
                delta_tool_calls = delta.get("tool_calls")
                if isinstance(delta_tool_calls, list):
                    for tc in delta_tool_calls:
                        tc_index = tc.get("index", 0)
                        while len(tool_calls_buffer) <= tc_index:
                            tool_calls_buffer.append({})
                        slot = tool_calls_buffer[tc_index]
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        if tc.get("type"):
                            slot["type"] = tc["type"]
                        fn_delta = tc.get("function") or {}
                        if fn_delta:
                            fn_slot = slot.setdefault("function", {})
                            if "name" in fn_delta:
                                fn_slot["name"] = (fn_slot.get("name") or "") + fn_delta["name"]
                            if "arguments" in fn_delta:
                                fn_slot["arguments"] = (fn_slot.get("arguments") or "") + fn_delta[
                                    "arguments"
                                ]

                # Stream text delta to the client.
                text_delta = delta.get("content") or ""
                if text_delta:
                    state.accumulated_text += text_delta
                    chunk_content = {"type": "text", "text": text_delta}
                    await send_mgp_stream_chunk(
                        session,
                        request_id=request_id,
                        index=index,
                        content=chunk_content,
                        done=False,
                    )
                    _record_chunk(state, index, chunk_content)
                    index += 1
        except (httpx.TimeoutException, httpx.ConnectError, LlmApiError) as e:
            # Recoverable mid-stream errors — if we have at least one chunk
            # emitted, surface a partial result rather than discarding it.
            stream_error = e

        # --- Cancel path: return the partial snapshot immediately. ------------
        if state.cancel_event.is_set():
            cancel_result: dict = {
                "type": "final",
                "content": state.accumulated_text,
                "_mgp": {
                    "streamed": True,
                    "chunks_sent": index,
                    "cancelled": True,
                    "cancel_reason": state.cancelled_reason or "unspecified",
                },
            }
            return [TextContent(type="text", text=json.dumps(cancel_result))]

        # --- Mid-stream error path (recoverable): partial result with error. --
        if stream_error is not None and state.accumulated_text:
            partial_result: dict = {
                "type": "final",
                "content": state.accumulated_text,
                "error": {
                    "code": (
                        stream_error.code
                        if isinstance(stream_error, LlmApiError)
                        else "stream_error"
                    ),
                    "message": str(stream_error),
                },
                "_mgp": {
                    "streamed": True,
                    "chunks_sent": index,
                    "partial": True,
                },
            }
            return [TextContent(type="text", text=json.dumps(partial_result))]
        if stream_error is not None:
            # No accumulated content — fall through to the standard error path.
            return _error_response(stream_error)

        # --- Normal completion: synthesize an OpenAI-shape response and reuse
        # the non-streaming parser (reasoning fallback, R1 quirks, etc.).
        synthetic: dict = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": state.accumulated_text,
                    },
                    "finish_reason": finish_reason,
                }
            ]
        }
        if tool_calls_buffer:
            # Restore dot-names on any emitted tool calls.
            # _sanitize_tool_names was applied inside call_llm_api_streaming and
            # the forward map is lost when the generator exits; however the
            # kernel agentic loop also performs dot-name recovery, so leaving
            # names as-is is safe. Keep underscore names for now.
            synthetic["choices"][0]["message"]["tool_calls"] = tool_calls_buffer
        if usage is not None:
            synthetic["usage"] = usage

        result = parse_chat_think_result(config, synthetic)
        if usage is not None:
            result["usage"] = usage

        # §12.5: final response carries the complete accumulated result.
        result["_mgp"] = {"streamed": True, "chunks_sent": index}

        return [TextContent(type="text", text=json.dumps(result))]
    except Exception as e:
        return _error_response(e)
    finally:
        _active_streams.pop(request_id, None)


# ============================================================
# Server Lifecycle Helper
# ============================================================


def _build_mgp_capabilities(config: "ProviderConfig") -> dict[str, dict]:
    """Build MGP Tier 3 capability declaration for an llm_provider-based server.

    All servers that run through :func:`run_server` share the same MGP feature
    surface — streaming chunks (§12.4), stream cancellation (§12.7 via
    ``mcp_stream_interceptor``), and gap detection (§12.9). Declaring the
    ``streaming`` extension here lets the kernel negotiate these features
    without each provider server duplicating the declaration.

    The returned dict is shaped ``{"mgp": {...}}`` so it can be passed to
    :meth:`mcp.server.Server.create_initialization_options` as
    ``experimental_capabilities`` — the kernel accepts
    ``capabilities.experimental.mgp`` as equivalent to ``capabilities.mgp``
    during the Python SDK transition period (MGP_SECURITY §2.3).

    Note: ``permissions_required`` is intentionally omitted. LLM-bridge
    servers are configured at the kernel side with the ``core`` trust level,
    which grants Unrestricted network access via the isolation profile
    (MGP_ISOLATION_DESIGN §3.2). Self-declaring ``network.outbound`` would
    add the permission to ``CLOTO_YOLO_EXCEPTIONS`` (see MGP_SECURITY §3.3)
    and require operator approval on every startup, blocking the server
    even though the kernel already permits the network access. The trust
    level is likewise informational at the protocol layer (§2.3) — the
    kernel determines the effective trust level from ``mcp.toml`` — so we
    omit ``trust_level`` from the self-declaration as well.
    """
    from _vendored_mcp_common.mgp_utils import MgpCapabilities

    mgp = MgpCapabilities()
    mgp.set_server_id(f"cloto-mcp-{config.provider_id}")
    mgp.add_extension("streaming")
    return mgp.as_dict()


async def run_server(server: Server, config: "ProviderConfig | None" = None):
    """Run an MCP server over stdio with the MGP interceptor mounted.

    The interceptor (``common.mcp_stream_interceptor``) sits between the raw
    stdio read stream and the ``ServerSession``'s read stream. It pre-dispatches
    MGP Layer-3 requests (``mgp/stream/cancel``) and custom notifications
    (``notifications/mgp.stream.gap``) that the MCP SDK would otherwise reject
    during closed-union validation.

    Non-streaming servers and non-MGP clients see identical behavior: every
    message they care about is forwarded untouched.

    When ``config`` is provided, MGP Tier 3 capabilities are declared via
    ``experimental_capabilities`` (see :func:`_build_mgp_capabilities`). Callers
    that omit ``config`` fall back to a plain non-MGP initialize response.
    """
    import anyio

    from _vendored_mcp_common.mcp_stream_interceptor import mgp_message_interceptor

    experimental_caps = _build_mgp_capabilities(config) if config is not None else {}

    async with stdio_server() as (raw_read, write_stream):
        # A small buffer keeps producer (interceptor) and consumer
        # (ServerSession) decoupled without unbounded memory growth.
        inner_send, inner_recv = anyio.create_memory_object_stream(max_buffer_size=8)

        async with anyio.create_task_group() as tg:
            tg.start_soon(
                mgp_message_interceptor,
                raw_read,
                inner_send,
                write_stream,
                _active_streams,
            )
            try:
                init_options = server.create_initialization_options(
                    experimental_capabilities=experimental_caps,
                )
                await server.run(inner_recv, write_stream, init_options)
            finally:
                # When the server exits (EOF or error), ensure the interceptor
                # task also terminates by cancelling the task group.
                tg.cancel_scope.cancel()


# ============================================================
# Configuration Loader
# ============================================================


def load_llm_provider_config(
    prefix: str,
    display_name: str,
    default_model: str = "",
    supports_tools: bool = True,
    default_timeout: int = 120,
    default_reasoning_prefill: bool = False,
) -> ProviderConfig:
    """Load an LLM provider config from environment variables.

    Environment variables: {PREFIX}_PROVIDER, {PREFIX}_MODEL,
    {PREFIX}_API_URL, {PREFIX}_TIMEOUT_SECS, {PREFIX}_REASONING_PREFILL.

    MGP §8-10 Proxy-Only Architecture:
    When running under OS-level isolation (NetworkScope::ProxyOnly), the kernel
    injects CLOTO_LLM_PROXY / HTTP_PROXY / HTTPS_PROXY env vars pointing to
    the kernel's LLM proxy. All outbound HTTP is expected to route through this
    proxy. Direct API keys are stripped from the child environment.
    """
    import logging
    import os

    logger = logging.getLogger(__name__)

    # CLOTO_LLM_PROXY is injected by the kernel when NetworkScope::ProxyOnly.
    # Use it as the default API base if present.
    proxy_base = os.environ.get("CLOTO_LLM_PROXY")
    default_api_url = (
        f"{proxy_base}/v1/chat/completions"
        if proxy_base
        else "http://127.0.0.1:8082/v1/chat/completions"
    )

    api_url = os.environ.get(f"{prefix}_API_URL", default_api_url)

    # Warn if proxy-only mode is active but api_url points outside localhost.
    if proxy_base and "127.0.0.1" not in api_url and "localhost" not in api_url:
        logger.warning(
            "CLOTO_LLM_PROXY is set (%s) but %s_API_URL (%s) does not point to "
            "localhost. In proxy-only isolation, direct external API calls may be "
            "blocked. Consider removing the custom API URL override.",
            proxy_base,
            prefix,
            api_url,
        )

    model_id = os.environ.get(f"{prefix}_MODEL", default_model)

    # Resolve the </think> prefill flag with three-level precedence:
    #   1. Explicit `{PREFIX}_REASONING_PREFILL` env var (user override)
    #   2. Heuristic auto-detection from the configured model_id
    #   3. `default_reasoning_prefill` fall-back
    # Auto-detection only fires when the model name is recognisable — mixed /
    # unknown names fall through to the server-supplied default so we don't
    # silently flip behaviour under users with custom deployments.
    prefill_env = os.environ.get(f"{prefix}_REASONING_PREFILL", "").strip().lower()
    if prefill_env in ("true", "1", "yes", "on"):
        reasoning_prefill = True
    elif prefill_env in ("false", "0", "no", "off"):
        reasoning_prefill = False
    else:
        detected = _model_suggests_reasoning(model_id)
        if detected is None:
            reasoning_prefill = default_reasoning_prefill
        else:
            reasoning_prefill = detected
            logger.info(
                "%s: auto-detected %s model from model_id=%r; "
                "reasoning_prefill=%s. Override with %s_REASONING_PREFILL=true/false.",
                display_name,
                "reasoning" if detected else "non-reasoning",
                model_id,
                reasoning_prefill,
                prefix,
            )

    streaming_env = os.environ.get(f"{prefix}_STREAMING", "").strip().lower()
    supports_streaming = streaming_env in ("true", "1", "yes", "on")

    return ProviderConfig(
        provider_id=os.environ.get(f"{prefix}_PROVIDER", prefix.lower()),
        model_id=model_id,
        api_url=api_url,
        request_timeout=int(os.environ.get(f"{prefix}_TIMEOUT_SECS", str(default_timeout))),
        supports_tools=supports_tools,
        display_name=display_name,
        reasoning_think_prefill=reasoning_prefill,
        supports_streaming=supports_streaming,
    )


# ============================================================
# Server Factory
# ============================================================


def create_llm_mcp_server(config: ProviderConfig) -> Server:
    """Create a fully configured LLM MCP server with think/think_with_tools tools.

    Eliminates boilerplate duplication across provider servers.
    """
    from mcp.types import Tool

    server = Server(f"cloto-mcp-{config.provider_id}")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        tools = [
            Tool(
                name="think",
                description=(f"Generate a text response using {config.display_name} LLM."),
                inputSchema=THINK_INPUT_SCHEMA,
            ),
        ]

        if model_supports_tools(config):
            tools.append(
                Tool(
                    name="think_with_tools",
                    description=(
                        "Generate a response that may include tool calls. "
                        "Returns either final text or a list of tool calls to execute."
                    ),
                    inputSchema=THINK_WITH_TOOLS_INPUT_SCHEMA,
                )
            )

        return tools

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        if name == "think":
            return await handle_think(config, arguments)
        elif name == "think_with_tools":
            if config.supports_streaming and _mgp_stream_requested.get():
                try:
                    ctx = server.request_context
                except LookupError:
                    ctx = None
                if ctx is not None:
                    return await handle_think_with_tools_streaming(config, arguments, ctx)
            return await handle_think_with_tools(config, arguments)
        else:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"error": f"Unknown tool: {name}"}),
                )
            ]

    # Wrap the registered CallToolRequest handler to extract the `_mgp.stream`
    # opt-in flag from the raw `params` (which is NOT exposed to the decorator's
    # (name, arguments) signature). Stash the flag in a ContextVar so the
    # decorated handler above can read it.
    _install_mgp_stream_wrapper(server)

    return server


def _extract_stream_flag(req) -> bool:
    """Return True if the tools/call request opts into MGP §12 streaming.

    Opt-in signal: ``params._mgp.stream == True`` on the incoming tools/call.
    ``CallToolRequestParams`` uses ``extra="allow"`` so the ``_mgp`` field is
    captured in ``model_extra`` without a schema change.
    """
    params = getattr(req, "params", None)
    extra = getattr(params, "model_extra", None) if params is not None else None
    if not isinstance(extra, dict):
        return False
    mgp = extra.get("_mgp")
    if not isinstance(mgp, dict):
        return False
    return bool(mgp.get("stream"))


def _install_mgp_stream_wrapper(server: Server) -> None:
    """Wrap ``server.request_handlers[CallToolRequest]`` to set a ContextVar.

    On stdio transport ``RequestContext.request`` is ``None`` (the MCP SDK
    populates it only with HTTP transport metadata), so the decorated
    ``@server.call_tool()`` handler cannot reach the raw ``_mgp`` field via
    ``server.request_context``. The CallToolRequest handler, however, receives
    the typed request object directly — so we hook there and stash the flag
    in a ContextVar for the decorated handler to consume.
    """
    original = server.request_handlers.get(CallToolRequest)
    if original is None:
        return  # @server.call_tool() was not used; nothing to wrap

    async def _wrapped(req):
        token = _mgp_stream_requested.set(_extract_stream_flag(req))
        try:
            return await original(req)
        finally:
            _mgp_stream_requested.reset(token)

    server.request_handlers[CallToolRequest] = _wrapped
