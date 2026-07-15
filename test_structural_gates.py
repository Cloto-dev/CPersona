"""Structural-enforcement gates (2.5.0 stabilization).

These are NOT behavioural tests — they statically analyse the cpersona source with
the ``ast`` module and fail CI when a load-bearing invariant is violated. They exist
because the two most damaging bug classes found in the 2.4.37/2.4.39 audits are
*structural consistency* failures that a plain test never catches: a single writer
that forgets the shared-connection write lock (bug-042/043), or a single agent-scoped
query that forgets its isolation predicate (bug-044/045/047/055/058). One missed call
site is enough; grep is too blunt (SQL spans lines, ``async with`` nests), so the gate
walks the AST.

2.5.0 C-seam: the write-lock gate was tightened into the seam-ownership pair — the
commit/rollback boundary lives only in database.py (connection()/transaction()), and a
transaction() body performs no network I/O (bug-072 class). See Gate 1 / Gate 1b.

Run as part of the normal ``uv run pytest`` CI gate (no ci.yml change needed).

Waiver protocol: a genuinely global operation (boot migration, deliberate cross-agent
maintenance scan) is exempted by name in an allow-list here, or inline with a
``# isolation-waiver: <reason>`` / ``# seam-waiver: <reason>`` comment on the
statement. A waiver is a deliberate, reviewed decision — adding one is the escape hatch,
not silence.
"""

import ast
import pathlib

PKG = pathlib.Path(__file__).parent / "cpersona"

# Agent-scoped tables: every runtime DML statement touching one of these MUST filter by
# agent_id (the mandatory isolation axis). DDL/schema/index statements are exempt (they
# are structural, not per-agent), as are statements carrying an inline isolation-waiver.
AGENT_SCOPED_TABLES = {"memories", "episodes", "profiles", "pending_memory_tasks"}

def _iter_module_files():
    for p in sorted(PKG.glob("*.py")):
        yield p


def _source_lines(path):
    return path.read_text(encoding="utf-8").splitlines()


def _has_inline_waiver(lines, lineno, marker):
    """A waiver comment may sit on the statement line or the line just above it."""
    for ln in (lineno - 1, lineno - 2):
        if 0 <= ln < len(lines) and marker in lines[ln]:
            return True
    return False


# --------------------------------------------------------------------------------------
# Gate 1 (C-seam, 2.5.0): the commit/rollback boundary lives ONLY in database.py.
#
# Every handler reaches the DB through the two seam CMs — connection() (reads) and
# transaction() (write_lock + commit + auto-rollback). Outside database.py there is
# therefore no legitimate call to .commit()/.rollback(), to the lock accessors, or to
# get_db() itself. This subsumes the old "every commit under write_lock" gate
# (bug-042/043): a handler that cannot commit cannot commit unserialised — and it also
# closes the bug-068 class (a committer without rollback-on-fault) because the only
# commit path rolls back automatically.
# --------------------------------------------------------------------------------------

# Call names whose presence outside database.py bypasses the seam.
_SEAM_OWNER_MODULE = "database.py"
_SEAM_BYPASS_ATTRS = {"commit", "rollback"}
_SEAM_BYPASS_NAMES = {"get_db", "write_lock", "maybe_write_lock"}


