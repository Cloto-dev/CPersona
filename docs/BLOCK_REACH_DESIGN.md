# Block Reach — design

**Status:** design, not shipped behaviour. Nothing described here is in a
release yet. Numbers marked as estimates are estimates, and the section that
says how the step is judged says which ones must be replaced by measurement
before the work is called done.

## 0. The defect, and what this step does about it

A record longer than the embedding window is stored whole and embedded once.
The single vector is computed from the text the window admits, so everything
past that point contributes nothing to what the vector says. The overflow tree
already divides such a record into nodes and embeds each one, but those nodes
are read only when a recall item quotes a passage. No retrieval path looks at
them. The consequence is exact: **the tail of a long record cannot be reached
by semantic search at all**, however close it is to the query.

On this project's deployment, 2,419 of 5,341 records (45.3%) run past the
window, and 2,074,065 of 4,242,156 characters (48.9%) sit outside it.

That is a volume, not a case for building anything, and it was measured before
the relevant question was. The relevant question is what the unreachable text
costs, and an earlier measurement answers it: when a query aimed at a tail uses
the vocabulary of the document it is aiming at, the lexical and fused arms
already return that document at hit@10 of 100%, so reaching further is worth
nothing. The gain appears only for paraphrases that share no vocabulary with
the target, where reach moved hit@10 from 9.6% to 34.3%. In the same
measurement, unrelated short records were dragged into the result set at a cost
of 3.6 to 4.6 points, in every configuration, with no case improving. The net
is therefore `P × gain − (1 − P) × loss`, where `P` is the share of real
queries that aim at a tail in paraphrase. **`P` has not been measured.**

This step does not claim the net is positive. It builds the capability and the
instrument to find out: text that is structurally out of reach becomes
reachable, and the failures of that reach become attributable. Precision is a
separate step with its own entry conditions, and this page does not speak for
it.

## 1. What a block is

A block is a short clause-sized span of a record's own text, identified by
offsets into that text. It carries no copy of the content. It is a derived
object: deleting every block changes no answer that the server gives with the
feature disabled, and a lost or corrupt block set is rebuilt from the parent
rather than repaired.

Three ranges are kept apart, because conflating them is how a quotation ends up
severed from the condition that governs it:

| Range | Basis | Used for |
|---|---|---|
| Block span | a short clause in the source text | identity, and attribution of a hit |
| Representation input | the fixed text handed to the model | producing the vector |
| Quoted context | a bounded contiguous range containing the block | what a reader is shown |

In this step the representation input is the block's own text. The quoted
context is what the overflow tree already provides, and widening it does not
require a vector to be recomputed.

## 2. How a record is divided into blocks

Division is deterministic and uses no model. It detects structural boundaries —
paragraph, sentence terminator, list item, table row, code fence, bracket,
quotation — and may split only at a boundary that does not separate a predicate
from its subject, its object, its condition or its negation.

A comma alone is never a reason to split. When the policy cannot tell, it keeps
the larger unit: an undivided sentence is a correct block, and a sentence cut
before its negation is not. Only an input that exceeds the window is split by
force, and a forced boundary is recorded as such.

The resulting spans cover the parent's text with no gap and no overlap. Block
boundaries are subordinate to node boundaries: a block does not cross a node,
and a clause that a node boundary severs is served by the quoted context rather
than by a schema that lets one block span two nodes.

The fixtures for this policy come from the deployment's own corpus, not from
invented sentences — negation, conditionals, corrections, omitted subjects,
mixed scripts, code fences, long URLs and clauses that straddle a node
boundary all occur there in quantity.

## 3. The representation is one bit per dimension

Each block's vector is quantised to the sign of each dimension and stored as a
bit string. At 1,024 dimensions that is 128 bytes per block against 4,096 bytes
for float32.

The sign alone preserves angle well enough to rank candidates: for two vectors
the probability that a random hyperplane separates them is `θ/π`, so Hamming
distance stands in for cosine. It stands in approximately, and this step treats
that approximation as the whole of what a block contributes — see section 5.

**Blocks do not store float32 vectors at all.** The reason is scale rather than
taste. The division of section 2 was run over the deployment's own corpus, by
then 5,384 records: **105,975 blocks**, a mean of 19.7 per record and a median
of 13. At that multiplier a corpus of one million records is 19.7 million
blocks — 2.5 GB as bit strings, 81 GB as float32. The second number is not a
resident-memory figure that a smaller machine escapes; it is what the database
would have to hold, and it would break the documented practice of copying a
corpus with a single file-level backup.

The blocks the policy produces are short, which is what it is for: a median of
46 characters, 133 at the ninetieth percentile, 289 at the ninety-ninth. Only
75 of the 105,975 run past 739 characters, where the embedding window can begin
to close inside a single block, and 12 past 1,787, where it certainly does. A
forced boundary is therefore rare rather than routine.

