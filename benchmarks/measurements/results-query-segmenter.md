# Results: the query segmenter against trigram

Registration: [prereg-query-segmenter.md](prereg-query-segmenter.md), committed in `8612640` before any
test question was searched with the segmenter. Every rule was applied once, as registered, by the
analysis script whose hash the registration records; no run was repeated.

## What the rules say

- **P1 met (identifier questions, `rrf`, two keyword seats).** Evidence entries reached on the 80
  held-out identifier questions rose from 42 to 130 of 141 with `morph`: +88, +1.100 per question,
  two-sided 95% t interval +0.956 to +1.244. 67 questions reached more, none fewer.
- **P2 met (identifier questions, rsf with confidence, no seat).** From 50 to 121 of 141: +71, +0.888
  per question, 95% +0.733 to +1.042. 59 questions reached more, one fewer.
- **No harm detected on the private pack's 150 natural test questions** (rules H1 to H4), in any of the
  four configurations:

  | Fusion, keyword seats | `trigram` | `morph` | Per question, 95% t interval | More / fewer |
  | --- | --- | --- | --- | --- |
  | `rrf`, 0 | 168 / 196 | 169 | −0.023 to +0.036 | 1 / 1 |
  | `rrf`, 2 | 173 | 174 | −0.033 to +0.046 | 3 / 3 |
  | rsf + confidence, 0 | 162 | 162 | −0.050 to +0.050 | 7 / 7 |
  | rsf + confidence, 2 | 162 | 162 | −0.050 to +0.050 | 7 / 7 |

  This is not a claim of non-inferiority.
- **S1 met.** Under `rrf` with `morph`, two keyword seats raised the private pack's test questions from
  169 to 174 entries: +5, +0.033 per question, 95% +0.0043 to +0.0624, no question fewer. (With
  `trigram` the keyword-seats test had found 168 to 173.)
- **LongMemEval-S unchanged.** With `morph`, all 400 test contexts equal the published ones with no
  seat, and the keyword-seats test's with two. None of the 400 questions holds a Japanese or Chinese
  character, so the equality follows from the construction; the check shows that nothing else changed.
  No answer was made.

## Reported beside them, with no rule

| Identifier questions (141 entries) | `trigram` | `morph` |
| --- | --- | --- |
| `rrf`, no seat | 27 | 27 |
| `rrf`, two seats | 42 | 130 |
| rsf + confidence, no seat | 50 | 121 |
| rsf + confidence, two seats | 58 | 121 |

Shown characters per response moved by less than 3% between `trigram` and `morph` in every
configuration but one (for example 6,462 and 6,281 on the identifier questions under `rrf` with two
seats; 5,719 and 5,702 on the private pack under `rrf` with no seat). The exception is the identifier
questions under rsf with two seats, 6,514 and 5,885 (−9.7%), where fewer rows took a seat (14.00 and
12.40 rows per response).

## Run

- Commit `8612640`, the private pack's prepared databases, the shared embedding server, SudachiPy 0.7.0
  with sudachidict-core 20260723.1, `CPERSONA_LEXICAL_ENGINE=fts5`. Sixteen runs (two question sets ×
  two fusions × two seat counts × two segmenters), every one at that commit, each restoring its
  calibration and mapping every returned row. The `trigram` runs reproduce the keyword-seats test's
  counts on the private pack (168, 173, 162, 162).
- LongMemEval-S: copies of the published store with its calibration file and the published search's
  settings, two searches of the 400 test questions with `morph` (no seat, two seats), checked by
  [`query_segmenter_check.py`](../omnimemeval/query_segmenter_check.py).

## Reading

Under the default fusion neither change alone moves identifier lookups: two seats with `trigram` take
them from 27 to 42 entries, `morph` with no seat leaves them at 27, and the two together reach 130 of
141. The seats let rows only the keyword arms found into the response; `morph` makes the rows the
keyword arms rank first be the ones that name the identifier, instead of the ones that repeat the
request's wording. On the private pack's natural questions the segmenter changed nothing measurable.

Whether `morph` or the seats become the default, and whether SudachiPy becomes a dependency (197 MB
installed), are not decided by these rules.
