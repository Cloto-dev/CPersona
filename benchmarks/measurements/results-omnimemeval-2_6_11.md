# CPersona 2.6.11 on OmniMemEval, and against OpenViking 0.5.0: results

All runs: 2026-10-19, CPersona at tag `v2.6.11`, `reconstruct` in `mode: "pro"`
unless a row says otherwise, embeddings from `bge-m3` on a local server. Each
number is one run by this project. The rules for the OpenViking comparisons were
written down before any arm was answered or judged and are restated here
unchanged; departures from a clean run are listed under each section.

## Settings

- **Server**: CPersona 2.6.11 from the tag, over streamable HTTP on loopback,
  with `CPERSONA_MAX_CONTENT_LENGTH=80000` (default 16,000) so that no
  LongMemEval or LoCoMo session is cut when it is stored; every other setting
  at its default. The earlier runs used the same value
  ([results-omnimemeval-lme.md](results-omnimemeval-lme.md)).
- **Embeddings**: `BAAI/bge-m3` through sentence-transformers, normalized, with
  a 512-token window, served over HTTP by a local process.
- **Harness client**: OmniMemEval's pipeline at commit `0b1ea8d`, with
  [`omnimemeval-0b1ea8d-2_6_11.patch`](../omnimemeval/omnimemeval-0b1ea8d-2_6_11.patch)
  applied (it registers the client, stores each LoCoMo session once, as the
  harness's speaker-A view, which carries both speakers' turns, in one store per
  conversation, and searches that store with the harness's `shared_conv_search`,
  as for EverOS and Supermemory) and this project's CPersona
  client ([`cpersona_client.py`](../omnimemeval/cpersona_client.py)) added, which
  calls `reconstruct` in the mode a row names (`pro` or `lite`) with that mode's
  default count. The modes are `reconstruct`'s two response sizes: `pro` returns
  up to 15 items within 5,000 tokens of response JSON and holds two more places
  for block retrieval; `lite` returns the default 10 items within 3,000 tokens.
  Both hold one place for a keyword match.

## LongMemEval-S, 500 questions

