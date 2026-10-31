"""Build a small store for checking the dashboard's pages: a few memories, episodes,
entities with aliases, mentions and relations, profiles (one of them written for a
named user), and a calibration file with one gate left without a value, so every
view has something to draw.

Usage: python scripts/dashboard-fixture.py <path to a new .db>
"""

import asyncio
import json
import os
import sys
from pathlib import Path


async def build() -> None:
    from cpersona.database import close_db, get_db

    db = await get_db()
    mem = {}
    for key, agent, text, ts, recalled in (
        ("move", "claude-code", "Caroline moved to Sweden last spring and now works at a design studio in Malmö.", "2026-10-28T09:12:00+00:00", 0),
        ("garden", "claude-code", "The garden plan: tomatoes along the south fence, herbs by the kitchen door.", "2026-10-29T14:03:00+00:00", 3),
        ("trip", "codex", "Booked the train to Gothenburg for the visit on 14 November.", "2026-10-30T08:40:00+00:00", 0),
    ):
        cur = await db.execute(
            "INSERT INTO memories (agent_id, content, timestamp, recall_count) VALUES (?, ?, ?, ?)", (agent, text, ts, recalled)
        )
        mem[key] = cur.lastrowid
    ep = {}
    for key, agent, summary, keywords, start, end, resolved in (
        ("plan", "claude-code", "Planned the visit to Caroline in Sweden: train on 14 November, two nights in Malmö, a day trip to Gothenburg. Open question: whether Melanie joins.", "visit sweden train", "2026-10-30T08:00:00+00:00", "2026-10-30T08:45:00+00:00", 0),
        ("garden", "claude-code", "Reworked the garden layout. Tomatoes moved to the south fence for sun; herbs stay by the kitchen door.", "garden tomatoes herbs", "2026-10-29T13:30:00+00:00", "2026-10-29T14:10:00+00:00", 1),
    ):
        cur = await db.execute(
            "INSERT INTO episodes (agent_id, summary, keywords, start_time, end_time, resolved) VALUES (?, ?, ?, ?, ?, ?)",
            (agent, summary, keywords, start, end, resolved),
        )
        ep[key] = cur.lastrowid
    ent = {}
    for key, name, aliases in (("caroline", "Caroline", ["Caro"]), ("melanie", "Melanie", []), ("malmo", "Malmö", ["Malmo"])):
        cur = await db.execute(
            "INSERT INTO entities (agent_id, name, normalized, declared_by, created_at) "
            "VALUES ('claude-code', ?, ?, 'claude-code', '2026-10-28T09:15:00+00:00')",
            (name, name.lower()),
        )
        ent[key] = cur.lastrowid
        for alias in aliases:
            await db.execute("INSERT INTO entity_aliases VALUES (?, ?, ?)", (ent[key], alias, alias.lower()))
    for key, ref in (("caroline", f"mem:{mem['move']}"), ("caroline", f"ep:{ep['plan']}"), ("malmo", f"mem:{mem['move']}"), ("melanie", f"ep:{ep['plan']}")):
        await db.execute("INSERT INTO entity_mentions VALUES (?, ?, 'claude-code', '2026-10-30T09:00:00+00:00')", (ent[key], ref))
    for subject, predicate, obj, anchor in (
        (("entity", ent["melanie"]), "friend_of", ("entity", ent["caroline"]), ""),
        (("entity", ent["caroline"]), "lives_in", ("entity", ent["malmo"]), f"mem:{mem['move']}"),
    ):
        await db.execute(
            "INSERT INTO relations (agent_id, subject_kind, subject_id, predicate, object_kind, object_id, anchor_ref, declared_by, declared_at) "
            "VALUES ('claude-code', ?, ?, ?, ?, ?, ?, 'claude-code', '2026-10-30T09:05:00+00:00')",
            (*subject, predicate, *obj, anchor),
        )
    for agent, user, content, updated in (
        ("claude-code", "", "Writes Python and TypeScript; prefers short answers with the evidence attached.", "2026-10-31 02:40:00"),
        ("claude-code", "guest", "A guest who asked how to search episodes.", "2026-10-25 08:00:00"),
        ("codex", "", "Works mostly in one repository. Likes small pull requests.", "2026-10-20 10:25:00"),
    ):
        await db.execute("INSERT INTO profiles (agent_id, user_id, content, updated_at) VALUES (?, ?, ?, ?)", (agent, user, content, updated))
    await db.commit()
    await close_db()


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    path = Path(sys.argv[1]).resolve()
    if path.exists():
        sys.exit(f"{path} exists; give a new path")
    # Read when cpersona's config is first imported, so set before the import.
    os.environ["CPERSONA_DB_PATH"] = str(path)
    asyncio.run(build())
    path.with_name(path.name + ".calibration.json").write_text(json.dumps({
        "embedding_model": "bge-m3", "embedding_dim": 1024, "scoring_version": "sample", "global_threshold": 0.553,
        "agent_thresholds": {"claude-code": 0.597, "codex": 0.517}, "global_fused_gate": None,
        "agent_fused_gates": {"claude-code": 0.05}, "fused_gate_signal": "confidence", "agent_betas": {},
        "calibrated_at": "2026-10-30T22:14:05+00:00",
    }), encoding="utf-8")
    print(f"sample store: {path}")


if __name__ == "__main__":
    main()
