# Keyword seats against none: the private real-use pack and OmniMemEval's LongMemEval-S

Registered before any test question of this study was searched with keyword
seats, and before any answer or judge call of it. The commit that adds this file
is the evidence of the order.

## Question

Under the default fusion (`rrf`) the quality gate keys a row that carries no
cosine on its fused score. A row only the keyword arms found has one
reciprocal-rank vote, at most 1/(K+1) = 1/61 ≈ 0.0164, and the calibrated gate
of the stores below is 0.05, so no such row reaches a response: the refusal is
the scale's, not a judgement of the row. `CPERSONA_KEYWORD_SEATS=2` holds two
places for such rows after the answer, in the shape of the block arm's
reservation ([block reach §5](../../docs/BLOCK_REACH_DESIGN.md)): the gate is not
consulted for them, they displace nothing, and they are not credited to the
recall count. A row the gate refused on a scale that can pass it (rsf,
confidence, cosine) does not take a seat; one the gate admitted and the count
cut may.

Two questions: does it bring more evidence into `reconstruct`'s response on a
pack of real agent memory, and does the longer context it sends harm answers on
LongMemEval-S?

## Development (done before this registration)

All on development questions only, search only, no model call, at the commit
that adds this file's parent (`7f81570`, the seats; `1fd4d0d`, the switch that
turns the keyword arms off).

- **The keyword arms' share.** With both keyword arms off, the private pack's 150
  development questions under `rrf` reached 148 of 193 evidence entries against
  157 with them on (+0.060 per question, 95% +0.020 to +0.107). Under rsf with
  confidence the difference was +0.020 (−0.067 to +0.100), and on the 100
  LongMemEval-S development questions +3.47 points of answer sessions shown
  (−1.1 to +8.5): the gate this study touches decides how much of the keyword
  arms' work reaches a response.
- **What the gate drops.** Under `rrf`, every keyword-only candidate of the
  private pack's development questions was refused (2,691 of 2,691), and 15
  evidence records reachable only that way were among them.
- **Seats, measured with the implementation** (`reconstruct`, count 10, default
  budget, evidence entries reached; the run with no seat equals the runs before
  the seats existed on every field but timing):

  | Seats | Private pack, `rrf` | Private pack, rsf + confidence | Shown characters (`rrf`) |
  | --- | --- | --- | --- |
  | 0 | 157 / 193 | 153 / 193 | 5,744 |
  | 1 | 165 | 153 | 6,060 |
  | 2 | 167 | 153 | 6,397 |
  | 3 | 167 | 153 | 6,720 |
  | 5 | 167 | 153 | 7,379 |

  No question reached fewer entries with seats than without, which the
  construction guarantees.
- **LongMemEval-S development, `rrf`, two seats.** Every context changed
  (100 of 100), the mean context grew from 1,549.7 to 1,732.9 `cl100k_base`
  tokens (+11.8%), and the answer sessions shown did not change on any question.
  With no seat, the contexts equal the published ones on all 100.

**Two seats were chosen on the development curve**, where it stops rising. A
gain chosen on development questions is expected to shrink on test questions.

## Test

### The private real-use pack (primary)

- Its 150 test questions only. The questions cannot be published; the result is
  reported in counts. The registration of the run itself (paths, the prepared
  database, the commands) is kept with the pack.
- `reconstruct`, count 10, default budget, `rrf`, block reach on, at the commit
  that adds this file, with no seat and with two.
- **Rule.** Per question, the evidence entries reached with two seats minus
  those reached with none. The lower bound of the two-sided 95% t interval of
  the mean difference (149 degrees of freedom) above 0 (strictly) means: on the
  real-use pack, under the default fusion, two keyword seats bring more evidence
  into the response.
- **Reported beside it, with no claim:** quotes shown, shown characters and
  payload characters with each; the same comparison under rsf with confidence.

### LongMemEval-S (harm detection)

- The 400 test questions (the 500 less the 100 of
  [`v1_5_dev_questions.json`](../omnimemeval/v1_5_dev_questions.json)), and
  only those.
- Two searches on copies of the published store with its calibration file, at
  the commit that adds this file, with the published search's settings: no seat,
  and two seats. **Reproduction check, before any call:** with no seat, the
  contexts must equal the published ones for all 400
  ([`keyword_seats_build.py`](../omnimemeval/keyword_seats_build.py) stops
  otherwise).
- **Only the changed questions are answered**: those whose context with two
  seats differs from the published one. Every other question keeps its
  published answer and grade, because it sends the same prompt.
- **Answer and judge exactly as the published run**: OmniMemEval `0b1ea8d` with
  the same adapter patch, answer model `gpt-4.1-mini-2025-04-14`, judge
  `gpt-4o-mini`, one judge run, temperature 0, at most 3 concurrent model calls,
  into one directory, `cpersona-lme1-kwseats2`.
- **The control is the published run's recorded answers**, not answered again.
  The seats' answers therefore come from a second answer pass, and part of any
  difference is that pass's variation (per-question agreement of two passes on
  the same contexts was measured at 96.0%).
- **Rule.** Tango's score interval (95%, two-sided) for the paired difference
  in accuracy, seats minus control, applied by
  [`keyword_seats_analyze.py`](../omnimemeval/keyword_seats_analyze.py). Its
  upper bound below 0 means: on LongMemEval-S, two keyword seats lowered
  accuracy, and they are not recommended. Otherwise: no harm was detected.
  With no true difference, this rule detects harm with probability 2.2% to
  2.6% at discordance rates from 2% to 40% of the questions (computed by
  enumerating every outcome at 400 questions), close to the nominal 2.5%. The
  percentile bootstrap used by earlier registrations is not used here: with few
  discordant pairs it cannot generate a loss the sample did not contain, and at
  400 questions it passes a 2% loss against a two-point margin 9.7% of the time
  where the nominal rate is 2.5%.
- **Reported beside it:** accuracy with each, the discordant pairs, the mean
  retrieved context, the number of changed questions, and the answer sessions
  shown with each.

## What may be said

- The primary rule, as written above, about the private pack under `rrf` with
  two seats.
- The harm rule, as written above. A result that detects no harm is reported as
  "no harm detected", never as "no harm": this test has little power against a
  small loss (at 400 questions and a 5% discordance rate, a true difference of 0
  clears a two-point non-inferiority margin under Tango's interval about 39% of
  the time), and it does not claim non-inferiority.
- Whether two seats become the default is decided after both results. It is not
  part of either rule.

## What it does not say

- Nothing about rsf, about identifier lookups, about other seat counts, other
  counts or other budgets, or about `recall`'s response (only `reconstruct`'s is
  measured).
- Nothing about the development questions, which chose the number of seats.

One run of each. A rerun is made only for an infrastructure failure, and is
reported. Nothing in the construction or the settings changes after any test
result is seen.
