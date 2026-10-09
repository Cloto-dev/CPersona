"""Counting a response in ``cl100k_base`` tokens, offline (2.6.8).

``reconstruct``'s modes cap the tokens of the whole JSON the tool returns, so the
count has to be the same on every machine and has to work without a network.
tiktoken's own loader fetches the vocabulary on first use and caches it in a
temporary directory, also when it is handed a local path. The vocabulary ships in
``cpersona/vocab/`` instead (its license and source are in ``vocab/LICENSE``), is
checked against the digest tiktoken itself expects, and is read once per process.

The split pattern and the special tokens are tiktoken's definition of
``cl100k_base``; the pattern is the same string in every tiktoken release from
0.8.0 (the dependency's floor) to 0.14.0.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
from pathlib import Path

ENCODING = "cl100k_base"
VOCAB_PATH = Path(__file__).with_name("vocab") / "cl100k_base.tiktoken"
VOCAB_SHA256 = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"
_PATTERN = (
    r"""'(?i:[sdmt]|ll|ve|re)|[^\r\n\p{L}\p{N}]?+\p{L}++|\p{N}{1,3}+| ?[^\s\p{L}\p{N}]++[\r\n]*+"""
    r"""|\s++$|\s*[\r\n]|\s+(?!\S)|\s"""
)
_SPECIAL = {
    "<|endoftext|>": 100257,
    "<|fim_prefix|>": 100258,
    "<|fim_middle|>": 100259,
    "<|fim_suffix|>": 100260,
    "<|endofprompt|>": 100276,
}

_lock = threading.Lock()
_encoding = None


class VocabularyError(RuntimeError):
    """The shipped vocabulary is missing or is not the one the count is defined by."""


def _load():
    import tiktoken

    try:
        data = VOCAB_PATH.read_bytes()
    except OSError as exc:
        raise VocabularyError(f"{ENCODING} vocabulary not found at {VOCAB_PATH}") from exc
    if hashlib.sha256(data).hexdigest() != VOCAB_SHA256:
        raise VocabularyError(f"{ENCODING} vocabulary at {VOCAB_PATH} does not match its digest")
    ranks = {base64.b64decode(token): int(rank) for token, rank in (line.split() for line in data.splitlines() if line)}
    return tiktoken.Encoding(name=ENCODING, pat_str=_PATTERN, mergeable_ranks=ranks, special_tokens=_SPECIAL)


def encoding():
    """The ``cl100k_base`` encoding, built from the shipped vocabulary on first use."""
    global _encoding
    if _encoding is None:
        with _lock:
            if _encoding is None:
                _encoding = _load()
    return _encoding


def count(text: str) -> int:
    """Tokens of ``text``. A special token's spelling counts as ordinary text."""
    return len(encoding().encode(text, disallowed_special=()))


def serialized(value) -> str:
    """``value`` as the MCP layer sends a tool result (``_vendored_mcp_common/mcp_utils.py``)."""
    return json.dumps(value, ensure_ascii=False)


def count_json(value) -> int:
    """Tokens of ``value`` serialized as the MCP layer sends it."""
    return count(serialized(value))
