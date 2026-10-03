# Evidence Allocation — design

Status: design, for 2.6.5. Nothing here is implemented yet. The first
pre-release (2.6.5a1) cuts a payload sequence ordered across records (sections
3 and 4); the second (2.6.5a2) adds the coverage step of section 5, so the
difference between the two is the coverage step alone. Both are measured
against a rule registered before any answer call (section 7). `SCHEMA_VERSION`
does not change: everything below reads the blocks and int8 block vectors a
store already has.

## 0. What this step is

`reconstruct` returns items cut to a payload budget by one fixed sequence
([section 7](RELIABLE_RECALL_2_6.md#breadth-before-depth-the-payload-budget)):
every head quote in item order, then each item's excerpts in turn. Since v1.2 a
head quote is the part of its record that matched, sized by the item's place —
800 characters for the first five items and 400 after them. The budget chooses
only how long a prefix of that sequence to return, so raising it never removes
anything (invariant 9).

The sequence decides how a smaller budget is spent, and it spends it on place
in the window, not on evidence. This step orders the quoted passages by how
likely they are to carry the answer, across every record in the window, and
cuts that order instead.

## 1. What was measured first

Four instruments on OmniMemEval's LongMemEval-S (500 questions, v1.2 at the
same search) fixed what the change has to do:

| Measured | Result |
| --- | --- |
| Removing items from the end ([count curve](https://github.com/Cloto-dev/CPersona/blob/master/benchmarks/measurements/results-omnimemeval-lme-count-curve.md)) | Four items: 77.00% at 994.6 Context Tokens, against 81.60% at 1,786.7 with every item. Multi-session questions lose the most |
| Shortening every quote ([quote curve](https://github.com/Cloto-dev/CPersona/blob/master/benchmarks/measurements/results-omnimemeval-lme-quote-curve.md)) | At the same cost, worse than removing items at both points measured: 7.40 points below four items, 4.20 below six |
| What a context quotes ([evidence metrics](https://github.com/Cloto-dev/CPersona/blob/master/benchmarks/measurements/results-omnimemeval-lme-evidence.md)) | With every item, 96.2% of answer sessions are quoted, but only 21.9% of the quoted text comes from them. The first item carries an answer session on 426 of 500 questions |
| Where a shorter quote loses the evidence | The evidence turn left outside the quote, though in a quoted record, rises from 7.5% to 23.3% of evidence turns at 320/160 characters; none is lost by cutting a passage that was too long |
| Where the evidence ranks inside its record | Of 859 evidence turns in quoted answer records, the record's best-ranked passage reaches 62.9%; the second 16.3%, the third 6.3%, the fourth or fifth 6.4%, a later one 8.1%. Recomputed with the server's ranking, which reproduced all 5,888 quoted ranges |

The last two rows are the ones this design rests on. The turn that answers is
often not the best-ranked passage of its record: a long quote reaches it by filling the
second and third passages, and a short one does not. Removing items, the other
uniform cut, loses the second session a multi-session question needs. Both
cuts are blind to the evidence.

## 2. Candidate passages

A candidate passage is a block of a record in the window, extended to the range
that governs it (`blocks.context_range`: the sentence it sits in and a
neighbour that qualifies it). Every record of every item contributes its
blocks; a record with no current block set contributes the passages its quote
is divided into at read time, as today. Overlapping governing ranges of one
record are merged into one passage before ranking, so no text is offered twice.

## 3. One order across records (2.6.5a1)

Today `rank_blocks` orders the blocks of one record. Here every candidate
passage of every record is placed in one order, by reciprocal-rank fusion of
three ranks:

- the record's rank in the recall that produced the window;
- the passage's rank inside its record (the current `rank_blocks` order);
- the passage's rank among all candidates by the cosine of its stored int8
  vector to the query vector.

Ties are broken by the record's rank, then by the passage's position in its
text, so the order is total and deterministic. No score is reported; the trace
records each passage's three ranks.

Inside one record, this order differs from `rank_blocks` only by the extra
weight it gives the vector: the record's rank is the same for all of its
passages. Its use is across records, so it is measured as the payload sequence
of section 4, not inside today's sequence.

## 4. The payload sequence (2.6.5a1)

The payload sequence becomes the candidate passages in the order of section 3,
independent of the budget. The budget then cuts this sequence exactly as it
cuts today's: the response is the longest prefix that fits. Invariant 9 keeps
its form — the budget only
chooses the prefix, so raising it never removes a passage — but its first
clause changes: **the sequence no longer puts every head before any excerpt**.
A passage of the first record may now come before the only passage of the
tenth, so a small budget can return fewer items with more of each. Section 1 is
why: at the same cost, keeping every item thin lost more than keeping fewer
items whole.

What a response looks like does not change. Items are still one per record,
in recall order; an item's `content` is the passages of its head record that
the prefix took, shown in text order and joined as a head quote is today
(`ranges` names them), and `excerpts` keep quoting the item's other claims; a
record with no passage inside the budget returns no item, and the omission is
reported as today. `count` keeps
its meaning, a ceiling on items. The trace records the sequence, and for each
passage its ranks (and, from 2.6.5a2, the parts it covered).

## 5. Coverage (2.6.5a2)

The sequence of section 4 is rebuilt by a greedy pass, still independent of the
budget:

1. Start from the order of section 3.
2. Take the first passage. Mark the parts of the question it covers — the
   coverage ledger's parts: declared entities and rare words, as recorded in
   `trace.coverage`.
3. Re-rank the rest: a passage covering a part not yet covered moves up, one
   covering only parts already covered moves down, by fixed steps. Take the
   first. Repeat until every candidate is placed.

This is where a multi-session question's second session rises: its passage
covers a part the first session's did not.

## 6. What does not change

No model is called; content is a quotation of stored text; the result is
deterministic; every passage carries its record's reference. The schema, the
recall that produces the window and the meaning of `count` are unchanged.

## 7. How it is judged

- **Primary**: at a budget whose Context Tokens are at most 1,000, the 95%
  bootstrap interval of the paired accuracy difference against four items
  (77.00% at 994.6) has its lower bound above zero — better than today at the
  same cost.
- **Secondary**: against every item (81.60%), the lower bound at or above −2.0
  points, the count curve's rule.
- **Development and test are separated.** Settings are chosen on the private
  real-use pack's development questions and on 100 LongMemEval-S questions
  drawn once by a fixed seed
  ([`v1_5_dev_questions.json`](https://github.com/Cloto-dev/CPersona/blob/master/benchmarks/omnimemeval/v1_5_dev_questions.json));
  the claim is measured once, on the other 400.
- The evidence metrics are reported beside accuracy at every point, and the
  controls (four items, six items, every item, and the two quote-curve points)
  reuse their recorded answers.

## 8. Deferred

- Splitting the question into clauses for coverage: coverage starts with the
  ledger's lexical parts, and a finer split is considered only after the
  coverage step of section 5 has been measured.
- A budget that adapts to the question (section 7 of the recall design defers
  adaptation until a fixed policy has a reproducible baseline and an audit
  contract; this design is that fixed policy).
- Sub-block evidence units and sparse or late-interaction scores from the
  embedding server.
