# Results: CPersona on OmniMemEval's LongMemEval-S pipeline

Registration: [prereg](prereg-omnimemeval-lme.md) (`e41f5fc`, committed before
the full ingestion and before any answer or judge call). Harness:
[OmniMemEval](https://github.com/MemTensor/OmniMemEval) at `0b1ea8d`, with the
CPersona client and the six-line registration patch in
[`benchmarks/omnimemeval/`](../omnimemeval/). One run of all 500 questions per
arm; every search, answer and judge call succeeded.

**Verdict.**

- **v1.2 (arm B): 81.60% at 1,786.7 Context Tokens.** 2.6.3a1 (arm A): 80.80% at
  2,354.6.
- **v1.2 against 2.6.3a1** (the registered comparison): no claim about accuracy
  (408 against 404 correct, paired difference +4, 95% interval −10 to +18); a
  reduction in Context Tokens is claimed (median per-question ratio 0.723, 95%
  interval 0.720 to 0.728, below the registered 0.90).
- **Against the reproduced rows of the same harness snapshot**, under the
  registered rule (higher accuracy and fewer Context Tokens): v1.2 meets it
  against 10 of the 12; MemOS is more accurate, and Mem0 (cloud) uses fewer
  tokens. Several of the accuracy differences are within one run's sampling
  error (below).

Arm B ran v1.2 at `4c01ccc`. 2.6.4a1 adds to that code only measurement documents
and a `check_health` check that reads no search path, so with the coarse-search
settings off, as here, 2.6.4a1 returns the same search results.

## The two arms

In OmniMemEval's format: LLM-as-judge accuracy (`gpt-4o-mini-2024-07-18`, one
judge run), answer model `gpt-4.1-mini-2025-04-14`, Context Tokens = average
answer-stage prompt tokens as the API reports them (answer prompt plus the
rendered retrieval).

| Backend | Deployment | SS-User | SS-Asst | SS-Pref | Temp. Reas | Multi-S | Know. Upd | Overall | Context Tokens |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| CPersona 2.6.3a1 | local | 90.00 | 78.57 | 86.67 | 85.71 | 67.67 | 85.90 | 80.80 | 2,354.6 |
| CPersona v1.2 (2.6.4a1) | local | 91.43 | 80.36 | 90.00 | 83.46 | 71.43 | 84.62 | 81.60 | 1,786.7 |

Questions per type: 70, 56, 30, 133, 133, 78.

## Against the reproduced rows

The other rows are OmniMemEval's "Reproduced Results" for LongMemEval at
`0b1ea8d` (`docs/user_memory/results.md`), run by its maintainers through the
same pipeline, answer model and judge, against each backend's cloud service or
a local deployment. The harness makes no claim of optimal settings for any
backend. Lower Context Tokens mean better token efficiency when accuracy is
comparable.

| Backend | Deployment | Overall | Context Tokens | v1.2: higher accuracy and fewer tokens |
| --- | --- | ---: | ---: | --- |
| MemOS | cloud | 89.20 | 4,151 | no (accuracy) |
| **CPersona v1.2 (2.6.4a1)** | local | **81.60** | **1,786.7** | — |
| CPersona 2.6.3a1 | local | 80.80 | 2,354.6 | — |
| EverOS | local/self-hosted | 80.40 | 12,379 | yes |
| graphiti-zep | local/self-hosted | 79.80 | 117,106 | yes |
| mem9 | cloud | 78.00 | 3,805 | yes |
| Letta | cloud | 77.67 | 49,431 | yes |
| Hindsight | local/self-hosted | 72.20 | 29,755 | yes |
| Supermemory | cloud | 66.07 | 6,635 | yes |
| MemMachine | local/self-hosted | 63.60 | 2,803 | yes |
| Viking | cloud | 61.07 | 2,291 | yes |
| Mem0 | cloud | 56.00 | 856 | no (tokens) |
| Cognee | local/self-hosted | 51.80 | 10,305 | yes |
| Memori | cloud | 20.80 | 2,779 | yes |

2.6.3a1 meets the same rule against 9 of the 12 (not Viking, whose 2,291 tokens
are fewer than its 2,354.6).

One run of 500 questions has a 95% interval of about ±3.5 points on an overall
accuracy near 81%. The differences from EverOS (+1.2), graphiti-zep (+1.8), mem9
(+3.6) and Letta (+3.9) are inside that; against them the fair reading is the
same accuracy at between half and one sixty-fifth of the tokens.

The same file lists vendors' self-reported LongMemEval results (for example
Supermemory 95.0%, Hindsight 94.6%, Mem0 94.4%, Backboard.io 93.4%, Zep 90.2%).
They come from other pipelines and settings and are not compared here.

## v1.2 against 2.6.3a1

| Measure | Registered rule | Measured | Claim |
| --- | --- | --- | --- |
| Correct answers, paired difference | 95% bootstrap interval above 0 | +4 (−10 to +18) | no |
| Context Tokens, median per-question ratio B / A | 95% bootstrap interval below 0.90 | 0.723 (0.720 to 0.728) | yes |

Bootstrap: 10,000 resamples over questions, seed 20261007. The arms disagreed on
54 questions: B alone right on 29, A alone right on 25. B's context was the
smaller on 499 of the 500 questions.

The per-question count is the one the harness records for each question: the
retrieved context in `cl100k_base` tokens (arm A mean 2,112.3, arm B 1,531.4).
The API-reported prompt tokens it also keeps are totals only. Adding a fixed
250-token answer prompt to every question gives a median ratio of 0.751 (0.749
to 0.756); the totals give 0.759.

