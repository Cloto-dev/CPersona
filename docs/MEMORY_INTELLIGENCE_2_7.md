# Memory Intelligence — the 2.7 line

Status: design, not behaviour. This page is the canonical account of what the
2.7 line builds and why. Nothing on it is a shipped guarantee, and nothing on
it has been measured yet: a section becomes behaviour only when it has been
implemented, pinned by the behaviour golden, and released through the
[lifecycle standard](RELEASE_LIFECYCLE_STANDARD.md). Where a design question
has not been decided, the page says so under **Open** rather than filling the
gap with a plan. Where this page and a released tool description disagree, the
release is right and the disagreement is a defect in this page.

Each section stands on its own so that it can be read, cited and injected
separately. The [roadmap](roadmap.md) says where 2.7 sits among the lines;
this page says what it is.

## 0. Why this line exists

The 2.6 line changes *what comes back*: it makes recall a bounded process
inside one call, and that process follows cues — time, episode, source,
declared relations, overflow chains
([Reliable Recall](RELIABLE_RECALL_2_6.md)).

Every one of those cues answers *where a memory is*. None of them answers how
far the memory can be **trusted**, whether it is **still valid**, or whether it
was **corrected later**. The server cannot say any of the three today. 2.7 is
the line that gives it those signals.

> Finding a memory does not make it usable. The server has to be able to
> show, deterministically, the state in time a memory is returned as, how
> certain it is, and what has replaced it.

