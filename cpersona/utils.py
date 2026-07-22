"""Stateless helper functions for CPersona.

No global mutable state; all functions are pure or depend only on imported
config constants.
"""

import hashlib
import json
import math
import re
from datetime import datetime, timezone

from cpersona.config import (
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
    # bug-121: '' starts-with-matches every exclude entry (str.startswith('') is
    # always True in the reversed check), so any exclude filter silently dropped
    # every legitimately-empty-content memory. Empty content can never be a
    # dedup hit — nothing meaningful to deduplicate against.
    if not normalized:
        return False
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
        if dt.tzinfo is None:
            # bug-114 class: naive DB timestamps are UTC (SQLite datetime('now')),
            # not system-local — anchor before converting to local for display.
            dt = dt.replace(tzinfo=timezone.utc)
        local_dt = dt.astimezone()
        tz_name = local_dt.strftime("%Z")
        return local_dt.strftime(f"%Y-%m-%d %H:%M {tz_name}")
    except (ValueError, OSError):
        return None


def _parse_timestamp_utc(ts_raw: str) -> datetime | None:
    """Parse an ISO-8601 timestamp string into a UTC datetime.

    Naive timestamps are UTC by invariant (bug-114): every DB-written naive
    value comes from SQLite ``datetime('now')``, which emits UTC without an
    offset. ``astimezone()`` on a naive datetime would instead assume
    system-local time and shift the value by the host's UTC offset (on a JST
    host, 9 hours) — silently corrupting recall-boost decay and the episode
    boundary factor on every non-UTC deployment.
    """
    if not ts_raw:
        return None
    try:
        dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
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


# Canonical source contract (2.5.2, Task #282 item 1/1b).
# The wire shape is {"type": <"User"|"Agent"|"System">, "id": str, "name": str}.
# ~75 % of production memories carried legacy variants that survived a write path
# with zero validation; the enum here is intentionally the same three values as
# ClotoCore's serde tag ("Assistant" is written to "Agent"), so the marketplace
# and Rust callers agree on the discriminator.
_CANONICAL_TYPES = ("User", "Agent", "System")

# Case-insensitive type-word aliases. Anything not listed here is left untouched
# (health-check surfaces the invalid row for human review — the contract is
# "normalize what we understand, never fabricate a type we don't").
_TYPE_ALIASES = {
    "user": "User",
    "agent": "Agent",
    "system": "System",
    "assistant": "Agent",
    "ai": "Agent",
    "session": "System",
}

# Bare-string aliases (the whole source is a JSON string, not a dict). We are
# strict here because a bare "claude-code" or agent-id string is legitimately
# ambiguous — those rows are left for the (1a) human-reviewed migration.
_BARE_STRING_ALIASES = {
    "user": "User",
    "assistant": "Agent",
    "ai": "Agent",
}


def normalize_source(source):
    """Fold a legacy ``source`` value into the canonical contract.

    Returns ``(normalized, mapped)``:
    - ``mapped=True`` — the input matched a known legacy shape and was
      rewritten to ``{"type": <User|Agent|System>, "id": str, "name": str}``
      (id / name are preserved when present; the discriminator is authoritative).
    - ``mapped=False`` — the input is either already canonical OR uses a shape
      we deliberately do not touch (unknown dict, unknown vocabulary, unknown
      bare string, {}, None, etc.). The caller MUST persist the original
      value verbatim — silent fabrication of a discriminator would falsify
      attribution and defeat the anonymous_source detector downstream.

    Recognised legacy shapes (write path + check_invalid_source_type fixer
    share this mapping, so behaviour is symmetric):

    1. Canonical dict — untouched.
    2. Case-insensitive type vocabulary in ``$.type`` — rewritten to the
       canonical spelling; sibling ``id`` / ``name`` are preserved when present.
       ``assistant`` / ``ai`` fold to ``Agent`` (the enum stays 3-valued),
       ``session`` folds to ``System``. Unknown vocabulary (e.g. ``migration``)
       is left untouched.
    3. Rust serde externally-tagged dict from ClotoCore (single key ∈ enum):
       ``{"User": "u1"}`` → ``{"type":"User","id":"u1","name":"u1"}``,
       ``{"System": "ep"}`` → ``{"type":"System","id":"ep","name":""}``,
       ``{"Agent": {"id":"a","name":"A"}}`` → ``{"type":"Agent","id":"a","name":"A"}``.
    4. Bare string — ``"user"`` / ``"assistant"`` / ``"ai"`` (case-insensitive)
       fold to the corresponding canonical dict with empty id / name. Other
       bare strings (``"claude-code"``, arbitrary agent ids) stay untouched
       for the human-reviewed migration path.
    """
    # (5) Unknown / null / non-dict-non-str — leave the caller's value alone.
    if source is None:
        return source, False

    # (4) Bare string source.
    if isinstance(source, str):
        canon = _BARE_STRING_ALIASES.get(source.strip().lower())
        if canon is None:
            return source, False
        return {"type": canon, "id": "", "name": ""}, True

    if not isinstance(source, dict):
        return source, False

    # (1) Already canonical — the fast path used by every 2.5.x producer.
    raw_type = source.get("type")
    if isinstance(raw_type, str) and raw_type in _CANONICAL_TYPES:
        return source, False

    # (2) Case-insensitive type-word variant — preserve id / name, rewrite type.
    if isinstance(raw_type, str):
        canon = _TYPE_ALIASES.get(raw_type.strip().lower())
        if canon is None:
            # Unknown vocabulary ("migration", "bot", ...) — leave for human review.
            return source, False
        new_source = dict(source)
        new_source["type"] = canon
        return new_source, True

    # (3) Rust serde externally-tagged dict: exactly one key ∈ enum.
    # $.type absent (or non-string) AND len == 1 AND key ∈ enum is the discriminator.
    if raw_type is None and len(source) == 1:
        (key, value), = source.items()
        if key in _CANONICAL_TYPES:
            if isinstance(value, dict):
                # Inner dict may carry id / name and free-form extras.
                new_source = dict(value)
                new_source["type"] = key
                new_source.setdefault("id", "")
                new_source.setdefault("name", "")
                return new_source, True
            if isinstance(value, str):
                # String inner value: User/Agent → id + name mirror (preserves
                # display when name was implicit); System → id only (System's
                # inner has always been a bare label like "profile"/"episode").
                if key == "System":
                    return {"type": key, "id": value, "name": ""}, True
                return {"type": key, "id": value, "name": value}, True
            # Other inner types (list / None / int) — untouched.
            return source, False

    # (5) Everything else: empty {}, dicts with $.type absent that are not the
    # serde shape, dicts with multiple keys but no $.type — leave alone.
    return source, False
