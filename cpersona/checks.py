"""Deterministic health-check registry for CPersona (v2.4.37).

One registry, three surfaces: the MCP tools (``check_health`` / ``deep_check``),
the pytest fixture round-trips, and the ``python -m cpersona.checkup`` CLI all
call the same runner functions defined here, so a check's behaviour cannot
drift between surfaces.

Severity model
--------------
Every issue carries a ``severity``:

- ``critical`` — the read contract is broken *right now*: reads silently return
  wrong or missing data, or the database file itself is damaged. A CI gate
  should fail on any critical issue.
- ``warn`` — quality degradation or drift that does not yet falsify reads but
  will grow into a critical issue or degrades recall quality.
- ``info`` — an observation worth surfacing, not a defect. A rare value is not
  a wrong value (the bug-009 lesson: ``''`` is the *global* channel/project,
  never corruption).

``base_severity`` is the default; a runner may override per issue with a
deterministic escalation rule (numeric thresholds only — no model judgment):

- ``null_embedding`` / ``null_episode_embedding``: info when no embedding
  client is configured (NULL is then the expected steady state), warn when a
  client is configured, critical when a client is configured and more than
  half the rows are NULL (the embedding pipeline is effectively down).
- FTS count desync: warn for small drift, critical when more than 5% of rows
  are missing from the index.

``fix_capable`` is orthogonal to severity: ``sqlite_integrity`` is critical but
has no safe automatic repair, while cosmetic ``memory_annotation`` is info and
fully fixable. Fixes are always agent-scoped where the data is agent-scoped
and never touch ``locked`` rows (the bug-007 invariant).

The ``repairable`` contract (2.5.5)
----------------------------------------------
The escalation rules above are the only way a runner used to move its own
severity. This is the one systematic *de-escalation*: a finding whose repair
cannot touch a single row should not hold a gate down forever.

Every issue emitted by a ``fix_capable`` check MUST carry::

    issue["repairable"] = N      # rows/objects THIS run's fix would write
    issue["repairable"] = None   # this run could not determine it

Three properties make it load-bearing, and each has cost the project a bug:

1. **Computed independently of the ``fix`` argument.** A count that only ran
   when repairing would give the same data two different verdicts depending on
   how it was asked (``check_invalid_source_type`` carries the original note).
2. **Rows written, not rows considered.** Every fixer here is guarded by
   ``locked = 0``, so "rows matching the predicate" overcounts by the locked
   remainder, and a severity resting on that number answers "I changed 902
   rows" for a run that changed none (the ``timestamp_format_drift``
   ``normalized`` defect this contract was introduced with).
3. **``None`` is not zero.** ``check_invalid_source_type`` classifies at most
   ``INVALID_SOURCE_CLASSIFY_CAP`` rows; past the cap an empty repair set means
   "unknown", and an unknown is not evidence that nothing can be done. Only a
   determined zero de-escalates.

``run_health_checks`` owns the policy — the dispatcher cannot count rows, so
the counting stays in the runner and only the verdict moves:

- ``repairable == 0`` -> ``needs_human_review`` plus a hint, and a ``warn``
  drops to ``info``. There is nothing an operator can run; keeping the DB
  ``degraded`` says only that the check still holds an opinion.
- ``critical`` is NOT de-escalated. Severity there means the read contract is
  broken *right now* (see above), which is a statement about reads, not about
  repairs — an unfixable ``embedding_dimension`` mismatch is more urgent than a
  fixable one, not less. It is marked for review and keeps its gate.
- A missing declaration never de-escalates and is surfaced on the issue itself
  (``repairable_undeclared``), because a rule that silently exempts whoever
  forgot it is opt-in wearing a contract's clothes. The binding enforcement is
  the meta-test over the registry, not this fallback.
"""

import datetime
import json
import logging
import re
import sqlite3

from cpersona import config, health, operating_context, vector
from cpersona.isolation import isolation_where
from cpersona.config import (
    FTS_ENABLED,
    MAX_CONTENT_LENGTH,
    MAX_PROFILE_LENGTH,
    STORE_BLOB,
    VECTOR_SEARCH_MODE,
    local_blobs_stored,
)
from cpersona.database import SCHEMA_VERSION
from cpersona.utils import (
    SCORING_VERSION,
    _MEMORY_ANNOTATION_PATTERN,
    _MENTION_PATTERN,
    canonical_source_types_sql,
    normalize_source,
)

logger = logging.getLogger(__name__)

SEVERITIES = ("info", "warn", "critical")

# Deterministic escalation thresholds (see module docstring).
NULL_EMBEDDING_CRITICAL_RATIO = 0.5
FTS_DESYNC_CRITICAL_RATIO = 0.05
NEAR_DUPLICATE_COSINE = 0.97
# Embedded rows deep_near_duplicate compares, bounding its O(n^2) dense cosine
# matrix. Sized in config, where the measured time and peak memory behind the
# number are written down; bound here so a test can steer it by patching this
# name, as the other two caps below are.
NEAR_DUPLICATE_ROW_CAP = config.NEAR_DUPLICATE_ROW_CAP
# Offending source rows classified per check_invalid_source_type run. Bounds the
# JSON parsing a plain check_health does; past it the sample is incomplete and
# the check declines to downgrade its own severity.
INVALID_SOURCE_CLASSIFY_CAP = config.INVALID_SOURCE_CLASSIFY_CAP
# NULL-embedding rows one run re-embeds (prefetch and repair read the same
# number, and `repairable` is bounded by it — a fixer that reaches the cap must
# not report the whole backlog as repairable).
REEMBED_ROW_CAP = config.REEMBED_ROW_CAP
# Texts per /embed request when re-embedding in bulk. This is the largest batch
# CPersona ever sends, which is the number the getting-started contract quotes to
# anyone writing their own embedding backend — named rather than inlined so the
# doc check reads the same value the loop uses.
EMBED_BATCH_SIZE = 32
CALIBRATION_STALE_DAYS = 90

_USERNAME_PREFIX_PATTERN = re.compile(r"^\[(.+?)\]\s")
_SHORT_CONTENT_THRESHOLD = 5
_STALE_PROFILE_DAYS = 30


# ---------------------------------------------------------------------------
# check_health runners — each returns a list of issue dicts. The dispatcher
# stamps ``severity`` from the registry default unless the runner set one.
# ---------------------------------------------------------------------------


# bug-028: the content-rewriting fixers below (annotation/mention/oversized)
# must NULL the embedding alongside the content edit. The BLOB still encodes the
# OLD text, and no other fixer repairs a content/embedding mismatch
# (check_null_embedding only re-embeds NULL blobs, check_embedding_dimension only
# NULLs wrong-length blobs), so leaving it stale would make vector recall score
# the row on obsolete semantics indefinitely. NULLing routes the row into
# check_null_embedding's re-embed path — the same self-heal do_update_memory uses.
# bug-127: this shared guard generalizes bug-113 to every content-rewriting
# check. A rewritten body that collides with the existing row is itself a
# duplicate, so keep the existing row and never touch locked rows.
async def _rewrite_or_delete_on_collision(db, row_id: int, new_content: str) -> None:
    """Rewrite content for re-embedding, or delete an unlocked dedup collision."""
    try:
        await db.execute(
            "UPDATE memories SET content = ?, embedding = NULL WHERE id = ? AND locked = 0",
            (new_content, row_id),
        )
    except sqlite3.IntegrityError:
        await db.execute("DELETE FROM memories WHERE id = ? AND locked = 0", (row_id,))


# The three content-rewriting checks below share one shape: match a pattern,
# rewrite through _rewrite_or_delete_on_collision. That helper carries
# `locked = 0`, so a locked match is read, iterated and written past — the
# statement no-ops. Selecting `locked` and filtering here instead makes the fix
# loop and the `repairable` count the same set by construction, rather than two
# expressions of one invariant that can drift apart
# ([[feedback-one-invariant-one-implementation]]).
def _unlocked(rows: list) -> list[tuple[int, str]]:
    """(id, content) for rows the content-rewriting fixers can actually write."""
    return [(r[0], r[1]) for r in rows if not r[2]]


# bug-226: the SQL LIKE that DETECTS is wider than the regex that REPAIRS
# ('[Memory from' with no closing bracket, a Discord role mention '<@&1>'), so a
# matched row can rewrite to itself. The old fix wrote that identical text back
# with `embedding = NULL` on every run: the row never left the finding, and each
# maintenance pass destroyed a vector for a text that did not change (under
# EMBEDDING_MODE=none, permanently). Only rows the regex actually changes are
# repairable, and only those are written.
def _rewritable(rows: list, pattern: re.Pattern) -> list[tuple[int, str]]:
    """(id, cleaned) for the unlocked rows whose rewrite is not a no-op."""
    out = []
    for row_id, content in _unlocked(rows):
        cleaned = pattern.sub("", content).strip()
        if cleaned != content:
            out.append((row_id, cleaned))
    return out


async def check_memory_annotation(db, agent_id: str, fix: bool) -> list[dict]:
    iso = isolation_where(agent_id=agent_id or None)
    rows = await db.execute_fetchall(
        f"SELECT id, content, locked FROM memories WHERE content LIKE '%[Memory from%'{iso.and_clause}",
        iso.params,
    )
    if not rows:
        return []
    writable = _rewritable(rows, _MEMORY_ANNOTATION_PATTERN)
    if fix:
        for row_id, cleaned in writable:
            await _rewrite_or_delete_on_collision(db, row_id, cleaned)
    return [{"type": "memory_annotation", "count": len(rows), "repairable": len(writable)}]


async def check_discord_mention(db, agent_id: str, fix: bool) -> list[dict]:
    iso = isolation_where(agent_id=agent_id or None)
    rows = await db.execute_fetchall(
        f"SELECT id, content, locked FROM memories WHERE content LIKE '%<@%'{iso.and_clause}", iso.params
    )
    if not rows:
        return []
    writable = _rewritable(rows, _MENTION_PATTERN)
    if fix:
        for row_id, cleaned in writable:
            await _rewrite_or_delete_on_collision(db, row_id, cleaned)
    return [{"type": "discord_mention", "count": len(rows), "repairable": len(writable)}]


async def check_duplicate_content(db, agent_id: str, fix: bool) -> list[dict]:
    iso = isolation_where(agent_id=agent_id or None)
    # bug-014: group by (agent_id, project_id, content) — deliberately NOT the
    # same key as the idx_memories_dedup_content UNIQUE index
    # (agent_id, project_id, channel, content). Omitting channel is intentional:
    # the index only forbids exact (…,channel,…) duplicates at write time, and
    # this check is what collapses the same content across different channels of
    # one project (the cross-channel cleanup the index deliberately leaves to
    # check_health — see test_v2435_bugfixes.py::_insert_dup). Including
    # project_id is the fix: project_id is a hard γ-isolation axis, so the same
    # content under project '' (global) and project 'X' are legitimately
    # distinct rows with different visibility. The previous (agent_id, content)
    # grouping collapsed them and the MIN(id) survivor could delete the global
    # copy, silently narrowing visibility for every other project (bug-014).
    dup_rows = await db.execute_fetchall(
        f"""SELECT content, COUNT(*) as cnt FROM memories
            WHERE 1=1{iso.and_clause}
            GROUP BY agent_id, project_id, content HAVING cnt > 1""",
        iso.params,
    )
    if not dup_rows:
        return []
    total_dupes = sum(r[1] - 1 for r in dup_rows)
    # Agent-scoped, locked-safe (bug-007): only unlocked non-survivors within
    # scope. The survivor grouping MUST match the detection grouping above
    # (bug-014). bug-128: prefer the channel='' shared row so cross-channel dedup
    # never deletes the broadest-visibility copy; otherwise keep the MIN(id).
    #
    # One fragment, two verbs: the COUNT that reports `repairable` and the DELETE
    # that performs the repair have to select the same rows, and a count that
    # disagrees with its own delete is the defect the contract exists to catch
    # (the reasoning behind ``invalid_source_type_where``). Local rather than a
    # module constant so the isolation predicate stays inside the statement the
    # agent-scoping gate reads.
    surplus = f"""FROM memories
                WHERE locked = 0
                  AND id NOT IN (
                      SELECT COALESCE(MIN(CASE WHEN channel = '' THEN id END), MIN(id))
                      FROM memories GROUP BY agent_id, project_id, content
                  )
                 {iso.and_clause}"""
    # Counted from the fixer's own predicate, not derived from `total_extra`: a
    # group whose every non-survivor is locked contributes to total_extra and to
    # nothing a fix can write (bug-139's lesson, applied before the repair
    # rather than after it).
    deletable = (await db.execute_fetchall(f"SELECT COUNT(*) {surplus}", iso.params))[0][0]
    if fix:
        await db.execute(f"DELETE {surplus}", iso.params)
    return [
        {
            "type": "duplicate_content",
            "groups": len(dup_rows),
            "total_extra": total_dupes,
            "repairable": deletable,
        }
    ]


async def check_oversized_content(db, agent_id: str, fix: bool) -> list[dict]:
    iso = isolation_where(agent_id=agent_id or None)
    rows = await db.execute_fetchall(
        f"""SELECT id, content, locked, length(content) as len FROM memories
            WHERE length(content) > ?{iso.and_clause}""",
        (MAX_CONTENT_LENGTH, *iso.params),
    )
    if not rows:
        return []
    writable = _unlocked(rows)
    if fix:
        for row_id, content in writable:
            await _rewrite_or_delete_on_collision(db, row_id, content[:MAX_CONTENT_LENGTH])
    return [
        {
            "type": "oversized_content",
            "count": len(rows),
            "repairable": len(writable),
            "max_len": max(r[3] for r in rows),
        }
    ]


