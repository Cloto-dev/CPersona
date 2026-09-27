# Pre-registration — what the shipping line did to the score since 2.4.40

Registered before the run started. It answers a question that has been asked
in words several times and never measured: **between the oldest Track B record
kept in this repo and the line that ships today, did retrieval quality move,
and which way?**

## Why it cannot be answered from what is already here

The recorded sets are v2.4.40 (2026-07-10) plus two runs kept precisely because
they are invalid. Nothing later exists on this protocol: every measurement made
since is a frozen replay, which caps queries per subtask, switches the stage
identity check off and pins one model's calibration — three good choices for
comparing arms inside one build, and three reasons its absolute numbers cannot
be set beside a Track B record.

Track B records also carry no version field; which build produced a set is
recorded in the directory name and the repo's own README and nowhere in the
data. So the only way to place today's build on that scale is to run it.

## The two arms

| | Baseline | Today |
|---|---|---|
| Build | v2.4.40 (recorded, `trackb_results_v2440_bgem3`) | the development head, `e43ad34` |
| Run date | 2026-07-10 | this run |
| Overall mean | 57.66 | to be measured |

**The second arm is a branch state, not a release.** `e43ad34` reports its
version as `2.5.12b3` because a version string moves only at a release, and the
head sits **27 commits past** the tag of that name. Naming the arm after the
version string would claim a number for what users of that release get; this
measures what the line has become since.

For this benchmark the two happen to coincide, and that is checked rather than
assumed: of those 27 commits exactly one touches the shipped package, and it
adds reporting only — the three deleted lines are an `embedding_model` report
field and a docstring, and the diff touches no symbol on the retrieval path
(`do_recall`, `_recall_*`, `_apply_quality_gate`, `_autocut`, the score fields,
the gate constants). So the number will also describe the released tag on this
path. The arm is still labelled by its commit, because that is what was run.

**Held fixed, and measured to be fixed:** the launcher and every flag it pins
(`--recall_mode rrf --auto_calibrate`, autocut and the fused gate disabled by
the benchmark doctrine), the model (bge-m3), the embedding cache directory, the
task set (22), no cap on queries per subtask, the same eval data and the same
machine.

The embedding cache was verified warm before the run: a probe task reported
`256/256 hits` on every batch, so the encoder does not execute and the
`--device` / `--dtype` settings cannot reach the numbers. That was checked
rather than assumed, because it is the one place where a difference in this
run's invocation could have leaked into the scores.

**The treatment is the build together with its defaults** — scan window,
calibration, vector reach, the admission floor. Those are not pinned, and
pinning them would measure the wrong thing: a user upgrading gets the new
defaults, so the defaults are part of what shipped.

**Known differences that are not the treatment**, recorded now so they are not
explanations invented later: library versions have moved; `--unclamp_limit`
was a real flag in the baseline and is a documented no-op since 2.5.0; and the
baseline ran with `--device mps --dtype float16` against the same warm cache,
which the probe above shows to be inert.

## What will be reported

Per task, the delta, and the overall mean delta.

**Which claim is being made.** Two estimands are available and they are not the
same, so this file names the one in use before the numbers arrive.

1. *This benchmark's difference.* With one deterministic execution per arm over
   fixed data, observing all 22 tasks **determines** the mean difference. There
   is no sampling error to estimate; the only numerical uncertainty is the
   two-decimal storage of each task score, which bounds a difference of two
   rounded scores by 0.01 points, plus floating-point tie sensitivity.
2. *A population of tasks.* "The new build is better on tasks of this kind"
   needs a stated distribution over benchmark packages, and a paired test over
   the 22 task-level differences addresses it only under task transportability,
   task independence, and a workable normal approximation for a bounded score.
   A purposively assembled benchmark supplies no design-based sampling
   distribution for any of that.

**This run reports (1).** A paired *t* over the tasks will also be shown for
orientation, labelled as conditional on the model in (2) and not as evidence
that the model holds. Saying "one run per arm makes significance impossible"
would be wrong; declining to claim it without a task-population model is the
defensible position, and that is the one taken.

