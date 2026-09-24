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

The shapes the fixtures reproduce come from the deployment's own corpus rather
than from invented difficulty — negation after a condition, corrections in a
later sentence, omitted subjects, mixed scripts, code fences, long URLs and
clauses that straddle a node boundary all occur there in quantity. The fixture
text is written out rather than copied, because a test file is published and
the corpus is not. The corpus checks the policy the other way round: the
division is run over every record and the partition asserted, which is where
section 3's counts come from.

## 3. The representation is one bit per dimension

Each block's vector is quantised to the sign of each dimension and stored as a
bit string. At 1,024 dimensions that is 128 bytes per block against 4,096 bytes
for float32.

The motivation for a sign code is the random-hyperplane identity: for two
vectors the probability that a *randomly drawn* hyperplane separates them is
`θ/π`, which makes a Hamming distance an unbiased estimate of an angle. **That
identity does not carry over to this quantiser as stated.** The bits here are
the signs of the model's own coordinates, and the coordinate axes are not a
random draw — the estimator's mean and the independence between bits both have
to be argued from the embedding's distribution, not from the identity. Nothing
here does that.

So the code's fidelity is a heuristic this step does not establish, and the
design leans on that rather than hiding it: the reservation of section 5 exists
precisely because a block hit's ranking quality is unproven, and it bounds what
a bad one can cost. What would settle it is the corpus's own angular geometry —
the angle to the true nearest block, and the distribution of angles to
everything else — measured against full-precision block vectors. That
measurement does not exist, and until it does, the quantities that would fix a
code width are unknown. They grow with the corpus: a wider corpus needs a finer
code to keep the same false-survivor budget at the same retention, so a width
that serves this deployment says nothing about one an order of magnitude larger.

**Blocks do not store float32 vectors.** The reason is scale rather than
taste. The division of section 2 was run over the deployment's own corpus, by
then 5,387 records: **107,428 blocks**, a mean of 19.9 per record and a median
of 13. At that multiplier a corpus of one million records is 19.9 million
blocks — 2.5 GB as bit strings, 82 GB as float32. The second number is not a
resident-memory figure that a smaller machine escapes; it is what the database,
and every file-level backup of it, would have to hold.

What a block does keep beside its bits is one byte per dimension, read only to
re-order the few hundred rows the Hamming pass ranks highest (section 4b). At
the same multiplier that is 20 GB for a million records: a quarter of the
float32 figure and eight times the bit strings, held on disk rather than in
memory. Section 4b gives what it buys and why it is worth that.

The blocks the policy produces are short, which is what it is for: a median of
45 characters, 133 at the ninetieth percentile, 285 at the ninety-ninth, and
none above the 739-character limit at which a boundary is forced. That limit
was reached 42 times in 107,428 blocks — 0.039 per cent — so a forced boundary
is an exception rather than a routine. The same run checked the partition on
every record: 5,387 of 5,387 covered with no gap and no overlap.

Scanning cost follows the same arithmetic. A single-threaded `bitwise_count`
over one million 768-bit rows takes 79.8 ms on the reference machine, which
puts a pure-array implementation's ceiling near 1.9 million blocks. At the
measured multiplier that is about **95,000 records** — eighteen times the
current deployment, whose entire block index is 107,428 rows and scans in
roughly 9 ms. Note that the block index reaches that ceiling 19.9 times sooner
than a record-level index does, because it holds 19.9 times the rows. Below the
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

When the examined cap binds, it truncates in primary key order — the order the
rows are read in, which is arbitrary with respect to the query. The cap defends
against an unbounded scan; it is not a claim that the rows surviving it are the
best ones, and it sits above the whole index of the deployment section 3
measured.

## 4b. Re-rank: the stored vector orders what the Hamming pass ranked highest

The reach judgement of section 11 named where this arm's reach fails. Of the
targets it did not reach, two thirds were among the two hundred records the
Hamming pass ranked highest and still missed the reserved places: the pass
found them and its order put other records in front. The order is the loss, and
order is what a one-bit code is worst at.

