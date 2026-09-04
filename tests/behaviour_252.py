"""Behavioural snapshot of the five functions the 2.5.2 alpha stage splits apart.

This is the second half of the safety net; `scripts/mutation-proof.py`
is the first, and they prove different things:

    mutation proof   "if this behaviour broke, a test would go red"
    this snapshot    "the behaviour after the split is the behaviour before it"

The distinction is not academic. A mutation that gets CAUGHT tells us the suite
watches a behaviour we thought to name. It says nothing about the behaviours we
did not think to name — and a refactor changes ALL of them at once. #285 alone
would let a split through that quietly reorders results, drops a field from a
returned dict, or changes which rows a fall-through path scans, as long as every
hand-authored assertion still held.

So the expectations here are not hand-authored. `scripts/capture-behaviour.py`
runs the matrix below against the CURRENT (pre-refactor) implementation and
writes what it observed to `tests/golden/behaviour_252.json`. That artifact IS
the old implementation's behaviour, recorded rather than guessed.
`test_equivalence_252.py` replays the matrix afterwards and diffs. Nobody writes
down what the answer should be, which is the property we actually wanted from a
differential test.

Why not a true differential (run old and new side by side)? The package holds
module-level singletons -- `vector._embedding_client`, `vector._agent_thresholds`,
the cached `get_db` connection -- so two copies cannot coexist in one process
without a parallel package tree, and the write paths cannot run twice against one
database anyway (the second run sees the first's rows). Recording to disk buys
the same guarantee at a fraction of the fragility.

WHAT THIS DOES NOT PROVE
    Equivalence on the covered inputs only. An input shape absent from the matrix
    below is unprotected, exactly as if it had no test at all. The matrix is the
    claim; read it as one. When a scenario is added the golden must be
    regenerated, and the diff in that regeneration is the thing to review.

An observation is deliberately wider than the return value: for the write paths
the return value is the LEAST interesting half (a mutation found a dry_run that
could have committed rows while reporting `imported: 0`). Each scenario records

    result      the return value, or the exception type and message
    db          every row of memories / episodes / profiles afterwards
    outbound    remote index calls and HTTP requests, in order
    thresholds  vector._agent_thresholds, which calibration mutates in place
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

# Mirror conftest's hermetic pins so the capture script gets the same environment
# the suite runs under. setdefault is idempotent, so importing this from a test
# (where conftest already ran) changes nothing.
os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "behaviour.db"))
os.environ.setdefault("CPERSONA_EMBEDDING_MODE", "none")
os.environ.setdefault("CPERSONA_OPERATING_CONTEXT", "off")

import numpy as np  # noqa: E402

from cpersona import session  # noqa: E402
from cpersona import admin_handlers, maintenance_handlers, memory_handlers, utils, vector  # noqa: E402
from cpersona._vendored_mcp_common.embedding_client import EmbeddingClient, EmbedOutcome  # noqa: E402
from cpersona.database import get_db  # noqa: E402

FAKE_DIM = 64

# Rounding for recorded floats. A code move performs identical arithmetic in
# identical order, so the cosine values are bit-stable on one machine; this
# tolerance exists only so a golden captured on macOS survives replay on Linux
# CI. It is many orders of magnitude tighter than any behavioural change -- a
# different candidate set, threshold or ranking moves these values in the first
# decimal, not the tenth.
FLOAT_PLACES = 10

OBSERVED_TABLES = ("memories", "episodes", "profiles")

# `datetime('now')` renders as "2026-07-20 04:11:09" (space, no zone); every
# seeded literal below is ISO-with-T. That difference is what lets a row written
# during the run be told apart from a row the scenario planted, so wall-clock
# never reaches the golden while seeded ordering keys still do.
_GENERATED_TS = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def fake_embed_one(text: str) -> list[float]:
    """Deterministic bag-of-words embedding, identical to conftest's.

    Duplicated rather than imported because `scripts/capture-behaviour.py` runs
    outside pytest, where `tests/conftest.py` is not importable as a module.
    `test_equivalence_252.py` asserts the two stay in agreement.
    """
    vec = np.zeros(FAKE_DIM, dtype=np.float64)
    tokens = text.lower().split() or [text.lower()]
    for tok in tokens:
        seed = int.from_bytes(hashlib.sha256(tok.encode()).digest()[:8], "big")
        vec += np.random.default_rng(seed).standard_normal(FAKE_DIM)
    norm = float(np.linalg.norm(vec))
    if norm == 0.0:
        vec[0], norm = 1.0, 1.0
    return (vec / norm).astype(np.float32).tolist()


def pack(text: str) -> bytes:
    return EmbeddingClient.pack_embedding(fake_embed_one(text))


# ---------------------------------------------------------------------------
# Frozen wall clock (part 1)
# ---------------------------------------------------------------------------
#
# do_store and do_recall both read the wall clock, and the raw values leak into
# the observation in two different ways -- neither of them collapsed by the
# `_GENERATED_TS` space-format regex above:
#
#   memory_handlers.do_store            timestamp = message.get("timestamp",
#                                          datetime.now(timezone.utc).isoformat())
#       -- the default is ISO-with-T ("2027-01-01T00:00:00+00:00"), not the
#       space form SQLite `datetime('now')` produces, so a raw wall clock lands
#       verbatim in the stored row's timestamp column.
#
#   utils._compute_confidence          now = datetime.now(timezone.utc)
#       -- age_hours = (now - stored_timestamp).total_seconds() / 3600, and the
#       returned score = sqrt(norm_cos * time_decay) * ... derives from it.
#       Seeded rows carry Jan-1-2026 timestamps; without a freeze the score
#       moves every day.
#
#   session._now() = datetime.now(UTC)
#       -- feeds the "reason" string's TTL-remaining suffix ("30m left") that
#       session.make_skipped_response substitutes into the store no-persist
#       response. It is read TWICE for that one string (arming the pause, then
#       labelling the remainder), so both reads must see the same instant or the
#       suffix drifts to "29m left" on a slow run.
#
# Marking those output fields `Scenario.volatile` would work but is exactly the
# wrong shape for #362: `confidence.score` and the no-persist response body are
# what 2.5.2b1 must be shown NOT to disturb, and hiding them behind
# "<volatile>" would silently unprotect the pin. Freezing the clock at the
# source keeps the numbers real AND deterministic.
#
# Fixed instant: chosen strictly after every seeded corpus timestamp (the seed
# helpers go up to 2026-02-02) so age_hours is a clearly-artificial round
# number (a Jan-1-2026 row → ~8760h → visibly a year), and BEFORE any
# store-scenario `timestamp` literal so the scenario literals and the frozen
# default are easy to tell apart when eyeballing the diff.
FROZEN_INSTANT = datetime(2027, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


class _FrozenDatetime(datetime):
    """A datetime subclass whose wall-clock reads return FROZEN_INSTANT.

    Subclassing (not monkeypatching the free function) is deliberate: the same
    modules that call ``datetime.now(...)`` also call ``datetime.fromisoformat``,
    ``datetime.strptime`` and subtract datetimes to get timedeltas. The subclass
    inherits every one of those unchanged, so a code path that stamps 'now' AND
    parses a stored timestamp does not fall over the substitution -- only the
    wall clock is stopped.
    """

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            # A caller that asked for naive local time gets naive UTC. No
            # covered call site uses this branch today; it exists so a future
            # scenario that does won't get a real wall clock silently.
            return FROZEN_INSTANT.replace(tzinfo=None)
        return FROZEN_INSTANT.astimezone(tz)

    @classmethod
    def utcnow(cls):
        return FROZEN_INSTANT.replace(tzinfo=None)


def _install_frozen_clock(ctx: Ctx) -> None:
    """Patch every module on the do_store / do_recall path that reads the clock.

    The three modules below were located by grep for ``datetime.now`` /
    ``datetime.utcnow`` over the whole package and then narrowing to the ones
    a store or recall scenario can actually reach:

      cpersona.memory_handlers            do_store's timestamp default (line 86)
      cpersona.utils                       _compute_confidence's `now` (line 145)
      cpersona.session                     _now() → the pause deadline and the
                                          TTL label in the no-persist
                                          skipped-response body

    admin_handlers.py also uses ``datetime.now`` (calibration sidecar / export
    header), but those values are written to files, not into the observation
    dict any scenario returns, and existing golden entries do NOT contain
    them -- so patching admin_handlers is unnecessary and left out to keep the
    patch surface minimal.

    Installed unconditionally in observe() so every scenario sees the same
    frozen environment, including the existing 41 (which cannot reach the
    covered call sites in the first place, so the patch is a no-op for them).
    """
    ctx.patch(memory_handlers, "datetime", _FrozenDatetime)
    ctx.patch(utils, "datetime", _FrozenDatetime)
    ctx.patch(session, "datetime", _FrozenDatetime)


# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------


def canonical(obj: Any) -> Any:
    """Reduce a value to something stable enough to diff across runs.

    Floats are rounded (see FLOAT_PLACES), embedding blobs become a short digest
    -- their bytes are 256 characters of noise that would bury the diff, while a
    digest still fails loudly if the wrong vector is stored -- and generated
    timestamps collapse to a marker.
    """
    if isinstance(obj, float):
        # NaN is not equal to itself, so a recorded NaN could never replay
        # equal; and json would write it as a bare `NaN` token. Spell the three
        # non-finite values out so a golden can pin them like any other value.
        if obj != obj:
            return "<nan>"
        if obj in (float("inf"), float("-inf")):
            return "<inf>" if obj > 0 else "<-inf>"
        # -0.0 and 0.0 compare equal but serialise differently; normalise.
        r = round(obj, FLOAT_PLACES)
        return 0.0 if r == 0.0 else r
    if isinstance(obj, bytes):
        return f"<blob {len(obj)}B sha256:{hashlib.sha256(obj).hexdigest()[:16]}>"
    if isinstance(obj, str):
        return "<generated>" if _GENERATED_TS.match(obj) else obj
    if isinstance(obj, dict):
        return {str(k): canonical(v) for k, v in sorted(obj.items(), key=lambda kv: str(kv[0]))}
    if isinstance(obj, (list, tuple)):
        return [canonical(v) for v in obj]
    return obj


_ROW_REF = re.compile(r"\b(mem|ep):\d+\b")


async def dump_db(db, stable_ids: bool = True) -> dict:
    """Every row of the observed tables, canonicalised.

    With `stable_ids` the rows come back in id order and keep their ids. Without
    it (see `Scenario.unstable_row_ids`) the id is dropped and the rows are keyed
    by their content instead, because the ids themselves are not reproducible.
    """
    out: dict[str, list] = {}
    for table in OBSERVED_TABLES:
        cur = await db.execute(f"SELECT * FROM {table} ORDER BY id")
        cols = [d[0] for d in cur.description]
        rows = [canonical(dict(zip(cols, row))) for row in await cur.fetchall()]
        if not stable_ids:
            for row in rows:
                row["id"] = "<unstable>"
            rows.sort(key=lambda r: (r.get("agent_id", ""), r.get("content") or r.get("summary") or ""))
        out[table] = rows
    return out


class Outbound:
    """Records everything the code under test tries to send off-box.

    The write paths hand rows to the remote index after committing, and the
    remote search path posts to the embedding service. Both are invisible in the
    return value, so a split that dropped or reordered them would otherwise
    replay clean.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def record(self, kind: str, **payload) -> None:
        self.calls.append({"kind": kind, **canonical(payload)})


