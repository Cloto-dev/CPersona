"""What one viewer of a shared server may read: the store, narrowed to their agents.

A dashboard opened from a shared server must show a viewer only the agents their
grants let them read. Rather than add a condition to every query the dashboard
makes, each request's read-only connection gets temporary views named like the
tables it reads. SQLite looks a name up in the temp schema before the main one, so
every existing query reads the narrowed view without being changed, and a query
added later is narrowed too. A row tying records together (a mention, a relation, a
node of a record) is kept only when every record it names is visible: otherwise
its ids would say that a hidden record exists.
"""

import hashlib
import json
import sqlite3

from cpersona import acl

# Tables whose rows belong to an agent directly.
_OWNED = ("memories", "episodes", "entities", "profiles", "record_blocks")
# Every agent column a store can carry, so an agent with only entities or only
# relations is still listed.
_AGENT_SOURCES = ("memories", "episodes", "entities", "profiles", "relations", "record_blocks")


class Scope:
    """A viewer's reach: ``grants`` as the ACL table states them, or None for all.

    None is the single-operator case (no ACL: the one token is the store's owner),
    where nothing is narrowed.
    """

    def __init__(self, grants: dict[str, int] | None):
        self.grants = None if grants is None else dict(grants)
        # Caches of computed pictures are keyed by this, so one viewer's result is
        # never served to a viewer who sees other agents.
        self.key = "all" if grants is None else hashlib.sha256(
            json.dumps(sorted(self.grants.items())).encode()
        ).hexdigest()[:16]

    @property
    def everything(self) -> bool:
        """Whether this viewer reads every agent (and so the store's own health)."""
        return self.grants is None or acl.effective_permission(self.grants, acl.WILDCARD) >= acl.PERM_READ

    def readable(self, agent_id: str) -> bool:
        return self.grants is None or acl.effective_permission(self.grants, agent_id) >= acl.PERM_READ

    def apply(self, conn: sqlite3.Connection) -> None:
        """Narrow ``conn`` to this viewer's agents, for the rest of its life."""
        if self.grants is None:
            return
        tables = {r[0] for r in conn.execute("SELECT name FROM main.sqlite_master WHERE type = 'table'")}
        agents = set()
        for t in _AGENT_SOURCES:
            if t in tables:
                agents.update(r[0] for r in conn.execute(f"SELECT DISTINCT agent_id FROM main.{t}"))
        conn.execute("CREATE TEMP TABLE scope_agents (agent_id TEXT PRIMARY KEY)")
        conn.executemany(
            "INSERT INTO temp.scope_agents VALUES (?)", [(a,) for a in sorted(agents) if self.readable(a)]
        )
        mine = "agent_id IN (SELECT agent_id FROM temp.scope_agents)"
        for t in _OWNED:
            if t in tables:
                conn.execute(f"CREATE TEMP VIEW {t} AS SELECT * FROM main.{t} WHERE {mine}")
        # A record by its kind and id, as mentions, relations and nodes name one.
        visible = {
            "mem": f"(SELECT id FROM main.memories WHERE {mine})",
            "ep": f"(SELECT id FROM main.episodes WHERE {mine})" if "episodes" in tables else "(SELECT NULL WHERE 0)",
            "entity": f"(SELECT id FROM main.entities WHERE {mine})" if "entities" in tables else "(SELECT NULL WHERE 0)",
        }

        def shown(kind_col: str, id_col: str) -> str:
            return (
                f"(({kind_col} = 'mem' AND {id_col} IN {visible['mem']}) OR "
                f"({kind_col} = 'ep' AND {id_col} IN {visible['ep']}) OR "
                f"({kind_col} = 'entity' AND {id_col} IN {visible['entity']}))"
            )

        if "entity_aliases" in tables:
            conn.execute(
                f"CREATE TEMP VIEW entity_aliases AS SELECT * FROM main.entity_aliases WHERE entity_id IN {visible['entity']}"
            )
        if "entity_mentions" in tables:
            ref_kind = "substr(ref, 1, instr(ref, ':') - 1)"
            ref_id = "CAST(substr(ref, instr(ref, ':') + 1) AS INTEGER)"
            conn.execute(
                "CREATE TEMP VIEW entity_mentions AS SELECT * FROM main.entity_mentions "
                f"WHERE entity_id IN {visible['entity']} AND {shown(ref_kind, ref_id)}"
            )
        if "relations" in tables:
            conn.execute(
                "CREATE TEMP VIEW relations AS SELECT * FROM main.relations "
                f"WHERE {mine} AND {shown('subject_kind', 'subject_id')} AND {shown('object_kind', 'object_id')}"
            )
        if "record_nodes" in tables:
            conn.execute(
                f"CREATE TEMP VIEW record_nodes AS SELECT * FROM main.record_nodes WHERE {shown('parent_kind', 'parent_id')}"
            )


def for_principal() -> Scope:
    """The scope of the caller of the current MCP request.

    Without an ACL the server serves one operator, so they read everything. A
    per-subject connection is refused for now: its reach is one alias the server
    issues, which the dashboard does not resolve yet, and guessing would widen it.
    """
    config = acl.active_config()
    if config is None:
        return Scope(None)
    principal = acl.current_principal()
    if principal is None:
        raise PermissionError("No caller is known for this request.")
    if principal.client_id in config.per_subject_clients:
        raise PermissionError("The dashboard is not available to per-subject connections yet.")
    return Scope(config.grants_by_client.get(principal.client_id, {}))
