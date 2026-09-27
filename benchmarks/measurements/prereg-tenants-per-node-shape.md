# Pre-registration: which capacity saturates first as tenants are added to one node

Registered 2026-09-20, before the harness for it exists. Nothing below is a result.

## The question

**As independent instances are added to one machine, which capacity saturates first — disk, memory,
search throughput, or the shared embedding server — and is each one's growth linear or not?**

This is registered as a question about **ordering and shape**, not about absolute capacity. The
machine available for it is not the machine a deployment would use, so an absolute figure taken
from it would not transfer. An ordering might: which resource runs out first is a property of the
software's structure at least as much as of the hardware.

## Why it is worth measuring

A cost estimate for running many instances on one machine currently rests on one measured
ceiling — disk — and two unmeasured ones. Disk is known per record from a production database.
Memory is known only as a **floor**: the embedding model's resident size. Per-instance memory has
no measured value at all, because the running deployment is one process with one database serving
many identities, which is not the shape this question is about. Throughput has no measurement in
the deployed configuration either.

So "this many instances fit" is today a **disk** answer, and nothing rules out memory or throughput
running out sooner. If one of them does, the cost per instance changes by whatever ratio separates
them, and a price built on the disk figure would be wrong in the expensive direction.

## What an instance is here, and why there is no choice about it

One process serving one database. This is measured, not assumed: `DB_PATH` is read from the
environment at import and is a module-level value, the only runtime rewrite of it is a command-line
path in the index builder, and the database module states that all four data tables share a single
connection. **Serving several tenants from one process is therefore a change to the product, not a
deployment option**, so the shape measured here is the one that can be deployed as the code stands.

Each instance gets its own database path, its own process, and its own contiguous index. All
instances share **one** embedding server, which is the arrangement under consideration.

## Fixed conditions

- **Machine**: the reference Intel N150, 4 threads, `governor=performance`. The host also runs one
  container, allocated 2 cores and 2 GB; occupancy is recorded before and after every cell and a
  cell whose readings differ by more than 5% of one core is rerun.
- **Runtime**: the residual benchmark virtualenv, Python 3.11.2, numpy 2.4.6.
- **Corpus per instance**: the synthetic corpus of `perf_contiguous_index.py` (`build_corpus`),
  contiguous index built, scan window equal to the row count.
- **Model**: jina-v5-nano at 768 dimensions, one shared embedding server over HTTP, thread setting
  held at whichever value the concurrency measurement
  ([`prereg-recall-throughput-concurrency.md`](prereg-recall-throughput-concurrency.md)) finds best.
  That measurement runs first for this reason.
- **Offered load**: saturating. Each instance is driven by enough concurrent clients to keep it
  busy, and the reported number is **total** completed recalls per second across instances. No
  per-instance load is assumed, because no measurement of a realistic per-instance load exists yet.

## Arms

Instance count 1, 2, 4 and 8 at 10,000 records each; then 1, 2 and 4 at 100,000 records each.

The second sweep is **budget-bounded**: disk free is measured before it starts, per-instance size is
measured after the first instance is built, and arms are dropped from the largest end if the
remaining arms would not fit with 6 GB left free. A dropped arm is recorded as dropped, with the
numbers that caused it.

## What is recorded per cell

- **Disk**: actual bytes per instance, split into database, contiguous index, and anything else.
- **Memory**: resident set size per process **and** the machine's page-cache size, plus how much of
  each instance's index file is resident. Both are required — see the instrument check.
- **Throughput**: total completed recalls per second, and per-instance p50 / p95 / max latency.
- **Embedding server**: requests served per second, and its own resident size.
- **Occupancy** before and after.

## Registered decision rules

| Observation | What it licenses |
| --- | --- |
| Total throughput is flat within 5% as instance count doubles | The node is **throughput-bound**; instances divide a fixed capacity, and the count that fits is set by the service level each instance must keep, not by disk |
| Total throughput rises by more than 25% as instance count doubles | There was headroom at the lower count, and the single-instance figure understates the node |
| Page-cache size grows by roughly the index size per instance added, and available memory falls below the embedding model's size before disk or throughput run out | The node is **memory-bound**, and the disk-derived instance count already published is an overestimate. The ratio between the two is reported as the correction factor |
| Disk runs out first with memory and throughput still holding | The disk-derived count stands as the binding ceiling, and the published figure needs no correction |
| Per-instance resident size is flat in the record count while page cache grows with it | Confirms the index is paged rather than allocated, so memory is a **latency** limit rather than a hard one, and the limit must be stated as such |

**No figure for any other machine, architecture, core count or provider is derived from this run.**
What may be carried elsewhere is the ordering of the four capacities and whether each grows linearly.

## Instrument check, before any arm is read

**Resident set size alone is blind to the thing being measured.** Each instance's vector index is a
memory-mapped file read through a view, so its pages live in the page cache and never appear in the
process's resident set. A harness that measured only resident size would report per-instance memory
as small and flat, and would conclude memory is cheap, at exactly the point where the machine had
started faulting index pages from disk on every recall.

So the check is not optional and it is not satisfied by reading resident size carefully:

- **Both numbers are recorded**: resident set per process, and the machine's page cache. A cell
  where page cache did not grow when an instance was added is void, because either the index was
  not built or it was never touched.
- **Residency is verified per file**, not inferred: for one instance, the fraction of its index file
  resident in memory is read directly. If that fraction falls while throughput holds, the reading is
  that the corpus no longer fits and the arm is on the far side of the memory limit — which is the
  finding, not a failure.
- **Positive control on the memory axis**: the page cache must grow by at least half the index size
  when the first instance is built and driven. If it does not, the measurement of memory is not
  measuring anything and the run is void.

## What this does not measure

- **Any other machine.** Stated again because it is the whole scope of the reading: this host is
  x86 with 4 threads and 16 GB, and the deployment under consideration is neither.
- **Ingest.** Recall only. Ingest embeds whole windows instead of short queries and would change
  which capacity binds; it is a separate measurement.
- **Isolation or correctness between instances.** Only resource use is read here.
- **A realistic per-instance load.** Load is saturating by design, because the realistic figure is
  not yet measured.
- **The configuration without a contiguous index**, which is what an installation that has never
  built one runs.
- **Recall quality.** The corpora are synthetic.
- **Sustained load over hours**, so thermal behaviour and slow-growing resource use are outside it.
