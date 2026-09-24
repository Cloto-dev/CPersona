# The Recall Process, v0 — design

**Status:** the recall trace (§1) is implemented and not yet in a release;
the loop (§2) is design, not shipped behaviour. It is the first step of
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
  that bounded move and one seat. Without a cue, nothing changes.
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
| `policy` | `{scoring, process}`: the scoring version and the recall-process policy the call ran under (`single-pass-v0` without a cue, `cued-v0` with one) |
| `server_version` | The version that answered |
| `scope` | `agent_id`, `project_id`, `channel`, `source_id` as resolved |
| `request` | `limit`, the recall depth, `deep`, the fusion mode, the confidence ordering, the prior's settings, whether the episode penalty is on, and the `time_cue` when given |
| `config` | Embedding mode and model, scan window, reach and far-list limit, and whether the fused gate and autocut are enabled |
| `arms` | Per retrieval arm (near vector, far vector, episode full text, memory keyword, block, and the cue arm): `{ref, rank, raw}` up to the depth |
| `fusion` | Per candidate: the fused score and each arm's contribution |
| `scoring` | The episode penalty's factor and the prior's weight per row, where applied |
| `gate` | Signal, the calibrated threshold or the heuristic minimum and which one applied (`origin`), the pool size, one decision per candidate (admitted, or dropped with a reason: `below_gate`, `profile_small_pool`, `unscored_volume`), and whether `gate_fallback` fired |
| `autocut` | Whether it fired, where it cut, and what it dropped |
| `order` | The final order before the count cut, and the refs the count cut dropped |
| `reservation` | Rows admitted by a held seat (block reach, time cue) |
| `stages` | One entry per stage of the loop: what the stage searched and why the next one was started |
| `suspected` | Failures the loop suspected while it ran, with the stage and the action taken |
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

### 2.2 The cue arm

The server runs one more retrieval arm restricted to the period: vector and
keyword search over the records whose timestamp falls inside it, up to the
recall depth. Rows outside the period are not removed, down-weighted or
re-scored. A cue is a prior, never a filter.

### 2.3 How a cue moves a row

The cue never touches a fused score, the quality gate or autocut. After they
have decided which rows remain, a row the cue arm also found is moved up. The
move is bounded in positions, not in score:

```text
key(row) = p − L × 61 / (61 + c)        sorted ascending
```

- `p` is the row's position in the admitted order.
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

A record that only the cue arm found is not in the admitted order, so the
bounded move cannot reach it. One seat is held for it, filled in cue-arm
order, as the [block reservation](BLOCK_REACH_DESIGN.md) holds seats for
records only the block arm reached. The seat displaces nothing, and the row
says it came from the cue (`match_reason.signal` = `cue`,
`admission` = `reservation`).

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
  the set without it. The cue changes order and adds at most one reserved row.
- No row moves up more than `L` places.
- Isolation (`agent_id`, `project_id`, `channel`) is never widened: it is the
  space the search happens in, not a cue.

## 3. What v0 claims

v0 is a **capability**: a caller can say when, and the answer reflects it
within stated bounds. It does not yet claim to improve answer accuracy.

A precision claim needs enough questions that carry a cue. Only questions
whose text points at a time can carry one, and a few dozen cued questions give
little power to detect a moderate effect. The claim therefore waits for a
question set with at least sixty cued questions.

The harm a wrong cue can do is bounded by construction (`L` places and one
seat) rather than by a statistical test. A test showing that a wrong cue costs
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
