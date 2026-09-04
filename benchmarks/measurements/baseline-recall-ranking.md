# Baseline: which rows recall returns, and in what order

Frozen file: `rank-baseline-nonfinite-read-path.json`. Instrument:
`rank_identity.py` over `scan_window_ab.py`'s arms. Taken 2026-09-04 at
`1081309`.

This is not a result. It is a **record of the present**, taken so that a later
change to the recall read path can be held against something. It exists because
the records that were already here cannot answer the question: `lmeb_results`
keeps summary scores, and a mean can hold still while the rows underneath it are
shuffled or swapped.

## What is in the file

480 queries — the twelve rotations of the scene-blocked LongMemEval layout, 40
evaluated queries each — and for every one of them, the ordered ids a caller
received.

| Property | Value |
|---|---|
| Corpus | LongMemEval, 237,654 documents, scene-blocked, seed `20260903` |
| Regime | shipped: `rrf`, fused gate on, autocut on, confidence **off**, floor 0.30, window 10,000 |
| Per query | the top 10 ids, in order |
| Build | `1081309` |

The regime is the shipped one, confidence included: this file is meant to answer
"did a change move what a default install returns", so it is taken at the
default rather than at the configuration that makes an effect easiest to see.

## Why ids rather than scores

A float comparison fails for reasons that are not regressions. The same vectors
summed in a different order move the last bits, and the ranking does not change.
Ids are what a caller acts on, and two runs that return the same rows in the same
order are the same behaviour whatever the arithmetic did on the way.

The comparison reports three levels, because they mean different things: an
order change over the same set (a tie broken differently is enough to do this),
a set change (a row entered or left), and a query- or rotation-set mismatch
(the two runs are not over the same layout, which makes every other number
meaningless — it fails hard and separately).

## The control that makes the recipe usable

Keeping twelve 3.1 GB corpora would be the strongest way to re-run this: an arm
against the identical file cannot differ because of the build. Instead the build
was shown to be reproducible — **rotation 0 was built twice from the same seed
and returned identical ids on all 40 queries** — so the file above plus the
recipe below is enough, and the corpora are disposable.

## Reproducing it

One rotation at a time; each build takes about 160 s and each arm about 14 s.
Build rotation `N` into a scratch directory with `scan_window_ab.py build`
(`--rotation N`, writing its own plan file), then run one arm over it with
`scan_window_ab.py arm` at `--window 10000 --reach 0 --far-limit 0 --limit 10`,
writing `arm-r<N>.json`. Repeat for rotations 0 through 11 into one directory.

Then hold the directory against the frozen file:

```
python benchmarks/measurements/rank_identity.py compare \
    --baseline benchmarks/measurements/rank-baseline-nonfinite-read-path.json \
    --arms <that directory>
```

`compare` exits 0 only when every query returned the same rows in the same
order, so it can be used as a gate.

## The gate was tested by breaking it

A comparison that only ever passes is not evidence. Against a copy of the
frozen file:

| Mutation | Reported |
|---|---|
| none (the same run) | PASS, 40/40 identical |
| top two ids of one query swapped | FAIL — "order differs, set same: 1" |
| one returned id replaced | FAIL — "set differs: 1", naming the row that left and the one that arrived |
| an arm captured at window 200,000 | exit 2 — "REGIME MISMATCH", refusing to compare |

## What this baseline does not cover

The scan window is 10,000 and the corpus is 237,654 rows, so the vector arm sees
the newest 4% and the rest of the corpus reaches the result through the lexical
arms. A change that only affects rows below the window would not move these
numbers. That is a property of the shipped default rather than of this file, but
it bounds what a PASS here is worth.