async def check_oversized_profile(db, agent_id: str, fix: bool) -> list[dict]:
    """Detect (and truncate) profiles longer than the profile cap.

    bug-188 residual. The write path is bounded now — ``do_update_profile`` runs
    the text through ``store``'s sanitising seam — but that only governs new
    writes. A profile stored oversized by an earlier version keeps its size, and
    ``check_oversized_content`` scans ``memories`` only, so nothing looked at the
    one row that is injected into EVERY recall response. A cap on the write path
    with no detector behind it leaves the expensive case exactly where it was.

    Separate from ``check_oversized_content`` rather than folded into it: the
    repair differs. An oversized memory is truncated through
    ``_rewrite_or_delete_on_collision`` because shortening content can collide
    with the dedup index; a profile has no such index and is simply cut. Merging
    them would also merge their counts, and "3 oversized rows" that means two
    memories and a profile tells an operator less than either number alone.

    The threshold is MAX_PROFILE_LENGTH, the same constant the profile
    write path caps at — detection, repair and the writer read one number. When
    the memory cap moves (the 2.6 tree) this check does not move with it.
    """
    iso = isolation_where(agent_id=agent_id or None)
    rows = await db.execute_fetchall(
        f"""SELECT agent_id, user_id, length(content) AS len FROM profiles
            WHERE length(content) > ?{iso.and_clause}""",
        (MAX_PROFILE_LENGTH, *iso.params),
    )
    if not rows:
        return []
    if fix:
        for row_agent_id, user_id, _ in rows:
            await db.execute(
                """UPDATE profiles SET content = substr(content, 1, ?)
                   WHERE agent_id = ? AND user_id = ?""",
                (MAX_PROFILE_LENGTH, row_agent_id, user_id),
            )
    return [
        {
            "type": "oversized_profile",
            "count": len(rows),
            "max_len": max(r[2] for r in rows),
            # `profiles` has no `locked` column (the invariant is bug-098's, and
            # it is defined over authored memory rows), so the truncation reaches
            # every profile the scan found.
            "repairable": len(rows),
        }
    ]


async def check_embedding_dimension(db, agent_id: str, fix: bool, embedding_cache=None) -> list[dict]:
    if not vector._embedding_client:
        return []
    iso = isolation_where(agent_id=agent_id or None)
    try:
        # bug-083: when do_check_health pre-probed the dimension outside the write seam
        # (embedding_cache carries it as "expected_dim"), use that instead of a live
        # probe — a fix=True run executes this check INSIDE transaction(), and an embed
        # here holds the shared write lock across an HTTP round-trip bounded only by the
        # embedding timeout, stalling every other writer (the bug-072 class). A None
        # probe result skips the check, same as a failed live probe.
        if embedding_cache is not None:
            expected_dim = embedding_cache.get("expected_dim")
        else:
            test_emb = await vector._embedding_client.embed(["test"])
            expected_dim = len(test_emb[0]) if test_emb and test_emb[0] else None
        if not expected_dim:
            return []
        expected_bytes = expected_dim * 4
        mismatched_mem = (
            await db.execute_fetchall(
                f"""SELECT COUNT(*) FROM memories
                WHERE embedding IS NOT NULL AND length(embedding) != ?{iso.and_clause}""",
                (expected_bytes, *iso.params),
            )
        )[0][0]
        mismatched_ep = (
            await db.execute_fetchall(
                f"""SELECT COUNT(*) FROM episodes
                WHERE embedding IS NOT NULL AND length(embedding) != ?{iso.and_clause}""",
                (expected_bytes, *iso.params),
            )
        )[0][0]
        mismatched = mismatched_mem + mismatched_ep
        if mismatched == 0:
            return []
        if fix:
            # NULL out mismatched BLOBs so the null_embedding fixer re-embeds them.
            if mismatched_mem > 0:
                await db.execute(
                    f"""UPDATE memories SET embedding = NULL
                    WHERE embedding IS NOT NULL AND length(embedding) != ?{iso.and_clause}""",
                    (expected_bytes, *iso.params),
                )
            if mismatched_ep > 0:
                await db.execute(
                    f"""UPDATE episodes SET embedding = NULL
                    WHERE embedding IS NOT NULL AND length(embedding) != ?{iso.and_clause}""",
                    (expected_bytes, *iso.params),
                )
        return [
            {
                "type": "embedding_dimension_mismatch",
                "count": mismatched,
                "memories": mismatched_mem,
                "episodes": mismatched_ep,
                "expected_dim": expected_dim,
                # Every mismatched row is writable: this fixer NULLs a BLOB
                # rather than rewriting caller data, so it carries no locked = 0
                # guard (bug-098 protects authored content, and a wrong-length
                # vector is not that). The declaration is still required — and
                # `critical` is never de-escalated regardless.
                "repairable": mismatched,
            }
        ]
    except Exception as e:
        logger.warning("Embedding dimension check failed: %s", e)
        return []


def _blobs_are_stored() -> bool:
    """bug-182: does this configuration keep a local embedding BLOB per row?

    Reads the module-level copies at call time (so a test patching
    ``checks.STORE_BLOB`` steers this) and defers the rule itself to
    ``config.local_blobs_stored``, which the write gate calls with its own copies.
    """
    return local_blobs_stored(VECTOR_SEARCH_MODE, STORE_BLOB)


def _null_embedding_severity(null_count: int, total: int, *, blobs_expected: bool = True) -> str:
    if not vector._embedding_client:
        return "info"  # mode=none: NULL is the expected steady state
    if not blobs_expected:
        # bug-182: remote search with CPERSONA_STORE_BLOB=false never writes a
        # local BLOB for a memory, so every memory row is NULL *by configuration*.
        # Reading that as a dead embedding pipeline reported a correctly-configured
        # deployment as critical forever — the same "rare is not wrong" mistake
        # bug-009 fixed for the global channel. Episodes pass blobs_expected=True:
        # _prepare_episode_row has no storage gate, so a NULL episode embedding is
        # a real failure in every configuration.
        return "info"
    if total > 0 and null_count / total > NULL_EMBEDDING_CRITICAL_RATIO:
        return "critical"  # pipeline is effectively down
    return "warn"


def _reembeddable(null_count: int, *, blobs_expected: bool = True) -> int:
    """Rows the NULL-embedding repair would write this run (the `repairable`
    contract). Zero when the repair cannot run at all — no embedding client
    (``mode=none``), or a configuration that stores no local BLOB (bug-182) —
    because in both cases the count is a steady state, not a backlog."""
    if not vector._embedding_client or not blobs_expected:
        return 0
    return min(null_count, REEMBED_ROW_CAP)


async def probe_embedding_dim() -> tuple[int | None, bool]:
    """One live probe embed, meant to run OUTSIDE the write lock (bug-083).
    do_check_health(fix=True) calls this in the unlocked prefetch phase and passes the
    result through embedding_cache["expected_dim"], so check_embedding_dimension no
    longer holds the shared write lock across an embedding HTTP round-trip. None means
    the probe failed (or no client) — the dimension check then skips, same as a failed
    live probe.

    Returns ``(dimension, reached_backend)``. The two are separate because they are
    separately true (bug-248). This probe embeds the constant ``"test"``, which makes it
    the most cache-warm key in the process, and the client answers a repeated single-text
    embed from its TTL LRU cache without issuing a request. A cached vector still has the
    right *length*, so the dimension is returned and check_embedding_dimension keeps
    working — but it is not evidence the endpoint is up, and reading it as such let a dead
    backend be reported as connected. Only ``reached_backend`` may carry a liveness claim.
    """
    if not vector._embedding_client:
        return None, False
    # bug-274 (same class as the prefetch below): this probe is the ONLY network call the
    # unlocked phase makes when no row needs re-embedding, so a database that is fully
    # embedded and a backend that is down produced silence together. The call reports a
    # dead endpoint by returning nothing rather than by raising, so read the outcome.
    # Failure only, for the reason stated on the prefetch: recovery stays with recall.
    try:
        emb, outcome = await vector._embedding_client.embed_with_outcome(["test"])
    except Exception as exc:
        health.observe_failure(f"dimension probe raised {type(exc).__name__}")
        return None, True
    if outcome.error:
        health.observe_failure(outcome.error, attempted=outcome.attempted)
        return None, outcome.attempted
    return (len(emb[0]) if emb and emb[0] else None), outcome.attempted



async def check_embedding_backend(db, agent_id: str, fix: bool, embedding_cache=None) -> list[dict]:
    """Report what the embedding backend is doing, using state that already exists.

    ``docs/operations.md`` used to warn its readers, in prose, that a green
    ``check_health`` is not evidence the embedding server is up: an unreachable endpoint
    made the dimension check *skip*, and nothing else on this surface watched liveness.
    This turns that warning into a finding.

    No new state machine and no new I/O (both ruled out on this task). The unlocked
    prefetch phase already probes the backend and hands the result over as
    ``embedding_cache["expected_dim"]``; a configured client with no dimension back is a
    backend that did not answer. What was missing was a reader.

    Four states, and the distinction that matters is between the first two:

    - **unreachable** — ``warn``. Configured and not answering. Reads stay correct, so it
      is not ``critical``; it was invisible, which is why it is here.
    - **not probed** — ``info``. Liveness was not tested on this run, for either of two
      reasons the finding names in ``reason``. Under ``fix=False`` nothing calls the
      backend at all. Under ``fix=True`` the probe can be served from the embedding
      client's cache: it embeds a constant, so it is the most cache-warm key in the
      process, and a cached vector carries the right dimension without a request leaving
      it (bug-248). Reading that number as an answer let a dead backend report as
      connected — this check being answered by the very silence it was built to remove.
      Saying nothing in either case would re-create that silence.
    - **connected** — no finding, the same way every other check reports "nothing wrong".
      Claimed only for a probe that reached the backend.

    **No backend configured is not reported here**, and that is a decision rather than an
    omission. It is the one state that was never confusable with "the server is up" —
    there is no server to be up — so it is not part of the sentence this check exists to
    make true. It is also permanent, and a finding that can never be resolved is the
    crying-wolf cost that teaches an operator to skim past this check. The state already
    has an owner: ``observe_config()`` raises the hint that reaches the user on the
    surface they actually read, and it downgrades there for this same reason (a standing
    condition, not an outage).
    """
    if not vector._embedding_client:
        return []

    observed = health.observed_state()
    cache = embedding_cache or {}
    probed = cache.get("expected_dim")
    # bug-248: whether the probe reached the backend, not whether it produced a number.
    # The dimension can come from the client's embed cache, which answers without a
    # request leaving the process; only a call that went out can support "connected".
    reached_backend = bool(cache.get("dim_probe_reached_backend"))

    if probed is None and embedding_cache is not None:
        unreachable = True
    else:
        # Nothing probed on this run. A fault latched by a recall is still evidence.
        unreachable = health.is_faulted()

    if unreachable:
        # bug-275: "no dimension came back" is not the same fact as "the backend did
        # not answer". A failure that never reached it says nothing about the
        # endpoint, and the repair named below would send the operator to restart a
        # service that was never contacted. The axis is already decided at the
        # observation point, so read it rather than re-deriving it here.
        misconfigured = observed.get("fault_kind") == "misconfigured"
        return [
            {
                "type": "embedding_backend_misconfigured" if misconfigured else "embedding_backend_unreachable",
                "status": "misconfigured" if misconfigured else "unreachable",
                "severity": "warn",
                "evidence": observed["evidence"] or "no detail captured",
                "semantic_recall": "degraded — recall is falling back to keyword/FTS",
                "hint": (
                    "no request reached a backend: CPERSONA_EMBEDDING_MODE is not one of "
                    "none, http, api (http also needs CPERSONA_EMBEDDING_URL, api needs "
                    "CPERSONA_EMBEDDING_API_KEY). Correct it and restart CPersona — the "
                    "embedding server itself is not involved"
                    if misconfigured
                    else "the configured embedding backend did not answer; restart or "
                    "reconfigure it, then re-run this check to confirm recovery"
                ),
            }
        ]

    if embedding_cache is None or not reached_backend:
        # bug-248: two ways for liveness to go untested, and the third state already
        # existed for exactly this. Under fix=False nothing calls the backend. Under
        # fix=True the call can be served from the client's embed cache, which returns
        # the right dimension without reaching the endpoint — that used to fall through
        # to "connected", so the check built to remove this silence was answered by it.
        from_cache = embedding_cache is not None
        return [
            {
                "type": "embedding_backend_not_probed",
                "status": "not_probed",
                "last_observed": observed["state"],
                "reason": "served_from_cache" if from_cache else "no_probe_on_this_run",
                "hint": (
                    (
                        "the dimension probe was answered from the embedding client's "
                        "cache, so no request reached the backend on this run; re-run "
                        "once the cache entry expires to test liveness"
                    )
                    if from_cache
                    else (
                        "liveness was not tested on this run — it is probed under fix=true. "
                        "A finding-free result here is not evidence the backend is up"
                    )
                ),
            }
        ]

    return []


async def prefetch_null_embeddings(db, agent_id: str = "") -> dict:
    """Pre-compute embeddings for NULL-embedding memory/episode rows OUTSIDE any write
    lock (bug-072). do_check_health calls this before taking the shared write lock so the
    batched embedding HTTP calls do not stall every other writer — do_store, the queue
    drain, import/merge — for the whole re-embed duration. Returns
    {"memories": {id: (text, blob)}, "episodes": {id: (text, blob)}}; the text the blob
    was computed from rides along so the write path can refuse to attach it to changed
    content (bug-077). Empty when there is no embedding client (the fix loop then no-ops
    just as before)."""
    out: dict = {"memories": {}, "episodes": {}}
    if not vector._embedding_client:
        return out
    # bug-129: do not re-probe a backend already latched as faulted by recall.
    if health.is_faulted():
        return out
    iso = isolation_where(agent_id=agent_id or None)
    tables = [("memories", "content"), ("episodes", "summary")]
    if not _blobs_are_stored():
        # bug-182: a memory BLOB is not written in this configuration, so nothing
        # downstream may apply one — computing them is one embed round-trip per
        # NULL row (up to REEMBED_ROW_CAP) burned on every fix run. Episodes stay: they carry
        # a BLOB regardless of the storage policy.
        tables = [t for t in tables if t[0] != "memories"]
    for table, text_col in tables:
        rows = await db.execute_fetchall(
            f"SELECT id, {text_col} FROM {table} WHERE embedding IS NULL{iso.and_clause} LIMIT ?", (*iso.params, REEMBED_ROW_CAP)
        )
        for start in range(0, len(rows), EMBED_BATCH_SIZE):
            chunk = rows[start : start + EMBED_BATCH_SIZE]
            # bug-274: bug-129 wired this loop to feed the health breaker, and that fix was
            # recorded as done. It never fired on the failure that matters most. The call
            # it watched reports an unreachable endpoint by returning no embeddings, not
            # by raising, so the `except` below stayed dead on that path, the loop that
            # follows fell through on an empty sequence, and a re-embed pass that did no
            # work reported success. Reading the outcome the call now carries is what
            # makes the failure legible here.
            #
            # Only failures are reported. Recovery stays on the recall path's embed
            # success, which is the asymmetry bug-129 already chose: clearing the state
            # from here would let a maintenance run erase a fault a user's recall had just
            # latched, along with the record of which sessions had been told about it.
            failure: str | None = None
            embeddings = None
            try:
                embeddings, outcome = await vector._embedding_client.embed_with_outcome(
                    [text for _, text in chunk]
                )
                # `error` is set for a failed request and for a misconfigured mode, and is
                # absent when there is simply no backend to call — which is a standing
                # condition reported by observe_config(), not a fault to promote here.
                failure = outcome.error
            except Exception as exc:
                # Transport failure arrives through the outcome above, so reaching here
                # means something outside that contract broke. Name the exception type:
                # this string is the `evidence` slot of the advisory a user reads, and the
                # constant that used to sit here said nothing about what went wrong.
                failure = f"prefetch embed raised {type(exc).__name__}"
            if failure:
                health.observe_failure(failure)
                if health.is_faulted():
                    return out
                continue
            for (row_id, text), embedding in zip(chunk, embeddings or []):
                if embedding:
                    out[table][row_id] = (
                        text,
                        vector._embedding_client.pack_embedding(embedding),
                    )
    return out


