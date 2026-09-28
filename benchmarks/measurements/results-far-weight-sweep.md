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

Pending. By the rule, it decides nothing; it is reported.
