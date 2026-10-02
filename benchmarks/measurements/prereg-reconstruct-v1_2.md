# Reconstruction v1.2: pack replay and reader study

Registered before the pack's test questions were run on either arm and before
any reader call. The commit that adds this file is the evidence of the order.

## What changed

Reconstruction v1.2 (`4c01ccc`, on 2.6.3a1 at `70b03ae`) changes what
`reconstruct` quotes and what its default response repeats. It does not change
which records are retrieved or how items are formed.

1. **Block ranking.** A head quote's blocks are ranked by the cosine between the
   query vector and each block's stored int8 vector, when every block of the
   record has one, instead of by the Hamming distance of their sign bits.
2. **Joined passages.** Ranges that touch are quoted as one passage. A separator
   stands only where text lies between two ranges.
3. **Place sizing.** The first five items are quoted up to 800 characters and the
   rest up to 400. The default budget is the sum of those sizes, so at the
   default count of 10 it falls from 8,000 to 6,000 characters.
4. **Defaults left out.** `quote_basis`, `content_truncated`, a singleton's
   `independence_reason` and a seed claim's `why` restate a default and are left
   out of the default response; `trace=True` keeps them.

Changes 1 and 2 are for precision: the reader should see more of the evidence in
the first response. Changes 3 and 4 are for efficiency: that response should be
smaller. Two instruments answer different parts of this. A pack replay (no model)
measures the first response itself. A reader study measures what a model reads to
answer a question, end to end.

## Design data, and what is held out

The change was designed on the 150 development questions of a private pack of
real agent memories (4,478 records, 300 questions, 25 of each of six types in
each split; five types give evidence records and quotes, the sixth gives none).
The pack's 150 test questions were not looked at during the design and have not
been run on either commit.

With both arms pinned at the commits above, the development questions replay the
design runs exactly (all 150 responses identical, per arm) and give:

| Development questions | 2.6.3a1 | v1.2 |
| --- | --- | --- |
| Evidence reached (of 193) | 157 | 157 |
| Evidence quotes shown (of 193) | 127 | 136 (10 questions more, 2 fewer) |
| Response characters, total | 1,834,485 | 1,216,737 |
| Response characters, median ratio to 2.6.3a1 | | 0.659 (95% interval 0.653 to 0.667) |

Two more arms on the same questions: 2.6.3a1 with every quote cut to 600
characters (the same head budget as v1.2) shows 117 quotes at a median ratio of
0.813, and v1.2 with every quote up to 800 (change 3 off) shows 139 at 0.877.

## Instrument 1: pack replay (no model)

Each of the 150 test questions once through each arm's MCP `reconstruct` entry
with the arguments an agent sends when it names nothing (count 10, no budget),
fusion `rrf` with confidence off (the server default), block reach on. Every arm
reads its own copy of one prepared store (written by 2.6.0b2's store path with
block reach on: 5,270 nodes, 70,130 blocks, every block with an int8 vector), with
the calibration restored as a server restores it at startup, the clock frozen,
and recall counters reset before each question. Embeddings are bge-m3 (512-token
window), cached by text so that both arms receive the same vector for the same
text. Per question:

- **evidence reached**: evidence records among the records the items cite;
- **evidence shown**: evidence quotes that appear, whitespace-normalised, in the
  text the items carry (head quotes and excerpts);
- **response characters**: the response as JSON.

A is 2.6.3a1, B is v1.2. The rule:

- **Validity.** v1.2 changes no retrieval, so evidence reached must be the same
  on every question. If it differs on any question, no precision claim is made
  and the difference is investigated before release.
- **Evidence shown.** Let d be the per-question difference B − A in evidence
  shown. Claim that v1.2 shows more of the evidence when Σd > 0 and the 95%
  bootstrap interval of Σd (10,000 resamples over questions, seed 20261002) lies
  above 0. Otherwise, when Σd ≥ 0, state only that no quote was lost on this pack.
  When Σd < 0, the quoting changes are not released as they are; investigate.