Scanning cost follows the same arithmetic. A single-threaded `bitwise_count`
over one million 768-bit rows takes 79.8 ms on the reference machine, which
puts a pure-array implementation's ceiling near 1.9 million blocks. At the
measured multiplier that is about **97,000 records** — eighteen times the
current deployment, whose entire block index is 105,975 rows and scans in
roughly 8 ms. Note that the block index reaches that ceiling 19.7 times sooner
than a record-level index does, because it holds 19.7 times the rows. Below the
ceiling this step needs no new dependency and no second language. Above it, the
coarse pass belongs to the separate line that owns the index architecture, and
the structure here is chosen so that the same scan serves both.

These counts are the segmentation policy of section 2 applied to the corpus as
it stands. A change to the policy moves them, and the measurement is cheap
enough to repeat.

## 4. Retrieval: Hamming candidates, collapsed to their parent

The block arm runs beside the existing record vector, lexical and keyword arms.
It ranks blocks by Hamming distance to the quantised query, takes a bounded
number of them, and then does something before anything else sees the result:

**Block hits collapse to their parent before they are candidates.** A record
that several of its blocks match is one candidate, represented by its best
block, not several. Scores from separate blocks of one record are never added:
they are correlated observations of one source, and summing them would convert
the length of a record into evidence.

The number of blocks examined is capped, and the cap is applied so that it
cannot be consumed by a single long record — a per-parent best is retained
within the examined set rather than a global top-k being cut and then
deduplicated, because the latter lets one long record occupy the pool.

None of these bounds is derived from the number of items the caller asked for.
Changing the response count alone must leave the set of examined blocks, and
the set of candidate ids, unchanged.

## 5. Admission: a reservation, not a gate change

A block hit exists to bring a record into the pool that would not otherwise be
there — its parent's whole-text vector is, by construction, a poor match, which
is the entire reason the tail was unreachable. So the question of how such a
candidate is admitted cannot be answered by the existing quality gate: the gate
scores the parent, and the parent is exactly what scores badly.

Two obvious answers are both wrong. Letting the parent be judged on its own
vector drops the record the block just found, which defeats the step. Putting
the block's own similarity into the gate is worse in a subtler way: a short
span produces a higher similarity against a specific query than a long one
does, so the same threshold means something different for a block than it does
for a record, and the calibrated operating point silently governs a different
population. A gate that compares a relative quantity against an absolute
threshold is a defect this project has already recorded once.

This step therefore admits block-found candidates by **reservation**: a fixed,
small number of places in the result set are held for them, filled in Hamming
order, and the quality gate is not consulted for those places and is not
altered for any other. The consequences are stated plainly rather than argued
away:

- The reservation is an upper bound on how much a bad block hit can cost. It is
  not a claim that block hits are good.
- Reserved candidates displace nothing that the gate admitted. They occupy
  their own places, and when the reservation is not filled the result is
  shorter rather than padded. Turning the feature on therefore adds rows and
  removes none: every answer the previous release gave is still given.
- The reservation size is a configured bound with a conservative default, not a
  tuned parameter. Tuning it requires a reader-based measurement, which belongs
  to the precision step.

The quantity the gate is calibrated on does not move, because no new score
reaches it. That is the property this arrangement exists to preserve.

## 6. Schema and lifecycle

Blocks are held in their own table, shaped like the overflow tree's node table:
parent kind and id, block index, start and end offsets, token count, the window
the count was taken under, the bit string, and the model identity the bits were
produced by. The parent's text is not duplicated.

The row also carries the isolation axes, copied from its parent, which the node
table has no need of. Blocks are read by a retrieval path where nodes are not,
and a coarse pass that ranked the whole corpus and filtered afterwards would
spend its cut on rows the authority then drops — where a bucket is one per cent
of the corpus, a post-filter leaves almost nothing. The copies are not a second
authority: the isolation predicate has exactly one source, and the hydrate
re-applies it fail-closed. The obligation here is one-directional — the rows
this table offers must be a superset of the rows the authority admits.

A retag therefore has to reach the blocks, and must not destroy them: the text
is unchanged, so the vectors are still valid and rebuilding them would spend
embedding calls to arrive at identical bits. The axis triggers update the
copies; the content triggers delete the set. The asymmetry is deliberate, and a
test holds each half of it.

Construction is asynchronous, on the queue the tree already uses. A block set
is written only if the parent still holds the text it was divided from, and a
partially built set is never treated as current. A crash leaves an unpublished
set unpublished, and the resumed run re-checks the revision it was building
against.

Invalidation reuses the mechanism the tree established: the database triggers
that drop a record's nodes when the record is deleted, or when its content or
summary really changes, drop its blocks in the same statement. The project does
not use foreign keys, so cascade behaviour is written explicitly rather than
assumed.

The model identity stored with each block set is the one that produced it. A
deployment that cannot report which model answered it must not have its blocks
treated as current on the strength of a configured default that happens to
match — an unknown is an unknown, and a separate piece of work exists to make
the embedding service state its identity.

## 7. Opt-in, and the two gates

