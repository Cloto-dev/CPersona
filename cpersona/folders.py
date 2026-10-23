"""Declared folders: a hierarchy an agent declares under a project (2.6.14a1).

An agent creates folders below a project, up to ``config.FOLDER_MAX_DEPTH`` deep,
and files each record (a memory or an episode) into at most one of them. The
server judges no meaning: it checks that a folder exists, that a filing stays
within the depth limit and that the record belongs to the folder's agent and
project, and it stores the declaration. Records themselves are never written,
so a locked record can be filed as well as any other.

Folders classify; they never narrow. No search path reads these tables, so
filing a record changes no recall or reconstruct result.

A path is a list of folder names below the project: ``[]`` is the project
itself, ``["design"]`` a folder directly below it, ``["design", "recall"]`` one
below that. Names compare by ``associations.normalize`` (NFKC, case-folded,
whitespace collapsed), so two names that differ only in those ways are one
folder. Names may contain ``/``: a path is a list, never a joined string.
"""

from __future__ import annotations

from datetime import datetime, timezone

from cpersona import config
from cpersona._vendored_mcp_common.isolation import coerce_for_write
from cpersona.associations import canonical_ref, normalize, parse_ref
from cpersona.database import connection, transaction

MAX_NAME = 64
MAX_OPERATIONS = 64
OPERATIONS = ("create", "rename", "move", "file", "unfile", "delete")
DECLARED_BY = ("agent", "operator")
_TABLE = {"mem": "memories", "ep": "episodes"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _name(value) -> str | None:
    """A folder name within the bound, or None."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > MAX_NAME or not normalize(value):
        return None
    return value


def _path(value) -> list[str] | None:
    """A path of valid names (possibly empty), or None."""
    if not isinstance(value, list) or len(value) > config.FOLDER_MAX_DEPTH_CEILING:
        return None
    names = [_name(v) for v in value]
    return None if any(n is None for n in names) else names


async def _child(
    db, agent_id: str, project_id: str, parent_id: int, name: str
) -> int | None:
    rows = await db.execute_fetchall(
        "SELECT id FROM folders WHERE agent_id = ? AND project_id = ? AND parent_id = ? AND normalized = ?",
        (agent_id, project_id, parent_id, normalize(name)),
    )
    return rows[0][0] if rows else None


async def children(db, agent_id: str, project_id: str, parent_id: int) -> list[str]:
    """The names directly below a folder (0 = the project), sorted by normalized name."""
    rows = await db.execute_fetchall(
        "SELECT name FROM folders WHERE agent_id = ? AND project_id = ? AND parent_id = ? "
        "ORDER BY normalized, id",
        (agent_id, project_id, parent_id),
    )
    return [r[0] for r in rows]


async def resolve(
    db, agent_id: str, project_id: str, path: list[str]
) -> tuple[int | None, int, int]:
    """Walk `path` from the project. Returns (folder id or None, parent id reached, depth reached).

    The id is 0 for the empty path (the project). When a name is missing, the id is
    None and the parent id and depth say where the walk stopped, so the caller can
    name the folders that do exist there.
    """
    current = 0
    for depth, name in enumerate(path):
        nxt = await _child(db, agent_id, project_id, current, name)
        if nxt is None:
            return None, current, depth
        current = nxt
    return current, current, len(path)


async def path_of(db, folder_id: int) -> list[str]:
    """The names from the project down to `folder_id` (bounded, so a cycle cannot loop)."""
    names: list[str] = []
    current = folder_id
    for _ in range(config.FOLDER_MAX_DEPTH_CEILING + 1):
        if current == 0:
            return list(reversed(names))
        rows = await db.execute_fetchall(
            "SELECT name, parent_id FROM folders WHERE id = ?", (current,)
        )
        if not rows:
            break
        names.append(rows[0][0])
        current = rows[0][1]
    return list(reversed(names))


async def _height(db, folder_id: int) -> int:
    """How many levels lie below `folder_id` (0 for a leaf), bounded by the ceiling."""
    level, frontier = 0, [folder_id]
    while frontier and level <= config.FOLDER_MAX_DEPTH_CEILING:
        marks = ",".join("?" * len(frontier))
        rows = await db.execute_fetchall(
            f"SELECT id FROM folders WHERE parent_id IN ({marks})", frontier
        )
        frontier = [r[0] for r in rows]
        if frontier:
            level += 1
    return level


async def _record_scope(db, ref: str) -> tuple[str, str] | None:
    parsed = parse_ref(ref)
    if parsed is None:
        return None
    rows = await db.execute_fetchall(
        f"SELECT agent_id, project_id FROM {_TABLE[parsed[0]]} WHERE id = ?",
        (parsed[1],),
    )
    return (rows[0][0], rows[0][1]) if rows else None


def _missing(path: list[str], depth: int, existing: list[str]) -> dict:
    return {
        "reason": f"no folder {path[: depth + 1]!r}",
        "missing": path[depth],
        "existing": existing,
    }


async def _file(
    db,
    agent_id: str,
    project_id: str,
    ref: str,
    path: list[str],
    declared_by: str,
    now: str,
) -> dict:
    """File one record. Returns {"ok": True, ...} or {"ok": False, "reason": ...}."""
    canon = canonical_ref(ref)
    if canon is None:
        return {"ok": False, "reason": "ref must be mem:<id> or ep:<id>"}
    scope = await _record_scope(db, canon)
    if scope is None or scope[0] != agent_id:
        return {"ok": False, "reason": f"{canon} is not a record of this agent"}
    if scope[1] != project_id:
        return {
            "ok": False,
            "reason": f"{canon} belongs to project {scope[1]!r}, not {project_id!r}",
        }
    if not path:
        return {
            "ok": False,
            "reason": "a record is filed into a folder, not into the project itself",
        }
    folder_id, parent, depth = await resolve(db, agent_id, project_id, path)
    if folder_id is None:
        return {
            "ok": False,
            **_missing(path, depth, await children(db, agent_id, project_id, parent)),
        }
    if len(path) > config.FOLDER_MAX_DEPTH:
        return {
            "ok": False,
            "reason": f"the folder is deeper than the limit {config.FOLDER_MAX_DEPTH}",
        }
    await db.execute(
        "INSERT INTO folder_members (ref, folder_id, declared_by, declared_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(ref) DO UPDATE SET folder_id = excluded.folder_id, "
        "declared_by = excluded.declared_by, declared_at = excluded.declared_at",
        (canon, folder_id, declared_by, now),
    )
    return {"ok": True, "ref": canon, "folder": await path_of(db, folder_id)}


async def _apply(
    db, agent_id: str, project_id: str, item: dict, declared_by: str, now: str
) -> dict:
    op = item.get("op")
    path = _path(item.get("path", []))
    if path is None:
        return {
            "ok": False,
            "reason": f"path must be a list of at most {config.FOLDER_MAX_DEPTH_CEILING} "
            f"names of 1-{MAX_NAME} characters",
        }

    if op == "file":
        return await _file(
            db, agent_id, project_id, item.get("ref"), path, declared_by, now
        )

    if op == "unfile":
        canon = canonical_ref(item.get("ref"))
        if canon is None:
            return {"ok": False, "reason": "ref must be mem:<id> or ep:<id>"}
        cur = await db.execute(
            "DELETE FROM folder_members WHERE ref = ? AND folder_id IN "
            "(SELECT id FROM folders WHERE agent_id = ? AND project_id = ?)",
            (canon, agent_id, project_id),
        )
        if not cur.rowcount:
            return {"ok": False, "reason": f"{canon} is not filed in this project"}
        return {"ok": True, "ref": canon}

    if not path:
        return {"ok": False, "reason": "path must name a folder"}

    if op == "create":
        if len(path) > config.FOLDER_MAX_DEPTH:
            return {
                "ok": False,
                "reason": f"deeper than the limit {config.FOLDER_MAX_DEPTH}",
            }
        parent_id, parent, depth = await resolve(db, agent_id, project_id, path[:-1])
        if parent_id is None:
            return {
                "ok": False,
                **_missing(
                    path, depth, await children(db, agent_id, project_id, parent)
                ),
            }
        if await _child(db, agent_id, project_id, parent_id, path[-1]) is not None:
            return {
                "ok": True,
                "folder": await path_of(
                    db, await _child(db, agent_id, project_id, parent_id, path[-1])
                ),
                "created": False,
            }
        cur = await db.execute(
            "INSERT INTO folders (agent_id, project_id, parent_id, name, normalized, declared_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                agent_id,
                project_id,
                parent_id,
                path[-1],
                normalize(path[-1]),
                declared_by,
                now,
            ),
        )
        return {"ok": True, "folder": await path_of(db, cur.lastrowid), "created": True}

    folder_id, parent, depth = await resolve(db, agent_id, project_id, path)
    if folder_id is None:
        return {
            "ok": False,
            **_missing(path, depth, await children(db, agent_id, project_id, parent)),
        }
    row = await db.execute_fetchall(
        "SELECT parent_id FROM folders WHERE id = ?", (folder_id,)
    )
    parent_id = row[0][0]

    if op == "rename":
        name = _name(item.get("name"))
        if name is None:
            return {"ok": False, "reason": f"name must be 1-{MAX_NAME} characters"}
        clash = await _child(db, agent_id, project_id, parent_id, name)
        if clash is not None and clash != folder_id:
            return {"ok": False, "reason": f"a folder named {name!r} is already there"}
        await db.execute(
            "UPDATE folders SET name = ?, normalized = ? WHERE id = ?",
            (name, normalize(name), folder_id),
        )
        return {"ok": True, "folder": await path_of(db, folder_id)}

    if op == "move":
        target = _path(item.get("parent", []))
        if target is None:
            return {"ok": False, "reason": "parent must be a path"}
        target_id, t_parent, t_depth = await resolve(db, agent_id, project_id, target)
        if target_id is None:
            return {
                "ok": False,
                **_missing(
                    target, t_depth, await children(db, agent_id, project_id, t_parent)
                ),
            }
        ancestor = target_id
        for _ in range(config.FOLDER_MAX_DEPTH_CEILING + 1):
            if ancestor == folder_id:
                return {
                    "ok": False,
                    "reason": "a folder cannot move into itself or below itself",
                }
            if ancestor == 0:
                break
            ancestor = (
                await db.execute_fetchall(
                    "SELECT parent_id FROM folders WHERE id = ?", (ancestor,)
                )
            )[0][0]
        if len(target) + 1 + await _height(db, folder_id) > config.FOLDER_MAX_DEPTH:
            return {
                "ok": False,
                "reason": f"the moved folders would be deeper than the limit {config.FOLDER_MAX_DEPTH}",
            }
        name = (
            await db.execute_fetchall(
                "SELECT name FROM folders WHERE id = ?", (folder_id,)
            )
        )[0][0]
        clash = await _child(db, agent_id, project_id, target_id, name)
        if clash is not None and clash != folder_id:
            return {"ok": False, "reason": f"a folder named {name!r} is already there"}
        await db.execute(
            "UPDATE folders SET parent_id = ? WHERE id = ?", (target_id, folder_id)
        )
        return {"ok": True, "folder": await path_of(db, folder_id)}

    if op == "delete":
        if await children(db, agent_id, project_id, folder_id):
            return {"ok": False, "reason": "the folder has folders below it"}
        members = await db.execute_fetchall(
            "SELECT COUNT(*) FROM folder_members WHERE folder_id = ?", (folder_id,)
        )
        if members[0][0]:
            return {"ok": False, "reason": f"the folder holds {members[0][0]} records"}
        await db.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
        return {"ok": True, "folder": path}

    return {"ok": False, "reason": "op must be one of " + ", ".join(OPERATIONS)}


async def organize(
    agent_id: str, operations, *, project_id: str = "", declared_by: str = "agent"
) -> dict:
    """Apply a list of folder operations in order. Never raises for a malformed one; reports it.

    Each operation is applied on its own: one that is refused is reported in
    `dropped` with its reason, and the ones after it still run (a later one may
    depend on an earlier one, so the order is the caller's).
    """
    if declared_by not in DECLARED_BY:
        raise ValueError("declared_by must be one of " + ", ".join(DECLARED_BY))
    project_id = coerce_for_write(project_id)
    report: dict = {"applied": [], "dropped": []}
    if not isinstance(operations, list):
        report["dropped"].append({"item": "operations", "reason": "must be a list"})
        return report
    now = _now()
    async with transaction() as db:
        for index, item in enumerate(operations):
            label = f"operations[{index}]"
            if index >= MAX_OPERATIONS:
                report["dropped"].append(
                    {
                        "item": label,
                        "reason": f"beyond the {MAX_OPERATIONS}-operation bound",
                    }
                )
                continue
            if not isinstance(item, dict):
                report["dropped"].append({"item": label, "reason": "must be an object"})
                continue
            outcome = await _apply(db, agent_id, project_id, item, declared_by, now)
            ok = outcome.pop("ok")
            entry = {"item": label, "op": item.get("op"), **outcome}
            (report["applied"] if ok else report["dropped"]).append(entry)
    return report


async def file_stored(agent_id: str, project_id: str, ref: str, path) -> dict:
    """store's `folder`: file the record just stored, or say why it was left unfiled.

    The record is stored either way; a folder that does not exist is never created
    here, so a misspelt name leaves the record unfiled and the response names the
    folders that do exist at that level.
    """
    names = _path(path)
    if names is None or not names:
        return {"filed": False, "reason": "folder must be a nonempty list of names"}
    async with transaction() as db:
        outcome = await _file(
            db, agent_id, coerce_for_write(project_id), ref, names, "agent", _now()
        )
    if outcome.pop("ok"):
        return {"filed": True, "folder": outcome["folder"]}
    return {"filed": False, **outcome}


async def folders_of(db, refs: list[str]) -> dict[str, list[str]]:
    """The folder path of each filed ref (absent when unfiled)."""
    found: dict[str, list[str]] = {}
    for start in range(0, len(refs), 500):
        chunk = refs[start : start + 500]
        marks = ",".join("?" * len(chunk))
        rows = await db.execute_fetchall(
            f"SELECT ref, folder_id FROM folder_members WHERE ref IN ({marks})", chunk
        )
        cache: dict[int, list[str]] = {}
        for ref, folder_id in rows:
            if folder_id not in cache:
                cache[folder_id] = await path_of(db, folder_id)
            found[ref] = cache[folder_id]
    return found


async def read_folders_of(refs: list[str]) -> dict[str, list[str]]:
    async with connection() as db:
        return await folders_of(db, refs)


# --- carrying folders between agents and through export / import ---------------

FOLDER_COLUMNS = (
    "f.id, f.agent_id, f.project_id, f.parent_id, f.name, f.declared_by, f.created_at"
)
MEMBER_COLUMNS = "m.ref, m.folder_id, m.declared_by, m.declared_at"


def folder_record(row) -> dict:
    keys = (
        "id",
        "agent_id",
        "project_id",
        "parent_id",
        "name",
        "declared_by",
        "created_at",
    )
    return dict(zip(keys, row))


def member_record(row) -> dict:
    return dict(zip(("ref", "folder_id", "declared_by", "declared_at"), row))


async def drain_rows(cur) -> list:
    # fetchmany, not fetchall: export's reader-scope cursor offers only fetchmany.
    rows: list = []
    while chunk := await cur.fetchmany(500):
        rows.extend(chunk)
    return rows


async def read_tree(db, agent_id: str) -> tuple[list[dict], list[dict]]:
    """One agent's folders and memberships, as export and merge carry them."""
    # Plain execute + fetchmany: export reads through a snapshot connection that
    # offers no execute_fetchall.
    cur = await db.execute(
        f"SELECT {FOLDER_COLUMNS} FROM folders f WHERE f.agent_id = ? ORDER BY f.id",
        (agent_id,),
    )
    folder_rows = await drain_rows(cur)
    cur = await db.execute(
        f"SELECT {MEMBER_COLUMNS} FROM folder_members m JOIN folders f ON f.id = m.folder_id "
        "WHERE f.agent_id = ? ORDER BY m.ref",
        (agent_id,),
    )
    member_rows = await drain_rows(cur)
    return [folder_record(r) for r in folder_rows], [
        member_record(r) for r in member_rows
    ]


async def carry(
    db,
    folder_records: list[dict],
    member_records: list[dict],
    ref_map: dict | None,
    *,
    agent_id: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Write folders and memberships, with folder ids and record refs remapped.

    A folder lands under `agent_id` when one is given (else its own), on the folder
    of the same normalized name under the same parent when one is there, and is
    created otherwise. A membership whose record the map does not hold, whose folder
    did not come along, or whose record already sits in a folder is not written: a
    carry restores declarations, it does not move a record the target already filed.
    `dry_run` writes nothing and counts what a real run would carry.
    """
    out = {"folders": 0, "members": 0, "dropped": 0}
    by_id = {r.get("id"): r for r in folder_records if isinstance(r.get("id"), int)}
    out["dropped"] += len(folder_records) - len(by_id)
    new_id: dict[int, int] = {}
    home: dict[int, tuple[str, str]] = {}
    now = _now()

    async def place(old: int, seen: frozenset) -> int | None:
        if old in new_id:
            return new_id[old]
        record = by_id.get(old)
        name = _name(record.get("name")) if record else None
        owner = agent_id or (record or {}).get("agent_id")
        if (
            record is None
            or name is None
            or old in seen
            or not isinstance(owner, str)
            or not owner
        ):
            return None
        parent_old = record.get("parent_id") or 0
        parent = 0 if parent_old == 0 else await place(parent_old, seen | {old})
        if parent is None:
            return None
        project_id = coerce_for_write(record.get("project_id"))
        home[old] = (owner, project_id)
        existing = (
            await _child(db, owner, project_id, parent, name) if parent >= 0 else None
        )
        if existing is not None:
            new_id[old] = existing
            return existing
        if dry_run:
            new_id[old] = -old  # a stand-in id: nothing below it is looked up for real
            out["folders"] += 1
            return new_id[old]
        declared_by = (
            record.get("declared_by")
            if record.get("declared_by") in DECLARED_BY
            else "agent"
        )
        cur = await db.execute(
            "INSERT INTO folders (agent_id, project_id, parent_id, name, normalized, declared_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                owner,
                project_id,
                parent,
                name,
                normalize(name),
                declared_by,
                record.get("created_at") or now,
            ),
        )
        new_id[old] = cur.lastrowid
        out["folders"] += 1
        return cur.lastrowid

    for old in by_id:
        if await place(old, frozenset()) is None:
            out["dropped"] += 1

    for record in member_records:
        old_ref = canonical_ref(record.get("ref"))
        ref = old_ref if ref_map is None else ref_map.get(old_ref) if old_ref else None
        folder = new_id.get(record.get("folder_id"))
        if ref is None or folder is None:
            out["dropped"] += 1
            continue
        if (
            not dry_run
            and await _record_scope(db, ref) != home[record.get("folder_id")]
        ):
            out["dropped"] += (
                1  # a record is filed only under its own agent and project
            )
            continue
        if dry_run:
            out["members"] += 1
            continue
        declared_by = (
            record.get("declared_by")
            if record.get("declared_by") in DECLARED_BY
            else "agent"
        )
        cur = await db.execute(
            "INSERT OR IGNORE INTO folder_members (ref, folder_id, declared_by, declared_at) VALUES (?, ?, ?, ?)",
            (ref, folder, declared_by, record.get("declared_at") or now),
        )
        out["members" if cur.rowcount else "dropped"] += 1
    return out
