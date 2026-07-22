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
"""

import datetime
import json
import logging
import re
import sqlite3

from cpersona import health, operating_context, vector
from cpersona.isolation import isolation_where
from cpersona.config import FTS_ENABLED, MAX_CONTENT_LENGTH
from cpersona.database import SCHEMA_VERSION
from cpersona.utils import _MEMORY_ANNOTATION_PATTERN, _MENTION_PATTERN, normalize_source

logger = logging.getLogger(__name__)

SEVERITIES = ("info", "warn", "critical")

# Deterministic escalation thresholds (see module docstring).
NULL_EMBEDDING_CRITICAL_RATIO = 0.5
FTS_DESYNC_CRITICAL_RATIO = 0.05
NEAR_DUPLICATE_COSINE = 0.97
NEAR_DUPLICATE_ROW_CAP = 1000
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


async def check_memory_annotation(db, agent_id: str, fix: bool) -> list[dict]:
    iso = isolation_where(agent_id=agent_id or None)
    rows = await db.execute_fetchall(
        f"SELECT id, content FROM memories WHERE content LIKE '%[Memory from%'{iso.and_clause}", iso.params
    )
    if not rows:
        return []
    if fix:
        for row_id, content in rows:
            cleaned = _MEMORY_ANNOTATION_PATTERN.sub("", content).strip()
            await _rewrite_or_delete_on_collision(db, row_id, cleaned)
    return [{"type": "memory_annotation", "count": len(rows)}]


async def check_discord_mention(db, agent_id: str, fix: bool) -> list[dict]:
    iso = isolation_where(agent_id=agent_id or None)
    rows = await db.execute_fetchall(
        f"SELECT id, content FROM memories WHERE content LIKE '%<@%'{iso.and_clause}", iso.params
    )
    if not rows:
        return []
    if fix:
        for row_id, content in rows:
            cleaned = _MENTION_PATTERN.sub("", content).strip()
            await _rewrite_or_delete_on_collision(db, row_id, cleaned)
    return [{"type": "discord_mention", "count": len(rows)}]


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
    if fix:
        # Agent-scoped, locked-safe (bug-007): remove only unlocked
        # non-survivors within scope. The survivor grouping MUST match the
        # detection grouping above (bug-014).
        # bug-128: prefer the channel='' shared row so cross-channel dedup never
        # deletes the broadest-visibility copy; otherwise keep the MIN(id).
        await db.execute(
            f"""DELETE FROM memories
                WHERE locked = 0
                  AND id NOT IN (
                      SELECT COALESCE(MIN(CASE WHEN channel = '' THEN id END), MIN(id))
                      FROM memories GROUP BY agent_id, project_id, content
                  )
                 {iso.and_clause}""",
            iso.params,
        )
    return [{"type": "duplicate_content", "groups": len(dup_rows), "total_extra": total_dupes}]


async def check_oversized_content(db, agent_id: str, fix: bool) -> list[dict]:
    iso = isolation_where(agent_id=agent_id or None)
    rows = await db.execute_fetchall(
        f"SELECT id, content, length(content) as len FROM memories WHERE length(content) > ?{iso.and_clause}",
        (MAX_CONTENT_LENGTH, *iso.params),
    )
    if not rows:
        return []
    if fix:
        for row_id, content, _ in rows:
            await _rewrite_or_delete_on_collision(db, row_id, content[:MAX_CONTENT_LENGTH])
    return [{"type": "oversized_content", "count": len(rows), "max_len": max(r[2] for r in rows)}]


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
            }
        ]
    except Exception as e:
        logger.warning("Embedding dimension check failed: %s", e)
        return []


def _null_embedding_severity(null_count: int, total: int) -> str:
    if not vector._embedding_client:
        return "info"  # mode=none: NULL is the expected steady state
    if total > 0 and null_count / total > NULL_EMBEDDING_CRITICAL_RATIO:
        return "critical"  # pipeline is effectively down
    return "warn"


async def probe_embedding_dim() -> int | None:
    """One live probe embed, meant to run OUTSIDE the write lock (bug-083).
    do_check_health(fix=True) calls this in the unlocked prefetch phase and passes the
    result through embedding_cache["expected_dim"], so check_embedding_dimension no
    longer holds the shared write lock across an embedding HTTP round-trip. None means
    the probe failed (or no client) — the dimension check then skips, same as a failed
    live probe."""
    if not vector._embedding_client:
        return None
    try:
        emb = await vector._embedding_client.embed(["test"])
        return len(emb[0]) if emb and emb[0] else None
    except Exception:
        return None


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
    for table, text_col in (("memories", "content"), ("episodes", "summary")):
        rows = await db.execute_fetchall(
            f"SELECT id, {text_col} FROM {table} WHERE embedding IS NULL{iso.and_clause} LIMIT 500", iso.params
        )
        for start in range(0, len(rows), 32):
            chunk = rows[start : start + 32]
            try:
                embeddings = await vector._embedding_client.embed([text for _, text in chunk])
            except Exception:
                health.observe_failure("prefetch embed failed")
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
        f"SELECT id, {text_col} FROM {table} WHERE embedding IS NULL{iso.and_clause} LIMIT 500", iso.params
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
    issue = {
        "type": "null_embedding",
        "count": null_count,
        "severity": _null_embedding_severity(null_count, total),
    }
    if fix and vector._embedding_client:
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
        issue = {"type": "fts_integrity_failure", "table": table, "severity": "critical"}
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
}

_SQL_NORMALIZE = re.compile(r"\s+")


def _normalize_sql(sql: str) -> str:
    s = sql.replace("IF NOT EXISTS ", "").replace('"', "").strip().rstrip(";")
    return _SQL_NORMALIZE.sub(" ", s).upper()


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
        }
        if fix:
            try:
                if state == "definition_drift":
                    await db.execute(f"DROP {spec['kind'].upper()} IF EXISTS {name}")
                await db.execute(spec["sql"])
                issue["fixed"] = True
            except Exception as e:
                # e.g. UNIQUE index blocked by a locked duplicate row — surface,
                # never force (the locked row wins, same doctrine as the migration).
                issue["fixed"] = False
                issue["fix_error"] = str(e)
        issues.append(issue)
    return issues


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

    Distinct spellings of the same bucket ('cycia-mc-audit' / 'cyciamc-audit')
    split memories across γ buckets that no single read unifies — the rows are
    invisible to each other's recalls. Report-only: which spelling is
    canonical is an operator decision, and the registry of valid project_ids
    is deliberately *not* server knowledge. Distribution itself is not an
    issue (rare != wrong); it is exposed via axis_distribution() in stats.
    """
    # The project-distribution audit is corpus-wide by design (naming drift is a
    # cross-bucket phenomenon); the typed no-filter helper call replaces the old
    # waiver comment (Task #180).
    iso = isolation_where(agent_id=None)
    rows = await db.execute_fetchall(
        f"SELECT project_id, COUNT(*) FROM memories WHERE project_id != ''{iso.and_clause} GROUP BY project_id"
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
    return [{"type": "invalid_json", "bad_source": bad_source, "bad_metadata": bad_metadata}]


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
    if fix:
        await db.execute(
            f"UPDATE memories SET timestamp = created_at WHERE datetime(timestamp) IS NULL AND timestamp != '' AND locked = 0{iso.and_clause}",
            iso.params,
        )
    return [{"type": "invalid_timestamp", "count": bad_ts}]


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
        f"SELECT id, timestamp FROM memories WHERE timestamp != ''{iso.and_clause}", iso.params
    )
    counts = {"utc": 0, "aware": 0, "naive": 0}
    aware_rows: list[tuple[int, str]] = []
    for row_id, ts in rows:
        cls = _classify_timestamp(ts)
        counts[cls] += 1
        if cls == "aware":
            aware_rows.append((row_id, ts))
    present = [k for k, v in counts.items() if v > 0]
    if len(present) <= 1 and not aware_rows:
        return []
    issue = {"type": "timestamp_format_drift", **counts}
    if fix and aware_rows:
        normalized = 0
        for row_id, ts in aware_rows:
            try:
                parsed = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                canon = parsed.astimezone(datetime.timezone.utc).isoformat()
                if canon != ts:
                    await db.execute(
                        "UPDATE memories SET timestamp = ? WHERE id = ? AND locked = 0",
                        (canon, row_id),
                    )
                    normalized += 1
            except ValueError:
                pass
        issue["normalized"] = normalized
    if counts["naive"]:
        issue["unfixable_naive"] = counts["naive"]
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
    return [{"type": "stale_pending_tasks", "count": stale}]


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


