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
| Build | v2.4.40 (recorded, `trackb_results_v2440_bgem3`) | 2.5.12b3 (`e43ad34`) |
| Run date | 2026-07-10 | this run |
| Overall mean | 57.66 | to be measured |

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

Per task, the delta, and the overall mean delta. **One run per arm**: the
pipeline is deterministic given fixed data, so these differences are exact for
this corpus up to floating-point tie effects — but one corpus is not a sample
over corpora, and no significance will be claimed from a single pair of runs.

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
