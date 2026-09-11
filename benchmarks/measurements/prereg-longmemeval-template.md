# Pre-registration template: a recall change, read by LongMemEval question type

Copy this file to `prereg-longmemeval-<feature>.md`, fill every bracket
before the candidate arm is run, and commit it before the numbers exist. A
section that is still a bracket when the results land voids the claim it
governs. The instrument is `benchmarks/run_longmemeval_by_type.sh` and
`benchmarks/longmemeval_by_type.py`; its first two arms are recorded in
`results-longmemeval-by-type-baseline.md`, which is where the planning numbers
below come from.

## Question

[What the change is, in one sentence, and which question type it is expected
to move.] The expectation is stated per type because the mean over types hides
which capability moved; a change that lifts one type and drops another is
mixed, not an improvement, and the table will say so.

The types and the recall work each one exercises, as a guide to the claim:

| question type | n | what it asks of recall |
| --- | --- | --- |
| `temporal_reasoning` | 133 | a time prior — the answer's session is dated and the cue is when |
| `knowledge_update` | 78 | the newer of two statements on one subject |
| `multi_session` | 133 | evidence spread over several sessions, reached by cue propagation |
| `single_session_user` | 70 | one session the user stated a fact in |
| `single_session_assistant` | 56 | one session the assistant stated a fact in |
| `single_session_preference` | 30 | one session carrying a preference; thirty queries, so one query is 3.33 points of recall |

**Claimed type(s):** [one or two types]. **Every other type:** must not fall
(the rule below).

## Arms

| arm | checkout | what it is |
| --- | --- | --- |
| reference | [commit] | [the build the change is measured against — normally the last recorded arm on the line] |
| candidate | [commit] | [the change, and nothing else: the diff between the arms is the treatment] |

The harness is pinned too: both arms run the harness at [commit], with
`CPERSONA_REPO` selecting the checkout. The treatment is the build together
with its defaults, as in the version comparison; a default the change moves
is part of what it ships.

## Regimes and metrics (fixed)

- **Primary: `limit10`** — one scene's history as the haystack, limit = 10,
  autocut and the fused gate at their shipped defaults. What a caller of the
  recall tool receives. The claim is made here.
- **Secondary: `full`** — the pooled corpus, full ranking, gates off. The
  Track B regime; reported so the claim can be read against the number the
  regression gate uses, not as a second chance to claim.
- **Metric:** NDCG@10 per question type, the per-query paired difference,
  candidate minus reference. Recall@5 and Recall@10 are reported beside it
  and are not part of the rule. The macro mean is reported and no claim is
  made on it.

## Planning numbers: what this instrument can resolve

The per-query difference between two builds is sparse — on the baseline pair,
between 2 and 54 of a type's queries moved at all — and where it moves it
moves by tens of points, so the per-type standard deviation is large and the
resolution is coarse. At α = 0.05 two-sided and 80% power, a paired *t* over
n queries detects a mean difference of `factor × sd`, with the factor from the
noncentral *t* (0.6264 at n = 22, reproduced by
`longmemeval_by_type_resolution.py --factor 22`):

| question type | n | factor | sd, baseline pair (`full` / `limit10`) | MDE in points (`full` / `limit10`) |
| --- | --- | --- | --- | --- |
| `knowledge_update` | 78 | 0.3212 | 5.60 / 8.15 | 1.80 / 2.62 |
| `multi_session` | 133 | 0.2447 | 10.12 / 7.96 | 2.48 / 1.95 |
| `single_session_assistant` | 56 | 0.3811 | 5.72 / 6.91 | 2.18 / 2.63 |
| `single_session_preference` | 30 | 0.5292 | 12.56 / 21.90 | 6.65 / 11.59 |
| `single_session_user` | 70 | 0.3396 | 14.23 / 14.51 | 4.83 / 4.93 |
| `temporal_reasoning` | 133 | 0.2447 | 12.85 / 12.76 | 3.14 / 3.12 |

Two readings of this table, fixed now:

1. The sd belongs to the baseline pair. A change that touches one path can
   have a different dispersion; the MDE for this pre-registration is
   **[the table value for the claimed type, or a value re-derived from a
   stated pair]**, and it is not moved after the run.
2. As in the version comparison, one deterministic execution per arm over
   fixed data *determines* this benchmark's difference; the paired *t* is
   shown for orientation and is conditional on a query-population model this
   benchmark does not supply. The MDE is used as the line below which a
   per-type difference is **not claimed**, not as a test that proves one.

## Controls

1. **Reproduction of the reference.** The reference arm's `full` values must
   equal its recorded values on all six types (the baseline record shows the
   harness does this for both builds it has been pointed at). A miss stops
   the measurement before the candidate is read.
2. **The calibration draw (A/A).** `--auto_calibrate` samples 200 embeddings
   with `ORDER BY RANDOM()`, unseeded, so two runs of one build do not share
   the threshold, and in `limit10` the fused gate reads it. [Either: the
   candidate is run twice and the larger of the two per-type deltas against
   the reference is the one reported; or: the threshold is pinned to the
   reference arm's recorded value with `--min_similarity` in both arms, and
   the change is measured under that pin.] State which, and why; the baseline
   record did neither.
3. **Regime pins.** The reader refuses a `full` dump that was isolated, a
   `limit10` dump that was pooled or ran with a gate off, and any dump whose
   header lacks the pins. A refusal is a broken run, not a data point.
4. **Cache and accelerator.** A cache miss during a task (the log's
   `cache: k/n hits` with k < n), or a `--fast` self-check mismatch, voids
   that task's number for that arm; it is re-run, not interpreted.

## Decision rule (stated before the run)

Let Δ_t be the candidate-minus-reference mean NDCG@10 on type t in `limit10`,
and MDE_t the value fixed above.

- **The change does what it claims** if Δ_c ≥ +MDE_c on the claimed type c
  **and** no other type has Δ_t < −MDE_t.
- **Mixed** if Δ_c ≥ +MDE_c and some other type has Δ_t < −MDE_t: reported as
  mixed, with the type that fell named, and no improvement is claimed.
- **Not resolved** if |Δ_c| < MDE_c: the change may still be real, and this
  instrument cannot say so at this n; that is reported in those words.
- **A regression** if Δ_c < −MDE_c, whatever the other types do.
- The `full` table is published whole beside the `limit10` table. If the sign
  of Δ_c differs between the regimes, both are shown and the disagreement is
  a finding to be explained, not a choice to be made.
- Every table is published whole, including the types that did not move.

## Exploratory (not part of the decision rule)

Registered now so they cannot be added after a result is seen:

- [e.g. the number of queries whose relevant session entered or left the top
  five, per type — the shape of the change, not its size]
- [e.g. the same arms at limit = 20]

## Outputs

`results-longmemeval-<feature>.md` beside this file, with the two reader
tables, the calibrated thresholds, the resolution table re-derived on this
pair, and the harness records under `lme_by_type/<arm>/<regime>/`. The ranking
dumps stay with the run outputs and are named there.