def _collect_seam_bypasses(tree):
    """Return 'lineno  detail' strings for seam-bypassing calls in a non-owner module."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute) and node.func.attr in _SEAM_BYPASS_ATTRS:
            out.append(f"{node.lineno}  .{node.func.attr}() outside database.py")
        elif isinstance(node.func, ast.Name) and node.func.id in _SEAM_BYPASS_NAMES:
            out.append(f"{node.lineno}  {node.func.id}() outside database.py")
    return out


def test_db_boundary_owned_by_seam():
    """No .commit()/.rollback()/get_db()/write_lock() outside database.py — all DB access
    goes through connection()/transaction() (the 2.5.0 C-seam). Inline `# seam-waiver:
    <reason>` is the reviewed escape hatch."""
    violations = []
    for path in _iter_module_files():
        if path.name == _SEAM_OWNER_MODULE:
            continue
        lines = _source_lines(path)
        tree = ast.parse("\n".join(lines), filename=str(path))
        for hit in _collect_seam_bypasses(tree):
            lineno = int(hit.split()[0])
            if _has_inline_waiver(lines, lineno, "seam-waiver"):
                continue
            violations.append(f"{path.name}:{hit}")
    assert not violations, (
        "DB-seam bypass — the commit/rollback boundary is owned by database.py "
        "(connection()/transaction(), bug-042/043/068 classes). Route the access through "
        "the seam CMs, or add a `# seam-waiver: <reason>` for a reviewed exception:\n  "
        + "\n  ".join(violations)
    )


# --------------------------------------------------------------------------------------
# Gate 1b (C-seam, 2.5.0): a transaction() body performs no network I/O (bug-072 class).
#
# transaction() holds the shared write lock; an embedding HTTP round-trip inside it
# stalls every other writer for the full network timeout. The seam makes the scope
# explicit, so the gate is a simple lexical check over each `async with transaction()`
# body: no reference to the embedding client, the embed() entry point, or httpx.
# (The behavioural twin, test_check_health_never_embeds_under_write_lock below, proves
# the same invariant end-to-end for the check registry, whose calls this static walk
# cannot follow.)
# --------------------------------------------------------------------------------------

_NETWORK_IDENTIFIERS = {"embed", "_embedding_client", "httpx", "_client"}


def _holds_transaction(node):
    """True if an `async with` item opens transaction() — directly or via the
    conditional-seam idiom `(transaction() if write else connection())`."""
    if not isinstance(node, ast.AsyncWith):
        return False
    for item in node.items:
        for n in ast.walk(item.context_expr):
            if (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id == "transaction"
            ):
                return True
    return False


def _collect_network_in_transaction(tree):
    """Return 'lineno  identifier' strings for network-flavoured identifiers referenced
    inside a transaction()-holding `async with` body."""
    out = []

    def scan_body(node):
        for n in ast.walk(node):
            ident = None
            if isinstance(n, ast.Name) and n.id in _NETWORK_IDENTIFIERS:
                ident = n.id
            elif isinstance(n, ast.Attribute) and n.attr in _NETWORK_IDENTIFIERS:
                ident = n.attr
            if ident is not None:
                out.append(f"{n.lineno}  {ident} inside transaction()")

    for node in ast.walk(tree):
        if _holds_transaction(node):
            for stmt in node.body:
                scan_body(stmt)
    return out


def test_no_network_io_inside_transaction():
    """A transaction() body must not reference the embedding client / httpx (bug-072
    class: network I/O under the shared write lock stalls every writer). Pre-compute
    embeddings before entering the seam; inline `# seam-waiver: <reason>` for a
    reviewed exception."""
    violations = []
    for path in _iter_module_files():
        lines = _source_lines(path)
        tree = ast.parse("\n".join(lines), filename=str(path))
        for hit in _collect_network_in_transaction(tree):
            lineno = int(hit.split()[0])
            if _has_inline_waiver(lines, lineno, "seam-waiver"):
                continue
            violations.append(f"{path.name}:{hit}")
    assert not violations, (
        "Network I/O inside a transaction() body (bug-072 class — the shared write lock "
        "is held across the round-trip). Move the embed/HTTP work before the seam entry, "
        "or add a `# seam-waiver: <reason>`:\n  " + "\n  ".join(violations)
    )


# --------------------------------------------------------------------------------------
# Gate 2: every runtime DML on an agent-scoped table filters by agent_id (isolation).
# --------------------------------------------------------------------------------------

def _static_sql_repr(node):
    """Best-effort static reconstruction of a string expression, so f-strings and
    `"a" + "b"` concatenations are analysed too (most isolation-sensitive queries are
    f-strings like `f"... WHERE agent_id = ?{clause}"`). Dynamic parts become a `{?}`
    placeholder — if the agent_id predicate lives only inside a dynamic fragment the
    gate cannot see it and (correctly) demands a waiver or the shared helper."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else ""
    if isinstance(node, ast.JoinedStr):  # f-string
        out = []
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                out.append(part.value)
            else:
                out.append("{?}")
        return "".join(out)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _static_sql_repr(node.left) + _static_sql_repr(node.right)
    return ""