async def check_empty_content(db, agent_id: str, fix: bool) -> list[dict]:
    iso = isolation_where(agent_id=agent_id or None)
    empty = (
        await db.execute_fetchall(
            f"SELECT COUNT(*) FROM memories WHERE TRIM(content) = '' OR content IS NULL{iso.and_clause}",
            iso.params,
        )
    )[0][0]
    if empty == 0:
        return []
    if fix:
        await db.execute(
            f"DELETE FROM memories WHERE (TRIM(content) = '' OR content IS NULL) AND locked = 0{iso.and_clause}",
            iso.params,
        )
    return [{"type": "empty_content", "count": empty}]


async def check_invalid_source_type(db, agent_id: str, fix: bool) -> list[dict]:
    """Detect and (optionally) canonicalise legacy source shapes (Task #282, 1b).

    Historical fix path blanket-overwrote every offending row with an anonymous
    ``{"type":"User","id":"","name":""}`` sentinel — a lossy repair that
    destroyed attribution wholesale. The mapping-based fix walks known legacy
    shapes (see ``normalize_source`` for the exhaustive table) and updates only
    rows we can rewrite without fabricating a discriminator. Rows we don't
    recognise stay untouched so the finding remains visible on the next run.
    """
    iso = isolation_where(agent_id=agent_id or None)
    try:
        bad = (
            await db.execute_fetchall(
                f"""SELECT COUNT(*) FROM memories
                    WHERE (json_extract(source, '$.type') NOT IN ('User', 'Agent', 'System')
                    OR json_extract(source, '$.type') IS NULL){iso.and_clause}""",
                iso.params,
            )
        )[0][0]
    except Exception:
        return []
    if bad == 0:
        return []
    issue: dict = {"type": "invalid_source_type", "count": bad}
    if fix:
        # locked = 0 mirrors every sibling fixer (bug-098 invariant). We do the
        # rewrite per-row rather than in one UPDATE because the mapping is
        # value-dependent — the shape a row lands on is a function of its
        # current source, not a single canonical sentinel.
        rows = await db.execute_fetchall(
            f"""SELECT id, source FROM memories
                WHERE (json_extract(source, '$.type') NOT IN ('User', 'Agent', 'System')
                OR json_extract(source, '$.type') IS NULL) AND locked = 0{iso.and_clause}""",
            iso.params,
        )
        mapped = 0
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
            await db.execute(
                "UPDATE memories SET source = ? WHERE id = ? AND locked = 0",
                (json.dumps(new_source), row_id),
            )
            mapped += 1
        issue["mapped"] = mapped
        issue["unmapped"] = unmapped
        # bug-139: `count` spans every offending row, but the fix loop only
        # sees locked = 0 rows (bug-098 invariant). Surface the remainder so
        # mapped + unmapped + locked reconciles with count instead of reading
        # as "found N, processed 0" when the offenders are locked.
        issue["locked"] = bad - len(rows)
    return [issue]