The feature is opt-in — not "off by default until it looks good", but opt-in
for the whole of this step, with promotion to a default deliberately out of
scope. What would justify a default is a measured net gain, and the quantity
that decides it is the one section 0 says is unmeasured.

Opt-in is split across two switches, because a single one would still charge a
deployment for the half it is not using:

| Switch | Governs | Off means |
|---|---|---|
| block construction | whether blocks are built and stored at all | no embedding calls, no rows, no queue work |
| block retrieval | whether the block arm runs during recall | the index may exist, and nothing reads it |

Turning construction on starts a bounded backfill of the existing corpus.
Turning retrieval on while construction is off is a configuration error the
server reports at startup, not a silent no-op that returns fewer rows than the
caller has reason to expect.

With both off, the server does what the previous release did and costs what it
cost. The presence of the block table changes no path.

Every bound below is fixed by server policy and is registered before anything
is measured, never derived from the caller's request:

| Bound | Governs |
|---|---|
| examined block cap | how much of the block index one call may scan |
| blocks per parent cap | how much of that cap one record may consume |
| reservation size | how many result places block hits may fill |
| construction queue limits | records, tokens and embedding calls per backfill run |
| maximum block length | when a forced boundary is taken |

## 8. Invariants

1. A block never modifies its parent. Blocks are derived; the record is the
   original.
2. With the feature disabled, responses are byte-identical to the previous
   release, including when block rows exist.
3. Determinism — the same text, node layout, segmentation policy and tokenizer
   produce the same span list, and ties in Hamming order are broken by a
   written-down total order.
4. Coverage — a block set covers its parent's text with no gap and no overlap,
   and no block crosses a node boundary.
5. Count independence — none of the caps, the reservation or the examined set
   may be derived from the response count. Changing the count alone leaves the
   candidate id set unchanged.
6. One record, one candidate — several blocks of one record never become
   several candidates, and their scores are never summed.
7. No new score reaches the quality gate. Block hits are admitted by
   reservation or not at all.
8. A block set is current only if it was built from the parent's current text
   by a known model. An unreported model identity is not a match.
9. Quotation is never severed — a span shown to a reader carries the contiguous
   context that governs it, or is reported as incomplete.

Each invariant is held by a test whose power is demonstrated by mutation: the
mutation that re-couples a cap to the count, the one that sums a record's
blocks, the one that pushes a block's similarity into the gate, and the one
that treats a partially built set as current must each turn a named assertion
red.

## 9. What this step does not claim

It does not claim better answers. The benchmark this project regresses against
judges at record granularity, so a finer quotation cannot register in it at
all, and the reader-based instrument that could judge it is not built. Any
public statement about this work says what was measured — reach — and says that
precision was not.

It does not claim the tail is worth reaching. `P` is unmeasured, and the
earlier measurement showed a standing cost for every query that is not a
paraphrase aimed at a tail. This step makes that trade visible; it does not
settle it.

It does not claim the approximation is harmless. Hamming ranking is
approximate, and a candidate it fails to surface is not recovered later.

It does not widen what a caller can ask for. No tool is added, and no argument
or response shape changes.

## 10. Decided separately

- Whether blocks ever hold float32 vectors, and whether a second exact pass
  re-ranks reserved candidates. Both belong to the precision step, and both are
  reopened by what the reach failures attribute.
- Whether nodes should themselves become retrievable. Blocks are finer than
  nodes and answer the same question, so shipping this makes that a choice
  between two derived layers rather than an independent question.
- Whether the record layer is itself scanned coarsely. This page adds an arm;
  it does not re-implement the existing one. Replacing the exact record scan
  with an approximate one changes answers a caller already receives, which is a
  different kind of change from adding a reserved arm — it needs an upper bound
  on degradation where this page needs none. It belongs to the line that owns
  the index architecture, along with the coarse-scan implementation beyond the
  array ceiling in section 3. The scan built here is shaped so that line can
  use it rather than start again.
- Whether the scan window remains meaningful for an arm that sees the whole
  corpus.

## 11. How the step is judged

**Reach.** A registered slice of queries whose target sits past the first node
of a long record. The claim is that the target enters the candidate pool
through the vector path where it previously could not. The measurement is
registered before it runs — corpus, selection rule, model, configuration,
decision rule and sample size — and the post-change number counts as evidence
only because the same procedure produced the opposite result before the change.

**Non-interference.** Two states are checked on a fixed corpus, not one: both
switches off, and construction on with retrieval off. The second is the one
that can fail quietly, because the rows exist and something might read them.
Both must be byte-identical to the previous release.

**Cost.** The block count and index size in section 3 are measured rather than
estimated, and that section names the policy the counts came from. What remains
is the scan time at that size taken on the reference machine rather than by
arithmetic, and the backfill's cost together with its resumption after an
interrupted run.

**One attribution.** The step is not finished until it can name one way its own
reach fails — a candidate that arrives but ranks too low, a quotation that
arrives severed, or a long record that occupies the pool. That name is the
entry condition for the precision step, which does not begin without it.
