"""Cases for the block arm's candidate generation, observed through the code that serves recall.

The block arm's search (``blocks.search``) has two halves. The first reads the
block rows a call may look at, measures their Hamming distance to the query and
keeps the nearest few in a written-down order; the second reads the stored
vectors of those few and orders them by cosine. The first half returns integers
and nothing else, so another implementation can be held to returning exactly the
same rows in exactly the same order. This module states the cases that hold it
there, and the expected answer of each is taken from the functions recall runs —
``blocks._examined``, ``blocks._measured`` and ``blocks._order`` — over a real
database, never written by hand.

``scripts/capture-block-candidates.py`` writes the cases to
``tests/golden/block_candidates.json``; ``tests/test_block_candidates_golden.py``
asserts that the file still says what the code returns, and that the cases still
exercise what the contract names. The contract itself is
``docs/BLOCK_CANDIDATES_CONTRACT.md``.

Rows and queries are generated from fixed seeds, so the file is a function of
this module and the code under observation.
"""

from __future__ import annotations

import contextlib
import json
import os
import random
import tempfile
from dataclasses import dataclass, field

from cpersona import blocks, database
from cpersona.isolation import isolation_where

GOLDEN_FORMAT = 1

#: The two model keys a deployment compares a row's label with: the model it
#: reports, and the empty legacy label (``generation.block_keys``).
MODELS = ("model-a", "")

#: The caps recall uses, for the cases that keep them.
DEFAULT_CAPS = {
    "examined": blocks.BLOCK_EXAMINED_CAP,
    "per_parent": blocks.BLOCK_PER_PARENT_CAP,
    "depth": blocks.BLOCK_RERANK_DEPTH,
}


@dataclass
class Case:
    name: str
    note: str
    width: int
    caps: dict
    query: dict
    rows: list = field(default_factory=list)


def _bits(rng: random.Random, width: int) -> bytes:
    return bytes(rng.getrandbits(8) for _ in range(width))


def _row(kind, parent_id, block_index, *, agent, project="", channel="", model="model-a", bits=None):
    return [kind, parent_id, block_index, agent, project, channel, model, bits]


def _records(rng, width, count, *, agent, kinds=("ep", "mem"), blocks_per=(1, 9), gaps=True, **axes):
    """``count`` records of random length, with gaps in their ids and block indexes."""
    rows = []
    next_id = {kind: 1 for kind in kinds}
    for _ in range(count):
        kind = rng.choice(kinds)
        parent_id = next_id[kind]
        next_id[kind] += rng.randint(1, 3) if gaps else 1
        index = 0
        for _ in range(rng.randint(*blocks_per)):
            rows.append(_row(kind, parent_id, index, agent=agent, bits=_bits(rng, width), **axes))
            index += rng.randint(1, 2) if gaps else 1
    return rows


def _query(rng, width, *, agent="agent.a", project=None, channel=""):
    return {"bits": _bits(rng, width), "agent_id": agent, "project_id": project, "channel": channel,
            "models": list(MODELS)}


