# Reconstruction v1.1 reader study

Registered before any model call. The commit that adds this file is the evidence
of the order.

## Question

Reconstruction v1.1 exists to reduce what an agent reads per recall: a long
record is quoted from the node that matches the query, `get_contents` expands a
node range or a span instead of a whole record, and items carry less metadata.
Does an agent that reads memory through `reconstruct` take in less text than one
that reads through `recall`, without answering worse?

This is an instrument for one release decision (what the 2.6.0a3 notes may claim).
It does not measure retrieval quality: both arms see the same candidate rows.

## Corpus

LongMemEval-S (cleaned, 500 questions), one stored row per haystack session, as
plain text: a date line, then one paragraph per turn prefixed by its role. Each
question's haystack is stored under its own agent id, so a search sees only that
question's history. The dataset repeats a session inside one haystack 13 times:
the same turns under a different date, never an answer session. A store holds one
row per message id, so the first occurrence is stored and the repeat is dropped
(a repeat with different turns would stop the build). Rows are inserted directly because sessions exceed the
16,000-character write bound; nothing else about the store is changed. Embeddings
are computed live by the recommended default model (jina-embeddings-v5-text-nano,
ONNX, 512-token window), for rows, nodes and the reader's own queries alike.

Overflow-tree nodes are built by the `missing_nodes` repair of `check_health`,
run until it reports nothing left, which is the path an existing store takes when
it upgrades. The number of runs and records repaired is recorded.

What this corpus does not exercise: bundling. One row per session leaves every
item with one claim, so excerpts, roles and the evidence bound never act. Bundling
on this dataset would be decided by turn timestamps the dataset does not have and
the study would have to invent, so no release claim is made about it here. A
no-model replay with invented spacings (5 / 20 / 60 seconds per turn) is reported
separately, with the spacing sensitivity shown, as a description and nothing more.

## Questions

Abstention questions are excluded (they have no answer session). With seed
20260917, the questions of each of the six types are sorted by id, shuffled, and
the first five taken: 30 candidates. A candidate is eligible when every answer
session is among the 10 rows a plain `recall` of the question text returns. The
first three eligible candidates of each type, in shuffled order, are the study's
18 questions. A type with fewer than three eligible candidates draws the next
five of the same shuffled order, once; if it is still short the study runs with
fewer and says so. Eligibility is decided before any model call and is the same
for both arms, because both arms see the same rows.

This has been done, with no model involved: 20 of the first 30 were eligible;
knowledge-update had one (its other four had one of two answer sessions in the
rows returned), drew five more, and reached three. The plain recall returned 2 to
5 rows per question, never 10: the quality gate and autocut bound the pool before
the limit does, so neither `limit` nor `count` binds in this study and the budget
is never reached. The node repair built 4,385 nodes for 784 records in one run per
question (a second run found nothing left).

## Arms

The reader reaches memory through a thin tool layer that fixes every parameter;
it supplies only a query and refs. Tool names and the `search` description are
identical across arms.

| | `search(query)` | `expand(refs)` |
| --- | --- | --- |
| A | `recall`, limit 10, preview tier | `get_contents`, whole records only |
| B | `reconstruct`, count 10, top_k 10, budget 5,000 | `get_contents`, whole records, node ranges or spans |

The budget is 10 preview-tier quotes, so breadth is equal; the default of 4,000
would return at most 8 items at count 10 and confound depth with breadth. Before
any model call, a replay of the 18 question texts through both arms must return
the same ref set per question; otherwise the study stops. It did: 18 of 18 the
same, 65 of 66 items quoted from a node. In that replay arm B's first response is
the larger one, by 8% to 32% per question (58,236 against 50,057 characters in
total): an item still carries more metadata than a recall row. Any reduction the
study finds therefore has to come from what the reader does next -- fewer or
narrower expansions -- and the rule below is unchanged by knowing this.

The tool schemas of the two real tools differ in length. That cost is constant
per session, both tools are loaded in real use, and it is excluded here.

## Reader and judge

Reader and judge: `gpt-5.6-luna`, effort `high`, through the local Codex CLI with
subscription authentication, no API-key fallback, no shell, web, skills or
project instructions; the only tools are the two above. The CLI's requested model
is reported; usage events do not attest model identity. The reader is told to
answer from memory, to search as often as it needs, and to read only as much as
it needs. The judge sees the question, the reference answer and the reader's
answer, never the arm, and returns correct / incorrect.

Order is shuffled with seed 20260917. One call at a time, no wrapper retries, a
300-second timeout per call. A reader run with zero logged tool calls is a
failed run, not an answer: a non-interactive harness can return a fluent answer
while every tool call died. The study stops at the first failed run. It pauses
before a new call once reported input tokens pass 4,000,000.

**Smoke.** One question per arm runs first and stays in the cohort. It must show
logged `search` calls, and, in arm B, a response carrying `node`.

## Measures

Primary: **payload characters** per question — the summed length of every tool
result the reader received, read from the tool layer's log. It is deterministic
given the calls, and independent of any tokenizer.

Secondary: correctness; `search` and `expand` call counts; whole-record versus
ranged expansions; reader input / cached / output tokens; tokens per correct
answer; wall time (descriptive only: the embedding server is local and shared).

**Noise floor.** Six questions (one per type, the first selected of each) run arm
A twice. The paired difference between the two runs is the floor: a reader
chooses its own queries, so identical conditions do not give identical payloads.

## Decision rule

Let r be the per-question ratio B / A of payload characters, over the questions
both arms completed.

- **Claim a reduction** when the median of r is at most 0.70, the 95% bootstrap
  interval of the median (10,000 resamples over questions, seed 20260917) lies
  below 1.0, the median reduction exceeds the noise floor's median absolute
  paired difference (as a fraction of arm A), and arm B has at most two fewer
  correct answers than arm A. The release notes then state the measured median
  and interval.
- **Do not release** when arm B has three or more fewer correct answers than arm
  A. Investigate before 2.6.0a3.
- **Otherwise** release as experimental with no claim about reading cost.

Eighteen questions detect only a large loss of correctness; the correctness
condition is a guard against gross harm, not evidence of equivalence, and the
results will say so. Per-type results are reported but decide nothing.

## Validity checks before the run

- The payload counter is checked against a tool result of known length.
- The judge is checked on the reference answer (must be correct) and on a
  deliberately wrong answer (must be incorrect) for two questions.
- Removing the tool layer's log line must make the zero-tool-call check fail a
  run that did call tools (the check reads the log, so it must notice its absence).
