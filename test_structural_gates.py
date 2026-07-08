"""Structural-enforcement gates (2.5.0 stabilization).

These are NOT behavioural tests — they statically analyse the cpersona source with
the ``ast`` module and fail CI when a load-bearing invariant is violated. They exist
because the two most damaging bug classes found in the 2.4.37/2.4.39 audits are
*structural consistency* failures that a plain test never catches: a single writer
that forgets the shared-connection write lock (bug-042/043), or a single agent-scoped
query that forgets its isolation predicate (bug-044/045/047/055/058). One missed call
site is enough; grep is too blunt (SQL spans lines, ``async with`` nests), so the gate
walks the AST.

Run as part of the normal ``uv run pytest`` CI gate (no ci.yml change needed).

Waiver protocol: a genuinely global operation (boot migration, deliberate cross-agent
maintenance scan) is exempted by name in an allow-list here, or inline with a
``# isolation-waiver: <reason>`` / ``# writelock-waiver: <reason>`` comment on the
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

# write_lock gate: commits reached only through these functions are boot/teardown paths
# that run single-threaded before the server serves requests — the shared-connection race
# (bug-042/043) cannot occur there, so they are exempt from the lock requirement.
WRITELOCK_EXEMPT_FUNCS = {"get_db", "close_db"}


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
# Gate 1: every db.commit() runs while the shared write lock is held (bug-042/043).
# --------------------------------------------------------------------------------------

def _is_write_lock_with(node):
    """True if an `async with` item is write_lock() / maybe_write_lock(...)."""
    if not isinstance(node, ast.AsyncWith):
        return False
    for item in node.items:
        call = item.context_expr
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
            if call.func.id in ("write_lock", "maybe_write_lock"):
                return True
    return False


def _func_has_explicit_acquire(func_node):
    """The import/merge handlers acquire the lock explicitly (acquire()/finally release)
    rather than via `async with`, to avoid re-indenting a 140-line body. Detect
    `write_lock().acquire()` anywhere in the function body."""
    for n in ast.walk(func_node):
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "acquire"
            and isinstance(n.func.value, ast.Call)
            and isinstance(n.func.value.func, ast.Name)
            and n.func.value.func.id == "write_lock"
        ):
            return True
    return False


def _collect_unlocked_commits(tree):
    """Return line numbers of `.commit()` calls not covered by the write lock.

    Walks with a `locked` context that is set inside write_lock/maybe_write_lock
    `async with` blocks, and a per-function `explicit` flag for the acquire()/release
    pattern. A commit is covered if either is true.
    """
    unlocked = []
    exempt_funcs = set()

    def visit(node, locked, func_stack):
        # Track function boundaries so exemptions/explicit-acquire are function-scoped.
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            explicit = _func_has_explicit_acquire(node)
            if node.name in WRITELOCK_EXEMPT_FUNCS:
                exempt_funcs.add(node.name)
            new_stack = func_stack + [(node.name, explicit)]
            for child in ast.iter_child_nodes(node):
                visit(child, locked, new_stack)
            return

        if _is_write_lock_with(node):
            for child in ast.iter_child_nodes(node):
                visit(child, True, func_stack)
            return

        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "commit"
        ):
            in_exempt = bool(func_stack) and func_stack[-1][0] in WRITELOCK_EXEMPT_FUNCS
            explicit_here = any(explicit for _, explicit in func_stack)
            if not (locked or explicit_here or in_exempt):
                unlocked.append(node.lineno)

        for child in ast.iter_child_nodes(node):
            visit(child, locked, func_stack)

    visit(tree, False, [])
    return unlocked


def test_all_writers_hold_write_lock():
    """Every persisted commit must be serialised by the shared write lock (bug-042/043),
    except documented boot/teardown paths and inline-waived statements."""
    violations = []
    for path in _iter_module_files():
        lines = _source_lines(path)
        tree = ast.parse("\n".join(lines), filename=str(path))
        for lineno in _collect_unlocked_commits(tree):
            if _has_inline_waiver(lines, lineno, "writelock-waiver"):
                continue
            violations.append(f"{path.name}:{lineno}  db.commit() outside write_lock()")
    assert not violations, (
        "Unserialised commit(s) on the shared connection — wrap [first write … commit] in "
        "`async with write_lock():` (bug-042/043), or add a `# writelock-waiver: <reason>` "
        "if this is a genuine boot/teardown path:\n  " + "\n  ".join(violations)
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

def test_writelock_gate_has_teeth():
    src = (
        "async def bad():\n"
        "    db = await get_db()\n"
        "    await db.execute('INSERT INTO memories VALUES (1)')\n"
        "    await db.commit()\n"
    )
    tree = ast.parse(src)
    assert _collect_unlocked_commits(tree), "write_lock gate failed to flag an unlocked commit"

    ok = (
        "async def good():\n"
        "    async with write_lock():\n"
        "        await db.execute('INSERT INTO memories VALUES (1)')\n"
        "        await db.commit()\n"
    )
    assert not _collect_unlocked_commits(ast.parse(ok)), "write_lock gate false-positived a locked commit"


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
