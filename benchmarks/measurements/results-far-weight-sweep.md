# Results: the price of a far vote

Pre-registration: [`prereg-far-weight-sweep.md`](prereg-far-weight-sweep.md),
with its amendment 1. Harness: `scan_window_ab.py`, arms `M1_SPEC`, build
`7d850d3`. Run 2026-09-28, twelve rotations, 20 arms each, 240 arm files.

**Verdict, by the pre-registered rule: no weight passes on dev, so the default
of `CPERSONA_PRIOR_FAR_WEIGHT` stays at 1 and the test half decides nothing.**
Every weight above 0 keeps the far stratum's gain, but none keeps the recent
answers: the near stratum loses 3.01 NDCG@10 points even at `w = 0.25`. The
arithmetic in the pre-registration held: below `61/70` no row with only a far
vote displaced a recent answer. The loss that remains comes from rows that
hold a far vote **and** a lexical vote. No constant weight above 0 removes it.

## Controls, read before any outcome

Over all twelve rotations (480 queries), before any NDCG was computed:

| Control | Expectation | Measured |
| --- | --- | --- |
| Replicate (A = A-rep) | identical | 480/480 identical |
| A weight of 0 is the reach off (A = W0, A-p = W0-p) | identical | 480/480 and 480/480 |
| The default written as a number (S = W100) | identical | 480/480 |
| Near-list identity (A against every shipped reach arm, A-p against every production one) | every query | 4,320/4,320 and 3,840/3,840 |
| The weight acts (rows differ between S and W75) | at least one query | 465/480 shipped, 139/480 production |

The positive control (the far stratum moves between A and S) is an outcome,
and it is read with the dev table: it moves under the shipped fusion (+3.96)
and does not, on balance, under the production one (−0.28, 7 queries better
and 10 worse).

## Dev: rotations 0–5 (120 near and 120 far queries)

NDCG@10, and the paired change against the regime's baseline (A or A-p), with
queries better / worse.

| Arm | `w` | Near | Δnear | better/worse | Far | Δfar | better/worse |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| A | reach off | 38.76 | — | — | 8.80 | — | — |
| W0 | 0 | 38.76 | +0.00 | 0/0 | 8.80 | +0.00 | 0/0 |
| W25 | 0.25 | 35.75 | −3.01 | 0/17 | 12.97 | +4.17 | 19/5 |
| W50 | 0.5 | 35.75 | −3.01 | 0/17 | 13.09 | +4.29 | 19/5 |
| W75 | 0.75 | 35.75 | −3.01 | 0/17 | 13.16 | +4.36 | 18/5 |
| W875 | 0.875 | 35.39 | −3.37 | 0/19 | 13.41 | +4.61 | 18/5 |
| W90 | 0.9 | 35.31 | −3.45 | 0/20 | 13.41 | +4.61 | 18/5 |
| W95 | 0.95 | 34.12 | −4.64 | 0/26 | 13.90 | +5.10 | 20/6 |
| S | 1 | 31.15 | −7.61 | 0/43 | 12.76 | +3.96 | 22/13 |
| A-p | reach off | 25.62 | — | — | 16.25 | — | — |
| W0-p | 0 | 25.62 | +0.00 | 0/0 | 16.25 | +0.00 | 0/0 |
| W25-p | 0.25 | 25.62 | +0.00 | 0/0 | 16.05 | −0.20 | 8/9 |
| W50-p | 0.5 | 25.62 | +0.00 | 0/0 | 15.81 | −0.44 | 7/10 |
| W75-p | 0.75 | 25.62 | +0.00 | 0/0 | 15.62 | −0.63 | 6/10 |
| W875-p | 0.875 | 25.48 | −0.14 | 0/2 | 15.62 | −0.63 | 6/10 |
| W90-p | 0.9 | 25.48 | −0.14 | 0/2 | 15.62 | −0.63 | 6/10 |
| W95-p | 0.95 | 24.59 | −1.03 | 0/5 | 15.78 | −0.47 | 7/10 |
| S-p | 1 | 18.77 | −6.85 | 0/22 | 15.97 | −0.28 | 7/10 |

Every call returned ten rows in every arm.

## Selection on dev (shipped fusion)

