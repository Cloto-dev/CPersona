# The Recall Process, v0 — design

**Status:** the recall trace (§1) and the loop's basic form (§2) are
released in 2.6.0a7. It is the first step of
[the recall process](RELIABLE_RECALL_2_6.md#1-deliberative-recall-the-recall-process),
[Cued Recall](RELIABLE_RECALL_2_6.md#2-cued-recall-the-input-contract) and the
[recall trace](RELIABLE_RECALL_2_6.md#8-recall-quality-engineering). v0 has
two parts, built in this order: the trace, which is checked as an instrument
before anything relies on it, and then the basic form of the loop.

## 0. What v0 adds

- **A recall trace.** On request, `recall` and `reconstruct` return a record of
  which rows each stage kept, dropped and reordered, and why. It carries
  references and scores, never text. Without the request, nothing changes.
- **A time cue.** A caller that half-remembers *when* something happened can
  say so (`time_cue`), with a confidence of `sure`, `likely` or `vague`. The
  server searches that period as well as everywhere else, and lets a row found
  there move up by a bounded number of places. A wrong cue can cost at most
  that bounded move and as many seats as `L`. Without a cue, nothing changes.
- **One revision.** If the period holds nothing, the server widens it once and
  looks again, inside the same call.

Every recall that uses the loop records the policy it ran under, so a result
can be replayed and attributed later.

## 1. The recall trace

### 1.1 How it is requested

`recall` gains `trace: true`, and `reconstruct` already has an argument of that
name. When `reconstruct` is asked for a trace, it also returns the trace of
the recall it made, as `trace.recall`. The trace is returned in the response.
v0 does not store it on the server; a caller that wants to keep it, such as a
benchmark harness, keeps it.

The trace carries no stored text: references, ranks, scores and reasons only.
What a trace may contain when it leaves the machine is decided separately.

### 1.2 Shape (`trace_version` 1)

| Field | Content |
| --- | --- |
| `trace_version` | `1`. Raised only when an existing field changes meaning; adding a field does not raise it |
| `policy` | `{scoring, process}`: the scoring version and the recall-process policy the call ran under (`single-pass-v0` without a cue, `cued-v0.3` with one; `cued-v0.2` before §2.11, `cued-v0.1` before §2.10, `cued-v0` before §2.8) |
| `server_version` | The version that answered |
| `scope` | `agent_id`, `project_id`, `channel`, `source_id` as resolved |
| `request` | `limit`, the recall depth, `deep`, the fusion mode, the confidence ordering, the prior's settings, whether the episode penalty is on, and the `time_cue` when given |
| `config` | Embedding mode and model, scan window, reach and far-list limit, and whether the fused gate and autocut are enabled |
| `arms` | Per retrieval arm (near vector, far vector, episode full text, memory keyword, block, and the cue arm): `{ref, rank, raw}` up to the depth |
| `keyword_ranking` | Present when the memory keyword arm was asked for its order only (the rrf and cascade fusions): per call, how many of the query's phrases sat at bm25's idf floor and were left out of the ranking (`left_out` of `phrases`), the `bound` leaving them out cannot move a score by, and whether the rows were ranked on the rest (`rare`) or on the whole expression (`whole`). After `rare` the rows and their order are the whole expression's, and the arm's `raw` scores are the rest's, lower by less than the bound |
| `block_source` | Present when the block arm ran: where its examined rows came from. `file` (the block index file, [contract §3.4](BLOCK_CANDIDATES_CONTRACT.md#34-how-the-file-stays-exact)) with `changed_records` and `live_rows`, the records changed since the build and their rows read from SQLite beside it; or `sqlite` with the `reason` the file was not read: `off`, `no_file`, `unusable`, `width`, `no_log`, `logging_off`, `generation`, `newer_than_snapshot`, `pruned`, `schema_changed`, `live_bound`, `unholdable_value`, `error`. The answer is the same either way |
| `fusion` | Per candidate: the fused score and each arm's contribution |
| `scoring` | The episode penalty's factor and the prior's weight per row, where applied |
| `gate` | Signal, the calibrated threshold or the heuristic minimum and which one applied (`origin`), the pool size, one decision per candidate (admitted, or dropped with a reason: `below_gate`, `profile_small_pool`, `unscored_volume`), and whether `gate_fallback` fired |
| `autocut` | Whether it fired, where it cut, and what it dropped |
| `order` | The final order before the count cut, and the refs the count cut dropped |
| `reservation` | Rows admitted by a held seat (block reach, time cue) |
| `stages` | One entry per stage of the loop: what the stage searched and why the next one was started |
| `suspected` | Failures the loop suspected while it ran, with the stage and the action taken |
| `coverage` | Which parts of the question the returned records hold: the parts (span and kind), the refs that hold each, and the parts none holds (§1.5) |
| `timing_ms` | Time per stage |

### 1.3 Suspected and confirmed failure codes

The [failure taxonomy](RELIABLE_RECALL_2_6.md#8-recall-quality-engineering)
is used twice, and the two uses are kept apart:

- **Suspected**, inside a recall. The loop cannot know whether it failed: it
  has no answer to compare against. It can only see symptoms, and it records
  what it suspected (for example, `CANDIDATE_MISS` when the cue's period held
  no candidate) and what it did about it.
- **Confirmed**, after the fact, by a tool outside the server that compares a
  trace with the known answer: `CANDIDATE_MISS` when the answer's record is in
  no arm, `FILTER_DROP` when the gate or autocut removed it, `RANKING_MISS`
  when it was admitted but ranked below the count cut (or reached by the block
  arm but ranked below the seats held for it), and the evidence and
  reader codes when it was returned but not used. The tool is
  `benchmarks/recall_trace_confirm.py`.

Keeping the two apart makes the loop's own judgement measurable: how often
did what it suspected match what was confirmed? The history of confirmed
failures never changes the server's behaviour automatically. A change of
policy is a reviewed change with a new policy version.

### 1.4 What makes the trace ready

The trace is an instrument. Its claim is that a failed recall can be
attributed to a stage from the record alone, and it is checked before anything
relies on it:

1. **It names deliberate defects correctly.** Disabling one arm must produce
   `CANDIDATE_MISS`, forcing the gate high must produce `FILTER_DROP`, and
   enabling the episode penalty on a store with an episode per session must
   raise `RANKING_MISS`, the effect the penalty was found to have by hand.
2. **It reproduces a manual attribution.** The analysis that located the
   episode penalty's effect was done with ad hoc scripts. The trace and the
   confirmation tool must reach the same attribution from the record.
3. **It changes nothing when not requested.** The behaviour golden pins this.
   The cost of a requested trace is measured, with a target of no more than a
   fifth of the recall's own time.

### 1.5 The coverage ledger

A traced `recall` or `reconstruct` also records which parts of the question
the returned records hold. It is recorded only: it is computed after the
answer is final, and nothing in the recall reads it. It is the instrument for
a later stage of the loop, one that would fetch what the answer does not yet
cover. Counted over real traffic, it says how often a question has a part that
no returned record holds.

- **Parts** are the question's words, cut by script with no dependency: a run
  of two or more kanji, a run of two or more katakana, or an ASCII identifier
  of two or more characters (names, version numbers such as `2.5.5a1`,
  `bug-218`, `#354`, paths). Hiragana runs are particles and endings and are
  not parts. The question is normalized first (NFKC, then lower case), and a
  repeated part counts once. At most 32 parts are kept.
- **Coverage** is a substring match of the part in each record's full stored
  text, not the preview the response carries. For `recall` the records are
  the returned rows; for `reconstruct`, the records the returned items cite.
- **No text.** A part is recorded as its span in the normalized question and
  its kind; a record is its ref. A caller holding the question recovers a part
  as `normalize(question)[start:end]`.

| Field | Content |
| --- | --- |
| `normalization` | `nfkc-lower` |
| `parts` | `[{span: [start, end], kind}]`, kind one of `kanji`, `katakana`, `identifier`, in order of first appearance |
| `covered_by` | Per part, the refs whose text holds it, in the order the response lists the records |
| `uncovered` | The indices of the parts no record holds |
| `records` | How many records were read |
| `parts_omitted` | Present when the question had more than 32 parts: how many were left out |

When the ledger cannot be built, `coverage` is `{"error": <exception type>}`
and the call answers as it would have. Its cost is `timing_ms.coverage`.

What it does not say. An uncovered part is not proof that the answer is
missing: a record can state the same fact in other words, and in English every
word of two or more letters is a part, common words included. Measured on a
private question set, words taken this way were found in the returned records
for 82% of the parts, on average, of questions whose evidence was returned.
Rare character trigrams, the first design, reached 8%, because in Japanese
they are mostly fragments across word boundaries. The same measurement found
that uncovered parts do not tell a question whose answer is absent from one
whose answer is present, so the ledger is not a signal that an answer does
not exist.

## 2. The loop's basic form

### 2.1 The time cue

```text
time_cue = {
  "after": "2026-08-01", "before": "2026-08-31"      # absolute; either end may be omitted
    or
  "ago": {"unit": "days" | "weeks" | "months", "value": 3} | "long_ago",   # relative to now
  "confidence": "sure" | "likely" | "vague"                             # required
}
```

The argument is named `time_cue` because `reconstruct` already uses "cues" for
the entities of the associative layer. Confidence is a three-valued word, not
a number: a number from 0 to 1 would be a weight under another name, and no
two agents would calibrate it the same way.

The cue becomes a period. `sure` uses it as given, `likely` widens it on both
sides by half its length, and `vague` by its whole length. These margins are
part of the policy version.

The period is this version's
[Dynamic Retrieval Envelope](RELIABLE_RECALL_2_6.md#1-deliberative-recall-the-recall-process):
the part of the store the cue arm searches, and the thing the one revision of
§2.5 widens.

### 2.2 The cue arm

The server runs one more retrieval arm restricted to the period: vector and
keyword search over the records whose timestamp falls inside it, up to the
recall depth. Rows outside the period are not removed, down-weighted or
re-scored. A cue is a prior, never a filter.

### 2.3 How a cue moves a row

The cue never touches a fused score, the quality gate or autocut. After they
and the count have decided which rows are returned, a row among them that the
cue arm also found is moved up. Because the move comes after the count, it
cannot push a row out of the answer (§2.9). The move is bounded in positions,
not in score:

```text
key(row) = p − L × 61 / (61 + c)        sorted ascending
```

- `p` is the row's position in the returned order.
- `c` is its rank on the cue arm.
- `L` is set by confidence: `sure` 3, `likely` 2, `vague` 1.

The bonus is at most `L`, so every row that stood more than `L` places ahead
still stands ahead. **No row moves up more than `L` places**, however many
rows the cue lifts at once.

The shape comes from two derivations, both checked against the fusion code:

- **Adding the cue arm as another vote would make the period a first sort
  key.** Under `rrf` (k = 60) a row with one vote at rank `r` plus a cue vote
  at rank `c` beats every row with a single vote, including a first-place one,
  whenever `r × c < 61² = 3,721`. With the depth at 50 that holds for every
  pair. The worst case moves a row from last to first.
- **Adding the cue as another `rsf` channel would change every other row's
  score.** Every row outside the period is scaled by `n/(n+1)`, so rows near
  the calibrated threshold would start failing the gate.

A weight small enough to keep score gaps cannot bound positions either: tied
scores exist, and any positive bonus breaks a tie.

An age weight applied to the fused score was measured on a real long-term
store and lost to no weight at every rate tried. The reason is the same
flatness: under `rrf`, first and thirtieth place differ by a factor of 1.475,
less than the span of the weight. That result is why v0 moves rows in rank
space.

### 2.4 The reserved seat

A record the cue arm found that the answer does not hold is out of the
bounded move's reach. Seats are held for such records, filled in cue-arm
order, as the [block reservation](BLOCK_REACH_DESIGN.md) holds seats for
records only the block arm reached. The seats displace nothing, and each row
says it came from the cue (`match_reason.signal` = `cue`,
`admission` = `reservation`). Since `cued-v0.3` (§2.11) there are as many
seats as `L` for the confidence searched, and a seat may hold a record the
count cut; until then there was one seat, for a record no other arm reached.

### 2.5 One revision

If the cue arm finds nothing in the period, the loop suspects
`CANDIDATE_MISS`: the cue points at the wrong period. It widens the period by
one confidence step (`sure` to the `likely` margin, `likely` to the `vague`
margin, `vague` to no period) and runs **only the cue arm** again. The other
arms' results are reused, as the recall process requires. There are at most
two stages, and a time limit applies. A stop at a limit is recorded in the
trace.

### 2.6 Invariants

- Without `time_cue`, a recall is identical to today's, pinned by the golden.
- With `time_cue`, the set of rows that pass the quality gate is identical to
  the set without it, and so are the rows the count returns. The cue reorders
  those rows and adds at most `L` reserved rows (one before `cued-v0.3`).
- No row moves up more than `L` places.
- Isolation (`agent_id`, `project_id`, `channel`) is never widened: it is the
  space the search happens in, not a cue.

### 2.7 As implemented

The points the sections above leave open were settled this way. All of them
belong to the policy version `cued-v0`.

- **Relative periods.** `{"unit": u, "value": n}` is the period one unit long,
  centred `n` units before now, and ending no later than now. A month is 30
  days. `long_ago` is the oldest third of the time span the scope holds. An
  open end of an absolute cue is closed by the scope's oldest record or by now.
  A date names the whole day, so `before: "2026-08-31"` includes the 31st.
- **The cue arm** searches memories and, since `cued-v0.2` (§2.10), episodes.
  An episode's time is its start time, else the time it was recorded, as
  everywhere else. Its vector half reuses the query vector the ordinary vector
  arm already embedded, so a cue costs no second embedding; where no local
  vector exists, the arm is keyword only. All the lists are merged by
  reciprocal rank into one. With an empty query, the arm returns the period's
  newest records. With a source filter, episodes (which carry no per-user
  source) are searched only when a channel also scopes the recall, as in the
  ordinary arms.
- **Ties.** A tie between a row the cue found and one it did not goes to the
  found row; any other tie keeps the original order. So the row the cue arm
  ranks first rises exactly `L` places when it stands that far down, and rows
  it ranks lower rise less (at cue rank 60, half of `L`).
- **After a revision**, `L` is that of the confidence step actually searched.
  A `vague` cue whose period holds nothing stops, since there is no wider
  period.
- **The time limit** for the revision is `CPERSONA_RECALL_CUE_TIME_LIMIT_MS`
  (default 1000), measured from the start of the recall.
- **The response** carries `time_cue`: the policy, the period searched last,
  the confidence step used, whether the loop revised, how many rows moved and
  how many seats were used. A row the cue arm ranked carries
  `match_reason.cue_rank`. A cue that cannot be read is refused with `ok:
  false` and an `error` naming the part, never ignored.
- `reconstruct` accepts the same `time_cue` and applies it to the recall it
  reads its candidates from.

### 2.8 A cue for today is not used (`cued-v0.1`)

A cue whose own period, before any confidence margin, starts no earlier than
24 hours before now points only at today or at the future, and the recall does
not use it. The rows are exactly those of a recall without a cue. The
response's `time_cue` carries `ignored: "recent_only"` and the period, and the
trace records `cue_ignored`. The policy version is `cued-v0.1`; `cued-v0` is
the same policy without this rule.

Why: the first measurement of the loop gave each question the cue a separate
model extracted from the question text and the date the question was asked.
Of 60 cues, 36 named the question date itself although the question named no
time, and the cue period held the evidence for only 18 of 57 questions. A
caller that fills the cue with today's date is therefore the observed way a
cue goes wrong. The rule loses a correct cue only when the answer was stored
within the last day, and those records are the newest in the store anyway.
The tool description asks callers to pass a cue only when the request itself
names a time.

### 2.9 The move comes after the count (`cued-v0.1`)

In `cued-v0` the move ran before the count cut the order to `limit`. A row
just below the cut could then rise into the answer and push the last row out,
although the tool description said the cue never removed a row. Measured on a
real long-term store at a count of ten, that happened on 8 of 60 cued
questions with the extracted cues, and on 2 of 60 with deliberately wrong
ones. `cued-v0.1` cuts first and moves rows only among those returned, so the
returned rows are exactly those of a recall without the cue, reordered, plus
at most the one seat. The cost is that the move can no longer bring a row
from just below the cut into view; only the seat adds a row.

### 2.10 The cue arm searches episodes (`cued-v0.2`)

Until `cued-v0.1` the cue arm searched memories only. Measured on a real
long-term store with 181 questions whose cue period held the evidence in 93%
of cases, that split the effect by the kind of evidence: where it was a
memory, the evidence rose on 35 questions and fell on 3; where it was an
episode, it rose on none and fell on 12. The cue arm could not find the
episode, so it lifted the period's other memories past it. `cued-v0.2`
searches episodes in the period with the same vector and keyword halves.

Measured afterwards on fresh questions — the LongMemEval questions whose text
places what they ask about in time, a pre-registered rule, 69 questions —
`cued-v0.2` did not meet the rule (8 up, 5 down, one-sided p = 0.26;
`benchmarks/measurements/results-longmemeval-time-cue.md`).
The cues were right: the period held an evidence session on 64 of the 69.

### 2.11 Seats for rows the count cut, and a cue arm of its own depth (`cued-v0.3`)

Why `cued-v0.2` did little on those questions, read from the recall traces
after the verdict: of 186 evidence sessions, 47 were lost — 19 admitted by the
gate and cut by the count, 28 reached by no arm — and 34 of the lost ones lay
inside the cue's period. The cue arm reached only 3 of them, for two reasons
the policy itself set:

- The cue arm searched only as deep as the count. When the period covers most
  of what the scope holds, it then ranks what the ordinary arms already
  returned.
- A row the count cut was reached by an ordinary arm, so it was not eligible
  for the seat, and the move, which comes after the cut (§2.9), cannot bring it
  back either.

`cued-v0.3` changes both, and nothing else:

- **The cue arm searches to its own depth**, 50 records per half, whatever
  the count. Changing the count alone does not change its candidates.
- **The seats**: as many as `L` for the confidence searched (`sure` 3,
  `likely` 2, `vague` 1). A seat takes the cue arm's best record that the
  answer does not hold and that the quality gate and autocut did not refuse:
  one no ordinary arm reached, or one they admitted that the count cut. A row
  the gate or autocut refused still cannot come back this way.

The bounds of §2.6 stay structural: the rows that pass the gate, and the rows
the count returns, are those of a recall without the cue; the cue reorders them
and adds at most `L` rows. A recall with a cue can therefore return up to three
rows more than `limit`. Replayed on the same questions, the change returned all
evidence on 43 questions instead of 39, but so did adding the same number of
uncued rows on 40, so part of the gain is the extra places themselves. Whether
the cue's places carry more than that was measured on questions that depend on
time, under a rule that requires the cue to beat both no cue and the same
number of uncued rows: on the LMEB TMD task (1,167 questions whose text states
a time), it did. Mean NDCG over the returned rows rose from 0.189 to 0.281
(+49%) with the cue and to 0.204 (+8%) with as many uncued rows; the evidence
rose on 1,029 questions and fell on none (`benchmarks/measurements/results-tmd-time-cue.md`).

## 3. What v0 claims

v0 is a **capability**: a caller can say when, and the answer reflects it
within stated bounds. It does not yet claim to improve answer accuracy.

A precision claim needs enough questions that carry a cue. Only questions
whose text points at a time can carry one, and a few dozen cued questions give
little power to detect a moderate effect. The claim therefore waits for a
question set with at least sixty cued questions. The first such measurement
gave `cued-v0.2` no claim (§2.10). The second gave `cued-v0.3` one, at the
retrieval stage and on questions that depend on time (§2.11): a correct stated
time moves the evidence up beyond what the same number of extra rows gives.
It is not a claim about answer accuracy.

The harm a wrong cue can do is bounded by construction (`L` places and `L`
seats) rather than by a statistical test. A test showing that a wrong cue costs
under three points would need more than a thousand questions.

When the precision measurement runs, cues are extracted from the question
text and its date alone, by a fixed prompt, using a model from a different
family than the one that wrote the questions. A cue must not be derived from
the answer.

## 4. After v0

- **Cue propagation**: a strong hit's period, episode, project or declared
  relations become the next stage's cue.
- **Gate relief**: when many candidates sit just below the gate
  (`FILTER_DROP` suspected), the gate is lowered one bounded step.
- **Stale conflicts**: when a newer record contradicts an older one about the
  same thing, both are returned with their roles.
- **Richer stopping** conditions over more than two stages.
- **Trace storage** for investigating live recalls after the fact.
