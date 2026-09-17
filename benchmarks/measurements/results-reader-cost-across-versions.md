# Results: reader cost across 2.4.41, 2.5.12b4 and the 2.6 line

Registration: [prereg](prereg-reader-cost-across-versions.md) (`2ce1961`, threshold
amended in `13a532f` before the first reader call). 54 reader calls, none failed.

**Read this first.** The 2.6 arm ran at commit `a3843b1`, before the compact
envelope and the `expand` argument. It is reconstruct v1.1 *as first built*, which
the [reader study](results-reconstruct-v1_1-reader.md) found no cheaper than
recall and which a later change made cheaper. This table therefore does not
describe 2.6.0a3 as released; it was not re-run, and the two studies use
different stores, so their figures are not to be combined.

## What each line keeps and returns (no model)

Each store was written by its own version's `store` with its defaults: 840
sessions for the 18 questions.

| Version | Sessions over the write bound | Answer turns surviving in stored text | Every answer session returned | First-search characters, 18 questions |
| --- | --- | --- | --- | --- |
| 2.4.41 (bound 2,000) | 805 | **14 / 31** | 16 / 18 | 144,797 |
| 2.5.12b4 (bound 16,000) | 153 | 31 / 31 | 18 / 18 | 52,246 |
| 2.6 line at `a3843b1` (bound 16,000) | 153 | 31 / 31 | 18 / 18 | 58,163 |

2.4.41 keeps the first 2,000 characters of a record, so more than half the turns
that hold an answer were never stored, and it returns whole rows, so one search
costs nearly three times what it costs later.

## With a reader

| | 2.4.41 | 2.5.12b4 | 2.6 line at `a3843b1` |
| --- | --- | --- | --- |
| Correct answers | **6 / 18** | 15 / 18 | 13 / 18 |
| Payload characters, total | 1,790,473 | 855,225 | 807,739 |
| Reader input tokens, total | 3,037,825 | 2,324,783 | 2,433,631 |
| Characters per correct answer | 298,412 | 57,015 | 62,133 |
| Input tokens per correct answer | 506,304 | 154,985 | 187,202 |
| `search` calls | 303 | 159 | 156 |
| Records expanded, of which by range | none offered | 36, 0 | 28, 12 |

## What may be said

Ratio of cost per correct answer, 95% bootstrap interval over questions, the same
resample applied to both arms.

| Comparison | Characters | Input tokens | Statement |
| --- | --- | --- | --- |
| 2.5.12b4 / 2.4.41 | 0.19 (0.06 to 0.43) | 0.31 (0.09 to 0.75) | **2.5 costs less per correct answer** |
| 2.6 at `a3843b1` / 2.4.41 | 0.21 (0.06 to 0.54) | 0.37 (0.10 to 1.01) | less in characters; tokens not shown |
| 2.6 at `a3843b1` / 2.5.12b4 | 1.09 (0.70 to 1.76) | 1.21 (0.72 to 2.04) | no ranking |

The reader on 2.4.41 searched 303 times: it kept looking for text that had been
cut away on the way in. Reading little is not the goal; a line that stores less
cannot answer what it did not keep, which is why the measure is cost per correct
answer.

## Limits

Eighteen questions rank large differences only. Retrieval differs between
versions as well as the read path, so each figure is the end-to-end cost of a
version, not the effect of one mechanism; the no-model table is there to separate
them. One reader model, one corpus, sessions far longer than a typical stored
memory, which is the case the write bound and the preview tier were built for.
