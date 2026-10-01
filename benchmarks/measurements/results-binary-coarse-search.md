# Binary coarse search: how many candidates the Hamming pass keeps — measurement record

Pre-registration: [`prereg-binary-coarse-search.md`](prereg-binary-coarse-search.md). Harness:
`coarse_search_k.py` at the commit that added it. Run 2026-10-01 on an Apple M5 with 32 GiB, on the
build of that commit, whose runtime code is that of `master` then. Scores:
[`results-binary-coarse-search.json`](results-binary-coarse-search.json); cost:
[`results-binary-coarse-search-cost.json`](results-binary-coarse-search-cost.json).

**Decision: `K'` = 256.** It is the smallest value of the registered grid whose per-seat agreement
with an exact scan of the same positions is at least 95% in all eight cells. The provisional 1,000
it replaces is also above the bound everywhere; 256 meets it at a quarter of the candidates, and the
far ranking on the 1,000,000-row store takes 197 ms instead of 277.

This is a measurement of reach. It says how often the far seats are filled as an exact scan would
fill them. It does not say whether a seated far record answers the question
([design §9](../../docs/BINARY_COARSE_SEARCH_DESIGN.md#9-measurement)).

## Controls

All passed; nothing below is read from a run with a failed control.

| Control | Result |
| --- | --- |
| V1 identity: asking for every far record returns every far position once | pass on all four store / query sets (3 queries each) |
| V2 prefix: asking for `K'` ∈ {32, 1000, 8192} returns the first `K'` of the largest answer | pass, 30 calls per set |
| V3 bridge: the first two records of `far_seats.ranked` at 1,000 equal this harness's | 20/20 per set; largest cosine difference on shared records 1.4e-6 |
| V4 the live supplier returns what the index returns | pass on `s100k` and `s237` |
| V5 no list ran out of its 128 kept entries before two eligible records | 0 truncations |
| Settings took effect; the far ranking was asked for once per recall; the shipped regime wrote nothing | asserted in-process |
| Every supplier answer came from the index | yes |

The exact list filled both seats for 500 of 500 queries in every cell, so every query counts in
every mean.

## Agreement

Per-seat agreement (registered measure), by `K'`. From 4,096 up every cell is 1.000.

| Cell | 32 | 64 | 128 | 256 | 512 | 1,000 | 2,048 | 4,096 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `s100k/q1/shipped` | 0.897 | 0.958 | 0.983 | **0.990** | 0.995 | 0.998 | 1.000 | 1.000 |
| `s100k/q1/production` | 0.873 | 0.951 | 0.981 | **0.989** | 0.995 | 0.998 | 1.000 | 1.000 |
| `s237/q1/shipped` | 0.800 | 0.913 | 0.955 | **0.983** | 0.993 | 0.999 | 1.000 | 1.000 |
| `s237/q1/production` | 0.773 | 0.901 | 0.948 | **0.982** | 0.991 | 0.997 | 0.998 | 1.000 |
| `s1m/q1/shipped` | 0.806 | 0.903 | 0.960 | **0.985** | 0.996 | 1.000 | 1.000 | 1.000 |
| `s1m/q1/production` | 0.783 | 0.893 | 0.953 | **0.982** | 0.993 | 0.997 | 0.998 | 1.000 |
| `s1m/q2/shipped` | 0.836 | 0.905 | 0.948 | **0.978** | 0.988 | 0.994 | 0.998 | 1.000 |
| `s1m/q2/production` | 0.818 | 0.895 | 0.939 | **0.973** | 0.984 | 0.994 | 0.998 | 1.000 |

At 128, three cells fall below the bound (`s237/q1/production` 0.948, `s1m/q2/shipped` 0.948,
`s1m/q2/production` 0.939), so 256 is the decision. At 256 the lowest cell is
`s1m/q2/production`, 0.973, and the lower end of its bootstrap interval is 0.962:

| Cell | agreement at 256 | 95% interval | seats lost per recall | recalls keeping both seats | at 1,000 |
|---|---:|---|---:|---:|---:|
| `s100k/q1/shipped` | 0.990 | 0.982–0.996 | 0.020 | 0.984 | 0.998 |
| `s100k/q1/production` | 0.989 | 0.981–0.996 | 0.022 | 0.982 | 0.998 |
| `s237/q1/shipped` | 0.983 | 0.974–0.991 | 0.034 | 0.968 | 0.999 |
| `s237/q1/production` | 0.982 | 0.972–0.991 | 0.036 | 0.970 | 0.997 |
| `s1m/q1/shipped` | 0.985 | 0.977–0.992 | 0.030 | 0.970 | 1.000 |
| `s1m/q1/production` | 0.982 | 0.973–0.990 | 0.036 | 0.964 | 0.997 |
| `s1m/q2/shipped` | 0.978 | 0.968–0.987 | 0.044 | 0.960 | 0.994 |
| `s1m/q2/production` | 0.973 | 0.962–0.983 | 0.054 | 0.950 | 0.994 |

"Seats lost per recall" is the expected number of the two seats that differ from an exact scan; at
256 it is at most 0.054.

### Against the prediction

- The provisional 1,000 meets the bound on `s100k`: as predicted (0.998).
- "On `s1m` the smallest passing value lies between 1,000 and 8,192": **wrong**. It is 256, and 128
  for the LongMemEval queries alone.
- "`q2` needs no more than `q1`": **wrong**. The other-task queries are the binding set on `s1m`.
- The required `K'` is not monotone in store size: `s237/q1` needs 256 while `s1m/q1` passes at 128.
  The 762,346 rows below `s237` in `s1m` come from other corpora, and their codes rarely sit near a
  LongMemEval query's code. What sets `K'` is how many records compete near the query in Hamming
  distance, not how many rows the store holds.

## Reported, not part of the rule

**Raw agreement** — the first two records of each list, with no eligibility applied — and how many
far records a recall found ineligible (already in the answer, or reached and refused):

| Store / queries | raw 128 | raw 256 | raw 1,000 | ineligible per recall (median, shipped / production) |
|---|---:|---:|---:|---|
| `s100k/q1` | 0.990 | 0.995 | 1.000 | 4 / 7 |
| `s237/q1` | 0.964 | 0.987 | 0.999 | 4 / 7 |
| `s1m/q1` | 0.967 | 0.986 | 1.000 | 4 / 6 |
| `s1m/q2` | 0.965 | 0.984 | 0.996 | 4 / 6 |

**A size rule (exploratory).** The smallest passing grid value per set is 64 (`s100k`), 256
(`s237`), 128 (`s1m/q1`) and 256 (`s1m/q2`). That is not a rule in the store's size, and none is
suggested.

**Cost**, median of 25 queries after 3 warm-up, this machine only. "Far ranking" is
`far_seats.ranked` end to end: the supplier, the by-id read of the candidates' vectors from SQLite,
and the cosine. "Exact, SQLite" reads every far row's vector from the store in scan order;
"exact, contiguous index" multiplies the far slice of the float32 index.

| Store | far rows | far ranking, K' = 256 | supplier (index) | far ranking, K' = 1,000 | exact, SQLite | exact, contiguous index | peak memory, K' = 256 |
|---|---:|---:|---:|---:|---:|---:|---:|
| `c12k` | 2,000 | 1.3 ms | 0.5 ms | 4.0 ms | 6.0 ms | 0.0 ms | 2.2 MB |
| `c15k` | 5,000 | 2.0 ms | 1.0 ms | 5.1 ms | 10.9 ms | 0.3 ms | 3.0 MB |
| `c20k` | 10,000 | 3.0 ms | 1.7 ms | 6.4 ms | 17.9 ms | 0.6 ms | 5.8 MB |
| `c30k` | 20,000 | 5.0 ms | 2.9 ms | 9.1 ms | 32.9 ms | 1.1 ms | 11.3 MB |
| `c50k` | 40,000 | 8.5 ms | 5.5 ms | 14.3 ms | 61.1 ms | 2.2 ms | 22.3 MB |
| `s100k` | 90,000 | 23.9 ms | 12.3 ms | 25.6 ms | 136.8 ms | 4.9 ms | 37.0 MB |
| `s237` | 227,654 | 54.8 ms | 30.9 ms | 60.3 ms | 7,605.2 ms | 12.5 ms | 40.3 MB |
| `s1m` | 990,000 | 196.5 ms | 127.4 ms | 277.3 ms | 15,892.1 ms | 55.1 ms | 58.6 MB |

| Store | K' | far ranking | supplier (index) | supplier (live) | Hamming bytes | re-rank bytes | peak memory |
|---|---:|---:|---:|---:|---:|---:|---:|
| `s100k` | 256 | 23.9 ms | 12.3 ms | 163.9 ms (n=25) | 11.5 MB | 1.0 MB | 37.0 MB |
| `s100k` | 1,000 | 25.6 ms | 12.8 ms | 172.0 ms (n=25) | 11.5 MB | 4.1 MB | 37.0 MB |
| `s100k` | 4,096 | 73.2 ms | 16.5 ms | 201.8 ms (n=25) | 11.5 MB | 16.8 MB | 37.0 MB |
| `s100k` | 16,384 | 254.1 ms | 29.5 ms | 320.1 ms (n=25) | 11.5 MB | 67.1 MB | 141.2 MB |
| `s237` | 256 | 54.8 ms | 30.9 ms | 1,668.5 ms (n=25) | 29.1 MB | 1.0 MB | 40.3 MB |
| `s237` | 1,000 | 60.3 ms | 33.3 ms | 448.3 ms (n=25) | 29.1 MB | 4.1 MB | 40.3 MB |
| `s237` | 4,096 | 172.2 ms | 38.7 ms | 513.5 ms (n=25) | 29.1 MB | 16.8 MB | 40.4 MB |
| `s237` | 16,384 | 486.1 ms | 51.4 ms | 823.9 ms (n=25) | 29.1 MB | 67.1 MB | 141.2 MB |
| `s1m` | 256 | 196.5 ms | 127.4 ms | 16,015.7 ms (n=3) | 126.7 MB | 1.0 MB | 58.6 MB |
| `s1m` | 1,000 | 277.3 ms | 131.5 ms | 15,717.1 ms (n=3) | 126.7 MB | 4.1 MB | 58.6 MB |
| `s1m` | 4,096 | 732.9 ms | 144.4 ms | 14,968.2 ms (n=3) | 126.7 MB | 16.8 MB | 58.7 MB |
| `s1m` | 16,384 | 2,490.1 ms | 253.8 ms | 15,205.7 ms (n=3) | 126.7 MB | 67.1 MB | 141.2 MB |

| Store | rows | coarse index build | coarse index size | contiguous index build | contiguous index size |
|---|---:|---:|---:|---:|---:|
| `s100k` | 100,000 | 1.0 s | 19.0 MB | 1.3 s | 414 MB |
| `s237` | 237,654 | 3.0 s | 45.2 MB | 7.5 s | 984 MB |
| `s1m` | 1,000,000 | 16.0 s | 190.0 MB | 46.2 s | 4,139 MB |

What the cost says:

- **Against an exact scan read from SQLite, the coarse path is faster at every size measured**, from
  2,000 far rows (1.3 ms against 6.0) to 990,000 (197 ms against 15.9 s). There is no store size here
  where the coarse index costs more than an exact read of the same rows from the store.
- **Against an exact scan of the contiguous float32 index, the coarse path is slower at every size
  measured** (990,000 far rows: 197 ms against 55). On this machine the 4.1 GB float32 index sits in
  the page cache. What the coarse path buys is residency: a 190 MB file and 59 MB of peak memory per far
  ranking at 1,000,000 rows, against a 4.1 GB file that must be resident for the exact scan to be fast.
  That is the split the scale design draws between the contiguous index (latency) and the one-bit
  index (memory), and this measurement does not move it.
- On `s1m` the supplier is two thirds of the far ranking at 256 (127 ms of 197): the Hamming pass over
  127 MB of codes. Above about 1,000 the re-rank's by-id reads dominate (2.5 s at 16,384).
- Without a coarse index the live supplier takes about 16 s per query on `s1m`. A missing index on a
  store that size makes the far seats unusable, not slow.
- **Cold cache in two `s237` figures.** The SQLite scan (7.6 s) and the live supplier at 256
  (1.7 s, against 0.45 s at 1,000 measured next) were the first reads of that store's file, and
  three warm-up queries did not warm a 3 GB file. Read them as cold, not as the warm figure.
- The recalls of the eligibility step, with the far ranking replaced by a precomputed list, took a
  median of 724–976 ms on `s1m` (lexical arms included). Adding the far ranking at 256 brings a
  recall on a 1,000,000-row store to about one second on this machine. Reported, not measured as one
  quantity.

## What follows

`coarse_search.CANDIDATES` (formerly `K_PROVISIONAL`) is 256. A test ties the constant to the
decision recorded in the JSON next to this file, so the two cannot drift apart. Both switches stay
off by default.

## Re-deriving

From the repository root, with the LMEB data and its bge-m3 embedding cache under `~/lmeb`:

```
uv run python benchmarks/measurements/coarse_search_k.py run --workdir <dir>
uv run python benchmarks/measurements/coarse_search_k.py score --workdir <dir> --json <file>
uv run python benchmarks/measurements/coarse_search_k.py cost --workdir <dir> --store <store> --k 256,1000,4096,16384
```