@dataclass
class Ctx:
    """What a scenario is handed: the database, a place to record outbound
    traffic, a scratch directory, and undoable attribute patching."""

    db: Any
    out: Outbound
    tmp: str
    _undo: list[Callable[[], None]] = field(default_factory=list)

    def patch(self, obj: Any, name: str, value: Any) -> None:
        had = hasattr(obj, name)
        old = getattr(obj, name, None)
        setattr(obj, name, value)
        self._undo.append(lambda: setattr(obj, name, old) if had else delattr(obj, name))

    def restore(self) -> None:
        for undo in reversed(self._undo):
            undo()
        self._undo.clear()

    def path(self, name: str) -> str:
        return os.path.join(self.tmp, name)


@dataclass
class Scenario:
    id: str
    seam: str  # the CSC task whose extraction this input shape guards
    covers: str  # the branch or edge it pins, in one line
    run: Callable  # async (ctx) -> Any
    seed: Callable | None = None  # async (ctx) -> None, before the call
    # Result keys recorded as "<volatile>" instead of their value, for the rare
    # output that is legitimately not reproducible. Calibration draws its sample
    # with `ORDER BY RANDOM()`, so any scenario whose corpus exceeds the sample
    # size gets a different subset every run and every statistic derived from it
    # differs too. Most calibration scenarios avoid this by seeding a corpus
    # SMALLER than the sample size -- the draw is then the whole corpus and the
    # pairwise multiset is order-invariant. Only the cap scenario, whose entire
    # point is that the sample is smaller than the corpus, cannot.
    #
    # A volatile key is unprotected: nothing here would notice if the split
    # changed how it is computed. Keep the list as short as the scenario allows,
    # and never add one to silence a diff that has another explanation.
    volatile: tuple[str, ...] = ()
    # Set when the scenario creates rows whose ids are not reproducible, so the
    # dump is keyed by content instead. Only merge needs it, and the reason is a
    # property of the code rather than of the test: the source SELECT
    # (`FROM memories WHERE agent_id = ?`, admin_handlers.py:1865) has no
    # ORDER BY, so SQLite may hand the rows over in any order -- and because
    # `INSERT OR IGNORE` still consumes an AUTOINCREMENT value when it skips a
    # collision, a different copy order shifts every id that follows it. The
    # golden was captured with the skipped row first and the suite observed it
    # second, which is how this surfaced.
    #
    # Worth carrying into the #287 extraction: the copied rows' ids are already
    # unspecified today, so an extraction that changes them is not by itself a
    # regression -- but one that changes their CONTENT is, and that is what
    # stays pinned here.
    unstable_row_ids: bool = False


async def _reset(db) -> None:
    session.reset_pauses_for_tests()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await db.execute(f"DELETE FROM {table}")
        # Autoincrement counters survive DELETE, so row ids would drift between
        # a capture and a replay -- and ids are load-bearing here (the remote
        # scenarios address rows by id). Reset them too.
        await db.execute("DELETE FROM sqlite_sequence WHERE name = ?", (table,))
    await db.commit()
    vector._agent_thresholds.clear()


def _mask_row_refs(calls: list[dict]) -> list[dict]:
    """Blank the row ids in remote-index payloads and order the items by text.

    Which rows were handed to the index is behaviour worth pinning; the integer
    each one happened to be assigned is not (see `Scenario.unstable_row_ids`).
    """
    masked = []
    for call in calls:
        call = dict(call)
        items = call.get("items")
        if isinstance(items, list):
            unmasked = [
                {**i, "id": _ROW_REF.sub(r"\1:<unstable>", str(i.get("id", "")))}
                if isinstance(i, dict)
                else i
                for i in items
            ]
            call["items"] = sorted(
                unmasked, key=lambda i: str(i.get("text", "")) if isinstance(i, dict) else str(i)
            )
        masked.append(call)
    return masked


def scrub(obj: Any, tmp: str) -> Any:
    """Replace the run's scratch directory with a marker.

    Handlers echo the path they were given back in their result (and in error
    messages), and `tempfile` picks a fresh name every run -- so without this the
    golden differs from itself on a second capture and the whole comparison is
    noise. The file NAME is kept: which file a handler reports is behaviour.
    """
    if isinstance(obj, str):
        return obj.replace(tmp, "<tmp>")
    if isinstance(obj, dict):
        return {k: scrub(v, tmp) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub(v, tmp) for v in obj]
    return obj


async def observe(scenario: Scenario) -> dict:
    """Run one scenario against a clean database and record what happened."""
    db = await get_db()
    await _reset(db)

    with tempfile.TemporaryDirectory() as tmp:
        ctx = Ctx(db=db, out=Outbound(), tmp=tmp)

        # Remote index writes are fire-and-forget in production; capture them
        # instead of letting them reach the network.
        async def _fake_upsert(agent_id, items):
            ctx.out.record("remote_index_upsert", agent_id=agent_id, items=items)

        ctx.patch(vector, "remote_index_upsert", _fake_upsert)
        # Freeze the wall clock so the do_store timestamp default
        # and every _compute_confidence age_hours/score is deterministic. See
        # the module-level `_install_frozen_clock` docstring for why this is a
        # subclass swap rather than a per-field `volatile` mark.
        _install_frozen_clock(ctx)

        try:
            if scenario.seed:
                await scenario.seed(ctx)
            try:
                result = canonical(await scenario.run(ctx))
                if scenario.volatile and isinstance(result, dict):
                    result = {
                        k: ("<volatile>" if k in scenario.volatile else v) for k, v in result.items()
                    }
                raised = None
            except Exception as e:  # noqa: BLE001 -- the exception IS the observation
                result = None
                raised = {"type": type(e).__name__, "message": str(e)}
        finally:
            ctx.restore()

        return scrub(
            {
                "covers": scenario.covers,
                "seam": scenario.seam,
                "result": result,
                "raised": raised,
                "db": await dump_db(db, stable_ids=not scenario.unstable_row_ids),
                # The remote-index payload addresses rows as "mem:{id}", so it
                # carries the same unreproducible ids; sort by text and mask them.
                "outbound": (
                    _mask_row_refs(ctx.out.calls) if scenario.unstable_row_ids else ctx.out.calls
                ),
                # "thresholds" is accepted in `volatile` too: calibration writes
                # its derived threshold here, so a volatile threshold in the
                # result is volatile in this dict by the same argument.
                "thresholds": (
                    "<volatile>"
                    if "thresholds" in scenario.volatile
                    else canonical(dict(vector._agent_thresholds))
                ),
            },
            ctx.tmp,
        )


async def observe_all() -> dict:
    return {s.id: await observe(s) for s in SCENARIOS}


def to_json(observations: dict) -> str:
    return json.dumps(observations, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


async def close_db() -> None:
    """Close the cached aiosqlite connections.

    Only the capture script needs this; under pytest, conftest's session fixture
    does the same job. aiosqlite runs each connection on a NON-daemon thread, so
    a script that leaves one open finishes its work and then hangs forever in
    interpreter shutdown -- which reads exactly like a hung test run.
    """
    from cpersona import database

    for attr in ("_read_db", "_db"):
        conn = getattr(database, attr, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
            setattr(database, attr, None)


# ---------------------------------------------------------------------------
# Fakes for the remote vector branch
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._payload


class FakeHTTP:
    """Records the request and replays a canned response, or raises.

    Recording the posted body matters for the #286 split: `min_similarity` and
    the dedicated short timeout (bug-027 / bug-033) are carried in it, and both
    are the kind of argument a careless extraction silently stops passing.
    """

    def __init__(self, out: Outbound, payload: dict | None, error: Exception | None = None) -> None:
        self._out = out
        self._payload = payload
        self._error = error

    async def post(self, url, json=None, **kwargs):  # noqa: A002 -- httpx's own name
        self._out.record("http_post", url=url, body=json, timeout=kwargs.get("timeout"))
        if self._error is not None:
            raise self._error
        return FakeResponse(self._payload or {})


def install_remote(ctx: Ctx, payload: dict | None, error: Exception | None = None) -> None:
    class _Client:
        _http_url = "http://embed.test/embed"
        _client = FakeHTTP(ctx.out, payload, error)
        mode = "remote"

        async def embed_with_outcome(self, texts):
            result = await self.embed(texts)
            return result, EmbedOutcome(
                attempted=True,
                ok=bool(result),
                error=None if result else "test double produced no embeddings",
            )

        async def embed(self, texts):
            return [fake_embed_one(t) for t in texts]

        @staticmethod
        def pack_embedding(embedding):
            return EmbeddingClient.pack_embedding(embedding)

    ctx.patch(vector, "VECTOR_SEARCH_MODE", "remote")
    ctx.patch(vector, "_embedding_client", _Client())


def install_local(ctx: Ctx) -> None:
    class _Client:
        _http_url = None
        _client = None
        mode = "fake"

        async def embed_with_outcome(self, texts):
            result = await self.embed(texts)
            return result, EmbedOutcome(
                attempted=True,
                ok=bool(result),
                error=None if result else "test double produced no embeddings",
            )

        async def embed(self, texts):
            return [fake_embed_one(t) for t in texts]

        @staticmethod
        def pack_embedding(embedding):
            return EmbeddingClient.pack_embedding(embedding)

    ctx.patch(vector, "VECTOR_SEARCH_MODE", "local")
    ctx.patch(vector, "_embedding_client", _Client())


# ---------------------------------------------------------------------------
# Seeds
# ---------------------------------------------------------------------------

# Seeded timestamps are ISO-with-T so `canonical` can tell them from rows written
# during the run. created_at is explicit and descending-distinct because the local
# scan orders by it, making it part of the observed behaviour.
_MEM_COLS = (
    "agent_id, project_id, channel, msg_id, content, source, timestamp, metadata, "
    "embedding, locked, created_at"
)


async def _mem(db, *, agent="a1", project="", channel="", msg_id="", content="", source="{}", locked=0, seq=1):
    await db.execute(
        f"INSERT INTO memories ({_MEM_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            agent,
            project,
            channel,
            msg_id,
            content,
            source,
            f"2026-01-01T00:00:{seq:02d}Z",
            "{}",
            pack(content),
            locked,
            f"2026-01-01T00:00:{seq:02d}Z",
        ),
    )


async def _ep(db, *, agent="a1", project="", channel="", summary="", resolved=0, seq=1):
    await db.execute(
        "INSERT INTO episodes (agent_id, project_id, channel, summary, keywords, embedding, "
        "start_time, resolved, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            agent,
            project,
            channel,
            summary,
            "k",
            pack(summary),
            f"2026-01-01T00:00:{seq:02d}Z",
            resolved,
            f"2026-01-01T00:00:{seq:02d}Z",
        ),
    )