The five things that hold on every line are stated once on the
[roadmap](roadmap.md#what-never-changes) and assumed here. The first of them
shapes this whole line: the server never calls a model. So **a judgement about
meaning is asserted by the agent; the server stores it, queries it, and checks
it for consistency**. The server does not read two texts and find that they
contradict each other.

## 1. Five items and one extension

| # | Item | The question it answers | What it inherits from 2.6 |
| --- | --- | --- | --- |
| 1 | Correction and contradiction | Has this memory been corrected since, or does it disagree with another one? | The role vocabulary of declared relations (`supersedes`, `corrects`, `contradicts`, `qualifies`) |
| 2 | Temporal state and history | At what time was this memory a fact, and is it still valid? | The temporal cue of Cued Recall, the prior function, timestamp normalisation |
| 3 | Evidence-weighted confidence | How far can this memory be trusted, and on what grounds? | The claims and independence judgement of Reconstructive Recall; the opt-in confidence scorer |
| 4 | Forgetting and retention policy | What stays, what retires from recall, and what is deleted? | Lock, delete, the resolved flag, isolation by agent / project / channel |
| 5 | Feedback on what a recall was used for | Was the memory that came back of any use? | The recall trace, the Reconstruction Window |
| Extension | Control of quality degradation as the corpus grows | Where does the quality curve bend as the row count rises? | The scale ladder and its per-size measurements |

The extension gets **measurement only** in this line. The design of a control
opens after the measurement exists.

### Order of dependency

```text
1 correction ─────┐
                  ├─→ 3 confidence ──→ 4 forgetting and retention
2 temporal state ─┘                          ↑
5 feedback ──────────────────────────────────┘
```

Items 1 and 2 can start independently. Item 3 takes 1 and 2 as inputs: a
corrected memory, or one whose validity has ended, is less certain. Item 4
takes 3 and 5 as inputs: what is least trusted and least used retires first.
Item 5 has an independent input side, so it can run alongside 1 and 2.

## 2. The design question in each item

Every item is written in the same form: the problem, what the server
receives, what the server does, its role inside the recall process, what does
not change, how it is measured, and what is still open.

### 2.1 Correction and contradiction

**Problem.** Memories get revised. An in-place update leaves no history, and
when a *separate* record corrects an earlier one — "A is X", and later "A was
Y; X was wrong" — both come back at the same strength.

**What the server receives.** Record-to-record relations declared by the
agent: `corrects`, `supersedes`, `contradicts`, `qualifies`. The vocabulary is
already accepted by the associative memory of 2.6.

**What the server does.**

- By default it returns the correcting record, carrying a marker that an
  earlier version exists and that version's `ref`. The corrected text itself
  is returned only on an explicit request. Nothing is hidden — the `ref` is
  always there — but the old content does not arrive looking like a current
  fact.
- It returns an unresolved `contradicts` pair marked as a contradiction,
  without silently choosing a side.
- It checks declarations for consistency: a `supersedes` cycle, a
  self-reference, a relation that crosses an isolation boundary.

**Inside the recall process.** A correction edge is a cue. When the loop hits
the older side, it follows the edge to the newer one without going back to the
index.

**What does not change.** Stored rows are not rewritten. A corrected memory is
not deleted; it remains as history.

**Measurement.** On a question set that contains corrections: the rate at
which pre-correction content is returned as a current fact. Pre-registered.

**Open.** Whether unresolved contradictions are detected only among declared
pairs, or whether relations with the same entity and predicate but a different
object are also listed mechanically as candidates (listed, never judged).

### 2.2 Temporal state and history

**Problem.** A memory's timestamp says when it was written. It does not say
when the fact held, or until when. Last year's address and this year's are
both true; their validity intervals differ.

**What the server receives.** A validity interval asserted by the agent
(`valid_from` / `valid_to`, either one optional). An earlier plan had a model
extract dates from text; under the no-model rule the agent asserts the
interval.

**What the server does.** This line takes the bi-temporal model whole: two
time axes, the time a fact held (asserted) and the time the server recorded it
(already stored).

- It stores the interval and answers "what was valid at this point in time".
- It answers along the record axis as well — "what did the server hold at
  that time about this point in time" — so that a later correction does not
  erase what was known before it.
- It returns a memory whose validity has ended as a past state, not as a
  current fact.
- It bundles the successive states of one subject into a history ordered in
  time, at the reconstruction exit.

**Inside the recall process.** The temporal cue of Cued Recall applies to the
validity interval as well as to the write time.

**What does not change.** A memory without an interval behaves byte for byte
as it does today (progressive enhancement). Schema changes are additive only.

**Measurement.** Evidence recall on question types that ask about time (when,
at that time, now), and the rate at which a past state is returned as the
present.

**Open.** Whether the interval lives only on relations (edges) or on memory
rows as well.

### 2.3 Evidence-weighted confidence

**Problem.** The confidence score that exists today is an opt-in scorer built
from similarity, time decay, the resolved flag and recall count. When it is
on, it re-sorts the whole fused list, and 2.6 decides whether that step is
removed or becomes a term of the fusion
([the decision](RELIABLE_RECALL_2_6.md#3-one-prior-function)). Whichever way
that goes, the value says how well a row fits the *query*. It does not say how
certain the *memory* is.

**What the server receives.** (a) A certainty the agent attaches at write
time, as a three-valued enumeration — for the same reason the temporal cue of
2.6 uses one: a float is not calibrated between one agent and the next.
(b) The signals items 1 and 2 produce.

**What the server does.** It composes a per-memory confidence,
deterministically, from the number of independent corroborations, the
correction and contradiction state, the validity interval and the kind of
source, and returns it **together with the breakdown of its grounds**. It does
not return a bare number.

**Inside the recall process.** Confidence enters the judgement of whether the
evidence is sufficient: the loop can tell one certain row from three uncertain
ones when it decides to stop or to continue.

**What does not change.** Confidence does not reorder the result. Relevance
and confidence are returned as two separate values, and the ranking stays the
ranking of relevance.

**Measurement.** Calibration: whether memories the server called certain are
correct at a higher rate than those it called uncertain.

**Open.** The definition of an "independent corroboration" — another episode,
another source, another day — and whether the independence judgement of
Reconstructive Recall is reused as it is.

### 2.4 Forgetting and retention policy

**Problem.** Today there is delete and there is lock. There is no "keep it,
but retire it from recall". As the row count grows, old, corrected and unused
memories occupy the candidate pool.

**What the server receives.** A retention policy declared by the user or the
agent, per unit of isolation.

**What the server does.**

- It makes *retire* (leave the recall candidates, stay retrievable on
  request) and *delete* two different operations.
- It **lists** the memories a policy matches and presents them; applying the
  policy is a separate, explicit operation. The side that observes and the
  side that changes are kept apart.
- A locked memory is outside every policy.

**Inside the recall process.** A retired memory does not enter the near list.
It arrives only as a far candidate when the window is widened.

**What does not change.** Under no setting does the server delete a memory on
its own judgement.

**Measurement.** Before and after a policy is applied: evidence recall (it
must not fall), and the size of the candidate pool and the latency (they
should fall).

**Open.** Whether the retired state is held in a new column or as a value of
an existing field. Whether a default policy ships with the server or a policy
is always written by the user.

### 2.5 Feedback on what a recall was used for

**Problem.** The server knows what it returned. It does not know whether any
of it was used. Quality work today turns only on benchmarks.

**What the server receives.** A per-recall report from the agent — used, not
used, wrong — naming its targets by `ref`.

**What the server does.** It stores the report, joins it to the recall trace,
and (a) feeds item 4, (b) attaches a mechanical failure classification,
(c) publishes the aggregate as a diagnostic. **A report does not change the
ranking automatically in this line.** A loop in which the server reinforces
itself from its own output is not introduced before the reliability of the
reports has been measured.

**Inside the recall process.** Not directly. It enters through item 4, and
through later lines.

**What does not change.** With no report, nothing changes. The content of a
report — a raw query, a memory's text — never leaves the install.

**Measurement.** Agreement between the reports and correctness scored
independently. If the reports cannot be trusted, this item is recorded as
*not adopted*.

**Open.** The report vocabulary (whether three values are enough). Whether a
report counts as a write, and is therefore not recorded in a session that has
paused persistence.

## 3. What "done" means

Every item closes in two layers.

- **Complete**: the design is on this page, it is implemented, it is pinned by
  the behaviour golden, and mutation has shown that the tests fail when the
  logic is broken.
- **Verdict**: the effect has been measured against a decision rule
  registered beforehand, and one of three outcomes is recorded — made the
  default, kept opt-in, or not adopted. A *not adopted* is published as a
  result like the others.

The line is done when all five items are complete and each carries a verdict.
The extension asks only that the per-size measurements are published.

## 4. What this page does not decide

- **Bounded remediation** — a conformer that applies a reviewed repair and
  re-audits. It belongs with the policy, permission and rollback boundaries
  around repair, which are the subject of the
  [2.8 candidate](roadmap.md#28-a-candidate-not-a-line), and has no design
  yet.
- **How the lines after this one use these signals.**
- **Defaults.** No item changes a default before its verdict has been
  measured.
- **Version numbers and dates.** The [roadmap](roadmap.md) places 2.7 among
  the lines; the release notes say what shipped.
