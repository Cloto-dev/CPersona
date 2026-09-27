# Overflow Tree — design

Status: design for the 2.6 line, not behaviour. This page fixes the storage
layer that lets a long record be quoted by the part that matters instead of by
its first few hundred characters. Its first use is excerpt selection for
[Reconstructive Recall](RELIABLE_RECALL_2_6.md#7-reconstructive-recall-the-exit).
It does not change which records recall returns or in what order.

## 0. The defect, and what this first step does about it

An embedding model reads a bounded number of tokens. Everything past that
window is stored and searchable by the lexical arm, but it does not reach the
vector, and when a long record is returned, the reader gets a preview cut from
its start. Measured on a production corpus of 1,625 rows with bge-m3 at a
512-token window, the window closes anywhere between character 739 and
character 1,787 depending on the text. No character constant can stand in for
it.

The embedding server now reports that position for each text
(`POST /count_tokens`, CEmbedding 0.8.0). This design uses it to divide a long
record into **nodes**: consecutive spans of the record, each small enough to be
embedded whole.

The first step is deliberately narrow:

| | this design | decided separately |
|---|---|---|
| Nodes are built, stored and embedded | yes | |
| A returned record is quoted by its most relevant node | enabled (used by reconstructive recall) | |
| Nodes are search candidates, so a tail can bring its record into the results | **no** | yes — see §7 |
| The store length limit is removed | **no** | yes — see §7 |

Because nodes stay out of the search index, the records recall returns and
their order are unchanged by construction. The retrieval gain that indexing
nodes would bring has been measured, and so has its cost to records that were
never split; that trade is a separate decision with its own preregistration.

## 1. Schema

A new table. Nothing in `memories` or `episodes` changes.

```sql
CREATE TABLE IF NOT EXISTS record_nodes (
    parent_kind     TEXT    NOT NULL,   -- 'mem' | 'ep'
    parent_id       INTEGER NOT NULL,
    node_index      INTEGER NOT NULL,   -- 0, 1, 2, ... in text order
    start_char      INTEGER NOT NULL,   -- inclusive offset into the parent text
    end_char        INTEGER NOT NULL,   -- exclusive offset
    token_count     INTEGER NOT NULL,   -- as counted by the embedding server
    window          INTEGER NOT NULL,   -- the window the span was cut against
    embedding       BLOB,
    embedding_model TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (parent_kind, parent_id, node_index)
);
```

- **A node stores offsets, not text.** Its text is
  `parent_text[start_char:end_char]`. The stored memory is never copied or
  modified, and the table adds no second copy of any record.
- **A separate table, not rows in `memories`.** More than eighty queries in the
  package read `memories` (recall, listing, deduplication, counting, health,
  export, deletion, the isolation axes). A node stored as a memory row would
  need an exclusion in every one of them, and a single omission would put nodes
  into results or counts. A separate table leaves every existing path
  unchanged.
- **One table for memories and episodes.** The longest records are episode
  summaries, so episodes are covered from the start.
- The migration adds a table and its triggers. No existing table is
  restructured.

## 2. How a record is divided

Only a record whose text runs past the window is divided. A record that fits
has no nodes: the record itself is its only span.

The rule, applied to the remaining text until nothing remains:

1. Ask the embedding server where the window closes on the remaining text
   (`window_end_char`). If the remaining text fits, it is the last node.
2. Cut at the last boundary at or before that position, taking boundary classes
   in this order: a blank line (paragraph), a line break (line or list item), a
   sentence end, whitespace. A full-width `。` `．` `！` `？` ends a sentence
   wherever it stands, because Japanese and Chinese prose puts the next sentence
   directly after it; a half-width `.` `!` `?` counts only before whitespace or
   the end of the text, so a decimal point or a file extension is not a sentence
   end.
3. A boundary that would leave the node shorter than half of `window_end_char`
   is not taken; the next class is tried. This keeps a stray early paragraph
   break from producing a node of a few words.
4. With no acceptable boundary in any class, cut at `window_end_char` itself.
5. Continue with the text after the cut.

Properties this gives:

- **Every node fits the window.** Each cut is measured on the span that will be
  embedded, not derived from a whole-text count, so no node is truncated by the
  model. No character constant is involved, and a different model or window
  yields its own division.
- **Deterministic.** The same text, tokenizer and window give the same nodes.
- **Contiguous and complete.** Nodes cover `[0, len(text))` with no gap and no
  overlap. Overlap is not used in this step.
- **Unknown is not "fits".** When the embedding server cannot report tokens
  (a remote API, an older server, embeddings turned off), no nodes are built,
  and a record is quoted from its start as it is today.

## 3. When nodes are built

Node construction runs on the existing memory task queue, not inside `store`.
A 16,000-character record is about fourteen nodes, and embedding each inline
would add seconds to a call that returns in milliseconds today.

- `store` and `archive_episode` answer as they do now. When the record runs
  past the window, the response adds `nodes: {"status": "queued"}`. A record
  that fits adds nothing.
- Until its nodes exist, a record is quoted from its start. Pending nodes never
  make a read fail or wait.
- With the task queue disabled, nodes are not built at write time. A health
  check (`check_health`, `missing_nodes`) reports long records without nodes,
  and its repair builds them. That check is also how records written before
  this feature get nodes, how a failed build is retried, and how nodes from a
  previous embedding model are replaced: the repair adds nodes and never
  modifies the record, so it covers locked records too. One run builds at most
  50 records, divided and embedded before the write lock is taken; a larger
  backlog converges over successive runs.

## 4. Keeping nodes true to their parent

Triggers, not call sites, keep nodes consistent, because a trigger also covers
paths written after this design:

- deleting a memory or an episode deletes its nodes;
- changing a memory's `content` or an episode's `summary` deletes its nodes and
  the record is queued for new ones.

Memory and episode ids are never reused (`AUTOINCREMENT`), so a node cannot
come to point at a different record. A node whose `embedding_model` differs from
the current model is treated as missing and rebuilt, as record embeddings are.
Nodes are not exported: they are derived from text the export already carries,
and an import rebuilds them. A node is read only through its parent, so it
inherits the parent's isolation axes and access control.

## 5. Invariants

1. **Retrieval is unchanged.** Recall and reconstruct return the same records
   in the same order with or without nodes. Test: build nodes for a corpus and
   compare result ids against the same corpus without nodes; a mutation that
   admits nodes into vector search must turn the test red.
2. **Stored records are not modified** by building, rebuilding or deleting
   nodes.
3. **Every node's `token_count` is at most its `window`**, as counted on the
   node's own span.
4. **Nodes partition their parent**: sorted by `node_index`, the first starts at
   0, each starts where the previous ended, and the last ends at the parent's
   length.
5. **Division is deterministic** for a given text, tokenizer and window.
6. **No stale node survives** a delete or a text change of its parent.

## 6. What the nodes are for in this step

Reconstructive recall selects records first. Then, for each claim in an item,
it scores that record's nodes against the query and quotes the most relevant
node instead of the record's first characters. The scoring rule belongs to
reconstructive recall, not here. This design only guarantees that every long
record offers bounded, embedded, addressable spans to score.

The same offsets give the expansion path a smaller unit: a caller can fetch
one node rather than the whole record.

## 7. Decided separately

- **Nodes as search candidates.** Measured on long records with queries that
  share no wording with the tail, in one ranking configuration: indexing nodes
  raised top-10 reach by 24.7 points and rank-1 by 10.6 points. The size of the
  gain depended on that configuration. It also lowered records that were never
  divided by 3.6 points, because long records occupied more of the index. That
  loss applies to every query, so indexing wins only when enough queries aim at
  tails. That share has not been measured yet, and neither has a two-stage
  variant that keeps the parent ranking fixed and merges node hits below it.
  Both come before the decision.
- **Removing the store length limit.** The limit stays at 16,000 characters. It
  is a write-path change and does not belong to reconstructive recall. Removing
  it matters most together with nodes as search candidates, because a tail no
  search can reach gains little from being longer.

## 8. How the step is judged

- **Retrieval unchanged**: invariant 1, under mutation.
- **Division quality**: the share of nodes that begin inside a sentence, on a
  real corpus.
- **Report accuracy**: `nodes.status` and the node table agree with the stored
  records.
- **Write cost**: `store` latency with node construction queued; queue
  throughput per node.
- **Storage**: bytes added per divided record.
