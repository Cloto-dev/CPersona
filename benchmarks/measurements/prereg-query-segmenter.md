# Query segmenter: leaving a request's wording out of the keyword arms' Japanese phrases

Registered before any test question of this study was searched with the segmenter. The commit that
adds this file is the evidence of the order.

## Question

The keyword arms cut every Japanese or Chinese run of a query into all of its overlapping
three-character pieces and OR them, and FTS5's bm25 adds up the phrases a row holds. The wording of a
request therefore votes: `bug-191 について教えて` ("tell me about bug-191") is one phrase for the
identifier and five for `について教えて`, and rows holding that wording outscore the one row that names
the identifier. `CPERSONA_QUERY_SEGMENTER=morph` cuts each run into morphemes first (SudachiPy 0.7.0,
split mode C, sudachidict-core 20260723.1), leaves function words out (particles, auxiliary verbs,
symbols, conjunctions, interjections, adnominals, pronouns, adverbs, and words marked as not
independent) and cuts the runs of the remaining words into trigrams as before. ASCII terms are built as
they always were, so an identifier stays one phrase and an English query is unchanged. The default,
`trigram`, is the shipped builder.

Two questions: does it bring more evidence into `reconstruct`'s response when an agent asks its memory
about an identifier, and does it harm the response to the natural questions of a pack of real agent
memory? A third is a check: an English benchmark must not change at all.

## Development (done before this registration)

All on development questions only, search only, no model call, at commit `082829e` (the switch).

- **The keyword arms alone.** On 80 identifier questions built from the private pack's records (each
  names a rare `bug-NNN` or a short commit hash held by one to three records; every record holding it is
  evidence), phrased by five fixed templates, the evidence records in the top 20 of their arm rose from
  120 to 149 of 149. On the pack's 150 development questions, from 127 to 143 of 193 (24 entered, 8
  left). The rows a question matches fell from a median of 2,587 to 1,670.
- **`reconstruct`'s response** (count 10, default budget, block reach on; evidence entries reached; the
  one `trigram` run repeated at the switch's commit, development questions under `rrf` with no seat,
  equals the run made before the switch existed on every field but timing):

  | Questions | Fusion, keyword seats | `trigram` | `morph` |
  | --- | --- | --- | --- |
  | Identifier, five templates (149 entries) | `rrf`, 0 | 27 | 28 |
  | Identifier, five templates | `rrf`, 2 | 39 | 133 |
  | Identifier, five templates | rsf + confidence, 0 | 56 | 130 |
  | Private pack, development (193 entries) | `rrf`, 0 / 2 | 157 / 167 | 159 / 169 |
  | Private pack, development | rsf + confidence, 0 | 153 | 159 |

  Under `rrf` a row only the keyword arms found cannot pass the quality gate
  ([prereg-keyword-seats.md](prereg-keyword-seats.md)), so the segmenter's rows reach the response
  only through the keyword seats; under rsf the gate can pass them.
- A second variant (keeping the shipped trigrams that have two characters inside nouns) reached 102
  and 98 in the two identifier configurations above and was dropped. No other variant is registered.
- Cost of the segmenter: 0.032 ms per question against 0.008 ms, 16 ms to load the dictionary, 80 MiB
  more resident memory, and 197 MB installed. Whether it becomes a dependency is decided after the test
  and is not part of it.

## Test

All runs at the commit that adds this file, `reconstruct`, count 10, default budget, block reach on,
`trigram` and `morph`, with the private pack's prepared databases and the shared embedding server.
Every arm runs anew, the `trigram` arms included.

### Identifier questions (primary)

- 80 new identifier questions built as the development ones, excluding every development identifier,
  40 of each kind, with the same five templates. They name the pack's records and cannot be published;
  the result is reported in counts.
- **Rule P1.** Under `rrf` with two keyword seats, per question, the evidence entries reached with
  `morph` minus those with `trigram`. The lower bound of the two-sided 95% t interval of the mean
  difference (79 degrees of freedom) above 0 (strictly) means: when an agent asks about an identifier,
  `morph` brings more evidence into the response under the default fusion with two seats.
- **Rule P2.** The same under rsf with confidence and no seat.
- Reported beside them, with no claim: `rrf` with no seat, rsf with two seats, and seats under `morph`.

### The private pack's natural questions (harm detection)

- Its 150 test questions, under `rrf` and rsf with confidence, each with no seat and with two.
- **Rules H1 to H4.** Per question, `morph` minus `trigram`. The upper bound of the two-sided 95% t
  interval (149 degrees of freedom) below 0 means: `morph` lowered the evidence reached in that
  configuration. Otherwise: no harm detected. This does not claim non-inferiority.
- **Rule S1.** Under `rrf` with `morph`, two seats minus none, lower bound above 0: under the
  segmenter, the keyword seats bring more evidence. These test questions were searched in the
  keyword-seats test, with `trigram` only.

### LongMemEval-S (no change expected)

- The 400 test questions, on copies of the published store with its calibration file, with the
  published search's settings and `morph`: with no seat, compared with the published contexts; with two
  seats, compared with the two-seat contexts of the keyword-seats test
  ([`query_segmenter_check.py`](../omnimemeval/query_segmenter_check.py)).
- **Rule.** Equal for all 400 in both: `morph` does not change what LongMemEval-S sends to the answer
  model, and no answer is made. If any question differs, the study stops there for this benchmark and
  reports which; answering them is a separate decision, not part of this registration.

## What may be said

- The rules as written above, each about its own configuration.
- "No harm detected", never "no harm".

## What it does not say

- Nothing about a gain on natural questions (the development intervals include 0), about speed (the
  rows matched fall, but no time is registered), about other segmenters, dictionaries or split modes,
  other counts or budgets, or `recall`'s response.
- Nothing about the development questions, which chose `morph`.

One run of each. A rerun is made only for an infrastructure failure, and is reported. Nothing in the
construction or the settings changes after any test result is seen.