async def apply_embedding_cache(db, embedding_cache) -> int:
    """Write pre-computed embeddings onto rows that are STILL NULL and whose text is
    unchanged since prefetch. Meant to run under the write lock but does no network I/O.
    The `AND {text_col} = ?` predicate is the bug-077 guard: prefetch ran unlocked, so a
    raced writer (do_update_memory on embed failure) or a sibling fixer in the same run
    (memory_annotation / discord_mention / oversized_content rewrite content and NULL the
    embedding) may have changed the text after the blob was computed — attaching the old
    text's vector to the new text would silently desync content and embedding (the
    bug-028 coherence class). A mismatch simply leaves the row NULL for the next
    unlocked pass. Returns the number of rows updated."""
    applied = 0
    for table, text_col in (("memories", "content"), ("episodes", "summary")):
        for row_id, (text, blob) in (embedding_cache or {}).get(table, {}).items():
            cur = await db.execute(
                f"UPDATE {table} SET embedding = ? WHERE id = ? AND embedding IS NULL AND {text_col} = ?",
                (blob, row_id, text),
            )
            if getattr(cur, "rowcount", 0) == 1:
                applied += 1
    return applied


async def _reembed_null_rows(db, table: str, text_col: str, iso, embedding_cache) -> int:
    """Fill NULL embeddings for one table, preferring a pre-computed blob from
    embedding_cache (bug-072) so the HTTP round-trips happened outside the write lock.

    bug-077: a cache hit is applied with `AND {text_col} = ?` so a blob computed from
    stale text (the row's content changed between the unlocked prefetch and this locked
    write) is never attached to the new content.
    bug-083: when a cache dict is present (the locked do_check_health path) a cache miss
    does NOT fall back to a live embed — that would hold the shared write lock across an
    HTTP round-trip, the exact stall bug-072 removed. The row stays NULL and is repaired
    by do_check_health's second unlocked pass. An embedding_cache of None (direct calls /
    tests, no lock held) keeps the live path."""
    cache = (embedding_cache or {}).get(table, {})
    rows = await db.execute_fetchall(
        f"SELECT id, {text_col} FROM {table} WHERE embedding IS NULL{iso.and_clause} LIMIT ?", (*iso.params, REEMBED_ROW_CAP)
    )
    re_embedded = 0
    for row_id, text in rows:
        try:
            cached = cache.get(row_id)
            if cached is not None:
                cached_text, blob = cached
                if cached_text != text:
                    continue  # bug-077: stale prefetch — leave NULL for the next pass
            elif embedding_cache is not None:
                continue  # bug-083: no live embeds while the write lock is held
            else:
                emb = await vector._embedding_client.embed([text])
                blob = vector._embedding_client.pack_embedding(emb[0]) if emb and emb[0] else None
            if blob is not None:
                cur = await db.execute(
                    f"UPDATE {table} SET embedding = ? WHERE id = ? AND embedding IS NULL AND {text_col} = ?",
                    (blob, row_id, text),
                )
                if getattr(cur, "rowcount", 0) == 1:
                    re_embedded += 1
        except Exception:
            pass
    return re_embedded


async def check_null_embedding(db, agent_id: str, fix: bool, embedding_cache=None) -> list[dict]:
    iso = isolation_where(agent_id=agent_id or None)
    null_count = (
        await db.execute_fetchall(
            f"SELECT COUNT(*) FROM memories WHERE embedding IS NULL{iso.and_clause}", iso.params
        )
    )[0][0]
    if null_count == 0:
        return []
    total = (
        await db.execute_fetchall(f"SELECT COUNT(*) FROM memories WHERE 1=1{iso.and_clause}", iso.params)
    )[0][0]
    blobs_expected = _blobs_are_stored()
    issue = {
        "type": "null_embedding",
        "count": null_count,
        "severity": _null_embedding_severity(null_count, total, blobs_expected=blobs_expected),
        # Bounded by the fixer's own per-run reach, not by the finding: a corpus
        # with 5,000 NULL rows is repaired 500 at a time, and claiming 5,000 here
        # would describe work this run will not do.
        "repairable": _reembeddable(null_count, blobs_expected=blobs_expected),
    }
    if not blobs_expected:
        # bug-182: say why the repair does not run, so a NULL count that is never
        # going down reads as policy rather than as a fixer that keeps failing.
        issue["repair"] = "skipped: this configuration stores no local memory embeddings"
    elif fix and vector._embedding_client:
        re_embedded = await _reembed_null_rows(db, "memories", "content", iso, embedding_cache)
        if re_embedded > 0:
            issue["re_embedded"] = re_embedded
    return [issue]


async def check_null_episode_embedding(db, agent_id: str, fix: bool, embedding_cache=None) -> list[dict]:
    iso = isolation_where(agent_id=agent_id or None)
    null_count = (
        await db.execute_fetchall(
            f"SELECT COUNT(*) FROM episodes WHERE embedding IS NULL{iso.and_clause}", iso.params
        )
    )[0][0]
    if null_count == 0:
        return []
    total = (
        await db.execute_fetchall(f"SELECT COUNT(*) FROM episodes WHERE 1=1{iso.and_clause}", iso.params)
    )[0][0]
    issue = {
        "type": "null_episode_embedding",
        "count": null_count,
        "severity": _null_embedding_severity(null_count, total),
        # blobs_expected=True: _prepare_episode_row has no storage gate, so an
        # episode carries a BLOB in every configuration (bug-182).
        "repairable": _reembeddable(null_count),
    }
    if fix and vector._embedding_client:
        re_embedded = await _reembed_null_rows(db, "episodes", "summary", iso, embedding_cache)
        if re_embedded > 0:
            issue["re_embedded"] = re_embedded
    return [issue]


async def check_fts_integrity(db, agent_id: str, fix: bool) -> list[dict]:
    """Content-level FTS5 index verification via the ``integrity-check`` command.

    With the external-content flag (rank=1, SQLite >= 3.42) this catches both
    ghost index rows and rows whose *indexed text* no longer matches the
    content table — the bug-008 failure class. It supersedes the pre-v2.4.37
    row-count comparison, which was structurally blind here: on an
    external-content FTS5 table ``COUNT(*)`` proxies to the content table, so
    the two counts could never differ (verified empirically). On older SQLite
    the enhanced form is unavailable and we fall back to the internal-only
    structural check. Fix rebuilds the index and re-verifies.
    """
    if not FTS_ENABLED:
        return []
    issues: list[dict] = []
    for table, fts in (("memories", "memories_fts"), ("episodes", "episodes_fts")):
        rebuild = f"INSERT INTO {fts}({fts}) VALUES('rebuild')"
        corrupt = False
        try:
            await db.execute(f"INSERT INTO {fts}({fts}, rank) VALUES('integrity-check', 1)")
        except sqlite3.OperationalError:
            # Enhanced (external-content) form unsupported — structural check only.
            try:
                await db.execute(f"INSERT INTO {fts}({fts}) VALUES('integrity-check')")
            except sqlite3.OperationalError:
                continue  # FTS table absent or command unsupported entirely
            except sqlite3.DatabaseError:
                corrupt = True
        except sqlite3.DatabaseError:
            corrupt = True
        if not corrupt:
            continue
        # Object-scoped, not row-scoped: the repair is one whole-index rebuild,
        # always attempted, with no locked subset to fall short of. 1 = "there is
        # an action" (whether it succeeds is reported separately as `fixed`).
        issue = {
            "type": "fts_integrity_failure",
            "table": table,
            "severity": "critical",
            "repairable": 1,
        }
        if fix:
            await db.execute(rebuild)
            # bug-069: mirror the detection fallback ladder. The enhanced rank=1 verify is
            # unsupported on SQLite < 3.42 and raises OperationalError there; without this
            # fallback it was caught as corruption below, falsely reporting fixed:False even
            # though the rebuild succeeded. Try enhanced → structural; only a genuine
            # DatabaseError (real corruption after rebuild) marks it unfixed.
            try:
                await db.execute(f"INSERT INTO {fts}({fts}, rank) VALUES('integrity-check', 1)")
                issue["fixed"] = True
            except sqlite3.OperationalError:
                try:
                    await db.execute(f"INSERT INTO {fts}({fts}) VALUES('integrity-check')")
                    issue["fixed"] = True
                except sqlite3.DatabaseError:
                    issue["fixed"] = False
            except sqlite3.DatabaseError:
                issue["fixed"] = False
        issues.append(issue)
    return issues


async def check_schema_version(db, agent_id: str, fix: bool) -> list[dict]:
    try:
        db_version = (await db.execute_fetchall("SELECT MAX(version) FROM schema_version"))[0][0]
    except Exception:
        return []
    if db_version == SCHEMA_VERSION:
        return []
    return [
        {
            "type": "schema_version_mismatch",
            "db_version": db_version,
            "expected": SCHEMA_VERSION,
        }
    ]


# Canonical definitions of load-bearing schema objects, compared against
# sqlite_master after token normalization. The golden-DDL test pins these to
# what database.py actually creates, so the two definitions cannot drift.
# critical = losing the object silently breaks a data guarantee (dedup
# uniqueness, FTS sync); warn = performance/scoping index only.
_EXPECTED_OBJECTS: dict[str, dict] = {
    "idx_memories_dedup_content": {
        "kind": "index",
        "severity": "critical",
        "sql": "CREATE UNIQUE INDEX idx_memories_dedup_content "
        "ON memories(agent_id, project_id, channel, content)",
    },
    "idx_memories_dedup_msg_id": {
        "kind": "index",
        "severity": "critical",
        "sql": "CREATE UNIQUE INDEX idx_memories_dedup_msg_id "
        "ON memories(agent_id, project_id, msg_id) WHERE msg_id != ''",
    },
    "memories_fts_ai": {
        "kind": "trigger",
        "severity": "critical",
        "fts": True,
        "sql": "CREATE TRIGGER memories_fts_ai AFTER INSERT ON memories BEGIN "
        "INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content); END",
    },
    "memories_fts_ad": {
        "kind": "trigger",
        "severity": "critical",
        "fts": True,
        "sql": "CREATE TRIGGER memories_fts_ad AFTER DELETE ON memories BEGIN "
        "INSERT INTO memories_fts(memories_fts, rowid, content) "
        "VALUES ('delete', old.id, old.content); END",
    },
    "memories_fts_au": {
        "kind": "trigger",
        "severity": "critical",
        "fts": True,
        "sql": "CREATE TRIGGER memories_fts_au AFTER UPDATE OF content ON memories "
        "WHEN old.content <> new.content BEGIN "
        "INSERT INTO memories_fts(memories_fts, rowid, content) "
        "VALUES ('delete', old.id, old.content); "
        "INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content); END",
    },
    "episodes_ai": {
        "kind": "trigger",
        "severity": "critical",
        "fts": True,
        "sql": "CREATE TRIGGER episodes_ai AFTER INSERT ON episodes BEGIN "
        "INSERT INTO episodes_fts(rowid, summary, keywords) "
        "VALUES (new.id, new.summary, new.keywords); END",
    },
    "episodes_ad": {
        "kind": "trigger",
        "severity": "critical",
        "fts": True,
        "sql": "CREATE TRIGGER episodes_ad AFTER DELETE ON episodes BEGIN "
        "INSERT INTO episodes_fts(episodes_fts, rowid, summary, keywords) "
        "VALUES ('delete', old.id, old.summary, old.keywords); END",
    },
    "episodes_au": {
        "kind": "trigger",
        "severity": "critical",
        "fts": True,
        "sql": "CREATE TRIGGER episodes_au AFTER UPDATE OF summary, keywords ON episodes "
        "WHEN old.summary <> new.summary OR old.keywords <> new.keywords BEGIN "
        "INSERT INTO episodes_fts(episodes_fts, rowid, summary, keywords) "
        "VALUES ('delete', old.id, old.summary, old.keywords); "
        "INSERT INTO episodes_fts(rowid, summary, keywords) "
        "VALUES (new.id, new.summary, new.keywords); END",
    },
    "idx_memories_isolation": {
        "kind": "index",
        "severity": "warn",
        "sql": "CREATE INDEX idx_memories_isolation "
        "ON memories(agent_id, project_id, created_at DESC)",
    },
    "idx_episodes_isolation": {
        "kind": "index",
        "severity": "warn",
        "sql": "CREATE INDEX idx_episodes_isolation "
        "ON episodes(agent_id, project_id, created_at DESC)",
    },
    "idx_memories_agent": {
        "kind": "index",
        "severity": "warn",
        "sql": "CREATE INDEX idx_memories_agent ON memories(agent_id, created_at DESC)",
    },
    "idx_memories_msg_id": {
        "kind": "index",
        "severity": "warn",
        "sql": "CREATE INDEX idx_memories_msg_id ON memories(agent_id, msg_id)",
    },
    "idx_episodes_agent": {
        "kind": "index",
        "severity": "warn",
        "sql": "CREATE INDEX idx_episodes_agent ON episodes(agent_id, created_at DESC)",
    },
    # Losing either of these costs the index-served arm its cheapest question
    # (see PROBE_INDEX_SQL in database.py): the lost-embedding probe goes back
    # to walking the id range, ~29 ms per recall at 100,000 rows. Warn, not
    # critical — no answer changes, and boot recreates them anyway; registering
    # them is what lets check_health(fix=True) restore one without a restart.
    "idx_memories_lost_embedding": {
        "kind": "index",
        "severity": "warn",
        "sql": "CREATE INDEX idx_memories_lost_embedding ON memories(id) WHERE embedding IS NULL",
    },
    "idx_episodes_lost_embedding": {
        "kind": "index",
        "severity": "warn",
        "sql": "CREATE INDEX idx_episodes_lost_embedding ON episodes(id) WHERE embedding IS NULL",
    },
    # Losing this one sends the confidence span back to walking the isolation
    # scope, ~72 ms per recall at 100,000 rows (see SPAN_INDEX_SQL). Warn for the
    # same reasons: no answer changes, boot recreates it, and registering it is
    # what lets check_health(fix=True) restore it without a restart.
    "idx_memories_span": {
        "kind": "index",
        "severity": "warn",
        "sql": (
            "CREATE INDEX idx_memories_span"
            " ON memories(agent_id, timestamp, project_id, channel)"
        ),
    },
}