**What this design can resolve, fixed in advance.** At n = 22, α = 0.05
two-sided and 80 % power, the minimum detectable mean difference is
0.6264 × the standard deviation of the per-task difference (noncentral *t*,
λ = 2.938171, computed and reproduced independently). At the dispersion the
sibling analysis on this same benchmark shows — sd 1.6 to 3.6 points — that is
**1.00 to 2.26 points**. A version difference need not have that dispersion,
and its own is unknown until this run completes.

Fixed now, so the framing cannot be chosen after the numbers are in:

- **"Improved"** requires the overall mean to rise *and* the tasks that fell to
  be a minority. Otherwise the result is reported as **mixed**, with the tasks
  that fell named.
- If the overall mean falls, that is a **regression** and is reported as one,
  in those words, whatever the per-task story is.
- Either way the per-task table is published whole. A task that moved by more
  than 5 points in either direction gets a sentence about what changed in the
  line that could plausibly account for it — labelled as a hypothesis, not as a
  finding, because this design cannot attribute a delta to a cause.

## Disclosure: what was already seen

One task was run as a protocol smoke test before this file was written:
**DeepPlanning scored 53.89 against the baseline's 56.70, i.e. −2.81.** It is
disclosed because it is not zero and because it points against the comfortable
answer. It is one task of 22, and the smoke test's purpose was to prove the
harness still executes the old regime, which it does.

## The one confound found before running, and how it was closed

The launcher pins autocut and the fused gate off as the benchmark regime. **The
launcher did not exist when the baseline ran** — it was added two days later —
the harness does not set those variables itself, and at v2.4.40 both defaulted
to *on*. The baseline's log records neither. So the baseline's regime for those
two layers is not recoverable from the record, and this arm has them off.

The tempting argument — "those layers only remove rows from a ranked list, so
they can only lower a ranking metric, hence the recorded 57.66 is a lower
bound" — is the launcher's own comment, and it is **only half right**. It holds
for a suffix cut, where surviving rows keep their positions. It fails for a
removal from the middle: everything below moves up, and a relevant row can be
promoted into the top ten. The two layers differ exactly there — autocut keeps
a prefix (`results[:cut_idx]`), while the quality gate compares a *different*
key (confidence, else cosine) from the one the list is ordered by in rrf mode,
so it can drop a row from the middle.

Closed two ways rather than argued:

- **Structurally**: autocut refuses to cut a rank-fusion-ordered list at all —
  it returns the list unchanged as soon as a row carries `_rrf_score`, because
  fusion gaps mark retriever agreement rather than a relevance break. This
  benchmark runs `--recall_mode rrf`, so autocut is inert in both arms
  whatever the environment says.
- **By measurement**: the same build was run on four tasks with both layers
  *enabled*, the regime the baseline's defaults would have given it. The
  results are reported alongside the main arm; where they agree, the baseline's
  unknown regime cannot have moved its numbers either.

## What would invalidate the run

- Any embedding cache miss: it would mean vectors were re-encoded under
  today's settings rather than reused, making the arms differ in their inputs.
- An accel self-check mismatch, or a fallback count above zero.
- Fewer than 22 tasks completing.
- Autocut or the fused gate not actually disabled in the run's own log.

Each is checked in the result rather than assumed.

## What this cannot answer

Whether any *unshipped* work — the reservation, the depth/count separation, the
adaptive fusion line — helps. None of it is on the shipping line, so none of it
is in this measurement. This compares two released regimes and nothing else.

---

## Third arm, registered before it runs: the line with its 2.6 work merged

The two arms above compare released regimes. This one asks the question that
prompted them: *does the work that is not yet merged move the number?*

**The arm.** The development head with the two unmerged runtime branches on top
— the depth/count separation and the reservation with its gate change — merged
with their conflict resolved (both changes apply: the fused arms search to the
depth and fill the reservation; the cascade path keeps the caller's count, as
the depth change left it). 2405 tests pass on the merge, 5 skipped. Same
protocol, same model, same warm cache, same 22 tasks.

