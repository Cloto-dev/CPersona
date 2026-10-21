# Results — the keyword seat on a reconstruct call without a mode, on LoCoMo (bug-808)

A search-only check, no model called. It asks whether giving a `reconstruct` call that names no
mode the one keyword seat a mode already has makes the answer show LoCoMo's evidence sessions less
often. Every number below is in
[results-keyword-seat-default-reconstruct-locomo.json](results-keyword-seat-default-reconstruct-locomo.json)
(the base is labelled `A` there, this change `C`); the script is [`benchmarks/omnimemeval/keyword_seat_locomo.py`](../omnimemeval/keyword_seat_locomo.py).

## Why

bug-808: on a small store before calibration, the pool-size heuristic gate keys a row only the
keyword arms found on its fused rrf score, and one arm's vote (1/61) is below that threshold on
pools of about 30 rows or fewer. Only a mode asked for the keyword seat that holds such rows, so a
call without a mode could not return the one note holding an identifier the question named exactly.
Measured with the agent plugin in a five-row store: `reconstruct("hosting invoice number")` did not
return the note holding invoice INV-4471, ranked first by the keyword arm (0.0164 against 0.0203).

## Setup

- Store: LoCoMo as OmniMemEval's ingestion builds it with the cpersona adapter (one record per
  session, one agent per conversation of 19 to 32 records, bge-m3), the store the published LoCoMo
  results were searched on. A clone per run, since recall updates recall counts.
- Questions: the 1,540 of categories 1-4 (1,536 with evidence); `reconstruct` in its default, `pro`
  and `lite` modes.
- Configurations: `pub` keeps the store's calibration file (per-agent vector thresholds; the fused
  gate was never calibrated, so recall used the heuristic gate); `fresh` has none and does no startup
  calibration, as a new user's store.
- Base: the 2.6.12 release candidate (`93da745`). New: this change.
- Measure: the share of a question's evidence sessions among the head records of its items.
- Rule, fixed before the run: the change goes in when all six configuration-mode pairs have a paired
  95% bootstrap interval (10,000 resamples over questions, seed 20261031) of new − base above −0.01.

## Instrument check

- `pro` and `lite` already held the seat, so they must come back unchanged: all 6,144 searches
  returned the same head refs in the same order.
- A seat only adds: in all 3,072 default calls the new answer held every record the base returned.

## Result: all six pairs pass

| Configuration / mode | Base | New | New − base (95%) | Questions better / worse |
| --- | ---: | ---: | --- | --- |
| pub / default | 0.887 | 0.915 | +0.027 (+0.020 to +0.035) | 59 / 0 |
| pub / pro | 0.962 | 0.962 | 0 | 0 / 0 |
| pub / lite | 0.913 | 0.913 | 0 | 0 / 0 |
| fresh / default | 0.894 | 0.914 | +0.020 (+0.014 to +0.026) | 47 / 0 |
| fresh / pro | 0.965 | 0.965 | 0 | 0 / 0 |
| fresh / lite | 0.913 | 0.913 | 0 | 0 / 0 |

The cost, in the default call only: the seat was filled on 1,355 (`pub`) and 1,298 (`fresh`) of the
1,536 questions, with an evidence session 59 and 47 times. The answer held 12.4 items instead of
11.5, and its head records 8.7% (`pub`) and 8.2% (`fresh`) more characters. LoCoMo's records are
whole sessions (the seated ones averaged 3,598 characters); stores of short notes were not measured.

This is evidence reaching the answer, not answer accuracy: no reader was run, and no accuracy is
claimed. `pro` and `lite`, the modes the published LoCoMo results used, return exactly what they did.

## The first attempt, rejected by the same check

Capping the heuristic threshold at one vote for every keyword-only row (bug-509's cap, widened)
showed the evidence sessions less often in all six pairs: −0.016, −0.011, −0.034 (`pub` default,
`pro`, `lite`) and −0.024, −0.013, −0.035 (`fresh`), every upper bound below zero. Traced on five
of the questions it lost: the rows it let through were the evidence itself, which the block
reservation had been returning while the gate refused them; once admitted they ranked below the
count, were cut, and no longer qualified for the reservation. The two conversations of 31 records or
more, where the cap cannot act, came back identical (1,812 searches), so the instrument held.

## Reproduce

```bash
export CPERSONA_LOCOMO_STORE=<store.db> CPERSONA_LOCOMO_DATA=<locomo10.json>
export CPERSONA_EMBEDDING_URL=<the bge-m3 endpoint the store was embedded with>
python keyword_seat_locomo.py search base <checkout of 93da745> pub out/
python keyword_seat_locomo.py search new <checkout of this change> pub out/
# the same two for fresh, then:
python keyword_seat_locomo.py analyze out/ base new 20261031
```