- **Response size.** Let r = B / A per question in response characters. Claim a
  smaller first response when the 95% bootstrap interval of the median of r lies
  below 0.90. The release notes then state the median and its interval.

Reported, deciding nothing: 2.6.3a1 with every quote cut to 600 characters
(`CPERSONA_RECONSTRUCT_QUOTE_CHARS=600`, which also makes its default budget
6,000), which asks whether v1.2 shows more evidence than spending the same head
budget evenly; v1.2 with `CPERSONA_RECONSTRUCT_TAIL_QUOTE_CHARS=800`, which
separates changes 1, 2 and 4 from change 3; results per question type.

Not covered: `rsf` with confidence on. The pack has no prepared store for that
configuration with block reach, and none is built for this study.

## Instrument 2: reader study

### Corpus

As in the [v1.1 reader study](prereg-reconstruct-v1_1-reader.md): LongMemEval-S
(cleaned), one stored row per haystack session as plain text (a date line, then
one paragraph per turn prefixed by its role), each question's haystack under its
own agent id, a session repeated inside one haystack stored once, rows inserted
directly because sessions exceed the write bound. Two differences:

- Embeddings are bge-m3 (512-token window) instead of jina-embeddings-v5-text-nano.
  Change 1 ranks blocks by their stored vectors, and the pack replay and the
  deployment this ships to first use bge-m3.
- Nodes **and blocks** are built, by the `missing_nodes` and `missing_blocks`
  repairs of `check_health` run until each reports nothing left, which is the path
  an existing store takes on upgrade.

Done, with no model involved: 2,286 rows for the 48 candidates below; 13,343
nodes for 2,142 records; 322,692 blocks for all 2,286 records, every one with an
int8 vector. Each repair needed at most three runs per question, and the last run
of each found nothing left.

The store is built once with arm A's code and frozen. Every reader run reads its
own copy of it, so nothing one run writes reaches another.

### Questions

Abstention questions and the 18 questions of the earlier reader studies are
removed. With seed 20261002, each type's remaining questions are sorted by id,
shuffled, and the first eight taken: 48 candidates. A candidate is **eligible**
when every answer session is among the records arm A's response to the question
text cites. The first six eligible candidates of each type, in shuffled order, are
the study's 36 questions. A type with fewer than six draws the next eight of the
same order, once; if it is still short the study runs with fewer and says so.

This has been done, with no model involved: 46 of the 48 candidates were
eligible (all eight of every type but temporal-reasoning, which had six), so no
type drew further. A response held 9 to 12 items.

### Arms

| | Code | `search(query)` | `expand(refs)` |
| --- | --- | --- | --- |
| A | 2.6.3a1, `70b03ae` | the MCP `reconstruct` entry with the agent id and the query | the MCP `get_contents` entry with the refs |
| B | v1.2, `4c01ccc` | the same | the same |

Every other argument is the arm's server default: count 10, top_k 20, and the
default budget, 8,000 characters in A and 6,000 in B. Each arm runs its own
checkout's interpreter. Tool names and descriptions are identical across arms.
`expand` keeps the guidance of the v1.1 re-measurement (smallest part first, the
whole record last), restated for the fields items carry today: quotes are filled
from character ranges, so an item names its `ranges` and `content_len`, and only a
cut best passage carries `expand`. The node fields that description named are no
longer on items (none of the 48 replayed responses had one).

**Equivalence before any model call.** For every selected question, arm B's
response to the question text must cite the same records as arm A's; otherwise
the study stops. It did not: all 36 (and all 48 candidates) cite the same
records in the same number of items. On the 36 question texts, arm B's first
response is the smaller one: 295,703 characters against 445,180 (0.664; from
0.610 to 0.702 per question). Whether a reader takes in less depends also on what
it reads next: smaller quotes may send it to `expand` more often. The rule below
is unchanged by knowing this.

