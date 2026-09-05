# Roadmap

Where each release line is going and why — not where it is. Progress lives
in the [release notes](https://github.com/Cloto-dev/cpersona/releases), the
line's tier and its current version in
[SUPPORT.md](https://github.com/Cloto-dev/cpersona/blob/master/SUPPORT.md),
and the rules a line follows in the
[release lifecycle standard](RELEASE_LIFECYCLE_STANDARD.md). This page is
descriptive: it records what a line is *for*, what it is allowed to break, and
which problems its features answer. It is not a delivery commitment, and an
entry that says "undecided" is more useful than one filled with a plan nobody
has measured yet.

## Three axes, not one line

A single version list would flatten three things that move independently:

| Axis | What moves along it | Where to read it |
| --- | --- | --- |
| **Release lines** (2.4 → 2.5 → 2.6 → …) | What the server does and what it may break | this page |
| **Runtime and scale** | How large a corpus one install can hold and how fast it answers, from Python on SQLite towards a Go index service | [the scale ladder](#the-runtime-and-scale-ladder) below |
| **Support tiers** (Stable / Current / Experimental) | Which line to run and how long it receives fixes | [SUPPORT.md](https://github.com/Cloto-dev/cpersona/blob/master/SUPPORT.md) |

The runtime axis cuts across release lines: a scale stage ships in whichever
line is Current when it is ready, so "2.6" names a set of retrieval features
and says nothing about how far the ladder has been climbed.

## What never changes

These hold on every line. A feature that needs one of them to bend is not
scheduled; it is redesigned.

- **The server never calls a language model.** Summarising, extracting and
  judging are the agent's job; the server stores, indexes, and retrieves
  deterministically. Memory therefore adds no generative API cost, and a
  result is reproducible from the corpus and the configuration.
- **One SQLite file the user owns.** No external database, no service the
  data depends on. Embedding is a separate process that can be absent.
- **The database schema only moves forward.** Upgrades migrate in place;
  additive changes (new columns, new tables) are the norm, and restructuring
  an existing table has no precedent and would need a migration design that
  does not exist yet.
- **Degradation is reported, never hidden.** A recall that lost its vector
  layer says so in the response; a health check names what it could not
  verify.
- **Behaviour is pinned before it is changed.** A recorded golden of observed
  responses and an equivalence gate stand between any refactor and a silent
  change in what a caller gets back.

## Release lines

### 2.4 — the shape that shipped

Established the product as it is documented today: three retrieval layers
(vector, full-text, keyword) fused by rank, a scored and gated result list,
isolation by agent / project / channel, the health and maintenance tools, and
PyPI packaging. Feature-frozen; it receives the Stable fix policy only.

### 2.5 — stabilise the inside, keep the outside

**Purpose.** Rebuild the internals that a deep audit showed to be fragile
*before* touching retrieval quality: a single connection seam, one isolation
helper, a test harness that pins behaviour, and mutation proof that tests bite.
Everything on the 2.6 list touches the recall hot path; this line is the
foundation it sits on.

**What it may break.** Internal architecture — freely; that is the point.
The tool contract — where it improves the contract's honesty or consistency,
and only through the pre-release ladder. The database schema — no; a 2.4
database opens under 2.5 and back.

**What also landed here**, because each was additive and rollback-safe:
per-client capabilities enforced server-side; OAuth as the identity layer over
them; a server-served operating context; declared session identity; recorded
access origin; the contiguous embedding index and chunked scan (the first two
rungs of the ladder below); opt-in reach beyond the scan window; size caps
that warn before they reject; and update awareness — the server can say a
newer or withdrawn release exists.

**How it closes.** Feature work on this line has stopped. The remaining
pre-releases are fixes, the final release is the last one that carries new
behaviour, and after it the line freezes, soaks in production, and either
certifies as Stable or does not — the mechanics are the lifecycle standard's.
Scoring corrections that would change ranking are deliberately held for 2.6:
a ranking change during a soak cannot be told apart from a regression.

### 2.6 — recall quality

**Purpose.** Improve *what comes back*, on the foundation 2.5 built. Every
item below changes ranking or reach, which is why none of them shipped in
2.5.

**What it may break.** Internal architecture and the tool contract, through
the ladder. Schema: additive tables and columns are expected (graph and
chain nodes need them); restructuring is not planned.

**Features, each answering a measured problem:**

- **Adaptive fusion** — the flagship. Benchmarks showed the lexical layers
  help a weak embedding model a lot and a strong one barely, and that the
  fixed-weight fusion cannot tell which case it is in. The goal is a fusion
  whose behaviour follows the model, the corpus and the query rather than a
  constant.
- **Associative memory** — a declared graph layer: registered terms with
  aliases, and subject–predicate–object relations the *agent* asserts and the
  server stores and walks. Deterministic by design; fuzzy expansion has
  regressed every time it was tried, so association is a third retrieval
  path that is exact where the other two are probabilistic. Off by default
  until an A/B run shows no contamination regression.
- **Recency in scoring, and the fate of the final re-sort** — time should
  weigh in candidate selection, not only in a re-ranking pass afterwards.
  Measurement showed the current confidence re-sort overrides the fusion
  order entirely when it is enabled, so this line decides whether that pass
  survives or recency becomes one term inside fusion. The weight of "far"
  candidates beyond the scan window is designed as a special case of the same
  prior.
- **Fusion depth separated from response size** — asking for five results
  today also fuses only five candidates per retriever, which measurably costs
  accuracy. Depth becomes its own knob; `limit` means what it says.
- **Overflow chains** — the embedding window is shorter than the longest
  memory, so a long record's tail is invisible to vector search. Records past
  the window split into chained nodes that each carry their own embedding;
  the response points at the hit node and lets the agent fetch the rest.
- **Reconstructive recall** — a read-side pass that clusters and summarises
  what the retrievers returned without altering any stored memory. The
  overflow chains are its substructure; a chain is a natural cluster key.
- **A delegation route for semantic judgement** — the server enumerates
  candidates deterministically and hands the agent a brief; the agent (or a
  sub-agent it chooses) judges; a separate tool applies the verdict as an
  explicit, validated operation. Keeps the no-model rule while letting
  maintenance use one.

Also at the start of this line, because they break more than one consumer:
the MCP SDK 2.0 migration of the shared vendored layer, and the native
`{attempted, ok, error}` result shape for embedding calls.

### 2.7 — named, not planned

The working theme is **memory operations at scale**: how a corpus that has
grown through 2.6's chains and graph is maintained, consolidated and kept
searchable over time. Nothing here is designed or measured. Expect this entry
to be rewritten; do not build on it.

### 3.0 — what remains of the graph plan

An earlier plan made 3.0 "the graph release": entity and relation tables, a
bi-temporal model on edges, and model-driven memory evolution, in three
sub-phases. Sorted against what has since moved:

| Earlier 3.0 item | Where it stands now |
| --- | --- |
| Graph memory (entities, edges, mentions) | Brought forward to 2.6 as associative memory — and redesigned: declared relations and alias canonicalisation instead of model-driven entity extraction. |
| Bi-temporal edges (`valid_from` / `valid_to`, temporal queries) | Stays a 3.0 candidate. The old design had the model extract dates; under the no-model rule the agent asserts validity and the server stores and queries it. |
| Full memory evolution (retroactive edge updates, pruning, strengthening) | Open. The model-driven form is out. What remains is whichever deterministic consolidation the 2.6 delegation route and maintenance tools do not already cover. |
| Sub-phasing (alpha → beta → final by feature) | Superseded by the line structure on this page. |

On the runtime axis, 3.x is where the server itself is expected to be
largely Go (below); the two are the same major version for that reason.

## The runtime and scale ladder

The target for a default install is **one file, a small machine, millions of
rows**; hundreds of millions are an opt-in or a separate, dedicated service.
Measured on a 100 000-row corpus, the bottleneck of vector search was not the
arithmetic — it was moving embeddings out of SQLite into memory. The ladder
attacks that, one rung at a time, and each rung falls back to the one below
when its precondition is missing:

| Rung | What | Status |
| --- | --- | --- |
| 0 | Full scan of embeddings read from SQLite | the baseline |
| 1 | Chunked exact scan — bounded peak memory | shipped in 2.5 |
| 2 | Exact float32 contiguous index beside the database — no approximation, several times faster | shipped in 2.5 |
| 3 | Binary coarse candidates + exact re-rank — the first Go component, a sidecar process, and the first rung that needs a degradation gate | designed; triggers at roughly a million rows |
| 4 | Approximate nearest neighbour | opt-in, or the dedicated embedding service |
| 5 | The lexical layer's cost, which grows with matched rows rather than corpus size | under measurement |

Go arrives gradually: new components and measured bottlenecks first, the
server body later, Python kept as the reference runtime and the generator of
the behaviour golden that any port must reproduce. The boundary is a sidecar
process rather than an in-process extension so that the PyPI package stays
pure Python.

## How this page is kept honest

Entries name reasons, not tickets. When a line's purpose changes, this page
changes with the decision; when a feature ships, the entry is not marked done
here — the release notes are. If this page and a shipped release disagree, the
release is right and the disagreement is worth a report.