async def check_anonymous_source(db, agent_id: str, fix: bool) -> list[dict]:
    iso = isolation_where(agent_id=agent_id or None)
    try:
        anon = (
            await db.execute_fetchall(
                f"""SELECT COUNT(*) FROM memories
                    WHERE json_extract(source, '$.type') = 'User'
                    AND json_extract(source, '$.id') = ''
                    AND json_extract(source, '$.name') = ''{iso.and_clause}""",
                iso.params,
            )
        )[0][0]
    except Exception:
        return []
    if anon == 0:
        return []
    return [
        {
            "type": "anonymous_source",
            "count": anon,
            "hint": "Use deep_check with fix=true to recover names from content",
        }
    ]


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
    Check("null_embedding", "warn", True, check_null_embedding),
    Check("null_episode_embedding", "warn", True, check_null_episode_embedding),
    Check("fts_integrity", "warn", True, check_fts_integrity),
    Check("schema_version", "critical", False, check_schema_version),
    Check("schema_objects", "critical", True, check_schema_objects),
    Check("sqlite_integrity", "critical", False, check_sqlite_integrity),
    Check("axis_hygiene", "warn", False, check_axis_hygiene),
    Check("invalid_json", "warn", True, check_invalid_json),
    Check("invalid_timestamp", "warn", True, check_invalid_timestamp),
    Check("timestamp_format_drift", "warn", True, check_timestamp_format_drift),
    Check("stale_pending_tasks", "warn", True, check_stale_pending_tasks),
    Check("missing_profile", "info", False, check_missing_profile),
    Check("empty_content", "warn", True, check_empty_content),
    Check("invalid_source_type", "warn", True, check_invalid_source_type),
    Check("anonymous_source", "info", False, check_anonymous_source),
    Check("operating_context_parse", "warn", False, check_operating_context_parse),
    Check("operating_context_size", "info", False, check_operating_context_size),
]

