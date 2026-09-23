"""Query-relevant excerpts of the records a LongMemEval retrieval run returned,
cut by the server's own quotation code, for `longmemeval_reader.py --excerpts`.

The question this serves: does showing the part of a record that matched, under
a character cap, recover what the recall preview's fixed prefix loses? The
excerpt is made by the same functions `reconstruct` quotes with — the block
divider (`blocks.segment`), the block ranking (`reconstruct.rank_blocks`:
lexical trigrams fused with Hamming distance), the governing-context rule
(`blocks.context_range`) and the cap (`reconstruct._quote`) — so what is
measured is what would ship. A record that divides into one block has no block
set, and is quoted from its start, as the server quotes it.

Two ways of cutting are produced for each cap:

  quote   what `reconstruct` quotes today: the best block's governing context,
          one passage per record, cut at the cap (it does not fill the cap)
  fill    a candidate rule: governing contexts of the blocks in the server's
          ranking order, added while they fit the cap without overlapping, then
          shown in text order joined by " … ". The ranking and the context rule
          are the server's; filling to the cap is not, and would be new code

Three steps, because the divider and the quotation need this package and the
embedding needs sentence-transformers, which it does not depend on:

  spans   divide every returned record (the stored text: title + ' ' + text)
  embed   embed each block and each question with BAAI/bge-m3, normalized
  quote   rank, extend and cut, for each (question, returned record) and cap

One difference from a deployment is stated rather than hidden: the server
never lets a block cross an overflow-tree node boundary, and node boundaries
come from the embedding server's token counts, which this offline step does
not have. Blocks here are divided without node bounds. Only records past the
512-token window have nodes at all.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace


def _returned(rankings, k=10):
    out = {}
    for line in open(rankings, encoding="utf-8"):
        row = json.loads(line)
        if not row.get("header"):
            scene = row["query_id"].split("_q_", 1)[0]
            out[row["query_id"]] = [d for d in row["returned_ids"][:k] if d.startswith(scene + "_session_")]
    return out


def cmd_spans(args):
    from cpersona import blocks

    wanted = {d for docs in _returned(args.rankings).values() for d in docs}
    with open(args.out, "w", encoding="utf-8") as out:
        for line in open(Path(args.lmeb_dir) / "corpus.jsonl", encoding="utf-8"):
            row = json.loads(line)
            if row["id"] not in wanted:
                continue
            content = f"{row.get('title', '')} {row['text']}"
            spans = [(s.start, s.end) for s in blocks.segment(content)]
            out.write(json.dumps({"id": row["id"], "content": content, "spans": spans}, ensure_ascii=False) + "\n")


def cmd_embed(args):
    import numpy as np
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer("BAAI/bge-m3", device=args.device)
    model.max_seq_length = 512
    texts = []
    for line in open(args.spans, encoding="utf-8"):
        row = json.loads(line)
        if len(row["spans"]) > 1:
            texts += [row["content"][s:e] for s, e in row["spans"]]
    queries = {}
    for sub in Path(args.lmeb_dir).iterdir():
        if (sub / "queries.jsonl").exists():
            for line in open(sub / "queries.jsonl", encoding="utf-8"):
                q = json.loads(line)
                queries[q["id"]] = q["text"]
    unique = sorted(set(texts) | set(queries.values()))
    vectors = model.encode(unique, normalize_embeddings=True, batch_size=64,
                           show_progress_bar=False, convert_to_numpy=True).astype(np.float32)
    np.save(args.out, vectors)
    Path(args.out).with_suffix(".texts.json").write_text(json.dumps(unique, ensure_ascii=False))


SEPARATOR = " … "


def fill(content, spans, ranked, cap):
    """Governing contexts in ranking order, while they fit ``cap``, shown in text order."""
    from cpersona import blocks

    chosen, used = [], 0
    for index, _, _, _ in ranked:
        start, end, _ = blocks.context_range(content, spans, index)
        if any(start < e and s < end for s, e in chosen):
            continue
        cost = (end - start) + (len(SEPARATOR) if chosen else 0)
        if used + cost > cap:
            if not chosen:  # the best passage alone is longer than the cap: cut it, as a quote is
                chosen.append((start, start + cap))
            break
        chosen.append((start, end))
        used += cost
    return SEPARATOR.join(content[s:e] for s, e in sorted(chosen))


def cmd_quote(args):
    import numpy as np

    from cpersona import blocks, reconstruct

    texts = json.loads(Path(args.emb).with_suffix(".texts.json").read_text())
    vectors = np.load(args.emb)
    index = {t: i for i, t in enumerate(texts)}
    records = {}
    for line in open(args.spans, encoding="utf-8"):
        row = json.loads(line)
        records[row["id"]] = row
    queries = {}
    for sub in Path(args.lmeb_dir).iterdir():
        if (sub / "queries.jsonl").exists():
            for line in open(sub / "queries.jsonl", encoding="utf-8"):
                q = json.loads(line)
                queries[q["id"]] = q["text"]
    caps = [int(c) for c in args.caps.split(",")]
    out = {}
    for qid, docs in _returned(args.rankings).items():
        query = queries[qid]
        query_bits = blocks.pack_bits(vectors[index[query]].tolist())
        grams = reconstruct._trigrams(query)
        for d in docs:
            row = records[d]
            content, spans = row["content"], [tuple(s) for s in row["spans"]]
            block_sets = {}
            if len(spans) > 1:
                block_rows = [(i, s, e, blocks.pack_bits(vectors[index[content[s:e]]].tolist()))
                              for i, (s, e) in enumerate(spans)]
                block_sets[d] = (content, block_rows)
            claim = SimpleNamespace(ref=d, content=content)
            ranked = (reconstruct.rank_blocks(content, block_sets[d][1], query_bits, grams)
                      if block_sets else None)
            for cap in caps:
                quote = reconstruct._quote(claim, {}, None, grams, cap,
                                           block_sets=block_sets, query_bits=query_bits)
                cell = out.setdefault(qid, {}).setdefault(d, {})
                cell[f"quote{cap}"] = quote["content"]
                # A record without blocks is quoted from its start by both rules.
                cell[f"fill{cap}"] = fill(content, spans, ranked, cap) if ranked else content[:cap]
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("spans")
    p.add_argument("--rankings", required=True)
    p.add_argument("--lmeb_dir", required=True)
    p.add_argument("--out", required=True)
    p = sub.add_parser("embed")
    p.add_argument("--spans", required=True)
    p.add_argument("--lmeb_dir", required=True)
    p.add_argument("--out", required=True, help=".npy; the texts go beside it")
    p.add_argument("--device", default="mps")
    p = sub.add_parser("quote")
    p.add_argument("--spans", required=True)
    p.add_argument("--emb", required=True)
    p.add_argument("--rankings", required=True)
    p.add_argument("--lmeb_dir", required=True)
    p.add_argument("--caps", default="500,800")
    p.add_argument("--out", required=True)
    args = parser.parse_args()
    {"spans": cmd_spans, "embed": cmd_embed, "quote": cmd_quote}[args.cmd](args)


if __name__ == "__main__":
    main()
