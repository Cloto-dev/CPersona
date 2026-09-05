# Always-Loaded Policy Block Standard (v1.1)

A standard for Cloto-family projects whose product value depends on an AI
agent behaving correctly *without being asked*: the project's skill MUST be
able to generate a small, marker-wrapped **policy block** into the file the
user's client loads on every session — `CLAUDE.md` for Claude Code and Claude
Desktop, `AGENTS.md` for Codex and Cursor, a workspace instructions file for
VS Code (§2.1).

## Status of this document

**Pilot.** This standard is piloted in **cpersona** (the same pilot model as
the [Release Lifecycle Standard](RELEASE_LIFECYCLE_STANDARD.md)): every rule
below is exercised by the `cpersona-memory` skill before family-wide
adoption. Canonical home: this repository, while the pilot runs.

**v1.1 (2026-09-05)** generalises the target from `CLAUDE.md` to whichever
file the client loads every session, and adds rule 7 (client neutrality). The
rules are otherwise unchanged, and the file keeps its historical name so that
existing links resolve.

## 1. Motivation

A skill is loaded **conditionally and probabilistically** — whether it
activates in a given session depends on the conversation. The client's
always-loaded instructions file (`CLAUDE.md`, `AGENTS.md`, …) is loaded
**deterministically** in every session. Products like a memory server
live or die on the agent *proactively* calling their tools (recall at session
start, store on decisions, archive at session end); a rule that fires only
when a skill happens to activate cannot carry that guarantee.

The fix is a promotion from probabilistic to deterministic: the skill — which
the user does invoke at install time — writes the product's operating policy
into the user's always-loaded file. The skill remains the detailed manual; the policy block is the small resident kernel that makes the agent
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

### 2.1 Targets

The block goes into the file the user's client loads on every session. The
client, not the standard, names that file; the rules below are the same for
each:

| Client | User-level default | Project-level alternative |
| --- | --- | --- |
| Claude Code / Claude Desktop | `~/.claude/CLAUDE.md` | `./CLAUDE.md` |
| Codex CLI | `~/.codex/AGENTS.md` | `./AGENTS.md` at the repository root |
| Cursor | User Rules (a setting, not a file) | `.cursor/rules/<product>.mdc` with `alwaysApply: true`, or `./AGENTS.md` |
| VS Code (Copilot) | per the client's custom-instructions documentation | `.github/copilot-instructions.md`, or `./AGENTS.md` with `chat.useAgentsMdFile` enabled |

A client not listed is in scope whenever it loads a file into every session.
The skill that runs inside a client knows which one it is in and picks the
row; a reader on a client with no skill support pastes the block by hand.

## 3. Requirements for the generation task

An applicable repository's skill MUST offer to persist the policy, and the
generated block MUST satisfy all of the following:

1. **Consent** — the skill MUST show the exact block and get the user's
   approval before writing. Never modify a user's always-loaded file silently.
2. **Placement** — default target is the user-level file of the client in
   use (`~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`, … — §2.1), because the
   products this standard covers are cross-project infrastructure. The
   project-level file MUST be offered as the scoped alternative.
3. **Idempotency** — the block is wrapped in versioned markers:

   ```
   <!-- BEGIN <product>-policy vN (managed by the <skill-name> skill) -->
   ...
   <!-- END <product>-policy -->
   ```

   On re-run, a block whose `BEGIN` marker is already present is **replaced
   in place** (never appended twice). Content outside the markers is never
   touched. The markers are HTML comments, which every target treats as
   Markdown; in a Cursor `.mdc` rule the block follows the frontmatter.
4. **Size budget** — at most **40 lines** between the markers. The budget
   exists to force selection, not to forbid substance: baseline operations
   (an obvious store, an explicit recall) work with no block at all, so
   every line must earn its place by changing what the agent does *by
   default*. Explanations, setup, and troubleshooting stay in the skill,
   referenced by a one-line pointer. The file is paid for in every
   session — respect the user's context window.
5. **Versioning** — bump `vN` whenever the block content changes. On re-run
   the skill upgrades an older-versioned block (with consent, per rule 1).
6. **Language** — the block is written in English only.
7. **Client neutrality** — the block MUST NOT assume one client. A line that
   applies only to some clients is written conditionally ("if this client
   keeps a memory file that loads every session …") so the same block is
   correct wherever it is pasted. The skill, not the block, knows which client
   it is running in.

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
| ClotoCore | N/A | The kernel is its own agent middleware; end users do not drive it through an instructions file. |
| mgp-spec / mgp-rs | N/A | Specification / library — no agent-side behavior to persist. |
| awesome-mcp-servers | N/A | Curated list. |
| clotohub-servers | Out of scope | Monorepo — fails the independence precondition. |

New independent repositories add a row here at creation time.
