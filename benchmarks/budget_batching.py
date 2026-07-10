"""Token-budget dynamic batching for SentenceTransformer on MPS/Apple Silicon.

Shared by benchmark_lmeb.py (Track A) and benchmark_trackb_lmeb.py (Track B).

Three MPS pathologies this shim addresses (all observed on LongMemEval,
237k docs, M5 32GB):

1. O(batch x seq^2) attention buffers — a fixed batch of 8 x 8192-token docs
   asks for one ~34 GB allocation, which MPS rejects outright.
   -> token-budget batch packing (batch * maxlen^2 <= budget_sq).
2. Allocator hoarding across distinct tensor shapes — with natural (ragged)
   padding every batch has a new shape, the MPS allocator caches a block per
   shape and never reuses across them; throughput decayed 488 -> 30 docs/s
   and the process OOMed at 23 GB hoarded.
   -> pad every batch up to a 64-token bucket (max ~128 distinct shapes) and
      empty_cache() periodically.
3. Output tensors parked on the GPU keep the pool full.
   -> convert_to_numpy is forced on.
"""

import hashlib
import logging
import math
import os
import sqlite3
import time

import numpy as np

logger = logging.getLogger("budget_batching")


class _EmbeddingDiskCache:
    """text-hash -> L2-normalized float32 vector, in one SQLite file.

    Track A (mteb) and Track B (cpersona store/recall) encode the same corpus
    and query texts with the same model; without this, the biggest corpus
    (LongMemEval, 237k docs, ~4-5 h on M5) is paid twice. Keyed by
    sha256(model_name + text); values are stored L2-normalized, which is
    lossless for bge-m3's cosine scoring.

    Enabled by setting EMB_CACHE_DIR in the environment.
    """

    def __init__(self, path: str, model_name: str):
        self._model = model_name
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS emb (k BLOB PRIMARY KEY, dim INTEGER NOT NULL, v BLOB NOT NULL)"
        )
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")

    def _key(self, text: str) -> bytes:
        return hashlib.sha256((self._model + "\x00" + text).encode("utf-8")).digest()

    def get_many(self, texts: list[str]) -> list[np.ndarray | None]:
        keys = [self._key(t) for t in texts]
        found: dict[bytes, np.ndarray] = {}
        CHUNK = 500
        for i in range(0, len(keys), CHUNK):
            chunk = keys[i : i + CHUNK]
            ph = ",".join("?" * len(chunk))
            for k, dim, v in self._db.execute(
                f"SELECT k, dim, v FROM emb WHERE k IN ({ph})", chunk
            ):
                found[bytes(k)] = np.frombuffer(v, dtype=np.float32, count=dim)
        return [found.get(k) for k in keys]

    def put_many(self, texts: list[str], vecs: np.ndarray) -> None:
        rows = [
            (self._key(t), int(v.shape[0]), v.astype(np.float32).tobytes())
            for t, v in zip(texts, vecs)
        ]
        self._db.executemany("INSERT OR REPLACE INTO emb VALUES (?, ?, ?)", rows)
        self._db.commit()


