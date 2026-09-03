# Results: raising the shipped scan-window default

Pre-registration: `prereg-scan-window-default-ab.md` (with one amendment).
Harness: `scan_window_ab.py`. Run 2026-09-03.

**Verdict, by the pre-registered rule: do not raise the default as it stands.**
A twenty-fold wider window costs 20.19 NDCG@10 points on queries whose answer
is recent and buys 4.93 on queries whose answer is old. The rule asked for
Δ_far ≥ +5.0 with Δ_near ≥ −1.0; the gain missed its bar and the cost exceeded
its own by a factor of twenty.

## What was measured

`CPERSONA_MAX_MEMORIES` — how many of the newest rows the vector arm ranks per
recall — at the shipped 10,000 against a candidate 200,000, on LongMemEval's
237,654 stored documents, through the real `do_recall` with the shipped recall
regime (`rrf`, fused gate on, autocut on, confidence off, threshold 0.3, local
vector search; each arm asserts and records what actually took effect).

Queries are split by where their answer lives, because the two directions are
different questions:

- **near** — the answer is inside the 10,000-row window. Both arms can see it.
- **far** — the answer is below it, inside the wide window. Only the wide arm's
  vector channel can see it; the lexical arms reach it in both.

Both strata are paired: the same query, the same database file, one variable.

## Controls

| Control | Expectation | Measured |
|---|---|---|
| Replicate (`A-rep`, same window, second pass) | identical | **identical** — Δ = ±0.00 on 480 paired queries, top-10 set and order unchanged for every one |
| Positive (the window must matter above itself) | far stratum moves | **moves** — Δ = +4.93, 40 queries better, 5 worse |
| Identity (two windows that both cover the corpus) | identical | **identical** — 300,000 and 500,000 agree to the last digit on every cell (see below) |

The replicate control is what makes the rest readable: the noise band is not
small, it is zero, so every difference below is the window and nothing else.

The negative control registered in the pre-registration — the same two windows
over a corpus smaller than either — could not be run as written. The second
dataset (LoCoMo) has queries whose relevant documents lie outside their own
scene, which the scene-block layout refuses to place; the harness stopped
rather than quietly measuring something else. It is replaced by the identity
control above, which makes the same claim on the corpus already built: once a
window covers the corpus, its value stops mattering.

## Primary result (`limit=10`, the MCP default)

12 rotations, 240 paired queries per stratum.

| Arm | Window | Stratum | NDCG@10 | Recall@10 | Latency p50 | p95 |
|---|---:|---|---:|---:|---:|---:|
| A | 10,000 | near | 34.18 | 43.72 | 311 ms | 791 ms |
| B | 200,000 | near | **13.98** | 20.69 | 640 ms | 1192 ms |
| A | 10,000 | far | 5.69 | 11.04 | 295 ms | 722 ms |
| B | 200,000 | far | **10.62** | 16.83 | 624 ms | 1041 ms |

| Pair | Stratum | Δ NDCG@10 | better | worse |
|---|---|---:|---:|---:|
| A → B | near | **−20.19 ± 1.70** | 1 | 108 |
| A → B | far | **+4.93 ± 0.90** | 40 | 5 |
| A → A-rep | either | ±0.00 | 0 | 0 |

Top-10 membership changed for 100% of queries in both strata (mean Jaccard
0.36 near, 0.35 far). Membership change alone was pre-registered as *not*
degradation; the NDCG column is what carries the verdict.

The pooled "all" figure (−7.63) is **not** an estimate of what a user would
see. It is the average of a 50/50 near/far mix that this instrument authored.
What a real installation gets depends on how often its queries are about
something recent, which this measurement does not observe.

## Mechanism: nothing was truncated, everything was outranked

The suspects named in the pre-registration were the fused quality gate,
autocut, and fusion rank. The first two can be eliminated from the data:
**every call in every arm returned exactly ten rows, and no call reported a
gate fallback.** Neither truncation layer engaged, in either arm.

So the loss is rank displacement inside reciprocal-rank fusion. Widening the
window does not admit worse rows past a filter; it enlarges the field the
right row has to win. A recent answer that ranked third among 10,000
candidates ranks far lower among 200,000, its reciprocal-rank contribution
collapses, and rows that fused better take its place in the top ten.