So the rows the pass ranks highest are ranked again, by a vector that keeps
more than the sign:

- **A fixed depth of 200 rows**, taken in the total order of section 4 —
  distance, then kind, parent id and block index — before anything is collapsed
  to its parent. The depth is cut on rows because a row is what has a vector to
  read, and it is server policy like the caps of section 4: derived from
  nothing the caller asks for.
- **Each block keeps its vector at one byte per dimension**, scaled so the
  largest component is 127 and rounded. The scale is not kept, because the only
  thing ever computed from the bytes is a cosine, and a cosine does not see it.
  The bytes come from the same embedding call that produced the bits; building
  them costs no request the block did not already make.
- **The rows are ordered by the cosine** between the query vector and the stored
  bytes, then collapsed to their parent exactly as section 4 collapses them: a
  record is represented by its best block, and nothing is summed. Equal cosines
  go by kind, parent id and block index. A record none of whose blocks made the
  depth is not a candidate.
- **All or nothing.** If any of the rows has no vector, the whole query uses the
  Hamming order of section 4 — the order the previous release used. A cosine
  and a Hamming distance cannot be put in one order, so a mixture would compare
  some rows on one scale and the rest on another. A block set that lacks its
  vectors is not current (invariant 8) and the sweep rebuilds it, so the
  fallback is the state of a deployment part-way through that rebuild, not a
  steady state.
- **The response says which order filled the reserved places**: a reserved
  row's `match_reason` carries an `order` key whose value is `vector` or
  `hamming`. The cosine itself
  is not shown, for the reason section 5 shows a distance rather than a score.

**What was measured.** The corpus and query families were those registered for
the reach judgement: 4,567 records divided into 92,807 blocks, paraphrases aimed
past the first node of a long record (195 targets), and a replication family
(189). Through the recall path as built, the re-rank raised the reservation's
reach from 57 to 78 of 195 and from 64 to 81 of 189, and no target the Hamming
order reached was lost; with the arm switched off, every response was
byte-identical to the previous release's. Re-ranking every examined row by its
vector reaches 79 and 81, and the curve is flat from a depth of about fifty; 200
is the smallest depth on a grid fixed before the run that came within one target
of that on both families. One byte per dimension reached the same targets as
float32, to within one. The places the reservation fills are still
mostly taken by records that are not the target: the share of places the
target holds rises from 13% to 18%. This is reach, not precision: whether the reached text answers the
question needs a reader, and the instrument that has one is not built.

**Where the vector comes from.** Four sources were measured against each other
on the same families:

| Source | Reach (of 195 / of 189) | Cost |
|---|---|---|
| one byte per dimension, stored with the block | 78 / 81 | 1,024 bytes per block on disk; 200 reads per recall |
| the block's text, embedded again at query time | 71 / 73 at a depth of 12 | 463 ms at a depth of 12 and 5.6 s at 200, on the reference machine |
| the parent record's own vector | 21 or fewer / — | none, and it cannot see the tail it is asked about |
| the vector of the node containing the block | 60 or fewer / 54 or fewer | none, and it dilutes one clause in a node ten times its length |

Embedding at query time is ruled out twice over. It is slow, and it breaks
determinism: the quantised embedding backend this project deploys returns a
different vector for the same text depending on what else is in the request (a
median cosine of 0.980 between a text embedded alone and in a batch of
sixteen), so the order would depend on which candidates happened to be sent
together.

