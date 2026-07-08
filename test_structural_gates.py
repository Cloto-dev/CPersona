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