_SQL_NORMALIZE = re.compile(r"\s+")


def _normalize_sql(sql: str) -> str:
    s = sql.replace("IF NOT EXISTS ", "").replace('"', "").strip().rstrip(";")
    return _SQL_NORMALIZE.sub(" ", s).upper()


_UNIQUE_INDEX_SQL = re.compile(
    r"CREATE\s+UNIQUE\s+INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?\S+\s+ON\s+(\w+)\s*\(([^)]*)\)\s*(?:WHERE\s+(.+))?$",
    re.IGNORECASE | re.DOTALL,
)


async def _unique_index_blocked_by(db, sql: str) -> str | None:
    """Why the canonical UNIQUE index cannot be created, or None when it can.

    bug-224: asked BEFORE the drifted object is dropped. The corpus is the only
    thing that can refuse a UNIQUE index (duplicate rows the index cannot
    admit), and a duplicate can be locked — the very case the fixer must never
    force. Answering after the DROP is what left the database with no index at
    all.
    """
    match = _UNIQUE_INDEX_SQL.match(_SQL_NORMALIZE.sub(" ", sql.strip()))
    if match is None:
        # Unparseable canonical DDL: refuse rather than drop on a guess.
        return "cannot verify uniqueness before the drop: unrecognised index DDL"
    table, columns, predicate = match.group(1), match.group(2), match.group(3)
    # An index is a global schema object: a duplicate under ANY agent blocks the
    # CREATE, so this is a DELIBERATE cross-agent scan, spelled
    # isolation_where(agent_id=None) (empty and_clause) exactly as
    # check_dedup_msg_id_index spells its own.
    iso = isolation_where(agent_id=None)
    where = f"WHERE {predicate}" if predicate else "WHERE 1=1"
    try:
        dupes = (
            await db.execute_fetchall(
                f"SELECT COUNT(*) FROM (SELECT 1 FROM {table} {where}{iso.and_clause} "
                f"GROUP BY {columns} HAVING COUNT(*) > 1)",
                iso.params,
            )
        )[0][0]
    except Exception as e:  # a scan that cannot run is not a licence to drop
        return f"cannot verify uniqueness before the drop: {e}"
    if dupes:
        return (
            f"{dupes} duplicate group(s) in {table} violate the canonical UNIQUE "
            "index; the drifted object is left in place (dropping it would remove "
            "the guarantee entirely). Collapse the duplicates, then re-run fix"
        )
    return None


async def check_schema_objects(db, agent_id: str, fix: bool) -> list[dict]:
    """Compare load-bearing indexes/triggers against their canonical DDL.

    Catches the silent-failure path of the v12 migration (dedup UNIQUE index
    creation is non-fatal there) and any hand-edited or half-migrated trigger
    (e.g. an FTS trigger dropped without being recreated).
    """
    rows = await db.execute_fetchall(
        "SELECT name, sql FROM sqlite_master WHERE type IN ('index', 'trigger')"
    )
    actual = {r[0]: (r[1] or "") for r in rows}
    issues: list[dict] = []
    for name, spec in _EXPECTED_OBJECTS.items():
        if spec.get("fts") and not FTS_ENABLED:
            continue
        expected_norm = _normalize_sql(spec["sql"])
        if name not in actual:
            state = "missing"
        elif _normalize_sql(actual[name]) != expected_norm:
            state = "definition_drift"
        else:
            continue
        issue = {
            "type": "schema_object_drift",
            "object": name,
            "kind": spec["kind"],
            "state": state,
            "severity": spec["severity"],
            # Object-scoped like fts_integrity: the DROP/CREATE is always
            # attempted. Whether it succeeds is `fixed` / `fix_error`.
            "repairable": 1,
        }
        if fix:
            # bug-224: the repair must never end with FEWER objects than it
            # found. The old order (DROP, then CREATE, then swallow the CREATE's
            # failure) deleted a drifted UNIQUE index the corpus could not
            # re-admit and committed that — the write-time dedup guarantee this
            # check calls `critical` was destroyed by its own fix.
            previous_sql = actual.get(name) or ""
            blocked = None
            if state == "definition_drift" and "UNIQUE" in expected_norm:
                blocked = await _unique_index_blocked_by(db, spec["sql"])
            if blocked:
                # Refuse: keep the drifted object, say why (the locked row wins,
                # same doctrine as the migration).
                issue["fixed"] = False
                issue["fix_error"] = blocked
                issues.append(issue)
                continue
            try:
                if state == "definition_drift":
                    await db.execute(f"DROP {spec['kind'].upper()} IF EXISTS {name}")
                await db.execute(spec["sql"])
                issue["fixed"] = True
            except Exception as e:
                issue["fixed"] = False
                issue["fix_error"] = str(e)
                if state == "definition_drift" and previous_sql:
                    # The CREATE failed after the DROP: put back what was there,
                    # and record BOTH errors — a restore that also fails is the
                    # one outcome an operator must not have to infer.
                    try:
                        await db.execute(previous_sql)
                    except Exception as restore_error:
                        issue["restore_error"] = str(restore_error)
                    else:
                        issue["restored"] = True
        issues.append(issue)
    return issues


async def check_dedup_msg_id_index(db, agent_id: str, fix: bool) -> list[dict]:
    """Detect and remediate a permanently-missing msg_id dedup UNIQUE index (bug-145).

    check_schema_objects already flags idx_memories_dedup_msg_id when it is
    missing, but its fix only re-issues the identical CREATE, which fails
    identically whenever a pre-v12 DB holds two rows sharing a non-empty
    (agent_id, project_id, msg_id) with different content (the TOCTOU race the
    index closes). Nothing else collapses msg_id collisions, so that critical
    finding is otherwise permanent — only hand-written SQL could clear it. This
    check supplies the same non-destructive remediation the v12 migration
    applies: keep the newest (MAX id) colliding row's msg_id, blank the older
    unlocked colliders ('' is excluded by the partial index), then retry the
    CREATE. No row is deleted, no content is touched, and locked rows are left
    alone (bug-098) — a locked collider still blocks the CREATE and is surfaced
    via fix_error, the same "locked row wins" doctrine as check_schema_objects.

    The index is a global schema object, so (like check_schema_objects) this
    check ignores agent_id: a collision under any agent blocks the whole index.
    """
    table_row = await db.execute_fetchall(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='memories'"
    )
    if not table_row:
        # No table yet (fresh/uninitialised DB) — nothing to guard.
        return []
    index_row = await db.execute_fetchall(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_memories_dedup_msg_id'"
    )
    if index_row:
        # Index present — the guarantee holds, no finding.
        return []
    issue: dict = {
        "type": "dedup_msg_id_index_missing",
        "object": "idx_memories_dedup_msg_id",
        # Object-scoped: the collision resolution + CREATE is always attempted.
        # A locked collider that blocks the CREATE surfaces as `fix_error`, not
        # as repairable=0 — the action exists, it is the outcome that fails.
        "repairable": 1,
    }
    if fix:
        # Same collision resolution as the v12 migration (database.py): keep the
        # newest (MAX id) colliding row's msg_id, blank the older unlocked ones.
        # The index is global, so the resolution is a DELIBERATE cross-agent scan
        # spelled isolation_where(agent_id=None) (empty and_clause) — a collision
        # under any agent blocks the whole index, so scoping to one agent would
        # leave it uncreatable. locked = 0 honours bug-098.
        iso = isolation_where(agent_id=None)
        await db.execute(
            f"""UPDATE memories
               SET msg_id = ''
               WHERE locked = 0
                 AND msg_id != ''
                 AND id NOT IN (
                     SELECT MAX(id) FROM memories
                     WHERE msg_id != ''
                     GROUP BY agent_id, project_id, msg_id
                 ){iso.and_clause}""",
            iso.params,
        )
        try:
            await db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_dedup_msg_id "
                "ON memories(agent_id, project_id, msg_id) WHERE msg_id != ''"
            )
            issue["fixed"] = True
        except Exception as e:
            # A locked collider still blocks the CREATE — surface, never force.
            issue["fixed"] = False
            issue["fix_error"] = str(e)
    return [issue]


async def check_sqlite_integrity(db, agent_id: str, fix: bool) -> list[dict]:
    """PRAGMA quick_check — file-level corruption. Report-only: there is no
    safe automatic repair for a damaged database file; restore from backup."""
    try:
        rows = await db.execute_fetchall("PRAGMA quick_check")
    except Exception as e:
        return [{"type": "sqlite_integrity_failure", "detail": str(e), "severity": "critical"}]
    messages = [r[0] for r in rows]
    if messages == ["ok"]:
        return []
    return [
        {
            "type": "sqlite_integrity_failure",
            "detail": messages[:10],
            "severity": "critical",
        }
    ]


_PROJECT_ID_NORMALIZE = re.compile(r"[^a-z0-9]")


async def check_axis_hygiene(db, agent_id: str, fix: bool) -> list[dict]:
    """Flag project_id values that normalize to the same key (naming drift).

    Distinct spellings of the same bucket ('data-ops-audit' / 'dataops-audit')
    split memories across γ buckets that no single read unifies — the rows are
    invisible to each other's recalls. Report-only: which spelling is
    canonical is an operator decision, and the registry of valid project_ids
    is deliberately *not* server knowledge. Distribution itself is not an
    issue (rare != wrong); it is exposed via axis_distribution() in stats.
    """
    # bug-243: the emitted clusters are the requesting agent's own buckets. The
    # audit stays corpus-wide for the CLI global sweep (empty agent_id), where
    # cross-bucket drift belongs, but a per-agent call named every OTHER agent's
    # project_id and row count in `issues` — the disclosure bug-062 closed one
    # function below in `stats.axes`, still open here. Same boundary as
    # axis_distribution: scoped call -> own buckets, global sweep -> corpus.
    iso = isolation_where(agent_id=agent_id or None)
    rows = await db.execute_fetchall(
        f"SELECT project_id, COUNT(*) FROM memories WHERE project_id != ''{iso.and_clause} GROUP BY project_id",
        iso.params,
    )
    clusters: dict[str, list] = {}
    for pid, count in rows:
        key = _PROJECT_ID_NORMALIZE.sub("", pid.lower())
        clusters.setdefault(key, []).append({"project_id": pid, "count": count})
    drifted = [members for members in clusters.values() if len(members) > 1]
    if not drifted:
        return []
    return [{"type": "project_id_naming_drift", "clusters": drifted}]


async def axis_distribution(db, agent_id: str = "") -> dict:
    """project_id / channel distributions for the stats block (observation only).

    bug-062: scoped to the requested agent (the axes sibling of bug-058). Before this,
    a per-agent check_health returned a corpus-wide project_id/channel distribution, so
    check_health(agent_id='A') on the shared multi-agent DB disclosed every other agent's
    bucket names + counts. Empty agent_id keeps the corpus-wide view (CLI global sweep).
    """
    iso = isolation_where(agent_id=agent_id or None)
    out: dict = {}
    for axis in ("project_id", "channel"):
        rows = await db.execute_fetchall(
            f"SELECT {axis}, COUNT(*) FROM memories WHERE 1=1{iso.and_clause} GROUP BY {axis} ORDER BY COUNT(*) DESC LIMIT 20",
            iso.params,
        )
        out[axis] = {(r[0] if r[0] != "" else "(global)"): r[1] for r in rows}
    return out


async def check_invalid_json(db, agent_id: str, fix: bool) -> list[dict]:
    iso = isolation_where(agent_id=agent_id or None)
    try:
        bad_source = (
            await db.execute_fetchall(
                f"SELECT COUNT(*) FROM memories WHERE json_valid(source) = 0{iso.and_clause}", iso.params
            )
        )[0][0]
        bad_metadata = (
            await db.execute_fetchall(
                f"SELECT COUNT(*) FROM memories WHERE json_valid(metadata) = 0{iso.and_clause}", iso.params
            )
        )[0][0]
    except Exception:
        return []
    if bad_source + bad_metadata == 0:
        return []
    # Before the repair, or it would count what it just cleared. One row can be
    # bad on both columns, so this is a row count and not bad_source +
    # bad_metadata — the question the policy asks is "is there a row to write".
    repairable = (
        await db.execute_fetchall(
            f"""SELECT COUNT(*) FROM memories
                WHERE (json_valid(source) = 0 OR json_valid(metadata) = 0)
                AND locked = 0{iso.and_clause}""",
            iso.params,
        )
    )[0][0]
    if fix:
        # bug-098: every fixer that rewrites row fields carries locked = 0 —
        # check_health(fix=true) must never alter a locked memory (same guard on
        # invalid_timestamp / timestamp_format_drift / invalid_source_type /
        # deep_anonymous_source).
        await db.execute(
            f"UPDATE memories SET source = '{{}}' WHERE json_valid(source) = 0 AND locked = 0{iso.and_clause}", iso.params
        )
        await db.execute(
            f"UPDATE memories SET metadata = '{{}}' WHERE json_valid(metadata) = 0 AND locked = 0{iso.and_clause}",
            iso.params,
        )
    return [
        {
            "type": "invalid_json",
            "bad_source": bad_source,
            "bad_metadata": bad_metadata,
            "repairable": repairable,
        }
    ]


