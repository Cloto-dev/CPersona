# Results: recall latency on the reference machine, with the query embedded by a real model

Registration: [prereg-recall-latency-with-embedding.md](prereg-recall-latency-with-embedding.md),
pushed at 2026-09-19 10:46 UTC in `5c29ba7`, before the first run. Driver:
[perf_recall_with_embedding.py](perf_recall_with_embedding.py). Records:
[recall_latency_with_embedding/](recall_latency_with_embedding/).

**Verdict.** On the Intel N150, recall over 100,000 memories took a median of
**446.6 ms including embedding the query** with jina-v5-nano, and every one of the 25
timed queries finished under 520 ms. The registered rule allows the statement that
recall over 100,000 memories completes in under one second on an N100-class PC,
including embedding the query. The secondary bge-m3 arm failed the registered validity
check on both of its runs and supports no statement.

## Setup

Intel N150 (4 threads, `governor=performance`), Python 3.11.2, numpy 2.4.6. CEmbedding
0.8.0 from PyPI (`cembedding[onnx]`, onnxruntime 1.30.0) on the same machine at
`127.0.0.1:8401`, started with `EMBEDDING_PROVIDER=onnx_jina_v5_nano` or `onnx_bge_m3`.
The tree was `master` at `e255143` plus the registration and the driver. Synthetic corpus
of 100,000 memories at the model's width, contiguous index built, scan window 100,000,
default configuration (`rrf`, confidence off, FTS on), no episodes, response limit 10.
Queries `topic {i} question`, which the lexical arms match on every row. Each of the 25
timed texts was recalled once with the stand-in vector and once through the real client,
in alternating order; three distinct warm-up texts first; 25 further distinct texts timed
through the client alone.

## Primary arm: jina-v5-nano (768 dimensions)

| | median | p95 | max | min |
| --- | ---: | ---: | ---: | ---: |
| `do_recall`, query embedded by the model | **446.57 ms** | 487.59 | **511.06** | 438.49 |
| `do_recall`, stand-in vector (same texts, same run) | 414.28 ms | 462.25 | 484.43 | 408.06 |
| paired difference (real − stand-in) | 32.05 ms | 48.82 | 73.31 | −15.97 |
| embedding one query, client call alone | 36.20 ms | 38.02 | 38.09 | 35.43 |

The registered rule — median under 1,000 ms and every sample under 1,000 ms — is met
with the largest sample at 511 ms. Embedding the query costs about 36 ms, under a tenth
of the recall.

Checks the registration asked for: the model's width matched the corpus (768); every
real-client call returned a vector; the client's cache held 54 entries at the end, one
per distinct text (1 width probe + 3 warm-up + 25 timed + 25 embed-only), so no timed
query was answered from the cache; every recall returned 10 rows in both arms.

## Secondary arm: bge-m3 (1024 dimensions) — not readable under the rule

| Run | load at arm start | `do_recall` real, median / max | stand-in, median | embed alone, median |
| --- | ---: | --- | ---: | ---: |
| 1 | 1.22 | 452.00 / 528.69 ms | 416.23 ms | 39.92 ms |
| 2 (the one permitted repeat) | 1.44 | 445.36 / 517.61 ms | 420.33 ms | 39.59 ms |

The registration invalidates an arm whose one-minute load average exceeds 1.0 when it
starts and permits one repeat. Both runs exceeded it, so this arm supports no statement;
the figures are recorded as measured and not read.

## The validity check measured the harness itself

The load average is sampled inside the driver after it has built the 100,000-row corpus
and the index, so a one-minute average taken at that moment is dominated by the
driver's own build. The process list recorded at the same point does not settle it
either way: `ps` reports each process's CPU share averaged over its lifetime, which puts
the driver at 78–100%, the embedding server at 41–59% and a long-running virtual machine
on the host at about 19% of one core, none of them an instantaneous reading. The
primary arm passed at
**0.9995**, below the 1.0 line by the smallest of margins and for the same reason the
secondary arm failed. The check therefore did not separate outside load from the
measurement's own. It is reported rather than replaced: changing the rule after seeing
which arm passed would be choosing the result. The timed loop runs after the build has finished, and
its samples are tight (jina-v5-nano real-client arm: 438–511 ms, interquartile range
about 10 ms), which is not the shape of a loop competing with a heavy neighbour — but
that is a reading of the samples, not a check the registration defined.

## Why the stand-in figure is not 299 ms

The stand-in arm above came out near 415 ms, where the earlier record says 299 ms for the
same corpus shape. The runs below are diagnostic: none of them was registered, and none
changes the verdict. Same machine, same day.

**Not the machine, and not a slowdown in the code.** `perf_contiguous_index.py` itself,
run on two trees, 1024 dimensions, window 100,000, 15 queries:

| Tree | `do_recall` with the index (median) |
| --- | ---: |
| v2.5.11 | 292.0 ms (the record: 299 ms) |
| `master` at `e255143` | 248.7 ms |

**Not the harness shape.** [diag_recall_latency_harness.py](diag_recall_latency_harness.py)
changes the older harness's conditions one at a time on one `master` corpus, stand-in
vector throughout, no embedding server running:

| Condition | median, run 1 | median, run 2 (a full scan first) |
| --- | ---: | ---: |
| A: distinct texts, `do_recall` alone | 247.5 ms | 247.5 ms |
| B: `_search_vector` on the same text first | 254.2 ms | 250.1 ms |
| C: texts already recalled | 288.3 ms | 269.1 ms |
| A again, at the end | 250.1 ms | 324.7 ms |

**The embedding server on the same four cores.** The primary arm repeated with the
server limited to one inference thread (`ONNX_INTRA_OP_THREADS=1`; CEmbedding's default,
0, lets the runtime use every physical core):

| jina-v5-nano | default threads | one thread |
| --- | ---: | ---: |
| `do_recall`, stand-in vector, median | 414.3 ms | **243.5 ms** |
| `do_recall`, query embedded by the model, median / max | 446.6 / 511.1 ms | **290.6 / 414.0 ms** |
| embedding one query alone, median | 36.2 ms | 59.8 ms |

With the server free to use all four cores, recall that runs right after an embedding
request loses about 170 ms, whether or not that recall used the model's vector — the
stand-in arm in the alternating loop always follows a real-client call. Limited to one
thread, the model embeds more slowly but the recall around it returns to the 245 ms the
harnesses above report. A runtime that keeps its worker threads spinning for a while
after each request would produce exactly this; that mechanism is inferred from the
numbers, not observed. The practical reading: on a machine this small, an embedding
server sharing the cores should be given fewer threads than there are cores. The
registered figure above is the default configuration, and it is the one this record's
verdict rests on.

## Limits

- One machine, one run per arm, 25 queries. A synthetic corpus, so nothing here is about
  recall quality.
- The default configuration without episodes. The production configuration with 20,000
  episodes measured 360.4 ms with the stand-in vector in
  [results-recall-path-profile.md](results-recall-path-profile.md); it was not re-run with
  a real model.
- The embedding server was warm and served one request at a time. A cold start, and
  concurrent requests, are outside this record.
- The one-thread figures and the harness diagnostics were not registered. They explain
  the difference from 299 ms; they are not a second verdict.