## Run record

What departed from a single uninterrupted run, in order:

1. **Ingestion and search ran as registered.** Arm A ingested all 500 haystacks
   (23,867 sessions); the build queue was waited out (30,063 s) and
   `check_health` reported no record missing nodes or blocks, then again on each
   copy of the store; each arm searched its own copy.
2. **The first answer launch stopped before any API call:** the harness found no
   `python` on the path (its virtual environment was not active).
3. **The second launch hit the account's rate limit** (200,000 tokens per minute
   for `gpt-4.1-mini`) at the default 10 concurrent answer calls, and was
   stopped within two minutes. Its partial responses were moved aside unread,
   and the answer stage started again from nothing at 3 concurrent calls.
   Concurrency changes no prompt, model or temperature.
4. **Arm A:** 498 questions were answered in that pass and 2 failed after their
   retries; the harness's resume, which asks again only for missing answers,
   answered the 2 with the same settings. **Arm B** completed in one pass.
5. **The harness rewrites its token-usage file on every pass** instead of adding
   to it, so arm A's own report gives 2,398 tokens over the 2 resumed calls. Arm
   A's Context Tokens were recounted by running the answer step alone on arm A's
   saved search results, in a separate directory, with the same settings: 499
   plus 1 calls, all usage-reported, none estimated. Prompt tokens depend only on
   the input. The recount's answers were not used; accuracy is the run's.
6. **Answers were seen before judging finished.** The harness prints each
   generated answer to its log, and a few were read while monitoring progress.
   No setting or adapter changed after that.
7. **The per-question token count** used for the registered ratio was chosen
   after the run, because the registration did not say which of the harness's
   two counts to use.

Search latency, descriptive only: mean 26.6 s for arm A and 24.4 s for arm B
(p95 42.6 s and 39.2 s), on a laptop serving one store of all 500 haystacks with
a local embedding server. **These figures are not the cost of a search.** Each
server started on a copy of the store without a calibration file, so it began
calibrating its global threshold and then each of the 500 agents in the
background, 29 to 36 minutes per agent. Both servers were calibrating through
both arms' searches, six agents each by the time the searches ended.

Arm B was searched again on 2026-10-03 with the calibration that run had written
restored at startup, so no calibration ran: one server, the harness's search
wrapper and client with the same two workers and the same user ids
([`search_driver.py`](../omnimemeval/search_driver.py)). Mean 0.57 s,
median 0.54 s, p95 0.88 s, maximum 1.38 s, and all 500 contexts identical to the
run's, byte for byte. The embedding server had been replaced by one that keeps
the same cache on disk; it returned the vectors of all 500 questions unchanged.
Neither set of figures is comparable with a hosted service.

## Embedding backend

Both arms reached the same local embedding server over HTTP
(`CPERSONA_EMBEDDING_MODE=http`, `CPERSONA_EMBEDDING_MODEL=bge-m3`).

| Setting | Value |
| --- | --- |
| Model | `BAAI/bge-m3`, Hugging Face snapshot `5617a9f61b02` |
| Output | dense vectors only, 1,024 dimensions, float32, normalized to unit length |
| Window | 512 tokens (`max_seq_length`) |
| Runtime | sentence-transformers 5.6.0, PyTorch 2.12.1, transformers 5.12.1, Python 3.11.15, Apple MPS, batch 32 |
| Cache | each text's vector computed once and kept under the text's SHA-256, so both arms received the same vector for the same text |
| Endpoints | `/embed` and `/count_tokens`; no `/capabilities`, so stored vectors carry the configured name as their label, not a model fingerprint |

The CPersona servers otherwise ran with `CPERSONA_VECTOR_SEARCH_MODE=local`,
`CPERSONA_STORE_BLOB=true`, `CPERSONA_FTS_ENABLED=true` and
`CPERSONA_MAX_CONTENT_LENGTH=80000`, defaults elsewhere (fusion `rrf`, block reach
on). Long records are split into 512-token windows for their nodes and blocks; a
block keeps its vector as int8 values and sign bits.

CEmbedding's `onnx_bge_m3` provider serves the same model through ONNX Runtime,
with a window set by `ONNX_MAX_SEQ_LEN` (default 2,048). A deployment that keeps
that default embeds up to four times as much of a long record into one vector as
this run did, and even at 512 its vectors are not guaranteed to match this run's to the bit.

## What this setup does not exercise

The harness searches once with the question text and answers from that result.

- No time cue: a question's stated period is not passed, so `time_cue` is unused
  (133 questions are temporal-reasoning).
- No declared associations or corrections: sessions are stored as text; nothing
  declares entities or `supersedes` relations (78 questions are knowledge-update).
- No expansion or second search: an agent can widen a quote with `get_contents`
  or search again; a single-shot pipeline cannot.
- At most 10 items: `reconstruct`'s maximum count. The harness's `top_k` of 20 is
  not passed.
- No model at write time. Several other backends extract or summarise with a
  model when they store; CPersona stores the session text as it is.

## Limits

- One run per arm, run by this project, not by the harness's maintainers. The
  other rows were run at other times against other deployments.
- LongMemEval is this line's main development instrument, so the line's design
  has been measured on these questions before; nothing was tuned in this run.
- One judge run per question, by a model; the judge's own error is not
  measured.