def install_budget_batching(st_model, budget_sq: float = None, max_batch: int = None):
    """Replace a SentenceTransformer's encode with token-budget dynamic batching.

    Defaults were tuned for bge-m3 (560M). Light models (MiniLM-L6: 22M,
    max_seq 256) are bound by max_batch rather than the token budget — raise
    EMB_MAX_BATCH for them. Env overrides: EMB_BUDGET_SQ / EMB_MAX_BATCH.

    Sorts ascending by token length (the short 99% of a memory corpus
    finishes first at full throughput), packs batches under the attention
    budget, quantizes padded shapes to 64-token buckets, and keeps the MPS
    allocator drained. The mteb wrapper, prompts, and normalization run
    unchanged.
    """
    import torch
    import torch.nn.functional as F

    if budget_sq is None:
        budget_sq = float(os.environ.get("EMB_BUDGET_SQ", 6.5e7))
    if max_batch is None:
        max_batch = int(os.environ.get("EMB_MAX_BATCH", 128))
    logger.info("budget batching: budget_sq=%.1e max_batch=%d", budget_sq, max_batch)

    tokenizer = st_model.tokenizer
    max_seq = st_model.get_max_seq_length() or 8192
    orig_encode = st_model.encode
    orig_tokenize = st_model.tokenize
    pad_id = tokenizer.pad_token_id or 0

    on_mps = torch.backends.mps.is_available()

    cache = None
    cache_dir = os.environ.get("EMB_CACHE_DIR")
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        model_label = os.environ.get("EMB_CACHE_MODEL", "BAAI/bge-m3")
        cache = _EmbeddingDiskCache(
            os.path.join(cache_dir, "embcache.sqlite3"), model_label
        )
        logger.info(f"    embedding disk cache: {cache_dir} (model={model_label})")

    def bucket_tokenize(texts, **kw):
        """Pad each batch up to the next 64-token bucket so tensor shapes
        repeat and MPS kernel/allocator caches actually get reused."""
        features = orig_tokenize(texts, **kw)
        ids = features.get("input_ids")
        if ids is not None and ids.dim() == 2:
            L = ids.shape[1]
            bucket = min(max_seq, math.ceil(L / 64) * 64)
            if bucket > L:
                features["input_ids"] = F.pad(ids, (0, bucket - L), value=pad_id)
                if "attention_mask" in features:
                    features["attention_mask"] = F.pad(
                        features["attention_mask"], (0, bucket - L), value=0
                    )
                if "token_type_ids" in features:
                    features["token_type_ids"] = F.pad(
                        features["token_type_ids"], (0, bucket - L), value=0
                    )
        return features

    st_model.tokenize = bucket_tokenize

    def budget_encode(sentences=None, *, inputs=None, **kwargs):
        # sentence-transformers 5.x renamed the first parameter to `inputs`
        # (encode_query/encode_document delegate with inputs=...); accept both.
        if sentences is None:
            sentences = inputs
        if isinstance(sentences, str):
            return orig_encode(sentences, **kwargs)
        sentences = list(sentences)
        n = len(sentences)
        if n <= 1:
            kwargs["batch_size"] = 1
            return orig_encode(sentences, **kwargs)

        kwargs["show_progress_bar"] = False
        # Results must leave the GPU immediately or they keep the pool full.
        kwargs["convert_to_numpy"] = True
        kwargs.pop("convert_to_tensor", None)
        # Always normalize: cached vectors must be model-state, not call-state.
        # bge-m3 scoring is cosine on both tracks, so this is score-neutral.
        kwargs["normalize_embeddings"] = True

        out: list = [None] * n
        todo = list(range(n))
        if cache is not None:
            hits = cache.get_many(sentences)
            todo = []
            for i, h in enumerate(hits):
                if h is not None:
                    out[i] = h
                else:
                    todo.append(i)
            if len(todo) < n:
                logger.info(f"    cache: {n - len(todo)}/{n} hits")
        if not todo:
            return np.asarray(out, dtype=np.float32)

        lengths: dict[int, int] = {}
        for s in range(0, len(todo), 2048):
            chunk = todo[s : s + 2048]
            enc = tokenizer([sentences[j] for j in chunk], truncation=True, max_length=max_seq)
            for j, ids in zip(chunk, enc["input_ids"]):
                lengths[j] = len(ids)
        order = sorted(todo, key=lambda j: lengths[j])  # ascending

        i = 0
        batches = 0
        t0 = time.time()
        win_t0, win_i = t0, 0
        m = len(order)
        while i < m:
            # Grow the batch greedily: ascending order means the candidate at
            # i+cap is always the would-be max of the extended slice, so the
            # budget check is exact (a two-pass probe/recompute scheme here
            # widened slices past long docs and asked MPS for 48 GB).
            maxlen = max(lengths[order[i]], 16)
            cap = 1
            while cap < max_batch and i + cap < m:
                nxt = max(lengths[order[i + cap]], 16)
                if (cap + 1) * nxt * nxt > budget_sq:
                    break
                cap += 1
                maxlen = nxt
            idx = order[i : i + cap]
            kwargs["batch_size"] = len(idx)
            embs = orig_encode([sentences[j] for j in idx], **kwargs)
            for j, e in zip(idx, embs):
                out[j] = e
            if cache is not None:
                cache.put_many([sentences[j] for j in idx], np.asarray(embs))
            i += len(idx)
            batches += 1
            if on_mps and (batches % 25 == 0 or maxlen >= 2048):
                torch.mps.empty_cache()
            if batches % 25 == 0 or (maxlen >= 2048 and batches % 5 == 0):
                win_rate = (i - win_i) / max(time.time() - win_t0, 1e-9)
                logger.info(f"    encoded {i}/{m} ({win_rate:.0f} docs/s now, maxlen={maxlen})")
                win_t0, win_i = time.time(), i

        # float32 output: downstream numpy similarity math on float16 is
        # precision- and overflow-fragile.
        return np.asarray(out, dtype=np.float32)

    st_model.encode = budget_encode
