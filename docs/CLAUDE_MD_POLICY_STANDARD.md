# CLAUDE.md Policy Generation Standard (v1.0)

A standard for Cloto-family projects whose product value depends on an AI
agent behaving correctly *without being asked*: the project's skill MUST be
able to generate a small, marker-wrapped **policy block** into the user's
`CLAUDE.md` (or equivalent always-loaded agent memory file).

## Status of this document

**Pilot.** This standard is piloted in **cpersona** (the same pilot model as
the [Release Lifecycle Standard](RELEASE_LIFECYCLE_STANDARD.md)): every rule
below is exercised by the `cpersona-memory` skill before family-wide
adoption. Canonical home: this repository, while the pilot runs.

## 1. Motivation

A skill is loaded **conditionally and probabilistically** — whether it
activates in a given session depends on the conversation. `CLAUDE.md` is
loaded **deterministically** in every session. Products like a memory server
live or die on the agent *proactively* calling their tools (recall at session
start, store on decisions, archive at session end); a rule that fires only
when a skill happens to activate cannot carry that guarantee.

The fix is a promotion from probabilistic to deterministic: the skill — which
the user does invoke at install time — writes the product's operating policy
into the user's always-loaded `CLAUDE.md`. The skill remains the detailed
manual; the policy block is the small resident kernel that makes the agent
open the manual at the right moments.

## 2. Applicability

The unit of adoption is the **independent repository**: a project published
as its own repository. Independence is a **necessary condition** — monorepos
and servers vendored inside them (e.g. `clotohub-servers`) are out of scope
of this standard entirely. Private repositories are exempt.

Every Cloto-family independent public repository MUST record a verdict in the
table below (§5):

- **Applicable** — correct end-user experience depends on agent-side behavior
  that must persist across sessions. The repository MUST ship a skill, and
  that skill MUST include a policy-block generation task conforming to §3.
- **N/A** — no such behavior exists (specifications, libraries, curated
  lists, products with their own agent middleware). Recorded with a reason.

## 3. Requirements for the generation task

An applicable repository's skill MUST offer to persist the policy, and the
generated block MUST satisfy all of the following:

1. **Consent** — the skill MUST show the exact block and get the user's
   approval before writing. Never modify a user's `CLAUDE.md` silently.
2. **Placement** — default target is the user-level file
   (`~/.claude/CLAUDE.md`), because the products this standard covers are
   cross-project infrastructure. A project-level `CLAUDE.md` MUST be offered
   as the scoped alternative.
3. **Idempotency** — the block is wrapped in versioned markers:

   ```
   <!-- BEGIN <product>-policy vN (managed by the <skill-name> skill) -->
   ...
   <!-- END <product>-policy -->
   ```

   On re-run, a block whose `BEGIN` marker is already present is **replaced
   in place** (never appended twice). Content outside the markers is never
   touched.
4. **Size budget** — at most **40 lines** between the markers. The budget
   exists to force selection, not to forbid substance: baseline operations
   (an obvious store, an explicit recall) work with no block at all, so
   every line must earn its place by changing what the agent does *by
   default*. Explanations, setup, and troubleshooting stay in the skill,
   referenced by a one-line pointer. `CLAUDE.md` is paid for in every
   session — respect the user's context window.
5. **Versioning** — bump `vN` whenever the block content changes. On re-run
   the skill upgrades an older-versioned block (with consent, per rule 1).
6. **Language** — the block is written in English only.

## 4. What belongs in a policy block

The test for every line: **would the agent already do this without the
block?** If yes, cut it. The block's job is to reproduce the *quality of
life* of a well-tuned operator environment — the reference here is the
maintainer's own setup — not to restate behavior the agent performs anyway.

Include: the stable identity the agent should use (e.g. `agent_id`); the
mandatory triggers **with concrete natural-language fire conditions** (the
phrases that should cause a tool call — this is what the agent gets wrong
without a policy); the non-obvious craft that separates a good deployment
from a default one (e.g. pre-computing summaries so storage is synchronous,
passing real history, lock discipline for critical rules, update-not-recreate
for rule changes); the degraded/error behavior the user must not miss; and a
minimal maintenance cadence. Exclude: install steps, tool references,
configuration tables, prose rationale — that is the skill's job.

## 5. Applicability table

| Repository | Verdict | Notes |
| --- | --- | --- |
| cpersona | **Applicable** (pilot) | Memory triggers must fire proactively; block generated by the `cpersona-memory` skill. |
| CEmbedding | **Applicable** | Block generated by the `cembedding` skill: embedding-server liveness + degraded-recall runbook. |
| ClotoCore | N/A | The kernel is its own agent middleware; end users do not drive it through `CLAUDE.md`. |
| mgp-spec / mgp-rs | N/A | Specification / library — no agent-side behavior to persist. |
| awesome-mcp-servers | N/A | Curated list. |
| clotohub-servers | Out of scope | Monorepo — fails the independence precondition. |

New independent repositories add a row here at creation time.