async def seed_corpus(ctx: Ctx) -> None:
    """A small corpus spanning every axis the search reads: two projects, two
    channels, a per-user source, an episode, and a row whose embedding is a
    foreign dimension."""
    db = ctx.db
    await _mem(db, content="apples and pears in the orchard", seq=1)
    await _mem(db, content="raspberry pi cluster wiring", seq=2)
    await _mem(db, project="proj-b", content="apples in another project", seq=3)
    await _mem(db, channel="chat", content="apples discussed in chat", seq=4)
    await _mem(db, content="apples from a tagged user", source='{"id": "discord:42"}', seq=5)
    await _ep(db, summary="orchard apples episode", seq=6)
    await _ep(db, channel="chat", summary="chat apples episode", resolved=1, seq=7)
    # Foreign embedding width: the scan must skip it rather than reshape-crash.
    await db.execute(
        "INSERT INTO memories (agent_id, content, timestamp, embedding, created_at) VALUES (?,?,?,?,?)",
        ("a1", "apples with a ragged vector", "2026-01-01T00:00:08Z", EmbeddingClient.pack_embedding([0.1] * 8),
         "2026-01-01T00:00:08Z"),
    )
    # A row with no embedding at all -- invisible to the vector retriever, but it
    # must not shift the ids or counts of the rows that are visible.
    await db.execute(
        "INSERT INTO memories (agent_id, content, timestamp, created_at) VALUES (?,?,?,?)",
        ("a1", "apples with no vector", "2026-01-01T00:00:09Z", "2026-01-01T00:00:09Z"),
    )
    await db.commit()


async def seed_calibration(ctx: Ctx, *, n: int = 30, ragged: int = 0) -> None:
    db = ctx.db
    for i in range(n):
        await db.execute(
            "INSERT INTO memories (agent_id, content, timestamp, embedding, created_at) VALUES (?,?,?,?,?)",
            ("cal", f"sample text number {i}", f"2026-02-01T00:00:{i % 60:02d}Z", pack(f"sample text number {i}"),
             f"2026-02-01T00:00:{i % 60:02d}Z"),
        )
    for i in range(ragged):
        await db.execute(
            "INSERT INTO memories (agent_id, content, timestamp, embedding, created_at) VALUES (?,?,?,?,?)",
            ("cal", f"ragged {i}", "2026-02-02T00:00:00Z", EmbeddingClient.pack_embedding([0.2] * 8),
             "2026-02-02T00:00:00Z"),
        )
    await db.commit()


def write_jsonl(path: str, records: list[dict], header: dict | None = None) -> None:
    with open(path, "w", encoding="utf-8") as f:
        if header is not None:
            f.write(json.dumps(header) + "\n")
        for rec in records:
            f.write(json.dumps(rec) + "\n")


# ---------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------

SCENARIOS: list[Scenario] = []


def scenario(
    id: str,
    seam: str,
    covers: str,
    seed=None,
    volatile: tuple[str, ...] = (),
    unstable_row_ids: bool = False,
):
    def deco(fn):
        SCENARIOS.append(
            Scenario(
                id=id,
                seam=seam,
                covers=covers,
                run=fn,
                seed=seed,
                volatile=volatile,
                unstable_row_ids=unstable_row_ids,
            )
        )
        return fn

    return deco


# --- _search_vector, local branch ------------------------------------------


@scenario("sv-local-basic", "_search_vector", "local scan: ranking, top-k cut, returned field set", seed=seed_corpus)
async def _(ctx):
    install_local(ctx)
    return await vector._search_vector(ctx.db, "a1", "apples", 3, min_similarity=0.0)


@scenario("sv-local-limit-1", "_search_vector", "local scan: limit smaller than the candidate set (heapq ordering)", seed=seed_corpus)
async def _(ctx):
    install_local(ctx)
    return await vector._search_vector(ctx.db, "a1", "apples", 1, min_similarity=0.0)


@scenario("sv-local-threshold", "_search_vector", "local scan: min_similarity=None falls back to the per-agent threshold", seed=seed_corpus)
async def _(ctx):
    install_local(ctx)
    vector._agent_thresholds["a1"] = 0.5
    try:
        return await vector._search_vector(ctx.db, "a1", "apples", 10)
    finally:
        vector._agent_thresholds.pop("a1", None)


@scenario("sv-local-high-threshold", "_search_vector", "local scan: a threshold no row clears returns empty, not an error", seed=seed_corpus)
async def _(ctx):
    install_local(ctx)
    return await vector._search_vector(ctx.db, "a1", "apples", 10, min_similarity=0.999)


@scenario("sv-local-project", "_search_vector", "local scan: γ project axis ('X' means X ∪ global)", seed=seed_corpus)
async def _(ctx):
    install_local(ctx)
    return await vector._search_vector(ctx.db, "a1", "apples", 10, min_similarity=0.0, project_id="proj-b")


@scenario("sv-local-project-global", "_search_vector", "local scan: project_id='' is global-only", seed=seed_corpus)
async def _(ctx):
    install_local(ctx)
    return await vector._search_vector(ctx.db, "a1", "apples", 10, min_similarity=0.0, project_id="")


@scenario("sv-local-channel", "_search_vector", "local scan: channel axis, with ''=global still matching", seed=seed_corpus)
async def _(ctx):
    install_local(ctx)
    return await vector._search_vector(ctx.db, "a1", "apples", 10, min_similarity=0.0, channel="chat")


@scenario("sv-local-source-no-channel", "_search_vector", "local scan: source_id without channel drops ALL episodes (bug-080)", seed=seed_corpus)
async def _(ctx):
    install_local(ctx)
    return await vector._search_vector(ctx.db, "a1", "apples", 10, min_similarity=0.0, source_id="discord:")


@scenario("sv-local-source-with-channel", "_search_vector", "local scan: source_id WITH channel keeps episodes (bug-080)", seed=seed_corpus)
async def _(ctx):
    install_local(ctx)
    return await vector._search_vector(
        ctx.db, "a1", "apples", 10, min_similarity=0.0, source_id="discord:", channel="chat"
    )


@scenario("sv-local-source-escape", "_search_vector", "local scan: LIKE metacharacters in source_id are escaped, not matched", seed=seed_corpus)
async def _(ctx):
    install_local(ctx)
    return await vector._search_vector(ctx.db, "a1", "apples", 10, min_similarity=0.0, source_id="discord:_2")


@scenario("sv-local-empty-embed", "_search_vector", "local scan: an empty query embedding returns [] and records the failure", seed=seed_corpus)
async def _(ctx):
    install_local(ctx)

    class _Empty:
        _http_url = None
        _client = None
        mode = "fake"

        async def embed_with_outcome(self, texts):
            result = await self.embed(texts)
            return result, EmbedOutcome(
                attempted=True,
                ok=bool(result),
                error=None if result else "test double produced no embeddings",
            )

        async def embed(self, texts):
            return []

    ctx.patch(vector, "_embedding_client", _Empty())
    return await vector._search_vector(ctx.db, "a1", "apples", 10, min_similarity=0.0)


@scenario("sv-local-unknown-agent", "_search_vector", "local scan: an agent with no rows", seed=seed_corpus)
async def _(ctx):
    install_local(ctx)
    return await vector._search_vector(ctx.db, "nobody", "apples", 10, min_similarity=0.0)


# --- _search_vector, remote branch -----------------------------------------
#
# The split turns the remote branch into a helper whose return signals whether
# local should run. These four scenarios are the ones that pin that signal, and
# `sv-remote-empty` is the sharp one: a remote call that SUCCEEDS with zero hits
# returns [] and must NOT fall through to local. An extraction that conflates
# "no hits" with "remote unavailable" passes every other scenario here.


@scenario("sv-remote-hits", "_search_vector", "remote: memory and episode hits become rows; request body and timeout", seed=seed_corpus)
async def _(ctx):
    install_remote(ctx, {"results": [{"id": "mem:1", "score": 0.91}, {"id": "ep:1", "score": 0.77}]})
    return await vector._search_vector(ctx.db, "a1", "apples", 10, min_similarity=0.25)


@scenario("sv-remote-empty", "_search_vector", "remote: a SUCCESSFUL empty result returns [] and does NOT fall through to local", seed=seed_corpus)
async def _(ctx):
    install_remote(ctx, {"results": []})
    return await vector._search_vector(ctx.db, "a1", "apples", 10, min_similarity=0.0)


@scenario("sv-remote-error-fallback", "_search_vector", "remote: a transport failure falls through to the local scan", seed=seed_corpus)
async def _(ctx):
    install_remote(ctx, None, error=RuntimeError("endpoint down"))
    return await vector._search_vector(ctx.db, "a1", "apples", 3, min_similarity=0.0)


@scenario("sv-remote-isolation-miss", "_search_vector", "remote: a hit whose row fails the γ predicate is dropped (bug-046/075/100)", seed=seed_corpus)
async def _(ctx):
    install_remote(ctx, {"results": [{"id": "mem:3", "score": 0.99}, {"id": "mem:1", "score": 0.95}]})
    # mem:3 is the proj-b row; querying global-only must not surface it.
    return await vector._search_vector(ctx.db, "a1", "apples", 10, min_similarity=0.0, project_id="")


@scenario("sv-remote-stale-id", "_search_vector", "remote: a hit for a row that no longer exists is skipped silently", seed=seed_corpus)
async def _(ctx):
    install_remote(ctx, {"results": [{"id": "mem:9999", "score": 0.99}, {"id": "ep:9999", "score": 0.98}]})
    return await vector._search_vector(ctx.db, "a1", "apples", 10, min_similarity=0.0)


@scenario("sv-remote-episode-src-gate", "_search_vector", "remote: source_id without channel skips episode hits (mirrors local bug-080)", seed=seed_corpus)
async def _(ctx):
    install_remote(ctx, {"results": [{"id": "ep:1", "score": 0.9}, {"id": "mem:5", "score": 0.8}]})
    return await vector._search_vector(ctx.db, "a1", "apples", 10, min_similarity=0.0, source_id="discord:")


# --- do_import_memories -----------------------------------------------------


async def _seed_import_target(ctx: Ctx) -> None:
    await ctx.db.execute(
        "INSERT INTO memories (agent_id, msg_id, content, source, timestamp, created_at) "
        "VALUES ('imp', 'mid-1', 'original text', '{}', '2026-01-01T00:00:01Z', '2026-01-01T00:00:01Z')"
    )
    await ctx.db.commit()


@scenario("import-fresh", "admin-handlers", "import: fresh records land; counts and rows agree", seed=_seed_import_target)
async def _(ctx):
    path = ctx.path("fresh.jsonl")
    write_jsonl(path, [
        {"_type": "memory", "agent_id": "imp", "content": "fresh one"},
        {"_type": "memory", "agent_id": "imp", "content": "fresh two", "msg_id": "mid-2"},
        {"_type": "episode", "agent_id": "imp", "summary": "an episode", "keywords": ["k"]},
        {"_type": "profile", "agent_id": "imp", "content": "profile body"},
    ])
    return await admin_handlers.do_import_memories(path, target_agent_id="imp")


