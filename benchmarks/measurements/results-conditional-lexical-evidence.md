# Does the lexical arm carry evidence the dense arm does not already have?

Measured 2026-09-10 against the pre-registration in
[`prereg-conditional-lexical-evidence.md`](prereg-conditional-lexical-evidence.md),
which fixed the question, the statistic, the decision rule and the qualification
checks before the dump existed.

The quantity is \(J(v,l)=\log f_1(l\mid v)-\log f_0(l\mid v)\): what the lexical
arm adds *given the dense score*. If it is identically zero the fused order is
the dense order, the conditional-evidence fusion mode has nothing to recover, and
the measured constant of the design is the answer.

**It is not zero.** On both endpoint models, on a majority of the adequately
powered tasks, a gold row outscores a non-gold row of the same query at the same
dense score more often than chance — and it is present, sometimes largest, on the
tasks where the shipped fusion *loses*.

## What was run

`benchmarks/labelled_evidence_dump.py` on eight tasks × three embedding models
(commit recorded in each `<task>.meta.json`), the same 200-query-per-subtask
population the frozen-stage replay uses; 6,799 queries with gold and 1.52 M
dumped rows across the three arms. Analysis and qualification: `conditional_lexical_evidence.py`.

```bash
bash ~/lmeb/evidence_dump.sh            # three models, one at a time
python benchmarks/measurements/conditional_lexical_evidence.py --selftest
python benchmarks/measurements/conditional_lexical_evidence.py \
    --dump_dir ~/lmeb/evidence_bgem3 --replay_dir ~/lmeb/replay_d1d2_bgem3
```

All four qualification checks pass: a synthetic redundancy world is rejected at
0.060 against a 0.10 refutation line, a known 0.603 effect is recovered at 0.592,
the closed-form null moments match real permutations to within 3.4 %, and
abstaining strata reach neither numerator nor denominator. The lexical sign
convention was confirmed against the returned order on every query of every task
(0 disagreements).

## The registered statistic was too coarse, and its own control said so

The pre-registration stratified rows by **(query, dense-rank block)** and rejected
the rival hypothesis — that the statistic is seeing residual dense signal inside a
block — by narrowing the blocks. Amendment 1 added a *measurement* of that
leakage: the identical statistic with the lexical score swapped for the dense
score. It changed the reading.

jina-v5-nano, blocks of consecutive dense ranks (AUC, control):

| Task | w=30 | w=10 | w=5 | w=2 |
|---|---|---|---|---|
| EPBench | 0.884 (0.806) | 0.867 (0.728) | 0.855 (0.656) | 0.846 (0.565) |
| LMEB_SciFact | 0.853 (0.934) | 0.721 (0.820) | 0.672 (0.745) | 0.596 (0.696) |
| ReMe | 0.825 (0.903) | 0.733 (0.816) | 0.676 (0.747) | 0.628 (0.628) |
| QASPER | 0.672 (0.738) | 0.615 (0.638) | 0.595 (0.590) | 0.552 (0.574) |

A rank block does not fix the dense score. At the head of a cosine ranking
adjacent ranks are far apart in value, so even a two-rank block still carries most
of the difference it was meant to remove — the control sits at 0.57–0.70, as large
as or larger than the effect it competes with. On the registered criterion only
EPBench clears the bar, and the rest are **undecided rather than refuted**:
comparing the lexical AUC with the dense AUC under-credits the lexical arm, since
two equal AUCs on partly independent signals still mean the lexical score adds
information.

**The fix is to stratify on the dense score itself.** Blocks of width \(\delta\)
in cosine bound the within-block dense difference directly, and the control
reports when it has actually gone: it reaches one half exactly when the block
stops carrying dense signal. At \(\delta=0.01\) it does.

## Result

Stratified AUC at \(\delta=0.01\), with the leakage control in brackets, beside
the per-task fusion delta the replay already measured:

