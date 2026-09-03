"""Environment variable configuration for CPersona."""

import logging
import os

logger = logging.getLogger(__name__)


# bug-133: malformed numeric settings fall back independently instead of
# preventing the server from importing.
def _parse_int(env_key: str, default: int) -> int:
    raw = os.environ.get(env_key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (ValueError, TypeError):
        logger.warning("bug-133: invalid int for %s=%r; using default %r", env_key, raw, default)
        return default


def _parse_float(env_key: str, default: float) -> float:
    raw = os.environ.get(env_key)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (ValueError, TypeError):
        logger.warning("bug-133: invalid float for %s=%r; using default %r", env_key, raw, default)
        return default


# C10: public wrappers so entrypoint env reads that live outside config.py
# (server.py's HTTP port and embedding timeout) can route through the same
# bug-133 warn-and-fall-back-to-default path instead of a bare int()/float()
# that would raise an opaque ValueError and abort startup before listening.
def parse_int(env_key: str, default: int) -> int:
    return _parse_int(env_key, default)


def parse_float(env_key: str, default: float) -> float:
    return _parse_float(env_key, default)


DB_PATH = os.environ.get("CPERSONA_DB_PATH", "data/cpersona.db")
# Per-subject alias ledger (docs/OAUTH_DESIGN.md §12). Defaults beside the
# database rather than beside the ACL file on purpose: the server writes this
# file (first connection issues an alias), while the grant table's directory is
# operator-owned and on a hardened deployment not writable by the service user.
ALIAS_LEDGER_FILE = os.environ.get("CPERSONA_ALIAS_LEDGER_FILE", "")


def alias_ledger_path() -> str:
    return ALIAS_LEDGER_FILE or os.path.join(
        os.path.dirname(DB_PATH) or ".", "alias_ledger.json"
    )
# bug-054: optional confinement root for export_memories' caller-supplied
# output_path. When set, an export's resolved realpath MUST stay within this
# directory. When unset (default), export still rejects '..' traversal but allows
# an absolute/relative path — the readOnlyHint=False/destructiveHint=True tool
# annotation makes the host confirm the write. Set this for a hardened deployment.
EXPORT_DIR = os.environ.get("CPERSONA_EXPORT_DIR", "")
MAX_IMPORT_BYTES = _parse_int("CPERSONA_MAX_IMPORT_BYTES", 104857600)
# bug-085: MAX_MEMORIES is the vector retriever's SCAN WINDOW — how many of the
# newest rows it fetches and cosine-ranks per recall — not a response size (that
# is the per-call `limit`, clamped in the handlers). It is the single knob that
# bounds vector-recall reach: rows older than the window are invisible to the
# vector retriever, so the default must comfortably exceed a real corpus.
# Benchmarks on larger corpora raise it via the env var instead of patching code.
MAX_MEMORIES = _parse_int("CPERSONA_MAX_MEMORIES", 10000)
# The vector retriever's REACH: how many of the newest rows its arm may look at
# IN TOTAL. `0` (the default) means "the same as the window", and the far list
# below does not exist -- with the default set, none of that code runs.
#
# It is a second knob rather than a larger value for the first because
# MAX_MEMORIES does two jobs at once. It bounds what a recall reads, which is
# what it was named for; and by keeping only the NEWEST rows it also hands every
# recent memory a candidate field of N instead of the whole corpus, which is a
# recency prior that nothing named and nothing could turn off separately.
# Widening the window removes the prior in the act of extending the reach.
# Measured on 237,654 stored documents, a window of 200,000 instead of 10,000
# gained +4.93 NDCG@10 where the answer lay below the window and lost 20.19
# where it lay inside it, with nothing truncated
# (benchmarks/measurements/results-scan-window-default-ab.md). The loss is rank
# displacement: the arm hands the fusion its top `limit` rows, so a recent
# answer that ranked third among 10,000 candidates and thirtieth among 200,000
# is not lower on that list, it is OFF it, and its vote is gone.
#
# So the two are set separately. Above MAX_MEMORIES, the rows at scan positions
# [MAX_MEMORIES, REACH) are ranked as a SECOND list -- same threshold, same
# stable top-`limit` cut -- and handed to the fusion as one more ranked list.
# The near list is untouched, so every row that places today keeps the vote it
# has today, and rows past the window can only be added. Design and the
# measurement that decides the values: docs/SCAN_WINDOW_REACH_DESIGN.md.
#
# What it must not be coupled to: the response `limit` (bug-085's rule holds for
# both windows -- how far back the arm looks is not how many rows the caller
# asked for), and the near window itself. Raising this one INSTEAD of raising
# MAX_MEMORIES is the whole point; a change that moved them together would
# reproduce the loss above.
#
# A value at or below MAX_MEMORIES is off rather than an error: the region
# [MAX_MEMORIES, REACH) is empty by construction and the guard skips the scan
# entirely instead of running one that returns nothing. Negative values clamp to
# 0 for the same reason.
VECTOR_REACH = max(0, _parse_int("CPERSONA_VECTOR_REACH", 0))
# How many rows of the far list reach the fusion. `0` (the default) means "the
# same as the response `limit`", which is the far list exactly as it is built
# today -- same rows, same order, bit for bit -- so this setting is off unless
# it is set. A positive value cuts the far list to `min(limit, N)` rows.
#
# It bounds a CANDIDATE COUNT and nothing else: it changes how many far rows the
# fusion receives, not how any row is scored. A far row that survives the cut is
# returned with its cosine and votes exactly as it does now.
#
# Why the length is the thing worth bounding. Measured on 237,654 stored
# documents, a reach of 200,000 cost the near stratum 6.67 NDCG@10, and 75% of
# the rows that displaced a recent answer carried a far-list vote and no other;
# the exploratory sweep then found the near cost nearly the same at a reach of
# 50,000 (-5.97) as at 300,000 (-8.49), so it is the far list's ten
# full-strength votes, not the depth they were drawn from, that displaces
# (benchmarks/measurements/results-scan-window-reach-ab.md). Shortening the list
# attacks that directly, and does it as a count rather than as a weight -- a
# per-list weight would be a scoring change, which this line does not make.
#
# What it must not be coupled to: the NEAR list's cut, which stays `limit` and is
# not this knob's business (the near list is the one thing the reach design
# promises is untouched); and the reach itself, which decides which rows the far
# region holds, not how many of them are handed on. With the reach off there is
# no far list and this setting is irrelevant.
VECTOR_FAR_LIMIT = max(0, _parse_int("CPERSONA_VECTOR_FAR_LIMIT", 0))
# The library layer's own ceiling on a caller-supplied `limit`. It used to BE
# MAX_MEMORIES, which coupled two bounds that answer different questions: how far
# back the vector retriever looks, and how many rows one call may materialise.
# Raising the scan window for a larger corpus therefore raised a response bound
# by the same factor, silently — the mirror of the rule stated on
# MAX_CONTENT_LENGTH below, where a write bound was kept from enlarging the read
# budgets. Pinned at what the coupling happened to produce (the shipped
# MAX_MEMORIES default), so the window can move on its own.
#
# This is the LIBRARY bound, not the agent-facing one: the recall tools' JSON
# Schema declares `maximum: 100`, and that is what bounds a context window. This
# one bounds resource use for callers that legitimately ask for full depth —
# benchmark full-ranking, bulk export, a future rerank — and in rrf mode the
# fusion-list depth tracks `limit`, so a ceiling that bites collapses deep-ranking
# quality rather than merely trimming a response. It bit once already, at 100
# (bge-m3 LongMemEval 81.17 -> 48.98), which is why a bench that reaches it is
# told so rather than left to read the damage off its own scores.
RECALL_LIBRARY_MAX_LIMIT = max(1, _parse_int("CPERSONA_RECALL_LIBRARY_MAX_LIMIT", 10000))
# How many embedding rows the fallback vector scan turns into a matrix at a
# time. The scan reads `MAX_MEMORIES` rows of `(id, embedding)`; it used to
# fetch all of them in one call and then join the blobs, which holds TWO copies
# of the window at once -- the list of blobs and the joined bytes. Measured at
# 20,000 rows x 768 dimensions, that is 2.07x the window, which extrapolates to
# about 6.1 GB at 1,000,000 rows: the path that exists to answer when the index
# cannot became an out-of-memory kill rather than a slow answer. Reading the
# cursor in chunks makes the peak O(chunk) -- 0.08x of the same window,
# measured 4.8 MB and flat in the window size.
#
# The bound is 2 * this - 1 rows in flight, not this many: 1,023 rows at the
# default, 3.1 MB of embedding at 768 dimensions. The scan never scores a small
# matrix, so a window's short tail (`window % chunk` rows) is carried into the
# chunk before it rather than multiplied on its own. That is not tidiness --
# the scores are a matmul, the BLAS selects its kernel by ROW COUNT, and below
# some platform-dependent threshold the last bits move. Measured with Apple
# Accelerate on aarch64 (numpy 2.4.6), the threshold is 64 rows at 64
# dimensions and 16 rows at 768; those are observations of one platform, not
# constants, so the scan is built to keep every matmul comfortably above any
# such threshold rather than to know where it is.
#
# 512 x 768 x 4 bytes = 1.5 MB of embedding per chunk, which fits in L2/L3.
# This bounds memory: it is NOT a latency knob (the read dominates the scan,
# and the chunked form measured no slower), so lowering it does not make a
# recall faster and raising it only raises the peak. It is not free to lower
# either -- a chunk below the platform's threshold moves scores by about one
# ULP, which at a tight cut changes which row is returned. A small value is a
# test instrument rather than a configuration; `_chunked_cosine_scan` carries
# the measurements.
VECTOR_SCAN_CHUNK_ROWS = max(1, _parse_int("CPERSONA_VECTOR_SCAN_CHUNK_ROWS", 512))
# Rows the contiguous vector index may name as holes — the rows it could not put
# in the file (a non-canonical `created_at`) plus the rows that carried no
# embedding when the build ran. The index reads them by id out of the live table
# on every query, so this bounds that read; past it the builder declines to build
# at all, because an index that cannot name all its holes would answer
# approximately and this file format does not approximate.
#
# 10,000 is 6.7% of a 150,000-row corpus, which is the state a bulk import leaves
# behind while the embedding backlog drains — the case where declining to build
# hurts most, since that corpus is exactly the one that needs the index. The
# worst case is that every named hole has since gained an embedding and is
# therefore read in full: measured at 150,000 rows of 1024-d float32, 1,000 holes
# cost 8.8 ms and 5,000 cost 34.5 ms, so 10,000 interpolates to roughly 65 ms per
# query. That is the price of having an index at all versus not having one, and
# it decays to nothing at the next rebuild, which absorbs the holes.
#
# The holes are bound as a single JSON array (see `_index_tail_rows`), so this
# number is not also a count of SQL variables.
VECTOR_INDEX_MAX_EXCLUDED_IDS = max(0, _parse_int("CPERSONA_VECTOR_INDEX_MAX_EXCLUDED_IDS", 10000))
# NULL-embedding rows one `check_health(fix=true)` run re-embeds. Prefetch and
# repair read the same number, and the `repairable` count is bounded by it — a
# fixer that reaches 5,000 rows must not report 50,000 as repairable.
#
# 5,000 is 3.3% of a 150,000-row corpus per run. At the previous 500, draining a
# 50,000-row backlog took 100 fix runs, and the two caps compound: while the
# backlog exceeds VECTOR_INDEX_MAX_EXCLUDED_IDS the index cannot be built either,
# so the corpus stays on the live scan for as long as the drain takes. The
# embedding calls happen BEFORE the write lock is taken (prefetch), so what this
# number bounds is prefetch wall time and the number of locked UPDATEs — not HTTP
# round-trips made while holding the lock.
REEMBED_ROW_CAP = max(1, _parse_int("CPERSONA_REEMBED_ROW_CAP", 5000))
# Embedded rows `deep_near_duplicate` compares, bounding an O(n^2) dense cosine
# matrix (`unit @ unit.T` plus `np.triu`) — the memory, not the time, is what
# decides this one. Measured on 1024-d float32: n=1,000 is 17 ms / 17 MB peak,
# n=2,000 is 18 ms / 52 MB, n=5,000 is 103 ms / 266 MB, n=10,000 is 392 ms /
# 982 MB. 5,000 covers 3.3% of a 150,000-row corpus for a transient 266 MB;
# 10,000 would ask an 8 GB machine for a gigabyte of scratch space inside a
# maintenance call, so the sample stays a sample.
NEAR_DUPLICATE_ROW_CAP = max(2, _parse_int("CPERSONA_NEAR_DUPLICATE_ROW_CAP", 5000))
# Offending source rows `check_invalid_source_type` classifies per run. It bounds
# the JSON parsing a plain `check_health` does — microseconds per row, so this is
# an order of magnitude cheaper than the two caps above and can afford to be the
# largest. 10,000 is 6.7% of a 150,000-row corpus. Past the cap the sample is
# incomplete and the check declines to downgrade its own severity, so the cap
# costs a verdict, not correctness.
INVALID_SOURCE_CLASSIFY_CAP = max(1, _parse_int("CPERSONA_INVALID_SOURCE_CLASSIFY_CAP", 10000))
# 16000, raised from 2000, because the old cap was destroying the part
# of a memory that is worth the most. Long records put the conclusion first and
# the hard-won detail last, so cutting the tail on every write removed exactly
# what nothing else records — and no later line can restore it (the 2.6 tree
# splits what is stored; it cannot recover what was never stored). A measurement
# of the live corpus found 124 of 1625 rows (7.6%) sitting exactly at 2000
# characters: scars, not coincidences.
#
# The number is derived, not picked: 1.5x the longest record that ever existed
# (a 10,432-character episode) and 8x the old cap, while no row in the corpus
# exceeds 8000 — so it refuses no honest write, and still stops a runaway blob
# with 60x of headroom. The read side does NOT follow it up: each full-text read
# path keeps its own fixed character budget pinned at what its worst case used
# to be — get_contents at GET_CONTENTS_MAX_CHARS (40000 = 20 refs x 2000),
# recall(full_content=true) at server.RECALL_FULL_CONTENT_MAX_CHARS (200000 =
# limit 100 x 2000; bug-211 — this path shipped unbudgeted at first, so the
# claim here was briefly true only of get_contents), and the two list tools at
# admin_handlers.LIST_MEMORIES_MAX_CHARS / LIST_EPISODES_MAX_CHARS (1,000,000
# and 800,000; bug-255 — the second wave of the same omission). None of them is
# derived from this constant, so a batch cannot grow just because this number did. The one
# documented exception is a single row that alone exceeds the budget, which is
# still returned whole rather than made unreachable; at this default no such
# row can exist.
#
# The embedding window is unchanged (512 tokens), so text past the window is
# still invisible to the vector retriever. It is NOT invisible to search: the
# FTS triggers index the stored content in full, so the tail becomes reachable
# through the lexical channel the moment it is stored. Closing the vector gap is
# the 2.6 tree's job.
MAX_CONTENT_LENGTH = _parse_int("CPERSONA_MAX_CONTENT_LENGTH", 16000)
# The profile owns its ceiling, because the two rows are bounded for
# different reasons and only one of them is bounded by its cap alone.
#
# A memory is preview-trimmed on the way out (RECALL_PREVIEW_CHARS) and its full
# text stays reachable through `ref` + get_contents, so its cap governs what is
# stored, not what a recall response costs. The profile is injected as the id=-1
# sentinel row, and _apply_preview deliberately skips rows with no `ref` —
# trimming them would make their full content permanently unreachable (bug-117).
# So the profile's cap is the ONLY thing bounding it, and it is paid in EVERY
# recall response.
#
# Sharing one constant meant a future relaxation of the memory cap (the 2.6
# tree) would silently unbound the profile too. The number is unchanged —
# 2000 is right for the profile, and small is the point — only its ownership is.
MAX_PROFILE_LENGTH = _parse_int("CPERSONA_MAX_PROFILE_LENGTH", 2000)
# 2.5.2b1 (audit C12): the JSON sidecar fields — source and metadata — reach the
# row with no bound at all, so one call could park an arbitrarily large blob per
# memory (content has been capped since 2.1; these never were). They are cheap
# structured annotations, not payload: the cap is generous enough that no honest
# producer meets it, and truncation is not an option because half a JSON document
# is not a JSON document — the write is refused instead (result='rejected').
#
# bug-176: "the write path" here means the seams that accept content from OUTSIDE
# this database — store, update_memory, the episode prepare, and (bug-221) the
# import, whose file may have been produced by another DB, an older version or a
# hand edit. merge_memories stays outside it: it moves rows that this database
# already accepted under some earlier cap, and re-sanitising an intra-DB move would
# silently rewrite content the operator never edited. check_health's
# oversized_content check reports those after the fact, which is the right division
# — bound what arrives from outside, detect what is being moved within.
MAX_METADATA_LENGTH = _parse_int("CPERSONA_MAX_METADATA_LENGTH", 8000)
FTS_ENABLED = os.environ.get("CPERSONA_FTS_ENABLED", "true").lower() == "true"

# Embedding env: the server-specific CPERSONA_* key takes precedence, then the
# generic key shared across Cloto MCP servers (matches the CScheduler convention
# and the marketplace catalog, which sets EMBEDDING_MODE / EMBEDDING_HTTP_URL).
# Without the generic fallback a catalog-installed cpersona ran with embeddings
# silently off (recall degraded to FTS-only) — bug-001.
EMBEDDING_MODE = os.environ.get("CPERSONA_EMBEDDING_MODE") or os.environ.get("EMBEDDING_MODE", "none")

# The values the client can act on. Anything else reaches every embed as a
# per-call failure that never leaves the process (bug-275), which recall then had
# to classify after the fact. Validated once at startup instead — see
# `assert_embedding_mode_supported`. Not enforced at import: this module is
# imported by tools that never embed, and a value they will not use is not their
# problem to fail on.
SUPPORTED_EMBEDDING_MODES = ("none", "http", "api")
EMBEDDING_URL = os.environ.get("CPERSONA_EMBEDDING_URL") or os.environ.get("EMBEDDING_HTTP_URL", "")
EMBEDDING_API_KEY = os.environ.get("CPERSONA_EMBEDDING_API_KEY") or os.environ.get("EMBEDDING_API_KEY", "")
EMBEDDING_API_URL = os.environ.get("CPERSONA_EMBEDDING_API_URL") or os.environ.get("EMBEDDING_API_URL", "https://api.openai.com/v1/embeddings")
EMBEDDING_MODEL = os.environ.get("CPERSONA_EMBEDDING_MODEL") or os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")

VECTOR_MIN_SIMILARITY = _parse_float("CPERSONA_VECTOR_MIN_SIMILARITY", 0.3)

EMBEDDING_CACHE_SIZE = _parse_int("CPERSONA_EMBEDDING_CACHE_SIZE", 256)
EMBEDDING_CACHE_TTL = _parse_int("CPERSONA_EMBEDDING_CACHE_TTL", 300)

# Degraded-advisory runtime guard (v2.4.33): when embeddings are unavailable at runtime
# (mode=none, or a configured http endpoint is unreachable) do_recall attaches an
# `advisory` to its response so the calling agent can self-report the degradation
# instead of silently serving keyword/FTS-only recall. On by default; opting out records
# an operator who accepts a supported-but-not-recommended standalone deployment, and it
# stops the report rather than the degradation. See health.py + DEGRADED_ADVISORY_DESIGN.
DEGRADED_ADVISORY_ENABLED = os.environ.get("CPERSONA_DEGRADED_ADVISORY", "true").lower() == "true"

TASK_QUEUE_ENABLED = os.environ.get("CPERSONA_TASK_QUEUE_ENABLED", "true").lower() == "true"

# New-release detection (cpersona/update_check.py). On by default: a memory
# server is installed once and then left alone, so the process answering
# today's recall can be several releases behind — or running a release its
# publisher has since withdrawn — with nothing in the running system saying so.
#
# What being on costs: ONE outbound GET to the package index per process start
# (cached for UPDATE_CHECK_INTERVAL_SECONDS), issued by a background task that
# nothing waits on and bounded end to end by update_check.TIMEOUT_SECONDS. It
# sends no data about this deployment — it asks for the public index page of
# this project and reads the answer.
#
# Setting it to false is total, not partial: no fetch, no cache read, no notice
# on any surface, and check_update answers `state=disabled`. That is the switch
# for an air-gapped or egress-controlled deployment, and for an operator who
# wants no outbound connection they did not ask for. Updating is never
# automatic either way.
UPDATE_CHECK_ENABLED = os.environ.get("CPERSONA_UPDATE_CHECK", "true").lower() == "true"
# How long a fetched verdict stays usable. A release is not news twice a day,
# and the sidecar makes a restart loop cost one request rather than one per
# start.
UPDATE_CHECK_INTERVAL_SECONDS = _parse_int("CPERSONA_UPDATE_CHECK_INTERVAL_SECONDS", 86400)

# Process-local cache for the per-scope aggregates every recall reads (the
# confidence span's MIN/MAX and the gate pool's COUNTs — see scope_stats.py).
# They are full scans over the isolation scope whose answers change only when a
# row is written, so a read-heavy deployment re-derives the same numbers from a
# scan on every call. On by default; turning it off recomputes every time, which
# is the pre-cache behaviour and the reference an equivalence run compares
# against.
SCOPE_STATS_CACHE_ENABLED = os.environ.get("CPERSONA_SCOPE_STATS_CACHE", "true").lower() == "true"
# How long a cached entry may outlive a write made by ANOTHER process on the same
# file (this process's own writes invalidate exactly, by generation). It bounds
# staleness rather than preventing it: the cached values scale a confidence curve
# and size the gate pool, so a delay of this order changes how rows are scored,
# never which rows exist. Lower it for a shared file with an active second writer;
# 0 disables reuse entirely (every lookup recomputes).
SCOPE_STATS_TTL_SECONDS = _parse_float("CPERSONA_SCOPE_STATS_TTL_SECONDS", 60.0)

CONFIDENCE_ENABLED = os.environ.get("CPERSONA_CONFIDENCE_ENABLED", "false").lower() == "true"
COSINE_FLOOR = _parse_float("CPERSONA_COSINE_FLOOR", 0.20)
COSINE_CEIL = _parse_float("CPERSONA_COSINE_CEIL", 0.75)
DECAY_RATE = _parse_float("CPERSONA_DECAY_RATE", 0.005)
DECAY_FLOOR = _parse_float("CPERSONA_DECAY_FLOOR", 0.3)
DECAY_CEIL = _parse_float("CPERSONA_DECAY_CEIL", 0.5)
RECALL_BOOST = _parse_float("CPERSONA_RECALL_BOOST", 0.02)
BOOST_DECAY_RATE = _parse_float("CPERSONA_BOOST_DECAY_RATE", 0.002)
MIN_TIME_RANGE_HOURS = _parse_float("CPERSONA_MIN_TIME_RANGE_HOURS", 24.0)
REFERENCE_HOURS = _parse_float("CPERSONA_REFERENCE_HOURS", 168.0)
RESOLVED_DECAY_FACTOR = _parse_float("CPERSONA_RESOLVED_DECAY_FACTOR", 0.3)
RECENT_RECALL_PENALTY = _parse_float("CPERSONA_RECENT_RECALL_PENALTY", 0.7)
RECENT_RECALL_WINDOW_MIN = _parse_float("CPERSONA_RECENT_RECALL_WINDOW_MIN", 5.0)
TASK_MAX_RETRIES = _parse_int("CPERSONA_TASK_MAX_RETRIES", 3)
TASK_RETRY_DELAY = _parse_int("CPERSONA_TASK_RETRY_DELAY", 30)

VECTOR_SEARCH_MODE = os.environ.get("CPERSONA_VECTOR_SEARCH_MODE", "local")
# bug-033: dedicated per-call timeout for the remote /search POST on the recall
# hot path. Without it the POST inherits the embed client's 30s DEFAULT_TIMEOUT_SECS,
# so a hung/flapping endpoint blocks every recall ~30s before falling back to local.
# Short enough to fail over fast, long enough for a healthy remote search.
REMOTE_SEARCH_TIMEOUT_SECS = _parse_float("CPERSONA_REMOTE_SEARCH_TIMEOUT_SECS", 5.0)
# #361 item (6): the sibling of the above for the /index POST on the WRITE hot
# path. Every other remote call states its own short deadline (probe 3s, search
# 5s); the index push was the one that silently inherited the embed client's 30s
# default, so the read path was better protected than the write path against the
# same hung endpoint. Slightly longer than search: indexing does the embedding
# work server-side, and a failure here is non-fatal (embedded=false) rather than
# a fallback to another retriever.
REMOTE_INDEX_TIMEOUT_SECS = _parse_float("CPERSONA_REMOTE_INDEX_TIMEOUT_SECS", 10.0)
STORE_BLOB = os.environ.get("CPERSONA_STORE_BLOB", "true").lower() == "true"


def local_blobs_stored(vector_search_mode: str, store_blob: bool) -> bool:
    """Whether a write leaves an embedding BLOB in the local row.

    bug-182: this rule used to exist only as an inline condition on the write
    paths, so the maintenance layer had no way to know that a NULL embedding was
    the *configured* steady state rather than a failed pipeline — it read the
    NULLs as a broken embedding pipeline and re-embedded the corpus on every
    fix run. One definition now, called by the write gate and by the checks that
    interpret its result.

    Takes the two values as arguments rather than reading the module globals so
    each caller passes the copy it actually stores by (and a test that patches
    one module's copy still steers that module alone).
    """
    return vector_search_mode == "local" or store_blob

AUTO_CALIBRATE = os.environ.get("CPERSONA_AUTO_CALIBRATE", "false").lower() == "true"
CALIBRATE_SAMPLE_SIZE = _parse_int("CPERSONA_CALIBRATE_SAMPLE_SIZE", 200)
# bug-053: hard upper bound on the calibration sample. sample_size is a
# caller-supplied MCP tool parameter that feeds both a LIMIT scan and an O(n^2)
# dense cosine matrix (vecs @ vecs.T) plus np.triu_indices — an unclamped large
# value (e.g. 20000) allocates multi-GB transient arrays and OOM-kills the whole
# server process, taking recall down for every agent on the shared connection.
# Mirrors the _clamp_limit discipline already applied to the recall/list handlers.
#
# 5,000, raised from 2,000: the same matrix NEAR_DUPLICATE_ROW_CAP bounds, so the
# same measurement decides it (1024-d float32) — n=2,000 is 18 ms / 52 MB peak,
# n=5,000 is 103 ms / 266 MB, n=10,000 is 392 ms / 982 MB. A threshold calibrated
# on 2,000 rows of a 150,000-row corpus samples 1.3% of it; 5,000 samples 3.3%
# for a transient the machine calibration already runs on can hold. The ceiling
# is what stops the OOM, so it moves only as far as a measured allocation.
CALIBRATE_MAX_SAMPLE = max(1, _parse_int("CPERSONA_CALIBRATE_MAX_SAMPLE", 5000))
CALIBRATE_Z_FACTOR = _parse_float("CPERSONA_CALIBRATE_Z_FACTOR", 1.0)
CALIBRATE_FLOOR = _parse_float("CPERSONA_CALIBRATE_FLOOR", 0.05)
# v2.4.24 — calibration method. "percentile" sets the threshold at a quantile of
# the random-pair (null) similarity distribution; "zscore" uses mean + z*std.
# Both place the floor ABOVE the null mean so unrelated pairs are rejected — the
# pre-2.4.24 zscore formula subtracted (mean - z*std), placing the floor below
# the null mean and admitting the majority of unrelated pairs (topic drift).
# bug-231: one definition of the method enum. The handler validates against it and the
# calibrate_threshold JSON Schema publishes it, so a spelling the schema advertises and
# the handler rejects (or the reverse) cannot drift into existence — the same
# single-definition treatment CANONICAL_SOURCE_TYPES got for the store schema.
CALIBRATE_METHODS = ("separation", "percentile", "zscore")
CALIBRATE_METHOD = os.environ.get("CPERSONA_CALIBRATE_METHOD", "separation")
CALIBRATE_PERCENTILE = _parse_float("CPERSONA_CALIBRATE_PERCENTILE", 0.95)
# v2.4.24 — method="separation" positive proxy: memories stored within this window
# (minutes) are treated as same-session ≈ related, a representative (non-extreme)
# proxy for the two-population operating-point search. Falls back to nearest-neighbour
# when too few temporally-adjacent pairs exist.
CALIBRATE_TEMPORAL_WINDOW_MIN = _parse_float("CPERSONA_CALIBRATE_TEMPORAL_WINDOW_MIN", 30.0)
# v2.4.24 — recalibrate on embedding-model change. The calibration is fingerprinted
# by embedding dimension (robust to a missing/stale EMBEDDING_MODEL label); when the
# live corpus dimension differs from the persisted one, the threshold is recomputed
# at startup even if AUTO_CALIBRATE is off. Catches silent jina(768d)->bge-m3(1024d)
# style swaps that would otherwise leave a stale, mis-scaled threshold in place.
CALIBRATE_ON_MODEL_CHANGE = os.environ.get("CPERSONA_CALIBRATE_ON_MODEL_CHANGE", "true").lower() == "true"

# v2.4.26 — post-fusion quality-gate calibration. The fused-score
# (RSF/RRF) quality gate is calibrated by simulate-query separation: sample stored
# memories as pseudo-queries, run the active fusion pipeline, and separate the fused
# scores of temporally-adjacent (same-session ≈ related) rows from unrelated rows.
# This replaces the pool-size heuristic _adaptive_min_score, which never used the
# calibrated distribution and so left rsf/rrf precision uncalibrated. Falls back to
# the heuristic when disabled or when too few samples exist.
FUSED_GATE_ENABLED = os.environ.get("CPERSONA_FUSED_GATE_ENABLED", "true").lower() == "true"
# Number of pseudo-queries sampled at calibration time (each runs one fusion recall,
# so this bounds calibration cost — an offline / startup event, not per-recall).
FUSED_GATE_SAMPLE_QUERIES = max(1, _parse_int("CPERSONA_FUSED_GATE_SAMPLE_QUERIES", 40))
# Independent calibration draws per gate; the applied threshold is their median
# of several. The separation objective is multimodal over a real corpus, so a
# single draw's argmax can hand the gate to a minor mode — production shipped a
# 0.1544 gate that 21 probe draws never reproduced. Total calibration cost is
# DRAWS * SAMPLE_QUERIES recalls, still an offline / startup event.
FUSED_GATE_CALIBRATION_DRAWS = max(1, _parse_int("CPERSONA_FUSED_GATE_CALIBRATION_DRAWS", 5))
# knob 3 — the precision point. The calibrated separation curve is data-derived; this
# is the single policy choice of where to sit on it. strict / balanced / lenient map to
# a specificity weight beta in _separation_threshold (maximise sensitivity +
# beta*specificity): strict=2.0 (fewer contaminants, more misses), balanced=1.0
# (Youden's J), lenient=0.5 (fewer misses, more contaminants). A raw
# CPERSONA_FUSED_GATE_BETA overrides the named level.
RECALL_PRECISION = os.environ.get("CPERSONA_RECALL_PRECISION", "balanced").lower()
_PRECISION_BETA = {"strict": 2.0, "balanced": 1.0, "lenient": 0.5}
FUSED_GATE_BETA = _parse_float(
    "CPERSONA_FUSED_GATE_BETA", _PRECISION_BETA.get(RECALL_PRECISION, 1.0)
)

# Autocut (v2.4 / v2.4.13: relative gap ratio, enabled by default)
AUTOCUT_ENABLED = os.environ.get("CPERSONA_AUTOCUT_ENABLED", "true").lower() == "true"
AUTOCUT_MIN_GAP_RATIO = _parse_float("CPERSONA_AUTOCUT_MIN_GAP_RATIO", 0.15)
# v2.4.25: minimum result count before autocut engages. RSF min-max normalization
# forces the lowest-scoring row to 0.0, so any small result set carries an
# artificial full-scale gap that autocut would cut to a single row (the 2-item
# over-cut that blocked making rsf the default). Below this floor, recall is too
# small for a "gap" to be meaningful — keep every row. Hard floor of 2 keeps the
# gap computation well-defined.
AUTOCUT_MIN_RESULTS = max(2, _parse_int("CPERSONA_AUTOCUT_MIN_RESULTS", 3))

# Episode boundary soft penalty (L3 — v2.4.14)
# Memories created before the latest archived episode are penalised by a
# multiplicative factor so cross-session noise is filtered by the quality gate.
EPISODE_PENALTY_ENABLED = os.environ.get("CPERSONA_EPISODE_PENALTY_ENABLED", "true").lower() == "true"
EPISODE_DECAY_RATE = _parse_float("CPERSONA_EPISODE_DECAY_RATE", 0.01)
EPISODE_DECAY_FLOOR = _parse_float("CPERSONA_EPISODE_DECAY_FLOOR", 0.5)

RECALL_MODE = os.environ.get("CPERSONA_RECALL_MODE", "rrf")
# 2.5.0: MCP-boundary preview tier for recall responses. Message
# content longer than this many characters is returned as a pure prefix (plus
# content_truncated/content_len markers) unless the caller opts out with
# full_content=true; full text is re-fetchable via get_contents(refs). 0
# disables trimming. Boundary-layer only — library callers (do_recall) always
# receive full content, same layering as the limit cap.
RECALL_PREVIEW_CHARS = _parse_int("CPERSONA_RECALL_PREVIEW_CHARS", 500)
RRF_K = max(1, _parse_int("CPERSONA_RRF_K", 60))
RRF_THRESHOLD_FACTOR = _parse_float("CPERSONA_RRF_THRESHOLD_FACTOR", 0.5)
# v2.4.12: Max theoretical _rrf_score ≈ num_retrievers / (RRF_K + 1), with 3
# retrievers (vector, FTS episodes, FTS memories) at rank 0 each. Used by
# _apply_quality_gate to map cosine-scale min_score (0.2–1.0) into the RRF
# score's tight range (0–~0.05).
RRF_MAX_SCALE = 3.0 / (RRF_K + 1)

# OAuth 2.0 as a protected resource: discovery (RFC 9728,
# docs/OAUTH_DESIGN.md §7) and token verification (§8). The metadata document
# and the 401's resource_metadata parameter are what let a conformant client
# find the authorization server it should talk to; without them the client
# finds nothing and falls through to asking a human to type in a client id.
#
# The feature is off unless OAUTH_RESOURCE is non-empty AND at least one
# authorization server is listed. Off means byte-identical responses to today.
# The same two settings enable verification, because advertising a door and
# then refusing everyone who walks through it is the failure §7 exists to end.
# Verification additionally requires ACL mode: without a grant table there is
# nothing to provision against, so every holder of a token for this resource
# would reach every tool.
OAUTH_RESOURCE = os.environ.get("CPERSONA_OAUTH_RESOURCE", "")
# Whitespace- or comma-separated issuer URLs.
OAUTH_AUTHORIZATION_SERVERS = os.environ.get("CPERSONA_OAUTH_AUTHORIZATION_SERVERS", "")
# Advertised on the 401 and in the resource metadata. Measured (docs/
# OAUTH_DESIGN.md §2): the client sends back exactly the scope the resource
# server asked for — and the authorization server refuses the whole request
# with invalid_scope when it does not define that scope, before the user ever
# reaches a sign-in page (measured live, 2026-08-31). This server does not
# enforce scopes (§10), so the default advertises none; set this only to
# values the configured issuer actually defines.
OAUTH_SCOPES = os.environ.get("CPERSONA_OAUTH_SCOPES", "")
# Escape hatch for an authorization server whose metadata this server cannot
# read. Normally the signing keys are found through the issuer's own metadata,
# which is the only way that survives the issuer moving them; this setting is
# for the deployment where that fetch is not possible. Ignored unless exactly
# one authorization server is configured — one URL cannot be the right key set
# for several issuers.
OAUTH_JWKS_URI = os.environ.get("CPERSONA_OAUTH_JWKS_URI", "")


# Transport. Unlike everything above this is read at CALL time, not at import:
# `main()` reads it after the ACL file, the preflight and the embedding client
# are already up, and the tests that cover those paths set it with
# `monkeypatch.setenv`. A module constant would freeze the import-time value and
# make every one of them silently test the default.
def transport() -> str:
    """The configured transport name, unvalidated (``main`` rejects unknown ones)."""
    return os.environ.get("CPERSONA_TRANSPORT", "stdio")


def shared_transport() -> bool:
    """True when ONE process serves SEVERAL client sessions.

    The streamable-HTTP transport runs ``stateless=True``: a single process
    answers every connected client and no session survives a request, so any
    process-level "already told them" state is shared by callers that never saw
    each other's responses (bug-251). Under stdio the process IS the session.

    Deliberately not spelled `transport() == "streamable-http"` at the call
    sites: this asks "is my process-level state shared", which happens to
    coincide with "am I serving HTTP" only because the HTTP mode is stateless.
    A sessionful HTTP mode would keep the second answer and change this one.
    """
    return transport() == "streamable-http"


def assert_embedding_mode_supported(mode: str | None = None) -> None:
    """Fail at startup on an embedding mode the client cannot act on (bug-275).

    Left unchecked, an unsupported value did not stop anything: it produced a
    failed outcome at every embed, with `attempted=False` because no request was
    ever issued, and the operator was told the endpoint was unreachable. One loud
    failure at boot is cheaper than a per-call one that has to be classified.

    Raising rather than falling back to `none`: `none` is a supported
    configuration that disables semantic recall deliberately, so silently
    substituting it would give a misconfigured server the same shape as a
    correctly configured one — the silence this whole area keeps being repaired
    for.
    """
    value = EMBEDDING_MODE if mode is None else mode
    if value not in SUPPORTED_EMBEDDING_MODES:
        raise ValueError(
            f"CPERSONA_EMBEDDING_MODE={value!r} is not supported; expected one of "
            f"{', '.join(SUPPORTED_EMBEDDING_MODES)}. Use 'none' to run without an "
            "embedding backend (recall becomes keyword/FTS-only)."
        )