@scenario("import-dry-run", "admin-handlers", "import: dry_run reports the same counts and writes nothing", seed=_seed_import_target)
async def _(ctx):
    path = ctx.path("dry.jsonl")
    write_jsonl(path, [
        {"_type": "memory", "agent_id": "imp", "content": "fresh one"},
        {"_type": "memory", "agent_id": "imp", "content": "fresh two", "msg_id": "mid-2"},
        {"_type": "episode", "agent_id": "imp", "summary": "an episode", "keywords": ["k"]},
    ])
    return await admin_handlers.do_import_memories(path, target_agent_id="imp", dry_run=True)


# A preview has no INSERT to learn from, so it has to remember what it already
# previewed or it double-counts duplicates that a real run would skip (bug-070).
# Added while refactoring #287: dropping the `else` branch that populates those
# sets leaves the DB untouched and every other import scenario green, so nothing
# else in the matrix watches this.
@scenario("import-dry-run-intra-file-duplicates", "admin-handlers",
          "import: a preview dedups WITHIN the file, on both the content and msg_id axes (bug-070)",
          seed=_seed_import_target)
async def _(ctx):
    path = ctx.path("dupes.jsonl")
    write_jsonl(path, [
        {"_type": "memory", "agent_id": "imp", "content": "repeated body"},
        {"_type": "memory", "agent_id": "imp", "content": "repeated body"},
        {"_type": "memory", "agent_id": "imp", "msg_id": "mid-x", "content": "first under mid-x"},
        {"_type": "memory", "agent_id": "imp", "msg_id": "mid-x", "content": "second under mid-x"},
    ])
    return await admin_handlers.do_import_memories(path, target_agent_id="imp", dry_run=True)


@scenario("import-msgid-collision", "admin-handlers","import: an existing msg_id is skipped, the stored row is not overwritten", seed=_seed_import_target, unstable_row_ids=True)
async def _(ctx):
    path = ctx.path("collide.jsonl")
    write_jsonl(path, [
        {"_type": "memory", "agent_id": "imp", "msg_id": "mid-1", "content": "edited text"},
        {"_type": "memory", "agent_id": "imp", "msg_id": "mid-3", "content": "new text"},
    ])
    return await admin_handlers.do_import_memories(path, target_agent_id="imp")


@scenario("import-content-collision", "admin-handlers", "import: duplicate content is absorbed by the UNIQUE index, not counted twice", seed=_seed_import_target, unstable_row_ids=True)
async def _(ctx):
    path = ctx.path("dupe.jsonl")
    write_jsonl(path, [
        {"_type": "memory", "agent_id": "imp", "content": "original text"},
        {"_type": "memory", "agent_id": "imp", "content": "same body twice"},
        {"_type": "memory", "agent_id": "imp", "content": "same body twice"},
    ])
    return await admin_handlers.do_import_memories(path, target_agent_id="imp")


@scenario("import-truncated", "admin-handlers", "import: a header/row-count mismatch aborts and rolls back (bug-091/110)", seed=_seed_import_target)
async def _(ctx):
    path = ctx.path("short.jsonl")
    write_jsonl(
        path,
        [{"_type": "memory", "agent_id": "imp", "content": f"row {i}"} for i in range(2)],
        header={"_type": "header", "counts": {"memories": 5, "episodes": 0, "profiles": 0}},
    )
    return await admin_handlers.do_import_memories(path, target_agent_id="imp")


@scenario("import-torn-line", "admin-handlers", "import: a malformed line is reported and the transaction rolls back", seed=_seed_import_target)
async def _(ctx):
    path = ctx.path("torn.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"_type": "memory", "agent_id": "imp", "content": "good row"}) + "\n")
        f.write('{"_type": "memory", "agent_id": "imp", "cont')
    return await admin_handlers.do_import_memories(path, target_agent_id="imp")


@scenario("import-missing-file", "admin-handlers", "import: a nonexistent path fails cleanly", seed=_seed_import_target)
async def _(ctx):
    return await admin_handlers.do_import_memories(ctx.path("nope.jsonl"), target_agent_id="imp")


@scenario("import-retarget", "admin-handlers", "import: target_agent_id overrides the agent_id in the file", seed=_seed_import_target)
async def _(ctx):
    path = ctx.path("retarget.jsonl")
    write_jsonl(path, [
        {"_type": "memory", "agent_id": "someone-else", "content": "belongs to the target now"},
        {"_type": "episode", "agent_id": "someone-else", "summary": "retargeted episode", "keywords": []},
    ])
    return await admin_handlers.do_import_memories(path, target_agent_id="imp")


@scenario("import-preserves-axes", "admin-handlers", "import: γ axes and locked survive the round trip", seed=_seed_import_target)
async def _(ctx):
    path = ctx.path("axes.jsonl")
    write_jsonl(path, [
        {"_type": "memory", "agent_id": "imp", "content": "scoped row", "project_id": "p1",
         "channel": "chat", "locked": 1, "metadata": {"k": "v"}, "source": {"id": "discord:7"}},
    ])
    return await admin_handlers.do_import_memories(path, target_agent_id="imp")


# --- do_merge_memories ------------------------------------------------------


async def _seed_merge(ctx: Ctx) -> None:
    db = ctx.db
    await _mem(db, agent="src", content="only in source", seq=1)
    await _mem(db, agent="src", content="shared body", seq=2)
    await _mem(db, agent="src", project="p1", channel="chat", content="scoped source row", locked=1, seq=3)
    await _mem(db, agent="dst", content="shared body", seq=4)
    await _mem(db, agent="dst", content="only in target", seq=5)
    await _ep(db, agent="src", summary="source episode", seq=6)
    await db.execute(
        "INSERT INTO profiles (agent_id, content, updated_at) VALUES ('src', 'source profile', '2026-01-01T00:00:07Z')"
    )
    await db.commit()


@scenario("merge-copy-skip", "admin-handlers", "merge: copy+skip leaves the source intact and dedups the shared row", seed=_seed_merge, unstable_row_ids=True)
async def _(ctx):
    return await admin_handlers.do_merge_memories("src", "dst")


@scenario("merge-move", "admin-handlers", "merge: move deletes the source rows in the same transaction", seed=_seed_merge, unstable_row_ids=True)
async def _(ctx):
    return await admin_handlers.do_merge_memories("src", "dst", mode="move")


@scenario("merge-dry-run", "admin-handlers", "merge: dry_run reports the counts and touches nothing", seed=_seed_merge, unstable_row_ids=True)
async def _(ctx):
    return await admin_handlers.do_merge_memories("src", "dst", dry_run=True)


@scenario("merge-move-dry-run", "admin-handlers", "merge: a move preview must not delete the source either", seed=_seed_merge, unstable_row_ids=True)
async def _(ctx):
    return await admin_handlers.do_merge_memories("src", "dst", mode="move", dry_run=True)


@scenario("merge-empty-source", "admin-handlers", "merge: an agent with no rows is a no-op, not an error", seed=_seed_merge, unstable_row_ids=True)
async def _(ctx):
    return await admin_handlers.do_merge_memories("ghost", "dst")


@scenario("merge-into-self", "admin-handlers", "merge: source == target", seed=_seed_merge, unstable_row_ids=True)
async def _(ctx):
    return await admin_handlers.do_merge_memories("src", "src")


# --- do_calibrate_threshold -------------------------------------------------


@scenario("calibrate-basic", "admin-handlers", "calibrate: the null distribution, the derived threshold, and the in-place mutation",
          seed=lambda ctx: seed_calibration(ctx, n=30))
async def _(ctx):
    install_local(ctx)
    return await admin_handlers.do_calibrate_threshold("cal")


# The only scenario whose corpus (30) exceeds its sample (12), so the draw is a
# genuine random subset. `sampled_embeddings` and `num_pairs` -- the cap actually
# taking effect, which is what the scenario is for -- stay pinned; the statistics
# computed from the subset cannot be.
@scenario("calibrate-sample-cap", "admin-handlers", "calibrate: an explicit sample_size bounds the draw",
          seed=lambda ctx: seed_calibration(ctx, n=30),
          volatile=("distribution", "new_threshold", "old_threshold", "null_admit_rate",
                    "pos_admit_rate", "pos_mean", "youden_j", "separation", "thresholds"))
async def _(ctx):
    install_local(ctx)
    return await admin_handlers.do_calibrate_threshold("cal", sample_size=12)


@scenario("calibrate-percentile", "admin-handlers", "calibrate: the percentile method rather than the z-score default",
          seed=lambda ctx: seed_calibration(ctx, n=30))
async def _(ctx):
    install_local(ctx)
    return await admin_handlers.do_calibrate_threshold("cal", method="percentile", percentile=95.0)


@scenario("calibrate-too-few", "admin-handlers", "calibrate: below the raw sample floor, refuse and leave the threshold unset",
          seed=lambda ctx: seed_calibration(ctx, n=4))
async def _(ctx):
    install_local(ctx)
    return await admin_handlers.do_calibrate_threshold("cal")


@scenario("calibrate-ragged", "admin-handlers", "calibrate: the post-dimension-filter floor, the second of the two",
          seed=lambda ctx: seed_calibration(ctx, n=8, ragged=6))
async def _(ctx):
    install_local(ctx)
    return await admin_handlers.do_calibrate_threshold("cal")


@scenario("calibrate-no-corpus", "admin-handlers", "calibrate: an agent with nothing to sample")
async def _(ctx):
    install_local(ctx)
    return await admin_handlers.do_calibrate_threshold("empty-agent")


# --- do_export_memories (round-trip partner of import) ----------------------


@scenario("export-roundtrip", "admin-handlers", "export writes what import reads: the file survives its own reader", seed=seed_corpus)
async def _(ctx):
    path = ctx.path("export.jsonl")
    exported = await admin_handlers.do_export_memories("a1", path, include_embeddings=True)
    with open(path, encoding="utf-8") as f:
        line_types = [json.loads(line).get("_type") for line in f if line.strip()]
    reimported = await admin_handlers.do_import_memories(path, target_agent_id="roundtrip")
    return {"exported": exported, "line_types": line_types, "reimported": reimported}


# --- do_store ---------------------------------------------------------------
#
# 2.5.2b1 breaks the store contract at this layer, and the mutation harness
# (`scripts/mutation-proof.py`) does not touch `do_store` / `do_recall` / the
# memory-handlers path at all -- verified `grep -cE 'do_store|do_recall|
# memory_handlers' tests/behaviour_252.py scripts/mutation-proof.py` was 0 / 0
# before this file added the pins below. The scenarios here are recordings of
# pre-break behaviour, one branch each, in the same style as the #286/#287
# blocks above.


async def _seed_store_target(ctx: Ctx) -> None:
    """A minimal pre-existing row so a store-scenario can collide with it.

    Kept intentionally small: one memory for `s1`, no msg_id, no source. The
    scenarios that need a msg_id collision plant their own seed row inline so
    the pin includes the msg_id text.
    """
    db = ctx.db
    await _mem(db, agent="s1", content="prior body", seq=1)
    await db.commit()


@scenario("store-local-basic", "store-recall-health", "store: fresh write, local mode — {ok, id, embedded:true}, row lands, no outbound")
async def _(ctx):
    install_local(ctx)
    return await memory_handlers.do_store(
        "s1", {"content": "fresh entry", "timestamp": "2026-07-22T00:00:00+00:00"}
    )


# The remote push is fire-and-forget in production but the payload it carries
# is the whole point of a "remote mode" store contract, so we pin the body
# (namespace / items) AND the timeout kwargs the handler chose to pass. Note
# `install_remote` only patches `vector.VECTOR_SEARCH_MODE`; do_store reads the
# same name off the `cpersona.config` binding it imported into
# `memory_handlers` at module load, so the second patch below is what actually
# takes the remote branch.
@scenario("store-remote-basic", "store-recall-health", "store: fresh write, remote mode — /index POST body (namespace/items), embedded:true")
async def _(ctx):
    install_remote(ctx, {})
    ctx.patch(memory_handlers, "VECTOR_SEARCH_MODE", "remote")
    return await memory_handlers.do_store(
        "s1", {"content": "remote fresh entry", "timestamp": "2026-07-22T00:00:00+00:00"}
    )


# Missing and empty content both hit `raw_content = message.get("content", "")`
# and produce the same skipped-response, so one scenario pins both.
@scenario("store-empty-content", "store-recall-health", "store: content missing or empty string — rejected 'empty content', no row")
async def _(ctx):
    install_local(ctx)
    return await memory_handlers.do_store("s1", {"content": ""})


# `_sanitize_content` strips the `[Memory from ...]` annotation, leaving an
# empty string that trips the SECOND skip branch. Whitespace-only after strip
# hits the same branch; picking the annotation form so a reader can see WHICH
# sanitizer step reduced it.
@scenario("store-sanitized-empty", "store-recall-health", "store: content that sanitizes to empty ([Memory from …] only) — rejected 'empty after sanitization'")
async def _(ctx):
    install_local(ctx)
    return await memory_handlers.do_store(
        "s1", {"content": "[Memory from x] ", "timestamp": "2026-07-22T00:00:00+00:00"}
    )


# v2.5.2 additive: the skip response echoes the pre-existing row's id so a
# caller can chain (e.g. update_memory) without a second SELECT. The msg_id
# branch pins that echo AND the "duplicate msg_id" reason string.
@scenario("store-duplicate-msg-id", "store-recall-health", "store: existing msg_id — skipped 'duplicate msg_id', echoes existing id (v2.5.2 additive)")
async def _(ctx):
    install_local(ctx)
    # Plant a row that carries a msg_id (the standard _mem helper leaves it "").
    await ctx.db.execute(
        f"INSERT INTO memories ({_MEM_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "s1", "", "", "m-1", "row under m-1", "{}",
            "2026-01-05T00:00:00Z", "{}", pack("row under m-1"), 0,
            "2026-01-05T00:00:00Z",
        ),
    )
    await ctx.db.commit()
    return await memory_handlers.do_store(
        "s1",
        {"id": "m-1", "content": "different body under same msg_id",
         "timestamp": "2026-07-22T00:00:00+00:00"},
    )


