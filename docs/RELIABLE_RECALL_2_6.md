# Reliable Recall — the 2.6 line

Status: design, not behaviour. This page is the canonical account of what the
2.6 line builds and why — the recall process, its input contract, the scoring
prior, the exit, and how each is measured. Nothing on it is a shipped
guarantee: a section becomes behaviour only when it has been implemented,
pinned by the behaviour golden, and released through the
[lifecycle standard](RELEASE_LIFECYCLE_STANDARD.md). Where this page and a
released tool description disagree, the release is right and the disagreement
is a defect in this page.

Each section stands on its own so that it can be read, cited and injected
separately. The [roadmap](roadmap.md) says where 2.6 sits among the lines;
this page says what it is.

## 0. Why this line exists

The 2.5 line rebuilt the inside of the server without changing what a caller
gets back. It delivered one connection seam, one isolation helper, a recorded
golden of observed responses, mutation proof that the tests bite, and the first
two rungs of the scale ladder. It stopped short of retrieval quality on purpose —
every change that alters ranking or reach was held, because a ranking change
during a production soak cannot be told apart from a regression.

2.6 is the line that changes *what comes back*. It does so on one thesis:

> Recall is a process, not a lookup. The server should be allowed to think
> about a memory the way a model thinks about an answer — iteratively,
> deterministically, and without spending the agent's tokens on it.

