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
    MAX_PROFILE_LENGTH,
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


# bug-217 (bug-121's class, one step up in length): the bidirectional prefix test
# below exists ONLY to absorb the truncation asymmetry between a preview/context
# echo and the full stored row, which cannot arise for a short entry. Without a
# floor, a 2-character acknowledgement arriving through recall_with_context's
# external_context ("ok", "yes", "はい") prefix-matched every memory that starts
# with it and removed them from the candidate set before ranking — silently, since
# the exclusion happens inside the retrievers and nothing in the response reports
# it. Entries below this length must therefore match exactly.
EXCLUDE_PREFIX_MIN_CHARS = 32


def _content_excluded(content: str, exclude_set: set[str]) -> bool:
    """Check if content matches any excluded string (starts-with, normalized).

    Handles truncation asymmetry: conversation_context entries may be truncated
    to 500 chars while a stored memory runs to MAX_CONTENT_LENGTH. The
    starts_with check in both directions accounts for this — but only for entries
    of at least ``EXCLUDE_PREFIX_MIN_CHARS`` (bug-217); a shorter entry has to be
    an exact (case-insensitive) match to exclude anything.
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
        if len(excl) < EXCLUDE_PREFIX_MIN_CHARS:
            if normalized == excl:  # bug-217: exact match only below the floor
                return True
            continue
        if normalized.startswith(excl) or excl.startswith(normalized):
            return True
    return False


def _sanitize_with_cap(content: str, cap: int) -> tuple[str, bool]:
    """Strip the annotation and whitespace, then apply ``cap``.

    bug-175: callers derived the ``truncated`` flag from ``len(raw) > cap``, but
    the raw string still carries the ``[Memory from ...]`` annotation this
    function strips. Content whose annotation alone pushed it over the cap was
    therefore reported as truncated while the stored text had not been cut —
    and update_memory's description promises the flag marks a real cap hit. The
    decision belongs where the cut happens, so the flag is returned with it.
    """
    content = _MEMORY_ANNOTATION_PATTERN.sub("", content)
    content = content.strip()
    truncated = len(content) > cap
    if truncated:
        content = content[:cap]
    return content, truncated


def sanitize_content_with_flag(content: str) -> tuple[str, bool]:
    """Sanitize a memory/episode write, and report whether the cap cut anything."""
    return _sanitize_with_cap(content, MAX_CONTENT_LENGTH)


def sanitize_profile_with_flag(profile: str) -> tuple[str, bool]:
    """Sanitize a profile write against the profile's own cap (an earlier decision).

    Same seam as ``sanitize_content_with_flag`` — the two paths differ only in
    which ceiling applies. The profile row is injected into every recall response
    and is never preview-trimmed (bug-117), so its bound cannot be inherited from
    a constant that a later line intends to relax; see config.MAX_PROFILE_LENGTH.
    """
    return _sanitize_with_cap(profile, MAX_PROFILE_LENGTH)


def _sanitize_content(content: str) -> str:
    """Sanitize content before storing in memory.

    Removes [Memory from ...] annotations, trims whitespace, and enforces
    length limit. Discord-specific sanitization (mention stripping) is
    handled by the Discord bridge before content reaches CPersona.
    """
    return sanitize_content_with_flag(content)[0]


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


def episode_timestamp(start_time: str | None, created_at: str | None) -> str:
    """The time an episode is scored and shown by: its own ``start_time``, else the time
    the row was recorded.

    bug-213: ``episodes.start_time`` is nullable and 338 of 505 episodes on this
    project's deployment carry NULL there, while ``episodes.created_at`` is
    ``TEXT NOT NULL DEFAULT (datetime('now'))`` and every row has one. Every read path
    passed ``start_time or ""`` straight through, so those rows reached
    ``_compute_confidence`` with no timestamp at all and took bug-207's imputed age —
    an age SYNTHESISED from the corpus span while the same row carried a usable time.

    ``created_at`` is a good enough stand-in and is measured, not assumed: across the 167
    episodes holding both values it trails ``start_time`` by under 24h in 93.4% of cases
    (mean +8.8h, max +209h), against an imputation whose median error on the same
    deployment was 950h. The imputation's error is also one-directional — a flat corpus
    midpoint (1713h there) ages every recent episode at once, which is what made
    session-start grounding lose the episodes it exists to surface.

    It remains an approximation, and the only one the row itself can supply: ``created_at``
    is when the episode was RECORDED, not when what it describes happened. It is read, never
    written back — the data stays as it is (the same rule bug-207 set). When neither value
    parses, ``_compute_confidence`` still imputes and still reports ``age_unknown``:
    bug-207's branch is narrowed to the rows that genuinely have no time, not removed.

    One implementation for one invariant: every path that turns an episode row into a
    recall result calls this, so the age a row is ranked by cannot depend on which
    retriever found it.
    """
    return start_time or created_at or ""


# bug-184: fingerprint of the scoring function below. The calibrated recall gate is an
# operating point measured ON a specific score distribution, so it may only be restored
# for the scoring function it was calibrated on — restoring it across a scoring change
# gates a different quantity than was measured and silently over-filters. The calibration
# sidecar stamps this string; ``ensure_calibrated_on_startup`` treats a mismatch (or an
# absent key — every pre-2.5.2b2 sidecar) exactly like an embedding-dimension change and
# forces recalibration.
#
# BUMP THIS whenever a change shifts the confidence/score distribution: the branch
# structure of ``_compute_confidence``, its constants (COSINE_FLOOR/CEIL, the decay
# rates), the episode penalty, or which rows reach scoring carrying a cosine at all. The
# 2.5.2b2 cosine backfill (bug-155) is the first such change — it moved FTS-only rows off
# the cosine-less ``sqrt(time_decay)`` branch, lowering their scores by construction. The
# 2.5.4a3 unknown-age imputation (bug-207) is the second: a row whose timestamp does not
# parse no longer takes the ``time_decay`` of 1.0 reserved for a row written this instant,
# so every undated row scores lower than the sidecar was calibrated against — two thirds
# of the episodes on this project's deployment. The 2.5.5a1 created_at fallback (bug-213)
# is the third, and it moves the same two thirds again: an episode with no start_time is
# now scored on the time it was recorded instead of on the imputed corpus midpoint, which
# on this deployment raises every recent episode and lowers the older ones — the imputed
# age was a single flat value for rows whose real ages span months.
# The 2.5.5a3 episode-penalty exemption (bug-257) is the fourth: episode rows no longer
# enter the boundary penalty at all, so every episode older than the boundary scores up
# to 2x higher (the EPISODE_DECAY_FLOOR was 0.5) than the sidecar was calibrated against.
SCORING_VERSION = "255a3-episode-penalty-exempt"


def _compute_confidence(
    raw_cosine: float | None,
    timestamp_str: str,
    *,
    resolved: bool = False,
    deep: bool = False,
    time_range_hours: float = 0.0,
    newest_age_hours: float | None = None,
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

    parsed = _parse_timestamp_utc(timestamp_str)
    # bug-207: an unparseable or empty timestamp used to leave age_hours at 0.0. That is
    # not "unknown" — it is the exact age of a row written this instant, so the rows whose
    # age nobody knows took the full time_decay of 1.0 that no dated row can reach, and
    # outranked every row whose age IS known. Measured on this project's deployment: all
    # 2,221 memories carry a timestamp, while 333 of 500 episodes have no start_time, and
    # _search_episodes_fts passes that straight through as "". Two thirds of the episodes
    # were therefore ranked above every dated memory on the time axis.
    #
    # bug-213 narrowed which rows reach this branch: the episode read paths now fall back
    # to episodes.created_at (utils.episode_timestamp), so a row that HAS a recorded time
    # is scored on it rather than on the midpoint below. What remains here is the case the
    # fallback cannot serve — a row with no usable time at all — and the imputation is
    # still the answer for it.
    #
    # An unknown age is now placed in the middle of the corpus's own AGE range: neutral
    # rather than newest, deterministic, derived from values the caller already computed,
    # and it never writes a fabricated timestamp back to the row. The caller is told, via
    # age_unknown, that the age it is reading was imputed.
    #
    # The anchor matters. time_range_hours is the corpus's internal WIDTH (MAX - MIN of
    # the timestamps), while every dated row's age is measured from now, so half the width
    # is only a midpoint when the newest row is roughly now. On a scope whose newest row
    # is itself old, half the width is YOUNGER than every dated row and the defect returns
    # intact: with oldest 51d / newest 21d (width 720h) an imputed 360h outscores the 504h
    # newest. Anchoring on the newest row's age fixes both ends —
    # newest_age + width/2 == ((now - newest) + (now - oldest)) / 2 — and it collapses to
    # the newest row's own age when the scope holds a single timestamp (width 0), which
    # ties rather than wins.
    effective_range = max(MIN_TIME_RANGE_HOURS, time_range_hours)
    age_unknown = parsed is None
    if parsed:
        age_hours = max(0.0, (now - parsed).total_seconds() / 3600)
    elif newest_age_hours is not None:
        age_hours = max(0.0, newest_age_hours) + max(0.0, time_range_hours) / 2.0
    else:
        # No anchor: the caller scored rows without computing a corpus span at all (the
        # gate-calibration path, or a scope whose memories table is empty). Half the
        # floored width is the pre-anchor behaviour, and it still assumes a live corpus —
        # the one case this fix cannot reach, kept explicit rather than silently correct.
        age_hours = effective_range / 2.0

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
    if age_unknown:
        # bug-207: age_hours above is imputed, not observed. Saying so is the difference
        # between a caller reading a measurement and a caller reading a default.
        confidence["age_unknown"] = True
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


def error_response(message: str, **extra) -> dict:
    """The one shape a tool answers with when the call could not be carried out.

    2.5.2b1 (audit C23): the surface used to answer failures two ways — five
    sites returned ``{"ok": False, "error": ...}`` and twenty-nine returned a
    bare ``{"error": ...}`` with no ``ok`` key at all. A caller checking ``ok``
    therefore read ``None`` for most failures, which is the same defect b1-1
    fixed inside ``store``: the obvious field did not answer the obvious
    question. Every tool-level failure now carries both — ``ok`` for the
    caller that branches on a boolean, ``error`` for the one that wants the
    reason.

    ``extra`` carries the per-tool fields a specific failure still owes its
    caller (``episode_id=None``, an ``errors`` list, a rollback flag).
    """
    return {"ok": False, "error": message, **extra}


def _try_parse_json(s: str) -> dict:
    """Try to parse a string as JSON, return empty dict on failure."""
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return {}


# Canonical source contract (2.5.2, an earlier decision item 1/1b).
# The wire shape is {"type": <"User"|"Agent"|"System">, "id": str, "name": str}.
# ~75 % of production memories carried legacy variants that survived a write path
# with zero validation; the enum here is intentionally the same three values as
# ClotoCore's serde tag ("Assistant" is written to "Agent"), so the marketplace
# and Rust callers agree on the discriminator.
#
# 2.5.2b1 (an earlier decision item b1-2): this tuple is the SINGLE SOURCE for all three
# surfaces that state the contract — the published JSON Schema (server.py store
# tool), the write seam (``normalize_source`` below), and the health check that
# audits legacy rows (``checks.check_invalid_source_type``, via
# ``canonical_source_types_sql``). Until b1 the check carried its own SQL
# literal, so the enum lived in two places and could drift silently; the schema
# had no enum at all, which is why callers could only learn the contract by
# reading checks.py. Add a type here and every surface follows.
CANONICAL_SOURCE_TYPES = ("User", "Agent", "System")

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


def canonical_source_types_sql() -> str:
    """Render the canonical enum as a SQL tuple literal, e.g. ``('User', 'Agent', 'System')``.

    Used by the source-type health check, which cannot bind the enum as
    parameters without rewriting its ``NOT IN`` predicates per call site. The
    values are compile-time constants of this module, never caller input; the
    alphabetic guard keeps it that way — a future non-identifier value fails
    loudly here instead of producing a quoted string that SQLite would happily
    accept as data.
    """
    for type_name in CANONICAL_SOURCE_TYPES:
        if not type_name.isalpha():
            raise ValueError(
                f"canonical source type {type_name!r} is not alphabetic; "
                "inline SQL rendering would need quoting/escaping rules"
            )
    return "(" + ", ".join(f"'{t}'" for t in CANONICAL_SOURCE_TYPES) + ")"


def source_type_alias_summary() -> str:
    """Describe the legacy spellings folded into each canonical type.

    Derived from ``_TYPE_ALIASES`` so the published tool description cannot
    drift from the mapping the write seam actually applies (the C26 doc-drift
    class: prose that states a rule the code no longer implements). Case
    variants of the canonical names themselves are omitted — they are implied
    by "matched case-insensitively".
    """
    folded: dict[str, list[str]] = {}
    for alias, canon in _TYPE_ALIASES.items():
        if alias == canon.lower():
            continue
        folded.setdefault(canon, []).append(alias)
    parts = [
        " / ".join(f"'{a}'" for a in sorted(aliases)) + f" are normalized to '{canon}'"
        if len(aliases) > 1
        else f"'{aliases[0]}' is normalized to '{canon}'"
        for canon, aliases in sorted(folded.items())
    ]
    return "; ".join(parts) + " (type words are matched case-insensitively)"


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
    if isinstance(raw_type, str) and raw_type in CANONICAL_SOURCE_TYPES:
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
        if key in CANONICAL_SOURCE_TYPES:
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