**Cost.** At the multiplier of section 3, a million records hold 20 GB of these
vectors. A recall reads 200 of them by primary key: 2.9 ms from the page cache
on the reference machine, and 12.5 ms as 200 random page reads that bypass it.
The arm as a whole got cheaper rather than dearer: 167 ms at the median against
193 ms without the re-rank, because the collapse to parents now runs over the
200 re-ranked rows instead of every examined row.
They live in the database, in a table of their own (section 6). In the database
because, unlike the contiguous vector index, they cannot be rebuilt from it —
only by embedding every block again — so a backup that left them out would
restore to a corpus that has to be re-embedded. In a table of their own because
the Hamming pass reads every block row on every recall, and a kilobyte beside
each 128-byte bit string would make it read about eight times the pages to use
none of them.

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
small number of places in the result set are held for them, filled in the
order section 4b produces, and the quality gate is not consulted for those places and is not
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

Two consequences follow for a caller. The reserved places are additional to the
ones the gate filled, so a response can carry up to the reservation more rows
than the count asked for — taking them out of the count instead would be the
displacement the bullet above rules out. reconstruct, which reads this recall at
its own candidate depth, keeps the same shape: the held records follow its
window as items of their own, marked as reserved, and never compete for a place
in it ([reconstruction window](RELIABLE_RECALL_2_6.md#the-reconstruction-window)).
Ranked with everything else they would have come last, because they carry no gate
score, and a full window would never have reached them. And the arm ranks against the query
vector the local vector search embedded, so a deployment whose vector search is
remote does not get block reach: the remote service answers for itself and
never produces the vector this arm quantises.

## 5b. Quotation: a block, and what governs it

A block is a clause. That is small enough to be read, and small enough to say
the opposite of the record it came from — "we adopted A" is not what the record
says when the next sentence withdraws it. So what a reader is shown is never
the block alone: it is the contiguous range of the parent's text that governs
the block, decided by two rules that are conservative about what they can see.

1. **Finish the sentence.** A block that does not begin one is extended
   backwards, and a block that does not end one is extended forwards. The
   divider cuts at structure and sometimes inside a sentence, so a block can be
   a clause, and a clause read without its sentence is the first way a
   quotation goes wrong.
2. **Follow the qualifier.** A block that begins with a word qualifying what
   came before is extended backwards, and a block followed by such a word is
   extended forwards. This is the case where both sentences are complete and
   the second reverses the first.

Both rules work in whole blocks and repeat until neither fires. The context
they produce is bounded by a fixed limit, and a rule still reaching when the
limit stops it makes the quotation **incomplete**: it is reported as such and
carries the range to read instead, rather than being presented as whole
evidence. The same is true of a quotation the payload budget cuts short.

The limit is server policy and not a budget. The context is decided before
anything is cut, so raising the payload budget adds items and excerpts and
never replaces a quotation with a different one — the property the response is
a prefix of a fixed sequence depends on it.

What these rules do not cover is an unmarked dependency: a correction in the
next sentence that announces itself only by its content. The rules see marked
dependencies and sentence boundaries, and a control fixture holds them to it —
two independent sentences must be quoted apart, or a rule that always took the
neighbour would satisfy every severance test by quoting the whole record.

A range handed back this way carries the **revision** of the text its offsets
were measured in. A block range validates itself, because the triggers drop a
record's blocks when its text changes; a raw span has nothing equivalent, and
serving old offsets against new text would quote something the record never
said. A record rewritten since is refused, not served.

## 6. Schema and lifecycle

Blocks are held in their own table, shaped like the overflow tree's node table:
parent kind and id, block index, start and end offsets, a flag for an end the
length limit forced, the bit string, and the model identity the bits were
produced by. The parent's text is not duplicated.

The re-rank vectors of section 4b sit in a second table keyed by the same three
columns, holding nothing else. They are written in the same transaction as
their blocks, and a trigger on the block table deletes a block's vector with
the block — so every path that already drops a record's blocks, a delete, a
rewrite or an agent's erasure, drops the vectors without being told about them.
The block table's axis triggers do not reach this one, because it is never
filtered: it is read only by primary key, for rows the filtered pass already
chose.

There is no token count and no window, though the node table has both. The
divider is offline by design, so it has no honest value for either, and a column
that could only be filled by breaking that property is an invitation to break
it. A block's length is its end minus its start.

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

Turning construction on starts a bounded backfill of the existing corpus. One
sweep exists at a time, and it queues its own continuation when a bound stops
it, so the corpus is built in bounded runs across restarts rather than in one
run that holds the queue for as long as the corpus takes. The cursor a
continuation carries says where to resume and nothing about what the records
there said: a set is written only if its parent still holds the text it was
divided from, so a resumed run re-checks the revision it is building against. A
run that reaches a bound before it has built anything still starts the record in
front of it — otherwise a record larger than a single bound would be the place
every run stopped, and the sweep would never pass it.

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
| re-rank depth | how many of the examined rows are re-ranked by their stored vector |
| reservation size | how many result places block hits may fill |
| construction queue limits | records, characters, embedding calls and elapsed time per backfill run |
| maximum block length | when a forced boundary is taken |

The volume bound counts characters rather than tokens, for the reason the table
in section 6 has no token count: the divider is offline, and a bound only
enforceable by fetching a token report would put a network call in front of the
decision not to make one. The bounds are checked between records, never inside
one, so a run overshoots by the record it began.

What a run reports keeps what it did apart from what the corpus holds. Records
built is not coverage: a run that builds every record it was allowed to touch
says nothing by itself about how much of the corpus has blocks, and the second
number is the one an operator watching a backfill needs to see move.

That number is also a health finding. With construction on, `check_health`
reports `missing_blocks`: the records that divide into more than one block and
hold no current set, found offline by the same division and the same
currentness predicate the builder and the sweep use, so the three cannot
disagree about a record. Under `fix=true` it builds up to 50 of them in one run,
embedding outside the write lock, and never modifies a record. Two cases need
it. A deployment whose task queue is off has no other builder, because the
write path and the sweep both run on the queue. And a deployment upgrading from
a release whose block sets have no re-rank vectors finds every set not current
(invariant 8); the sweep rebuilds them in bounded runs, and the check is how an
operator sees that progress and moves it along. With construction off the
check reports nothing and builds nothing.

## 8. Invariants

1. A block never modifies its parent. Blocks are derived; the record is the
   original.
2. With the feature disabled, responses are byte-identical to the previous
   release, including when block rows exist.
3. Determinism — the same text, node layout and segmentation policy produce
   the same span list. The node layout is what carries the tokenizer's
   influence; the divider itself never calls one, and never reaches the
   network. Ties in Hamming order, and in the cosine order of the re-rank, are
   broken by a written-down total order.
4. Coverage — a block set covers its parent's text with no gap and no overlap,
   and no block crosses a node boundary.
5. Count independence — none of the caps, the re-rank depth, the reservation
   or the examined set may be derived from the response count. Changing the count alone leaves the
   candidate id set unchanged.
6. One record, one candidate — several blocks of one record never become
   several candidates, and their scores are never summed.
7. No new score reaches the quality gate. Block hits are admitted by
   reservation or not at all.
8. A block set is current only if it was built from the parent's current text
   by a known model, and every block in it has its re-rank vector. An
   unreported model identity is not a match.
9. Quotation is never severed — a span shown to a reader carries the contiguous
   context that governs it, or is reported as incomplete.

Each invariant is held by a test whose power is demonstrated by mutation: the
mutation that re-couples a cap to the count, the one that sums a record's
blocks, the one that pushes a block's similarity into the gate, the one that
treats a partially built set as current, and the one that orders a row without
a vector among rows with one must each turn a named assertion red.

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

It does not widen what a caller can ask for. No tool is added and no argument
changes; a reserved row's `match_reason` gains one key, saying which order
placed it (section 4b).

## 10. Decided separately

- Settled in section 4b: blocks hold no float32 vector, they hold one byte per
  dimension, and a second pass re-ranks the Hamming candidates by it. Still
  open is whether the quotation of section 5b should choose its block by the
  same vector, and whether doing so leaves the node vectors with any use.
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
