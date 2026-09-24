# FAQ

> **Applies to: CPersona {{ version_line }}.** Seeded from real questions asked by
> production operators (anonymized). The answers here are short; the canonical
> detail lives in [Behavior Contracts](behavior-contracts.md) and the
> [Operations Runbook](operations.md).

---

### Why does `recall` return the best match *last*?

This is a deliberate contract. Results are ordered by ascending score, so the
strongest memory sits at the end of the injected context, where LLMs attend
most strongly ("lost in the middle"). If you evaluate hit@k, index from the
tail: measuring from the head inverts your numbers. `recall_with_context` is
different — it returns a chronological merge.
→ [Contract §1](behavior-contracts.md#1-recall-return-order-last-is-best)

### My newest decisions keep losing to older ones. How do I make recency win?

In priority order:

1. **Do not bet must-win facts on recall at all.** Put the current decision in
   a deterministically injected surface (`CLAUDE.md` or a system prompt), and
   use memory for what is *asked for*, not for what must *always fire*.
2. **Overwrite, do not append.** `update_memory` the superseded decision. A
   stale decision that no longer exists cannot win.
3. Then, optionally, enable `CPERSONA_CONFIDENCE_ENABLED=true`, which blends
   time decay into the ranking. Be aware that it takes over ordering and the
   quality gate from the fusion mode, and run `calibrate_threshold` once after
   switching. Fine-grained recency *ranking* (recency-weighted search) is
   planned for the 2.6 line.

→ [When not to rely on recall](operations.md#when-not-to-rely-on-recall)

### Is `CPERSONA_CONFIDENCE_ENABLED=false` a "temporarily disabled" feature?

No. It is a conservative shipping default, not a flag disabled because
something is broken. Confidence changes ranking semantics, so it ships opt-in.
It is used in production: the maintainer's own instance runs `rsf` with
confidence on. If you enable it, know that it re-sorts results and re-keys the
quality gate.
→ [Contract §2](behavior-contracts.md#2-confidence-scoring-overrides-the-fusion-mode)

### How do I keep an index of Markdown files in sync with CPersona?

There is no built-in file watcher and no upsert: CPersona is a passive server,
and ingestion is caller-driven. Two patterns are supported. (A) A dedicated
`agent_id` for the index, rebuilt wholesale on change — recommended first,
because it is provably in sync and needs no diff logic. (B) A caller-side
content-hash ledger, with `update_memory` for changed chunks. The one trap:
re-storing *changed* content under the same `msg_id` is **skipped**, not
updated, and nothing says so.
→ [Corpus indexing patterns](operations.md#corpus-indexing-and-sync-patterns)

### What should I tune for a Japanese (or other CJK) corpus?

Set `CPERSONA_RECALL_MODE=rsf`, and that is all. The rsf mode exists largely
to compensate for FTS5's weak CJK tokenization. Expect the default embedding
model to be strong when query and memory share a proper-noun or identifier
anchor, and weaker on pure concept matches. Phrasing queries with a concrete
anchor term is the right adaptation.
→ [Japanese / CJK corpora](operations.md#japanese-and-cjk-corpora)

### Recall returns too few results. Which knob actually widens the gate?

`set_recall_precision(agent_id, "lenient")`. Under the default fusion modes it
is effectively the *only* policy knob. `CPERSONA_AUTOCUT_MIN_RESULTS` does
nothing under `rsf` or `rrf`, because autocut is deliberately inert on
rank-fusion scores, and disabling the fused gate entirely is a last resort.
→ [Tuning recall](operations.md#tuning-recall)

### What happens when the corpus grows past `CPERSONA_MAX_MEMORIES`?

Nothing is deleted and nothing breaks. The constant is the *vector scan
window*, not a storage cap. Rows older than the window stay reachable through
the FTS and keyword channels. For a large corpus, raise the environment
variable — that is the supported knob, and no archival routine is needed.
→ [Contract §4](behavior-contracts.md#4-the-vector-scan-window-cpersona_max_memories)

### How often should `archive_episode` run, and does bulk backfill hurt?

The intended cadence is one episode per session, at session end.

Backfilling is harmless under the defaults. It matters only if you have
turned on the episode boundary penalty (off by default from 2.6.0a7), which
softly prefers current-session memories by halving older ones at the floor.
Its boundary is simply the newest episode's timestamp, so with the penalty on,
**bulk-importing historical conversations moves the boundary to import time and
penalizes everything older**. Either do not backfill episodes, or keep the
penalty off (`CPERSONA_EPISODE_PENALTY_ENABLED=false`) while you do.
→ [Contract §3](behavior-contracts.md#3-episode-boundary-penalty)

### Does `lock_memory` make a memory rank higher?

No. Lock protects against deletion and editing. Ranking is unaffected, and a
locked memory can still lose a recall. "Must never be lost" → lock. "Must
always be in context" → deterministic injection.

The profile (`update_profile`) is a reliable always-surfaces channel only when
confidence scoring is on. With it off, profile rows carry no score and are cut
by `limit` on a full corpus.
→ [Contract §7](behavior-contracts.md#7-profile-rows-carry-no-score) /
[§9](behavior-contracts.md#9-lock_memory-protects-it-does-not-boost)

### Do I need to configure the operating context?

Not for single-client, single-agent setups. Leaving it unconfigured is the
correct state, not a gap. `operating-context.toml` exists for operators who
run *several* MCP clients against one server and want to distribute shared
operating instructions and a project-id registry to all of them.
→ [OPERATING_CONTEXT_DESIGN](OPERATING_CONTEXT_DESIGN.md)

### How do I back up the database safely?

Not with a plain `cp` while the server runs, because of WAL. Use
`sqlite3 ... ".backup ..."` or `VACUUM INTO`, or stop the server and copy the
`.db` with its `-wal` and `-shm` siblings. Complement that with a monthly
`export_memories` JSONL. Keep the live database out of cloud-sync folders.
→ [Backup & restore](operations.md#backup-and-restore)

### How do I notice the embedding server died?

You do not have to catch it yourself. Degraded recalls carry an `advisory`
field (instruct your agent to surface it), a `store` that writes a row reports
`embedded: true|false`, and `check_health(fix=true)` repairs rows written
during the outage.

Do not poll `embedded` alone. A `skipped` or `rejected` store omits the key, so
re-storing content the corpus already has tells you nothing about the encoder.
And a green `check_health` on its own does not prove the endpoint is up.
→ [Detecting a dead embedding server](operations.md#detecting-a-dead-embedding-server)

### Will CPersona ever merge or summarize memories with an LLM?

No. *The server never calls a generative model* is a core, unchanging
invariant. Embedding calls are the only model traffic, so memory itself adds no
API cost and stays deterministic.

Retrieval-side features planned for future lines stay within deterministic SQL
and pure-function processing, return reference-traceable results rather than
generated text, and never modify or replace the underlying memories. Semantic
summarization remains the calling agent's job, and `archive_episode` is where
its results land.

### Do I have to sponsor anything to use CPersona?

No. It is MIT-licensed, and nothing is withheld from anyone who does not
sponsor: no paid tier, no sponsor-only build, and no effect on how issues are
triaged. [Sponsorship](sponsorship.md) says what it does and does not buy, and
lists the ways to help that cost nothing.