**The prediction, fixed here.** This protocol cannot see most of that work, and
the reasons are structural rather than statistical:

- *Depth is inert.* The harness asks for the full ranking, so the count already
  is the corpus, and the depth floor's default of 0 keeps the depth equal to
  the count. There is nothing for a depth to widen.
- *The lexical weight is inert.* Its default of 1.0 is the division the fusion
  has always computed, bit for bit.
- *The reservation is nearly out of reach.* It appends only when the qualified
  rows fall short of ten, measured at 0.21 % of 24,244 queries — and those are
  the queries whose eligible universe is smaller than ten to begin with.
- *The gate change is out of reach on the branch that could have mattered.*
  Dropping the pool-size heuristic for a fused row fires only where no
  calibrated gate applies, which the benchmark regime does supply; but on the
  cosine branch it can only re-admit rows whose similarity sits below that
  heuristic, and every calibration measured in this run puts the admission
  floor **above** it (thresholds 0.5420 to 0.7106, floors 0.27 to 0.36, against
  a heuristic of 0.20). On the rank branch it re-admits rows below a threshold
  applied to the *same* key the list is ordered by, which is a suffix: the rows
  return to the tail, not to the top ten.

**So the registered expectation is a null: the third arm reproduces the second,
task for task.** A difference beyond the two-decimal storage would mean one of
the four readings above is wrong, and the result section will say which.

**What a null here does and does not mean.** It would say this benchmark is
blind to that work — not that the work is worthless. Every one of the four
mechanisms bites where this protocol does not look: at a caller's ten rows
rather than a full ranking, and on pools small enough for an absolute floor to
empty them. Measuring their value needs a protocol built for that, and the
absence of one is the finding to carry forward.

**Cost control.** The prediction is tested on three tasks first — the ones the
second arm has already scored — and the full 22-task arm is run only if those
three disagree with it. A 13-hour run to confirm a null that three tasks can
show is not a measurement, it is a habit.

## Amendment 1 (2026-09-11 09:15 JST, after three tasks, before the fourth was scored)

**What was seen.** The second arm's log shows one embedding-cache batch that did
not fully hit: 128 of 256 texts in the fourth task, LongMemEval, at 07:59.
Every batch in the three tasks scored before it — EPBench, KnowMeBench, LoCoMo —
hit in full. No LongMemEval number exists yet; this amendment is decided on
the miss count alone, which is not an outcome.

**The clause as registered was too broad.** "Any embedding cache miss
invalidates the run" was written as if a miss changed the inputs of the whole
run. It does not. A missed text is re-encoded under today's settings, and the
arms then differ in the inputs of *that text*, which can move only the task
that contains it. Voiding twenty-one clean tasks over 128 texts in one would
discard the measurement this file exists to make.

**The rule, narrowed from run to task.**

- A task with any cache miss is published in the per-task table with its
  missed-text count and a flag, and is **excluded from the paired judgement**:
  the overall mean, the count of tasks that fell, and the five-point sentence.
- A task with zero misses is judged exactly as registered above.
- The overall mean is reported twice — over the clean tasks, and over all 22
  with the flagged tasks included — each labelled as what it is. The verdict
  is read from the clean set.
- A flagged task is not lost: re-running it alone against the now-warm cache
  yields a clean number, and the result section will say whether that was done.

**Unchanged.** The accel self-check, the 22-task completion requirement and the
regime check stand as written. One more disclosure: two other benchmark
processes shared this machine's CPU during the run (a four-task probe of the
autocut/gate regime, stopped at 09:14 after two tasks reproduced the second
arm to the last digit, and the three-task test of the third arm). They share
no database and no output; they cost wall-clock, so this run's latency fields
are not representative and are not compared.

## Amendment 2 (2026-09-11 09:17 JST, after two of the three third-arm tasks, before MLDR and ReMe)

**What was seen.** The three-task test of the third arm did not reproduce the
second arm. EPBench came back 90.11 against 89.94; KnowMeBench reproduced
47.40 to the digit; LoCoMo was still running when this was written. Under the
cost-control rule above, the disagreement calls for the full 22-task arm. This
amendment replaces that with a narrower run, for a reason that was found and
verified before any further number was taken.

