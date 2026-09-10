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