@scenario("store-duplicate-content", "store-recall-health", "store: existing content — skipped 'duplicate content', echoes existing id", seed=_seed_store_target)
async def _(ctx):
    install_local(ctx)
    return await memory_handlers.do_store(
        "s1", {"content": "prior body", "timestamp": "2026-07-22T00:00:00+00:00"}
    )


# The truncation flag is derived from the RAW length, but the STORED content is
# the sanitized/truncated body -- pin both so a code move that swaps the two
# orders is caught.
#
# The cap is now an explicit input rather than whatever config says
# today. It used to be read live (config.MAX_CONTENT_LENGTH + 1), which made the
# cap's VALUE an unrecorded input to a recorded observation: raising the default
# from 2000 to 16000 changed the stored body and its embedding, and the scenario
# went red for a reason that has nothing to do with the code move it guards. The
# golden cannot be regenerated to match (it is the pre-refactor record, and a
# golden captured after the code it checks agrees by construction), so the input
# is pinned to the cap the recording was made under instead. Changing the
# default cap must not silently rewrite what "unchanged behaviour" means.
_GOLDEN_CONTENT_CAP = 2000


@scenario("store-truncated", "store-recall-health", "store: content > MAX_CONTENT_LENGTH — truncated:true AND the row stores the truncated body")
async def _(ctx):
    install_local(ctx)
    # Patch where the cap is READ (utils holds its own binding), not where it is
    # defined: bug-175 removed the re-export config -> utils, so setting it on
    # config alone would leave the sanitiser on the live default.
    ctx.patch(utils, "MAX_CONTENT_LENGTH", _GOLDEN_CONTENT_CAP)
    long_content = "y" * (_GOLDEN_CONTENT_CAP + 1)
    return await memory_handlers.do_store(
        "s1", {"content": long_content, "timestamp": "2026-07-22T00:00:00+00:00"}
    )


# No timestamp on the message: `message.get("timestamp", datetime.now(...)
# .isoformat())` fires and the frozen 2027-01-01 lands verbatim in the row. The
# ISO-with-T shape is NOT collapsed by _GENERATED_TS so a code move that changes
# the default source (e.g. drops the tz suffix) shifts the pinned literal.
@scenario("store-timestamp-default", "store-recall-health", "store: timestamp omitted — frozen datetime.now default lands as ISO-with-T in the row")
async def _(ctx):
    install_local(ctx)
    return await memory_handlers.do_store("s1", {"content": "unstamped"})


# Source-normalization seam (write path). Three scenarios pin
# three distinct classes of the mapping table -- the ones b1-2 rewrites:
#   (a) bare-string alias    "assistant" → {"type":"Agent","id":"","name":""}
#   (b) bare-string UNKNOWN  "claude-code" → stored verbatim (JSON string).
#       This is what the "anonymous_source" contract calls out: unknown shapes
#       are NOT fabricated a type for. The task brief listed "claude-code" as
#       an example of a normalized bare string; on the current code it lands
#       in the verbatim class instead (`_BARE_STRING_ALIASES` only covers
#       user/assistant/ai). The recording matches the CODE, not the brief.
#   (c) Rust serde tagged    {"User": "u-1"} → {"type":"User", ...}
@scenario("store-source-normalize-bare-alias", "store-recall-health", "store: source='assistant' bare-string alias — normalized to canonical Agent dict")
async def _(ctx):
    install_local(ctx)
    return await memory_handlers.do_store(
        "s1", {"content": "bare alias", "source": "assistant",
               "timestamp": "2026-07-22T00:00:00+00:00"}
    )


@scenario("store-source-verbatim-bare-unknown", "store-recall-health", "store: source='claude-code' bare-string unknown — stored verbatim (anonymous-source contract)")
async def _(ctx):
    install_local(ctx)
    return await memory_handlers.do_store(
        "s1", {"content": "bare unknown", "source": "claude-code",
               "timestamp": "2026-07-22T00:00:00+00:00"}
    )


@scenario("store-source-normalize-serde-tagged", "store-recall-health", "store: source={'User': 'u-1'} Rust serde form — normalized to canonical User dict")
async def _(ctx):
    install_local(ctx)
    return await memory_handlers.do_store(
        "s1", {"content": "serde tagged", "source": {"User": "u-1"},
               "timestamp": "2026-07-22T00:00:00+00:00"}
    )


# bug-106: dedup probes the γ-VISIBLE scope, matching read semantics. A bucket
# write ('p1') sees X ∪ '' and so collides with an identical global-pool row;
# a global write probes '' only and does NOT collide with a bucket copy. The
# two scenarios below are the mirror pair the comment at memory_handlers.py:99
# specifies. Both use `_seed_store_target` (global "prior body") plus an
# inline bucket seed where needed, so the collision axis is the only thing
# changing between them.
@scenario("store-gamma-bucket-collides-global", "store-recall-health", "store: bucket write ('p1') collides with an identical global-pool row (bug-106 γ-visible scope)", seed=_seed_store_target)
async def _(ctx):
    install_local(ctx)
    return await memory_handlers.do_store(
        "s1", {"content": "prior body", "timestamp": "2026-07-22T00:00:00+00:00"},
        project_id="p1",
    )


@scenario("store-gamma-global-not-collides-bucket", "store-recall-health", "store: global write does NOT collide with a bucket-only row (bug-106 γ-visible scope)")
async def _(ctx):
    install_local(ctx)
    # Plant a bucket-only row (no global copy).
    await _mem(ctx.db, agent="s1", project="p1", content="isolated body", seq=1)
    await ctx.db.commit()
    return await memory_handlers.do_store(
        "s1", {"content": "isolated body", "timestamp": "2026-07-22T00:00:00+00:00"}
    )


# `_reset` calls `session.reset_pauses_for_tests()` before the scenario body runs, so the
# pause() below always fires on a clean state. Frozen clock makes the "reason"
# TTL suffix deterministic ("30m left" for ttl=1800).
@scenario("store-no-persist-paused", "store-recall-health", "store: no_persist paused — {id:'no-persist', persisted:false, dry_run:true, reason:'…30m left…'} shape (bug-141)")
async def _(ctx):
    install_local(ctx)
    session.pause_for(session.TRANSPORT_KEY, False, 1800)
    return await memory_handlers.do_store(
        "s1", {"content": "should not persist", "timestamp": "2026-07-22T00:00:00+00:00"}
    )


# --- do_recall --------------------------------------------------------------
#
# do_recall is the read hot path but it also WRITES: it bumps recall_count and
# last_recalled_at on every returned memory (see memory_handlers.py:1050).
# Under the DEFAULT config (`CONFIDENCE_ENABLED=false`) that write never fires
# -- `_apply_recall_scoring` only populates `recall_counts` inside `if
# CONFIDENCE_ENABLED:`, and the bump is gated on `recall_counts`. So the pin
# for the read-path write, AND the pin for the `confidence` dict in messages,
# both require the confidence branch. Two flavours below:
#
#   recall-basic-hits / recall-exclude-contents / recall-deep-no-decay-no-bump
#       CONFIDENCE_ENABLED=True — full match_reason (signal="confidence"),
#       confidence dict in messages, recall_count bump captured in the db dump.
#
#   recall-empty-query-pure-recency / recall-no-hits
#       CONFIDENCE_ENABLED=False (module default) — the branches they pin are
#       pre-scoring (empty-query volume-rule bypass, no-hits early return),
#       so enabling confidence would only add noise.
#
# _install_confidence_on is preferred over env-poking because config.py reads
# the env at import time and rebinds nothing after; memory_handlers imports
# CONFIDENCE_ENABLED by value, so the patch is a module-attribute swap.


def _install_confidence_on(ctx: Ctx) -> None:
    ctx.patch(memory_handlers, "CONFIDENCE_ENABLED", True)


