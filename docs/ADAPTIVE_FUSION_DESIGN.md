# Adaptive Fusion — design

Status: design for the 2.6 line, not behaviour. Everything below ships
default-off behind a mode switch; the shipped fusion is untouched until the
success condition of the
[2.6 design page](RELIABLE_RECALL_2_6.md#5-adaptive-fusion) is met on the
measurement this document pre-registers. The facts this design rests on are in
two research notes — the
[frozen-stage replay](research/frozen-stage-replay-2026-09.md), which located
the benchmark losses stage by stage, and the
[identifiability note](research/adaptive-fusion-identifiability.md), which
says what a fusion rule must know to avoid them. This page decides; it does not
re-derive.

## 0. What the measurements changed

The line was framed as "adaptive calibration": estimate each retrieval arm's
reliability from its null distribution and fuse the arms through those
estimates. Three things are now known that reshape it.

1. **The loss has three causes, and only one needs adaptivity.** On the
   pure-ranking tasks the whole Track B − Track A loss is the fusion step: a
   lexical vote lifts rows the dense arm had already ranked lower past the
   relevant ones. On small candidate universes the absolute admission floor
   empties the top ten, and on small pools the heuristic gate deletes the
   lexical arm. The second and third are structural defects with
   deterministic fixes.
2. **Per-arm null calibration cannot decide the fusion.** No rule whose inputs
   are one row's scores and their per-arm exceedance probabilities can tell
   whether the lexical arm should override the dense arm; two worlds with the
   same nulls and the same observations demand opposite orders. The quantity
   that decides is the lexical evidence *conditional on* the dense score.
3. **A constant recovers most of it on one model class and none of it across
   classes.** Reducing the lexical weight to 0.1 recovers most of the
   pure-ranking loss on the mid and strong models and would cost the weakest
   model most of its gains: on the same frozen candidates SciFact, TMD and
   ESGReports gain under the shipped fusion with the weakest model and lose
   with the other two. No constant serves all three. The adaptive layer's
   target is that model dependence — the reason this line exists — and, on
   top of it, the three to six points of per-query oracle gap.

## 1. Decisions

### D1. A reservation invariant across the whole recall path

For a query whose eligible universe (after mandatory isolation and candidate
filtering) has \(n\) rows, let \(k=\min(10,n)\). The dense arm's top \(k\) rows
are reserved before admission, stay reachable by every later stage, and at
final selection eligible reserved rows are appended — marked as fallback, not
as qualified — until the output has at least \(k\) rows. The invariant is a
cardinality contract over the path, not a change to any stage's scoring: it
preserves candidates and calibrated scores, promises no relevance, and does
not protect a dense row from being demoted by fusion.

*Why.* Admission alone emptied the top ten for 31.8 % of QASPER's queries
(median eligible universe 45 rows) and cost Gorilla 7.1 points on the mid
model and 18.2 on the weakest; the gate removed what lexical rows had refilled. Both are the same
error — an absolute threshold applied to a universe too small for it — and
neither is repaired by any fusion rule.

*Shape.* A `fallback: true` marker on appended rows, additive to the existing
message shape; the count of fallback rows in the response. No new tool.

### D2. The pool-size gate stops being a rank cut on the lexical arm

The heuristic gate compares a lexical-only row's fused score \(1/(K+r+1)\)
with \(3\,m(P)/(K+1)\), which is a cut at a lexical *rank* that depends on the
pool size \(P\) — rank 40 above 500 rows, rank 21 at 196, and no row at all at
30 or fewer. That is not a quality judgement; it is an accident of comparing a
rank-fusion score with a cosine-scale threshold, and it produced EPBench's
entire loss (−10.2, 99.6 % in the four 19–20-row groups). The gate's rank
branch is removed for rows the fusion produced; a row's admission to the output
is decided by the fusion mode's own evidence scale (D3) and by D1. The cosine
branch keeps its role for dense-only orders. Whether the benchmark regime is
redefined to bypass the heuristic gate — which moves the published small-corpus
Track B numbers — is a separate decision, recorded in the benchmark
documentation when taken; this design does not depend on it.

### D3. Fusion: one knob now, one mode later, no fitted weight ever

**Now — a measured constant.** `CPERSONA_RRF_LEXICAL_WEIGHT` (default 1.0,
byte-identical) scales the lexical arms' reciprocal-rank votes. It is a single
global constant, not per model or per task, and its default moves only through
the release ladder after the measurement in section 2. Its purpose is to be
the *non-adaptive control arm* of that measurement, and — if the adaptive mode
fails its success condition — the fallback that still recovers most of the
loss on the stronger models. It cannot serve every model at once; that is the
fixed-weight compromise this line exists to replace, and why it is a control
arm rather than the answer.

**Later — conditional-evidence fusion, default-off.** A new fusion mode
(`CPERSONA_RECALL_MODE=evidence`, name provisional) scores a row by

\[
S^\star(q,d)=\log\frac{h_q(v_d,l_d)}{f_{0q}(v_d,l_d)},
\]

the joint density of its (dense, lexical) score pair over a declared reference
population, divided by the same density under the joint null. This is the
construction of the identifiability note, section 5: it orders rows as the
relevance likelihood ratio does under the stated assumptions, collapses to the
dense order where the lexical arm is redundant, keeps lexical corrections where
they carry evidence, contains reciprocal rank fusion as an identifiable limit,
and fits no per-arm weight — the unknown prevalence of relevant rows cancels
from the order.

*What "evidence" is estimated from.* Both densities come from a **reference
panel** declared per agent scope: a fixed sample of documents and a fixed set
of queries, chosen independently of any retrieval (retained past queries where
they exist, pseudo-queries drawn from stored text otherwise, with the query
encoder role applied). The null joint is the score pair over random
query–document pairs of the panel; the population joint is the score pair over
all pairs. Because the panel does not depend on what a recall returns, the
score is candidate-set independent by construction. The panel is bounded
(caps on documents and queries), fingerprinted like the calibration sidecar
(model, dimension, tokenizer, null definition, seed) so that a model swap or a
scoring change invalidates it, and rebuilt in the background.

*How the densities are represented.* A discretised joint: cosine quantile
bins from the null × lexical bins (absent from the lexical list as its own
bin; present rows by bm25 quantile), add-one smoothing, and a coordinatewise
monotone regularisation of the ratio so that the monotonicity contract holds —
raising either arm's score never lowers the fused score. Absence on the
lexical arm is an observation with its own bin, never a p-value of one; a
dense score below the admission floor is censored, not absent.

*Gate.* The mode's output is compared on its own log-evidence scale (a new
gate signal kind), never against a cosine threshold; the calibrated gate
machinery already carries the signal it was calibrated for.