**Which reading was wrong, and why.** The fourth reading — that dropping the
pool-size heuristic on a fused row is out of reach because every calibrated
floor sits above a heuristic of 0.20 — took the heuristic for a constant. It
is not: it is `0.5 − 0.3·log(n+1)/log(500)`, and 0.20 is only its value at a
pool of 500 rows and beyond. At 20 rows it is 0.353, above every calibrated
floor in this run (0.27–0.36). The 2.5 gate keys a fused row on its cosine, so
a row the dense arm admitted past the floor was then refused by the heuristic
wherever the pool was small enough — and because the fused order is not the
cosine order, those rows are not a suffix: re-admitting them re-orders the top
ten in both directions. Verified by replaying the four EPBench short-corpus
subtasks on both builds (the replay reproduced the two runs' numbers to the
digit, with no cache miss): every one of the 1,911 re-admitted rows carrying a
cosine sat in `[floor, heuristic)`, none outside; the 369 without a cosine were
lexical-only rows the rank branch had refused for the same reason; the 36
long-corpus subtasks, where the heuristic (0.245) sits below the floor, were
identical row for row.

**Where the protocol can see it.** The heuristic exceeds the calibrated floor
in exactly seven corpus groups of the 22 tasks, read from this model's
recorded per-group floor and pool size: four in EPBench (19–20 rows), two in
ReMe (99 and 110 rows) and one in MLDR (1,535 rows, where the two sit 0.004
apart). Every other group has its floor above the heuristic, so on those the
gate change is the identity and the other three mechanisms remain inert as
registered.

**The prediction, fixed here.** On the third arm, MLDR and ReMe move and no
other task does: the 19 remaining tasks reproduce the second arm to the digit.
A move anywhere else falsifies this reading too. The third arm is therefore
run on MLDR and ReMe only, and its per-task record is read against the second
arm's. If the prediction holds, the 13-hour full arm is not run, because the
same reading that predicts the two movers predicts the nineteen non-movers,
and LoCoMo and KnowMeBench (5,882 and 6,644–11,995 rows) already test the
non-mover half.

**What this does not say.** Whether the re-admission improves retrieval. On
EPBench it moved twelve small-corpus subtasks, eight up and four down, for a
task mean of +0.17; that is one task, and the sign on MLDR and ReMe is not
predicted here.

## Amendment 3 (2026-09-11 09:37 JST, second arm stopped at three tasks and restarted)

**What was seen.** The second arm stalled on its fourth task for two hours
with the encoder busy and the cache not growing. The cause is in the harness,
not the server: sentence-transformers 5.x hands every model a default prompts
dict of `{"query": "", "document": ""}`, and the harness (since it learned to
drive prompted models) treated the presence of a `document` prompt as the
model being prompted. The empty prompt leaves the vector unchanged — bare and
prompt-tagged vectors for the same text agree to a cosine of 0.999997 or
better — but the prompt name is part of the cache key, so the run looked up
`document`-tagged keys, found the baseline's bare-keyed vectors invisible, and
began re-encoding six large corpora (about 1.87 million texts) on the CPU at
roughly 25 texts a minute. The three tasks it did score hit tagged keys that an
earlier replay had written under today's settings.

**The fix, applied to every arm.** The harness now treats a model as prompted
only when its prompt has text. bge-m3 is promptless again, every lookup is a
bare key, and every corpus in the suite is already there from the baseline's
own run (0 of 2.37 million texts missing, checked directly against the cache).
The change touches no server code; it is the same harness for all arms.

**What is re-run and what is kept.** The second arm restarts from the first
task on the fixed harness, with the baseline's `--fast` acceleration and its
self-check, so that its inputs are the baseline's vectors rather than
re-encodes. The three scored tasks are re-taken with the rest. The third arm's
seven tasks are kept as recorded: their vectors were the tagged re-encodes,
which agree with the bare ones to within float noise, and the reading they
established does not depend on that difference; the results file says so.