def build_cases() -> list[Case]:
    cases: list[Case] = []

    # A deployment's shape: the real width of a 768-dimension model and the caps
    # recall uses, none of which binds at this size.
    rng = random.Random(20261008)
    rows = _records(rng, 96, 120, agent="agent.a", blocks_per=(1, 12))
    cases.append(Case("deployment_shape", "768 bits, recall's caps, nothing binds", 96, dict(DEFAULT_CAPS),
                      _query(rng, 96), rows))

    # One byte of bits: distances collide constantly, so the depth cut lands
    # inside a run of equal distances and only the written-down order decides.
    rng = random.Random(1)
    rows = _records(rng, 1, 120, agent="agent.a", blocks_per=(1, 6))
    cases.append(Case("ties_at_the_cut", "one-byte bits, the cut falls inside a tie", 1,
                      {"examined": 100_000, "per_parent": 64, "depth": 40}, _query(rng, 1), rows))

    # A record's share binds: long records, a share of three.
    rng = random.Random(2)
    rows = _records(rng, 4, 60, agent="agent.a", blocks_per=(4, 15))
    cases.append(Case("per_parent_cap_binds", "a share of three over long records", 4,
                      {"examined": 100_000, "per_parent": 3, "depth": 50}, _query(rng, 4), rows))

    # The examined cap binds in the middle of a record, in key order: every
    # episode comes before every memory.
    rng = random.Random(3)
    rows = _records(rng, 4, 80, agent="agent.a", blocks_per=(2, 8))
    cases.append(Case("examined_cap_binds", "the examined cap cuts mid-record, in key order", 4,
                      {"examined": 57, "per_parent": 64, "depth": 30}, _query(rng, 4), rows))

    # Both caps bind, and rows the filter refuses sit inside records: a NULL bit
    # string and another model's label do not spend a share or the cap, and a
    # row of another width, narrower or wider, spends both but is never measured.
    rng = random.Random(4)
    rows = []
    for parent_id in range(1, 41):
        kind = "ep" if parent_id % 3 == 0 else "mem"
        for index in range(rng.randint(3, 10)):
            roll = rng.random()
            if roll < 0.12:
                rows.append(_row(kind, parent_id, index, agent="agent.a", bits=None))
            elif roll < 0.22:
                rows.append(_row(kind, parent_id, index, agent="agent.a", model="model-b", bits=_bits(rng, 4)))
            elif roll < 0.26:
                rows.append(_row(kind, parent_id, index, agent="agent.a", bits=_bits(rng, 3)))
            elif roll < 0.30:
                rows.append(_row(kind, parent_id, index, agent="agent.a", bits=_bits(rng, 5)))
            elif roll < 0.36:
                rows.append(_row(kind, parent_id, index, agent="agent.a", model="", bits=_bits(rng, 4)))
            else:
                rows.append(_row(kind, parent_id, index, agent="agent.a", bits=_bits(rng, 4)))
    cases.append(Case("refused_rows_and_both_caps", "NULL bits, other models, other widths, both caps bind", 4,
                      {"examined": 90, "per_parent": 4, "depth": 35}, _query(rng, 4), rows))

    # The axes. Records of several agents, projects and channels interleave in
    # key order; each query reads one slice of them.
    rng = random.Random(5)
    axis_rows = []
    parent_id = {"ep": 1, "mem": 1}
    for _ in range(140):
        kind = rng.choice(("ep", "mem"))
        agent = rng.choice(("agent.a", "agent.a", "agent.b", ""))
        project = rng.choice(("", "", "proj-x", "proj-y"))
        channel = rng.choice(("", "", "chan-1", "chan-2"))
        for index in range(rng.randint(1, 5)):
            axis_rows.append(_row(kind, parent_id[kind], index, agent=agent, project=project, channel=channel,
                                  bits=_bits(rng, 2)))
        parent_id[kind] += 1
    axis_caps = {"examined": 150, "per_parent": 3, "depth": 60}
    for name, agent, project, channel in (
        ("axes_agent_only", "agent.a", None, ""),
        ("axes_empty_agent", "", None, ""),
        ("axes_global_project_pool", "agent.a", "", ""),
        ("axes_project_and_pool", "agent.a", "proj-x", ""),
        ("axes_channel_and_global", "agent.a", None, "chan-1"),
        ("axes_all_three", "agent.b", "proj-y", "chan-2"),
    ):
        cases.append(Case(name, f"agent {agent!r}, project {project!r}, channel {channel!r}", 2, dict(axis_caps),
                          _query(rng, 2, agent=agent, project=project, channel=channel), [list(r) for r in axis_rows]))

    # Fewer rows than the depth: everything measured comes back.
    rng = random.Random(6)
    rows = _records(rng, 8, 6, agent="agent.a", blocks_per=(1, 3))
    cases.append(Case("fewer_rows_than_the_depth", "every measured row comes back", 8, dict(DEFAULT_CAPS),
                      _query(rng, 8), rows))

    # Nothing qualifies: an agent with no rows, and a query of a width no row has.
    rng = random.Random(7)
    rows = _records(rng, 4, 10, agent="agent.a", blocks_per=(1, 4))
    cases.append(Case("no_rows_for_the_agent", "the agent holds no rows", 4, dict(DEFAULT_CAPS),
                      _query(rng, 4, agent="agent.nobody"), rows))
    cases.append(Case("no_row_of_the_query_width", "every row is another width", 5, dict(DEFAULT_CAPS),
                      _query(rng, 5), [list(r) for r in rows]))

    for case in cases:
        case.rows.sort(key=lambda r: (r[0], r[1], r[2]))
    return cases


