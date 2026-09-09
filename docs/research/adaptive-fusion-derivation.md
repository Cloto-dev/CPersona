# Adaptive fusion — a derivation

Status: derivation under stated assumptions, not behaviour. Nothing here is
implemented. The equations were produced for the
[adaptive fusion](../RELIABLE_RECALL_2_6.md#5-adaptive-fusion) section of the
2.6 design page, whose success condition they do not change; the transcribed
facts about the code and the benchmark files were spot-checked against the
tree, and the equations have not yet been re-derived independently. Every
assumption in section 1 names the observation that would refute it, and
section 9 lists the measurements that must come before an implementation.

The one-paragraph result: fusing arms by their **exceedance probability
against each arm's own null** is defensible, and a **mixture likelihood ratio**
over those probabilities gives, without any fitted weight, a closed-form
per-row influence for each arm and an identifiable limit that reproduces
today's reciprocal rank fusion exactly. What the mathematics does *not* give is
the claim that a small null p-value means the arm is reliable, or that a
confident dense arm suppresses lexical errors across a whole query: the first
is an assumption about the relevant class, the second holds for the mixture
rule and fails for Fisher's rule.

## 1. Assumptions, each with its refutation

1. **The measured pipeline must be identified before a loss is attributed to
   fusion.** Two premises in the record needed correction. First, a row does
   not receive three votes: `_recall_rrf` fuses the vector list, the episode
   FTS list and the memory keyword list, but a memory row can only appear in
   the vector and memory-keyword lists, and an episode row only in the vector
   and episode-FTS lists, so each row has two applicable arms. Second,
   `CPERSONA_FUSED_GATE_ENABLED=false` disables the *calibrated* gate only;
   `do_recall` still runs `_apply_quality_gate` with its heuristic threshold,
   so the benchmark regime is not a gate-free ranking experiment.
   *Refuted if* a frozen-candidate replay shows that post-fusion filtering,
   duplicate handling or candidate coverage explains a material part of the
   Track B − Track A difference.
2. **Null samples represent irrelevant query–document scores in the
   deployment scope** — same model, same encoder roles, same corpus snapshot,
   same row type, same isolation scope, and a declared query population.
   Random documents approximate irrelevant ones; random query–document pairs
   can contain positives. *Refuted if* held-out irrelevant pairs show excess
   small-p rates, or calibration moves materially across query language,
   length, corpus group or retrieval eligibility.
3. **Relevance changes the score distribution measurably.** A valid null
   identifies \(f_0\), not \(f_1\); any Bayesian reliability statement needs an
   alternative distribution or a structural stand-in for one. *Refuted if*
   gold/non-gold likelihood ratios do not increase with the fused statistic,
   or extremely small lexical p-values mostly identify distractors.
4. **Bayes ranking is evaluated on a fixed eligible candidate universe.** For
   expected DCG, rank by expected gain; for binary relevance the posterior
   probability suffices. Expected *normalised* DCG is a different quantity.
   The Track B metric uses binary membership in the relevant set.
5. **Candidate-set independence is required; query conditioning is
   permitted.** \(S(q,d;\mathcal C_1)=S(q,d;\mathcal C_2)\) whenever query,
   row, snapshot and scope are identical. A reference distribution may depend
   on \(q\), provided it is not estimated from the returned candidates.
   *Refuted if* changing retrieval depth, adding an unrelated row or
   activating another list changes an existing row's score.
6. **Finite-sample guarantees use independent sampling units.** All-pairs
   products over a document sample reuse vectors; they are not independent
   observations. *Refuted if* an uncertainty calculation uses pair counts as
   sample counts.
7. **Historical cross-model comparisons are hypotheses, not controlled
   interventions.** The MiniLM per-task deltas were measured under an older
   pipeline than the jina and bge-m3 ones. *Refuted if* the QASPER sign flip
   disappears when both models run identical candidates, flags, code and
   labels.

## 2. Scores, nulls and the Bayes-optimal combination

For each applicable arm \(a\) define a higher-is-better score: the cosine for
the dense arm, and the negated FTS5 bm25 for the lexical arms (SQLite returns
lower values for better matches). A short-query `LIKE` fallback is a separate
discrete measurement, not a missing bm25 value.

Let \(F^0_a(t)\) be the null law of the arm's score and define the inclusive
exceedance probability

\[
p_a(q,d)=\Pr_0\{S_a\ge s_a(q,d)\}=1-F^0_a\bigl(s_a(q,d)^-\bigr).
\]

With a finite reference sample of size \(m_a\),

\[
\widehat p_a(s)=\frac{1+\sum_{i=1}^{m_a}\mathbf 1\{S^0_{ai}\ge s\}}{m_a+1}.
\tag{1}
\]

The added one prevents an unsupported zero; it does not repair domain shift or
selection bias, and finite-sample validity needs exchangeability of the test
score with the reference scores.

With class prior \(\pi\) and joint class densities \(f_1,f_0\),

\[
\Lambda(\mathbf s)=\operatorname{logit}\pi+\log\frac{f_1(\mathbf s)}{f_0(\mathbf s)},
\tag{2}
\]

and sorting by expected gain is optimal for expected DCG. If the arms are
conditionally independent in **both** classes,

\[
\Lambda=\operatorname{logit}\pi+\sum_a\log\frac{f^1_a(s_a)}{f^0_a(s_a)}.
\tag{3}
\]

This is a sum of likelihood ratios — not of raw scores and not of p-values.
Two special cases name the classical rules. If \(p_a\mid R=1\sim
\operatorname{Beta}(\alpha_a,1)\),

\[
\Lambda=C+\sum_a(1-\alpha_a)(-\log p_a),
\tag{4}
\]

which is Fisher's statistic when the \(\alpha_a\) are equal. If
\(z_a=\Phi^{-1}(1-p_a)\) is Gaussian with a common covariance \(\Sigma\) and a
shift \(\boldsymbol\delta\) under relevance,

\[
\Lambda=C+\boldsymbol\delta^\top\Sigma^{-1}\mathbf z,
\tag{5}
\]

which is weighted Stouffer. Writing the joint densities as
\(f_y(\mathbf s)=D_y(\mathbf s)\prod_a f^y_a(s_a)\),

\[
\Lambda=C+\sum_a\log\frac{f^1_a}{f^0_a}+\log\frac{D_1(\mathbf s)}{D_0(\mathbf s)}.
\tag{6}
\]

Marginal null calibration says nothing about the last term. Equation (5) also
shows a contract conflict: \(\Sigma^{-1}\boldsymbol\delta\) can have negative
coordinates, so the unconstrained optimum can violate the requirement that
raising an arm's score never lowers the fused score.

## 3. The candidate rule: a mixture likelihood ratio

Define the decreasing, normalised function

\[
B_{\kappa,\beta}(p)=\frac{(\kappa+p)^{-\beta}}{Z_{\kappa,\beta}},\qquad
Z_{\kappa,\beta}=\int_0^1(\kappa+u)^{-\beta}\,du,
\tag{7}
\]

so that \(\mathbb E_0\,B(p_a)=1\) for a uniform null p-value. Let the
relevant class be explained by one latent arm \(A\) with prior \(\pi_a\):

\[
f_1(\mathbf p\mid A=a)=B_{\kappa,\beta}(p_a)\,f_0(\mathbf p).
\tag{8}
\]

Marginalising \(A\),

\[
\frac{f_1(\mathbf p)}{f_0(\mathbf p)}=E(\mathbf p)=\sum_a\pi_a B_{\kappa,\beta}(p_a),
\qquad
\boxed{S(q,d)=\log E(\mathbf p(q,d)).}
\tag{9,10}
\]

Equal \(\pi_a\) over the arms applicable to the row type is an equal prior,
not a learned weight. Equation (8) permits correlated null arms: under the
alternative the other coordinates keep their null conditional law given the
explanatory one. The unverified step is the single-coordinate tilt itself —
multi-arm agreement may carry extra evidence, and lexical distractors may not
follow the tilt at all; gold-conditioned joint distributions decide.

A first experimental choice is \(\kappa=0,\beta=\tfrac12\):

\[
E=\frac1{A}\sum_a\frac1{2\sqrt{p_a}}.
\tag{11}
\]

**Validity survives dependence.** Each \(B(p_a)\) is an e-value (decreasing,
integrates to one), an average of e-values is an e-value, so
\(\mathbb E_0 E\le1\) whatever the dependence between arms, and by Markov

\[
p_{\mathrm{comb}}=\min(1,1/E),\qquad\Pr_0(p_{\mathrm{comb}}\le\alpha)\le\alpha.
\tag{12}
\]

This is a conservative null statement, not a relevance posterior. Rank by
\(S\); truncating \(p_{\mathrm{comb}}\) at one creates ties.

**Emergent influence.** With \(u_a=-\log(\kappa+p_a)\),

\[
\frac{\partial S}{\partial u_a}=\beta\,\omega_a,\qquad
\boxed{\omega_a=\frac{\pi_a(\kappa+p_a)^{-\beta}}{\sum_b\pi_b(\kappa+p_b)^{-\beta}}}
\tag{13}
\]

and under the generative model \(\omega_a=\Pr(A=a\mid\mathbf p,R=1)\). For
two equally weighted arms

\[
\omega_l=\frac1{1+\bigl((\kappa+p_l)/(\kappa+p_v)\bigr)^\beta},
\tag{14}
\]

so with \(\kappa=0\) the lexical influence vanishes as \(p_v/p_l\to0\). Three
limits: the influence is per row, not a weight shared across a query; a lexical
distractor with its own extreme p-value still outranks a dense hit; and the
finite reference caps suppression — with \(p_v\ge1/(m+1)\) and \(\beta=\tfrac12\),
\(\omega_v\le\sqrt{m+1}/(\sqrt{m+1}+1)\).

For Fisher's rule \(\partial S_F/\partial(-\log p_a)=1\): the dense arm can
*contribute* more, but the marginal lexical influence never shrinks. "A
confident dense arm silences lexical votes" is a property of the mixture rule,
not of p-value combination in general.

*Correction (September 2026).* "Silences" describes the influence on one
row's score, not the fused order. The rule is symmetric in the arms: a row
that is extreme on the lexical arm alone outranks a row that is extreme on
the dense arm alone, as the worked pair in the
[identifiability note](adaptive-fusion-identifiability.md#2-the-mixture-rule-is-symmetric-in-the-arms-a-correction)
shows. Read \(\omega_a\) as a per-row derivative, and nothing more.

## 4. When equal-weight RRF harms

Let \(X_a(d)=1/(K+r_a(d))\) if \(d\) occurs in arm \(a\) and \(0\) otherwise
(the implemented vote). For a relevant \(g\) and an irrelevant \(b\) with
\(M_a=X_a(g)-X_a(b)\), if the dense arm orders the pair correctly
(\(M_v>0\)), equal-weight RRF reverses it **if and only if**

\[
\boxed{\sum_{a\ne v}M_a<-M_v,}
\tag{15}
\]

with equality decided by the tie order. For the mixture rule the exact
reversal condition is

\[
\sum_{a\ne v}\pi_a\bigl[B(p_a(b))-B(p_a(g))\bigr]>\pi_v\bigl[B(p_v(g))-B(p_v(b))\bigr].
\tag{16}
\]

No unconditional theorem follows from per-arm separations alone: means, AUCs
or marginal separations do not fix the alignment of the arms' errors, the
near-top margins, candidate censoring or the position relative to the cutoff.
Two systems with identical separations can fuse differently. The exact
pairwise statement is \(\Pr(\sum_a M_a>0)<\Pr(M_v>0)\) (17).

Under a Gaussian approximation of the vote margins,
\(\mathbf M\approx N(\boldsymbol\mu,\Sigma_M)\), with equal margin variances,
correlation \(\rho\) and \(\delta_a=\mu_a/\sigma_a\), two-arm RRF harms
pairwise accuracy exactly when

\[
\boxed{\delta_l<\bigl(\sqrt{2+2\rho}-1\bigr)\delta_v}
\tag{18}
\]

(three equal arms: \(\delta_l+\delta_k<(\sqrt{3+6\rho}-1)\delta_v\), (19)).
Equation (18) matches the two-arms-per-row structure of the implementation.
It explains *how* QASPER's sign can flip — lexical separation above the
threshold for a weak dense arm, below it for a strong one — and how Gorilla
can lose on every model; it does not establish that those separations
occurred, which only a frozen replay can.

Pairwise loss is not NDCG@10 loss. For a judged query compute exactly

\[
\Delta_q=\frac{\sum_d G_d\bigl[w(r_{\mathrm{fusion}}(d))-w(r_v(d))\bigr]}{\operatorname{IDCG}_q},
\qquad w(r)=\frac{\mathbf 1\{r\le10\}}{\log_2(r+1)}.
\tag{20}
\]

## 5. RRF as a limit of the mixture rule

For complete rankings of the same \(N\) documents, identify the reference
tail with the empirical rank tail \(p_a=r_a/N\), and set \(\beta=1\),
\(\kappa=K/N\) in (7). Then

\[
B_{\kappa,1}(p_a)=\frac{N}{\log((K+N)/K)}\cdot\frac1{K+r_a},
\qquad
E(\mathbf p)\propto\sum_a\frac1{K+r_a},
\tag{21}
\]

and since the logarithm is increasing, (10) produces **exactly the RRF
order** in this limit. The path \(\beta(t)=1-t/2\), \(\kappa(t)=(1-t)K/N\)
for \(t\in[0,1]\) connects RRF (\(t=0\)) to the square-root mixture on genuine
null p-values (\(t=1\)). It is an analytic bridge, not a knob to tune on
benchmark outcomes. The identification does not cover truncated lists or
unequal eligible universes: today's RRF gives an absent list entry a zero
vote, and a genuine non-match, an inapplicable arm and a censored score are
three different observations.

**Flat distributions.** Identical raw scores get identical p-values, so the
rule creates no artificial within-arm separation; scores inside the null bulk
get no exceptional evidence; a degenerate finite reference gives \(p=1\)
rather than a full-strength vote. This removes the artificial promotion of
flat or singleton lists that per-query min-max produces (`_minmax_norm` maps
an all-equal list to one). It cannot fix contamination caused by a
misspecified null or by an alternative that mistakes topical unusualness for
relevance.

## 6. Query–document versus document–document nulls

Let normalised query and document vectors have means \(\mu_Q,\mu_D\) and
covariances \(\Sigma_Q,\Sigma_D\). For irrelevant, independent pairs,

\[
\mathbb E(Q^\top D)=\mu_Q^\top\mu_D,\qquad
\mathbb E(D_1^\top D_2)=\|\mu_D\|^2,
\tag{22}
\]

\[
\operatorname{Var}(Q^\top D)=\operatorname{tr}(\Sigma_Q\Sigma_D)+\mu_Q^\top\Sigma_D\mu_Q+\mu_D^\top\Sigma_Q\mu_D,
\qquad
\operatorname{Var}(D_1^\top D_2)=\operatorname{tr}(\Sigma_D^2)+2\mu_D^\top\Sigma_D\mu_D.
\tag{23}
\]

No Gaussian assumption is needed. Under a location–scale approximation a
doc–doc quantile transports to a query–doc one as

\[
t_{QD,\alpha}\approx\mu_{QD}+\frac{\sigma_{QD}}{\sigma_{DD}}\,(t_{DD,\alpha}-\mu_{DD}),
\tag{24}
\]

so a purely multiplicative factor additionally assumes the intercept is
negligible. Today's `RRF_THRESHOLD_FACTOR` of 0.5 is a crude operating-point
transport of this kind; the measured p95 ratios in the
[calibration note](calibration-admission-floor-2026-09.md) already vary from
0.64 to 0.86 across tasks, so no constant can be right for all of them.

**Cold start.** For \(m\) independent reference scores drawn from \(G\), with
probability at least \(1-\delta\),

\[
\sup_s|\widehat G_m(s)-F^0(s)|\le
\sqrt{\frac{\log(2/\delta)}{2m}}+\sup_s|G(s)-F^0(s)|,
\tag{25}
\]

the first term being the DKW–Massart bound (\(m=738\) gives \(0.05\) at
\(\delta=0.05\)) and the second the unavoidable shift between the reference
and the true null.

| Reference | Recommendation | What invalidates it |
| --- | --- | --- |
| Retained real query embeddings | Preferred steady state; freeze snapshots, stratify only on predeclared features | Query drift, scope mismatch, repeated sessions counted as independent |
| Document fragments encoded in the query role | Provisional only if those embeddings already exist outside the no-call path | Unknown fragment-to-query shift, near-duplicate leakage, wrong prompt |
| Doc–doc null with learned transport | Only after held-out quantile-transport validation | Non-affine shift, changed model/prompt/corpus |
| **Current query against a fixed document panel** | Cold-start estimator that needs no new model call: a query-conditional random-document null | Contamination \(\eta_q\) of the panel by relevant documents enters (25) as an extra term |

The recommendation is retained real queries in the steady state and the
query-against-panel estimator at cold start. Using the whole small corpus as
the panel reduces the score to a rank and erases the cross-query absolute
separation that motivated adaptivity; a finite panel also saturates its tail.

## 7. Small corpora, proxy degeneration and starvation

A null-only fusion does not need a temporal positive proxy; keep the proxy out
of the p-value transform. Where a separation threshold is still used, with
\(J(t)=F_0(t)-F_1(t)\) estimated from independent samples of sizes
\(m_0,m_1\), two DKW bounds and a union bound give
\(|\widehat J(t)-J(t)|\le\epsilon_0+\epsilon_1\) uniformly, with
\(\epsilon_y=\sqrt{\log(4/\delta)/(2m_y)}\). A defensible acceptance rule is

\[
\boxed{\widehat J(\widehat t)-\epsilon_0-\epsilon_1\ge J_{\min},}
\tag{26}
\]

with \(J_{\min}=0.2\) as declared policy, not a mathematical boundary. At
\(m_0=m_1=220\) and \(\delta=0.05\) the uncertainty is about \(0.2\), so an
observed \(\widehat J\ge0.4\) is needed to certify \(J\ge0.2\). Thousands of
overlapping pairs are not thousands of observations.

**Starvation.** A fixed-p test cannot guarantee both precision and a
non-empty result: under an all-null query with \(n\) independent uniform
p-values, \(\Pr(\text{no admission})=(1-\alpha)^n\). The policy
\(\alpha(n)=\max\{\alpha_0,1-\eta^{1/n}\}\) (27) bounds that by \(\eta\)
under those assumptions only. The recommended fallback is deterministic:
retain the top \(L_{\mathrm{reserve}}\) dense rows inside the declared
candidate budget, mark rows failing the p criterion as fallback candidates,
keep their calibrated scores, and separate candidate retention from any claim
of relevance. Choose \(L_{\mathrm{reserve}}\) before evaluating; retaining at
least the evaluation depth stops an admission rule from silently removing the
raw dense top ten.

## 8. Invariants an implementation must keep

1. **Monotone.** (1) is non-increasing in the raw score, (7) and (10) are
   non-increasing in \(p\); the composition is non-decreasing in every
   higher-is-better raw score.
2. **No zero tails.** Floor at \(1/(m+1)\); no extrapolation past the observed
   tail without a validated tail model.
3. **Stable arithmetic.** Evaluate (10) as
   \(\operatorname{LSE}_a[\log\pi_a-\beta\log(\kappa+p_a)-\log Z]\); reject
   non-finite raw scores; validate dimensions and norms.
4. **Ties.** Total order \((-S,\,-s_v,\ \text{row-type code},\ \text{row id})\)
   with a fixed sentinel for a missing dense score. Equal raw scores get
   equal p-values; ids break ranking ties, never probabilities.
5. **Fixed arm applicability** from row type and configuration; never divide
   by the number of lists that happened to return rows.
6. **Missing is not non-matching.** Score every applicable arm on each
   candidate in the bounded union, or declare the candidate's score
   incomplete. A row outside a top-\(L\) list has a censored score, not
   \(p=1\); treating it as \(p=1\) and later learning its score would break
   candidate-set independence.
7. **Frozen reference.** Neither the null sample nor its stratum may depend on
   the returned candidates.
8. **Lexical atoms.** Include non-matches in the lexical reference;
   calibrating only returned FTS matches estimates a selected conditional
   law. Short-query `LIKE` fallbacks need discrete calibration; the FTS5
   trigram tokenizer does not match strings shorter than three characters.
9. **Bound the work.** Bound candidate union, reference panel, cross-scoring
   and calibration storage separately. A SQL `LIMIT` bounds returned rows, not
   index traversal; the FTS and `LIKE` paths need an explicit work-budget
   audit before "no scan past declared limits" can be claimed.
10. **No extra model calls.** Fusion consumes stored embeddings and lexical
    scores; pseudo-query embeddings must be supplied beforehand.
11. **Determinism scope.** Freeze seed, sampled ids, calibration version,
    dtype, reduction order and tie policy. Cross-platform bitwise equality is
    a separate requirement.
12. **Multiple comparisons.** A per-row \(\alpha\) bounds expected false
    admissions by \(N_0\alpha\), not by \(\alpha\). Ranking quality, admission
    and contamination control are three measurements.

## 9. What to measure before implementing, in order

Each step gates the next.

1. **Identify the measured pipeline and the candidate population.** Per
   query: eligible corpus ids, stored row ids, vector candidates before and
   after admission, fused order, gate survivors, final order, effective
   configuration. The calibration record for QASPER lists 65,300 corpus rows
   while the null probe saw 20,508 unique ids; until that is reconciled the
   two instruments may not be scoring the same population. *Gate:* no loss is
   attributed to fusion alone until stages and populations agree.
2. **Verify cache identity, normalisation and pair alignment** through
   read-only connections. `_null_distribution` computes dot products without
   normalising, which is a cosine only while every stored vector is
   unit-norm. *Gate:* no cosine-distribution inference until norms and
   pairing are verified.
3. **Separate admission, fusion and downstream stages** on frozen embeddings
   and lexical scores: dense-only, dense with admission, RRF before
   post-processing, RRF after each stage; compute (20) at every transition.
   *Gate:* if the heuristic gate or the corpus population explains the DOWN
   family, revise the causal question before choosing fusion mathematics.
4. **Test query–doc null calibration** on held-out blocks at predeclared
   levels (0.01, 0.05, 0.1), stratified by model, language, query length,
   corpus group and row type, including lexical non-matches; record gold
   rejection and tail saturation. *Gate:* reject fixed-p admission wherever
   the null is anti-conservative or gold rejection is unacceptable.
5. **Test the relevance model.** Labelled joint p-value distributions,
   per-arm conditional error rates, empirical relevance as a function of
   \(S\); compare equal-\(\beta\) Fisher, the square-root mixture, dense-only
   and current RRF as one frozen, pre-registered experiment. *Gate:* if rare
   lexical distractors dominate the mixture or joint moderate evidence
   supplies most corrections, reject the single-tilt alternative.
6. **Test emergent suppression directly** with (13) and (16): count
   dense-correct reversals prevented and new reversals introduced. *Gate:*
   suppression counts only if harmful cross-document comparisons fall without
   destroying useful lexical corrections.
7. **Check the Gaussian approximation** (18) against empirical reversal
   probabilities; keep the exact replay equations if it mispredicts near-top
   reversals.
8. **Evaluate cold-start references** against held-out real query–doc
   scores, split by query group rather than by pair.
9. **Apply the pre-fixed success condition**: for both endpoint models,
   \(\overline\Delta_m=\frac1{22}\sum_t(\mathrm{NDCG}^{\mathrm{new}}_{m,t}-\mathrm{NDCG}^{A}_{m,t})>0\)
   on the same 22 tasks. *Gate:* an explicit weights layer is considered only
   after the declared p-only experiment fails this.
10. **Verify the operational invariants** of section 8 separately; ranking
    success does not authorise deployment if they fail.

## 10. Where it would slot in

All identifiers below are proposals, not settings the code reads today.

| Location | Integration |
| --- | --- |
| `_recall_rrf` | Keep the mode. Add a sibling path that gathers a bounded union, cross-scores the applicable arms, applies frozen null transforms and computes (10). |
| `_minmax_norm`, `_recall_rsf` | Do not reuse min-max. Keep `rsf` unchanged as a comparison mode. |
| `_autocut` | Recognise the new score explicitly; its gaps are not cosine gaps. |
| `_apply_quality_gate` | Add a distinct signal type. Never compare log-evidence or a combined p-value with a cosine threshold. Make the gate's bypass observable in experiments. |
| `do_recall` | Explicit mode dispatch and trace fields. Resolve the final confidence re-sort before claiming the fusion reaches the output. |
| `_null_distribution`, `do_calibrate_threshold` | A bounded query–doc and lexical calibration artefact: store the empirical reference distributions with provenance, not one threshold. Keep the existing methods. |
| `_get_vector_threshold` | Keep the cosine getter; add an independent null-artefact lookup. A p threshold cannot be substituted into a cosine-valued accessor. |
| `config.py` | Reserve a value (for example `null_p`) for both the calibration method and the recall mode, default off, with explicit validation of compatible combinations. |
| benchmark runner | Add the mode only once implemented; record every effective gate, arm coverage, reference fingerprint and per-query stage outcome. |

Stored artefacts need a fingerprint over: model identity and revision,
dimension, query/document prompts; normalisation, chunking, dtype, scoring
version; corpus/index generation and isolation scope; row type and near/far
eligibility; FTS tokenizer, query builder, bm25 configuration, fallback
policy; null definition, reference population, strata, sampled ids, seed,
effective sampling units; tail convention, clipping, combiner parameters, arm
applicability, candidate-depth contract; timestamp and schema version. The
existing sidecar checks dimension and scoring version; dimension alone cannot
detect a same-width model or prompt replacement.

## 11. What this note would not do

- Claim that null calibration estimates relevance probability.
- Promise that dense confidence alone recovers Gorilla: cross-document
  lexical extremes defeat row-specific dominance.
- Treat Fisher's rule as implicit arm selection.
- Apply independent chi-square or normal references to correlated, discrete
  arms.
- Declare admission solved by \(p=0.05\): it controls a null-tail event, not
  gold retention.
- Use pair counts in i.i.d. error bounds, or an NN-maximum as an ordinary
  positive sample.
- Treat \(\widehat J>0.2\) as sufficient.
- Generate fragment embeddings inside the no-call path.
- Use per-query candidate min-max, returned-list normalisation or
  top-result-dependent weights: each violates the score contract.
- Collapse absent, inapplicable, failed and censored arms into one value.
- Infer scan bounds from SQL output limits.
- Read the historical sign table as proof of structural causation.
- Build explicit weights before the declared p-only experiment has failed.
