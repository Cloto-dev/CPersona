# FAQ

> **Applies to: CPersona 2.5.x.** Seeded from real questions asked by
> production operators (anonymized). Short answers here; the canonical detail
> lives in [Behavior Contracts](behavior-contracts.md) and the
> [Operations Runbook](operations.md).

---

### Why does `recall` return the best match *last*?

Deliberate contract: results are ordered by ascending score so the strongest
memory sits at the end of the injected context, where LLMs attend most
strongly ("lost in the middle"). If you evaluate hit@k, index from the tail —
measuring from the head inverts your numbers. `recall_with_context` is
different: it returns a chronological merge.
→ [Contract §1](behavior-contracts.md#1-recall-return-order-last-is-best)

### My newest decisions keep losing to older ones. How do I make recency win?

In priority order:

1. **Don't bet must-win facts on recall at all** — put the current decision in
   a deterministically injected surface (`CLAUDE.md` / system prompt) and use
   memory for what is *asked for*, not what must *always fire*.
2. **Overwrite, don't append**: `update_memory` the superseded decision. A
   stale decision that no longer exists cannot win.
3. Then, optionally, enable `CPERSONA_CONFIDENCE_ENABLED=true` — it blends
   time decay into the ranking. Be aware it takes over ordering and the
   quality gate from the fusion mode, and run `calibrate_threshold` once
   after switching. Fine-grained recency *ranking* (recency-weighted search)
   is planned for the 2.6 line.

→ [When not to rely on recall](operations.md#when-not-to-rely-on-recall)

### Is `CPERSONA_CONFIDENCE_ENABLED=false` a "temporarily disabled" feature?

No — it is a conservative shipping default, not a disabled-because-broken
flag. Confidence changes ranking semantics, so it ships opt-in. It is used in
production (the maintainer's own instance runs `rsf` + confidence on). If you
enable it, know that it re-sorts results and re-keys the quality gate —
→ [Contract §2](behavior-contracts.md#2-confidence-scoring-overrides-the-fusion-mode).

### How do I keep an index of Markdown files in sync with CPersona?

There is no built-in file watcher or upsert — CPersona is a passive server
and ingestion is caller-driven. Two supported patterns: (A) a dedicated
`agent_id` for the index, rebuilt wholesale on change (recommended first —
provably in sync, no diff logic), or (B) caller-side content-hash ledger with
`update_memory` for changed chunks. The one trap: re-storing *changed*
content under the same `msg_id` is silently **skipped**, not updated.
→ [Corpus indexing patterns](operations.md#corpus-indexing-and-sync-patterns)

### What should I tune for a Japanese (or other CJK) corpus?

`CPERSONA_RECALL_MODE=rsf`, and that's it — the rsf mode exists largely to
compensate for FTS5's weak CJK tokenization. Expect the default embedding
model to be strong when query and memory share a proper-noun/identifier
anchor and weaker on pure concept matches; phrasing queries with a concrete
anchor term is the right adaptation.
→ [Japanese / CJK corpora](operations.md#japanese-and-cjk-corpora)

### Recall returns too few results. Which knob actually widens the gate?

`set_recall_precision(agent_id, "lenient")` — under the default fusion modes
it is effectively the *only* policy knob. `CPERSONA_AUTOCUT_MIN_RESULTS` does
nothing under `rsf`/`rrf` (autocut is deliberately inert on rank-fusion
scores), and disabling the fused gate entirely is a last resort.
→ [Tuning recall](operations.md#tuning-recall)

### What happens when the corpus grows past `CPERSONA_MAX_MEMORIES`?

Nothing is deleted and nothing breaks: the constant is the *vector scan
window*, not a storage cap. Rows older than the window stay reachable via the
FTS and keyword channels. For a large corpus, raise the env var — that is the
supported knob; no archival routine is needed.
→ [Contract §4](behavior-contracts.md#4-the-vector-scan-window-cpersona_max_memories)

### How often should `archive_episode` run, and does bulk backfill hurt?

Intended cadence: one episode per session, at session end. The episode
boundary penalty softly prefers current-session memories (halving older ones
at the floor) — and its boundary is simply the newest episode's timestamp, so
**bulk-importing historical conversations moves the boundary to import time
and penalizes everything older**. Don't backfill episodes, or disable the
penalty (`CPERSONA_EPISODE_PENALTY_ENABLED=false`) while you do.
→ [Contract §3](behavior-contracts.md#3-episode-boundary-penalty)

### Does `lock_memory` make a memory rank higher?

No. Lock protects against deletion and editing; ranking is unaffected, and a
locked memory can still lose a recall. "Must never be lost" → lock. "Must
always be in context" → deterministic injection. The profile
(`update_profile`) is only a reliable always-surfaces channel when confidence
scoring is on — with it off, profile rows carry no score and are cut by
`limit` on a full corpus.
→ [Contract §7](behavior-contracts.md#7-profile-rows-carry-no-score) /
[§9](behavior-contracts.md#9-lock_memory-protects-it-does-not-boost)

### Do I need to configure the operating context?

Not for single-client, single-agent setups — leaving it unconfigured is the
correct state, not a gap. `operating-context.toml` exists for operators who
run *several* MCP clients against one server and want to distribute shared
operating instructions and a project-id registry to all of them.
→ [OPERATING_CONTEXT_DESIGN](OPERATING_CONTEXT_DESIGN.md)

### How do I back up the database safely?

Not with a plain `cp` while the server runs (WAL). Use
`sqlite3 ... ".backup ..."` or `VACUUM INTO`, or stop the server and copy the
`.db` with its `-wal`/`-shm` siblings; complement with a monthly
`export_memories` JSONL. Keep the live DB out of cloud-sync folders.
→ [Backup & restore](operations.md#backup-and-restore)

### How do I notice the embedding server died?

You don't have to catch it yourself: degraded recalls carry an `advisory`
field (instruct your agent to surface it), a `store` that writes a row reports
`embedded: true|false`, and `check_health(fix=true)` repairs rows written during
the outage. Do not poll `embedded` alone: a `skipped` or `rejected` store omits
the key, so re-storing content the corpus already has tells you nothing about
the encoder. Note that a green `check_health` alone does not prove the endpoint is
up.
→ [Detecting a dead embedding server](operations.md#detecting-a-dead-embedding-server)

### Will CPersona ever merge or summarize memories with an LLM?

No. *The server never calls a generative model* is a core, unchanging
invariant — embedding calls are the only model traffic, so memory itself adds
no API cost and stays deterministic. Retrieval-side features planned for
future lines stay within deterministic SQL + pure-function processing, return
reference-traceable results rather than generated text, and never modify or
replace the underlying memories. Semantic summarization remains the calling
agent's job (`archive_episode` is where its results land).

### Do I have to sponsor anything to use CPersona?

No. It is MIT-licensed, and nothing is withheld from anyone who does not
sponsor — no paid tier, no sponsor-only build, and no effect on how issues are
triaged. [Sponsorship](sponsorship.md) says what it does and does not buy, and
lists the ways to help that cost nothing.
