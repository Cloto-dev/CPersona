"""Subscription-authenticated, isolated Codex reader/judge measurements."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from benchmarks.reconstruct_count_qa import (
    JUDGE_SCHEMA,
    JUDGE_SYSTEM,
    READER_SCHEMA,
    READER_SYSTEM,
    canonical,
    packet_hash,
    validate_output,
)

MODEL = "gpt-5.6-luna"
EFFORT = "high"

#: The executable to run. A wrapper earlier on PATH can add flags of its own (one
#: that bypasses hook trust, say), and its warnings then arrive as items the
#: isolation check rightly refuses, so a measurement can point this at the real
#: binary. Recorded in each call's invocation.json.
CODEX_BIN = os.environ.get("CODEX_BIN", "codex")


def metrics(events, wall_ms):
    """Codex cached input is a subset of input, not an additional input term."""
    if any(e.get("type") in ("error", "turn.failed") for e in events):
        raise ValueError("Codex reported a failed turn")
    turns = [e for e in events if e.get("type") == "turn.completed"]
    if len(turns) != 1:
        raise ValueError("Expected exactly one completed turn")
    for event in events:
        if (event.get("type", "").startswith("item.")
                and event["item"].get("type") not in ("agent_message", "reasoning")):
            raise ValueError("Unexpected tool or external item in isolated measurement")
    usage = turns[0]["usage"]
    result = {}
    for key in ("input_tokens", "cached_input_tokens", "output_tokens"):
        value = usage[key]
        if type(value) is not int or value < 0:
            raise ValueError(f"Invalid usage: {key}")
        result[key] = value
    if result["cached_input_tokens"] > result["input_tokens"]:
        raise ValueError("Cached input exceeds total input")
    result["total_input_tokens"] = result["input_tokens"]
    result["uncached_input_tokens"] = result["input_tokens"] - result["cached_input_tokens"]
    if "reasoning_output_tokens" in usage:
        value = usage["reasoning_output_tokens"]
        if type(value) is not int or not 0 <= value <= result["output_tokens"]:
            raise ValueError("Invalid reasoning usage")
        result["reasoning_output_tokens"] = value
    result["wall_ms"] = wall_ms
    return result


def run_isolated(prompt, system, schema, output_dir, *, model, effort, timeout=180):
    """One isolated Codex call: no user config, hooks, memories, tools or API keys.

    Returns (structured answer, usage metrics). No wrapper retries; the call's
    request, events and answer stay in ``output_dir`` as evidence whether it
    succeeded or failed. ``output_dir`` must not exist yet, so a result is never
    silently overwritten.
    """
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "schema.json").write_text(canonical(schema))
    (output_dir / "instructions.txt").write_text(system)
    (output_dir / "request.json").write_text(prompt)
    command = [CODEX_BIN, "exec", "--model", model,
               "-c", f'model_reasoning_effort="{effort}"',
               "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
               "--sandbox", "read-only", "--json", "--color", "never",
               "--output-schema", str(output_dir / "schema.json"),
               "--output-last-message", str(output_dir / "answer.json"),
               "-c", f'model_instructions_file={json.dumps(str(output_dir / "instructions.txt"))}',
               "-c", "project_doc_max_bytes=0", "-c", 'web_search="disabled"',
               "-c", "suppress_unstable_features_warning=true",
               "-c", 'forced_login_method="chatgpt"',
               "-c", 'approval_policy="never"']
    for feature in ("shell_tool", "multi_agent", "apps", "hooks", "memories",
                    "skill_search", "computer_use", "browser_use", "browser_use_external"):
        command += ["--disable", feature]
    command += ["--enable", "skip_host_skill_discovery", "-"]
    (output_dir / "invocation.json").write_text(canonical({
        "argv": command, "model": model, "effort": effort,
        "model_identity_evidence": "requested CLI model; JSON usage has no model attestation",
    }))
    env = dict(os.environ)
    for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_BASE_URL"):
        env.pop(name, None)
    started = time.perf_counter()
    with (
        tempfile.TemporaryDirectory(prefix="count-codex-") as scratch,
        (output_dir / "events.jsonl").open("w") as stdout,
        (output_dir / "stderr.txt").open("w") as stderr,
    ):
        try:
            proc = subprocess.run(command, input=prompt, text=True, stdout=stdout,
                                  stderr=stderr, cwd=scratch, env=env, timeout=timeout,
                                  check=False)
        except subprocess.TimeoutExpired:
            (output_dir / "execution.json").write_text(canonical({
                "wall_ms": (time.perf_counter() - started) * 1000, "timeout": True,
            }))
            raise
    wall_ms = (time.perf_counter() - started) * 1000
    (output_dir / "execution.json").write_text(canonical({"wall_ms": wall_ms, "exit_code": proc.returncode}))
    if proc.returncode:
        raise RuntimeError(f"Codex exited {proc.returncode}; inspect retained call artifact")
    events = [json.loads(line) for line in (output_dir / "events.jsonl").read_text().splitlines() if line.strip()]
    measured = metrics(events, wall_ms)
    return json.loads((output_dir / "answer.json").read_text()), measured


def invoke(packet, kind, output_dir, *, timeout=180):
    """No wrapper retries; preserve successful or failed execution evidence."""
    if kind not in ("reader", "judge"):
        raise ValueError("Unknown call kind")
    schema = READER_SCHEMA if kind == "reader" else JUDGE_SCHEMA
    system = READER_SYSTEM if kind == "reader" else JUDGE_SYSTEM
    output_dir = Path(output_dir).resolve()
    raw, measured = run_isolated(canonical(packet), system, schema, output_dir,
                                 model=MODEL, effort=EFFORT, timeout=timeout)
    value = validate_output(raw, kind)
    result = {"kind": kind, "model": MODEL, "effort": EFFORT,
              "model_identity_evidence": "requested CLI model; not independently attested",
              "packet_sha256": packet_hash(packet), "output": value, "metrics": measured}
    (output_dir / "result.json").write_text(canonical(result))
    return result