| Task | MiniLM (weakest) | jina-v5-nano | bge-m3 (strongest) | fusion Δ (min/jina/bge) |
|---|---|---|---|---|
| EPBench | **0.922** (0.51) | **0.925** (0.51) | **0.871** (0.54) | +25.58 / +9.69 / +2.66 |
| Gorilla | **0.684** (0.51) | **0.559** (0.42) | **0.647** (0.49) | +20.73 / +5.22 / −0.15 |
| TMD | **0.612** (0.49) | **0.558** (0.49) | **0.572** (0.49) | +4.16 / −6.44 / −4.92 |
| QASPER | **0.614** (0.58) | 0.544 (0.46) | 0.501 (0.46) | +4.38 / +2.15 / −4.14 |
| LMEB_SciFact | **0.610** (0.51) | 0.567 (0.56) | **0.580** (0.52) | +1.33 / −5.37 / −2.17 |
| ReMe | **0.588** (0.48) | 0.533 (0.52) | **0.579** (0.49) | −1.28 / −4.30 / −2.42 |
| *MLDR* | *0.812* (0.44) | *0.750* (0.50) | *0.769* (0.62) | +6.89 / +0.15 / −1.53 |
| *ESGReports* | *0.535* (0.50) | *0.488* (0.47) | *0.509* (0.61) | +10.19 / −7.92 / +0.62 |

Bold marks a bootstrap lower bound above 0.5. *MLDR* and *ESGReports* were
**declared under-powered before the run** (100 and 36 gold-bearing queries against
a 0.589 and 0.649 detection floor) and are excluded from the decision whatever
they show; at the finest caliper MLDR has 8–16 usable pairs and at
\(\delta=0.002\) it correctly abstains with none.

**The registered decision rule is met.** On the six adequately powered tasks the
bootstrap lower bound exceeds 0.5 on 6/6 for MiniLM and 5/6 for bge-m3 — a
majority on both endpoint models. The rival hypothesis is rejected by
measurement, not argument: the leakage control sits at 0.49–0.52 wherever the
effect is claimed, so the dense score is genuinely pinned. Where the control is
*not* at one half (QASPER/MiniLM 0.58, SciFact/jina 0.56, and both under-powered
tasks) the claim is weakened accordingly and is not counted as clean.

The largest samples make the smallest claims most solid: TMD rests on 162 k–260 k
matched pairs over 14 k–18 k strata, so its modest 0.558–0.612 is a small effect
measured precisely, not a noisy one.

### How large, in the units the mode would use

The spread of \(\widehat J\) across lexical bins at a fixed dense bin — the
reordering force available at an unchanged dense score — counting only cells with
at least 30 gold and 30 null rows:

| Task | MiniLM | jina | bge-m3 |
|---|---:|---:|---:|
| EPBench | 5.29 | 4.64 | 4.01 |
| TMD | 5.32 | 5.34 | 5.35 |
| ReMe | 1.26 | 1.20 | 1.44 |
| LMEB_SciFact | 0.99 | 0.36 | 0.31 |
| Gorilla, QASPER, MLDR, ESGReports | — | — | — |

A dash is an abstention, not a zero: no cell on those tasks reached the count
floor, which is the tail-sample problem the identifiability note predicted —
relevant rows are a small fraction of pairs and the signal lives where the counts
are thinnest. Reporting them as zero would have been the error the floor exists
to prevent.

On ReMe under bge-m3, at the top dense bin, holding the top lexical rank is worth
\(J=+0.80\) while ranking 4th–10th lexically is worth \(-0.63\) and carrying no
lexical vote at all is worth \(-1.00\): about 1.4 nats of separation available
without the dense score moving at all.

## The finding that contradicts the design's expectation

The design page expected the losing tasks to be the redundant ones — that fusion
loses where the lexical arm has nothing conditional to say. The pre-registration
fixed the refutation: *refuted if the losing tasks show an effect as large as the
winning ones.*

**Not as stated, and the corrected numbers say something narrower.** The losing
tasks are **not redundant** — every one of them carries measurable conditional
evidence — but their effect is smaller than the largest winner's, so "as large as
the winning ones" is not met against EPBench.

TMD is the clearest case: the shipped fusion loses on two of three models
(−6.44, −4.92) and the evidence is positive on **all three** (0.612, 0.558,
0.572), on the tightest intervals in the table and with the control at 0.49.
ReMe loses on all three models and is positive on two of them, the mid model's
interval covering 0.5. SciFact loses on two and is positive on two.

