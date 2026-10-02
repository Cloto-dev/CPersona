# Reconstruction v1.2 reader study: re-measurement with gpt-6-luna

Registered before any reader call of this run. The commit that adds this file is
the evidence of the order.

## Why a second run

The [first run](prereg-reconstruct-v1_2.md) read through `gpt-5.6-luna` and made
no claim about reading cost ([results](results-reconstruct-v1_2.md)): the median
payload ratio was 0.624 (95% interval 0.559 to 0.710) with 33 correct answers
against 32, but the reduction did not exceed the registered noise floor of
0.512. This run asks whether the conclusion depends on the reader, with a newer
one. Its verdict stands on its own and does not replace the first run's.

## What is the same

The store, the 36 questions, both arms at the same commits (`70b03ae` and
`4c01ccc`), the tool layer and its descriptions, every reader and judge prompt
and schema, the CLI and its flags, reasoning effort `high`, the 300-second
timeout, the stop on a run without a logged `search` call, the measures and the
thresholds of the decision rule.

## What changes

- **The reader** is `gpt-6-luna`. One reader call on a question outside the
  study (a knowledge-update candidate that was eligible and not selected)
  checked before this registration that it calls the tools through this
  harness: one `search`, an answer, 8,770 payload characters.
- **The judge stays `gpt-5.6-luna`**, so that only the reader changes.
- **The noise floor.** The first run repeated arm A on six questions and arm B on
  six others and took the larger floor; the two came out 0.512 and 0.000, and
  four of B's repeats were identical call for call. Six pairs on different
  questions do not hold a floor still. Here the first two selected questions of
  each type (twelve) run **both** arms a second time, and the floor is the median
  of |second − first| / first in payload characters over all 24 pairs. Each
  repeat is a unit of its own, both arms back to back in a coin-flipped order.
- **Order**: seed 20261006 for the order and the bootstrap.
- **Pause** before a new call once this run's reported input tokens pass
  16,000,000 (96 reader and 96 judge calls; the first run used 7.7 million for
  84 reader calls).

**Smoke**: the first unit. Both arms must show logged `search` calls, and arm A's
items must carry `quote_basis` while arm B's do not. The smoke stays in the
cohort.

## Decision rule

The first run's, with the floor above. Let r be the per-question ratio B / A of
payload characters, over the questions both arms completed.

- **Claim a reduction for this reader** when the median of r is at most 0.85,
  its 95% bootstrap interval (10,000 resamples over questions) lies below 1.0,
  the median reduction exceeds the pooled noise floor, and arm B has at most two
  fewer correct answers than arm A. The release notes then state the median and
  interval with the reader named, and state beside it that the `gpt-5.6-luna`
  run did not meet its rule.
- **Do not release** when arm B has four or more fewer correct answers than arm
  A. Investigate first.
- **Otherwise** make no claim about reading cost.

Reported and deciding nothing: input tokens (median per-question ratio and
totals), payload and tokens per correct answer, searches and expansions, the
per-arm floors, per-type results, and the comparison with the first run's
figures for the same questions.
