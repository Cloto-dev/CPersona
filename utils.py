"""Stateless helper functions for CPersona.

No global mutable state; all functions are pure or depend only on imported
config constants.
"""

import hashlib
import json
import math
import re
from datetime import datetime, timezone

from config import (
    BOOST_DECAY_RATE,
    COSINE_CEIL,
    COSINE_FLOOR,
    DECAY_CEIL,
    DECAY_FLOOR,
    DECAY_RATE,
    MAX_CONTENT_LENGTH,
    MIN_TIME_RANGE_HOURS,
    RECALL_BOOST,
    RECENT_RECALL_PENALTY,
    RECENT_RECALL_WINDOW_MIN,
    REFERENCE_HOURS,
    RESOLVED_DECAY_FACTOR,
)


def _clamp_limit(limit: int, cap: int) -> int:
    """Clamp a user-supplied limit to [0, cap], preventing negative bypass."""
    return min(max(0, limit), cap)


_MENTION_PATTERN = re.compile(r"<@!?\d+>")
_MEMORY_ANNOTATION_PATTERN = re.compile(r"\[Memory from [^\]]+\]\s*")


def _content_excluded(content: str, exclude_set: set[str]) -> bool:
    """Check if content matches any excluded string (starts-with, normalized).

    Handles truncation asymmetry: conversation_context entries may be truncated
    to 500 chars while stored memories can be up to 2000 chars. The starts_with
    check in both directions accounts for this.
    """
    if not exclude_set:
        return False
    normalized = content.strip().lower()
    for excl in exclude_set:
        if normalized.startswith(excl) or excl.startswith(normalized):
            return True
    return False


def _sanitize_content(content: str) -> str:
    """Sanitize content before storing in memory.

    Removes [Memory from ...] annotations, trims whitespace, and enforces
    length limit. Discord-specific sanitization (mention stripping) is
    handled by the Discord bridge before content reaches CPersona.
    """
    content = _MEMORY_ANNOTATION_PATTERN.sub("", content)
    content = content.strip()
    if len(content) > MAX_CONTENT_LENGTH:
        content = content[:MAX_CONTENT_LENGTH]
    return content


def generate_mem_key(agent_id: str, message: dict) -> str:
    """Generate a unique key for a memory entry (2.1-compatible)."""
    ts = message.get("timestamp", datetime.now(timezone.utc).isoformat())
    content = message.get("content", "")
    hash_input = f"{agent_id}:{ts}:{content}"
    short_hash = hashlib.sha256(hash_input.encode()).hexdigest()[:8]
    return f"mem:{agent_id}:{ts}:{short_hash}"


def _format_memory_timestamp(ts_raw: str) -> str | None:
    """Convert an ISO-8601 timestamp to a human-readable local time annotation.

    Uses the OS-local timezone (no hardcoded TZ). Returns None on parse failure.
    """
    if not ts_raw:
        return None
    try:
        dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        local_dt = dt.astimezone()
        tz_name = local_dt.strftime("%Z")
        return local_dt.strftime(f"%Y-%m-%d %H:%M {tz_name}")
    except (ValueError, OSError):
        return None


def _parse_timestamp_utc(ts_raw: str) -> datetime | None:
    """Parse an ISO-8601 timestamp string into a UTC datetime."""
    if not ts_raw:
        return None
    try:
        dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc)
    except (ValueError, OSError):
        return None


def _compute_confidence(
    raw_cosine: float | None,
    timestamp_str: str,
    *,
    resolved: bool = False,
    deep: bool = False,
    time_range_hours: float = 0.0,
    recall_count: int = 0,
    last_recalled_at_str: str = "",
) -> dict:
    """Compute confidence metadata for a recall result (v2.3.2+).

    Returns a dict with 'age_hours', 'score', and optionally 'cosine', 'resolved'.
    Score = sqrt(norm_cos × time_decay) × completion_factor.
    When deep=True, time_decay and completion_factor are both 1.0.

    v2.4.4: Dynamic time decay + recall boost with gradual decay.
    Boost protection fades slowly (BOOST_DECAY_RATE) if memory is
    not recalled again, converging back to DECAY_FLOOR.
    """
    now = datetime.now(timezone.utc)
    age_hours = 0.0

    parsed = _parse_timestamp_utc(timestamp_str)
    if parsed:
        age_hours = max(0.0, (now - parsed).total_seconds() / 3600)

    raw_boost = math.log(1 + recall_count) * RECALL_BOOST
    if raw_boost > 0 and last_recalled_at_str:
        last_recalled = _parse_timestamp_utc(last_recalled_at_str)
        if last_recalled:
            hours_since = max(0.0, (now - last_recalled).total_seconds() / 3600)
            boost_decay = 1.0 / (1.0 + hours_since * BOOST_DECAY_RATE)
            raw_boost *= boost_decay
    effective_floor = min(DECAY_CEIL, DECAY_FLOOR + raw_boost)

    if deep:
        time_decay = 1.0
    elif time_range_hours > 0:
        effective_range = max(MIN_TIME_RANGE_HOURS, time_range_hours)
        effective_rate = DECAY_RATE / max(1.0, effective_range / REFERENCE_HOURS)
        time_decay = max(effective_floor, 1.0 / (1.0 + age_hours * effective_rate))
    else:
        time_decay = max(effective_floor, 1.0 / (1.0 + age_hours * DECAY_RATE))
    completion_factor = 1.0 if (deep or not resolved) else RESOLVED_DECAY_FACTOR

    recency_penalty = 1.0
    if last_recalled_at_str and not deep:
        lr = _parse_timestamp_utc(last_recalled_at_str)
        if lr:
            minutes_since = max(0.0, (now - lr).total_seconds() / 60)
            if minutes_since < RECENT_RECALL_WINDOW_MIN:
                recency_penalty = RECENT_RECALL_PENALTY

    confidence: dict = {"age_hours": round(age_hours, 1)}
    if resolved:
        confidence["resolved"] = True

    if raw_cosine is not None:
        denom = COSINE_CEIL - COSINE_FLOOR
        norm_cos = max(0.0, min(1.0, (raw_cosine - COSINE_FLOOR) / denom)) if denom > 0 else 0.0
        confidence["cosine"] = round(raw_cosine, 4)
        confidence["score"] = round(math.sqrt(norm_cos * time_decay) * completion_factor * recency_penalty, 4)
    else:
        confidence["score"] = round(math.sqrt(time_decay) * completion_factor * recency_penalty, 4)

    return confidence


def _try_parse_json(s: str) -> dict:
    """Try to parse a string as JSON, return empty dict on failure."""
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return {}