async def check_invalid_timestamp(db, agent_id: str, fix: bool) -> list[dict]:
    iso = isolation_where(agent_id=agent_id or None)
    bad_ts = (
        await db.execute_fetchall(
            f"SELECT COUNT(*) FROM memories WHERE datetime(timestamp) IS NULL AND timestamp != ''{iso.and_clause}",
            iso.params,
        )
    )[0][0]
    if bad_ts == 0:
        return []
    # Before the repair (it rewrites the very rows this counts), and with the
    # fixer's own locked = 0 / parseable-created_at guards so the two agree.
    repairable = (
        await db.execute_fetchall(
            f"""SELECT COUNT(*) FROM memories
                WHERE datetime(timestamp) IS NULL AND timestamp != ''
                AND datetime(created_at) IS NOT NULL
                AND locked = 0{iso.and_clause}""",
            iso.params,
        )
    )[0][0]
    if fix:
        # bug-229: write the CANONICAL AWARE form, not created_at verbatim.
        # created_at is SQLite-native `YYYY-MM-DD HH:MM:SS` (naive) while the
        # write path stores `...T...+00:00`, so copying it minted a `naive`
        # timestamp_format_drift finding the very next check refuses to repair
        # ("their intended zone is unknowable") — one repair manufacturing a
        # permanent one. created_at is UTC by the bug-114 invariant, so the
        # conversion adds no information. datetime(created_at) IS NOT NULL keeps
        # the strftime from evaluating to NULL against the NOT NULL column.
        await db.execute(
            f"""UPDATE memories
                SET timestamp = strftime('%Y-%m-%dT%H:%M:%S+00:00', created_at)
                WHERE datetime(timestamp) IS NULL AND timestamp != ''
                AND datetime(created_at) IS NOT NULL
                AND locked = 0{iso.and_clause}""",
            iso.params,
        )
    return [{"type": "invalid_timestamp", "count": bad_ts, "repairable": repairable}]


async def check_missing_episode_start_time(db, agent_id: str, fix: bool) -> list[dict]:
    """Episodes with no ``start_time`` (bug-208), repaired from ``created_at``.

    Two thirds of this project's production episodes carried NULL/'' here (333 of
    500, measured 2026-08-14) and every read path passed that emptiness straight
    into scoring until bug-213 taught the read paths to fall back to
    ``created_at`` (``utils.episode_timestamp``). This check is the write-side
    complement: it makes the gap visible, and ``fix=true`` materialises the same
    fallback into the row itself for every consumer that reads ``start_time``
    directly (exports, list surfaces, external tooling).

    The predicate is the SQL spelling of ``episode_timestamp``'s fallback
    condition (``start_time`` falsy); the two languages cannot share one
    implementation, so they share this reference instead — change one, check the
    other.

    ``episodes`` has no ``locked`` column, so the bug-007 fixer guard is
    structurally N/A. ``repairable`` is bounded instead by ``created_at``
    parseability: copying an unparseable created_at would trade "no timestamp"
    for "garbage timestamp" — a worse row, not a repaired one. Severity is info,
    not warn: since bug-213 the gap no longer degrades scoring (the fallback
    reads the same value this fix would write); what remains is data
    completeness.
    """
    iso = isolation_where(agent_id=agent_id or None)
    missing = (
        await db.execute_fetchall(
            f"SELECT COUNT(*) FROM episodes WHERE (start_time IS NULL OR start_time = ''){iso.and_clause}",
            iso.params,
        )
    )[0][0]
    if missing == 0:
        return []
    # Before the repair (it rewrites the very rows this counts), same discipline
    # as check_invalid_timestamp above.
    repairable = (
        await db.execute_fetchall(
            f"""SELECT COUNT(*) FROM episodes
                WHERE (start_time IS NULL OR start_time = '')
                AND datetime(created_at) IS NOT NULL{iso.and_clause}""",
            iso.params,
        )
    )[0][0]
    if fix:
        # bug-245: the same canonical aware form check_invalid_timestamp writes
        # (bug-229). Copying created_at verbatim left `start_time` holding two
        # lexical conventions at once — a genuinely recorded value is caller
        # ISO-8601 with an offset, created_at is SQLite-native and naive, and
        # ' ' < 'T' so every backfilled row sorts before every recorded one
        # regardless of date. check_episode_timestamp_format_drift below covers
        # rows the pre-2.5.5 backfill already wrote in the naive form.
        await db.execute(
            f"""UPDATE episodes
                SET start_time = strftime('%Y-%m-%dT%H:%M:%S+00:00', created_at)
                WHERE (start_time IS NULL OR start_time = '')
                AND datetime(created_at) IS NOT NULL{iso.and_clause}""",
            iso.params,
        )
    return [
        {
            "type": "missing_episode_start_time",
            "count": missing,
            "repairable": repairable,
            # The approximation caveat is load-bearing, not decorative: after the
            # fix the row is indistinguishable from one whose start_time was
            # recorded at store time.
            "hint": (
                "fix copies created_at into start_time — the time the episode was "
                "recorded, not the time of what it describes; an approximation, "
                "not a recovered truth"
            ),
        }
    ]


def _classify_timestamp(ts: str) -> str:
    """'utc' | 'aware' (non-UTC offset) | 'naive'. Deterministic string check."""
    if ts.endswith("Z") or ts.endswith("+00:00"):
        return "utc"
    # An explicit offset looks like ±HH:MM in the tail (after the 'T' part).
    tail = ts[10:]
    if "+" in tail or "-" in tail.replace("-", "", 0):
        # datetime.fromisoformat is the authority; fall back below.
        try:
            parsed = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return "naive" if parsed.tzinfo is None else "aware"
        except ValueError:
            return "naive"
    return "naive"


async def check_timestamp_format_drift(db, agent_id: str, fix: bool) -> list[dict]:
    """Mixed timezone-aware / naive timestamp formats break lexicographic
    ordering (ISO strings only sort correctly within one convention).

    Fix normalizes *aware* timestamps to UTC (+00:00) — lossless. Naive
    timestamps stay untouched: their intended zone is unknowable, so rewriting
    them would fabricate data; they are reported as unfixable instead.
    """
    iso = isolation_where(agent_id=agent_id or None)
    rows = await db.execute_fetchall(
        f"SELECT id, timestamp, locked FROM memories WHERE timestamp != ''{iso.and_clause}", iso.params
    )
    counts = {"utc": 0, "aware": 0, "naive": 0}
    aware_rows: list[tuple[int, str, int]] = []
    for row_id, ts, locked in rows:
        cls = _classify_timestamp(ts)
        counts[cls] += 1
        if cls == "aware":
            aware_rows.append((row_id, ts, locked))
    present = [k for k, v in counts.items() if v > 0]
    if len(present) <= 1 and not aware_rows:
        return []
    issue = {"type": "timestamp_format_drift", **counts}

    # The repair set is built whether or not this run repairs (the `repairable`
    # contract): the verdict must not depend on how the check was called.
    #
    # bug-212: `locked` is selected and filtered HERE rather than left to the
    # UPDATE's own `AND locked = 0`. Deferring it is what made the old
    # `normalized` count attempts instead of writes — it incremented on
    # `canon != ts` while the statement silently wrote nothing, so a run that
    # changed no row reported that it had normalised 902 of them, and any
    # severity resting on that number inherited the lie.
    repairs: list[tuple[int, str]] = []
    locked_aware = 0
    for row_id, ts, locked in aware_rows:
        try:
            parsed = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        canon = parsed.astimezone(datetime.timezone.utc).isoformat()
        if canon == ts:
            continue
        if locked:
            locked_aware += 1  # bug-098: the fixer may not touch it
            continue
        repairs.append((row_id, canon))
    issue["repairable"] = len(repairs)
    if fix and repairs:
        normalized = 0
        for row_id, canon in repairs:
            cur = await db.execute(
                "UPDATE memories SET timestamp = ? WHERE id = ? AND locked = 0",
                (canon, row_id),
            )
            # Counted from the write, not from the intent: the predicate is
            # re-evaluated by the statement and a row locked since the scan above
            # must not be counted as normalised.
            if getattr(cur, "rowcount", 0) == 1:
                normalized += 1
        issue["normalized"] = normalized
    if locked_aware:
        # So aware = normalisable + locked reconciles instead of reading as
        # "found N aware rows, normalised fewer, no reason given".
        issue["unfixable_locked"] = locked_aware
    if counts["naive"]:
        issue["unfixable_naive"] = counts["naive"]
    return [issue]


async def check_episode_timestamp_format_drift(db, agent_id: str, fix: bool) -> list[dict]:
    """Format drift in ``episodes.start_time`` / ``end_time`` (bug-245 residual).

    ``check_timestamp_format_drift`` reads ``memories`` only, while the episode
    columns hold the same mix of conventions: a recorded value is caller
    ISO-8601 with an offset, and the pre-2.5.5 ``missing_episode_start_time``
    backfill copied SQLite-native naive ``created_at`` verbatim — ``' ' < 'T'``,
    so every such row sorts before every recorded one regardless of date.

    Fix policy mirrors the memories check (aware → UTC, lossless) with one
    episode-specific addition: a naive value that verbatim-equals the row's
    ``created_at`` is the old backfill's copy, and ``created_at`` is
    ``datetime('now')`` — UTC by schema. Re-encoding it to the canonical aware
    form (the same string the bug-245 backfill now writes) changes the
    encoding, not the instant. Any other naive value stays untouched: its
    intended zone is unknowable, so rewriting it would fabricate data.
    """
    iso = isolation_where(agent_id=agent_id or None)
    rows = await db.execute_fetchall(
        f"""SELECT id, start_time, end_time, created_at FROM episodes
            WHERE (COALESCE(start_time, '') != '' OR COALESCE(end_time, '') != ''){iso.and_clause}""",
        iso.params,
    )
    # Counts are per value, not per row — a row contributes each non-empty column.
    counts = {"utc": 0, "aware": 0, "naive": 0}
    repairs: list[tuple[int, str, str]] = []  # (row_id, column, canonical)
    unfixable_naive = 0
    for row_id, start_time, end_time, created_at in rows:
        for column, value in (("start_time", start_time), ("end_time", end_time)):
            if not value:
                continue
            cls = _classify_timestamp(value)
            counts[cls] += 1
            if cls == "aware":
                try:
                    parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError:
                    continue
                canon = parsed.astimezone(datetime.timezone.utc).isoformat()
                if canon != value:
                    repairs.append((row_id, column, canon))
            elif cls == "naive":
                if value == created_at:
                    # The verbatim-copy fingerprint: same instant, canonical form
                    # (identical to strftime('%Y-%m-%dT%H:%M:%S+00:00', created_at)).
                    repairs.append((row_id, column, value.replace(" ", "T", 1) + "+00:00"))
                else:
                    unfixable_naive += 1
    present = [k for k, v in counts.items() if v > 0]
    if len(present) <= 1 and not repairs:
        return []
    issue = {"type": "episode_timestamp_format_drift", **counts}
    # `episodes` has no locked column, so unlike the memories check the repair
    # set is not reduced by a locked guard — every repair found is reachable.
    issue["repairable"] = len(repairs)
    if fix and repairs:
        normalized = 0
        for row_id, column, canon in repairs:
            # `column` comes from the literal tuple above, never from data.
            cur = await db.execute(
                f"UPDATE episodes SET {column} = ? WHERE id = ?", (canon, row_id)
            )
            if getattr(cur, "rowcount", 0) == 1:
                normalized += 1
        issue["normalized"] = normalized
    if unfixable_naive:
        issue["unfixable_naive"] = unfixable_naive
        issue["hint"] = (
            "naive values that do not equal created_at were recorded by a caller "
            "in an unknowable zone; only the created_at-verbatim backfill copies "
            "are re-encoded"
        )
    return [issue]


async def check_stale_pending_tasks(db, agent_id: str, fix: bool) -> list[dict]:
    # bug-031: pending_memory_tasks is per-agent (agent_id NOT NULL), so the
    # count/DELETE MUST be agent-scoped like every sibling check. Without the
    # predicate, check_health(agent_id='A', fix=true) deletes EVERY agent's
    # >1h-old un-drained store tasks — silent cross-agent data loss (the bug-007
    # scope-leak class). An empty agent_id (CLI global sweep) yields no clause and
    # keeps the corpus-wide behavior.
    iso = isolation_where(agent_id=agent_id or None)
    stale = (
        await db.execute_fetchall(
            f"SELECT COUNT(*) FROM pending_memory_tasks WHERE created_at < datetime('now', '-1 hour'){iso.and_clause}",
            iso.params,
        )
    )[0][0]
    if stale == 0:
        return []
    if fix:
        await db.execute(
            f"DELETE FROM pending_memory_tasks WHERE created_at < datetime('now', '-1 hour'){iso.and_clause}",
            iso.params,
        )
    # pending_memory_tasks has no `locked` column — these are un-drained queue
    # entries, not authored memories, so the DELETE reaches every row it finds
    # and the finding and the repair are the same set.
    return [{"type": "stale_pending_tasks", "count": stale, "repairable": stale}]


async def check_missing_profile(db, agent_id: str, fix: bool) -> list[dict]:
    # bug-055: scope to the requested agent like every sibling check. Without the
    # predicate this corpus-wide LEFT JOIN reports OTHER agents' missing profiles
    # into the requested agent's result (the bug-031/bug-007 scope-leak class), so
    # check_health(agent_id='A') falsely reports healthy=False and injects an issue
    # about an unrelated agent B. Empty agent_id (CLI global sweep) yields no clause
    # and keeps the corpus-wide behavior.
    iso = isolation_where(agent_id=agent_id or None, alias="m")
    missing = await db.execute_fetchall(
        f"""SELECT DISTINCT m.agent_id FROM memories m
           LEFT JOIN profiles p ON m.agent_id = p.agent_id
           WHERE p.id IS NULL{iso.and_clause}""",
        iso.params,
    )
    if not missing:
        return []
    agents = [r[0] for r in missing]
    return [{"type": "missing_profile", "count": len(agents), "agents": agents}]


# bug-236: ONE predicate string for the count, the repairable count and the
# DELETE. Unparenthesised in the count, AND bound tighter than OR, so it parsed
# as `TRIM(content) = '' OR (content IS NULL AND agent_id = ?)` — and content is
# TEXT NOT NULL, so the isolation predicate was inert and the count went
# corpus-wide while the repair stayed agent-scoped. A scoped fix run could then
# never drive its own finding to zero (the bug-031/bug-055 scope-leak class).
_EMPTY_CONTENT_WHERE = "(TRIM(content) = '' OR content IS NULL)"


async def check_empty_content(db, agent_id: str, fix: bool) -> list[dict]:
    iso = isolation_where(agent_id=agent_id or None)
    empty = (
        await db.execute_fetchall(
            f"SELECT COUNT(*) FROM memories WHERE {_EMPTY_CONTENT_WHERE}{iso.and_clause}",
            iso.params,
        )
    )[0][0]
    if empty == 0:
        return []
    # Before the DELETE, with the DELETE's own locked = 0 guard.
    repairable = (
        await db.execute_fetchall(
            f"""SELECT COUNT(*) FROM memories
                WHERE {_EMPTY_CONTENT_WHERE}
                AND locked = 0{iso.and_clause}""",
            iso.params,
        )
    )[0][0]
    if fix:
        await db.execute(
            f"DELETE FROM memories WHERE {_EMPTY_CONTENT_WHERE} AND locked = 0{iso.and_clause}",
            iso.params,
        )
    return [{"type": "empty_content", "count": empty, "repairable": repairable}]