OmniMemEval's LongMemEval pipeline (commit `0b1ea8d`; later commits up to the
current `main` change no pipeline code), the cleaned LongMemEval-S
(`longmemeval_s_cleaned.json`, sha256 `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`),
answers by `gpt-4.1-mini-2025-04-14` and one judge run by `gpt-4o-mini` (which
resolved to `gpt-4o-mini-2024-07-18` that day),
as in [OmniMemEval's results](https://github.com/MemTensor/OmniMemEval/blob/main/docs/user_memory/results.md#longmemeval).
Context Tokens is the average answer-stage prompt as the API reports it: the
answer prompt plus the rendered retrieval: 2,131,237 tokens over 500 calls in
pro mode and 496,670 in lite mode, none estimated.

| Backend | Deployment | SS-User | SS-Asst | SS-Pref | Temp. Reas | Multi-S | Know. Upd | Overall | Context Tokens |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| CPersona 2.6.11 (pro mode) | local | 97.14 | 94.64 | 96.67 | 86.47 | 85.71 | 88.46 | 89.60 | 4,262.5 |
| CPersona 2.6.11 (lite mode) | local | 95.71 | 83.93 | 90.00 | 80.45 | 74.44 | 82.05 | 82.20 | 993.3 |
| MemOS | cloud | 100.00 | 100.00 | 100.00 | 89.47 | 78.95 | 84.62 | 89.20 | 4,151 |

The MemOS row is OmniMemEval's reproduction, quoted. Against the 12 backends in
that table, 89.60% is the highest Overall. MemOS is level with it: two runs of
500 questions differ by about ±3.8 points at 95% from sampling alone, and the gap
is 0.4. Every other backend in the table is at least 9.2 points lower (EverOS,
80.40%). CPersona answers more multi-session and knowledge-update questions than
MemOS, and fewer temporal and single-session ones. It sends 2.7% more tokens per
question.

In lite mode CPersona answers 82.20% at 993.3 tokens a question: more than every
backend in that table except MemOS, with fewer tokens than all of them but Mem0's
cloud service (856 tokens, 56.00%).

The median search took 570 ms (p95 769 ms) with two questions in flight. The
machine was under memory pressure from other measurements at the time, so this
is not a latency claim.

What differs from a clean run:

1. **Calibration.** The run reuses the calibration file of this project's
   earlier LongMemEval-S store (same data, same embedding model): one global
   similarity threshold, no per-user entry applying to any of the 500 users.
   Calibrating each user costs about 200 recalls; on this store that is about
   16 hours for the 500. The server restored the file at startup rather than
   recalibrating while searching. The same file was behind this project's
   earlier LongMemEval runs ([results](results-omnimemeval-lme.md)).
2. **Ingestion, second attempt.** The first ingestion (four workers) lost 339
   embedding calls to a refused connection on the embedding server and was
   discarded. The second, with two workers, stored all 500 users and 23,867
   sessions, the dataset's total, with no embedding failure and no record left
   without its nodes.
3. **Lite on the same store.** The lite run searched the store the pro run had
   searched, with the same calibration file, restored at startup. Under the
   default settings a recall does not record anything the ranking reads, so a
   second search of the store sees it as the first did.

## LoCoMo, 1,540 questions

OmniMemEval's LoCoMo pipeline, same answer and judge models. The store was
ingested once and copied for the two modes before any search.

| Backend | Deployment | Single-Hop | Multi-Hop | Temporal | Open-Domain | Overall | Context Tokens |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| CPersona 2.6.11, pro mode | local | 88.70 | 78.01 | 83.18 | 63.54 | 84.03 | 4,441.0 |
| CPersona 2.6.11, lite mode | local | 73.84 | 51.06 | 72.59 | 54.17 | 68.18 | 1,485.0 |
| MemOS | cloud | 92.51 | 88.65 | 85.05 | 69.79 | 88.83 | 5,400 |

84.03% is second to MemOS among the 14 backends OmniMemEval reproduces on
LoCoMo, ahead of Cognee (83.48%), EverOS (82.75%) and Hindsight (81.99%). Lite
mode answers 68.18% at 1,485.0 tokens.

What differs from a clean run: each conversation's user was calibrated once
before the store was copied, but on these small stores (19 to 32 records a user)
the recall gate cannot be calibrated, so the server recalibrates the whole store
at every start while it serves, and both searches ran beside that. A rerun that
waited for the startup calibration to finish returned the same context for every
one of the 1,540 questions in both modes (every user's threshold came out the
same each time, and the gate stayed at its default), so the accuracy here stands.
The original runs' search latency does not; the next section uses the rerun's.

## Against OpenViking 0.5.0

[OpenViking](https://github.com/volcengine/OpenViking) 0.5.0 from PyPI, run
locally by this project through the same harness as CPersona. Its write path
calls a generative model to extract memories; CPersona's calls none. The
comparison asked whether CPersona answers about as well while costing clearly
less.

### LoCoMo (registered comparison)

OpenViking was given the same local `bge-m3` server for embeddings,
`gpt-4.1-mini` as its extraction model (the model OmniMemEval uses for a
backend's own model calls), `find` with limit 50, no rerank, and up to 30,000
characters of retrieved text per question. Each session was committed and its
extraction awaited before the next. Both systems searched one question at a
time, one system after the other. The latency below was measured again for both
systems on the same day, after the rerun described above: the registered
measurement had CPersona's searches running beside its startup recalibration.

Registered rules: accuracy is **about equal or better** when the paired
difference's one-sided 95% lower bound is above −3 points; the **cost lead is decisive**
only if all three hold: (1) no generated tokens at write time, (2) the context
the answer model sees, de-duplicated and counted in `cl100k_base`, is at most
half of OpenViking's, (3) the median search latency is not higher.

| Arm | Accuracy | Context seen (cl100k, mean) | Search median / p95 | Generated tokens at write |
| --- | ---: | ---: | ---: | ---: |
| CPersona, pro mode | 84.03% | 4,031.2 | 72.7 / 83.8 ms | 0 |
| CPersona, lite mode | 68.18% | 1,003.2 | 29.8 / 36.7 ms | 0 |
| OpenViking 0.5.0 | 59.09% | 6,937.2 | 50.0 / 56.3 ms | 2,314,992 |

- **Accuracy: held, by a wide margin.** Pro answered 480 questions OpenViking
  missed and missed 96 it answered: +24.9 points, one-sided 95% lower bound +22.6.
- **Cost lead: not decisive.** (1) holds. (2) fails: Pro's context is 0.58 of
  OpenViking's, not half. (3) fails: 72.7 ms against 50.0 ms (the registered
  measurement gave 80.8 against 49.3, the same verdict). Lite mode searches
  faster than OpenViking, 29.8 ms.
- A reported arm (no rule) replaced the embedding model with OpenAI's
  `text-embedding-3-large`: OpenViking answered 57.27% (5,902.1 tokens seen,
  2,286,522 generated at write), so the embedding model does not explain the gap.
  OpenViking's recommended rerank was not tested.
- OpenViking's first answer pass hit the answer model's rate limit on 52
  questions; as registered, those were answered once more, and every question
  has an answer and a judgement.

### LongMemEval, OAMB's 60-question screen

The 60 questions of the [Open Agent Memory Benchmark](https://github.com/rocke2020/open-agent-memory-benchmark)
v0.1.0 (ten for each of the six types, four with no answer in the haystack),
answered and judged the way OAMB does it, with its prompt templates byte for byte
(commit `9117145`): `deepseek-flash` (DeepSeek-V4.1-Flash-1017 on the day) at
temperature 0, reasoning effort low for the answer and high for the judge.
OpenViking used `deepseek-flash` for extraction, `Qwen3-Embedding-0.6B`
(float32, on the CPU) and OAMB's retrieval budget: `find` limit 150, no
character cap. CPersona's contexts are the 60 questions' contexts from the
500-question run above. Both systems ingested through OmniMemEval's LongMemEval
pipeline rather than OAMB's own ingestion text.

Registered rules: CPersona is **more accurate** if an exact one-sided binomial
test on the questions only one system got right gives p < 0.05; **about equal**
if not, and the paired lower bound is above −3 points; otherwise undetermined.
Cost: (1) no generated tokens at write time, (2) the evidence shown to the
answer model at most half of OpenViking's (`cl100k_base`, mean).

| | CPersona 2.6.11 | OpenViking 0.5.0 |
| --- | ---: | ---: |
| Correct | 52 / 60 | 51 / 60 |
| Evidence shown (cl100k, mean) | 4,049.0 | 17,105.8 |
| Generated tokens at write | 0 | 36,958,070 |

- **Accuracy: undetermined.** 6 questions only CPersona answered, 5 only
  OpenViking: p = 0.50, one-sided 95% lower bound −7.4 points. Sixty questions cannot tell
  these two apart.
- **Cost: both conditions hold.** No generated tokens against 37.0 million, and
  0.24 of the evidence.
- By type, CPersona answered all 10 single-session-assistant questions to
  OpenViking's 5; OpenViking answered 8 multi-session questions to CPersona's 6.
- OpenViking's answers agree with OAMB's published OpenViking 0.4.19 results on
  56 of the 60 questions, and both score 51.

What differs from a clean run, OpenViking's ingestion:

1. The first attempt was discarded: the embedding server ran out of GPU memory
   14 minutes in, and OpenViking logged 1,167 failed embeddings. The second
   attempt embedded on the CPU.
2. The second attempt stopped at 2,600 of 2,826 sessions when the model API
   account ran out of credit, and the harness's resume added the remaining 226
   without a failure. 58 sessions had been half-extracted when the credit ran
   out and were extracted again from the start; one session failed earlier on a
   lock timeout inside OpenViking and was extracted after the later sessions of
   its user.

### Beside OAMB's published results

Not run by this project. The other systems' numbers are OAMB's (its September
run of DeepSeek-V4.1-Flash, with its own ingestion text), re-counted in
`cl100k_base` from its published contexts.

| System | Correct | Evidence shown (cl100k, mean) | Only CPersona right / only it right |
| --- | ---: | ---: | --- |
| CPersona 2.6.11 (this run) | 52 / 60 | 4,049 | — |
| Hindsight | 57 / 60 | 16,171 | 2 / 7 |
| Mem0 | 52 / 60 | 6,607 | 5 / 5 |
| OpenViking 0.4.19 | 51 / 60 | 15,657 | 7 / 6 |

## What this does not show

- That CPersona is ahead of MemOS on LongMemEval-S: the two are level within one
  run's sampling error.
- A ranking beyond OmniMemEval's same-pipeline tables. Several systems publish
  higher LongMemEval scores from their own setups (models, prompts, judges),
  which OmniMemEval lists as references rather than reproductions.
- That CPersona is cheaper than OpenViking on every axis: on LoCoMo its pro mode
  searched slower and its context was not half of OpenViking's.

Earlier CPersona runs on the same pipeline (2.6.3a1, 80.80%; 2.6.4a1, 81.60%) and
the rules they were judged by: [results-omnimemeval-lme.md](results-omnimemeval-lme.md).