**Never — a learned arm weight.** A per-model or per-task weight fitted to
labels reintroduces the fixed-weight problem one level up and is outside this
line. If the evidence mode fails its condition, the constant of D3-now is the
answer, not a fitted vector.

### D4. Depth, prior and count stay separate

The mode consumes the candidate depth of the depth/count separation, is
applied before the prior function of the 2.6 page (section 3) and before the
reconstruction window (section 7), and never derives its reference or its
bins from the response count. Changing the count alone must leave every row's
\(S^\star\) unchanged; that is the same invariant the depth work asserts.

## 2. The measurement that decides

Pre-registered before any of D3-later is implemented.

**Instrument.** A row-keyed dump from the frozen-stage replay: for every
candidate row of every query, both raw scores, both null exceedance
probabilities against the panel null, arm presence and censoring, the
relevance label, and its rank under each arm and each fusion rule. The
per-query summaries of the replay note cannot evaluate the rules' reversal
conditions or estimate the conditional evidence; this can.

**Arms.** (a) shipped reciprocal rank fusion; (b) dense-first with the constant
of D3-now at 0.1; (c) the square-root mixture of the first derivation, so that
its symmetry is measured and not only argued; (d) the evidence mode. All four
on the same frozen lists, both endpoint models (the weakest and the strongest
in the benchmark record), all twenty-two tasks, with D1 applied to every arm
so that starvation does not confound the comparison.

**Success.** The condition the 2.6 page fixed: the 22-task mean of
NDCG@10 against the *dense-only order* is above zero on both endpoint models.
Not against the admitted list, not against the gated list.

**Refutation.** The criteria of the identifiability note, section 9, apply
verbatim: any strict pair order that disagrees with the rule's own reversal
inequality refutes the implementation; a reference profile that changes with
retrieval depth refutes candidate-set independence; a monotonicity violation
after regularisation refutes the representation. In addition:

- if arm (b) alone meets the success condition and arm (d) does not exceed it
  by more than the paired uncertainty on either model, the evidence mode is
  not shipped and D3-now's default is moved instead;
- if arm (d) meets the condition only with a panel larger than the declared
  work budget, the mode is not shipped and the panel budget is recorded as the
  reason;
- if the null joint from the panel is anti-conservative on held-out all-null
  queries at the declared levels, the reference is rejected before any
  ranking result is read.

## 3. Invariants

1. **No model is called** on the read path; embedding yes, generation never.
2. **Candidate-set independence.** Same query, row, snapshot, scope and
   reference — same score, whatever else was retrieved.
3. **Determinism.** Same database state, same panel, same query — same order;
   ties broken by a written total order.
4. **Default-off, contract untouched.** The existing `recall` response shape
   is unchanged; new fields (`fusion_mode`, per-row evidence, fallback marker)
   are additive and absent when the mode is off.
5. **Bounded work.** Panel construction and scoring have declared caps; the
   mode refuses to build a panel beyond them and reports that it did.
6. **Reference fingerprinted.** A stale panel is detected the way a stale
   calibration is, and the mode falls back to the shipped fusion, saying so.

## 4. Order of work

1. D1 and D2 — structural, model-independent, measurable on the existing
   replay (S1 and S3 must rise to S0 and S2 on the affected tasks with no
   change elsewhere). They ship first, default-on, through the ordinary ladder,
   because they remove defects rather than add behaviour.
2. D3-now — the knob, default 1.0; ships with D1/D2.
3. The row-keyed dump and the panel builder as benchmark-side instruments.
4. The pre-registered comparison of section 2.
5. D3-later — implemented only if the comparison selects it; the default flip
   of either the knob or the mode only through the ladder.

## 5. What this page does not decide

- The benchmark regime's treatment of the heuristic gate (see D2).
- The panel's query source in deployments with no recorded queries beyond
  pseudo-queries; measured in step 3.
- Interaction with the confidence layer and the prior function; the 2.6 page
  places the prior after fusion and this design keeps that order.
- Any per-query panel; a single panel per scope is the first form.