The far bar is `Δfar(S) − 1.0 = +2.96`. A weight passes when `Δnear ≥ −1.0`
and `Δfar ≥ +2.96`.

| `w` | Near condition | Far condition | Passes |
| ---: | --- | --- | --- |
| 0 | holds (0.00) | fails (0.00) | no |
| 0.25, 0.5, 0.75 | fails (−3.01) | holds (+4.17 to +4.36) | no |
| 0.875, 0.9 | fails (−3.37, −3.45) | holds (+4.61) | no |
| 0.95 | fails (−4.64) | holds (+5.10) | no |
| 1 | fails (−7.61) | holds (+3.96) | no |

**No candidate passes. The default stays at 1.** This selection was committed
before the test half was scored.

## Test: rotations 6–11

By the rule the test half decides nothing, because no weight was chosen. It is
reported because it says whether dev's picture holds on questions that were
not looked at. It does:

| Arm | `w` | Near | Δnear | better/worse | Far | Δfar | better/worse |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| A | reach off | 35.17 | — | — | 5.89 | — | — |
| W25 | 0.25 | 32.60 | −2.57 | 0/20 | 8.19 | +2.30 | 14/0 |
| W50 | 0.5 | 32.60 | −2.57 | 0/20 | 8.17 | +2.28 | 14/0 |
| W75 | 0.75 | 32.60 | −2.57 | 0/20 | 8.12 | +2.23 | 13/0 |
| W875 | 0.875 | 32.60 | −2.57 | 0/20 | 8.07 | +2.17 | 13/1 |
| W90 | 0.9 | 32.60 | −2.57 | 0/20 | 8.07 | +2.17 | 13/1 |
| W95 | 0.95 | 32.20 | −2.97 | 0/24 | 8.42 | +2.53 | 15/1 |
| S | 1 | 29.11 | −6.06 | 0/46 | 8.72 | +2.83 | 17/6 |
| A-p | reach off | 23.15 | — | — | 10.23 | — | — |
| W25-p | 0.25 | 22.95 | −0.20 | 0/1 | 10.36 | +0.14 | 6/6 |
| W75-p | 0.75 | 22.95 | −0.20 | 0/1 | 10.42 | +0.19 | 6/6 |
| W90-p | 0.9 | 22.65 | −0.50 | 0/2 | 10.61 | +0.38 | 7/6 |
| W95-p | 0.95 | 22.20 | −0.95 | 0/4 | 10.80 | +0.57 | 8/5 |
| S-p | 1 | 19.21 | −3.94 | 0/15 | 11.11 | +0.89 | 9/5 |

Under the shipped fusion, every weight above 0 again costs the near stratum
more than a point, and no near query improves at any weight, on either half.

## Pooled: all twelve rotations (240 near and 240 far queries)

| Arm | `w` | Near | Δnear | better/worse | Far | Δfar | better/worse |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| A | reach off | 36.97 | — | — | 7.35 | — | — |
| W25 | 0.25 | 34.18 | −2.79 | 0/37 | 10.58 | +3.23 | 33/5 |
| W50 | 0.5 | 34.18 | −2.79 | 0/37 | 10.63 | +3.28 | 33/5 |
| W75 | 0.75 | 34.18 | −2.79 | 0/37 | 10.64 | +3.30 | 31/5 |
| W875 | 0.875 | 34.00 | −2.97 | 0/39 | 10.74 | +3.39 | 31/6 |
| W90 | 0.9 | 33.96 | −3.01 | 0/40 | 10.74 | +3.39 | 31/6 |
| W95 | 0.95 | 33.16 | −3.80 | 0/50 | 11.16 | +3.81 | 35/7 |
| S | 1 | 30.13 | −6.84 | 0/89 | 10.74 | +3.40 | 39/19 |
| A-p | reach off | 24.39 | — | — | 13.24 | — | — |
| W25-p | 0.25 | 24.29 | −0.10 | 0/1 | 13.21 | −0.03 | 14/15 |
| W75-p | 0.75 | 24.29 | −0.10 | 0/1 | 13.02 | −0.22 | 12/16 |
| W90-p | 0.9 | 24.06 | −0.32 | 0/4 | 13.11 | −0.12 | 13/16 |
| W95-p | 0.95 | 23.39 | −0.99 | 0/9 | 13.29 | +0.05 | 15/15 |
| S-p | 1 | 18.99 | −5.40 | 0/37 | 13.54 | +0.30 | 16/15 |