HEALTH_CHECK_NAMES = [c.name for c in HEALTH_CHECKS]


# bug-083: embedding_dimension is cache-aware too — it consumes the pre-probed
# "expected_dim" instead of live-embedding under the write lock.
_EMBEDDING_CHECKS = {"null_embedding", "null_episode_embedding", "embedding_dimension"}


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
            summary[issue["severity"]] += 1
            issues.append(issue)
    return issues, summary


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
    an info-only DB reports ``status='healthy'`` even though the caller-visible
    ``healthy`` boolean (``len(issues) == 0``) may be False. Colocated with
    ``exit_code`` so the two gate mappings evolve together.
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
    rows = await db.execute_fetchall(
        """SELECT id, content FROM memories
           WHERE agent_id = ?
           AND json_extract(source, '$.type') = 'User'
           AND json_extract(source, '$.id') = ''
           AND json_extract(source, '$.name') = ''""",
        (agent_id,),
    )
    recoverable = []
    unrecoverable = []
    for row_id, content in rows:
        match = _USERNAME_PREFIX_PATTERN.match(content)
        if match:
            recoverable.append({"id": row_id, "recovered_name": match.group(1)})
        else:
            unrecoverable.append({"id": row_id, "content_preview": content[:60]})
    fixed_count = 0
    if fix and recoverable:
        for item in recoverable:
            new_source = json.dumps({"type": "User", "id": "", "name": item["recovered_name"]})
            await db.execute(
                "UPDATE memories SET source = ? WHERE id = ? AND locked = 0",
                (new_source, item["id"]),
            )
        fixed_count = len(recoverable)
    result = {"recoverable": len(recoverable), "unrecoverable": len(unrecoverable)}
    if fix:
        result["fixed"] = fixed_count
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
    a non-trivial corpus, or ``calibrated_at`` older than
    ``CALIBRATION_STALE_DAYS``. (Corpus-growth-since-calibration would need a
    corpus-size field in the sidecar — a v2.4.38+ candidate.)
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
