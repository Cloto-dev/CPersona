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

Waiver protocol (seam gates only): a reviewed exception to Gate 1/1b is granted inline
with a ``# seam-waiver: <reason>`` comment on the statement. The isolation gates (2/4)
accept NO waivers as of 2.5.0 (Task #180): a deliberately global operation is spelled
out in code as ``isolation_where(agent_id=None)`` — a typed, greppable decision —
instead of a comment the analyser trusts blindly.
"""

import ast
import pathlib

PKG = pathlib.Path(__file__).parent.parent / "cpersona"

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
            if isinstance(n, ast.Call) and (
                (isinstance(n.func, ast.Name) and n.func.id == "transaction")
                # 2.5.0b1 audit: module-prefixed calls (database.transaction())
                # are ast.Attribute — a Name-only match silently ungated them.
                or (isinstance(n.func, ast.Attribute) and n.func.attr == "transaction")
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


# 2.5.0 (Task #180): a dynamically-assembled isolation predicate is accepted ONLY when it
# demonstrably comes from cpersona.isolation.isolation_where(). Two conditions, both
# required: (a) the f-string embeds an IsolationFilter accessor — an attribute named
# clause / and_clause / where — and (b) the enclosing function actually calls
# isolation_where(). This replaces the pre-2.5.0 variable-NAME heuristic
# (agent/clause/scope), whose documented hole — any fragment *named* `clause` passed,
# whatever it contained — is now a teeth-tested violation.
_ISO_FRAGMENT_ATTRS = {"clause", "and_clause", "where"}


def _isolation_call_spans(tree):
    """(lineno, end_lineno) spans of every function whose body calls isolation_where()
    OR declares an `iso` parameter (bug-120: a shared worker like _reembed_null_rows
    receives the IsolationFilter built by its gate-checked callers — the conduit
    parameter is the documented way to thread the helper through)."""
    spans = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        calls_helper = any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "isolation_where"
            for n in ast.walk(node)
        )
        takes_iso_param = any(
            a.arg == "iso" for a in node.args.args + node.args.kwonlyargs + node.args.posonlyargs
        )
        if calls_helper or takes_iso_param:
            spans.append((node.lineno, node.end_lineno))
    return spans


def _has_helper_fragment(node):
    """True if the string expression embeds an IsolationFilter accessor
    (``{iso.where}`` / ``{iso.and_clause}`` / ``{iso.clause}``)."""
    for n in ast.walk(node):
        if isinstance(n, ast.FormattedValue):
            for a in ast.walk(n.value):
                if isinstance(a, ast.Attribute) and a.attr in _ISO_FRAGMENT_ATTRS:
                    return True
    return False


def _uses_isolation_helper(node, spans):
    """True if the SQL expression embeds a helper fragment AND sits inside a function
    that calls isolation_where() — the accessor must be fed by the real helper, not a
    like-named attribute on some other object."""
    return _has_helper_fragment(node) and any(a <= node.lineno <= b for a, b in spans)


def _is_id_keyed(sql_upper):
    """True if the statement selects/mutates by primary key (`WHERE id = ?`, `id IN (`).
    The id was obtained from a prior agent-scoped read, so operating by it is
    provenance-safe (the row identity is already pinned)."""
    import re

    return bool(re.search(r"\bID\s*=\s*\?", sql_upper) or re.search(r"\bID\s+IN\s*\(", sql_upper))


def _dml_targets_agent_scoped(sql_upper):
    """Return the set of agent-scoped tables this statement performs DML against
    (SELECT ... FROM t / UPDATE t / DELETE FROM t / INSERT INTO t). Excludes pure DDL.

    bug-120 (C04): an interpolated table target (`f"DELETE FROM {table}"` reconstructs
    as `FROM {?}`) is returned as the pseudo-target '<interpolated>' — the analyser
    cannot prove it is NOT an agent-scoped table, so the statement must satisfy the
    same scoping rules instead of being silently skipped."""
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
    if re.search(r"\b(FROM|UPDATE|INTO)\s+\{\?\}", sql_upper):
        hit.add("<interpolated>")
    return hit


def _strip_sql_comments(sql):
    """Remove SQL line (`-- …`) and block (`/* … */`) comments so a commented
    `agent_id` can no longer satisfy the scoping check (bug-120 / C03)."""
    import re

    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    return re.sub(r"--[^\n]*", " ", sql)


def _where_scoped_by_agent_id(where_text):
    """True when the WHERE clause carries a real agent_id *conjunct* (bug-120 / C03):

    - the mention must be a comparison predicate (`AGENT_ID = …` / `AGENT_ID IN (…)`),
      not a bare substring (`OR agent_id IS NULL`, an unrelated column, a comment);
    - the clause must not contain a paren-depth-0 OR — a top-level disjunction voids
      conjunctive scoping (`WHERE agent_id = ? OR locked = 0` is unscoped for half its
      rows). Depth-limited on purpose: a lexical gate, not a SQL parser.
    """
    import re

    depth = 0
    depth0_chars = []
    for ch in where_text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            depth0_chars.append(ch)
    if re.search(r"\bOR\b", "".join(depth0_chars)):
        return False
    return bool(re.search(r"\bAGENT_ID\s*(=|IN\s*\()", where_text))


def _agent_dml_violations(tree):
    """Return 'lineno  detail' strings for agent-scoped DML that carries neither a
    visible agent_id predicate nor an isolation_where()-derived fragment.

    A statement is considered scoped (not a violation) when any of these hold:
      1. the static SQL text contains `agent_id` (predicate visible to the analyser);
      2. it is id-keyed (`WHERE id = ?` / `id IN (`) — provenance-safe, the id came from a
         prior agent-scoped read;
      3. it embeds an IsolationFilter accessor AND its enclosing function calls
         isolation_where() — the predicate (or the typed decision to omit it,
         `agent_id=None`) comes from the single helper.
    There is NO waiver escape (2.5.0, Task #180): the residual fails the gate.
    """
    import re

    spans = _isolation_call_spans(tree)
    out = []
    for lineno, sql, node in _sql_string_constants(tree):
        up = _strip_sql_comments(sql).upper()  # bug-120: a commented agent_id is not scoping
        # Skip pure DDL / index / trigger / pragma statements.
        if re.search(r"\b(CREATE|DROP|ALTER|PRAGMA|REINDEX)\b", up):
            continue
        # Skip FTS5 self-referencing control commands (`INSERT INTO fts(fts)
        # VALUES('rebuild'/'integrity-check')`) — index maintenance, not row DML.
        if re.search(r"\bINTO\s+(\{\?\}|\w+)\s*\(\s*\1\s*[,)]", up):
            continue
        targets = _dml_targets_agent_scoped(up)
        if not targets:
            continue
        # (1) static predicate visible — 2.5.0b1 audit + bug-120 (C03): for
        # UPDATE/DELETE/SELECT the agent_id must be a real WHERE *conjunct*
        # (comparison predicate, no top-level OR), so `OR agent_id IS NULL`,
        # comments, and unrelated mentions no longer clear an unscoped
        # statement. INSERTs have no WHERE: agent_id in the column list IS
        # the scoping there.
        if up.lstrip().startswith("INSERT"):
            if re.search(r"\bAGENT_ID\b", up):
                continue
        else:
            where_idx = up.find("WHERE")
            if where_idx >= 0 and _where_scoped_by_agent_id(up[where_idx + len("WHERE"):]):
                continue
        if _is_id_keyed(up):                       # (2) provenance-safe by primary key
            continue
        if _uses_isolation_helper(node, spans):    # (3) predicate from isolation_where()
            continue
        out.append(f"{lineno}  DML on {sorted(targets)} without agent_id")
    return out


def test_agent_scoped_dml_carries_agent_id():
    """Any SELECT/UPDATE/DELETE/INSERT whose target is memories/episodes/profiles/
    pending_memory_tasks must reference agent_id (the mandatory isolation axis) — either
    statically, by primary key, or through isolation_where(). Guards against the
    missing-predicate class (bug-044/045/047/055/058)."""
    violations = []
    for path in _iter_module_files():
        # database.py is schema/migration/DDL + deliberate cross-agent maintenance.
        # isolation.py is the helper itself (it holds fragments, not statements).
        if path.name in ("database.py", "isolation.py"):
            continue
        lines = _source_lines(path)
        tree = ast.parse("\n".join(lines), filename=str(path))
        for hit in _agent_dml_violations(tree):
            violations.append(f"{path.name}:{hit}")
    assert not violations, (
        "Agent-scoped DML missing the agent_id isolation predicate (bug-044/045/047/055/"
        "058 class). Add agent_id to the WHERE, or build the predicate with "
        "isolation_where() — a deliberate global scan is spelled isolation_where("
        "agent_id=None); comment waivers are not accepted:\n  " + "\n  ".join(violations)
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
    content/summary probes). Guards the bug-044/047/057/076 class. No waiver escape
    (2.5.0, Task #180) — a cross-bucket probe that is genuinely wanted is a gate-design
    conversation, not a comment."""
    violations = []
    for path in _iter_module_files():
        if path.name == "database.py":
            continue
        lines = _source_lines(path)
        tree = ast.parse("\n".join(lines), filename=str(path))
        for hit in _identity_probe_violations(tree):
            violations.append(f"{path.name}:{hit}")
    assert not violations, (
        "Identity/dedup probe missing its γ isolation axes (bug-044/047/057/076 class) — "
        "match the composite UNIQUE-index identity:\n  " + "\n  ".join(violations)
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
    # An agent-scoped SELECT with no agent_id, not id-keyed, no helper → must flag.
    bad = ast.parse('q = "SELECT content FROM memories WHERE content = ?"')
    assert _agent_dml_violations(bad), (
        "isolation gate failed to flag an unscoped agent-table query"
    )

    # A statically-scoped query must NOT flag.
    good = ast.parse('q = "SELECT content FROM memories WHERE agent_id = ?"')
    assert not _agent_dml_violations(good), (
        "isolation gate false-positived an agent_id-scoped query"
    )

    # The documented hole in the pre-2.5.0 varname heuristic: an ad-hoc fragment merely
    # NAMED `clause` (contents unknown to the analyser) must now flag.
    adhoc = ast.parse(
        "async def f(clause):\n"
        "    q = f\"SELECT content FROM memories WHERE 1=1 {clause}\"\n"
    )
    assert _agent_dml_violations(adhoc), (
        "isolation gate still passes an ad-hoc clause-named fragment (Task #180 hole)"
    )

    # The helper idiom must pass — including the typed global scan (agent_id=None).
    helper = ast.parse(
        "async def f(agent_id):\n"
        "    iso = isolation_where(agent_id=agent_id)\n"
        "    q = f\"SELECT COUNT(*) FROM memories{iso.where}\"\n"
    )
    assert not _agent_dml_violations(helper), (
        "isolation gate false-positived the isolation_where() idiom"
    )

    # A helper-LOOKING accessor with no isolation_where() call in the enclosing
    # function must flag (the accessor alone proves nothing about its origin).
    impostor = ast.parse(
        "async def f(other):\n"
        "    q = f\"SELECT COUNT(*) FROM memories{other.where}\"\n"
    )
    assert _agent_dml_violations(impostor), (
        "isolation gate trusts a .where accessor without an isolation_where() call"
    )

    # A waiver comment must no longer rescue an unscoped statement. _agent_dml_violations
    # never consults comments, but pin the end-to-end behaviour anyway: the marker text
    # appears nowhere in the decision path.
    waived = ast.parse(
        'q = "SELECT content FROM memories WHERE content = ?"  # isolation-waiver: nope'
    )
    assert _agent_dml_violations(waived), (
        "isolation gate resurrects the abolished isolation-waiver escape"
    )


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


# --------------------------------------------------------------------------------------
# Gate 6 (bug-124): no orphaning re-point of the connection singletons.
#
# Every aiosqlite connection owns a NON-daemon worker thread, so re-pointing
# database._db / database._read_db without first closing (or stashing) the old object
# leaks a thread that blocks interpreter exit forever: the suite passes in seconds and
# the process then hangs until the CI job limit (bug-124 — 2.5.0b2 was merged with its
# test jobs still pending because of exactly this). The doctrine lives on
# database.close_db(); this gate makes the orphaning shape structurally unwritable.
#
# The analyser recognises the two safe idioms in the enclosing scope BEFORE the
# assignment, so routine fixtures need no waiver:
#   - a close: `await <conn>.close()` / `await database.close_db()`
#   - a stash: `saved = database._db` (the old object stays referenced for restore)
# Anything else needs an inline `# orphan-waiver: <reason>`.
# --------------------------------------------------------------------------------------

TESTS_DIR = pathlib.Path(__file__).parent
_ORPHAN_ATTRS = {"_db", "_read_db"}
_CLOSE_NAMES = {"close", "close_db"}


def _enclosing_scope(tree, lineno):
    """Innermost function whose span contains lineno, else the module itself."""
    scope = tree
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.lineno <= lineno <= (node.end_lineno or node.lineno):
                if scope is tree or node.lineno > scope.lineno:
                    scope = node
    return scope


def _scope_has_safeguard(scope, before_lineno):
    """A close call or a stash of the old connection object, earlier in the scope."""
    for node in ast.walk(scope):
        if (node.lineno if hasattr(node, "lineno") else before_lineno) >= before_lineno:
            continue
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr in _CLOSE_NAMES:
                return True
            if isinstance(f, ast.Name) and f.id in _CLOSE_NAMES:
                return True
        if isinstance(node, ast.Assign):
            for sub in ast.walk(node.value):
                if isinstance(sub, ast.Attribute) and sub.attr in _ORPHAN_ATTRS:
                    return True
    return False


def _collect_orphan_repoints(tree):
    """Return 'lineno  detail' strings for unsafeguarded singleton re-points."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not (isinstance(target, ast.Attribute) and target.attr in _ORPHAN_ATTRS):
                continue
            scope = _enclosing_scope(tree, node.lineno)
            if _scope_has_safeguard(scope, node.lineno):
                continue
            out.append(
                f"{node.lineno}  .{target.attr} re-pointed with no prior close()/stash"
            )
    return out


def test_no_orphaning_connection_repoint():
    """Re-pointing database._db / _read_db without closing or stashing the old object
    leaks a non-daemon aiosqlite thread that blocks interpreter exit (bug-124). Route
    the teardown through database.close_db(), stash the old object for restore, or add
    an inline `# orphan-waiver: <reason>` for a reviewed exception."""
    violations = []
    scanned = list(sorted(PKG.glob("*.py"))) + list(sorted(TESTS_DIR.glob("*.py")))
    assert len(scanned) > 20, "orphan gate glob collapsed — scanning almost nothing"
    for path in scanned:
        if path.name == _SEAM_OWNER_MODULE:
            continue  # database.py owns the singletons; close_db() re-points by design
        lines = _source_lines(path)
        tree = ast.parse("\n".join(lines), filename=str(path))
        for hit in _collect_orphan_repoints(tree):
            lineno = int(hit.split()[0])
            if _has_inline_waiver(lines, lineno, "orphan-waiver"):
                continue
            violations.append(f"{path.name}:{hit}")
    assert not violations, (
        "connection-orphaning re-point — every aiosqlite connection owns a non-daemon "
        "thread; a None/other assignment that drops the last reference hangs interpreter "
        "exit (bug-124). Close via database.close_db(), stash the old object, or add a "
        "`# orphan-waiver: <reason>`:\n  " + "\n  ".join(violations)
    )


def test_orphan_gate_has_teeth():
    # The literal bug-124 shape — a bare re-point with no close and no stash — must flag.
    bad = ast.parse(
        "async def bad():\n"
        "    database._db = None\n"
    )
    assert _collect_orphan_repoints(bad), (
        "orphan gate failed to flag a bare connection re-point"
    )

    # close-then-repoint must NOT flag.
    ok_close = ast.parse(
        "async def good():\n"
        "    await database._db.close()\n"
        "    database._db = None\n"
    )
    assert not _collect_orphan_repoints(ok_close), (
        "orphan gate false-positived the close-then-repoint idiom"
    )

    # close_db()-then-repoint must NOT flag.
    ok_close_db = ast.parse(
        "async def good():\n"
        "    await database.close_db()\n"
        "    database._db = None\n"
    )
    assert not _collect_orphan_repoints(ok_close_db), (
        "orphan gate false-positived the close_db()-then-repoint idiom"
    )

    # stash-then-repoint must NOT flag (the old object stays referenced for restore).
    ok_stash = ast.parse(
        "async def good():\n"
        "    saved = database._db\n"
        "    database._db = None\n"
    )
    assert not _collect_orphan_repoints(ok_stash), (
        "orphan gate false-positived the stash-then-repoint idiom"
    )

    # A safeguard in a DIFFERENT function must not sanctify this one.
    cross_scope = ast.parse(
        "async def other():\n"
        "    await database.close_db()\n"
        "async def bad():\n"
        "    database._db = None\n"
    )
    assert _collect_orphan_repoints(cross_scope), (
        "orphan gate accepts a close() from an unrelated scope"
    )

    # A close AFTER the re-point is too late — the old object is already dropped.
    too_late = ast.parse(
        "async def bad():\n"
        "    database._db = None\n"
        "    await database.close_db()\n"
    )
    assert _collect_orphan_repoints(too_late), (
        "orphan gate accepts a close that happens after the re-point"
    )
