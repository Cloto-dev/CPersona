# Recall Preview Tier Design (v2.5.0)

Status: approved 2026-07-15 (design review), shipped on the 2.5.0 pre-release line.
Scope: `recall` / `recall_with_context` MCP responses, the new `get_contents` tool.

## Problem

The recall tools return the **full content of every hit**. Measured on the
Claude Code environment (token inventory, 2026-07-11): ~815 tokens per memory,
~1,040 per episode, so roughly 9–10k tokens per `limit=10` call. At 5.2 calls
per session that is **23k tokens per session**, the single largest recall-side
context cost.

Most of that text is never read. The caller usually needs a relevance
judgement over the list, plus the full text of one or two rows.

## Design principle (shared with the v2.5.0 limit re-layering)

**Shaping and capping belong to the boundary layer; the library returns full
data.** An earlier change moved the agent-facing `limit` cap into the MCP JSON
Schema and left the library clamping only to the scan window, so that a caller
using the library directly is not held to a ceiling that exists to protect a
context window. The preview tier is the same move, for payload shape:

- `do_recall` / `do_recall_with_context` (library) — full content, unchanged.
- The MCP tool wrappers (`server.py`) — trim content to the preview tier
  unless the caller opts out.

This placement makes the bench harness (which calls the library directly), and
any future in-process consumer, structurally immune to the diet. Only
MCP-path consumers see previews.

## Response contract

Every recall message gains a stable full-fetch handle:

```json
{
  "ref": "mem:123",            // or "ep:45" — always present on DB-backed rows
  "content": "<pure prefix, at most CPERSONA_RECALL_PREVIEW_CHARS chars>",
  "content_truncated": true,    // only present when trimmed
  "content_len": 1893,          // full length, only present when trimmed
  "source": {...}, "timestamp": "...", "id": "<msg_id>", "confidence": {...}
}
```

Contract details, each load-bearing:

- **`ref` is new and always present** (memories and episodes). Before 2.5.0 the
  response exposed only `msg_id`, which is absent on episodes, so there was no
  way to address a row for a follow-up fetch. Kinds are prefixed (`mem:` /
  `ep:`) because both tables share one AUTOINCREMENT id space (the
  bug-040/041 collision class).
- **The preview is a pure prefix, with no ellipsis marker.** The
  `exclude_contents` dedup contract (`_content_excluded`) is a normalized
  bidirectional starts-with match, so a preview fed back into a later call
  still matches the stored full text. An embedded marker would break that,
  and nothing would report it.
- **Markers only when trimmed.** Short rows carry neither `content_truncated`
  nor `content_len`, for payload-diet consistency.
- `recall_with_context`'s echoed conversation entries are trimmed uniformly,
  since the caller already holds their full text. They carry no `ref`.

## Excerpt (2.6)

The prefix answers "is this row relevant?" only when the record says so early.
Measured on LongMemEval with an answer reader over the same ten returned rows
per question, the preview's first 500 characters answered 260 of 500
questions, the full records 351. The rows had arrived; the answer was past the
cut. So a row the preview cuts also carries the part that matched:

```json
{
  "ref": "mem:123",
  "content": "<pure prefix, unchanged>",
  "content_truncated": true, "content_len": 1893,
  "excerpt": "<matching passages, at most CPERSONA_RECALL_EXCERPT_CHARS, joined by ' … '>",
  "excerpt_basis": "blocks"
}
```

- **How it is made.** The record's blocks are ranked as the reconstruction
  exit ranks them (lexical overlap fused with Hamming distance), each is
  extended to the range that governs it (the sentence it sits in, and a
  qualifying neighbour), and the ranges are taken in that order while they fit
  the cap without overlapping. They are shown in text order, so the excerpt
  reads forwards. On the same benchmark the excerpt at 800 characters
  answered 341 of 500 — within the reader's run-to-run noise of the full
  records at 56% of their length — and at 500 characters, 318, against the
  prefix's 260 at the same size.
- **`excerpt_basis` says how.** `blocks`: the record's current block set.
  `lexical`: no current block set, so the record is divided at read time and
  ranked by shared words only. `start`: the record is one block, so the excerpt
  is its start.
- **Additive.** `content` is the same pure prefix it was, so `exclude_contents`
  and every consumer reading `content` see no change. The excerpt is absent
  under `full_content=true` (every row is whole), on rows the preview shows
  whole, and at the library layer: `do_recall` computes it only when the MCP
  boundary asks.
- **Deterministic.** No model is called; the query vector is the one the recall
  already embedded.

A later release may make the excerpt the default content and retire the prefix;
that change, like the preview's own, is a separate decision.

## Full-content access (two routes)

1. **`full_content: true`** — a new boolean parameter on both recall tools
   (default `false`), an opt-out for trusted consumers.

    Since bug-211 it is bounded by a 200,000-character per-response budget.
    Rows past that budget degrade back to the preview tier — the most relevant
    are kept whole first (bug-214) — and the response carries
    `full_content_budget_chars`.

    Unknown parameters are ignored by pre-2.5.0 servers, so consumers can
    adopt it before their connector upgrades (forward-compatible migration).
2. **`get_contents(agent_id, refs)`** — a new tool that batch-resolves up to
   20 refs to full rows.

    Reads are id-keyed (recall provenance) with the `agent_id` ownership
    predicate, so another agent's refs land in `missing` rather than leaking.
    Malformed refs land in `missing` too, because one bad ref must not abort
    the batch.

    The 20-ref cap exists because a full row is worth ~800 tokens: a larger
    batch would reopen the context-explosion hole the preview closes.

## Configuration

`CPERSONA_RECALL_PREVIEW_CHARS` — default **500**, decided at design review.
Long rows shed 60% or more of their tokens at that setting, while the preview
stays sufficient for a relevance judgement. `0` disables trimming entirely.

## Consumer impact and migration

| Consumer | Impact | Migration |
| --- | --- | --- |
| Bench harness (LMEB) | none — direct library calls; retrieval metrics are id-based | none |
| ClotoCore kernel (`build_chat_messages`, Discord bridge) | injects recall content verbatim into the LLM prompt — previews would degrade it | Phase 1: pass `full_content: true` (behavior-identical while a response stays under the bug-211 200,000-char budget — beyond it, rows degrade to previews and the kernel must fetch the rest via `get_contents`; safe to land before the connector bump). Phase 2 (optional): adopt previews + `get_contents` for kernel-side context diet |
| Claude Code sessions | primary beneficiary (~15k tokens/session projected) | none — previews by default, `get_contents` on demand |

## Versioning

The change is additive at the MCP protocol level — new fields, a new optional
parameter, a new tool — but the default content shape changes. That is a
deliberate breaking behaviour change, filed under the 2.5.0 "destructive but
internal" stabilization axis.

Consumers migrate during the 2.5.0 alpha line, before the stable connector
ships. Nothing is superseded at the DB layer: there is no schema change, and
SCHEMA_VERSION stays 13.