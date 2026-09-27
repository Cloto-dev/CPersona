# Reader cost across versions: 2.4.41, 2.5.12b4, 2.6.0a3

Registered before any reader call of this study. The commit that adds this file
is the evidence of the order. It reuses the 18 questions, the reader, the judge,
the tool layer and the validity checks of the
[v1.1 reader study](prereg-reconstruct-v1_1-reader.md), which was registered and
started first; nothing here was chosen after seeing that study's results except
the expectation of cost stated at the end.

## Question

What does it cost an agent to answer a question from memory on each line, and
how often is the answer right? Each line reads memory differently:

| Version | What `store` keeps of a long record | What a search returns | Reading further |
| --- | --- | --- | --- |
| 2.4.41 | the first 2,000 characters | whole rows | nothing to read: there is no `get_contents` |
| 2.5.12b4 | the first 16,000 characters | the first 500 characters of each row | the whole record |
| 2.6.0a3 (candidate, the commit under test) | the first 16,000 characters | recall items quoted from the matching node | the whole record, a node range or a span |

Reading little is not the goal. A line that stores less returns less, and cannot
answer what it did not keep. The measure is therefore cost **per correct answer**.

This is a description of three lines, not a release gate. Its result decides what
the project may say about reading cost when it compares its own versions.

## Stores

One store per version, written by that version's own `store` from its tag (a
detached worktree), with that version's defaults. Only what a benchmark cannot do
without is set: the embedding endpoint (the same local server and model for all
three: jina-embeddings-v5-text-nano, ONNX, 512-token window), local vector search
with stored vectors, full-text search on, and the task queue off. The 2.6 store
then gets its nodes from the `missing_nodes` repair of `check_health`, run until
nothing is left, as an upgraded store would.

The corpus is the same sessions in the same text form as the first study: one
`store` call per session, its session id as the message id, its date as the
timestamp. The write bound is left at each version's default on purpose. How many
sessions exceed it is recorded per version, and for each question whether the
turns marked as holding the answer survive in the stored text.

## Arms

Same tool layer as the first study; the reader supplies only a query and refs.

| Arm | Code | `search(query)` | `expand(refs)` |
| --- | --- | --- | --- |
| V24 | tag v2.4.41 | `recall`, limit 10 | not offered |
| V25 | tag v2.5.12b4 | `recall`, limit 10, preview tier | `get_contents`, whole records |
| V26 | the 2.6.0a3 candidate commit | `reconstruct`, count 10, top_k 10, budget 5,000 | `get_contents`, whole records, node ranges or spans |

The tool layer refuses to start if the `cpersona` package it imported is not the
one under the named worktree. V24's `search` description omits the sentence about
`expand`, because there is none; nothing else differs.

Retrieval differs between versions as well as the read path, so a difference in
cost mixes the two. That mixture is the honest end-to-end figure for a version.
To let a reader separate them, a no-model table is reported per version and
question: the characters the first search of the question text returns, whether
every answer session is among the rows returned, and whether the answer turns
survive in the stored text.

## Run

Eighteen questions by three arms: 54 reader calls and 54 judge calls, order
shuffled with seed 20260918, one call at a time, no wrapper retries, 300-second
timeout, a reader run without a logged `search` call is a failed run and stops the
study. It pauses before a new call once reported input tokens pass 9,000,000 (registered as
6,000,000 and raised before the first reader call of this study, once the first
study had shown a reader run costs about 100,000 input tokens on average, not 50,000).

## Measures and what may be claimed

Per arm: correct answers out of 18; payload characters (every tool result the
reader received, from the tool layer's log); reader input, cached and output
tokens; `search` and `expand` calls.

Cost per correct answer is a ratio of sums over the arm's questions — total
payload characters divided by correct answers, and the same with input tokens —
never a mean of per-question ratios, which an unanswered question cannot enter.

A statement that one version costs less per correct answer than another is made
only when the 95% bootstrap interval of the ratio of the two costs (10,000
resamples over questions, the same resample applied to both arms, seed 20260918)
excludes 1. Otherwise the figures are reported without a ranking. The noise floor
of the first study (arm A run twice on six questions) is reported beside them: a
reader chooses its own queries, and the first study's smoke runs already showed
one question costing 16,307 characters in one arm and 123,411 in the other.

Eighteen questions rank large differences only. Per-type figures are reported
and decide nothing.
