# Results: keyword seats against none

Registration: [prereg-keyword-seats.md](prereg-keyword-seats.md), committed in
`a35bb60` before any test question was searched with seats and before any model
call. Both rules were applied once, as registered; no run was repeated.

## What the rules say

- **Primary rule met (the private real-use pack).** Under `rrf`, with
  `reconstruct` at count 10 and the default budget, two keyword seats raised the
  evidence entries reached on the 150 test questions from 168 to 173 of 196:
  +5, +0.033 per question, two-sided 95% t interval +0.0043 to +0.0624. No
  question reached fewer entries. The development questions had shown +10; the
  test gain is half of it, as a gain chosen on development questions is expected
  to shrink.
- **No harm detected (LongMemEval-S).** On the 400 test questions, the answers
  with two seats were right on 81.50% against the published 80.75%: +0.75
  points, Tango's 95% interval −1.72 to +3.30. The upper bound is not below 0.
  This is not a claim of non-inferiority: the test detects a large loss and has
  little power against a small one.

## The cost

| | Without seats | With two seats | Change |
| --- | --- | --- | --- |
| Private pack, shown characters per response (`rrf`) | 5,719 | 6,381 | +11.6% |
| Private pack, payload characters per response (`rrf`) | 8,082 | 9,152 | +13.2% |
| LongMemEval-S, retrieved context (`cl100k_base`, the harness's count) | 1,526.9 | 1,705.7 | +11.7% |

Every response carried both seats: on the private pack's test questions each of
the 150 responses held two more rows (14 against 12), and on LongMemEval-S every
one of the 400 contexts changed.

## Run

- **The private pack.** Commit `a35bb60`, the prepared databases of the pack, the
  shared embedding server, `CPERSONA_LEXICAL_ENGINE=fts5`, seats 0 and 2. All
  four runs (two fusions × two seat counts) restored their calibration and
  mapped every returned row. Quotes shown rose from 146 to 151. Under rsf with
  confidence (reported, no rule): 162 and 162 entries, shown characters 5,700
  and 6,237.
- **LongMemEval-S.** Copies of the published store with its calibration file,
  the published search's settings, commit `a35bb60`. The reproduction check
  passed: with no seat, every one of the 400 contexts equals the published one.
  With two seats, all 400 changed, so all 400 were answered (one pass, 400
  answer calls, 783,176 prompt and 59,326 completion tokens as the API reported
  them, model `gpt-4.1-mini-2025-04-14`) and judged once by `gpt-4o-mini`.
  Discordant pairs: 10 right to wrong, 13 wrong to right. Answer sessions shown:
  96.4% with and without seats (all shown 93.8% with both).
- The interval was recomputed with a second implementation (a numeric
  maximisation of the restricted likelihood in place of the closed form) and
  agreed to the second decimal.

## Reading

The seats do what they were built for where the gate was the obstacle: on a
pack of real agent memory under the default fusion, rows only the keyword arms
found reach the response, and some of them are the evidence. On LongMemEval-S,
whose records are long sessions, the share of answer sessions shown did not
move, so the seats added context without adding evidence on average, and the
answers did not measurably change. Under rsf the gate can already pass a keyword-only row, and
the seats added nothing on the private pack.

Whether two seats become the default is not decided by these rules. The gain on
the private pack is 5 entries of 196 for about a ninth more text in every
response.