def invalid_source_type_where(canonical_types: str) -> str:
    """The predicate for "this source carries a type we do not recognise".

    Written once because it has two callers — the COUNT that reports the finding
    and the SELECT the fixer repairs from — and they must agree. A row the check
    counts but the fixer will not select is a finding that never clears; a row
    the fixer rewrites but the check never counted is a silent mutation. bug-195
    is the same class of defect (see also bug-198's shared-guard note).

    bug-187: the anonymous shapes are excluded. ``store`` accepts an omitted or
    null ``source`` and records it as ``{}`` — the write path's own normalisation
    (memory_handlers) and a documented, supported way to say "producer unknown".
    Counting it as an invalid type made the check ``warn``, which made the single
    health ``status`` verdict ``degraded``, permanently: ``normalize_source``
    correctly refuses to invent a discriminator for ``{}``, so ``fix=true`` could
    never clear what the check kept reporting. A DB whose only sin was storing
    memories without attribution read as unhealthy forever.

    A stored JSON ``null`` is folded in for the same reason: the write seam
    treats null and ``{}`` as one anonymous shape, so a legacy row that predates
    that normalisation carries the same information and is equally unrepairable.

    Excluded here means *not a type defect*. An object that has keys but no
    recognised ``$.type`` (``{"id":"x"}``, ``{"type":"assistant"}``) is still
    counted: something claimed a producer and got the contract wrong. The
    anonymous shapes are reported by ``check_anonymous_source`` instead, which
    2.5.4 widened to include them — until then this docstring said "not
    invisible" while ``{}`` appeared on no health surface at all.
    """
    return f"""json_valid(source)
                    AND (json_extract(source, '$.type') NOT IN {canonical_types}
                    OR json_extract(source, '$.type') IS NULL)
                    AND json_type(source) IS NOT 'null'
                    AND NOT (json_type(source) = 'object'
                             AND (SELECT COUNT(*) FROM json_each(memories.source)) = 0)"""


async def check_invalid_source_type(db, agent_id: str, fix: bool) -> list[dict]:
    """Detect and (optionally) canonicalise legacy source shapes (2.5.2).

    Historical fix path blanket-overwrote every offending row with an anonymous
    ``{"type":"User","id":"","name":""}`` sentinel — a lossy repair that
    destroyed attribution wholesale. The mapping-based fix walks known legacy
    shapes (see ``normalize_source`` for the exhaustive table) and updates only
    rows we can rewrite without fabricating a discriminator. Rows we don't
    recognise stay untouched so the finding remains visible on the next run.

    2.5.4 — severity follows what a fix can actually do. bug-187 closed one
    instance of "a warn ``fix=true`` cannot clear pins ``status=degraded``
    forever"; the class stayed open, because a schema-conformant ``source``
    still reaches it. ``{"id":"discord:1","name":"bob"}`` has no ``type`` and
    the store schema has no ``required``, so it is accepted, counted here, and
    refused by ``normalize_source`` — measured on the production corpus as 495
    rows, 443 of them the bare string ``"claude-code"`` from a producer that
    stopped writing in 2026-07. No number of fix runs converges.

    Deciding it by shape (bare string vs dict, known vocabulary vs not) would
    put a second copy of the mapping rule in this file, next to the one in
    ``normalize_source``, and the two would drift. So the check asks the mapper
    itself and reports what that answer means:

    - at least one offending row a fix run would rewrite -> ``warn``. There is
      an action, and the operator can take it.
    - none -> ``info`` plus ``needs_human_review``. Still counted, still listed,
      no longer holding the verdict down. Deciding that ``"claude-code"`` means
      ``{"type":"Agent","id":"claude-code"}`` is a migration someone must
      authorise; a monthly maintenance run is not that someone.

    Classification is capped (``INVALID_SOURCE_CLASSIFY_CAP``) like the
    near-duplicate scan. Past the cap the sample is incomplete, so the check
    stays at ``warn`` — an unknown is not evidence that nothing can be done.

    bug-210: the cap bounds the REPAIR too — the fix loop walks only the rows
    classification saw, so one run rewrites at most the cap. The cap itself is
    sound defence (it bounds the JSON parsing a health call performs); the
    defect was the response, which read as convergence. A fix run therefore
    reports ``remaining`` — a fresh post-repair count of unlocked offenders —
    and, when capped, a hint saying to run fix again until it stops decreasing.
    """
    iso = isolation_where(agent_id=agent_id or None)
    # bug-144: json_extract raises OperationalError 'malformed JSON' (not NULL) on
    # any non-JSON source value, so a single malformed row aborts a whole-agent
    # COUNT and the outer except then reports the entire agent clean — a silent
    # false negative that also blocks this fixer. The json_valid(source) guard
    # excludes malformed rows (they are check_invalid_json's responsibility) so
    # the check keeps working for the rest of the corpus. It MUST come first:
    # SQLite short-circuits AND left-to-right, so json_valid(source)=0 stops the
    # row before json_extract is ever evaluated (a guard placed last still
    # throws). The outer except stays as a last-resort backstop.
    # 2.5.2b1 (b1-2): the enum comes from utils.CANONICAL_SOURCE_TYPES, the same
    # tuple the store JSON Schema publishes and normalize_source enforces at the
    # write seam. It used to be an inline literal here, which made checks.py the
    # de-facto (and unpublished) home of the contract.
    canonical_types = canonical_source_types_sql()
    try:
        bad = (
            await db.execute_fetchall(
                f"""SELECT COUNT(*) FROM memories
                    WHERE {invalid_source_type_where(canonical_types)}{iso.and_clause}""",
                iso.params,
            )
        )[0][0]
    except Exception:
        return []
    if bad == 0:
        return []
    issue: dict = {"type": "invalid_source_type", "count": bad}

    # locked = 0 mirrors every sibling fixer (bug-098 invariant), and the same
    # rows are read whether or not this run repairs them: the severity below
    # depends on what a fix WOULD do, so a check that only looked when fixing
    # would report a different verdict on alternate calls over identical data.
    rows = await db.execute_fetchall(
        f"""SELECT id, source FROM memories
            WHERE {invalid_source_type_where(canonical_types)}
            AND locked = 0{iso.and_clause}
            LIMIT ?""",
        (*iso.params, INVALID_SOURCE_CLASSIFY_CAP),
    )
    repairs: list[tuple[int, dict]] = []
    unmapped = 0
    for row_id, raw in rows:
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            # invalid_json handles the surrounding case; leave the row
            # so that check surfaces it, and don't count it as mapped.
            unmapped += 1
            continue
        new_source, was_mapped = normalize_source(parsed)
        if not was_mapped:
            unmapped += 1
            continue
        repairs.append((row_id, new_source))

    if fix:
        # Per-row rather than one UPDATE because the mapping is value-dependent
        # — the shape a row lands on is a function of its current source, not a
        # single canonical sentinel.
        for row_id, new_source in repairs:
            await db.execute(
                "UPDATE memories SET source = ? WHERE id = ? AND locked = 0",
                (json.dumps(new_source), row_id),
            )
        issue["mapped"] = len(repairs)
        issue["unmapped"] = unmapped
        # bug-139: `count` spans every offending row, but the repair only
        # touches locked = 0 rows (bug-098 invariant). Surface the remainder so
        # mapped + unmapped + locked reconciles with count instead of reading
        # as "found N, processed 0" when the offenders are locked.
        #
        # Counted, not derived as `bad - len(rows)`: subtraction made the
        # reconciliation an identity that held for any predicate, so the test
        # asserting it could not fail (a C5 mutant produced locked = -2 and the
        # sum still matched). An independent COUNT is also the only form that
        # survives the classification cap.
        issue["locked"] = (
            await db.execute_fetchall(
                f"""SELECT COUNT(*) FROM memories
                    WHERE {invalid_source_type_where(canonical_types)}
                    AND locked = 1{iso.and_clause}""",
                iso.params,
            )
        )[0][0]
        # bug-210: the repair loop above is bounded by the classification cap, so
        # a single fix run on a 5000-row backlog stops at 1000 and its response —
        # "mapped: 1000" — reads as convergence. This is the number that says
        # otherwise: unlocked offenders still standing AFTER this run's repairs.
        # Recounted, not derived (the bug-139 rule two comments up): a fresh
        # COUNT is the only form that is true whatever the cap, the mapping, or
        # a repair that failed to take. On an uncapped run it equals `unmapped`
        # — rows more runs will never fix; past the cap it exceeds it — rows
        # another run WILL classify.
        issue["remaining"] = (
            await db.execute_fetchall(
                f"""SELECT COUNT(*) FROM memories
                    WHERE {invalid_source_type_where(canonical_types)}
                    AND locked = 0{iso.and_clause}""",
                iso.params,
            )
        )[0][0]

    classified_all = len(rows) < INVALID_SOURCE_CLASSIFY_CAP
    if not classified_all:
        issue["classified"] = len(rows)
    # 2.5.5: this check's local de-escalation became the registry-wide
    # `repairable` contract, so it now only reports what a fix would write and
    # run_health_checks owns the verdict. None past the classification cap: the
    # sample is incomplete, and an unknown is not evidence that nothing can be
    # done (the reason the local rule stayed at warn there too).
    issue["repairable"] = len(repairs) if classified_all else None
    if fix and not classified_all:
        # bug-210: past the cap, the one wrong reading of this response is "fix
        # ran, therefore done". Say the opposite in words next to the number.
        issue["hint"] = (
            f"repair is bounded by the classification cap "
            f"({INVALID_SOURCE_CLASSIFY_CAP} rows per run); `remaining` counts "
            "the unlocked offenders still standing — run fix again until it "
            "stops decreasing"
        )
    if not repairs and classified_all:
        # The generic dispatcher hint is true but says less than this one, and
        # the policy only setdefaults, so the specific text wins.
        issue["hint"] = (
            "No offending row can be canonicalised without inventing a producer "
            "discriminator (see normalize_source). Repair is a reviewed migration, "
            "not a maintenance action"
            + (
                "; every offending row is locked, so unlock them first"
                if not rows and bad
                else ""
            )
        )
    return [issue]


async def check_anonymous_source(db, agent_id: str, fix: bool) -> list[dict]:
    """Memories with no producer attribution (info: a gap, not a defect).

    Two shapes, reported together because they mean the same thing and split
    because only one of them can be repaired:

    - the ``{"type":"User","id":"","name":""}`` sentinel, whose name deep_check
      can sometimes recover from a ``[name] `` content prefix;
    - the anonymous ``{}`` (and its legacy JSON ``null`` twin), which is what
      ``store`` records for an omitted source.

    The second was reported by nothing at all until 2.5.4. bug-187 removed it
    from ``invalid_source_type`` — correctly, since a documented way to say
    "producer unknown" is not a contract violation — and the removal left it in
    no other check, while ``invalid_source_type_where``'s docstring said the
    exclusion did not make it invisible. Measured: a corpus of three ``{}`` rows
    produced issue types ``['missing_profile', 'null_embedding']`` and nothing
    else. It is counted here rather than given its own check because an operator
    asking "how much of this corpus has no attribution?" wants one number.
    """
    iso = isolation_where(agent_id=agent_id or None)
    # bug-144: same malformed-JSON hazard as check_invalid_source_type — a single
    # non-JSON source row makes json_extract raise and the outer except reports
    # the whole agent clean. json_valid(source) MUST lead so SQLite's
    # left-to-right AND short-circuit skips json_extract on malformed rows (they
    # stay check_invalid_json's responsibility); the outer except is a backstop.
    try:
        anon = (
            await db.execute_fetchall(
                f"""SELECT COUNT(*) FROM memories
                    WHERE json_valid(source)
                    AND json_extract(source, '$.type') = 'User'
                    AND json_extract(source, '$.id') = ''
                    AND json_extract(source, '$.name') = ''{iso.and_clause}""",
                iso.params,
            )
        )[0][0]
        unattributed = (
            await db.execute_fetchall(
                f"""SELECT COUNT(*) FROM memories
                    WHERE json_valid(source)
                    AND (json_type(source) IS 'null'
                         OR (json_type(source) = 'object'
                             AND (SELECT COUNT(*) FROM json_each(memories.source)) = 0))
                    {iso.and_clause}""",
                iso.params,
            )
        )[0][0]
    except Exception:
        return []
    if anon == 0 and unattributed == 0:
        return []
    issue: dict = {"type": "anonymous_source", "count": anon + unattributed}
    if unattributed:
        # Split out because the hint below does not apply to them: there is no
        # name to recover into a shape that never claimed one.
        issue["unattributed"] = unattributed
    if anon:
        issue["hint"] = "Use deep_check with fix=true to recover names from content"
    return [issue]


async def check_operating_context_parse(db, agent_id: str = "", fix: bool = False) -> list[dict]:
    """Sidecar present but unusable (v2.5.1 §8). The feature degrades to dormant
    rather than failing the boot, so this finding is the only surface where a
    config typo becomes visible. fix is a human editing the file — never automatic."""
    state = operating_context.load_state()
    if state["present"] and state["parse_error"]:
        return [
            {
                "type": "operating_context_parse_error",
                "path": state["path"],
                "detail": state["parse_error"],
                "hint": "operating context is dormant until the sidecar parses; edit the file",
            }
        ]
    return []


async def check_operating_context_size(db, agent_id: str = "", fix: bool = False) -> list[dict]:
    """Instructions summary over the fixed-cost budget (v2.5.1 §4/§8). The summary
    is injected into every client session at initialize — treat it like CLAUDE.md
    budget, not like a doc."""
    state = operating_context.load_state()
    if state["summary_len"] > operating_context.SUMMARY_WARN_CHARS:
        return [
            {
                "type": "operating_context_summary_oversized",
                "path": state["path"],
                "summary_len": state["summary_len"],
                "budget": operating_context.SUMMARY_WARN_CHARS,
                "hint": "move detail into [[doctrine]] sections (served via get_operating_context)",
            }
        ]
    return []