def _sql_string_constants(tree):
    """Yield (lineno, sql_text) for every string/f-string/concat expression that looks
    like SQL. Only top-level string expressions are yielded (not their inner Constant
    children) so an f-string is analysed as one reconstructed statement."""
    seen_children = set()

    def mark_children(n):
        for c in ast.iter_child_nodes(n):
            seen_children.add(id(c))
            mark_children(c)

    for node in ast.walk(tree):
        if id(node) in seen_children:
            continue
        if isinstance(node, (ast.JoinedStr, ast.BinOp)) or (
            isinstance(node, ast.Constant) and isinstance(node.value, str)
        ):
            v = _static_sql_repr(node)
            if v and any(kw in v.upper() for kw in ("SELECT ", "UPDATE ", "DELETE ", "INSERT ")):
                mark_children(node)
                yield node.lineno, v, node


# Dynamic-fragment variable-name conventions that carry the agent predicate at runtime.
# `_agent_scope(agent_id)` (checks.py) returns a `clause`/`params` pair; maintenance builds
# `agent_clause`. A fragment named for any of these is treated as agent-scoped, because the
# predicate lives where the static analyser cannot read it. Known limitation: a future dev
# who names a *channel-only* fragment `clause` would slip past — the durable fix is to route
# all predicate construction through one `isolation_where()` helper (2.5.0 structural note),
# after which this heuristic can be replaced by "fragment came from isolation_where()".
_AGENT_SCOPE_VARNAME_HINTS = ("agent", "clause", "scope")


def _has_agent_dynamic_fragment(node):
    """True if a string expression injects a dynamic fragment whose source references a
    variable named like an agent-scope clause (agent_clause / clause / scope). Such queries
    (e.g. `f"... WHERE 1=1 {clause}"`) carry the agent predicate at runtime where the static
    analyser cannot see it — treated as scoped."""
    for n in ast.walk(node):
        if isinstance(n, ast.FormattedValue):
            for name in ast.walk(n.value):
                ident = getattr(name, "id", None) or getattr(name, "attr", None)
                if ident and any(h in ident.lower() for h in _AGENT_SCOPE_VARNAME_HINTS):
                    return True
    return False


def _is_id_keyed(sql_upper):
    """True if the statement selects/mutates by primary key (`WHERE id = ?`, `id IN (`).
    The id was obtained from a prior agent-scoped read, so operating by it is
    provenance-safe (the row identity is already pinned)."""
    import re

    return bool(re.search(r"\bID\s*=\s*\?", sql_upper) or re.search(r"\bID\s+IN\s*\(", sql_upper))


def _dml_targets_agent_scoped(sql_upper):
    """Return the set of agent-scoped tables this statement performs DML against
    (SELECT ... FROM t / UPDATE t / DELETE FROM t / INSERT INTO t). Excludes pure DDL."""
    import re

    hit = set()
    for t in AGENT_SCOPED_TABLES:
        tu = t.upper()  # sql_upper is upper-cased; the table token must be too.
        # Match the table as a DML target, not as a substring (…_fts, comments).
        patterns = [
            rf"\bFROM\s+{tu}\b",
            rf"\bUPDATE\s+{tu}\b",
            rf"\bINTO\s+{tu}\b",
        ]
        if any(re.search(p, sql_upper) for p in patterns):
            hit.add(t)
    return hit


