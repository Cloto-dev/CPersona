"""Lossless public-history adapter, without answer annotations in reader text."""
from __future__ import annotations

import json


def iter_json_array(path, chunk_size=4 * 1024 * 1024):
    """Stream a large JSON array without loading every conversation into RAM."""
    decoder = json.JSONDecoder()
    with open(path, encoding="utf-8") as stream:
        buffer = ""
        eof = False

        def fill():
            nonlocal buffer, eof
            chunk = stream.read(chunk_size)
            buffer += chunk
            eof = not chunk

        fill()
        buffer = buffer.lstrip()
        if not buffer.startswith("["):
            raise ValueError("Expected a JSON array")
        buffer = buffer[1:]
        first = True
        while True:
            buffer = buffer.lstrip()
            while not buffer and not eof:
                fill()
                buffer = buffer.lstrip()
            if buffer.startswith("]"):
                if buffer[1:].strip() or stream.read().strip():
                    raise ValueError("Trailing data after array")
                return
            if not first:
                if not buffer.startswith(","):
                    raise ValueError("Missing array separator")
                buffer = buffer[1:].lstrip()
            while True:
                try:
                    value, end = decoder.raw_decode(buffer)
                    break
                except json.JSONDecodeError:
                    if eof:
                        raise ValueError("Incomplete or invalid JSON array") from None
                    fill()
                    buffer = buffer.lstrip()
            yield value
            buffer = buffer[end:]
            first = False


def normalized(text):
    return " ".join(text.split())


def restore_session(doc_id, indexed_text, record):
    """Validate LMEB's one-based session index and strip all oracle labels."""
    index = int(doc_id.rsplit("_session_", 1)[1]) - 1
    if index < 0:
        raise ValueError("Session indexes are one-based")
    turns = record["haystack_sessions"][index]
    user_text = " ".join(t["content"] for t in turns if t["role"] == "user")
    if normalized(user_text) != normalized(indexed_text):
        raise ValueError(f"Source mismatch for {doc_id}")
    return {
        "session_date": record["haystack_dates"][index],
        "turns": [{"role": t["role"], "content": t["content"]} for t in turns],
    }
