# Results — a floor for the reserved rows

Pre-registration: `prereg-reservation-floor.md`, written before any number here
was read. Instrument: `reservation_floor.py` (M0–M2) over the Track A/B
embedding cache, plus the completed frozen-replay dumps (M3). No encoder ran
and no benchmark was replayed: every vector was already on disk.

22 tasks, 24,244 queries, 9,947,852 non-answer pairs and 105,190 gold pairs per
model, three models, seed 1344.

**Verdict: no floor.** The rule's first condition fails on a shipping model.

## The rule, against the numbers

| Registered condition | Reading | Holds? |
|---|---|---|
| 1. No gold pair at `s <= 0`, both shipping models | jina-v5-nano: **2** of 105,190 (MemBench −0.0013, NovelQA −0.0183). bge-m3: 0, lowest gold 0.1645 | **No** |
| 2. `P(s <= 0) < 1 %` among non-answers, both shipping models | jina-v5-nano 0.420 %, bge-m3 0.000 % | Yes |

Condition 1 is what decides it, and it decides against the floor: a floor at
zero would delete two answers that the reservation can currently return. Two in
105,190 is a small number, and the rule was written to be indifferent to how
small it is — a floor that removes answers buys tidiness with the one thing this
path exists to protect, which is having something to return when the admission
floor has left nothing.

Condition 2 held at the model level and is reported at task level below, where
jina crosses 1 % on 3 of 22 tasks (MLDR 11.2 %, LooGLE 5.9 %, LongMemEval
5.7 %). That does not change the verdict, but it is the second reason not to
write the constant down: see "the models disagree" below.

## M0 — the vectors are unit-norm

| Model | dim | ‖v‖ min | ‖v‖ max |
|---|---:|---:|---:|
| jina-v5-nano | 768 | 0.99999988 | 1.0000000 |
| bge-m3 | 1024 | 0.9997594 | 1.0004561 |
| MiniLM-L6-v2 | 384 | 0.9997635 | 1.0004668 |

So the pipeline's dot product is a cosine to within 5e-4, and zero is
orthogonality rather than an arbitrary point on an unnormalised scale. The 5e-4
matters only for pairs within that distance of zero; bge-m3 has **none** within
1e-3 of zero, jina 3,574 of 9.9 M (0.036 %), MiniLM 24,641 (0.248 %).

## M1 / M2 — per task

`P(s<=0)` is over non-answer pairs; `gold` is the count of gold pairs at or
below zero and the lowest gold similarity in that task.

| Task | universe (median) | jina P(s≤0) | jina gold ≤0 / min | bge-m3 P(s≤0) | bge-m3 gold ≤0 / min | MiniLM P(s≤0) | MiniLM gold ≤0 / min |
|---|---:|---:|---:|---:|---:|---:|---:|
| EPBench | 196 | 0.00002 | 0 / 0.1213 | 0.00000 | 0 / 0.2809 | 0.01769 | 82 / −0.0753 |
| KnowMeBench | 8,423 | 0.00005 | 0 / 0.1319 | 0.00000 | 0 / 0.3072 | 0.00437 | 0 / 0.0218 |
| LoCoMo | 629 | 0.00040 | 0 / 0.0416 | 0.00000 | 0 / 0.2325 | 0.01840 | 0 / 0.0194 |
| LongMemEval | 476 | 0.05688 | 0 / 0.0383 | 0.00000 | 0 / 0.2694 | 0.25759 | 4 / −0.0279 |
| REALTALK | 476 | 0.00421 | 0 / 0.0077 | 0.00000 | 0 / 0.2517 | 0.03931 | 5 / −0.1037 |
| TMD | 676 | 0.00001 | 0 / 0.0497 | 0.00000 | 0 / 0.2502 | 0.00190 | 100 / −0.0663 |
| MemBench | 163 | 0.00547 | **1 / −0.0013** | 0.00000 | 0 / 0.2406 | 0.01807 | 6 / −0.0303 |
| ConvoMem | 46 | 0.00264 | 0 / 0.0964 | 0.00000 | 0 / 0.3252 | 0.06724 | 3 / −0.0413 |
| QASPER | 65,300 | 0.00107 | 0 / 0.0706 | 0.00000 | 0 / 0.2116 | 0.04924 | 1 / −0.0075 |
| NovelQA | 962 | 0.00734 | **1 / −0.0183** | 0.00000 | 0 / 0.1645 | 0.02731 | 17 / −0.1529 |
| PeerQA | 18,593 | 0.00437 | 0 / 0.0632 | 0.00000 | 0 / 0.2148 | 0.12847 | 2 / −0.0435 |
| CovidQA | 3,351 | 0.00219 | 0 / 0.1482 | 0.00000 | 0 / 0.3526 | 0.09833 | 2 / −0.0422 |
| ESGReports | 2,407 | 0.00083 | 0 / 0.1287 | 0.00000 | 0 / 0.3136 | 0.03506 | 0 / 0.0903 |
| MLDR | 1,536 | 0.11242 | 0 / 0.2919 | 0.00004 | 0 / 0.3626 | 0.30396 | 0 / 0.1508 |
| LooGLE | 14,095 | 0.05917 | 0 / 0.0185 | 0.00000 | 0 / 0.2505 | 0.32771 | 4 / −0.0450 |
| LMEB_SciFact | 1,748 | 0.00860 | 0 / 0.3586 | 0.00000 | 0 / 0.4177 | 0.19027 | 0 / 0.2752 |
| Gorilla | 907 | 0.00772 | 0 / 0.0490 | 0.00000 | 0 / 0.2977 | 0.13395 | 0 / 0.0180 |
| ToolBench | 13,862 | 0.00554 | 0 / 0.0214 | 0.00000 | 0 / 0.3253 | 0.11633 | 8 / −0.0835 |
| ReMe | 110 | 0.00178 | 0 / 0.0861 | 0.00000 | 0 / 0.2072 | 0.18002 | 13 / −0.0867 |
| Proced_mem_bench | 336 | 0.00000 | 0 / 0.2950 | 0.00000 | 0 / 0.4973 | 0.00449 | 0 / 0.0989 |
| MemGovern | 1,922 | 0.00026 | 0 / 0.2620 | 0.00000 | 0 / 0.4376 | 0.06613 | 0 / 0.0576 |
| DeepPlanning | 164 | 0.00000 | 0 / 0.3239 | 0.00000 | 0 / 0.4721 | 0.00000 | 0 / 0.2341 |
| **all** | | **0.00420** | **2 / −0.0183** | **0.00000** | **0 / 0.1645** | **0.06159** | **247 / −0.1529** |