def test_agent_scoped_dml_carries_agent_id():
    """Any SELECT/UPDATE/DELETE/INSERT whose target is memories/episodes/profiles/
    pending_memory_tasks must reference agent_id (the mandatory isolation axis), unless
    inline-waived. Guards against the missing-predicate class (bug-044/045/047/055/058).

    A statement is considered scoped (not a violation) when any of these hold:
      1. the static SQL text contains `agent_id` (predicate visible to the analyser);
      2. it is id-keyed (`WHERE id = ?` / `id IN (`) — provenance-safe, the id came from a
         prior agent-scoped read;
      3. it injects a dynamic fragment named for an agent-scope clause (clause/scope/agent*),
         where the predicate is added at runtime (e.g. `_agent_scope(agent_id)`);
      4. it carries an inline `# isolation-waiver: <reason>` for a deliberate global op.
    The residual — an agent-scoped DML matching none of these — fails the gate.
    """
    import re

    violations = []
    for path in _iter_module_files():
        # database.py is schema/migration/DDL + deliberate cross-agent maintenance.
        if path.name == "database.py":
            continue
        lines = _source_lines(path)
        tree = ast.parse("\n".join(lines), filename=str(path))
        for lineno, sql, node in _sql_string_constants(tree):
            up = sql.upper()
            # Skip pure DDL / index / trigger / pragma statements.
            if re.search(r"\b(CREATE|DROP|ALTER|PRAGMA|REINDEX)\b", up):
                continue
            targets = _dml_targets_agent_scoped(up)
            if not targets:
                continue
            if "AGENT_ID" in up:                      # (1) static predicate visible
                continue
            if _is_id_keyed(up):                       # (2) provenance-safe by primary key
                continue
            if _has_agent_dynamic_fragment(node):      # (3) predicate injected at runtime
                continue
            if _has_inline_waiver(lines, lineno, "isolation-waiver"):  # (4) deliberate global
                continue
            violations.append(
                f"{path.name}:{lineno}  DML on {sorted(targets)} without agent_id"
            )
    assert not violations, (
        "Agent-scoped DML missing the agent_id isolation predicate (bug-044/045/047/055/"
        "058 class). Add agent_id to the WHERE, or `# isolation-waiver: <reason>` for a "
        "deliberate global operation:\n  " + "\n  ".join(violations)
    )


# --------------------------------------------------------------------------------------
# Gate 3 (dynamic): check_health(fix=True) must never perform embedding network I/O
# while holding the shared write lock (bug-072/083 class).
# --------------------------------------------------------------------------------------

# The round-3 audit found that the bug-072 fix (prefetch outside the lock) left three
# in-lock embed paths open: the dimension-probe embed, the reembed live-fallback on a
# cache miss, and rows NULLed DURING the locked run (dimension mismatch / content
# rewrites) that prefetch could not have covered. A static gate cannot see this class —
# the embed call sites live in helpers reached through the check registry — so this gate
# is BEHAVIOURAL: it drives the real do_check_health(fix=True) through a DB state that
# exercises every embed path, with a client that records whether the lock was held at
# embed time.


import pytest  # noqa: E402  (kept close to the dynamic gate that needs it)


@pytest.mark.asyncio
async def test_check_health_never_embeds_under_write_lock():
    from conftest import FakeEmbeddingClient, fake_embed_one
    from cpersona import database, maintenance_handlers, vector
    from cpersona._vendored_mcp_common import no_persist
    from cpersona.database import get_db

    no_persist.resume()
    db = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await db.execute(f"DELETE FROM {table}")

    embeds_under_lock: list[str] = []

    class LockAwareClient(FakeEmbeddingClient):
        async def embed(self, texts):
            if database._write_lock.locked():
                embeds_under_lock.extend(texts)
            return [fake_embed_one(t) for t in texts]

    old_client = vector._embedding_client
    vector._embedding_client = LockAwareClient()
    try:
        # (a) a pre-existing NULL row — the round-1 prefetch path.
        await db.execute(
            "INSERT INTO memories (agent_id, content, timestamp) VALUES ('gate', 'plain null row', '')"
        )
        # (b) a dimension-mismatched blob — NULLed by check_embedding_dimension DURING
        # the locked run; absent from prefetch by construction.
        import struct

        await db.execute(
            "INSERT INTO memories (agent_id, content, timestamp, embedding) VALUES ('gate', 'wrong dim row', '', ?)",
            (struct.pack("<4f", 1.0, 2.0, 3.0, 4.0),),
        )
        # (c) an annotation row — content rewritten + embedding NULLed by a sibling
        # fixer DURING the locked run (the bug-077 stale-cache scenario).
        await db.execute(
            "INSERT INTO memories (agent_id, content, timestamp) VALUES ('gate', '[Memory from X] gate hello', '')"
        )
        # (d) a NULL-embedding episode — the episode arm of the same paths.
        await db.execute("INSERT INTO episodes (agent_id, summary) VALUES ('gate', 'gate episode')")
        await db.commit()

        await maintenance_handlers.do_check_health(agent_id="gate", fix=True)

        assert not embeds_under_lock, (
            "embedding network I/O while the shared write lock was held (bug-072/083 "
            f"class — this stalls every writer for the round-trip): {embeds_under_lock}"
        )
        # Convergence: one fix run repairs every row (the unlocked second pass) with a
        # vector coherent with the FINAL content (the bug-077 content revalidation).
        rows = await db.execute_fetchall(
            "SELECT content, embedding FROM memories WHERE agent_id = 'gate'"
        )
        for content, blob in rows:
            assert blob is not None, f"row {content!r} left unrepaired after one fix run"
            assert bytes(blob) == FakeEmbeddingClient.pack_embedding(fake_embed_one(content)), (
                f"row {content!r} carries a vector that does not match its final content"
            )
        ep = await db.execute_fetchall("SELECT embedding FROM episodes WHERE agent_id = 'gate'")
        assert ep[0][0] is not None
    finally:
        vector._embedding_client = old_client


