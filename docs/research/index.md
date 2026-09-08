# Research notes

The design pages say what the server builds and why. These notes hold what
those pages rest on: the derivations, the measurements and the refutations,
each written so that a reader can check it rather than trust it. A design
page cites a note; a note never overrides a design page. Where a note and a
design page disagree, the design page is wrong or the note is stale, and the
disagreement is the finding.

Every note opens with a **status** line from this vocabulary:

| Status | Meaning |
| --- | --- |
| derivation | A result that follows from stated assumptions. Each assumption names the observation that would refute it. Not behaviour until measured. |
| measurement | Numbers taken from a named instrument under named conditions, with the command that produced them. A measurement without a pre-registered claim says so. |
| refuted | A derivation or a claim that a later measurement contradicted. Kept, because the refutation is the useful part. |
| superseded | Replaced by a later note, which it links to. |

Nothing in this section is a shipped guarantee. A behaviour becomes one only
through the [release lifecycle](../RELEASE_LIFECYCLE_STANDARD.md).

## Notes

| Note | Status | One line |
| --- | --- | --- |
| [Adaptive fusion — a derivation](adaptive-fusion-derivation.md) | derivation | The Bayes-optimal way to combine retrieval arms through their null exceedance probabilities; a mixture rule with closed-form per-row influence whose limit is today's reciprocal rank fusion; the measurements that must precede an implementation. |
| [Is calibration the cause of the lexical-arm losses?](calibration-admission-floor-2026-09.md) | measurement | Three calibration methods, two replicates, seven losing tasks: the admission floor is not the cause; the null was taken from the wrong pair population; small corpora starve the dense arm. |

## How a note is written

- **Assumptions first, each with its refutation.** A derivation that cannot
  say what would prove it wrong is not one.
- **Numbers carry their instrument.** The model, the cache, the flags, the
  commit, and the command; a table a reader cannot regenerate is a claim,
  not a measurement.
- **Corrections stay visible.** When a later check tightens a number (a
  rounded "±0.1" that was 0.21, a "0 %" that was two queries), the note says
  so instead of silently replacing it.
- **No internal pointers.** A note explains its reasons in full; it does not
  refer to anything a reader of this site cannot open.