Everything below is that thesis broken into parts that can be built and
measured one at a time. Five things never change on any line: the server never calls a model, one SQLite
file the user owns, the schema only moves forward, degradation is reported, and
behaviour is pinned before it is changed. They are stated once on the
[roadmap](roadmap.md#what-never-changes), and assumed here without
restatement.

## 1. Deliberative Recall — the recall process

A language model that reasons before it answers does not produce a better
answer by being asked twice. It produces one by running an internal loop —
hypothesis, check, revision — whose intermediate steps never reach the caller.
Deliberative Recall gives the memory layer the same shape:

```text
recall intent
    ↓
retrieval envelope        (where to look first)
    ↓
fetch                     (one or two ranked lists)
    ↓
evaluate                  (is the evidence sufficient?)
    ↓
revise the envelope       (widen, relax, follow a cue) ──┐
    ↓                                                    │  bounded
select evidence           ←──────────────────────────────┘
    ↓
reconstruct               (the exit — section 7)
```

Four decisions make this a design rather than a metaphor.

**The loop runs inside the server.** An agent that calls `recall` repeatedly,
adjusting its query each time, is also running a loop. But every turn of it
costs a tool round-trip, input tokens, output tokens and latency, and the total
grows with the number of turns. That is the cost profile of an agentic memory
loop, and it is the profile this line refuses. Deliberative Recall iterates
inside one call, under a deterministic policy, so the agent pays for one
request and one response however many turns the loop took. The payload the
agent receives and the tokens it spends downstream are unchanged by the loop's
depth. This is the most concrete form of the project's engineering thesis:
where other systems solve a retrieval problem with more model calls, this one
solves it with structure.

**Most iterations do not touch the index.** The expensive step is fetching a
ranked list from the vector, lexical and keyword arms. Measured on a 100,000-row corpus, the contiguous index answers a vector scan in
tens of milliseconds. At a million rows the same scan is an order of magnitude
slower, and a loop of thirty such scans is not a feature anyone would enable. So the loop fetches once or twice: the near list, and the far list beyond the
scan window when the envelope asks for it. Every later iteration re-weights and
re-filters the candidate pool it already holds. The lexical arm, whose cost
grows with matched rows rather than corpus size and has not yet been measured
at scale, is not re-queried per iteration. This is the same principle as the
counterfactual replay in section 8: a change that only re-scores saved
candidates can be tried many times for the price of one retrieval.

**Cue propagation is the substance of the loop.** An iteration that re-runs the
same query against the same corpus with a slightly different threshold is a
parameter sweep, and it is exhausted in a few turns. The loop earns its iterations only when each turn learns something the next can
use. The timestamp of a strong hit narrows the temporal envelope. The episode,
source or project it belongs to becomes a context cue. The overflow chain it
sits in, and the relations declared on it (section 6), name the places to look
next.
This is spreading activation with every step deterministic and recorded. It is
also what makes the associative layer and the overflow chains part of the
recall process rather than features beside it — they are the fuel the loop
follows.

**The loop is bounded before it is judged.** Thinking has no natural ceiling;
memory retrieval must. The loop stops on evidence sufficiency: score separation, gate outcome,
candidate count, coverage of the cues the caller supplied, contradiction
density. But the hard limits come first — a maximum number of stages, of
candidates considered, and of milliseconds spent. A loop that
hits a limit says so in its trace, as every other bounded operation in this
server does.

Every turn of the loop is recorded in the recall trace (section 8): the intent
as supplied, the envelope at each stage, the gate values and candidate ids per
stage, and why the envelope was revised. A recall that the caller finds wrong
can therefore be replayed and attributed without re-running anything
expensive.

The process has a name in the tool contract only where the agent touches it:
the input (section 2) and the exit (section 7). Everything between is policy,
versioned with the server and stated in the trace.

## 2. Cued Recall — the input contract

Cognitive psychology distinguishes *free recall* — retrieve from a bare prompt —
from *cued recall*, where the retriever is given partial information about the
target: roughly when, roughly where, roughly what was going on. The existing
`recall` tool is free recall. Cued Recall is the contract by which an agent
hands the server what it half-remembers, so that the recall process can start
in the right place.

An agent declares cues, not search parameters. It does not set fusion weights
or decay rates; it says what it believes about the memory, and the server
turns that into a policy. A cue is a **prior, never a filter**:

> A cue must improve recall, and must never become a precondition for it.

A wrong cue biases the first envelope; it does not remove the answer from the
corpus. Widening always reaches the unconditioned search in the end, so the
worst case of a bad cue is the cost of the loop, never a memory that has become
invisible. Isolation boundaries are the exception and stay hard: `agent_id`,
`project_id` and `channel` are not cues, they are the space the search happens
in, and no widening crosses them.

What 2.6 accepts:

- **A temporal cue.** Absolute (`after` / `before`) or relative
  (`weeks_ago`, `months_ago`, `long_ago`, …), with a **three-valued
  confidence**: `sure`, `likely`, `vague`. Confidence is an enumeration on
  purpose. A number from 0 to 1 is a weight under another name, it is not
  calibrated between one agent and the next, and offering it would contradict
  the rule that the agent declares beliefs rather than parameters.
- **Nothing else yet.** A *place* cue (a world, an application, a workspace)
  has no column of its own: the axes that look like places — project and
  channel — are isolation boundaries, and a boundary must not be softened into
  a hint. A *situation* cue (coding, a meeting, a failure investigation) has no
  data behind it in this line. Both are candidates for the lines that add the
  data they need.

The absence of cues is the identity case: a call without them behaves exactly
as it does today, byte for byte. Cued Recall is a progressive enhancement of
`recall`, not a replacement for it.

## 3. One prior function

Several settings that already exist, and two that this line adds, are the same
thing: a weight on a row's vote as a function of where the row sits — by scan
position, by age, or by distance from what the caller declared. The
[reach and recency plan](REACH_AND_RECENCY_PLAN.md) lays out the first five
rows of this table and the measurement behind them; this line adds the sixth.

| Prior `p(row)` | What ships it |
| --- | --- |
| 1 inside the scan window, 0 beyond | the window alone (today's default) |
| 1 inside, 1 in `[window, reach)`, 0 beyond | the reach setting |
| 1 inside, 1 for the first *N* far rows, 0 for the rest | the far-list length |
| 1 inside, **w** in `[window, reach)`, 0 beyond | the priced far vote (2.6) |
| a smooth function of age | recency-weighted search (2.6) |
| a function of age **and the declared temporal cue** | Cued Recall (2.6) |

Building them as one mechanism is not tidiness; it is what keeps the
measurements comparable. The plan's instrument already has identity controls
at both ends of the far-vote sweep, and the temporal cue is one more parameter
of the same function measured on the same corpus with the same strata.

One decision precedes all three 2.6 rows. In production the confidence scorer is on, and when it is on, the recall's last
step re-sorts the whole fused list by a score the fusion order does not survive.
Two fusion modes that differ on a tenth of their rows with the scorer off agree
on every row with it on. A
prior that only the fusion sees would be invisible in production. So the line
either removes the final re-sort or makes the confidence score a function of
the fused score, and it decides this before any prior is measured. The
benchmark record so far was taken with the scorer off; the plan says so rather
than assuming the two regimes agree.

## 4. Depth is not count

Two numbers are conflated in the current `recall` tool, and the conflation
measurably costs accuracy. `limit` is documented as a per-retriever search
depth: it is the top-K each arm hands to the fusion, so asking for five results
also fuses only five candidates per arm. Measured on the benchmark corpus, a fusion over the full candidate list scores
far above the same fusion cut to a hundred. A limit of five put rows
structurally out of reach at every gate value.

The line separates them and gives each its name:

- **Recall Depth** is how far the server digs — the per-arm candidate depth
  the fusion sees, and, for the exit in section 7, how many hops of relation
  and how many pieces of evidence it may follow. It is a server-side knob with
  a floor, a default and a measured cost, and it is independent of what the
  caller asked to receive.
- **The response count** is how many rows come back. For `recall` this is what
  `limit` will mean; for the exit it is the Reconstruction Window of
  section 7.

The verification is a single invariant: changing the count alone must not
change the set of candidate ids the fusion considered. A test asserts it, and a
mutation that re-couples the two (`candidate_limit = count`) must turn that
test red before the work is called done.

## 5. Adaptive fusion

The three retrieval arms are fused by reciprocal rank with fixed weights. The benchmark record shows why that is the wrong constant. With a weak embedding
model, the lexical arms lift the score by six points and rescue whole task
families. With a strong one, the lift nearly vanishes and moves to different
tasks. The fusion cannot tell which case it is in.

The flagship of this line is a fusion whose behaviour follows the embedding
model, the corpus and the query rather than a constant. The candidate axes are
an unsupervised estimate of each arm's reliability (the null-distribution
machinery of threshold calibration is already there to reuse), a principled
connection between rank fusion and similarity scale, and per-query arm
selection. The success condition is stated up front so that it cannot be adjusted
afterwards. The adaptive fusion must beat the raw embedding on *both* the weak
and the strong model at once, on the same twenty-two-task benchmark that
produced the record.

This section is deliberately the shortest. Its content is a measurement
programme, and this page only fixes what the programme must show.

The programme has produced its first results and a design. The [frozen-stage replay](research/frozen-stage-replay-2026-09.md) located the
losses. On the pure-ranking tasks they are the fusion step itself: dense rows
lifted past relevant ones by lexical votes. The remaining losses are an
admission floor and a gate, applied to universes too small for them. The
[identifiability note](research/adaptive-fusion-identifiability.md) shows that
per-arm calibration cannot decide the fusion and names the quantity that can.
The [design](ADAPTIVE_FUSION_DESIGN.md) that follows from both — a reservation
invariant, the gate's rank cut removed, a measured lexical-weight constant and a
default-off conditional-evidence mode — is decided there, against the success
condition fixed above.

## 6. Associations and overflow chains as cues

Two features of this line were designed as retrieval paths; the recall process
of section 1 makes them something more.

**Associative memory** is a declared graph: registered terms with aliases,
and subject–predicate–object relations that the *agent* asserts and the
server stores and walks. It is deterministic by construction — every fuzzy
expansion tried so far regressed on the contamination benchmark, so
association is exact where the vector and lexical arms are probabilistic. It
ships behind a gate, off by default, until an A/B run shows no contamination
regression; an empty registry is a byte-identical no-op.

**Overflow chains** answer a measured defect: the embedding window is shorter
than the longest memory, so a long record's tail is invisible to vector search.
Records past the window split, deterministically and against the window the
embedding server reports, into a chain of nodes that each carry their own
embedding. The split is reported, never silent. The first step keeps nodes out
of the search index: they let a returned record be quoted by its most relevant
part, and the records recall returns are unchanged. Whether nodes also become
search candidates is decided on its own measurement. Schema, division rule and
invariants are in the [overflow tree design](OVERFLOW_TREE_DESIGN.md).

In the recall process both are **cues**. A hit inside a chain names its
siblings; a hit on a registered entity names the relations declared on it; the
loop follows either without a second fetch from the index. And for the exit in
section 7, "belongs to the same chain" and "is joined by a declared relation"
are two of the deterministic keys by which candidates are bundled into
evidence. Neither is required for the process to work — with no relations and
no chains the corresponding steps are identity maps — but with them the loop
has somewhere to go.

## 7. Reconstructive Recall — the exit

Free and cued recall return candidate rows. Reconstructive Recall returns
**recall items**: units of memory assembled from the candidates, each traceable
to the canonical rows that support it. "Reconstruct" here means *select, order
and assign roles* — never compose. The server does not write a sentence it did
not store.

**A separate tool.** The exit is a new tool, not a mode of `recall`. That
keeps the `recall` contract untouched, avoids two knobs with different
meanings on one response, and makes the addition additive — no pre-release
ladder is triggered by a tool that did not exist before.

**Input.** The candidate pool the recall process produced, the isolation axes
as they are, an optional temporal cue, a mandatory set of bounds — candidate
depth, relation hops, evidence count — and an optional `count`.

**The Reconstruction Window.** `count` is the *ceiling* on the number of
recall items returned. It is not a fill target, and it is not a search depth.

```text
base      = forced_count ?? requested_count ?? default_count
effective = min(base, max_count)
0 <= returned <= effective
```

- `default_count` is the server's default when the caller says nothing;
  `max_count` is an absolute ceiling; `forced_count` lets an operator pin the
  base for every call and is null unless set. A configuration in which the
  default or the forced value exceeds the maximum is a startup error, not a
  silent clamp.
- Every response states `effective_count` and `returned_count`. When the server
  did something other than what was asked — it clamped the count, or an operator
  forced it — the response adds `requested_count` and a `count_policy` (`source`,
  `clamped`, `reason`), so a caller can see what the server did with its request.
  A request honoured as asked is not restated: an agent searches several times
  per question, and an audit repeated on every call is paid for every time.
  `trace=true` returns the full audit on any call.
- Fewer items than the window is a normal result and carries a reason: no
  relevant evidence, below the quality threshold, filtered by policy,
  insufficient provenance, payload budget exhausted, or system degraded. The
  shortfall is never filled with duplicates, low-quality items or content the
  evidence does not support.
- The unconfigured default is **one item**, a conservative call contract rather
  than a measured optimum. The maximum remains experimental until a sweep
  over `count` and the payload budget on the long-memory benchmark compares
  answer and evidence quality against payload tokens and latency. Any proposed
  change to either default must be justified by that measurement.

**Breadth before depth — the payload budget.** An item's size is not fixed.
One item may be a single stored row; another may be a burst of turns or an
episode with its supporting claims. Two limits therefore shape a response:

- `count` bounds **breadth** — how many independent memories are returned.
- The **payload budget** bounds **depth** — how much quoted text the items
  carry between them.

The two measure different things, but they spend the same resource: the
reader's context. When the budget binds, keeping every item forces each to be
thinner, and keeping every item whole forces fewer of them. One of the two
must take precedence, and **breadth does**. A cut in depth is recoverable:
every omitted claim keeps its `ref` and expands through `get_contents`. A cut
in breadth is not: a memory that was never returned leaves nothing for the
caller to expand, or even to know about.

```text
budget_base      = forced_budget ?? requested_budget ?? default_budget
effective_budget = min(budget_base, max_budget)
```

- The budget is counted in **characters of quoted text** — `content` and the
  `excerpts` below. Tokens are not used: the server does not know the
  reader's tokenizer, and the preview tier and `get_contents` already bound
  their payloads in characters. Refs, times, reasons and roles are not
  counted; `max_evidence` bounds them.
- A budget below one preview-tier excerpt, or a default or forced budget
  above the maximum, is a startup error, not a silent clamp. The first item
  therefore always fits. The default and the maximum budget have no value yet:
  both are chosen by the sweep in section 9, and the figure in the example
  below is illustrative.
- An item carries its head claim in `content`, as before, and verbatim
  excerpts of its other retained claims in `excerpts`, most relevant first.
  Each excerpt is cut as the preview tier cuts. An excerpt only ever comes
  from the item's own claims: an item grows because the memory has more
  structure, never because a relevance score is high. Scores are not
  calibrated across models and corpora, and growing an item beyond its bundle
  would merge memories the bundling keys call independent.
- **Allocation is one fixed sequence.** Order the quoted text as: the heads of
  the selected items in item order; then each item's most relevant remaining
  excerpt, in item order; then each item's next excerpt; and so on. The
  response carries the longest prefix of that sequence that fits the budget.
  An item whose head falls outside the prefix is not returned, and the
  shortfall reason is `budget_exhausted`. An excerpt outside the prefix is
  omitted, but its claim, ref and roles remain in the item.
- Because the response is a prefix of a sequence the budget does not shape,
  raising the budget alone never removes an item or an excerpt, and changing
  it alone never changes the candidate pool, the clusters or the item order.
- A response states `requested_budget` and a `budget_policy` (`source`,
  `clamped`, `reason`) when the budget was clamped, raised or forced, and
  `effective_budget` and `used_budget` when the budget withheld an item or an
  excerpt; an item whose excerpts were cut states `excerpts_omitted`. A caller
  can therefore tell whether breadth or depth was cut, and a response the budget
  never touched says nothing about it.

The window sits fourth in a series this server already has: the embedding
window (what gets indexed; a split is reported), the scan window (what gets
scanned; a gate fallback is reported), the retrieval envelope (where the loop
looks; the widening is reported) and the Reconstruction Window (what reaches
the agent; a short return is reported). Each is a bounded aperture, and each
says so when it cuts.

**Processing — four stages, all SQL and pure functions.**

1. *Candidates* — the pool from the recall process, unchanged; depth is the
   section 4 knob.
2. *Bundling* — cluster candidates by deterministic keys: same message id,
   same episode's time span, adjacent timestamps, same source, same overflow
   chain. These keys are the *ceiling* of what the server calls "the same
   memory"; semantic sameness is not judged here (see below).
3. *Bounded relation walk*: follow only relations attached to the candidates, to
the hop limit. Those are episode containment, explicit references in metadata,
stable ids cited in the content, and declared relations where the associative
layer is present. With no relations this stage is the identity.
4. *Structuring* — order by time and by version; where two statements on the
   same subject disagree, keep both and mark the conflict. No summarising, no
   merging of text.

**Output — a recall item.**

```jsonc
{ "items": [{
    "content": "…",            // a verbatim excerpt of the head claim, cut as the preview tier cuts
    "head_ref": "…",           // the claim that content quotes
    "excerpts": [{ "ref": "…", "content": "…" }],      // other retained claims, most relevant first, within the budget; absent when none
    "excerpts_omitted": 1,     // retained claims whose text the budget did not carry; absent when zero
    "claims": [{ "ref": "mem:…", "as_of": "…",         // one entry per retained row, newest first
                 "why": "cluster:chain",               // why it is here, always
                 "roles": [{ "ref": "mem:…", "role": "supersedes" },
                           { "ref": "ep:…",  "role": "supports" }] }],  // absent when the row has none
    "independence_reason": "cluster:episode" }],                  // why it is a separate item
  "effective_count": 1, "returned_count": 1,                      // always
  // the rest appears only when it has something to say, or under trace=true:
  "effective_budget": 4000, "used_budget": 3980,                  // the budget withheld an item or an excerpt
  "bounds": { "top_k": 20, "max_hops": 2, "max_evidence": 40, "reached": ["top_k"] } }  // a bound acted
```

- `content` is a quotation. If an agent wants a composed sentence, the
  delegation route (a brief the server prepares, a verdict the agent returns
  and a separate tool applies) exists for exactly that, and it is optional.
- The **role vocabulary** is fixed now and filled in stages: `supports`,
  `supersedes`, `corrects`, `qualifies`, `contradicts`,
  `temporal_predecessor`. In this line the server can derive `supersedes` (message id and time order) and
`supports` (episode containment). `corrects` and `qualifies` need a source of
truth the server does not have — an in-place update leaves no history — so they
appear when declared relations do. A reader ignores a role it does not know.
- Full text is never inlined: `content` and every excerpt are cut as the
  preview tier cuts, and a `ref` expands through `get_contents`, as it does
  for the preview tier today.

**Invariants.**

1. Stored memories are never modified — this is a read path; the only writes
   are the existing recall counters.
2. No model is called. Embedding is allowed; generation is not. `content` is
   a quotation.
3. Determinism — same database state, same query, same bounds, same output;
   ties are broken by a total order that is written down.
4. Boundedness — nothing is scanned past the declared bounds, and no response
   quotes more than the effective payload budget; rows a bound dropped are
   reported in `bounds.omitted`, `claims_omitted`, `excerpts_omitted` or the
   shortfall reason, and a bound that was only met in `bounds.reached`.
5. Explainability — every element says why it is present.
6. The existing `recall` contract is untouched.
7. Count and breadth are decoupled — none of `candidate_limit`,
   `vector_top_k`, `fts_limit`, `selected_evidence_limit` may be derived from
   `count`. The test: change `count` alone and the candidate id set is
   unchanged.
8. No padding — a paraphrase, a fragment of one record, or several pieces of
   evidence for one conclusion are not separate items; a conflict the keys
   cannot fold is shown inside one item.
9. Breadth before depth — the payload budget omits excerpts before it omits
   items, and the response is the longest prefix of a sequence the budget does
   not shape. The test: raise the budget alone and no item or excerpt
   disappears; change it alone and the candidate id set, the clusters and the
   item order are unchanged.

**What is deferred.** An adaptive default, per-query maxima, per-agent count
and budget policy, a budget counted in the reader's tokens, model-assisted
independence judgement, statement-level provenance, and standardised export of
count fields. Adaptation waits until a fixed
policy has a reproducible baseline and an audit contract.

### Reconstruction v1 implementation boundary

The experimental v1 exit retains v0's four stages; the relation walk still
has no edges to follow. Since v1.1 it implements the payload budget as
described above, with a provisional default of 4,000 and maximum of 20,000
characters until the section 9 sweep chooses them; a caller's budget below one
preview-tier excerpt is raised to it and `budget_policy` says so. A claim whose
record has a current overflow-tree node set is quoted from the node that best
matches the query: nodes are ranked by cosine to the query embedding and by
shared character trigrams, the two ranks are fused by reciprocal rank with
equal values sharing a rank, and ties go to the earlier node. The quote then
carries `node` (index, node count and character span in the stored text). Nodes
are read after the items and their order are fixed, so they never change which
items come back ([overflow tree](OVERFLOW_TREE_DESIGN.md)). To read around a
quote without the rest of its record, `get_contents` accepts a ref that names a
node range or a character span in the stored text; a range it cannot serve
exactly is reported in `unresolved`, never widened to the whole row. It adds
these concrete qualifications:

- Time-based bundling requires the same project and channel. Adjacency also
  requires the same source role and id; the entire burst, rather than each
  successive gap, must fit within the configured adjacency interval.
- Episode time containment is evidence only within the same project and
  channel. Message-id bundling, supersession and conflicts require the same
  stored project: equal IDs in different projects are independent records.
  Unknown context does not establish identity. This does not create history
  for in-place updates or infer semantic contradictions.
- The head claim is the most relevant row in the item. If newer versions of
  that record (the same message id in the same stored project) are in the item,
  the head is their latest version. Newest is meaningful only within a version
  chain; among distinct rows of one burst or episode it is merely the last
  thing said. `head_ref` names the head; `claims` stay newest first.
- Every item has the same shape. A row's ref, time and reason appear once, in
  its claim: there is no separate timeline (sort `claims` by `as_of`) or
  evidence array, and `roles`, `excerpts` and `excerpts_omitted` are absent
  when empty. A one-row item carries about half the metadata characters the
  parallel arrays cost.
- `max_evidence` bounds the retained claims and their role targets. A cut
  keeps the head, then the most relevant
  remaining rows. The item counts the dropped rows in `claims_omitted`; a role
  never points to a claim discarded by this bound.
- **What the response admits.** Two facts are kept apart. `bounds.omitted`
  names a bound that dropped rows the tool held: `max_evidence` (for an item
  that was returned) or `max_hops`. `bounds.reached` names a bound that was
  only met: `top_k`, when retrieval returned exactly as many rows as it was
  allowed. Whether more lay beyond is not known, so it is never called a cut.
  A pool that fills its depth is the common case on a large store, and a
  single flag that was true for both facts distinguished nothing there. Both
  fields are absent when empty.
- A quote chosen in a degraded way says so. `quote_selection: lexical_only`
  means no query embedding was available and nodes were ranked by shared
  trigrams alone. An item whose cut quote did not come from a node carries
  `node_unavailable`: `no_nodes`, or `not_current` when nodes exist but are
  partial or built by another model. Such a quote is the start of the record,
  not the part that matched; a span passed to `get_contents` reads further.
- **Absence is not a verdict.** A response without these fields does not say
  that its items suffice to answer, that the whole store was searched, or that
  the rows were checked for contradiction: `conflicts` detects only rows with
  the same message id and timestamp. The exit reports what it did; deciding
  whether the evidence suffices belongs to the recall process of section 1,
  inside the server, and is not delegated to the caller through these fields.
- The response is compact by default. Measured with an answer reader, a search
  that did what it was asked spent about 500 characters restating its policy,
  bounds and pool counts, where the rows themselves cost what recall's rows cost;
  that envelope is now stated only when it has something to say (the count and
  budget rules above; `bounds` when a bound dropped rows, was reached or was
  lowered by the library ceiling; `reconstruction.excluded_without_provenance`
  when rows were excluded).
- An item whose node quote was cut carries `expand`, the argument `get_contents`
  takes to return the rest of that node. A node is several times a quote and a
  record is many times a node, so the smallest next read is handed over ready to
  use: the node first, its neighbours next, the whole record last.
- `trace=true` adds `reconstruction` (the policy version, candidate count, cluster
  count, selected count and rows excluded for missing provenance),
  candidate refs and cluster membership, without full text, and `node_order`:
  for each record quoted by node, its best few node indices, best first. That
  is an order, not a confidence, and it stays in the trace until an
  answer-reader evaluation shows a reader uses it. A caller can tell
  whether count limited the output or retrieval supplied too few candidates.
- `gate_fallback` is preserved even when the requested count is filled.
  An explicit zero count returns no items and states `count_zero`.
- Retrieval `advisory` and `update` notices survive reconstruction, including
  empty results, so consuming a per-session notice also delivers it.
- `bounds.top_k` records the requested candidate bound. If the library ceiling
  reduces it, `bounds.effective_top_k` records the applied bound, including on
  an empty result. A shortfall describes
  the retrieved pool; it does not establish that the whole corpus was exhausted.
- Authenticated use requires the same per-agent read grant as `recall`.

The call-level count defaults to one item by contract, with an optional
operator-forced override. Neither is a fill target: requesting or forcing five
returns two when only two valid items are available. The maximum of ten
remains experimental; neither value claims an empirically optimal
answer-quality/payload tradeoff. Qualification includes
real-write conversation fixtures and a count replay over retained session
rankings; that replay does not substitute for an answer-reader evaluation or
production-depth retrieval. See the reconstruction qualification record in
`benchmarks/measurements/`.

## 8. Recall Quality Engineering

A recall that came back wrong used to be investigated by reading logs,
guessing, changing a setting and re-running a whole benchmark. This line makes
recall failure an engineering object: observable, classifiable, replayable,
and fixable by the smallest change that addresses the cause.

**The pipeline, and where it fails.**

```text
capture / representation
    ↓
candidate generation
    ↓
ranking / fusion / filtering
    ↓
evidence selection / reconstruction
    ↓
agent consumption / task outcome
```

Each stage has its own way of losing an answer, and the failures already
found on the 2.5 baseline are spread across all of them: a scan window
mistaken for a response limit made old memories structurally invisible; rows
without a cosine outranked rows with one; a scoring change restored a stale
calibration; a single random draw swung the fused gate; undated rows were
treated as newest; a timestamp fix triggered a different penalty. None of
those was a tuning problem. Each was an interaction between mechanisms, and
the point of this section is that the next one is found by looking at the
stage, not by guessing.

**The recall trace.** A recall can produce, on request, a trace sufficient to
replay it: the query as normalised and the policy version; the isolation
scope; the requested and effective counts and the internal candidate counts;
the candidates per arm with their ranks and scores before and after fusion;
per-candidate contributions and the reason for every exclusion; the evidence
selected and rejected and each piece's contribution; the index generation,
embedding model, configuration hash and per-stage latency; and, for the
recall process, every stage of the loop as section 1 describes. A trace that
leaves the machine does not carry raw queries, memories or embeddings — the
diagnostic-capsule work under [PPDC](#11-what-this-page-does-not-decide)
defines what may.

**The failure taxonomy.** Every investigated failure gets one code:

| Code | Meaning |
| --- | --- |
| `CAPTURE_MISS` | the information was never stored |
| `REPRESENTATION_MISS` | stored, but not in a searchable form |
| `CANDIDATE_MISS` | the answer never entered any arm's candidates |
| `RANKING_MISS` | a candidate, but ranked below the return cut |
| `FILTER_DROP` | removed by a filter, threshold or budget |
| `EVIDENCE_NOISE` | buried by evidence that should not have been selected |
| `RECONSTRUCTION_LOSS` | lost while bundling or structuring |
| `RECONSTRUCTION_UNSUPPORTED` | content appeared that no evidence supports |
| `STALE_CONFLICT` | old or contradicted information won |
| `AGENT_MISUSE` | the right memory came back and the agent misused it |
| `SYSTEM_DEGRADED` | a fault, timeout or misconfiguration lowered quality |
| `UNATTRIBUTED` | not yet attributable |

The recall process adds its own: `INTENT_MISLEADING`, `WINDOW_TOO_NARROW`,
`WINDOW_TOO_WIDE`, `WIDENING_PREMATURE`, `WIDENING_INSUFFICIENT`,
`PRIOR_DOMINANCE`, `PRIOR_IGNORED`, `INTENT_NORMALIZATION_ERROR`. Because the loop's policy is deterministic, these can be assigned mechanically,
by replaying the trace with one thing changed. `UNATTRIBUTED` is reported, not
hidden: the rate at which failures can be attributed is itself a metric of this
section.

**Counterfactual replay.** Take a failed recall and change one condition:
raise the depth; lift a threshold; run one arm alone; change a fusion weight;
return the raw evidence instead of the reconstruction; widen the budget;
disable a cue; substitute the embedding; run the 2.5 code; hand the answer
model the oracle evidence. Whenever the saved candidate pool suffices, these
run locally in bulk without an embedding or a model; when a stage must be
re-executed, only the stages after the suspected cause are.

**The improvement loop.** A failure is detected (by a benchmark or by a
production report); the trace is analysed and a code assigned; similar
failures are clustered by mechanism; a hypothesis is tested by replay; a
candidate fix is tried in isolation; it is confirmed on the failure slice, then
on a frozen holdout and the full benchmark; its cost in tokens, latency, memory
and provenance fidelity is checked; and a human promotes it as a versioned
change. The isolated experiment is automated; the promotion is not.

Three data sets are kept apart so that the loop cannot overfit the public
benchmark: a development set for attribution and search, a validation set for
choosing among fixes, and a frozen holdout that is not opened until the final
decision. Cross-benchmark checks and privacy-preserving replay of
production-like cases sit alongside them.

**The auditor profile.** The attribution above is a *profile* of the
[SuperAuditor standard](SUPERAUDITOR_STANDARD.md). The standard fixes the shape
of a finding, its severity and its delivery, and says nothing about what is
detected. The profile fixes the trace, the taxonomy, the evidence for a cause,
and the replay outcome. The layering is deliberate. The core stays
small and general, the profile carries everything recall-specific, and a
second memory system could implement the profile without adopting this
server's internals. The profile is written once its second implementation
exists, as the standard itself was.

## 9. How the line is measured

Every claim on this page is a measurement waiting to happen, and the
measurements share one rule: **the 2.5 baseline is frozen, and 2.6 is measured
against it.** Same corpus, queries, embedding, answer model, hardware and token
budget. Compared per task, per query type, per language, per memory scale and
per failure slice, not only in aggregate; regressions and trade-offs
published beside improvements.

**Retrieval quality** is measured on the twenty-two-task benchmark the record
was built on, with the truncation layers off and the full ranking regime, as
the [benchmark harness](https://github.com/Cloto-dev/cpersona/blob/master/benchmarks/README.md)
documents. Adaptive fusion (section 5) and the prior function (section 3) are
judged here. A benchmark's `k` and this server's response count are different
variables and are never conflated.

**The recall process** (sections 1–2) is judged by a pre-registered claim:
*evidence recall rises while the payload tokens and the end-to-end memory
tokens stay unchanged*, because the loop spends none of the agent's tokens.
The instrument is the long-memory corpus with its near/far strata and
rotations that produced the reach measurements. The arms are a hint robustness pack: correct cue, approximate cue, wrong cue, no
cue, contradictory cues. The expected shape is that a correct cue improves,
an approximate one improves or is neutral, a wrong one recovers gracefully, and
no cue is identical to today.
If the loop does not move the evidence recall under those conditions, the loop
is decoration and is not shipped.

**The exit** (section 7) is judged by a `count` sweep — 1, 2, 4, up to the
maximum — crossed with a payload-budget sweep, reading answer quality and
evidence quality separately against payload tokens and latency. The sweep
runs on a corpus where bundling actually occurs — turns stored as rows with
their session dates and speaker roles — and reports the share of items with
more than one claim: on a corpus where every item is one row, the sweep
measures ranked rows, not reconstruction. The exit is also judged by seven
ablation arms that separate retrieval failure from evidence-selection failure
from reconstruction failure from agent-reasoning failure: the 2.5 flat recall; 2.6 retrieval alone; evidence
selection without reconstruction; reconstruction at `count = 1`; the sweep;
the answer model given oracle evidence; the answer model given raw evidence
instead of the reconstruction. Mutations that must fail before the exit is
called done: the default changed from one to two; the minimum with the maximum
removed; the forced and requested priorities swapped; the candidate limit
re-coupled to the count; deduplication disabled; provenance dropped; the
returned count always reported as the effective count; excerpts allocated
before every head is placed; an excerpt taken from outside its item; the
allocation sequence re-ordered by the budget.

**Tokens** are measured as the [project direction](roadmap.md) defines them:
recall payload tokens, downstream input tokens, end-to-end memory tokens,
amortised write tokens, and tokens per correct answer. They are reported with
cached and uncached, input and output, and memory-attributable and total agent
tokens kept apart. Reducing a count is not a token-efficiency claim.

**Scale** is measured at 1K, 10K, 100K and 1M rows, with quality, tokens,
latency and peak memory read together; the metric is the slope of degradation,
not the largest number reached.

No new instrument is built for any of this. The benchmark harness, the
embedding cache and the scan-window instrument already exist, and every
measurement above runs on them.

## 10. What "done" means

The line closes when all of the following hold, in this order of importance:

1. **The recall process and Cued Recall ship behind a gate, off by default,
   byte-identical at the default**, and the pre-registered claim in section 9
   has been shown on the frozen baseline.
2. **The final re-sort has been decided** (section 3), the priced far vote and
   the recency prior are one mechanism, and the benchmark record has been
   re-taken under the production regime.
3. **Depth and count are separated** (section 4) with the coupling invariant
   under test.
4. **Reconstructive Recall exists as a tool** with the count contract, the
   role vocabulary, the nine invariants of section 7 and the mutations of
   section 9, and its default count and payload budget were chosen by the
   sweep.
5. **Adaptive fusion beats the raw embedding on both models** on the same
   benchmark, or the section records why it did not and what replaces it.
6. **Every failure on the benchmark has a code and a replayable trace**, the
   unattributed rate is reported and falling, and the auditor profile's
   schema is published.
7. **The accuracy–token–latency–memory frontier has moved** relative to 2.5,
   with the per-slice regressions disclosed.
8. **The open quality debt of the 2.5 baseline** — a query-relative score used
   as an absolute gate, an unnormalised embedding backend against a calibrated
   scale, mixed-offset timestamps in a text comparison, the `limit` coupling,
   the missing comprehensive baseline — is each fixed, rejected with reasons,
   or carried forward with reasons.

Two things are **not** completion conditions, on purpose. The Go index
service belongs to the runtime axis of the [roadmap](roadmap.md#the-runtime-and-scale-ladder):
it triggers at a corpus size, not at a version, and tying it to this line
would let either hold the other hostage. And the associative layer's
default-on flip belongs to its own A/B run, not to the line's closing.

## 11. What this page does not decide

- **Version numbers and dates.** The [roadmap](roadmap.md) places 2.6 among
  the lines; the release notes say what shipped.
- **Implementation order.** Dependencies are stated where they bind (the
  re-sort decision before any prior; depth separation before the exit); the
  order within them is the work's.
- **The scoring semantics themselves** — the fusion formula, the prior's
  curve, the tie-break order — which are fixed by measurement in their own
  design records as they are settled.
- **The standards this line consumes.** The
  [SuperAuditor standard](SUPERAUDITOR_STANDARD.md) is published; an open
  benchmark for cognitive efficiency (resource-normalised, vendor-neutral,
  Pareto-ranked) and a privacy-preserving diagnostic capsule (**PPDC** —
  *raw data stays local, diagnosis travels*) are drafts and will be published
  as their own pages when they leave discussion.
- **The lines after this one.** Memory intelligence — confidence, correction,
  contradiction, forgetting — is 2.7's subject; the recall process is built
  so that those signals can enter the loop as cues when they exist.