# --------------------------------------------------------------------------------------
# Gate 4 (static): identity/dedup probes carry the γ isolation axes (bug-076 class).
# --------------------------------------------------------------------------------------

# Row identity on the agent-scoped tables is defined by the composite UNIQUE indexes
# (idx_memories_dedup_content = (agent_id, project_id, channel, content);
# idx_memories_dedup_msg_id = (agent_id, project_id, msg_id)) and, for episodes (which
# have no uniqueness constraint), by the same convention. A dedup pre-check that matches
# an identity column but omits the axes collapses distinct γ-bucketed rows into one —
# the bug-044/047/057 class on memories, and bug-076 (episode summary probe) where the
# omission became silent permanent data loss under merge mode='move'. Gate 2 cannot
# catch this (agent_id was present); this gate checks the axes.
_IDENTITY_AXES = [
    # (identity predicate in the WHERE clause, axes that must ride along)
    (r"\bCONTENT\s*=\s*\?", ("PROJECT_ID", "CHANNEL")),
    (r"\bSUMMARY\s*=\s*\?", ("PROJECT_ID", "CHANNEL")),
    (r"\bMSG_ID\s*=\s*\?", ("PROJECT_ID",)),
]


def _identity_probe_violations(tree, lines=None):
    """Return 'lineno:detail' strings for identity probes missing their γ axes."""
    import re

    out = []
    for lineno, sql, _node in _sql_string_constants(tree):
        up = sql.upper()
        if re.search(r"\b(CREATE|DROP|ALTER|PRAGMA|REINDEX)\b", up):
            continue
        if not _dml_targets_agent_scoped(up):
            continue
        # Only the predicate side defines identity — `SET content = ?` is a write,
        # not a probe.
        where = up.split(" WHERE ", 1)[1] if " WHERE " in up else ""
        if not where:
            continue
        for predicate, axes in _IDENTITY_AXES:
            if re.search(predicate, where):
                missing = [a for a in axes if a not in where]
                if missing:
                    out.append(f"{lineno}  identity probe missing {missing}")
    return out


def test_identity_probes_carry_isolation_axes():
    """Any WHERE-side content=?/summary=?/msg_id=? probe against an agent-scoped table
    must carry the γ axes that define row identity (project_id, and channel for the
    content/summary probes), unless inline-waived. Guards the bug-044/047/057/076 class."""
    violations = []
    for path in _iter_module_files():
        if path.name == "database.py":
            continue
        lines = _source_lines(path)
        tree = ast.parse("\n".join(lines), filename=str(path))
        for hit in _identity_probe_violations(tree):
            lineno = int(hit.split()[0])
            if _has_inline_waiver(lines, lineno, "isolation-waiver"):
                continue
            violations.append(f"{path.name}:{hit}")
    assert not violations, (
        "Identity/dedup probe missing its γ isolation axes (bug-044/047/057/076 class) — "
        "match the composite UNIQUE-index identity, or add `# isolation-waiver: <reason>` "
        "for a deliberate cross-bucket probe:\n  " + "\n  ".join(violations)
    )


