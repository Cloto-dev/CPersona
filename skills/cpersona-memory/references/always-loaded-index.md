# Using an always-loaded index over this store

Some MCP clients keep a small file that is injected into **every** session —
Claude Code's `MEMORY.md` is the one this reference is written against. This
page is about pairing such a file with CPersona. It is loaded on demand; the
one-line rule that belongs in every session lives in the policy block in
`SKILL.md`.

Read this before moving any existing memory. **The migration is not a setup
step and this skill will not perform it for you** — see "Moving existing
memories" below for why.

## Why pair them at all

The two channels fail in opposite directions, and that is the whole value:

| | always-loaded index | CPersona |
| --- | --- | --- |
| arrives | **deterministically**, every session | when a `recall` happens to retrieve it |
| holds | one line per memory | the full text, vector-searchable |
| bounded by | a hard size cap | corpus size |
| readable by | one client | every client sharing the `agent_id` |

An index alone cannot hold bodies; it runs out of room. A store alone cannot
guarantee that an agent ever learns a memory exists, because retrieval is
ranked and a query that does not resemble the memory will not surface it. Put
the pointers where arrival is certain and the bodies where search works, and
each channel does the thing the other cannot.

## The shape

One line per memory in the index:

```
- <slug> — <one sentence that changes behaviour without opening anything>
```

The line is not a title. It is the part that has to work when nothing else
loads, so write the conclusion, not the topic: `never delete the -wal file` is
a line, `about WAL files` is not. Keep it short enough that the index stays
well under its cap — a line that costs 180 bytes buys nothing a 90-byte line
does not.

The body goes to CPersona with the slug as the join key, so that reading the
line tells the agent exactly what to ask for:

```
store(
  agent_id="<AGENT_ID>",
  message={
    "id": "memory-index:<slug>",          # dedup key: re-storing edits, never duplicates
    "content": "[<slug>] <the full memory>",
  },
)
```

Retrieve it with `recall(agent_id="<AGENT_ID>", query="<slug>")`. The prefix is
what makes the join checkable in both directions (below); `message.id` is what
makes a re-store idempotent, because CPersona deduplicates on it.

### Not everything should move

Split by whether the memory is **fired** or **queried**:

- **Fired** — behavioural rules that must act without anyone asking for them
  ("always do X", "never do Y"). The index line is the working copy; the body
  in CPersona is the evidence and the reasoning.
- **Queried** — lookups you open on purpose (a runbook, a table of endpoints,
  a design decision you cite). These want a real file at a real path, because
  citing one means naming where it is. Keep the body wherever your client
  keeps files, and give it an index line too.

Splitting this way also keeps the index honest about its own cost: the bodies
that grow without bound are exactly the ones that left.

## Keeping the two in step

The index and the store can disagree in two directions, and **both are silent**:

- a line whose row was never written, or was deleted — it costs its bytes in
  every session and points at nothing;
- a row with no line — it is in the store, and no session is told it exists.

Nothing detects either one for you. `check_health` audits the store's internal
consistency, not its agreement with a file it has never heard of. So check the
join yourself, on the two sets of slugs:

```bash
# every slug named by an index line
grep -oE '^- [a-z0-9-]+' MEMORY.md | sed 's/^- //' | sort > /tmp/index-slugs

# every slug stored under the prefix, via list_memories on the agent
#   -> extract the text between "[" and "]" at the start of each content
#   -> sort into /tmp/store-slugs

comm -23 /tmp/index-slugs /tmp/store-slugs   # lines with nothing behind them
comm -13 /tmp/index-slugs /tmp/store-slugs   # rows no session will hear about
```

Both outputs empty is the invariant. Run it after any bulk change, and treat a
non-empty result as a defect rather than as drift to be tidied later — a
half-migrated corpus is worse than either whole one, because neither channel
can be trusted on its own.

## The cap is the binding constraint

The index has a hard limit (Claude Code's is 25,000 bytes / 200 lines), it is
paid in every session, and **exceeding it does not raise an error** — the
content is simply not injected, which from inside the session is
indistinguishable from having no memories at all.

So decide the rule before you need it:

- Set a working threshold well below the cap (80% is a reasonable warning
  line) and act at the threshold, not at the limit.
- At the threshold, **consolidate rather than compress**. Merging several
  related lines into one durable document — one you can cite, and reference by
  a single line — reduces the growth rate. Shortening existing lines is a
  one-off saving that buys a few sessions and changes nothing about the slope.
- Never solve a full index by deleting entries you have not read. A line you
  cannot justify is a line whose body you should read first; the body is in
  the store, which is the point of putting it there.

## Moving existing memories

**Ask before moving anything, and do the move yourself rather than having the
agent do it unattended.** This skill deliberately does not automate it:

- It is destructive in a way that is hard to see. If the bodies are deleted
  from their old location after being stored, the old location is the only
  copy that existed; if they are not deleted, you now have two copies that
  will drift.
- It is not idempotent until the join key exists. Re-running a half-finished
  move without `message.id` set as above writes second copies of everything it
  already wrote.
- It is unbounded. A corpus of any size is a long sequence of writes, and
  nothing reports that it stopped halfway.

If you do move a corpus, the order that survives an interruption is: store the
body (with `message.id`), verify the row, then rewrite the index line, then
remove the old body — one memory at a time, so an interruption leaves a
consistent prefix rather than a scattered mixture. Run the join check above
afterwards, in both directions, and read the result rather than the exit code
of the loop.
