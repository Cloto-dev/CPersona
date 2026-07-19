"""Behavioural snapshot of the five functions the 2.5.2 alpha stage splits apart.

CSC Task #293. This is the second half of the safety net; `scripts/mutation-proof.py`
(Task #285) is the first, and they prove different things:

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
the return value is the LEAST interesting half (Task #285 found a dry_run that
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
from typing import Any, Callable

# Mirror conftest's hermetic pins so the capture script gets the same environment
# the suite runs under. setdefault is idempotent, so importing this from a test
# (where conftest already ran) changes nothing.
os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "behaviour.db"))
os.environ.setdefault("CPERSONA_EMBEDDING_MODE", "none")
os.environ.setdefault("CPERSONA_OPERATING_CONTEXT", "off")

import numpy as np  # noqa: E402

from cpersona import admin_handlers, vector  # noqa: E402
from cpersona._vendored_mcp_common import no_persist  # noqa: E402
from cpersona._vendored_mcp_common.embedding_client import EmbeddingClient  # noqa: E402
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
    no_persist.resume()
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


# --- _search_vector, local branch (CSC Task #286) ---------------------------


@scenario("sv-local-basic", "#286", "local scan: ranking, top-k cut, returned field set", seed=seed_corpus)
async def _(ctx):
    install_local(ctx)
    return await vector._search_vector(ctx.db, "a1", "apples", 3, min_similarity=0.0)


@scenario("sv-local-limit-1", "#286", "local scan: limit smaller than the candidate set (heapq ordering)", seed=seed_corpus)
async def _(ctx):
    install_local(ctx)
    return await vector._search_vector(ctx.db, "a1", "apples", 1, min_similarity=0.0)


@scenario("sv-local-threshold", "#286", "local scan: min_similarity=None falls back to the per-agent threshold", seed=seed_corpus)
async def _(ctx):
    install_local(ctx)
    vector._agent_thresholds["a1"] = 0.5
    try:
        return await vector._search_vector(ctx.db, "a1", "apples", 10)
    finally:
        vector._agent_thresholds.pop("a1", None)


@scenario("sv-local-high-threshold", "#286", "local scan: a threshold no row clears returns empty, not an error", seed=seed_corpus)
async def _(ctx):
    install_local(ctx)
    return await vector._search_vector(ctx.db, "a1", "apples", 10, min_similarity=0.999)


@scenario("sv-local-project", "#286", "local scan: γ project axis ('X' means X ∪ global)", seed=seed_corpus)
async def _(ctx):
    install_local(ctx)
    return await vector._search_vector(ctx.db, "a1", "apples", 10, min_similarity=0.0, project_id="proj-b")


@scenario("sv-local-project-global", "#286", "local scan: project_id='' is global-only", seed=seed_corpus)
async def _(ctx):
    install_local(ctx)
    return await vector._search_vector(ctx.db, "a1", "apples", 10, min_similarity=0.0, project_id="")


@scenario("sv-local-channel", "#286", "local scan: channel axis, with ''=global still matching", seed=seed_corpus)
async def _(ctx):
    install_local(ctx)
    return await vector._search_vector(ctx.db, "a1", "apples", 10, min_similarity=0.0, channel="chat")


@scenario("sv-local-source-no-channel", "#286", "local scan: source_id without channel drops ALL episodes (bug-080)", seed=seed_corpus)
async def _(ctx):
    install_local(ctx)
    return await vector._search_vector(ctx.db, "a1", "apples", 10, min_similarity=0.0, source_id="discord:")


@scenario("sv-local-source-with-channel", "#286", "local scan: source_id WITH channel keeps episodes (bug-080)", seed=seed_corpus)
async def _(ctx):
    install_local(ctx)
    return await vector._search_vector(
        ctx.db, "a1", "apples", 10, min_similarity=0.0, source_id="discord:", channel="chat"
    )


@scenario("sv-local-source-escape", "#286", "local scan: LIKE metacharacters in source_id are escaped, not matched", seed=seed_corpus)
async def _(ctx):
    install_local(ctx)
    return await vector._search_vector(ctx.db, "a1", "apples", 10, min_similarity=0.0, source_id="discord:_2")


@scenario("sv-local-empty-embed", "#286", "local scan: an empty query embedding returns [] via the health probe", seed=seed_corpus)
async def _(ctx):
    install_local(ctx)

    class _Empty:
        _http_url = None
        _client = None
        mode = "fake"

        async def embed(self, texts):
            return []

    ctx.patch(vector, "_embedding_client", _Empty())
    return await vector._search_vector(ctx.db, "a1", "apples", 10, min_similarity=0.0)


@scenario("sv-local-unknown-agent", "#286", "local scan: an agent with no rows", seed=seed_corpus)
async def _(ctx):
    install_local(ctx)
    return await vector._search_vector(ctx.db, "nobody", "apples", 10, min_similarity=0.0)


# --- _search_vector, remote branch (CSC Task #286) --------------------------
#
# The split turns the remote branch into a helper whose return signals whether
# local should run. These four scenarios are the ones that pin that signal, and
# `sv-remote-empty` is the sharp one: a remote call that SUCCEEDS with zero hits
# returns [] and must NOT fall through to local. An extraction that conflates
# "no hits" with "remote unavailable" passes every other scenario here.


@scenario("sv-remote-hits", "#286", "remote: memory and episode hits become rows; request body and timeout", seed=seed_corpus)
async def _(ctx):
    install_remote(ctx, {"results": [{"id": "mem:1", "score": 0.91}, {"id": "ep:1", "score": 0.77}]})
    return await vector._search_vector(ctx.db, "a1", "apples", 10, min_similarity=0.25)


@scenario("sv-remote-empty", "#286", "remote: a SUCCESSFUL empty result returns [] and does NOT fall through to local", seed=seed_corpus)
async def _(ctx):
    install_remote(ctx, {"results": []})
    return await vector._search_vector(ctx.db, "a1", "apples", 10, min_similarity=0.0)


@scenario("sv-remote-error-fallback", "#286", "remote: a transport failure falls through to the local scan", seed=seed_corpus)
async def _(ctx):
    install_remote(ctx, None, error=RuntimeError("endpoint down"))
    return await vector._search_vector(ctx.db, "a1", "apples", 3, min_similarity=0.0)


@scenario("sv-remote-isolation-miss", "#286", "remote: a hit whose row fails the γ predicate is dropped (bug-046/075/100)", seed=seed_corpus)
async def _(ctx):
    install_remote(ctx, {"results": [{"id": "mem:3", "score": 0.99}, {"id": "mem:1", "score": 0.95}]})
    # mem:3 is the proj-b row; querying global-only must not surface it.
    return await vector._search_vector(ctx.db, "a1", "apples", 10, min_similarity=0.0, project_id="")


@scenario("sv-remote-stale-id", "#286", "remote: a hit for a row that no longer exists is skipped silently", seed=seed_corpus)
async def _(ctx):
    install_remote(ctx, {"results": [{"id": "mem:9999", "score": 0.99}, {"id": "ep:9999", "score": 0.98}]})
    return await vector._search_vector(ctx.db, "a1", "apples", 10, min_similarity=0.0)


@scenario("sv-remote-episode-src-gate", "#286", "remote: source_id without channel skips episode hits (mirrors local bug-080)", seed=seed_corpus)
async def _(ctx):
    install_remote(ctx, {"results": [{"id": "ep:1", "score": 0.9}, {"id": "mem:5", "score": 0.8}]})
    return await vector._search_vector(ctx.db, "a1", "apples", 10, min_similarity=0.0, source_id="discord:")


# --- do_import_memories (CSC Task #287) -------------------------------------


async def _seed_import_target(ctx: Ctx) -> None:
    await ctx.db.execute(
        "INSERT INTO memories (agent_id, msg_id, content, source, timestamp, created_at) "
        "VALUES ('imp', 'mid-1', 'original text', '{}', '2026-01-01T00:00:01Z', '2026-01-01T00:00:01Z')"
    )
    await ctx.db.commit()


@scenario("import-fresh", "#287", "import: fresh records land; counts and rows agree", seed=_seed_import_target)
async def _(ctx):
    path = ctx.path("fresh.jsonl")
    write_jsonl(path, [
        {"_type": "memory", "agent_id": "imp", "content": "fresh one"},
        {"_type": "memory", "agent_id": "imp", "content": "fresh two", "msg_id": "mid-2"},
        {"_type": "episode", "agent_id": "imp", "summary": "an episode", "keywords": ["k"]},
        {"_type": "profile", "agent_id": "imp", "content": "profile body"},
    ])
    return await admin_handlers.do_import_memories(path, target_agent_id="imp")


@scenario("import-dry-run", "#287", "import: dry_run reports the same counts and writes nothing", seed=_seed_import_target)
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
@scenario("import-dry-run-intra-file-duplicates", "#287",
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


@scenario("import-msgid-collision", "#287","import: an existing msg_id is skipped, the stored row is not overwritten", seed=_seed_import_target, unstable_row_ids=True)
async def _(ctx):
    path = ctx.path("collide.jsonl")
    write_jsonl(path, [
        {"_type": "memory", "agent_id": "imp", "msg_id": "mid-1", "content": "edited text"},
        {"_type": "memory", "agent_id": "imp", "msg_id": "mid-3", "content": "new text"},
    ])
    return await admin_handlers.do_import_memories(path, target_agent_id="imp")


@scenario("import-content-collision", "#287", "import: duplicate content is absorbed by the UNIQUE index, not counted twice", seed=_seed_import_target, unstable_row_ids=True)
async def _(ctx):
    path = ctx.path("dupe.jsonl")
    write_jsonl(path, [
        {"_type": "memory", "agent_id": "imp", "content": "original text"},
        {"_type": "memory", "agent_id": "imp", "content": "same body twice"},
        {"_type": "memory", "agent_id": "imp", "content": "same body twice"},
    ])
    return await admin_handlers.do_import_memories(path, target_agent_id="imp")


@scenario("import-truncated", "#287", "import: a header/row-count mismatch aborts and rolls back (bug-091/110)", seed=_seed_import_target)
async def _(ctx):
    path = ctx.path("short.jsonl")
    write_jsonl(
        path,
        [{"_type": "memory", "agent_id": "imp", "content": f"row {i}"} for i in range(2)],
        header={"_type": "header", "counts": {"memories": 5, "episodes": 0, "profiles": 0}},
    )
    return await admin_handlers.do_import_memories(path, target_agent_id="imp")


@scenario("import-torn-line", "#287", "import: a malformed line is reported and the transaction rolls back", seed=_seed_import_target)
async def _(ctx):
    path = ctx.path("torn.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"_type": "memory", "agent_id": "imp", "content": "good row"}) + "\n")
        f.write('{"_type": "memory", "agent_id": "imp", "cont')
    return await admin_handlers.do_import_memories(path, target_agent_id="imp")


@scenario("import-missing-file", "#287", "import: a nonexistent path fails cleanly", seed=_seed_import_target)
async def _(ctx):
    return await admin_handlers.do_import_memories(ctx.path("nope.jsonl"), target_agent_id="imp")


@scenario("import-retarget", "#287", "import: target_agent_id overrides the agent_id in the file", seed=_seed_import_target)
async def _(ctx):
    path = ctx.path("retarget.jsonl")
    write_jsonl(path, [
        {"_type": "memory", "agent_id": "someone-else", "content": "belongs to the target now"},
        {"_type": "episode", "agent_id": "someone-else", "summary": "retargeted episode", "keywords": []},
    ])
    return await admin_handlers.do_import_memories(path, target_agent_id="imp")


@scenario("import-preserves-axes", "#287", "import: γ axes and locked survive the round trip", seed=_seed_import_target)
async def _(ctx):
    path = ctx.path("axes.jsonl")
    write_jsonl(path, [
        {"_type": "memory", "agent_id": "imp", "content": "scoped row", "project_id": "p1",
         "channel": "chat", "locked": 1, "metadata": {"k": "v"}, "source": {"id": "discord:7"}},
    ])
    return await admin_handlers.do_import_memories(path, target_agent_id="imp")


# --- do_merge_memories (CSC Task #287) --------------------------------------


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


@scenario("merge-copy-skip", "#287", "merge: copy+skip leaves the source intact and dedups the shared row", seed=_seed_merge, unstable_row_ids=True)
async def _(ctx):
    return await admin_handlers.do_merge_memories("src", "dst")


@scenario("merge-move", "#287", "merge: move deletes the source rows in the same transaction", seed=_seed_merge, unstable_row_ids=True)
async def _(ctx):
    return await admin_handlers.do_merge_memories("src", "dst", mode="move")


@scenario("merge-dry-run", "#287", "merge: dry_run reports the counts and touches nothing", seed=_seed_merge, unstable_row_ids=True)
async def _(ctx):
    return await admin_handlers.do_merge_memories("src", "dst", dry_run=True)


@scenario("merge-move-dry-run", "#287", "merge: a move preview must not delete the source either", seed=_seed_merge, unstable_row_ids=True)
async def _(ctx):
    return await admin_handlers.do_merge_memories("src", "dst", mode="move", dry_run=True)


@scenario("merge-empty-source", "#287", "merge: an agent with no rows is a no-op, not an error", seed=_seed_merge, unstable_row_ids=True)
async def _(ctx):
    return await admin_handlers.do_merge_memories("ghost", "dst")


@scenario("merge-into-self", "#287", "merge: source == target", seed=_seed_merge, unstable_row_ids=True)
async def _(ctx):
    return await admin_handlers.do_merge_memories("src", "src")


# --- do_calibrate_threshold (CSC Task #287) ---------------------------------


@scenario("calibrate-basic", "#287", "calibrate: the null distribution, the derived threshold, and the in-place mutation",
          seed=lambda ctx: seed_calibration(ctx, n=30))
async def _(ctx):
    install_local(ctx)
    return await admin_handlers.do_calibrate_threshold("cal")


# The only scenario whose corpus (30) exceeds its sample (12), so the draw is a
# genuine random subset. `sampled_embeddings` and `num_pairs` -- the cap actually
# taking effect, which is what the scenario is for -- stay pinned; the statistics
# computed from the subset cannot be.
@scenario("calibrate-sample-cap", "#287", "calibrate: an explicit sample_size bounds the draw",
          seed=lambda ctx: seed_calibration(ctx, n=30),
          volatile=("distribution", "new_threshold", "old_threshold", "null_admit_rate",
                    "pos_admit_rate", "pos_mean", "youden_j", "separation", "thresholds"))
async def _(ctx):
    install_local(ctx)
    return await admin_handlers.do_calibrate_threshold("cal", sample_size=12)


@scenario("calibrate-percentile", "#287", "calibrate: the percentile method rather than the z-score default",
          seed=lambda ctx: seed_calibration(ctx, n=30))
async def _(ctx):
    install_local(ctx)
    return await admin_handlers.do_calibrate_threshold("cal", method="percentile", percentile=95.0)


@scenario("calibrate-too-few", "#287", "calibrate: below the raw sample floor, refuse and leave the threshold unset",
          seed=lambda ctx: seed_calibration(ctx, n=4))
async def _(ctx):
    install_local(ctx)
    return await admin_handlers.do_calibrate_threshold("cal")


@scenario("calibrate-ragged", "#287", "calibrate: the post-dimension-filter floor, the second of the two",
          seed=lambda ctx: seed_calibration(ctx, n=8, ragged=6))
async def _(ctx):
    install_local(ctx)
    return await admin_handlers.do_calibrate_threshold("cal")


@scenario("calibrate-no-corpus", "#287", "calibrate: an agent with nothing to sample")
async def _(ctx):
    install_local(ctx)
    return await admin_handlers.do_calibrate_threshold("empty-agent")


# --- do_export_memories (round-trip partner of import) ----------------------


@scenario("export-roundtrip", "#287", "export writes what import reads: the file survives its own reader", seed=seed_corpus)
async def _(ctx):
    path = ctx.path("export.jsonl")
    exported = await admin_handlers.do_export_memories("a1", path, include_embeddings=True)
    with open(path, encoding="utf-8") as f:
        line_types = [json.loads(line).get("_type") for line in f if line.strip()]
    reimported = await admin_handlers.do_import_memories(path, target_agent_id="roundtrip")
    return {"exported": exported, "line_types": line_types, "reimported": reimported}
