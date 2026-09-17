# Results: the v1.1 reader study and its re-measurement

Registrations: [first study](prereg-reconstruct-v1_1-reader.md) (`ba78fa2`, amended
`13a532f`), [re-measurement](prereg-reconstruct-v1_1-reader-rerun.md) (`a6c2d15`).
Both were committed before the reader calls they govern.

**Verdict.** The first study found no reduction in what a reader takes in through
`reconstruct`. Its tool log showed why, two changes followed, and the
re-measurement met the registered rule: **median payload ratio 0.579 against
recall (95% interval 0.473 to 0.937), 16 correct answers against 15.**

## Setup, in one paragraph

LongMemEval-S, one stored row per session (median about 9,500 characters), 18
questions (three per type, seed 20260917, every answer session among the rows a
plain recall returns), embeddings from jina-embeddings-v5-text-nano (ONNX,
512-token window), nodes built by the `missing_nodes` repair (784 records, 4,385
nodes). The reader and the judge are `gpt-5.6-luna` at effort `high` through the
Codex CLI; the reader has two tools, `search(query)` and `expand(refs)`, with
every other parameter fixed (count 10, top_k 10, budget 5,000). Payload characters
are summed from the tool layer's log. 42 + 24 reader calls, none failed.

## Arms

| | A: recall | B: reconstruct v1.1 as first built (`a3843b1`) | B2: with the compact envelope and `expand` (`712cb02`) |
| --- | --- | --- | --- |
| Correct answers | 15 / 18 | 14 / 18 | **16 / 18** |
| Payload characters, total | 726,393 | 691,478 | **492,465** |
| Payload characters, median per question | 41,624 | 37,960 | **25,025** |
| Reader input tokens, total | 1,960,794 | 1,990,785 | **1,646,031** |
| of which cached | 1,331,712 | 1,383,680 | 1,200,896 |
| Characters per correct answer | 48,426 | 49,391 | **30,779** |
| Input tokens per correct answer | 130,719 | 142,198 | **102,876** |
| `search` calls | 141 | 126 | 112 |
| Records expanded, of which by range | 30, 0 | 22, 5 | 43, **34** |

Per-correct figures are ratios of sums. Arms A and B are the first study's runs;
B2 ran some hours later the same day and is not interleaved with them.

## The registered rule

Per-question ratio of payload characters, median with a 95% bootstrap interval
over questions (10,000 resamples).

| Comparison | Median | Interval | Correct | Rule |
| --- | --- | --- | --- | --- |
| B / A (first study) | 1.002 | 0.687 to 1.571 | 14 vs 15 | not met: no claim |
| **B2 / A** (re-measurement) | **0.579** | **0.473 to 0.937** | 16 vs 15 | **met** |
| B2 / B (old against new) | 0.680 | 0.570 to 0.945 | 16 vs 14 | reported, decides nothing |

The rule asked for a median at most 0.70, an interval below 1.0, a reduction
larger than the noise floor, and at most two fewer correct answers. The noise
floor — the same arm run twice on six questions — was 24.4% for arm A and 25.7%
for B2 (median absolute paired difference); the median reduction is 42.1%.

Input tokens fall less than characters: the median per-question token ratio B2 / A
is 0.878. Tool schemas and the conversation itself are paid on every turn whatever
the tools return.

## What the first study's log showed

| | recall | reconstruct as first built |
| --- | --- | --- |
| Characters per row / item | 741 | 740 |
| Characters per search response outside the rows | 0 | about 500 |
| Share of all characters that were search responses | 48% | 54% |
| Characters per expansion call | 20,860 | 17,716 |

The rows already cost the same. The difference was an envelope that restated the
request on every call, and a reader searches seven or eight times a question. And
a quote is the first 500 characters of a node about four times that long, so the
answer usually lay past the cut and the reader opened the whole record. The two
changes: state the envelope only when the server did something other than what
was asked, and put on each cut node quote the argument that reads the rest of its
node. In B2 the expansion characters fell from 318,893 to 198,371 and the search
characters from 372,585 to 294,094.

## By question type (correct of 3, payload characters)

| Type | A | B | B2 |
| --- | --- | --- | --- |
| knowledge-update | 3, 183,112 | 2, 31,074 | 3, 65,379 |
| multi-session | 2, 186,058 | 1, 178,796 | 2, 121,261 |
| single-session-assistant | 3, 22,300 | 3, 22,288 | 3, 12,642 |
| single-session-preference | 1, 117,490 | 2, 171,525 | 2, 114,734 |
| single-session-user | 3, 156,654 | 3, 194,942 | 3, 111,181 |
| temporal-reasoning | 3, 60,779 | 3, 92,853 | 3, 67,268 |

Three questions a type decide nothing; the table is here so nobody has to ask.

## Limits

- Eighteen questions, one reader model, one corpus. The per-question ratios run
  from 0.16 to 9.15: a reader chooses its own queries, and one question cost B2
  nine times what it cost A.
- A and B are reused from the first study, so B2 is not interleaved with them.
  Anything that drifted in those hours is confounded with the arm.
- The changes were designed from the first study's log, which is why that study
  cannot score them and a second registration was needed. They were not tuned
  against the second.
- Bundling never acts on this corpus (one row per session): excerpts, roles and
  the evidence bound are untested here.
- Neither `limit` nor `count` ever bound: recall returned 2 to 5 rows per
  question. The quality gate and autocut cut the pool first.
- Eighteen questions detect only a gross loss of correctness. 16 against 15 is
  not evidence that answers improve.
- The number of searches per question is untouched by these changes and remains
  the largest cost. Moving that loop inside the server is what the recall process
  of this line is for.

## Two things the harness taught

- A non-interactive run can return a fluent answer with exit 0 while every tool
  call was refused ("MCP tool call requires approval, but approval policy is
  never"). The first smoke run did exactly that. The registered check — a reader
  run without a logged `search` call is a failed run — caught it.
- Reader cost was about 100,000 input tokens a run, twice the estimate, because a
  reader searches up to 52 times. The first run paused at its registered
  4,000,000 tokens and continued after authorisation; the amendment records it.