async def check_vector_fallback_config(db, agent_id: str = "", fix: bool = False) -> list[dict]:
    """bug-180: remote vector search with no local BLOBs has no fallback, and
    nothing said so.

    ``_search_vector`` treats the local cosine scan as the safety net for a remote
    ``/search`` outage: the remote answer of ``None`` means "not answered", and the
    scan runs. That scan reads ``embedding IS NOT NULL``, so under remote mode with
    ``CPERSONA_STORE_BLOB=false`` it matches nothing — the net is empty by
    construction. A ``/search`` fault while ``/embed`` still answers therefore
    returns zero vector hits, and the degraded advisory stays silent because it
    observes the embed boundary only (``health`` scopes itself there by design;
    widening that contract is a separate decision, not this check's job).

    Report-only, ``info``: the configuration is legitimate — it trades local disk
    for the remote index. What was missing is the operator being able to see the
    trade from the maintenance surface instead of from a recall that quietly lost
    its vector arm. The counts are agent-scoped like every other data figure here
    (the bug-062 rule), so this never discloses another agent's corpus size.
    """
    if _blobs_are_stored():
        return []
    iso = isolation_where(agent_id=agent_id or None)
    total = (
        await db.execute_fetchall(f"SELECT COUNT(*) FROM memories WHERE 1=1{iso.and_clause}", iso.params)
    )[0][0]
    with_blob = (
        await db.execute_fetchall(
            f"SELECT COUNT(*) FROM memories WHERE embedding IS NOT NULL{iso.and_clause}", iso.params
        )
    )[0][0]
    return [
        {
            "type": "no_local_vector_fallback",
            "vector_search_mode": VECTOR_SEARCH_MODE,
            "store_blob": STORE_BLOB,
            "memories": total,
            "memories_with_local_embedding": with_blob,
            "hint": (
                "a remote /search outage leaves recall with FTS/keyword only; "
                "set CPERSONA_STORE_BLOB=true to arm the local scan"
            ),
        }
    ]


# Below this many embedded rows the contiguous index is not worth mentioning:
# its absence costs a scan that is already fast at that size, and an info line
# every operator has to learn to ignore is worse than silence.
INDEX_MATTERS_ROWS = 1000

# A tail this large relative to the indexed rows means the rebuild is overdue.
# The tail is read exactly on every query, so it is the design's own unit of
# staleness: a late rebuild never returns a wrong answer, it grows this number
# and the latency with it.
INDEX_TAIL_RATIO = 0.2


INDEX_TABLES = ("memories", "episodes")


async def check_vector_index(db, agent_id: str = "", fix: bool = False) -> list[dict]:
    """Report what the contiguous embedding index is doing, or why it is not.

    Every way that index can stop being used is silent by construction — that is
    what makes falling back safe, and it is also what makes the condition
    invisible. A file deleted, a file that no longer holds together, or a corpus
    whose embedding width moved: in all three the answers stay correct and only
    the latency changes, so nothing downstream has a reason to complain. Without
    a line here, an index that died a week ago reads as "somehow not faster".

    Report-only. Three states, deliberately not one:

    - **absent** — the ordinary state before the first build and after a
      deletion, and NOT a defect. Reported as ``info`` only once the corpus is
      large enough for the index to be worth having; below that, silence.
    - **unusable** — the file exists and does not hold together. ``warn``: reads
      are still correct, so this is degradation rather than a broken contract,
      but somebody has to hear about it.
    - **dimension drift** — the corpus no longer matches what the index was built
      for, which is what a model swap looks like from here. ``warn`` for the same
      reason.

    A fourth line reports a healthy index whose tail has grown past
    ``INDEX_TAIL_RATIO``, which is the one form of staleness this design admits.

    One index per table, and the same four states for each: the episode scan is
    served from its own file, and the two can be in different states (built
    nightly for one, never for the other). Every finding names its ``table``.
    """
    # Nested rather than module-level: the severity inventory
    # (tests/test_superauditor_findings.py) walks each ``check_*`` runner's own
    # body for the severities it stamps, and a helper outside it would hide them.
    async def one(table: str) -> list[dict]:
        from cpersona import vector_index

        iso = isolation_where(agent_id=agent_id or None)
        embedded = (
            await db.execute_fetchall(
                f"SELECT COUNT(*) FROM {table} WHERE embedding IS NOT NULL{iso.and_clause}",
                iso.params,
            )
        )[0][0]

        try:
            index = vector_index.cached_index(table)
        except vector_index.IndexUnusable as exc:
            return [
                {
                    "type": "vector_index_unusable",
                    "severity": "warn",
                    "table": table,
                    "detail": str(exc),
                    "hint": "delete the file; it is a derived artifact and is rebuilt, never repaired",
                }
            ]

        if index is None:
            if embedded < INDEX_MATTERS_ROWS:
                return []
            # Still ``info`` at any size, and deliberately so: no index is a state
            # the operator chose (or was never told about), not a fault, and the
            # scan it leaves recall on is correct. What changes with size is the
            # price, so the price is what the line carries -- in bytes read per
            # query rather than in a severity, because a warning about a
            # configuration the operator selected teaches operators to ignore
            # warnings. The scan reads at most the window, so a corpus beyond
            # ``MAX_MEMORIES`` pays for the window, not for the corpus.
            from cpersona.config import MAX_MEMORIES

            width_row = await db.execute_fetchall(
                f"SELECT length(embedding) FROM {table}"
                f" WHERE embedding IS NOT NULL{iso.and_clause} LIMIT 1",
                iso.params,
            )
            width = int(width_row[0][0]) if width_row and width_row[0][0] else 0
            scanned = min(embedded, MAX_MEMORIES)
            per_query = scanned * width
            return [
                {
                    "type": "vector_index_absent",
                    "table": table,
                    f"{table}_with_local_embedding": embedded,
                    "rows_scanned_per_query": scanned,
                    "embedding_bytes_read_per_query": per_query,
                    "hint": (
                        f"the local vector scan reads {table} embeddings from SQLite row by row: "
                        f"about {per_query / 1_000_000:.0f} MB per query at this size "
                        "(measured 604 ms per query at 100,000 rows of 768 dimensions on the "
                        "reference machine, against 77 ms with the index); "
                        "building the contiguous index removes that cost: "
                        f"python -m cpersona.vector_index --db <path> --table {table} build"
                    ),
                }
            ]

        # The widths the corpus actually holds. More than one means a model swap is
        # in flight: the builder declines while that is true, because the live scan
        # windows before it skips foreign-width rows and a single-width index cannot
        # reproduce that window.
        # Deliberately global, unlike the counts above: the builder declines while
        # ANY row in the table carries another width, so an agent-scoped question
        # would answer "no drift" about a corpus the builder is refusing. A width is
        # not corpus content, so this discloses nothing agent-scoped (the bug-062
        # rule is about sizes and contents).
        all_axes = isolation_where(agent_id=None)
        widths = {
            r[0]
            for r in await db.execute_fetchall(
                f"SELECT DISTINCT length(embedding) FROM {table}"
                f" WHERE embedding IS NOT NULL{all_axes.and_clause}",
                all_axes.params,
            )
        }
        if widths and widths != {index.dim * 4}:
            return [
                {
                    "type": "vector_index_dimension_drift",
                    "severity": "warn",
                    "table": table,
                    "index_dim": index.dim,
                    "corpus_widths_bytes": sorted(widths),
                    "hint": (
                        "the index is not being used; rebuild it once the corpus carries "
                        "one embedding width again"
                    ),
                }
            ]

        tail = (
            await db.execute_fetchall(
                f"SELECT COUNT(*) FROM {table} WHERE id > ? AND embedding IS NOT NULL{iso.and_clause}",
                (index.watermark, *iso.params),
            )
        )[0][0]
        if index.count and tail > index.count * INDEX_TAIL_RATIO:
            return [
                {
                    "type": "vector_index_tail_grown",
                    "table": table,
                    "indexed_rows": index.count,
                    "rows_past_watermark": tail,
                    "hint": "rebuild the index; a long tail is read exactly on every query",
                }
            ]
        return []

    issues: list[dict] = []
    for table in INDEX_TABLES:
        issues.extend(await one(table))
    return issues


class Check:
    """A registered health check: metadata + runner (see module docstring)."""

    __slots__ = ("name", "base_severity", "fix_capable", "runner")

    def __init__(self, name: str, base_severity: str, fix_capable: bool, runner):
        assert base_severity in SEVERITIES
        self.name = name
        self.base_severity = base_severity
        self.fix_capable = fix_capable
        self.runner = runner


HEALTH_CHECKS: list[Check] = [
    Check("memory_annotation", "info", True, check_memory_annotation),
    Check("discord_mention", "info", True, check_discord_mention),
    # bug-097: oversized_content runs BEFORE duplicate_content — its truncation
    # rewrites content and can mint fresh duplicate groups, which the dup check
    # must still see within the same fix pass (the bug-059 residual re-run
    # reports them, but ordering lets a single pass converge).
    Check("oversized_content", "warn", True, check_oversized_content),
    Check("duplicate_content", "warn", True, check_duplicate_content),
    Check("embedding_dimension", "critical", True, check_embedding_dimension),
    # info by default: "no backend configured" is a supported configuration, not a
    # defect. The runner stamps warn for the one state that is (bug-274).
    Check("embedding_backend", "info", False, check_embedding_backend),
    Check("null_embedding", "warn", True, check_null_embedding),
    Check("null_episode_embedding", "warn", True, check_null_episode_embedding),
    Check("fts_integrity", "warn", True, check_fts_integrity),
    Check("schema_version", "critical", False, check_schema_version),
    # bug-145: dedup_msg_id_index runs BEFORE schema_objects — it collapses the
    # msg_id collisions that make the UNIQUE index CREATE fail, so once it
    # (re)creates the index a single fix pass leaves schema_objects nothing to
    # report (schema_objects' own fix only re-issues the identical, still-failing
    # CREATE). Same ordering doctrine as oversized_content before duplicate_content.
    Check("dedup_msg_id_index", "critical", True, check_dedup_msg_id_index),
    Check("schema_objects", "critical", True, check_schema_objects),
    Check("sqlite_integrity", "critical", False, check_sqlite_integrity),
    Check("axis_hygiene", "warn", False, check_axis_hygiene),
    Check("invalid_json", "warn", True, check_invalid_json),
    Check("invalid_timestamp", "warn", True, check_invalid_timestamp),
    # bug-208: info, not warn — since bug-213 the read paths score these rows by
    # created_at anyway; the fix materialises that fallback for direct readers.
    Check("missing_episode_start_time", "info", True, check_missing_episode_start_time),
    Check("timestamp_format_drift", "warn", True, check_timestamp_format_drift),
    # bug-245 residual: the episode columns hold the same drift class the check
    # above detects for memories, and nothing covered them.
    Check("episode_timestamp_format_drift", "warn", True, check_episode_timestamp_format_drift),
    Check("stale_pending_tasks", "warn", True, check_stale_pending_tasks),
    Check("missing_profile", "info", False, check_missing_profile),
    Check("empty_content", "warn", True, check_empty_content),
    Check("invalid_source_type", "warn", True, check_invalid_source_type),
    Check("oversized_profile", "warn", True, check_oversized_profile),
    Check("anonymous_source", "info", False, check_anonymous_source),
    Check("vector_fallback_config", "info", False, check_vector_fallback_config),
    Check("vector_index", "info", False, check_vector_index),
    Check("operating_context_parse", "warn", False, check_operating_context_parse),
    Check("operating_context_size", "info", False, check_operating_context_size),
]

HEALTH_CHECK_NAMES = [c.name for c in HEALTH_CHECKS]

_CHECKS_BY_NAME = {c.name: c for c in HEALTH_CHECKS}

# bug-254: the checks whose DETECTION is a whole-database read. fts_integrity
# runs the FTS5 'integrity-check' command over both indexes and sqlite_integrity
# runs PRAGMA quick_check over the whole file. Of these, only the REPORT-ONLY
# member (fix_capable=False) leaves the write seam in do_check_health — a scan
# that can never write has no business under the shared lock (the bug-072/083
# doctrine). A fix-capable member stays locked even though its scan is also
# O(database): its detection must be atomic with its repair, or corruption the
# fix run's own content rewrites introduce lands in a window where an earlier
# clean pre-scan already voted "no repair needed". See the dispatch there.
WHOLE_DB_SCAN_CHECKS = frozenset({"fts_integrity", "sqlite_integrity"})


def is_fix_capable(name: str) -> bool:
    """Whether the named check can write a repair (registry metadata lookup).

    An unknown name answers False: a check that does not exist cannot repair
    anything, and the caller that could pass one (do_check_health) has already
    rejected unknown names (bug-230).
    """
    check = _CHECKS_BY_NAME.get(name)
    return bool(check and check.fix_capable)


def merge_issues(*groups: list[dict]) -> list[dict]:
    """Merge issue lists produced by separate runs back into registry order.

    bug-254 split one health run across two seams (an unlocked scan and the
    locked remainder), and the response's ``issues`` list is the operator's
    reading order. Sorting by the registry index restores exactly the order a
    single run would have produced; the sort is stable, so several issues from
    the same check keep the order their runner emitted them in. An issue with
    an unrecognised ``check`` sorts last rather than being dropped.
    """
    merged: list[dict] = []
    for group in groups:
        merged.extend(group)
    order = {name: index for index, name in enumerate(HEALTH_CHECK_NAMES)}
    merged.sort(key=lambda issue: order.get(issue.get("check"), len(order)))
    return merged


# bug-083: embedding_dimension is cache-aware too — it consumes the pre-probed
# "expected_dim" instead of live-embedding under the write lock.
_EMBEDDING_CHECKS = {
    "null_embedding",
    "null_episode_embedding",
    "embedding_dimension",
    # Reads expected_dim to tell an unreachable backend from an unprobed run;
    # without the cache it cannot, and says so rather than assuming health.
    "embedding_backend",
}


async def run_health_checks(
    db, agent_id: str = "", fix: bool = False, checks: list | None = None, embedding_cache=None
) -> tuple[list[dict], dict]:
    """Run (a subset of) the registry; returns (issues, severity_summary).

    Every issue carries ``severity`` (runner override wins, else the registry
    default) and ``check`` (the registry name that produced it).

    ``embedding_cache`` (bug-072) carries embeddings pre-computed outside the write lock
    for the two null-embedding checks; None means those checks embed live (CLI path).
    """
    selected = set(checks) if checks else None
    issues: list[dict] = []
    summary = {"critical": 0, "warn": 0, "info": 0}
    for check in HEALTH_CHECKS:
        if selected is not None and check.name not in selected:
            continue
        try:
            if embedding_cache is not None and check.name in _EMBEDDING_CHECKS:
                found = await check.runner(db, agent_id, fix, embedding_cache=embedding_cache)
            else:
                found = await check.runner(db, agent_id, fix)
        except Exception as e:
            logger.warning("health check %s crashed: %s", check.name, e)
            found = [{"type": "check_crashed", "check_name": check.name, "detail": str(e), "severity": "warn"}]
        for issue in found:
            issue.setdefault("severity", check.base_severity)
            issue.setdefault("check", check.name)
            if check.fix_capable:
                _apply_repairable_policy(issue, check)
            summary[issue["severity"]] += 1
            issues.append(issue)
    return issues, summary


