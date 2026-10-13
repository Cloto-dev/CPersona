# The 2.6.11 check on LongMemEval-S and LoCoMo: registration

Registered before any LongMemEval-S test question is searched at the
candidate commit, and before any LoCoMo answer or judge call.

## Question

The candidate for 2.6.11 is commit `a613114`
([#479](https://github.com/Cloto-dev/CPersona/pull/479)). It changes only what
`reconstruct` returns under `mode: "pro"`. Lite and a call that names no mode
return what 2.6.10 returns. Against 2.6.10 (commit `73d6bca`, tag `v2.6.10`),
Pro:

- **holds the extra places again**: the eight extra keyword places
  ([#463](https://github.com/Cloto-dev/CPersona/pull/463)) and the eight depth
  places ([#469](https://github.com/Cloto-dev/CPersona/pull/469)). 2.6.10
  removed them before its release
  ([#477](https://github.com/Cloto-dev/CPersona/pull/477)), because Pro
  answered worse on LongMemEval-S with them
  ([results](results-omnimemeval-lme-2_6_10.md)).
- **quotes long held records last**: the passages of a held item whose record
  is longer than 6,000 stored characters follow every other passage of the
  payload sequence. A held item quotes one or two passages; of a long
  conversation they seldom reach the evidence, and they took the cap from the
  window's own quotations.

Whether the places bring more of a question's evidence within the cap is
judged on a sealed private pack, registered separately because its questions
cannot be published. These two benchmarks ask:

- **LongMemEval-S**: does Pro answer clearly worse than 2.6.10? This is a net
  for a clear loss, not a showing that there is none.
- **LoCoMo**: does Pro answer worse than 2.6.10 by 2 points or more? This is a
  test of non-inferiority.
- **The cap**: does every Pro response stay within it?

## The arms

| | 2.6.10 | Candidate |
| --- | --- | --- |
| Commit | `73d6bca` (tag `v2.6.10`) | `a613114` |
| Pro | `mode: "pro"`, `count` 15, cap 5,000 | the same |

Both are run on Python 3.11.15. If the candidate's commit changes before a
search, this registration is amended first.

## Development (done before this registration)

- **The 6,000 characters** were chosen on development data only, with no
  model called:
  - the private pack's development questions;
  - LongMemEval-S's 100 development questions, searched only.
- **The places** were chosen on the private pack, as for 2.6.10.

## What was already seen

### LongMemEval-S

- **Earlier use.** The 400 test questions were used by:
  - the evidence-allocation tests;
  - the Lite and Pro test;
  - the 2.6.9 and 2.6.10 checks;
  - the exploration after the 2.6.10 check, which answered Pro at 2.6.10a2
    (354 of 400), with the extra keyword places (348), with the depth places
    (343), and with every held item's passages after the window (350).
- **The nearest arm.** The last of these is close to the candidate on this
  benchmark: 75% of its stored records are longer than 6,000 characters.
- **Not yet searched.** The candidate has not been searched on a test question.
- **Scope of the claim.** Only a clear loss is tested here; no accuracy gain is
  claimed from it.

### LoCoMo

- **Earlier use.** None by any earlier CPersona measurement.
- **Searched only, on 2026-10-13.** The 1,540 questions were searched with no
  answer or judge call, to count the questions whose context changes and the
  answer-stage input:
  - at 2.6.10 and at the candidate, and at two local experiment branches
    that reproduce them;
  - the commits and the branches gave the same context on 1,539 of 1,540
    questions; the one that differed was the first query after the server
    started;
  - the contexts of 2.6.10 and of the candidate differ on 1,240 questions.
- **Counted for the judge's cost.** The question and gold-answer texts were
  counted in tokens. No answer was compared with a gold answer, and no
  evidence label was read.
- **The candidate here.** The stored records are short: 1 of 272 is longer
  than 6,000 characters. On LoCoMo the candidate therefore behaves as
  2.6.10a4's Pro did.

## Settings common to both

- **Harness**: OmniMemEval `0b1ea8d` with the adapter patch
  [`omnimemeval-0b1ea8d.patch`](../omnimemeval/omnimemeval-0b1ea8d.patch).
  This registration's commit adds LoCoMo's two branches to it; the
  LongMemEval-S part is unchanged.
- **Models**: answer model `gpt-4.1-mini-2025-04-14`, judge `gpt-4o-mini`,
  as the harness publishes. Temperature 0, one judge run.

## LongMemEval-S

- **2.6.10's answers.**
  - 2.6.10's code is 2.6.10a2's (`b03be70`) with only the version changed:
    the 2.6.10a3 and 2.6.10a4 additions were added and removed again in
    between.
  - The exploration answered 2.6.10a2's Pro on the questions whose context
    changed against 2.6.9. Every other question kept 2.6.9's recorded answer.
  - The 400 test questions are searched at `73d6bca` as well. A question whose
    context there differs from 2.6.10a2's recorded context is answered at
    `73d6bca`, and that answer is 2.6.10's. Every other question keeps
    2.6.10a2's recorded answer.
- **The candidate.**
  - The 400 test questions are searched at `a613114` through the harness's
    search wrapper, with `mode: "pro"` and `count` 15, as in the 2.6.10 check.
  - A question is changed when its context differs from 2.6.10's. Only changed
    questions are answered. Every other question sends the same prompt and
    keeps 2.6.10's answer.
- **The rule, as in the 2.6.10 check.** Among the changed questions, count
  those 2.6.10 answered right and the candidate wrong (L), and the reverse
  (W). The candidate fails when an exact binomial test of L among L + W at one
  half gives one-sided p < 0.05.
- **What the net can catch.** At the rate of change of the 2.6.10 check:

  | True loss (of 400) | Chance of failing |
  | ---: | ---: |
  | 0 | about 3% |
  | 4 | about 14% |
  | 11 | 62–74% |

  A pass therefore says the candidate is not clearly worse here. It does not
  say the candidate is not worse.

### Recorded before the LongMemEval-S test

Added by a later commit, before any test question was searched at the
candidate commit.

- **The scripts**:
  - [`v2_6_11_lme_build.py`](../omnimemeval/v2_6_11_lme_build.py) writes the
    answer inputs: the questions whose context at `73d6bca` differs from
    2.6.10a2's recorded one, and the candidate's changed questions.
  - [`v2_6_11_lme_analyze.py`](../omnimemeval/v2_6_11_lme_analyze.py) applies
    the net and the cap. It also checks that every context the answers stand
    on is the context the two searches returned.
  - 2.6.10a2's recorded answers are read as the exploration recorded them.
    Read that way, they total 354, the published figure.
- **A dry run on recorded data.** With 2.6.9 in place of 2.6.10 and
  2.6.10a2's exploration run in place of the candidate, the analysis gives
  354 against 354, 8 lost and 8 gained: the published exploration.
- **The interpreter**: Python 3.11.15, as in the 2.6.10 check.

## LoCoMo

- **Data**: `locomo10.json` (sha256
  `79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4`). The
  1,540 questions the harness evaluates: categories 1 to 4 (the harness
  excludes category 5).
- **Ingestion** (done on 2026-10-13, before this registration):
  - the 272 sessions of the 10 conversations go into one fresh store;
  - the patch's LoCoMo branch keeps one memory per conversation: both speakers'
    turns under the first speaker's id, as the harness does for Cognee and
    Letta;
  - the search is the harness's shared search: one search per question, whose
    memories both speakers are given;
  - each conversation's threshold was calibrated once, and each arm searches a
    clone of that store with its calibration file, so both arms read the same
    store and thresholds.
- **Search**: through the harness's search with `mode: "pro"` at its default
  `count` (15). Before the 1,540 questions, each arm's server answers one query
  that is not a test question, because the first query after the server
  started returned a different context once on 2026-10-13.
- **Answers**:
  - The candidate answers all 1,540 questions.
  - 2.6.10 answers only the questions whose context differs from the
    candidate's.
  - Every other question sends the same prompt, so 2.6.10 takes the
    candidate's answer and grade there.
- **The rule (non-inferiority).**
  - Let N = 1,540, n the number of changed questions, W those the candidate
    answered right and 2.6.10 wrong, L the reverse, m = W + L, and
    D = W − L. Unchanged questions cannot disagree, so the test runs on the
    n changed questions with the margin of 2 points carried from all N:
    a = −0.02 · N / n.
  - The statistic is the paired score test for a difference of proportions
    (Tango, 1998), with the discordance rate estimated under the margin:

    ```text
    b = m + a·D
    q = (b + sqrt(b² − 4n(a·D − (n − m)·a²))) / (2n),  kept within [|a|, 1]
    Z = (D − n·a) / sqrt(n·(q − a²))
    ```

  - The candidate passes when Z > 1.68.
- **Why 1.68, not 1.645.**
  - At n = 1,240 the size of the test at its margin was computed by finite sums
    over every discordance rate.
  - At 1.6449 it reaches 5.04%; at 1.68 it is at most 4.69%.
  - 1.68 is the smallest of the values tried (1.6449, 1.66, 1.68, 1.70, 1.72)
    whose size stays at or below 5%. It is fixed whatever n the test search
    gives.
- **What the test can show.** Treating the questions as independent, at a
  discordance rate of 4.5% to 10%:

  | True difference | Chance of passing |
  | ---: | ---: |
  | 0 points | 86–99% |
  | −1 point | 38–62% |
  | −2 points | about 4.6% |

- **Its limit.** LoCoMo has only 10 conversations. If answers within one
  conversation are correlated, the effective number of questions is smaller
  than 1,540. The difference per conversation and the number of changed
  questions in each are reported.
- **Reported beside the rule**: correct answers, W and L, accuracy per
  category, and the answer-stage context tokens.
- **Scripts.** The scripts that build the answer inputs, record the raw
  responses for the cap and apply the rule are added by a later commit,
  before the LoCoMo test search, and named here then.

## The cap

- Every Pro response of the candidate's searches on both benchmarks is within
  5,000 tokens and equal to its `used_tokens`, counted outside the server in
  `cl100k_base`.
- One response over the cap means the release does not ship.

## Order, and what ships

- **LongMemEval-S runs first.** If its net fails, the release stops there and
  LoCoMo is not answered.
- **What ships.** 2.6.11 ships only when all four hold:
  - the private pack's test;
  - LoCoMo's test;
  - LongMemEval-S's net does not fail;
  - the cap.

## What it does not say

- That 2.6.11 improves accuracy on either benchmark. The claim the places are
  built for is judged on the private pack.
- Nothing about Lite, the default (no mode), other caps or other answer models.

One run each. A rerun is made only for an infrastructure failure, and is
reported. Nothing in the construction or the settings changes after any answer
is seen.