### Reader and judge

Unchanged from the v1.1 study: `gpt-5.6-luna` at effort `high` through the local
Codex CLI (0.159.2) with subscription authentication, no API-key fallback, no
shell, web, skills or project instructions; the reader's only tools are the two
above. The reader and judge prompts and output schemas are the v1.1 study's,
word for word. The judge sees the question, the reference answer and the reader's
answer, never the arm.

### Order and stopping

Seed 20261002. Each question is one unit of two reader calls, A and B back to
back in a coin-flipped order, so whatever drifts during the run falls on both
arms alike. The noise repeats (below) are units of one call. All units are
shuffled. One call at a time, no wrapper retries, a 300-second timeout per call.
A reader run with no logged `search` call is a failed run, not an answer, and the
study stops at the first one. It pauses before a new call once reported input
tokens pass 14,000,000 (84 reader and 84 judge calls; the v1.1 reader calls
averaged 100,000 to 130,000 input tokens).

**Smoke.** The run up to and including the first unit with both arms. Both arms
must show logged `search` calls, and the wiring must show: arm A's items carry
`quote_basis` and arm B's do not. Swapped or identical arms fail this check. The
smoke runs stay in the cohort.

### Measures

Primary: **payload characters** per question — the summed length of every tool
result the reader received, read from the tool layer's log.

Secondary: correctness; `search` and `expand` call counts; whole-record against
ranged expansions; reader input / cached / output tokens; payload characters and
input tokens per correct answer; wall time (descriptive only: the embedding
server is local and shared).

**Noise floor.** For each type, the first selected question runs arm A a second
time and the second selected question runs arm B a second time: twelve repeats.
For each arm, the floor is the median over its six pairs of |second − first| /
first in payload characters; the study's floor is the larger of the two.

### Decision rule

Let r be the per-question ratio B / A of payload characters, over the questions
both arms completed.

- **Claim a reduction** when the median of r is at most 0.85, the 95% bootstrap
  interval of the median (10,000 resamples over questions, seed 20261002) lies
  below 1.0, the median reduction (1 − median r) exceeds the noise floor, and arm
  B has at most two fewer correct answers than arm A. The release notes then
  state the measured median and interval.
- **Do not release** when arm B has four or more fewer correct answers than arm
  A. Investigate first.
- **Otherwise** make no claim about reading cost from this study.

The do-not-release threshold is four of 36 where the v1.1 study used three of 18:
the paired difference in correct answers spreads with the square root of the
number of questions. Thirty-six questions still detect only a large loss of
correctness; the correctness conditions guard against gross harm and are not
evidence that answers are as good or better, and the results will say so.
Per-type results are reported and decide nothing.

### Validity checks

Before the smoke:

- The payload counter is checked against a tool result of known length, through
  this study's tool layer.
- The judge is checked on the reference answer (must be correct) and on a
  deliberately wrong answer (must be incorrect) for two questions.

After the smoke and before the rest of the run, on a copy of a smoke run that
searched: removing its tool log must make the zero-search check fail it, and
restoring the log must make it pass.

## What each result decides

The 2.6.4 pre-release notes state a claim only where its rule was met, with the
measured figures. A rule not met is reported with its numbers and nothing is
claimed for it. "Do not release" in either instrument holds the change back until
it is understood.

## Amendments

Made without reading any arm's results into the rule.

- **Line splitting.** The first smoke run (arm A) answered after two logged
  searches, and the harness reported it failed with a JSON parse error. Its line
  splitter, carried over from the v1.1 study, used Python's `str.splitlines()`,
  which also splits at U+2028; the Codex event line that carried a quoted session
  held two of them unescaped, as JSON allows. The harness now splits JSON Lines at
  newlines only, for the event stream and the tool log alike. The run was re-read
  from its unchanged artifacts, not re-run, and its first reading is kept beside
  it. Its answer had not been judged.
