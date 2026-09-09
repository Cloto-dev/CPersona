# Two arms, one decision — what null calibration can and cannot identify

Status: derivation. Written against the
[frozen-stage replay](frozen-stage-replay-2026-09.md) of the hybrid pipeline,
which located the benchmark losses in the fusion step and described how they
arise; this note asks what a fusion rule would have to know to avoid them. It
extends the [first derivation](adaptive-fusion-derivation.md) and corrects one
sentence in it (section 2). Nothing here is behaviour. Every result is labelled
*derived* (follows from the stated assumptions), *assumed* (a modelling choice
that the named measurement would refute) or *illustrative* (a number computed
to show a formula's shape, not a measurement of the server). The measurements
that decide between the constructions are listed in section 9.

**The result in four lines.**

1. A fusion rule whose only inputs are each arm's score and its exceedance
   probability against that arm's own null — the rule class the first
   derivation proposed — **cannot decide when lexical evidence should override
   dense evidence.** Two worlds with identical nulls and identical observations
   require opposite orders. This is an identifiability limit, not a weakness
   of one formula.
2. What is missing is the **lexical evidence conditional on the dense score**,
   \(J(v,l)=\log f_1(l\mid v)/f_0(l\mid v)\). Null calibration fixes the
   denominator of \(J\) and says nothing about its numerator.
3. One construction supplies it without a fitted weight: the **joint density
   ratio** of the score pair over a fixed reference population against the
   joint null, \(S^\star=\log h_q(v,l)/f_{0q}(v,l)\). Under lexical redundancy
   it collapses to the dense order; under informative lexical evidence it keeps
   the correction; reciprocal rank fusion is still its identifiable limit. Its
   five assumptions are unverified.
4. Two of the three measured loss mechanisms need no adaptivity at all. An
   absolute admission floor empties small candidate universes with a
   probability given in closed form, and the pool-size gate deletes every
   lexical-only row exactly when the pool has thirty rows or fewer. Both are
   removed by a **reservation invariant**: no stage may leave fewer than
   \(\min(10,n)\) rows while eligible candidates exist.

## 1. Assumptions, each with its refutation

1. **Each row has two applicable arms.** A memory row is scored by the dense
   arm and by the memory keyword arm; an episode row by the dense arm and the
   episode FTS arm. The derivation is written for one such pair \((v,l)\).
   *Refuted if* a row can receive a third independent vote in the shipped
   fusion.
2. **The null of each arm represents irrelevant query–document scores in the
   deployment scope** (same model, encoder roles, corpus snapshot, row type,
   isolation scope, declared query population). *Refuted if* held-out
   irrelevant pairs show excess small-p rates or the calibration moves
   materially across query language, length or corpus group.
3. **A "row-local rule" sees, for one row, the two scores, the two null
   exceedance probabilities, and — if it wants them — the joint law of the two
   arms under the null.** It does not see labels, and it does not see the
   other rows returned for the query. This is the class the first derivation
   worked in, and the class the doctrine of candidate-set independence
   admits.
4. **Ranking quality is expected DCG over a fixed eligible universe with
   binary relevance.** With the relevance count held fixed the ideal DCG is a
   constant and expected NDCG is proportional to expected DCG. *Refuted if*
   the metric of record changes.
5. **Dependence between arms is permitted under both classes.** Nothing below
   assumes the arms are independent; where a formula needs independence it
   says so.

## 2. The mixture rule is symmetric in the arms — a correction

The first derivation proposed the mixture likelihood ratio
\(E=\tfrac12\bigl(B(p_v)+B(p_l)\bigr)\) with \(B(p)=1/(2\sqrt p)\), and
observed that each arm's *influence* on a row's score,
\(\omega_a=B(p_a)/\sum_b B(p_b)\), emerges without a fitted weight. Consider a
relevant row \(g\) that is extreme on the dense arm and absent from the
lexical list, and an irrelevant row \(b\) that is moderate on the dense arm
and extreme on the lexical arm:

\[
E_g=\frac{p_v^{-1/2}+p_l^{-1/2}}{4}\Big|_{(10^{-4},\,1)}=\frac{100+1}{4}=25.25,
\qquad
E_b=\frac{p_v^{-1/2}+p_l^{-1/2}}{4}\Big|_{(0.05,\,10^{-4})}=\frac{4.47+100}{4}=26.12 .
\]

The irrelevant row wins. Its own dense influence is \(\omega_v(g)=0.990\) and
its own lexical influence \(\omega_l(b)=0.957\): both rows are dominated by
their best arm, and the cross-row order is still wrong. **The influence
\(\omega_a\) is the derivative of one row's score with respect to one arm; it
says nothing about which of two rows ranks higher.** The first derivation's
sentence that the mixture "silences lexical votes when the dense arm is
confident" must be read as a statement about a row's score, not about the
order, and is corrected there. The e-value guarantee of that construction
survives unchanged: an average of e-values is an e-value under any dependence,
so \(\mathbb E_0 E\le1\) — a statement about the null, not about relevance.

## 3. No row-local rule is correct in every world

*Derived.* Let both arms have uniform null exceedance probabilities,
\(f_0(p_v,p_l)=1\) on \((0,1)^2\). Keep the nulls, the query, and the observed
scores fixed, and consider two relevance laws that are each admissible under
assumption 2:

\[
\mathcal W_D:\ f_1(p_v,p_l)=B(p_v),
\qquad
\mathcal W_L:\ f_1(p_v,p_l)=B(p_l).
\]

In \(\mathcal W_D\) the lexical arm carries no information beyond the dense
arm (\(Y\perp P_l\mid P_v\)); the likelihood ratio is decreasing in \(p_v\)
alone and the Bayes order is the dense order, which prefers \(g\) above. In
\(\mathcal W_L\) the roles are swapped and the Bayes order prefers \(b\). A
deterministic rule that sees only \((p_v,p_l)\) — or the raw scores, or the
null joint law, which is the same independent uniform in both worlds —
receives identical inputs in both worlds and must return the same order. It is
therefore wrong in one of them. Randomising the rule cannot make both
decisions certain. Conditioning on there being exactly one relevant row among
the pair changes nothing.

The consequence is not that every fixed asymmetric formula fails on a given
finite benchmark — that is an empirical question the replay's per-query
summaries cannot settle, because they do not carry paired row scores — but
that **no distribution-free guarantee exists for the row-local class.** A
rule in that class is a bet on a relevance law, and the bet should be stated.

## 4. What the decision actually depends on

*Derived.* Factor the relevance likelihood ratio as

\[
\log\frac{f_1(v,l)}{f_0(v,l)}
=\underbrace{\log\frac{f_{1v}(v)}{f_{0v}(v)}}_{A(v)}
+\underbrace{\log\frac{f_1(l\mid v)}{f_0(l\mid v)}}_{J(v,l)} .
\tag{1}
\]

\(A\) is the dense arm's own evidence. \(J\) is the lexical evidence
*conditional on the dense score* — how much more likely this lexical score is
under relevance than under the null, given that the dense arm already said
\(v\). If \(Y\perp L\mid V\) then \(J\equiv0\), and if \(A\) is monotone the
fused order is exactly the dense order. A lexical correction happens when the
difference in \(J\) between two rows exceeds the opposing difference in
\(A\). Null calibration determines \(f_0(l\mid v)\), the denominator of
\(J\); it cannot determine the numerator. Neither can the arms' null
correlation, a copula of the nulls, the maximum dense score over the corpus,
or separate marginal evidence profiles: all of them are functions of
quantities that are identical in the two worlds of section 3.

## 5. A sufficient construction that fits no weight

*Derived under the assumptions listed at the end of this section.* Suppose the
eligible universe of a query has a joint score density that is a two-class
mixture of the null and the relevant law,

\[
h_q(v,l)=(1-\rho_q)\,f_{0q}(v,l)+\rho_q\,f_{1q}(v,l),\qquad \rho_q>0,
\]

and that both \(h_q\) and \(f_{0q}\) can be identified — \(h_q\) from the
population of score pairs itself, \(f_{0q}\) from the null. Then

\[
\boxed{S^\star(q,d)=\log\frac{h_q(v_d,l_d)}{f_{0q}(v_d,l_d)}}
\tag{2}
\]

orders rows exactly as the relevance likelihood ratio does, because

\[
\frac{h_q}{f_{0q}}=(1-\rho_q)+\rho_q\,\frac{f_{1q}}{f_{0q}} .
\]

The prevalence \(\rho_q\) is unknown and does not need to be fitted: it is a
positive affine map of the likelihood ratio and changes neither the order nor
the balance between the arms. This is a **joint density ratio**, not an
arm-weight vector — there is no per-model or per-task constant in it. Under
conditional lexical redundancy, \(h_q(l\mid v)=f_{0q}(l\mid v)\), the lexical
factor cancels and \(S^\star\) is the dense order; where lexical evidence is
informative the factor stays. Reciprocal rank fusion remains the identifiable
limit shown in the first derivation (single-arm equal mixture,
\(\beta=1,\ \kappa=K/N,\ p_a=(r_a+1)/N\)).

*Assumed, to be verified before (2) is a candidate for implementation:*

1. the null represents irrelevant rows in that query and scope
   (assumption 2 above);
2. the population is the stated two-class mixture;
3. the **joint** profile can be estimated adequately, including its tails —
   relevant rows are a small fraction of pairs, so \(h_q/f_{0q}\) is close to
   one everywhere except in the tails where \(f_{1q}\) concentrates, and
   estimation noise there competes with the signal;
4. the resulting ratio is coordinatewise monotone, if the monotonicity
   contract of the first derivation stays binding — unconstrained likelihood
   ratios need not be;
5. expected-NDCG claims condition on the relevance count (assumption 4).

*Doctrine.* A profile taken from a **declared reference panel** — a fixed set
of queries and documents chosen independently of any retrieval — is a function
of the query and the corpus snapshot only, so a score built from it is
candidate-set independent: changing the retrieval depth or adding an unrelated
row changes nothing. A profile estimated from the *returned* candidate list is
not, and is excluded. A corpus-wide profile also needs a work-budget argument;
"no model calls" does not by itself make it affordable.

## 6. Exact reversal conditions, and what each rule can repair

Write the implemented votes for a row \(d\) as
\(V_d=\mathbf 1_v(d)/(K+r_v(d)+1)\) and \(L_d=\mathbf 1_l(d)/(K+r_l(d)+1)\),
with \(K=60\), zero-based ranks, and an absent vote equal to zero. For a pair
in which the dense arm orders the relevant row \(g\) above the irrelevant row
\(b\), define \(M_V=V_g-V_b>0\).

| Rule | \(b\) ends above \(g\) exactly when |
| --- | --- |
| equal-vote reciprocal rank fusion | \(L_b-L_g>M_V\)  (3) |
| square-root mixture, \(U=p_v^{-1/2},\ W=p_l^{-1/2}\) | \(W_b-W_g>U_g-U_b\)  (4) |
| conditional-evidence rule (1) or (2) | \(J_b-J_g>A_g-A_b\)  (5) |

Ties are resolved by the declared total order. The replay records four kinds
of top-ten membership change; each specialises (3) and (4):

| Event | reciprocal rank fusion | square-root mixture |
| --- | --- | --- |
| intruder with a lexical vote only (\(V_b=0\)) | \(L_b>V_g+L_g\) | \(W_b-W_g>U_g-U_b\) |
| intruder already on the dense list, lifted by a lexical vote | \(L_b-L_g>V_g-V_b\) | same |
| relevant row pushed out, no lexical vote | \(L_b>V_g-V_b\) | \(W_b-1>U_g-U_b\) if absence is a true non-match |
| relevant row pushed out despite a lexical vote | \(L_b-L_g>V_g-V_b\) | \(W_b-W_g>U_g-U_b\) |

"Lexical vote only" in the replay means *no admitted dense vote*, not *no dense
score*: a row below the admission floor still has a cosine, and a p-value
computed from it. Setting its \(p_v\) to one would conflate censoring with
absence.

*Derived repair boundaries.*

- Equal-vote reciprocal rank fusion has no setting that repairs (3); it is
  what produced the reversals.
- The square-root mixture repairs a reversal only if the calibrated spacing of
  the scores happens to flip (4). It guarantees repair of **none** of the four
  classes.
- The dense-first policy (lexical weight zero, lexical rows filling the tail)
  removes every strict reversal among admitted dense rows and blocks
  lexical-only displacement while ten admitted rows exist. It also removes
  every lexical correction.
- Under verified conditional redundancy and monotone dense evidence, rule (2)
  removes all lexical-induced reversals, including the within-top-ten
  demotions the membership taxonomy does not count; under informative lexical
  evidence it admits corrections through (5). Bayes optimality is a statement
  about expectations, not a guarantee for every realised pair.

## 7. Query-level selectors: significance is not the decision

*Derived under conditionally i.i.d. irrelevant rows.* Let \(M_q\) be the
maximum dense score over the \(N\) rows of the corpus and \(F_{0q}\) the
query–document null. Then \(\Pr_0(M_q\le x)=F_{0q}(x)^N\), so the
significance of the observed maximum is

\[
p_{\max}(q)=1-F_{0q}(M_q)^N .
\tag{6}
\]

For independent, non-identical rows replace the power by the product; under
general dependence use the joint law of the maximum, or the conservative
Bonferroni form \(\min\bigl(1,\sum_i\Pr_0(S_{vi}\ge M_q)\bigr)\). The quantity
is admissible under candidate-set independence — it depends on the corpus
snapshot and the query, not on what was returned — and it is a different object
from \(\omega_a\), which is a per-row score derivative.

*Derived.* For any admissible query descriptor \(Z_q\), the Bayes choice
between the dense order and the fused order is

\[
\text{fuse iff } \mathbb E[\,\Delta_q\mid Z_q\,]>0,
\qquad \Delta_q=\mathrm{NDCG}_{\text{fused}}-\mathrm{NDCG}_{\text{dense}} .
\tag{7}
\]

Knowing the null law of the maximum supplies neither the conditional gain nor
the conditional probability of harm, so "significant maximum ⇒ dense only" is
not Bayes-consistent even with a perfectly calibrated (6). One exceptional
dense hit does not establish that the dense top ten is correctly ordered. The
replay measures how little the maximum cosine predicts: the best threshold on
it gains at most a fraction of a point over the better constant policy, while
the per-query oracle sits three to six points higher.

*Illustrative.* A Gaussian null with mean \(\mu_0\) and p95 \(q_{.95}\) gives
\(\sigma_0=(q_{.95}-\mu_0)/\Phi^{-1}(.95)\) and a switching threshold at
maximum-significance level \(\gamma\) of
\(\tau_\gamma(N)=\mu_0+\sigma_0\Phi^{-1}\bigl((1-\gamma)^{1/N}\bigr)\). The
replay note reports where this lands against the empirical optimum; on three
of the losing tasks it is far off, which rejects the pooled Gaussian selector,
not (6). Tail resolution is a separate constraint: with an empirical reference
of \(m\) query-conditional null draws the smallest representable tail is
\(1/(m+1)\), and a Bonferroni level of \(0.05\) over \(N=20{,}000\) rows needs
\(m\ge 4\times10^5\) before the tail is representable without extrapolation.

## 8. Small universes: starvation, extinction, reservation

*Derived.* Let \(\alpha=\Pr_0(S_v\ge t)\) be the admission floor's null
exceedance and \(\beta=\Pr_1(S_v\ge t)\) its acceptance of relevant rows.
Conditional on an eligible universe of \(n\) rows with \(m\) relevant, and
independent admissions within each class, the number admitted is
\(A=A_0+A_1\) with \(A_0\sim\mathrm{Bin}(n-m,\alpha)\),
\(A_1\sim\mathrm{Bin}(m,\beta)\), and

\[
\Pr(A<10)=\sum_{j=0}^{\min(m,9)}\binom mj\beta^j(1-\beta)^{m-j}\,
F_{\mathrm{Bin}(n-m,\alpha)}(9-j) .
\tag{8}
\]

With query-dependent rates, average (8) over queries after computing it;
substituting pooled rates is invalid. *Illustrative*, all-null universe,
\(\alpha=0.05\):

| \(n\) | 19 | 45 | 196 | 200 |
| --- | --- | --- | --- | --- |
| \(\Pr(A<10)\) | \(\approx1\) | 0.9999 | 0.481 | 0.455 |

A fixed small-\(p\) admission rule cannot also promise ten results from a
small, weakly separated universe. The replay measures 31.8 % of QASPER's
queries — whose eligible universe has a median of 45 rows — with fewer than
ten admitted rows, and the pooled Gaussian version of (8) under-predicts that
and mispredicts the EPBench groups by orders of magnitude, which rejects the
pooled independent model, not the identity.

*Derived and checked against the code.* The pool-size gate compares a
lexical-only row's fused score \(1/(K+r_l+1)\) with
\(m(P)\cdot 3/(K+1)\), where \(P\) is the pool size and
\(m(P)=0.5-0.3\min\bigl(1,\log(P+1)/\log 500\bigr)\). A lexical-only row
survives exactly when

\[
r_l\le\frac{K+1}{3\,m(P)}-(K+1),
\tag{9}
\]

and **no lexical-only row survives iff \(m(P)>\tfrac13\), i.e. \(P\le30\)**
(\(500^{5/9}-1=30.58\); the shipped rounding to four decimals keeps the
integer boundary at 30). At \(P=196\) the cut is at lexical rank 21, at
\(P\ge500\) at rank 40. The same gate's cosine branch applies \(m(P)\) to
dense rows, which for small pools exceeds the admission floor and deletes
relevant dense rows as well; the replay's per-group figures show both effects.

*Derived: the reservation invariant.* Let \(\mathcal U_q\) be the eligible
universe after mandatory filtering and \(k_q=\min(10,|\mathcal U_q|)\). Reserve
the dense top \(k_q\) before admission, keep the reserve reachable by every
later stage, and at final selection append eligible reserved rows — marked as
fallback, not as statistically qualified — until

\[
|O_q|\ge k_q .
\tag{10}
\]

Protecting the initial dense list is not enough if a later gate can delete the
reserve; (10) is a cardinality contract over the whole path. It preserves
candidates and calibrated scores; it does not promise relevance and does not
protect dense rows from being demoted by fusion. For comparison, a purely
probabilistic admission that guaranteed \(\Pr(A<10)\le0.05\) on an all-null
universe would need a null allowance of
\(\alpha_{10}(n)=F^{-1}_{\mathrm{Beta}(10,\,n-9)}(0.95)\), which is 0.68 at
\(n=19\), 0.32 at \(n=45\) and 0.08 at \(n=200\) — allowances large enough to
show why deterministic fallback should be kept distinct from evidence-based
admission.

## 9. What must be measured before any of this is built

Each row names a computation and the outcome that refutes the claim; the
per-query fields are those of the replay's output.

| Claim | Computation | Refuted when |
| --- | --- | --- |
| Row-local rules cannot be distribution-free (section 3) | none — a theorem; its *relevance* is refuted empirically | a row-local rule beats the dense order on every task of both endpoint models (then the worlds of section 3 do not occur in practice) |
| The dominant loss mechanism is dense rows lifted by lexical votes | share of `intruder_dense_boosted` among intruders, per losing task | share \(\le50\,\%\) on a claimed losing task |
| Membership changes explain the loss | loss from queries with `s2 < s1` and `gold_out == 0` | already refuted for "at least half": 55 % and 64 % of the loss on two tasks is within-top-ten reordering |
| The mixture repairs harmful reversals | row-keyed \(p_v,p_l\), raw scores, presence and censoring, labels, both orders; evaluate (4) | any strict pair disagreement with (4) outside a \(10^{-12}\) tie band |
| Redundancy implies the dense order under (2) | a declared synthetic law with \(f_1(l\mid v)=f_0(l\mid v)\); evaluate (2) with exact densities | any strict reversal of unequal dense ratios |
| The data have that redundancy where fusion loses | a held-out estimate of \(J\) from a row-keyed dump | cannot be decided from per-query summaries; null-only dependence is insufficient |
| A maximum-significance selector is non-inferior | freeze the null estimator, \(N\), dependence treatment and \(\gamma\); compute its utility | more than 0.01 point below the better constant policy |
| The null maximum is calibrated | on held-out all-null queries count \(p_{\max}\le\gamma\) at \(\gamma\in\{.01,.05,.10\}\) | a one-sided 95 % lower bound above \(\gamma\), with queries as the unit |
| Lexical-only extinction iff \(P\le30\) | per-row arm membership, global lexical rank, gate branch and survival; evaluate (9) | one contradicting eligible row outside arithmetic equality |
| Reservation prevents starvation | eligible universe size, reserve ids, survivors of every stage | one output smaller than \(\min(10,n)\) with sufficient eligible reserved rows |
| Candidate-set independence | same query, row, snapshot, scope, reference; vary returned depth and list composition | any score difference above \(10^{-12}\) |
| The binding success condition | 22-task mean of \(\mathrm{NDCG}_{\text{new}}-\mathrm{NDCG}_{\text{dense}}\), separately for the weak and the strong model | \(\le0\) for either model |

The last row is the condition the [design page](../RELIABLE_RECALL_2_6.md#5-adaptive-fusion)
fixed in advance. It is stated against the dense-only order, not against the
admitted or the gated list; an improvement over either of those is not
success.