# --------------------------------------------------------------------------------------
# Teeth tests: prove the analysers actually catch violations. A static gate that silently
# stops matching (e.g. the case-mismatch bug where an upper-cased table pattern never hit
# an upper-cased SQL string) is worse than no gate — it reports false confidence. These
# feed known-bad snippets and assert the detectors flag them.
# --------------------------------------------------------------------------------------

def test_seam_gate_has_teeth():
    # The full pre-seam committer idiom — every call in it must flag.
    src = (
        "async def bad():\n"
        "    db = await get_db()\n"
        "    async with write_lock():\n"
        "        await db.execute('INSERT INTO memories VALUES (1)')\n"
        "        await db.commit()\n"
        "    await db.rollback()\n"
    )
    hits = _collect_seam_bypasses(ast.parse(src))
    assert len(hits) == 4, f"seam gate missed a bypass call (got {hits})"

    # The seam idiom itself must NOT flag.
    ok = (
        "async def good():\n"
        "    async with transaction() as db:\n"
        "        await db.execute('INSERT INTO memories VALUES (1)')\n"
        "    async with connection() as db:\n"
        "        await db.execute_fetchall('SELECT 1')\n"
    )
    assert not _collect_seam_bypasses(ast.parse(ok)), "seam gate false-positived the seam CMs"


def test_transaction_network_gate_has_teeth():
    # An embed under the write seam — the literal bug-072 shape — must flag,
    # including through the conditional-seam idiom.
    bad = (
        "async def bad(fix):\n"
        "    async with (transaction() if fix else connection()) as db:\n"
        "        embeddings = await vector._embedding_client.embed(['x'])\n"
    )
    assert _collect_network_in_transaction(ast.parse(bad)), (
        "transaction-network gate failed to flag an embed under the write seam"
    )

    # The same call under the read seam must NOT flag (no lock held).
    ok = (
        "async def good():\n"
        "    async with connection() as db:\n"
        "        embeddings = await vector._embedding_client.embed(['x'])\n"
        "    async with transaction() as db:\n"
        "        await db.execute('INSERT INTO memories VALUES (?)', (1,))\n"
    )
    assert not _collect_network_in_transaction(ast.parse(ok)), (
        "transaction-network gate false-positived a read-seam embed / clean write body"
    )


def test_isolation_gate_has_teeth():
    # An agent-scoped SELECT with no agent_id, not id-keyed, no dynamic clause → must flag.
    bad = ast.parse('q = "SELECT content FROM memories WHERE content = ?"')
    hits = [
        sql for _, sql, _ in _sql_string_constants(bad)
        if _dml_targets_agent_scoped(sql.upper()) and "AGENT_ID" not in sql.upper()
        and not _is_id_keyed(sql.upper())
    ]
    assert hits, "isolation gate failed to flag an unscoped agent-table query"

    # A properly-scoped query must NOT flag.
    good = ast.parse('q = "SELECT content FROM memories WHERE agent_id = ?"')
    hits = [
        sql for _, sql, _ in _sql_string_constants(good)
        if _dml_targets_agent_scoped(sql.upper()) and "AGENT_ID" not in sql.upper()
    ]
    assert not hits, "isolation gate false-positived an agent_id-scoped query"


def test_identity_probe_gate_has_teeth():
    # The literal pre-fix bug-076 probe: agent-scoped but γ-blind — must flag.
    bad = ast.parse('q = "SELECT id FROM episodes WHERE agent_id = ? AND summary = ? LIMIT 1"')
    assert _identity_probe_violations(bad), (
        "identity-probe gate failed to flag the pre-fix bug-076 episode dedup probe"
    )
    # The fixed probe carries both axes — must not flag.
    good = ast.parse(
        'q = "SELECT id FROM episodes WHERE agent_id = ? AND project_id = ? AND channel = ?'
        ' AND summary = ? LIMIT 1"'
    )
    assert not _identity_probe_violations(good), (
        "identity-probe gate false-positived the γ-scoped episode dedup probe"
    )
    # A SET-side content=? (a write, not a probe) must not flag.
    setter = ast.parse('q = "UPDATE memories SET content = ? WHERE id = ? AND agent_id = ?"')
    assert not _identity_probe_violations(setter), (
        "identity-probe gate false-positived a SET-side content assignment"
    )