@scenario("recall-basic-hits", "store-recall-health", "recall: seed_corpus hit set — messages, confidence, match_reason, refs; recall_count/last_recalled_at bumped (CONFIDENCE=on)", seed=seed_corpus)
async def _(ctx):
    install_local(ctx)
    _install_confidence_on(ctx)
    return await memory_handlers.do_recall("a1", "apples", 5)


# bug-125: an empty query is a pure-recency listing with no relevance signal,
# so the unscored volume rule is bypassed. Without the bypass, session-start
# recall would return [] for every agent with < 100 memories.
@scenario("recall-empty-query-pure-recency", "store-recall-health", "recall: empty query bypasses the unscored volume rule (bug-125) — pure recency listing", seed=seed_corpus)
async def _(ctx):
    install_local(ctx)
    return await memory_handlers.do_recall("a1", "", 5)


# The exclude filter is normalized (`c.strip().lower()`) so an exclude entry
# with different casing must still match; using the exact seed body avoids
# baking that separate invariant into this scenario -- one branch per
# scenario, in the existing file's style. Confidence on so the pin includes
# the `confidence` dict on every SURVIVING message.
@scenario("recall-exclude-contents", "store-recall-health", "recall: exclude_contents drops a matching row before ranking (CONFIDENCE=on)", seed=seed_corpus)
async def _(ctx):
    install_local(ctx)
    _install_confidence_on(ctx)
    return await memory_handlers.do_recall(
        "a1", "apples", 5,
        exclude_contents=["apples and pears in the orchard"],
    )


# deep=True disables BOTH time_decay and the completion_factor in
# _compute_confidence, AND skips the recall_count bump at the tail of
# do_recall. Pinning both effects in one scenario -- the score change is in
# `result` (via the confidence dict), the bump-skip is in `db`.
#
# With confidence=on, `recall_counts` populates from the SELECT so the
# skip-under-deep branch is the one being exercised; under default config the
# bump is skipped for a DIFFERENT reason (recall_counts empty). Confidence-on
# is what makes this scenario specifically pin the "deep skips the bump" wire.
@scenario("recall-deep-no-decay-no-bump", "store-recall-health", "recall: deep=True disables time_decay/completion_factor AND skips the recall_count bump (CONFIDENCE=on)", seed=seed_corpus)
async def _(ctx):
    install_local(ctx)
    _install_confidence_on(ctx)
    return await memory_handlers.do_recall("a1", "apples", 5, deep=True)


# Query with no lexical or vector matches on the seeded corpus. The rrf mode
# vector branch will still produce candidate cosines below the threshold; the
# FTS branch returns 0 rows for the nonsense trigram. Fused score is empty,
# so `messages: []` and no recall_count bump. Default confidence: the empty-
# result branch is what's under test, no need to enable scoring on it.
@scenario("recall-no-hits", "store-recall-health", "recall: a query matching no row returns messages=[] and does not touch recall_count", seed=seed_corpus)
async def _(ctx):
    install_local(ctx)
    return await memory_handlers.do_recall("a1", "xyzzyxyzzy", 5)


# --- do_recall_with_context -------------------------------------------------
#
# Absent from the matrix until 2026-09-04, which left the merge-and-sort tail of
# recall_with_context (memory_handlers._ts_sort_key onwards) unprotected. The
# four scenarios below record what it does TODAY, including the property the
# temporal-ordering work is about to change on purpose: `messages` is sorted by
# the raw timestamp STRING, so entries whose UTC offsets differ come back in
# byte order rather than in instant order. The fixture is built so the two
# orders disagree -- a fixture where they agree cannot tell the two apart, and
# a golden recorded from it would stay green through the fix. When that fix
# lands, the diff in `rwc-mixed-offset` (and only there) is the review surface.


def _ctx_user(content, ts, *, name="alice", user_id="u-1"):
    return {"role": "user", "name": name, "user_id": user_id, "content": content, "timestamp": ts}


def _ctx_assistant(content, ts):
    return {"role": "assistant", "content": content, "timestamp": ts}


@scenario("rwc-basic", "store-recall-health", "recall_with_context: user (name+user_id) and assistant entries, UTC Z stamps — merged with the seed hits and sorted; source shapes discord:<user_id> / Agent self", seed=seed_corpus)
async def _(ctx):
    install_local(ctx)
    return await memory_handlers.do_recall_with_context(
        "a1", "apples", limit=5,
        external_context=[
            _ctx_user("what about the apples in the orchard?", "2026-01-01T00:00:02Z"),
            _ctx_assistant("the orchard has apples and pears", "2026-01-01T00:00:06Z"),
        ],
    )


# Every stamp below names an instant inside the seed corpus's second-by-second
# window (00:00:01Z .. 00:00:09Z), but each is spelled so that its BYTE order
# differs from its instant order:
#   +09:00 spelling of 00:00:03Z sorts after every "…T00:00:0NZ" row ('9' > '0' at col 11)
#   -05:00 spelling of 00:00:07Z sorts before all of them (the date part is 2025)
#   +00:00 spelling of 00:00:05Z sorts before the seed row's "…05Z" ('+' < 'Z')
#   fractional 00:00:01.5Z sorts before "…01Z" ('.' < 'Z') though it is later
# The asserted disagreement is what makes this fixture able to fail.
_RWC_MIXED = [
    _ctx_user("apples at three, spelled in JST", "2026-01-01T09:00:03+09:00"),
    _ctx_assistant("apples at seven, spelled in EST", "2025-12-31T19:00:07-05:00"),
    _ctx_user("apples at five, spelled with +00:00", "2026-01-01T00:00:05+00:00", name="bob", user_id="u-2"),
    _ctx_assistant("apples at one and a half, fractional", "2026-01-01T00:00:01.500000Z"),
]


def _rwc_mixed_orders_disagree() -> None:
    """Fail loudly if someone edits _RWC_MIXED into a fixture that cannot fail."""
    from cpersona.utils import _parse_timestamp_utc

    stamps = [e["timestamp"] for e in _RWC_MIXED]
    by_bytes = sorted(stamps)
    by_instant = sorted(stamps, key=_parse_timestamp_utc)
    assert by_bytes != by_instant, "rwc-mixed-offset fixture: byte order equals instant order -- cannot discriminate"


@scenario("rwc-mixed-offset", "store-recall-health", "recall_with_context: external_context stamps in Z / +00:00 / +09:00 / -05:00 / fractional — TODAY sorted by raw string (byte order), which the temporal-ordering fix will change", seed=seed_corpus)
async def _(ctx):
    _rwc_mixed_orders_disagree()
    install_local(ctx)
    return await memory_handlers.do_recall_with_context(
        "a1", "apples", limit=5, external_context=list(_RWC_MIXED),
    )


@scenario("rwc-invalid-timestamp-mixed", "store-recall-health", "recall_with_context: entries with an empty, an unparseable and a missing timestamp among valid ones — TODAY '' sorts first, garbage sorts by its bytes", seed=seed_corpus)
async def _(ctx):
    install_local(ctx)
    return await memory_handlers.do_recall_with_context(
        "a1", "apples", limit=5,
        external_context=[
            _ctx_user("apples with an empty stamp", ""),
            _ctx_assistant("apples with a garbage stamp", "not-a-timestamp"),
            {"role": "user", "content": "apples with no stamp at all"},
            _ctx_user("apples with a good stamp", "2026-01-01T00:00:04Z"),
        ],
    )


@scenario("rwc-filter-only-roles", "store-recall-health", "recall_with_context: a system entry repeating a seed row's body suppresses that memory and is disclosed in context_filter_only (audit C13), never merged into messages", seed=seed_corpus)
async def _(ctx):
    install_local(ctx)
    return await memory_handlers.do_recall_with_context(
        "a1", "apples", limit=5,
        external_context=[
            {"role": "system", "content": "apples and pears in the orchard", "timestamp": "2026-01-01T00:00:00Z"},
            {"role": "tool", "content": "raspberry pi cluster wiring"},
        ],
    )


# --- corpus shapes the write path cannot refuse yet ---------------------------
#
# Two things a production corpus can hold that no scenario above seeds: an
# embedding whose floats are not finite, and timestamps spelled in several
# ISO-8601 conventions at once. Neither is refused at the write seam today. The
# scenarios below record what every READ surface does with them -- ranking,
# the contiguous index, check_health, export -- so that the boundary work can
# show, per surface, what it changed and what it left alone. Rows are inserted
# the way the write path writes them (float32 blob, ISO text), not through a
# test-only shortcut.

_NONFINITE_AGENT = "nf1"
_SPELLINGS_AGENT = "ts1"


async def _mem_raw(db, *, agent, content, timestamp, created_at, blob):
    await db.execute(
        f"INSERT INTO memories ({_MEM_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (agent, "", "", "", content, _HEALTH_SOURCE, timestamp, "{}", blob, 0, created_at),
    )


async def _profile_for(db, agent: str) -> None:
    await db.execute(
        "INSERT INTO profiles (agent_id, content, updated_at) VALUES (?, 'profile body', '2026-01-01T00:00:00Z')",
        (agent,),
    )


def _vec_with(value: float, *, at: int | None = None) -> bytes:
    """A unit vector for 'apples', with `value` written over every element (at=None)
    or over one element (at=i). float32 round-trips NaN and +/-inf exactly."""
    base = fake_embed_one("apples in the orchard")
    if at is None:
        vec = [value] * FAKE_DIM
    else:
        vec = list(base)
        vec[at] = value
    return EmbeddingClient.pack_embedding(vec)


async def _seed_nonfinite(ctx: Ctx) -> None:
    db = ctx.db
    nan, inf = float("nan"), float("inf")
    rows = [
        ("apples, a finite vector", 1, pack("apples, a finite vector")),
        ("apples, every element NaN", 2, _vec_with(nan)),
        ("apples, every element +inf", 3, _vec_with(inf)),
        ("apples, one element NaN", 4, _vec_with(nan, at=7)),
        ("apples, one element -inf", 5, _vec_with(-inf, at=7)),
        ("apples, another finite vector", 6, pack("apples, another finite vector")),
    ]
    for content, seq, blob in rows:
        # created_at in the fixed-width form the contiguous index requires
        # (vector_index._is_canonical) -- with any other spelling the builder
        # declines and the index scenario would only re-record the scan.
        await _mem_raw(
            db, agent=_NONFINITE_AGENT, content=content,
            timestamp=f"2026-01-01T00:00:{seq:02d}Z",
            created_at=f"2026-01-01 00:00:{seq:02d}", blob=blob,
        )
    await _profile_for(db, _NONFINITE_AGENT)
    await db.commit()


@scenario("corpus-nonfinite-recall-scan", "store-recall-health", "recall over a corpus holding NaN / +inf / -inf embeddings (live SQL scan, CONFIDENCE=on): which rows rank, which vanish, and what their scores read", seed=_seed_nonfinite)
async def _(ctx):
    install_local(ctx)
    _install_confidence_on(ctx)
    return await memory_handlers.do_recall(_NONFINITE_AGENT, "apples", 6)


