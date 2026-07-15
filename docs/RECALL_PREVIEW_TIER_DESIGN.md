# Recall Preview Tier Design (v2.5.0)

Status: approved 2026-07-15 (design review), shipped on the 2.5.0 pre-release line.
Scope: `recall` / `recall_with_context` MCP responses, the new `get_contents` tool.

## Problem

The recall tools return the **full content of every hit**. Measured on the
Claude Code environment (token inventory, 2026-07-11): ~815 tokens per memory,
~1,040 per episode, ≈9–10k tokens per `limit=10` call, ×5.2 calls/session ≈
**23k tokens/session** — the single largest recall-side context cost. Most of
that text is never read: the caller usually needs a relevance judgement over
the list and the full text of one or two rows.

## Design principle (shared with the v2.5.0 limit re-layering)

**Shaping and capping belong to the boundary layer; the library returns full
data.** The Task #190 change moved the agent-facing `limit` cap into the MCP
JSON Schema while the library clamps only to the scan window. The preview tier
is the same move for payload shape:

- `do_recall` / `do_recall_with_context` (library) — full content, unchanged.
- The MCP tool wrappers (`server.py`) — trim content to the preview tier
  unless the caller opts out.

This placement makes the bench harness (direct library calls) and any future
in-process consumer structurally immune to the diet: only MCP-path consumers
see previews.

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

- **`ref` is new and always present** (memories and episodes). Before 2.5.0
  the response exposed only `msg_id` (absent on episodes) — there was no way
  to address a row for a follow-up fetch. Kinds are prefixed (`mem:`/`ep:`)
  because both tables share one AUTOINCREMENT id space (the bug-040/041
  collision class).
- **The preview is a pure prefix — no ellipsis marker.** The
  `exclude_contents` dedup contract (`_content_excluded`) is a normalized
  bidirectional starts-with match; a preview fed back into a later call still
  matches the stored full text. An embedded marker would silently break that.
- **Markers only when trimmed.** Short rows carry neither
  `content_truncated` nor `content_len` (payload-diet consistency).
- `recall_with_context`'s echoed conversation entries are trimmed uniformly
  (the caller already holds their full text); they carry no `ref`.

## Full-content access (two routes)

1. **`full_content: true`** — a new boolean parameter on both recall tools
   (default `false`): wholesale opt-out for trusted consumers. Unknown
   parameters are ignored by pre-2.5.0 servers, so consumers can adopt it
   before their connector upgrades (forward-compatible migration).
2. **`get_contents(agent_id, refs)`** — new tool (27 → 28): batch-resolves up
   to 20 refs to full rows. Reads are id-keyed (recall provenance) with the
   `agent_id` ownership predicate; another agent's refs land in `missing`,
   never in a leak. Malformed refs also land in `missing` (fail-soft — one bad
   ref must not abort the batch). The 20-ref cap exists because a full row is
   worth ~800 tokens: a larger batch would reopen the context-explosion hole
   the preview closes.

## Configuration

`CPERSONA_RECALL_PREVIEW_CHARS` — default **500** (decided at design review;
long rows shed ~60%+ of their tokens while the preview stays sufficient for
relevance judgement). `0` disables trimming entirely.

## Consumer impact and migration

| Consumer | Impact | Migration |
| --- | --- | --- |
| Bench harness (LMEB) | none — direct library calls; retrieval metrics are id-based | none |
| ClotoCore kernel (`build_chat_messages`, Discord bridge) | injects recall content verbatim into the LLM prompt — previews would degrade it | Phase 1: pass `full_content: true` (behavior-identical; safe to land before the connector bump). Phase 2 (optional): adopt previews + `get_contents` for kernel-side context diet |
| Claude Code sessions | primary beneficiary (~15k tokens/session projected) | none — previews by default, `get_contents` on demand |

## Versioning

MCP-protocol-additive (new fields, new optional parameter, new tool) but the
default content shape changes — a deliberate breaking behavior change, filed
under the 2.5.0 "destructive but internal" stabilization axis. Consumers
migrate during the 2.5.0 alpha line, before the stable connector ships.
Supersedes nothing at the DB layer: no schema change (SCHEMA_VERSION stays 13).
