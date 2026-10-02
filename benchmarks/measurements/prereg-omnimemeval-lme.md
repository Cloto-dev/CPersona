# CPersona on OmniMemEval's LongMemEval-S pipeline

Registered before the full ingestion and before any answer or judge call. The
commit that adds this file is the evidence of the order.

## Question

[OmniMemEval](https://github.com/MemTensor/OmniMemEval) runs memory products
through one harness (same data, prompts, answer model, judge and metric code)
and publishes accuracy beside **Context Tokens**, the average tokens sent to the
answer model per question (the answer prompt plus the retrieved context). Where
does CPersona sit on that table, and does v1.2 move it?

## Instrument

- OmniMemEval at `0b1ea8d` (user-memory track), LongMemEval-S cleaned, all 500
  questions. The data file the harness downloads has SHA-256 `d6f21ea9d60a…`, the
  same file as this repository's earlier LongMemEval studies.
- Unchanged: prompts, answer model `gpt-4.1-mini` (`2025-04-14`), judge
  `gpt-4o-mini`, one judge run per question, temperature 0, metric and token
  counting code.
- Added to the harness: a CPersona client (`scripts/client_factory/cpersona_client.py`),
  its two registry names, a row in the session-key list and in the search
  dispatch table. Nothing else.

## The adapter

- **Store**: one haystack session is one record, a date line and then one
  paragraph per turn prefixed by its role, written through CPersona's `store` at
  the session's time. Sessions run to 78,174 characters (3,686 of 23,867 exceed
  CPersona's default write bound of 16,000), so the server runs with
  `CPERSONA_MAX_CONTENT_LENGTH=80000`; a truncated write fails the session.
- **Search**: `reconstruct` at the server's defaults. The harness's `top_k` (20)
  is not passed: CPersona's maximum count is 10, which is also its default.
- **Rendering**: one block per item, the record's time in brackets, then the
  text the item quotes (head quote, then any excerpts). JSON metadata is not
  passed to the answer model.
- **No model** is called by CPersona to store or recall.
- Server settings otherwise default: fusion `rrf`, block reach on, local vector
  search, embeddings `bge-m3` (512-token window) from a local server that caches
  vectors by text.

## Arms and order

| Arm | Code | Harness name |
| --- | --- | --- |
| A | 2.6.3a1, `70b03ae` | `cpersona263` |
| B | v1.2, `4c01ccc` | `cpersona` |

1. Arm A's server ingests all 500 haystacks.
2. Its task queue, which builds each record's nodes and blocks after the store
   returns, is waited out until it stays empty, and `check_health` must report
   no record missing nodes or blocks (any found are repaired and the count is
   reported) **before any search**.
3. The store is copied, one copy per arm, and each arm searches its own copy
   through its own server, into a results directory of its own.
4. Answer and judge calls for both arms.

**Smoke, done before this registration** (two questions, ingestion and search
only, no model): a search run right after ingestion read records whose blocks
were not built yet (283 tasks still queued), and the harness's search step
reuses an existing results file instead of searching again. Both are why steps 2
and 3 are written as they are.

## Measures

Overall and per-type accuracy as the harness judges it; Context Tokens as the
harness counts them; search latency (descriptive only: a local server on a
laptop, not a hosted service).

## What may be said

- **Each arm's row**, as measured, with the harness commit, the settings above,
  and the note that OmniMemEval's other rows were run by its maintainers against
  hosted services and that the harness makes no claim of optimal settings for any
  product.
- **Against another product** in the same harness snapshot, a claim that
  CPersona does better needs both higher accuracy and fewer Context Tokens. Any
  other comparison is reported as the two numbers side by side, without a
  ranking word.
- **v1.2 against 2.6.3a1**: a gain in accuracy is claimed when the 95% bootstrap
  interval of the paired difference in correct answers (10,000 resamples over
  questions, seed 20261007) lies above 0; a reduction in Context Tokens when the
  95% bootstrap interval of the median per-question ratio B / A lies below 0.90.
  Otherwise the two are reported without a claim.

One run. A rerun is made only for an infrastructure failure, and is reported.
Nothing in the adapter or the settings changes after any answer is seen.