@scenario("corpus-nonfinite-recall-index", "store-recall-health", "the same corpus through the contiguous index: build_index accepts non-finite rows, and the index-path recall answers as the scan does (or does not)", seed=_seed_nonfinite)
async def _(ctx):
    from cpersona import vector_index

    install_local(ctx)
    _install_confidence_on(ctx)
    path = vector_index.index_path("memories")
    try:
        built = await vector_index.build_index(ctx.db, "memories")
        recalled = await memory_handlers.do_recall(_NONFINITE_AGENT, "apples", 6)
        # The file sits beside the scratch database, whose directory differs
        # between the capture process and pytest's conftest; the path is not
        # the observation.
        built = {**built, "path": "<index>"} if "path" in built else built
        return {"built": built, "recalled": recalled}
    finally:
        # The index file sits beside the scratch database, outside `_reset`'s
        # reach; leaving it behind would feed every later scenario (and every
        # later test under pytest) an index built from this corpus.
        vector_index._cache.pop(path, None)
        if os.path.exists(path):
            os.remove(path)


@scenario("corpus-nonfinite-health", "store-recall-health", "check_health over the non-finite corpus: which checks (if any) notice a NaN or inf blob", seed=_seed_nonfinite)
async def _(ctx):
    return _mask_health_stats(await maintenance_handlers.do_check_health(agent_id=_NONFINITE_AGENT))


@scenario("corpus-nonfinite-export", "store-recall-health", "export with embeddings of the non-finite corpus: whether NaN/inf leave the process, and in what spelling", seed=_seed_nonfinite)
async def _(ctx):
    path = ctx.path("nonfinite.jsonl")
    exported = await admin_handlers.do_export_memories(_NONFINITE_AGENT, path, include_embeddings=True)
    import base64
    import math

    per_row = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            rec = json.loads(ln) if ln.strip() else None
            if not rec or rec.get("_type") != "memory":
                continue
            b64 = rec.get("embedding_b64")
            vec = EmbeddingClient.unpack_embedding(base64.b64decode(b64)) if b64 else []
            per_row.append(
                {
                    "content": rec.get("content"),
                    "dim": len(vec),
                    "nan": sum(1 for v in vec if v != v),
                    "inf": sum(1 for v in vec if math.isinf(v) and v > 0),
                    "neg_inf": sum(1 for v in vec if math.isinf(v) and v < 0),
                }
            )
    return {"exported": exported, "rows": per_row}


# Six spellings spread over the three weeks before the frozen clock, arranged
# so that the ends of the BYTE order and the ends of the INSTANT order fall on
# different rows, hours apart:
#   text MIN    = the -05:00 row (its date part reads 12-09) — instant 12-10 01:00Z
#   instant MIN = the early Z row                            — instant 12-10 00:00Z
#   text MAX    = the +09:00 row (reads 2027-01-01T02)        — instant 12-31 17:00Z
#   instant MAX = the late Z row                             — instant 01-01 00:00Z
# A span taken over the column as TEXT is therefore 8 h shorter than the span
# by instant. Three weeks, because the range only reaches the decay rate above
# REFERENCE_HOURS (one week) -- a narrower corpus scores identically under
# either span, which is how the first two versions of this fixture let a span
# mutation survive. created_at follows the instant, so the scan window sees
# instant order. The naive row is the form SQLite's datetime('now') writes and
# the drift check reports as unfixable.
_SPELLINGS = [
    ("apples spelled Z, early", "2026-12-10T00:00:00Z"),
    ("apples spelled -05:00", "2026-12-09T20:00:00-05:00"),
    ("apples spelled +00:00", "2026-12-15T00:00:00+00:00"),
    ("apples spelled naive", "2026-12-20 12:00:00"),
    ("apples spelled fractional Z", "2026-12-25T00:00:00.500000Z"),
    ("apples spelled +09:00", "2027-01-01T02:00:00+09:00"),
    ("apples spelled Z, late", "2027-01-01T00:00:00Z"),
]


def _spellings_orders_disagree() -> None:
    from cpersona.utils import _parse_timestamp_utc

    stamps = [ts for _, ts in _SPELLINGS]
    by_text = sorted(stamps)
    by_instant = sorted(stamps, key=_parse_timestamp_utc)
    assert by_text != by_instant, "spellings fixture cannot discriminate"
    # The ENDS must differ, by hours: the span is consumed in hours, and a
    # disagreement that rounds away is no disagreement.
    for a, b in ((by_text[0], by_instant[0]), (by_text[-1], by_instant[-1])):
        gap = abs((_parse_timestamp_utc(a) - _parse_timestamp_utc(b)).total_seconds())
        assert gap >= 3600, f"span ends {a!r} / {b!r} are {gap}s apart; need hours"


async def _seed_spellings(ctx: Ctx) -> None:
    _spellings_orders_disagree()
    db = ctx.db
    from cpersona.utils import _parse_timestamp_utc

    for content, ts in sorted(_SPELLINGS, key=lambda e: _parse_timestamp_utc(e[1])):
        instant = _parse_timestamp_utc(ts)
        await _mem_raw(
            db, agent=_SPELLINGS_AGENT, content=content, timestamp=ts,
            created_at=instant.strftime("%Y-%m-%dT%H:%M:%SZ"), blob=pack(content),
        )
    await _profile_for(db, _SPELLINGS_AGENT)
    await db.commit()


@scenario("corpus-mixed-spellings-recall-confidence", "store-recall-health", "recall (CONFIDENCE=on) over six timestamp spellings: the temporal span and every age_hours as computed today, with MIN/MAX taken over the column as TEXT", seed=_seed_spellings)
async def _(ctx):
    install_local(ctx)
    _install_confidence_on(ctx)
    return await memory_handlers.do_recall(_SPELLINGS_AGENT, "apples", 6)


@scenario("corpus-mixed-spellings-health", "store-recall-health", "check_health over six timestamp spellings: what timestamp_format_drift counts as utc / aware / naive", seed=_seed_spellings)
async def _(ctx):
    return _mask_health_stats(await maintenance_handlers.do_check_health(agent_id=_SPELLINGS_AGENT))


@scenario("corpus-mixed-spellings-export", "store-recall-health", "export preserves each timestamp spelling verbatim and in id order", seed=_seed_spellings)
async def _(ctx):
    path = ctx.path("spellings.jsonl")
    exported = await admin_handlers.do_export_memories(_SPELLINGS_AGENT, path, include_embeddings=False)
    with open(path, encoding="utf-8") as f:
        rows = [json.loads(ln) for ln in f if ln.strip()]
    stamps = [r.get("timestamp") for r in rows if r.get("_type") == "memory"]
    return {"exported": exported, "timestamps_in_file_order": stamps}


# --- bug-155 cosine backfill ------------------------------------------------
#
# Pins the FTS-only-hit backfill path directly. A lexically-matching row is
# kept out of the vector channel by packing its embedding blob from text
# DISJOINT from the query -- the vector channel's min_similarity threshold
# then excludes it, and only the FTS keyword channel admits it into the fused
# set. Under the UNFIXED code that row reaches ``_apply_recall_scoring`` with
# ``_cosine=None`` and ``_compute_confidence``'s None-branch scores it at
# ``sqrt(time_decay)`` -- an upper bound on the cosine branch, so it
# out-ranks a real vector hit.
#
# With the fix the row is backfilled from its stored blob (a low true
# cosine) and either sinks below the vector hit or is dropped by the quality
# gate. Either outcome is protected by this scenario: the golden captures
# exact match_reason.cosine values and the wire ordering.
#
# The four existing recall scenarios above (recall-basic-hits,
# recall-exclude-contents, recall-deep-no-decay-no-bump, and the two
# confidence-off ones) are seeded from ``seed_corpus``, whose only cosine-
# less rows on ``apples`` (mem:6 ragged-dim, mem:7 no-blob) are precisely
# the two cases the backfill DELIBERATELY does NOT fix -- so this addition
# is expected to leave those pins untouched.


async def _seed_bug155_backfill(ctx: Ctx) -> None:
    """One vector-strong row and one FTS-only row (blob DISJOINT from the
    query token). The FTS-only row is what makes ``_cosine=None`` reach
    ``_apply_recall_scoring`` deterministically under the local vector
    channel's default min_similarity."""
    db = ctx.db
    # Row 1: content and blob share the ``apples`` token -> strong vector hit.
    await _mem(db, agent="a374", content="apples orchard hit", seq=1)
    # Row 2: FTS-matches on ``apples`` in the content, but the blob is packed
    # from a text with no shared tokens -- vector cosine ~= dot of two
    # unrelated random unit vectors ~ 0, well below the min_similarity gate.
    await db.execute(
        f"INSERT INTO memories ({_MEM_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "a374", "", "", "",
            "apples zzz yyy www",
            "{}",
            "2026-01-01T00:00:02Z",
            "{}",
            pack("completely different unrelated content xxxx"),
            0,
            "2026-01-01T00:00:02Z",
        ),
    )
    await db.commit()


# The recorded result keeps ONE of the two seeded rows: the backfilled FTS-only row
# falls below the quality gate and is dropped. That drop is designed behaviour for a
# MIXED result and stays that way (bug-183 fix_note; the 2.6.0 membership-preserving
# scoring redesign owns any demotion tier). Only an ALL-blocked result is rescued, by
# the bug-183 `gate_fallback` path — which cannot fire here, because the vector-strong
# row survives. The rescue itself is pinned in tests/test_gate_remediation.py rather
# than by a new golden: a golden recorded after the change it guards agrees with the
# code by construction (see the header of tests/test_equivalence_252.py).
@scenario("recall-cosine-backfill", "cosine-backfill",
          "recall bug-155: FTS-only row whose stored blob is DISJOINT from the query gets a backfilled cosine (or is gated out) instead of out-scoring a real vector hit under CONFIDENCE_ENABLED — the drop recorded here is DESIGNED for a mixed result (bug-183); an all-blocked result is rescued instead by the gate_fallback path",
          seed=_seed_bug155_backfill)
async def _(ctx):
    install_local(ctx)
    _install_confidence_on(ctx)
    return await memory_handlers.do_recall("a374", "apples", 5, deep=True)