**The reading this points to:** today's window is doing two jobs at once. It
bounds the cost of a scan, and — because it keeps only the newest rows — it
also acts as an unpriced recency prior. For a query whose answer is recent
that prior is free accuracy, and it is worth 20 points here. Raising the
default removes it. The window cannot be widened for reach without something
else supplying the recency that widening takes away; the reach and the prior
are not separable while one number controls both.

That is a finding about the design, not only about a default value: the reach
knob and the recency prior need to be two knobs before either can move.

## Cost

Latency roughly doubles at p50 (305 → 634 ms) and grows 1.5× at p95 (729 →
1091 ms) for the same answers-per-call. The floor in both arms is the lexical
arm, which reads the whole corpus regardless of the window.

## Limitations

- **One dataset, one model.** LongMemEval with bge-m3 vectors. The filter
  layer's contribution is known to move with embedding strength, so the size
  of the effect may not transfer; its direction is a property of how
  rank fusion treats a larger field, which should.
- **One fusion mode.** `rrf`, the shipped default. A score-based fusion may
  price a larger field differently, and that is worth its own measurement
  before any conclusion is generalised to every configuration.
- **The near/far mix is authored**, as noted above.
- **Scene blocks are an authored recency structure.** Real memories are not
  stored one topic at a time. What the layout guarantees is that a query's
  hardest competitors are visible to both arms, which is what makes the two
  arms differ in the window rather than in the competition.

## Exploratory (not part of the decision rule)

Six rotations, 120 paired queries per stratum. Registered in amendment 1
before the pooled primary result was computed.

| Arm | Window | limit | near NDCG@10 | far NDCG@10 | p50 |
|---|---:|---:|---:|---:|---:|
| A | 10,000 | 10 | 35.68 | 6.92 | 311 ms |
| C | 50,000 | 10 | 23.37 | 19.79 | 377 ms |
| B | 200,000 | 10 | 14.34 | 12.98 | 622 ms |
| D | 300,000 | 10 | 13.58 | 12.50 | 705 ms |
| E | 500,000 | 10 | 13.58 | 12.50 | 694 ms |
| A100 | 10,000 | 100 | 42.05 | 5.22 | 293 ms |
| B100 | 200,000 | 100 | 15.95 | 13.57 | 616 ms |

**The identity control holds.** D and E agree on every cell — NDCG, recall,
MRR, and the paired delta against A (−22.10 on both, mean Jaccard 0.356 on
both). Once a window covers the corpus, its value stops mattering, measured
rather than asserted. That is the ground for saying a larger default is free
for anyone whose corpus is smaller than it.

**The benefit is not monotonic in the window.** A window of 50,000 buys
*more* far-stratum quality than one of 200,000 (+12.87 against +6.06) at
little more than half the near-stratum cost (−12.31 against −21.34), and its
pooled delta is +0.28 ± 1.54 — indistinguishable from no change, where
200,000 is −7.64. The far answers sit at depth 20,000–29,500, so both windows
reach them; the wider one simply makes them compete against 150,000 more
rows. **Reaching past the answer costs something and buys nothing.** Whatever
value is eventually chosen, "as large as possible" is the wrong shape for it.

**The deeper pass does not rescue the displaced answer; it displaces it
further.** At `limit=100` the same window change costs 26.10 points against
20.19 at `limit=10`, and mean Jaccard falls to 0.17. Asking for a hundred
rows deepens the fusion list, so the widened field is felt more, not less.
The narrow window's own score *improves* with depth (35.68 → 42.05) while the
wide window's barely moves (14.34 → 15.95): depth helps when the field is
small and cannot compensate when it is not.

## What this measurement does not settle

It says a twenty-fold raise fails its own test, and that an intermediate
value behaves better on both sides. It does not choose a value, and it should
not be read as one: the far stratum here sits at a depth this instrument
authored, and a window tuned to that depth is tuned to the instrument. The
next question is not "which number" but whether reach and recency can be
separated at all — a window that keeps only the newest rows is a recency
prior with no name and no setting, and every number in the near column is
what that prior is currently worth.