So the shipped fusion is not losing because the lexical arm is uninformative
there. It is losing while the information is present — which locates the fault in
**how reciprocal rank fusion uses the lexical arm**, not in whether the arm has
anything to say. What the corrected numbers withdraw is the stronger claim that
the losing tasks carry the *largest* evidence: they carry real but middling
evidence, and the largest sits on a task fusion already wins.

### The model dependence, measured in evidence rather than in NDCG

\(\widehat A\) falls from the weakest model to the strongest where it is largest
— EPBench 0.922 → 0.925 → 0.871, QASPER 0.614 → 0.544 → 0.501 — which is the
model dependence this line exists to solve, now visible in conditional-evidence
units instead of only in retrieval scores. It is **not** monotone everywhere
(TMD 0.612 → 0.558 → 0.572, ReMe 0.588 → 0.533 → 0.579), so "a stronger encoder
absorbs more of the lexical signal" is a tendency in this data rather than a law.
What is uniform is that it does not vanish for the strongest model: bge-m3 still
shows 0.871 on EPBench, 0.647 on Gorilla and 0.579 on ReMe.

## What this does not settle

- **It does not produce a deployment estimator.** The null here is
  unjudged-as-irrelevant on a labelled benchmark, and that is unavailable in a
  deployment. Choosing between an independently justified null population and a
  stated structural assumption remains open, and this measurement is the reason
  to spend effort on it rather than the answer to it.
- **The labels bias the answer downward.** Unjudged rows that are in fact relevant
  enter \(f_0\) and shrink \(|J|\), so every number above is a lower bound on the
  effect and QASPER/bge-m3's flat 0.501 is the weakest kind of null.
- **No weight was fitted and no retrieval score moved.** Whether the mode
  *recovers* this evidence is the separate measurement against the dense-only
  order that the design's success condition names.
- **Two tasks were never in the decision.** MLDR and ESGReports are reported for
  completeness and were excluded in advance.

## Correction, 2026-09-10: a query is (subtask, id), not id

The first version of this page keyed a query by its id alone. Three of the eight
tasks number their queries **per subtask**, so `query_1` names a different
question against a different corpus in each of them — one id appears in
fifty-four subtasks on the largest task. Keying on the bare id merged them into
one stratum, and the statistic then compared a gold row of one corpus with
non-gold rows of another. That is not the comparison this page measures.

Found by a check that had nothing to do with it: the sampling weights of the
row-keyed dump must sum to the eligible universe the dump recorded
independently, and they were out by a factor of twenty. The weights were right;
the key was wrong. A downstream number would never have revealed it, because a
merged stratum still produces a plausible AUC.

**What moved.** The five single-subtask tasks are unchanged to three decimals,
which is what confirms the fix touched only what it should:

| Task | before → after (MiniLM / jina / bge-m3) |
|---|---|
| EPBench | 0.913 → **0.922** / 0.899 → **0.925** / 0.834 → **0.871** |
| ReMe | 0.654 → **0.588** / 0.597 → **0.533** / 0.659 → **0.579** |
| TMD | 0.578 → **0.612** / 0.537 → **0.558** / 0.523 → **0.572** |
| Gorilla, QASPER, LMEB_SciFact, MLDR, ESGReports | unchanged |

**What survives.** The decision rule is still met: the bootstrap lower bound
clears 0.5 on six of six adequately powered tasks for the weakest model and five
of six for the strongest. The leakage control still sits at 0.48–0.54 wherever
the effect is claimed.

**What is withdrawn.** The claim that the losing tasks carry *the largest*
evidence. ReMe fell from among the largest to the middle of the table, and its
mid-model interval now covers 0.5. The narrower finding — that the losing tasks
are not redundant — stands, and TMD rather than ReMe is now its clearest case.

The instrument-qualification checks did not catch this, because every one of them
runs on synthetic rows where each query has a unique id. A check that a real
dump's weights reconstruct its recorded universe is now the thing that would
catch it, and it is the reason this correction exists.