# --- do_check_health / do_deep_check ----------------------------------------
#
# 2.5.2b1 drops `healthy` from the check_health response in favour of the
# three-valued `status` (they currently COEXIST — commit 448acd4 added
# `status` additively). The mutation harness (`scripts/mutation-proof.py`) and
# this matrix's other scenarios do not touch check_health or deep_check at
# all -- `grep -c check_health` on both files was 0 before these pins. Without
# a golden entry that deletion could land unobserved.
#
# The scenarios below record, one branch each:
#
#   * `total_memories`, `issues`, `severity_summary`, `status`, `healthy`,
#     `fixed` and `stats` on a clean corpus (health-clean).
#   * the info-only asymmetry `maintenance_handlers.py:136` documents: an
#     info-only DB reports `status='healthy'` AND `healthy=False`, because
#     the boolean is `len(issues) == 0` while the status follows the gate
#     rule "info never degrades" (health-info-only-asymmetry). This is the
#     stated motivation for the b1 change, so both halves of the current
#     contract are on the record before it moves.
#   * a warn finding drives `status='degraded'` (health-warn-degraded).
#   * `fix=True` -- the bug-059 residual re-run derives healthy/status from
#     the post-commit state, and the survivor row is visible in the db dump
#     (health-fix-repairs-warn).
#   * `do_deep_check` on a clean agent -- the dispatch envelope
#     (agent_id / checks_run / results / fixed) and every subcheck's zero
#     shape (deep-check-clean).
#   * `do_deep_check(fix=True)` on a recoverable anonymous-source row --
#     the recovery counts and the rewritten source in the db dump
#     (deep-check-anonymous-source-fix). deep_check shares only the do_*
#     dispatch envelope with check_health (each subcheck's dict is its own
#     contract), but the same "envelope stability before the split"
#     argument applies -- 2.5.2b1 refactors both handlers.
#
# WALL CLOCK REACH. `_install_frozen_clock` (installed unconditionally by
# `observe`) already covers memory_handlers / utils / session. The health
# checks read the clock only via:
#
#   * SQL `datetime('now', ...)` in check_stale_pending_tasks and in
#     deep_stale_profile. The seeds below insert nothing into
#     pending_memory_tasks (empty -> count=0 -> the branch never renders a
#     timestamp) and never insert a profile with user_id=''  for the deep
#     agent (empty -> count=0 -> `last_updated` is not written). Both are
#     dormant, so the SQL wall clock does not reach the observation.
#
#   * Python `datetime.datetime.now(...)` in deep_calibration_staleness.
#     The subcheck short-circuits at `if not vector._embedding_client:
#     return {"status": "not_applicable", ...}` BEFORE any clock read, and
#     these scenarios deliberately do NOT install a client. So the clock
#     read is unreachable in practice.
#
# Extending `_install_frozen_clock` to `cpersona.checks` was considered and
# NOT done: the only Python clock read there is deep_calibration_staleness
# and it never fires under these scenarios. Adding a patch on a call site
# that no covered path reaches would enlarge the frozen-clock surface for
# no observable gain.


_HEALTH_AGENT = "h1"
_DEEP_AGENT = "d1"
# invalid_source_type / anonymous_source both fire on the module-default
# `source='{}'`; a proper User dict is the shape both checks treat as clean.
_HEALTH_SOURCE = '{"type":"User","id":"u","name":"n"}'


def _mask_health_stats(result: dict) -> dict:
    """Blank the file-level stats field that isn't per-scenario reproducible.

    `stats.db_size_bytes` is `PRAGMA page_count * page_size`. Page count is a
    file-scoped counter that grows monotonically with cumulative inserts and
    never shrinks (DELETE frees pages back into the freelist but does not
    truncate the file), so its value at any given scenario reflects
    everything the DB ever held under this process -- and the exact history
    differs between contexts:

    * capture-behaviour.py: one process, ONLY the SCENARIOS matrix, its own
      tmpdir DB. Reproducible run-to-run there.
    * pytest / test_equivalence_252.py: the DB is conftest's tmpdir, shared
      with every other test in the suite. Whatever ran first shaped the
      page count that this scenario sees, and pytest's collection order
      changes with any addition to the suite.

    Every OTHER stats field is agent-scoped (bug-058/bug-062) and therefore
    reproducible: `memories`, `episodes`, `profiles`, `pending_tasks`,
    `axes`, and (for a specific agent_id) `agent_memories` /
    `agent_episodes`. Only db_size_bytes needs masking. Setting it to a
    literal sentinel (rather than marking the whole `stats` key volatile
    through Scenario.volatile) keeps the surrounding stats fields pinned:
    the equivalence test compares against the literal, so an unrelated
    stats field going wrong is still surfaced.
    """
    stats = result.get("stats")
    if isinstance(stats, dict) and "db_size_bytes" in stats:
        stats["db_size_bytes"] = "<file-scoped, masked>"
    return result


async def _seed_health_profile(ctx: Ctx) -> None:
    """A profile row for _HEALTH_AGENT (kept explicit so `updated_at` isn't
    filled by SQLite's `datetime('now')` default -- that literal would leak
    a wall clock into the profiles dump).

    Every seed below composes on top of this: without a profile row the
    check_health response also carries a missing_profile info finding, and
    that would blur "one branch per scenario" for the info-only asymmetry
    scenario in particular.
    """
    await ctx.db.execute(
        "INSERT INTO profiles (agent_id, content, updated_at) "
        "VALUES (?, 'profile body', '2026-01-01T00:00:00Z')",
        (_HEALTH_AGENT,),
    )
    await ctx.db.commit()


async def _seed_health_clean(ctx: Ctx) -> None:
    """A single well-formed memory + a profile -- no health check trips."""
    await _mem(
        ctx.db, agent=_HEALTH_AGENT,
        content="a perfectly ordinary memory",
        source=_HEALTH_SOURCE, seq=1,
    )
    await _seed_health_profile(ctx)


async def _seed_health_info(ctx: Ctx) -> None:
    """One `[Memory from …]` memory -- check_memory_annotation
    (base_severity=info) is the only check that fires."""
    await _mem(
        ctx.db, agent=_HEALTH_AGENT,
        content="[Memory from bob] a note",
        source=_HEALTH_SOURCE, seq=1,
    )
    await _seed_health_profile(ctx)


async def _seed_health_warn(ctx: Ctx) -> None:
    """Two rows sharing `(agent_id, project_id, content)` across channels --
    the cross-channel duplicate check_duplicate_content owns (bug-014).

    The v12 UNIQUE index keys on `(agent_id, project_id, channel, content)`,
    so identical content in DIFFERENT channels passes the write-time index
    yet still lands in one health-check duplicate group.
    """
    await _mem(ctx.db, agent=_HEALTH_AGENT, channel="c1",
               content="duplicated body", source=_HEALTH_SOURCE, seq=1)
    await _mem(ctx.db, agent=_HEALTH_AGENT, channel="c2",
               content="duplicated body", source=_HEALTH_SOURCE, seq=2)
    await _seed_health_profile(ctx)


async def _seed_deep_anonymous_source(ctx: Ctx) -> None:
    """A memory whose source is anonymous (`User/id=''/name=''`) and whose
    content matches `_USERNAME_PREFIX_PATTERN` -- the fix-capable branch of
    `deep_anonymous_source`.

    Not `[Memory from …]` (that would also trip check_memory_annotation);
    `[bob] …` matches `^\\[(.+?)\\]\\s` without matching
    `_MEMORY_ANNOTATION_PATTERN`, so the row surfaces cleanly through
    deep_check alone.
    """
    anon_source = '{"type":"User","id":"","name":""}'
    await ctx.db.execute(
        f"INSERT INTO memories ({_MEM_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            _DEEP_AGENT, "", "", "", "[bob] hello", anon_source,
            "2026-01-01T00:00:00Z", "{}", pack("[bob] hello"), 0,
            "2026-01-01T00:00:00Z",
        ),
    )
    await ctx.db.commit()


@scenario("health-clean", "store-recall-health",
          "check_health: clean corpus — full response shape (status='healthy', severity_summary zeros)",
          seed=_seed_health_clean)
async def _(ctx):
    return _mask_health_stats(
        await maintenance_handlers.do_check_health(agent_id=_HEALTH_AGENT)
    )


# Info counts are observations, not gate signals (checks.py:1183). An
# info-only finding therefore leaves status='healthy' while healthy=False
# (len(issues)==0 is falsified) -- the asymmetry maintenance_handlers.py:136
# calls out. That's the concrete pre-b1 contract this scenario pins.
@scenario("health-info-only-asymmetry", "store-recall-health",
          "check_health: info-only finding (memory_annotation) — status stays 'healthy' (the asymmetry the retired healthy boolean contradicted, motivating the b1 single verdict)",
          seed=_seed_health_info)
async def _(ctx):
    return _mask_health_stats(
        await maintenance_handlers.do_check_health(agent_id=_HEALTH_AGENT)
    )


# The cross-channel duplicate is a warn finding, so status='degraded' and
# healthy=False -- both come from the (issues, severity_summary) round trip
# through health_status(). Fix=False keeps the observation to detection
# only; the fix scenario below handles the mutation half.
@scenario("health-warn-degraded", "store-recall-health",
          "check_health: warn finding (duplicate_content cross-channel, bug-014) — severity_summary.warn=1, status='degraded'",
          seed=_seed_health_warn)
async def _(ctx):
    return _mask_health_stats(
        await maintenance_handlers.do_check_health(agent_id=_HEALTH_AGENT)
    )


# fix=True triggers the bug-059 residual re-run, which recomputes
# (issues, severity_summary) from the POST-commit state. The duplicate is
# gone so the residual sees no findings, and healthy/status flip to
# healthy=True/'healthy'. The survivor row and the deleted collider are
# visible in the observation's `db` dump.
@scenario("health-fix-repairs-warn", "store-recall-health",
          "check_health: fix=True on the warn corpus — fixed=True; bug-059 residual re-run yields status='healthy'; the survivor row is visible in db",
          seed=_seed_health_warn)
async def _(ctx):
    return _mask_health_stats(
        await maintenance_handlers.do_check_health(agent_id=_HEALTH_AGENT, fix=True)
    )


# The deep_check envelope (`agent_id`, `checks_run`, `results`, `fixed`)
# and each subcheck's zero shape:
#   anonymous_source     {'recoverable':0,'unrecoverable':0}
#   short_content        {'count':0}
#   stale_profile        {'count':0,'threshold_days':30}
#   orphaned_episodes    {'count':0}
#   calibration_staleness{'status':'not_applicable',
#                        'reason':'no embedding client configured'}
#   near_duplicate       {'pairs':0,'rows_scanned':0}
# `fix` is absent from the individual result dicts under fix=False (the
# `if fix:` blocks in each runner gate it), which is the difference between
# this scenario and the fix-True one below.
@scenario("deep-check-clean", "store-recall-health",
          "deep_check: envelope + zero shape of every subcheck on a clean agent (fix=False)")
async def _(ctx):
    return await maintenance_handlers.do_deep_check(_DEEP_AGENT)


# A single recoverable anonymous-source row under fix=True:
#   deep_anonymous_source: {'recoverable':1,'unrecoverable':0,'fixed':1,
#                           'samples':[{'id':1,'recovered_name':'bob'}]}
# and the source column in the db dump is rewritten to
# `{"type":"User","id":"","name":"bob"}` -- the row-level pin.
# The other five subchecks report their zero shape (`fixed:0` for
# short_content since fix is now True); the envelope's `fixed:True` reflects
# the effective fix flag.
@scenario("deep-check-anonymous-source-fix", "store-recall-health",
          "deep_check: fix=True with a recoverable anonymous-User row + [username] prefix — name recovered, source rewritten in db",
          seed=_seed_deep_anonymous_source)
async def _(ctx):
    return await maintenance_handlers.do_deep_check(_DEEP_AGENT, fix=True)