For orientation only: the reach measurement's record, on an older build, had
A at 34.18 near and 5.69 far, and S at −6.67 near and +3.87 far. This build
moves both levels, and S's price is about the same: −6.84 near and +3.40 far.

## Mechanism: which rows displaced the recent answers

The far-only reading registered in advance: among near-stratum queries that
lost NDCG@10 against A, the rows that entered the top ten, and how many of
them carried only a far vote. Pooled over all twelve rotations:

| Arm | `w` | Near queries that lost | Displacing rows | Of which far vote only |
| --- | ---: | ---: | ---: | ---: |
| S | 1 | 89 | 272 | 189 |
| W95 | 0.95 | 50 | 89 | 20 |
| W90 | 0.9 | 40 | 69 | **0** |
| W75 | 0.75 | 37 | 64 | **0** |
| S-p | 1 | 37 | 29 | 29 |

The prediction's arithmetic holds exactly. At `w ≤ 0.9` no row with only a far
vote displaced a recent answer, and at 0.95 some did again. The weight
removes the far-only displacement, which was about 70% of S's displacing rows.

What it cannot remove is the rest. At every weight from 0.25 to 0.9, the
displacing rows hold a far vote **and** another vote. Such a row is already a
lexical candidate with the reach off, ranked close to a recent answer that
holds one vote, and a far vote on top lifts it past that answer. Lowering the
weight from 0.75 to 0.25 recovered none of these queries: the same 37 near
queries lose at 0.25, 0.5 and 0.75, and the three arms have the same near
NDCG@10. Their top ten still differ on 47 of the 240 near queries, in rows
that are not answers.

The far gain does not shrink over the same range either: +3.23 at 0.25
against +3.40 for S. The rows that make up the gain on the far stratum are
the same kind as the rows that cost the near stratum: far rows that also
hold a lexical vote. Between 0.25 and 0.9, a single weight moves both
together, and only `w = 0` removes the loss, together with the gain.

So a far list at this reach cannot be admitted under `rrf` by a price on its
vote alone. The next candidates are structural, for example a far vote that
counts only for rows with no other vote, or fewer far rows
([far-limit measurement](results-scan-window-far-limit-ab.md)). Neither is
decided here.

## The production fusion

Two observations. Neither is part of the rule.

The production arms ran on build `7d850d3`, whose `rsf` put each channel on a
fixed scale. That scale was withdrawn before any release
([results](results-rsf-fixed-scale.md)), and the `rsf` that ships normalises
each channel per query as before. So these observations describe that build,
not the shipped `rsf`. The decision above rests on the shipped fusion (`rrf`),
and it does not change.

- Under `rsf` with the confidence scorer on, the far list buys almost nothing
  on the far stratum (pooled +0.30 at `w = 1`, with 16 queries better and 15
  worse), and it costs the near stratum 5.40 points.
- The baseline itself differs. A-p scores 24.39 on the near stratum where A
  scores 36.97, and 13.24 on the far stratum where A scores 7.35, on the same
  corpus and queries. The fused gate is uncalibrated here, as in every arm of
  this harness, and a deployment calibrates it. This run does not say how much
  of the gap calibration closes. It is reported as a reason to measure the
  production fusion on this instrument with a calibrated gate, not as a
  finding about deployments.

## Limitations

- One reach (200,000) and one response size (`limit = 10`). The reach-50,000
  companion of the plan was not run.
- The fused gate is uncalibrated in every arm, as in the reach and window
  measurements this reads against.
- Latency is recorded per arm in the JSON and is not compared: the run shared
  the machine with other jobs.

## Files

Arm files, plans and the manifest: `~/lmeb/m1_far_weight/run/` on the
measuring machine, 240 arm files (not committed, as for the earlier runs of
this harness). Scored reports: `dev.json`, `test.json` and `pooled.json` next
to them, from `scan_window_ab.py score --first-rotation {0,6,0} --rotations
{6,6,12} --identity A:W0,S:W100,A-p:W0-p`.