## The models disagree about what zero is

This is the reading that outlives the verdict.

- For **bge-m3**, zero is effectively unreachable: 0.000 % of ten million
  non-answer pairs, no pair within 1e-3 of it, and the lowest gold similarity
  anywhere is 0.1645. A floor at zero would never fire.
- For **jina-v5-nano**, zero is a tail line overall (0.42 %) but a body line on
  three tasks, reaching 11.2 % on MLDR.
- For **MiniLM-L6-v2** — the reference endpoint, no veto — zero cuts 6.2 % of
  non-answers and 0.23 % of gold pairs. It is not a tail line at all.

So "cosine ≤ 0 means anti-correlated, therefore useless" is not one statement
about retrieval; it is three different statements about three encoders, and
writing it into the server as a constant makes the server's behaviour depend on
which encoder is loaded — in exactly the way this line has been measuring
elsewhere. The model-relative line already exists and is already calibrated:
it is the admission floor. **The reserved rows are, by construction, the rows
below that line.** Giving them a second floor re-creates the thing the
reservation was added to repair.

## M3 — how often any of this is reachable

From the completed replay dumps (`replay_jinanano_cap200`, `replay_bgem3`,
`replay_minilm`), 24,244 queries each. The dumps carry what each arm brought
rather than the fused list, so two of these are bounds.

| Reading | jina | bge-m3 | MiniLM |
|---|---:|---:|---:|
| eligible universe < 10 (every row is reserved; no floor changes which) | 0.206 % | 0.206 % | 0.206 % |
| both arms under 10 (necessary for a short fused list) | 0 | 0 | 1 (0.004 %) |
| arms sum under 10 (sufficient for one) | 0 | 0 | 0 |

At benchmark scale the reservation is essentially never the thing that answers.
The regime that raised the question — a four-row corpus — is the 0.2 % line:
there the universe is smaller than the reservation, every row is reserved
whatever floor exists, and a floor decides only whether the caller gets a short
answer or an empty one.

## What the caller is told

No change is proposed to the contract. A reserved row already arrives with
`fallback: true`, never ahead of a qualified row, and `fallback_rows` says how
many there are (`docs/behavior-contracts.md` §11). That marker is what carries
this: the rows are declared as "this is what there was", not as hits, and a
caller that wants a relevance promise should read the marker rather than expect
the server to have deleted the evidence that it had nothing better.

## Instrument, and what validating it caught

Three checks before the numbers were believed:

1. **Against ground truth.** On LMEB_SciFact (1,748 rows), the sampled reading
   (500 non-answers per query) gives P(s≤0) = 0.00850; computing every one of
   the 328,258 pairs gives 0.00852. Error 0.2 %.
2. **Against an independent path.** The row-keyed evidence dumps hold the
   similarity the pipeline itself computed, under a different (rank-stratified)
   sampling. Re-weighted to the eligible universe they give 0.00763 ± 0.00125,
   0.7 SE from the exact value, and the lowest gold similarity agrees exactly:
   0.3586 both ways.
3. **Cache miss accounting.** 0 document misses, 0 query misses, 0 gold pairs
   without a vector, in all three models. A missing gold vector would bias M2
   downward silently, so it is counted rather than assumed away.

Two defects in the instrument were found by those checks, both of the kind that
returns a plausible number rather than an error:

- **The cache key.** A model that declares prompts is cached under
  `prompt\x1f text`. Read bare, jina missed all 1,748 rows of the first task —
  loud. But bge-m3 and MiniLM hold *both* forms (agreeing to 7e-4), so a bare
  read there would have silently mixed two runs' vectors. The key form is now
  probed per cache and reported with the result.
- **The population.** The candidate file's fields are `scene_id` /
  `candidate_doc_ids`; read under guessed names the map parsed to empty, every
  query fell back to the whole corpus, and LongMemEval's universe read as
  237,655 rows instead of the 476 the retrieval path actually ranks. A
  candidates file that parses to nothing is now a hard error, and queries whose
  scene is genuinely absent from the map are counted separately.

The read bound was also fixed from "drop the queries that do not fit" to
"read in chunks": taking the first N of a query file is a selection, and it had
quietly cut LongMemEval's per-query sample from 500 to about 64.

## Limits

- English corpora. A model's similarity scale need not survive a change of
  language, and the disagreement measured here between three encoders is
  itself the warning: nothing in this file licenses a constant for Japanese
  text.
- The gold labels are the benchmark's. A pair the benchmark does not mark as
  gold is counted as a non-answer, so `P(s <= 0)` mixes in whatever unjudged
  relevant rows these corpora contain.
- M3's middle two rows are bounds, not counts, because the dumps record each
  arm rather than the fused list.
