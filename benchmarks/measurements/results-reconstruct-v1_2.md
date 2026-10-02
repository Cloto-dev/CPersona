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
- **Reader re-measurement with gpt-6-luna
  ([registration](prereg-reconstruct-v1_2-reader-6luna.md)): the rule is met.**
  The median payload ratio was 0.697 (95% interval 0.643 to 0.795) against a
  pooled noise floor of 0.062. Correct answers were 29 against 31, at the edge
  of the guard (at most two fewer).

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

## Re-measurement with gpt-6-luna

Registration: [prereg](prereg-reconstruct-v1_2-reader-6luna.md) (`f12c7af`).
The same store, questions, arms, tools, prompts and judge (`gpt-5.6-luna`); the
reader is `gpt-6-luna`, and the noise floor comes from the same twelve questions
repeated in both arms. 96 reader calls and 96 judge calls; none failed.

| | A: 2.6.3a1 | B: v1.2 |
| --- | --- | --- |
| Correct answers | 31 / 36 | 29 / 36 |
| Payload characters, total | 836,445 | 625,782 |
| Payload characters, median per question | 18,241 | 15,831 |
| of which search responses | 726,272 | 481,767 |
| of which expansions | 110,173 | 144,015 |
| Reader input tokens, total | 2,557,542 | 2,175,094 |
| of which cached | 1,847,552 | 1,595,648 |
| Payload characters per correct answer | 26,982 | 21,579 |
| Input tokens per correct answer | 82,501 | 75,003 |
| `search` calls | 61 | 60 |
| `expand` calls (records: whole, ranged) | 29 (5, 35) | 23 (9, 24) |

| Condition | Registered | Measured | Met |
| --- | --- | --- | --- |
| Median of r = B / A | at most 0.85 | 0.697 | yes |
| 95% bootstrap interval of the median | below 1.0 | 0.643 to 0.795 | yes |
| Median reduction against the noise floor | larger | 0.303 against 0.062 | yes |
| Correct answers | B at least A − 2 | 29 against 31 | yes, at the edge |

The floor held still this time: 0.059 over arm A's twelve pairs, 0.072 over arm
B's, 0.062 pooled. B's payload was the smaller on 27 of the 36 questions (ratio
0.192 to 2.204). The median per-question ratio of input tokens was 0.903 (95%
interval 0.781 to 0.989), and the total fell by 15.0%.

**The two answers B lost.** The arms disagreed on six questions: A alone right
on four, B alone right on two. Two of the four did not hold on repetition: on
one, B's second run was right; on another, B's second run was right and A's
was wrong. One has no repetition. The fourth held: on a preference question
("Can you recommend a show or movie for me to watch tonight?") both of A's runs
were right and both of B's wrong. In all four runs the first response held the
answer session among its first four items, where quotes keep their full size,
and both of B's quotes carried the evidence (stand-up, storytelling, Netflix).
A's reader expanded that record both times; B's did not, and recommended other
genres. The quote was not cut away. Six questions a side
cannot tell a reader's habit from an effect, and the guard is registered as a
guard against gross harm, not as evidence of equivalence.

| Type | A (correct, payload) | B (correct, payload) |
| --- | --- | --- |
| knowledge-update | 5, 112,792 | 5, 99,091 |
| multi-session | 4, 96,178 | 5, 95,127 |
| single-session-assistant | 6, 100,612 | 6, 69,650 |
| single-session-preference | 4, 196,663 | 3, 168,764 |
| single-session-user | 6, 124,957 | 6, 57,505 |
| temporal-reasoning | 6, 205,243 | 4, 135,645 |

**Against the first reader.** `gpt-6-luna` read about half of what
`gpt-5.6-luna` read in either arm (A: 836,445 against 1,619,017 characters) and
searched about half as often (61 against 112), with more of its expansions
ranged. The reduction v1.2 brings is of similar size under both readers (median
ratio 0.697 and 0.624); what differed between the runs was the floor it had to
clear.

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
| Reader, gpt-6-luna | Payload per question, median ratio | 1 | 0.697 | −30.3% | registered, **met** |
| Reader, gpt-6-luna | Correct answers, of 36 | 31 | 29 | −2 | registered guard, passed at its edge |
| Reader, gpt-6-luna | Input tokens per question, median ratio | 1 | 0.903 | −9.7% | exploratory |
| Reader, gpt-6-luna | Payload characters per correct answer | 26,982 | 21,579 | −20.0% | exploratory |
| Pack, development | Evidence shown, of 193 | 127 | 136 | +7.1% | exploratory (design data) |

## Limits

- One private pack, and `rrf` with confidence off only; the configuration with
  `rsf` and confidence on is not covered.
- Thirty-six questions. The correctness guard detects only a large loss; 33
  against 32, and 29 against 31, are evidence neither that answers improve nor
  that they worsen.
- One corpus with one row per session: bundling, excerpts and the evidence bound
  never act on it.
- The first run's noise floor rests on six pairs per arm on different questions
  (above); the re-measurement's on twelve per arm on the same questions.
- Two readers from one family. A weaker reader, which may not read past a cut
  quote, is not covered.
