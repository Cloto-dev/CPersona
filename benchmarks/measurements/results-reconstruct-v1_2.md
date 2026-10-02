# Results: reconstruction v1.2

Registration: [prereg](prereg-reconstruct-v1_2.md) (`513ab5a`, amended in
`6f64637` before any answer was judged). The pack replay ran four arms over the
150 test questions, with no model. The reader study made 84 reader calls and 84
judge calls; none failed.

**Verdict.**

- **Pack replay: both registered claims are met.** Evidence quotes shown rose
  from 133 to 146 of 196 (+13; 95% interval of the difference 5 to 22), and the
  evidence reached was the same on every question (168 in both arms). The first
  response shrank to a median ratio of 0.662 (95% interval 0.655 to 0.669).
- **Reader study: no claim about reading cost.** The median payload ratio was
  0.624 (95% interval 0.559 to 0.710): below the 0.85 threshold, with an
  interval below 1.0. But the registered noise floor, the larger of the two
  arms' floors, was 0.512, and the 0.376 reduction does not exceed it. Correct
  answers were 33 against 32, so the correctness guard passed.

## Pack replay (150 test questions, 196 evidence quotes)

| | 2.6.3a1 | v1.2 |
| --- | --- | --- |
| Evidence reached | 168 | 168 (the same on all 150 questions) |
| Evidence quotes shown | 133 | **146** (16 questions more, 3 fewer) |
| Response characters, total | 1,837,556 | 1,212,340 |
| Response characters, median ratio to 2.6.3a1 | | **0.662** (0.655 to 0.669) |
| Quoted text, total characters | 1,250,083 | 857,805 |

Two more arms were reported and decide nothing:

| Arm | Evidence shown | Quoted text | Response, median ratio to 2.6.3a1 |
| --- | --- | --- | --- |
| 2.6.3a1, every quote cut to 600 (v1.2's head budget, spent evenly) | 123 | 934,216 | 0.823 |
| v1.2 | 146 | 857,805 | 0.662 |
| v1.2, every quote up to 800 (place sizing off) | 152 | 1,246,119 | 0.877 |

Spending v1.2's head budget evenly loses evidence (133 to 123). v1.2 quotes less
text than that control and shows 23 more quotes, so the gain comes from where
quotes are taken (block ranking and joined passages), not from how much is
quoted. Place sizing costs 6 quotes (152 to 146) and saves about a quarter of the
response (0.877 to 0.662).

| Type | Questions | Evidence | Shown, 2.6.3a1 | Shown, v1.2 |
| --- | ---: | ---: | ---: | ---: |
| T1 | 25 | 26 | 19 | 23 |
| T2 | 25 | 27 | 12 | 18 |
| T3 | 25 | 34 | 22 | 23 |
| T4 | 25 | 58 | 43 | 42 |
| T5 | 25 | 51 | 37 | 40 |
| T6 | 25 | 0 | 0 | 0 |

The development questions, on which the change was designed, gave 127 to 136
and 0.659; the held-out test questions gave 133 to 146 and 0.662.

## Reader study (36 LongMemEval-S questions)

| | A: 2.6.3a1 | B: v1.2 |
| --- | --- | --- |
| Correct answers | 32 / 36 | 33 / 36 |
| Payload characters, total | 1,619,017 | 1,164,376 |
| Payload characters, median per question | 25,529 | 16,369 |
| of which search responses | 1,364,741 | 974,422 |
| of which expansions | 254,276 | 189,954 |
| Reader input tokens, total | 2,982,698 | 3,156,800 |
| of which cached | 1,929,472 | 2,203,136 |
| Payload characters per correct answer | 50,594 | 35,284 |
| Input tokens per correct answer | 93,209 | 95,661 |
| `search` calls | 112 | 119 |
| `expand` calls (records: whole, ranged) | 21 (18, 12) | 24 (16, 21) |

Per-correct figures are ratios of sums. B's payload was the smaller on 29 of the
36 questions; the per-question ratio ran from 0.269 to 1.804. The answers
differed on three questions: B right and A wrong on two (multi-session,
temporal-reasoning), A right and B wrong on one (single-session-preference).

### The registered rule

| Condition | Registered | Measured | Met |
| --- | --- | --- | --- |
| Median of r = B / A | at most 0.85 | 0.624 | yes |
| 95% bootstrap interval of the median | below 1.0 | 0.559 to 0.710 | yes |
| Median reduction against the noise floor | larger | 0.376 against 0.512 | **no** |
| Correct answers | B at least A − 2 | 33 against 32 | yes |

The noise floor is the median, over six repeated questions per arm, of
|second − first| / first in payload characters:

| Arm | Pairs (relative difference) | Floor |
| --- | --- | --- |
| A | 0.000, 0.026, 0.223, 0.802, 0.978, 2.040 | 0.512 |
| B | 0.000, 0.000, 0.000, 0.000, 0.424, 1.430 | 0.000 |

Six pairs per arm do not hold a floor still: the same reader on the same store
repeated four of B's questions call for call, and read three times as much the
second time on one of A's. The rule takes the larger floor, and it is applied as
registered. A later registration should take the floor from more pairs, or from
the same questions in both arms; that would not change this verdict.

### Tokens

Input tokens fall far less than payload: the median per-question ratio of input
tokens, B / A, is 0.909 (95% interval 0.827 to 1.025), and the total rose by 5.8%
because B searched 119 times against A's 112. A reader pays for the tool
schemas and the conversation on every turn whatever the tools return, so a
smaller response moves tokens only by its own share.

### By question type (correct of 6, payload characters)

| Type | A | B |
| --- | --- | --- |
| knowledge-update | 6, 184,269 | 6, 72,286 |
| multi-session | 5, 207,826 | 6, 190,219 |
| single-session-assistant | 6, 150,315 | 6, 149,766 |
| single-session-preference | 4, 518,275 | 3, 436,825 |
| single-session-user | 6, 160,446 | 6, 61,588 |
| temporal-reasoning | 5, 397,886 | 6, 253,692 |

Six questions a type decide nothing.

## Changes against 2.6.3a1

| Instrument | Measure | 2.6.3a1 | v1.2 | Change | Status |
| --- | --- | --- | --- | --- | --- |
| Pack, test | Evidence quotes shown, of 196 | 133 | 146 | +9.8% | registered, met |
| Pack, test | First response, median ratio | 1 | 0.662 | −33.8% | registered, met |
| Pack, test | Evidence reached, of 196 | 168 | 168 | 0% | registered validity check, held |
| Pack, test | Evidence shown by 2.6.3a1 at v1.2's head budget, spent evenly | 133 | 123 | −7.5% | exploratory (same-cost control) |
| Pack, test | Evidence shown with place sizing off | 133 | 152 | +14.3% | exploratory |
| Reader | Payload per question, median ratio | 1 | 0.624 | −37.6% | registered, **not met** (noise floor) |
| Reader | Correct answers, of 36 | 32 | 33 | +1 | registered guard, passed |
| Reader | Input tokens per question, median ratio | 1 | 0.909 | −9.1% | exploratory |
| Reader | Payload characters per correct answer | 50,594 | 35,284 | −30.3% | exploratory |
| Pack, development | Evidence shown, of 193 | 127 | 136 | +7.1% | exploratory (design data) |

## Limits

- One private pack, and `rrf` with confidence off only; the configuration with
  `rsf` and confidence on is not covered.
- Thirty-six questions and one reader. The correctness guard detects only a large
  loss; 33 against 32 is not evidence that answers improve.
- One corpus with one row per session: bundling, excerpts and the evidence bound
  never act on it.
- The noise floor rests on six pairs per arm (above).
