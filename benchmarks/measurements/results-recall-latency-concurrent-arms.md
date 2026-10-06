# Results: recall with its arms run beside each other, and an embedding server that does not spin

Registration: [prereg-recall-latency-concurrent-arms.md](prereg-recall-latency-concurrent-arms.md),
pushed in `6aa0202` at 05:32 UTC on 2026-10-06, before the run at 06:33 UTC. Records:
[arms-A.json](recall_latency_realistic/arms-A.json),
[arms-B.json](recall_latency_realistic/arms-B.json),
[arms-C.json](recall_latency_realistic/arms-C.json),
[arms-D.json](recall_latency_realistic/arms-D.json), the machine state before and after each
arm (`arms-*-state-*.txt` beside them), and the server check
([arms-spin-check.txt](recall_latency_realistic/arms-spin-check.txt)).

**Verdict.** Both registered changes lowered the median recall time on the Intel N150, by
more than the registered 10% in both of their pairs:

- **Running the arms beside each other** (this branch): 1,944.71 → 1,643.51 ms with the
  embedding server spinning (×0.845), 1,578.11 → 1,249.12 ms without (×0.792).
- **An embedding server that does not spin** (CEmbedding, `ONNX_ALLOW_SPINNING=0`):
  1,944.71 → 1,578.11 ms on `v2.6.6a1` (×0.811), 1,643.51 → 1,249.12 ms on this branch
  (×0.760).

With both, recall over 100,000 memories of real length (about 56 million cl100k tokens in
all), including embedding the query, took a median of **1.25 s** and at most **2.06 s**
(arm D), against 1.94 s and 2.70 s for `v2.6.6a1` with the server as it was (arm A). The
median is above 1,000 ms, so the third row of the table applies: no sub-second statement.

## Run

The corpus was verified by SHA-256 before the run. The 8 GB virtual machine was shut down
before the first arm, was found shut down at the start and the end of every arm, and was
started again after the last. A 2 GB and a 3 GB container ran throughout. `MemAvailable` was
12.5 to 12.7 GB at the start of each arm.

The embedding server was CEmbedding at `632088d`, `onnx_jina_v5_nano`, default threads,
restarted between the settings. Before arms A and B, one `/embed` call was followed by 3.00,
3.01 and 3.01 CPU-seconds of the server's in the next second (`ONNX_ALLOW_SPINNING=1`);
before arms C and D, by 0.00, 0.02 and 0.00 (`ONNX_ALLOW_SPINNING=0`).

| Arm | CPersona | spinning | median | p95 | max | min |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| A | `v2.6.6a1` | on | 1,944.71 ms | 2,334.77 | 2,696.02 | 1,500.51 |
| B | this branch | on | 1,643.51 ms | 1,984.63 | 2,467.46 | 1,182.60 |
| C | `v2.6.6a1` | off | 1,578.11 ms | 1,979.05 | 2,386.83 | 1,193.48 |
| D | this branch | off | **1,249.12 ms** | 1,583.62 | 2,058.49 | 720.37 |

The figures are `do_recall` with the query embedded by the model, 25 questions per arm.

**Validity.** The load fell below 1.0 after 30, 90, 45 and 45 s; every recall returned 12
rows; no sample errored; width 768; exactly 100,000 memories; the virtual machine was shut
down at the start and the end of each arm. No arm was repeated.

## What else the records show (not registered)

- **Where the spinning cost went.** Each question is recalled twice in each arm, once with a
  stand-in vector and once through the server. The median of that paired difference was
  448 and 454 ms with the server spinning, and 108 and 110 ms without, which is less than
  one embed call measured on its own (below). The stand-in recalls themselves did not move with the setting (1,469 vs
  1,478 ms on `v2.6.6a1`, 1,217 vs 1,159 ms on this branch).
- **The arms change without the server.** On the stand-in recalls this branch took 1,217 and
  1,159 ms against 1,469 and 1,478 ms for `v2.6.6a1`.
- **An embed call on its own was slower without spinning.** The 25 embed-only calls each arm
  makes after its recalls, back to back, took a median of 112.9 and 128.6 ms with spinning
  and 146.3 and 148.1 ms without. A bare ONNX Runtime session on the same machine had shown
  no such cost (53 against 55 ms per run, 30 runs back to back), so the cost appears only
  through the server. A caller that only embeds, without a search after it, pays it.

## Limits

As in the first registration: one machine, one corpus, one configuration, English text,
recall quality not checked here (the responses of the two trees were compared on another
machine and were identical). The trees are a release and an unmerged branch, and the
embedding server is an unreleased commit.