@contextlib.asynccontextmanager
async def _database():
    saved = (database._db, database.DB_PATH)
    directory = tempfile.mkdtemp()
    database._db = None
    database.DB_PATH = os.path.join(directory, "block_candidates.db")
    try:
        yield await database.get_db()
    finally:
        await database.close_db()
        database._db, database.DB_PATH = saved


@contextlib.contextmanager
def _caps(caps: dict):
    saved = (blocks.BLOCK_EXAMINED_CAP, blocks.BLOCK_PER_PARENT_CAP, blocks.BLOCK_RERANK_DEPTH)
    blocks.BLOCK_EXAMINED_CAP = caps["examined"]
    blocks.BLOCK_PER_PARENT_CAP = caps["per_parent"]
    blocks.BLOCK_RERANK_DEPTH = caps["depth"]
    try:
        yield
    finally:
        blocks.BLOCK_EXAMINED_CAP, blocks.BLOCK_PER_PARENT_CAP, blocks.BLOCK_RERANK_DEPTH = saved


async def observe(case: Case) -> list[list]:
    """What recall's candidate generation returns for ``case``: the same three
    calls ``blocks.search`` makes, over the case's rows in a fresh database."""
    async with _database() as db:
        shuffled = list(case.rows)
        # Inserted out of order, so the order the answer depends on is the
        # table's key and not the order the rows arrived in.
        random.Random(case.name).shuffle(shuffled)
        await db.executemany(
            "INSERT INTO record_blocks (parent_kind, parent_id, block_index, agent_id, project_id,"
            " channel, start_char, end_char, forced_boundary, embedding_bits, embedding_model)"
            " VALUES (?, ?, ?, ?, ?, ?, 0, 1, 0, ?, ?)",
            [(r[0], r[1], r[2], r[3], r[4], r[5], r[7], r[6]) for r in shuffled],
        )
        await db.commit()
        q = case.query
        iso = isolation_where(agent_id=q["agent_id"], project_id=q["project_id"], channel=q["channel"])
        with _caps(case.caps):
            rows = await blocks._examined(db, iso, tuple(q["models"]))
            near = blocks._order(*blocks._measured(rows, q["bits"]))
    return [[row[0], int(row[1]), int(row[2]), int(distance)] for row, distance in near]


def _hex(value: bytes | None):
    return None if value is None else value.hex()


async def capture() -> dict:
    cases = []
    for case in build_cases():
        cases.append({
            "name": case.name,
            "note": case.note,
            "width": case.width,
            "caps": case.caps,
            "query": {**case.query, "bits": _hex(case.query["bits"])},
            "rows": [[*r[:7], _hex(r[7])] for r in case.rows],
            "expected": await observe(case),
        })
    return {"format": GOLDEN_FORMAT, "row_fields": ROW_FIELDS, "hit_fields": HIT_FIELDS, "cases": cases}


ROW_FIELDS = ["kind", "parent_id", "block_index", "agent_id", "project_id", "channel", "model", "bits"]
HIT_FIELDS = ["kind", "parent_id", "block_index", "distance"]


def to_json(golden: dict) -> str:
    """One row per line, so a change to the file reads as a change to rows."""
    out = ["{"]
    out.append(f' "format": {json.dumps(golden["format"])},')
    out.append(f' "row_fields": {json.dumps(golden["row_fields"])},')
    out.append(f' "hit_fields": {json.dumps(golden["hit_fields"])},')
    out.append(' "cases": [')
    for i, case in enumerate(golden["cases"]):
        out.append("  {")
        for key in ("name", "note", "width", "caps", "query"):
            out.append(f'   {json.dumps(key)}: {json.dumps(case[key], sort_keys=True)},')
        for key in ("rows", "expected"):
            items = case[key]
            if not items:
                out.append(f'   {json.dumps(key)}: []' + ("," if key == "rows" else ""))
                continue
            out.append(f'   {json.dumps(key)}: [')
            out.extend(f"    {json.dumps(item)}" + ("," if j < len(items) - 1 else "") for j, item in enumerate(items))
            out.append("   ]" + ("," if key == "rows" else ""))
        out.append("  }" + ("," if i < len(golden["cases"]) - 1 else ""))
    out.append(" ]")
    out.append("}")
    return "\n".join(out) + "\n"
