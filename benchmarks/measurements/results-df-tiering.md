# Document-frequency tiering for the FTS5 arm: measured, and not adopted

Instrument: `df_tiering_grid.py`. Corpus: a consistent read-only copy of a
production database (2,965 memories), taken with `VACUUM INTO` and opened with
`mode=ro`. Query set: 233 distinct `recall` queries, taken from one operator's
own call history rather than written for the benchmark -- 256 calls, deduplicated.
Isolation: one agent, no project or channel filter.

## What was being bought

Profiling the recall path attributed the non-vector time almost entirely to one
statement in the lexical arm, `MATCH ... ORDER BY rank LIMIT 10`, and showed the
cost is bm25 evaluation over every matching row (~2.2 us per matching row at
100k rows), unaffected by the JOIN and the isolation predicate. Two candidate
mechanisms had already been measured and rejected: a smaller disjunction does
not shrink the expensive queries and breaks the answer for CJK ones, and a
recency window cannot be applied because the lexical arm is the only retriever
that reaches past the vector arm's scan window.

That left df tiering: drop the terms that select the most rows, on the argument
that a row matched only by common terms ranks low anyway -- so the rows most
expensive to score are the ones contributing least. Reach is untouched: every
row remains a candidate.

## The rule, and the df it needs

    keep terms with df <= cutoff * rows; if fewer than K remain, restore the
    dropped ones in ascending df order until K remain.

`_build_fts_query` keeps every ASCII run of 3+ characters whole, so those terms
are *phrases* over a trigram index and have no vocabulary entry of their own.
Asking `fts5vocab` for one answers 0, which a tiering rule reads as "rarest
possible" -- the opposite of the truth for the terms that cost the most. An
earlier run of this experiment did exactly that, and so measured a different
rule than the one it described. Two sources are compared here:

| source | how | usable at run time |
|---|---|---|
| exact | `COUNT(*) ... MATCH '"term"'` | no -- one postings walk per term |
| proxy | min over the term's own trigram dfs, via `fts5vocab` in TEMP | yes |

The proxy is an upper bound: a document containing the phrase contains all of
its trigrams. It tracks the exact source closely enough that the two pick the
same terms at the cutoff that matters.

## Result

Cost is rows selected, summed over the 233 queries (183,371 at full). Fidelity
is the overlap of the top 10 the production statement returns, before and after.

| df | cutoff | K | matched | gain | top10 unchanged | >= 8/10 | worst |
|---|---:|---:|---:|---:|---:|---:|---:|
| — | full | — | 183371 | 1.00x | 233/233 | 233/233 | 10/10 |
| exact | 5% | 2 | 40759 | 4.50x | 74/233 | 141/233 | 0/10 |
| exact | 10% | 2 | 70389 | 2.61x | 138/233 | 199/233 | 3/10 |
| exact | 20% | 6 | 133279 | 1.38x | 204/233 | 231/233 | 5/10 |
| proxy | 20% | 6 | 131393 | 1.40x | 201/233 | 230/233 | 5/10 |
| proxy | 30% | 2 | 115612 | 1.59x | 190/233 | 233/233 | 8/10 |
| proxy | 30% | 6 | 141049 | 1.30x | 212/233 | 233/233 | 8/10 |

**No setting leaves every answer intact.** A 30% cutoff is the fidelity-safe
line: at any K from 2 to 6 every query keeps 8 of its 10 hits and 232 of 233
keep their first hit. Inside that line, K trades cost against exactness --
1.59x with 190 answers untouched, or 1.30x with 212. Below it the curve turns
sharply: 2.6x costs 95 changed answers, and 4.5x rewrites two thirds of them,
some to nothing in common.

The cost model was checked against a clock rather than assumed: over the same
233 queries, a 1.30x cut in matched rows produced a 1.22x cut in measured time
(the residual is per-query fixed cost, which is proportionally heavier on a
2,965-row corpus than on the 100k one the model came from).

## Why it does not pay more

A CJK query decomposes into eight to eleven trigrams whose **union** covers 60%
of the corpus while no single term is common. Taking the six most expensive
queries apart, the highest-df term selects ~1,000 rows and the remaining terms
double that. A per-term threshold can only ask "is this term common?", so it
cannot see what makes the union large; it trims the tail of the union, never the
head. The worst query in the set falls only from 2,019 rows to 1,746.

A rule shaped like the union was measured too -- take terms in ascending df
while their df budget lasts, since the sum of dfs bounds the union -- and it
loses to the per-term cutoff at matched cost: 1.42x with 186 answers intact
where the cutoff rule gives 1.38x with 204.

## Status

Not adopted. 1.30x is not obviously worth moving the top 10 of 9% of queries in
a memory system, and this measurement can only show that answers *change* --
whether the change is degradation is a benchmark question, and the decision waits
on it.

The query set is one operator's, and CJK-heavy. Growing it is unlikely to move
the conclusion, because the structural reason above is a property of the trigram
tokenizer rather than of any particular query.