_UNREPAIRABLE_HINT = (
    "Nothing this check's fix can write: every offending row is out of its "
    "reach (locked, or a shape it refuses to rewrite). Repair is an operator "
    "decision, not a maintenance run"
)


def _apply_repairable_policy(issue: dict, check: "Check") -> None:
    """The one de-escalation rule (see module docstring). Policy only — the row
    counting belongs to the runner, which is the only layer that can see rows.

    Mutates ``issue`` in place. Runners keep their own ``hint`` when they have a
    better one to give than the generic text above.
    """
    if "repairable" not in issue:
        # Never silently exempt: no de-escalation, and say so where the operator
        # is already looking rather than only in a log nobody reads.
        issue["repairable_undeclared"] = True
        logger.warning(
            "health check %s is fix_capable but emitted an issue without "
            "'repairable' (type=%s); the de-escalation rule cannot apply",
            check.name,
            issue.get("type"),
        )
        return
    if issue["repairable"] != 0:
        # Non-zero, or None = undetermined. Only a determined zero de-escalates.
        return
    issue["needs_human_review"] = True
    issue.setdefault("hint", _UNREPAIRABLE_HINT)
    if issue["severity"] == "warn":
        issue["severity"] = "info"


def exit_code(summary: dict, strict: bool = False) -> int:
    """CI/CLI gate semantics: critical always gates (2); warn gates only under
    --strict (1); info never gates (0)."""
    if summary.get("critical"):
        return 2
    if strict and summary.get("warn"):
        return 1
    return 0


def health_status(summary: dict) -> str:
    """Three-level gate status derived from a severity summary.

    critical -> ``unhealthy``; otherwise warn -> ``degraded``; otherwise
    ``healthy``. Info counts are observations, not gate signals — the same
    stance ``exit_code`` takes when it returns 0 for an info-only summary — so
    an info-only DB reports ``status='healthy'`` with a non-empty ``issues``
    list. Since 2.5.2b1 this is check_health's only verdict (the ``healthy``
    boolean it used to sit beside said False for exactly that case, which is
    why it was dropped). Colocated with ``exit_code`` so the two gate mappings
    evolve together.
    """
    if summary.get("critical"):
        return "unhealthy"
    if summary.get("warn"):
        return "degraded"
    return "healthy"


# ---------------------------------------------------------------------------
# deep-check runners — heuristic (but still deterministic) per-agent analysis.
# Each returns the per-check result dict used in do_deep_check's response.
# ---------------------------------------------------------------------------


async def deep_anonymous_source(db, agent_id: str, fix: bool) -> dict:
    # bug-227: `json_valid(source)` leads the predicate, as it does in
    # check_invalid_source_type and check_anonymous_source (the bug-144 guard).
    # json_extract RAISES 'malformed JSON' on a legacy non-JSON source instead of
    # returning NULL, so one such row aborted the whole scan; do_deep_check then
    # recorded {"error": ...}, which the checkup finding filter prints as
    # nothing — an operator saw a clean deep report and no row was repaired.
    # bug-228: `locked` is selected here so the reported `fixed` counts writes.
    rows = await db.execute_fetchall(
        """SELECT id, content, locked FROM memories
           WHERE agent_id = ?
           AND json_valid(source)
           AND json_extract(source, '$.type') = 'User'
           AND json_extract(source, '$.id') = ''
           AND json_extract(source, '$.name') = ''""",
        (agent_id,),
    )
    recoverable = []
    unrecoverable = []
    locked_ids: set[int] = set()
    for row_id, content, locked in rows:
        match = _USERNAME_PREFIX_PATTERN.match(content)
        if match:
            recoverable.append({"id": row_id, "recovered_name": match.group(1)})
            if locked:
                locked_ids.add(row_id)  # bug-098: the fixer may not touch it
        else:
            unrecoverable.append({"id": row_id, "content_preview": content[:60]})
    fixed_count = 0
    if fix and recoverable:
        for item in recoverable:
            if item["id"] in locked_ids:
                continue
            new_source = json.dumps({"type": "User", "id": "", "name": item["recovered_name"]})
            cur = await db.execute(
                "UPDATE memories SET source = ? WHERE id = ? AND locked = 0",
                (new_source, item["id"]),
            )
            # bug-228: counted from the write, not from the intent — the scan
            # carries no `locked` filter but the UPDATE does, so `len(recoverable)`
            # reported locked rows as repaired while the statement no-opped
            # (the defect bug-212 removed from check_timestamp_format_drift).
            if getattr(cur, "rowcount", 0) == 1:
                fixed_count += 1
    result = {"recoverable": len(recoverable), "unrecoverable": len(unrecoverable)}
    if fix:
        result["fixed"] = fixed_count
    # So recoverable = fixed + locked reconciles instead of reading as "found N,
    # recovered fewer, no reason given" (bug-212's shape).
    unfixable_locked = len(recoverable) - fixed_count if fix else len(locked_ids)
    if unfixable_locked:
        result["unfixable_locked"] = unfixable_locked
    if recoverable:
        result["samples"] = recoverable[:5]
    if unrecoverable:
        result["unrecoverable_samples"] = unrecoverable[:5]
    return result


async def deep_short_content(db, agent_id: str, fix: bool) -> dict:
    rows = await db.execute_fetchall(
        "SELECT id, content FROM memories WHERE agent_id = ? AND LENGTH(TRIM(content)) <= ?",
        (agent_id, _SHORT_CONTENT_THRESHOLD),
    )
    fixed_count = 0
    if fix and rows:
        ids = [r[0] for r in rows]
        placeholders = ",".join("?" * len(ids))
        # Never delete locked rows (bug-015 / the bug-007 invariant): a memory the
        # user explicitly locked must survive maintenance even when it is short.
        # rowcount (not len(ids)) so the reported count excludes the survivors.
        cur = await db.execute(
            f"DELETE FROM memories WHERE id IN ({placeholders}) AND locked = 0", ids
        )
        fixed_count = cur.rowcount
    result = {"count": len(rows)}
    if fix:
        result["fixed"] = fixed_count
    if rows:
        result["samples"] = [{"id": r[0], "content": r[1]} for r in rows[:10]]
    return result


async def deep_stale_profile(db, agent_id: str, fix: bool) -> dict:
    rows = await db.execute_fetchall(
        """SELECT id, updated_at FROM profiles
           WHERE agent_id = ? AND user_id = ''
           AND updated_at < datetime('now', ?)""",
        (agent_id, f"-{_STALE_PROFILE_DAYS} days"),
    )
    result = {"count": len(rows), "threshold_days": _STALE_PROFILE_DAYS}
    if rows:
        result["last_updated"] = rows[0][1]
    return result


async def deep_orphaned_episodes(db, agent_id: str, fix: bool) -> dict:
    # bug-116: memories.timestamp is caller-supplied ISO-8601 (usually offset-aware,
    # 'T' separator) while episodes.start/end_time may be naive datetime('now') format
    # (space separator) — raw string comparison across the two formats is lexicographic
    # garbage ('T' > ' '), yielding false-positive orphans. SQLite's datetime()
    # normalises both (offset-aware values are converted to UTC; naive values are
    # already UTC by the bug-114 invariant), making the range test format-independent.
    rows = await db.execute_fetchall(
        """SELECT e.id, e.summary, e.start_time, e.end_time FROM episodes e
           WHERE e.agent_id = ?
           AND e.start_time IS NOT NULL AND e.end_time IS NOT NULL
           AND NOT EXISTS (
               SELECT 1 FROM memories m
               WHERE m.agent_id = e.agent_id
               AND datetime(m.timestamp) >= datetime(e.start_time)
               AND datetime(m.timestamp) <= datetime(e.end_time)
           )""",
        (agent_id,),
    )
    result = {"count": len(rows)}
    if rows:
        result["samples"] = [
            {"id": r[0], "summary": r[1][:80], "start": r[2], "end": r[3]} for r in rows[:5]
        ]
    return result


async def deep_calibration_staleness(db, agent_id: str, fix: bool) -> dict:
    """Report when threshold calibration is absent or old (report-only).

    Deterministic signals only: sidecar missing while embeddings are active on
    a non-trivial corpus, a ``scoring_version`` the runtime no longer produces, or
    ``calibrated_at`` older than ``CALIBRATION_STALE_DAYS``.
    (Corpus-growth-since-calibration would need a corpus-size field in the
    sidecar — a v2.4.38+ candidate.)

    bug-184 (2.5.2): the scoring mismatch is checked BEFORE the age test because a
    sidecar calibrated yesterday on the previous scoring function is stale despite a
    fresh ``calibrated_at`` — age answers a different question. ``ensure_calibrated_on_startup``
    normally repairs this at boot, so what this reports is the case where it could NOT:
    a startup calibration that returned ok=False (too few embedded rows, an embedding
    backend that was down at boot), or recalibration disabled by config (AUTO_CALIBRATE
    and CALIBRATE_ON_MODEL_CHANGE both off — the guard then declines to restore and
    leaves the gate uncalibrated). Either way the condition is visible to an operator
    instead of living only in one boot-time log line.
    """
    from cpersona.admin_handlers import _load_calibration_state

    if not vector._embedding_client:
        return {"status": "not_applicable", "reason": "no embedding client configured"}
    embedded = (
        await db.execute_fetchall(
            "SELECT COUNT(*) FROM memories WHERE agent_id = ? AND embedding IS NOT NULL",
            (agent_id,),
        )
    )[0][0]
    state = _load_calibration_state()
    if state is None:
        if embedded >= 50:
            return {
                "status": "never_calibrated",
                "embedded_rows": embedded,
                "hint": "run calibrate_threshold",
            }
        return {"status": "ok", "reason": f"corpus too small to matter ({embedded} embedded rows)"}
    sidecar_scoring_version = state.get("scoring_version")
    if sidecar_scoring_version != SCORING_VERSION:
        return {
            "status": "stale_scoring_version",
            "sidecar_scoring_version": sidecar_scoring_version,
            "runtime_scoring_version": SCORING_VERSION,
            # Deliberately NOT "run calibrate_threshold": this status fires precisely when
            # nothing was restored, and a single-agent calibration then rewrites the whole
            # sidecar from empty in-memory state, dropping every other agent's thresholds
            # and gates (bug-189). A restart takes the startup guard's path, which
            # recalibrates every agent in one pass.
            "hint": "restart the server (it recalibrates automatically on boot)",
        }
    calibrated_at = state.get("calibrated_at")
    try:
        age_days = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.datetime.fromisoformat(calibrated_at)
        ).days
    except (TypeError, ValueError):
        return {"status": "unknown", "reason": "sidecar has no parseable calibrated_at"}
    if age_days > CALIBRATION_STALE_DAYS:
        return {
            "status": "stale",
            "age_days": age_days,
            "threshold_days": CALIBRATION_STALE_DAYS,
            "hint": "run calibrate_threshold",
        }
    return {"status": "ok", "age_days": age_days}


async def deep_near_duplicate(db, agent_id: str, fix: bool) -> dict:
    """Embedding-space near-duplicate pairs (cosine > 0.97) — merge candidates.

    Report-only by design: whether two nearly identical memories should merge
    (and which survives) is the calling agent's judgment, applied through
    merge_memories / delete_memory. Exact duplicates are excluded (they belong
    to duplicate_content / the v12 UNIQUE index). Capped at the most recent
    NEAR_DUPLICATE_ROW_CAP embedded rows to bound the O(n^2) comparison.
    """
    import numpy as np

    rows = await db.execute_fetchall(
        """SELECT id, content, embedding FROM memories
           WHERE agent_id = ? AND embedding IS NOT NULL
           ORDER BY id DESC LIMIT ?""",
        (agent_id, NEAR_DUPLICATE_ROW_CAP),
    )
    if len(rows) < 2:
        return {"pairs": 0, "rows_scanned": len(rows)}
    dims = {len(r[2]) for r in rows}
    if len(dims) != 1:
        return {"pairs": 0, "rows_scanned": len(rows), "skipped": "mixed embedding dimensions"}
    matrix = np.frombuffer(b"".join(r[2] for r in rows), dtype=np.float32).reshape(len(rows), -1)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    unit = matrix / norms
    sims = unit @ unit.T
    pairs = []
    n = len(rows)
    idx_a, idx_b = np.where(np.triu(sims, k=1) > NEAR_DUPLICATE_COSINE)
    for a, b in zip(idx_a.tolist(), idx_b.tolist()):
        if rows[a][1] == rows[b][1]:
            continue  # exact duplicate — duplicate_content's jurisdiction
        pairs.append(
            {
                "id_a": rows[a][0],
                "id_b": rows[b][0],
                "cosine": round(float(sims[a, b]), 4),
                "preview_a": rows[a][1][:60],
                "preview_b": rows[b][1][:60],
            }
        )
    pairs.sort(key=lambda p: -p["cosine"])
    result = {"pairs": len(pairs), "rows_scanned": n, "threshold": NEAR_DUPLICATE_COSINE}
    if pairs:
        result["samples"] = pairs[:20]
        result["hint"] = "review with merge_memories / delete_memory (agent judgment)"
    return result


DEEP_CHECKS: dict = {
    "anonymous_source": deep_anonymous_source,
    "short_content": deep_short_content,
    "stale_profile": deep_stale_profile,
    "orphaned_episodes": deep_orphaned_episodes,
    "calibration_staleness": deep_calibration_staleness,
    "near_duplicate": deep_near_duplicate,
}

DEEP_CHECK_NAMES = list(DEEP_CHECKS)

# Which deep checks actually act on ``fix=True``. The rest are report-only: they
# accept the flag (the runner signature is uniform) and ignore it, so a caller
# passing fix=True gets a silent no-op. The deep_check tool description is
# rendered from this set — audit C26 found the description naming only two of
# the four report-only checks, which is exactly the drift a hand-maintained
# sentence produces. tests/test_structural_gates.py asserts this set against the
# AST, so a check that gains or loses a repair path cannot leave it stale.
DEEP_FIX_CAPABLE = frozenset({"anonymous_source", "short_content"})
DEEP_REPORT_ONLY = tuple(n for n in DEEP_CHECK_NAMES if n not in DEEP_FIX_CAPABLE)
