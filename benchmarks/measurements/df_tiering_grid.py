"""Grid a document-frequency tiering rule for the FTS5 arm: does it pay?

Background. `_build_fts_query` turns a query into an OR of terms: each CJK run
becomes its overlapping trigrams, each ASCII run of 3+ characters is kept whole.
Profiling the recall path showed the lexical arm's time is bm25 evaluation, and
that it is proportional to the number of rows the MATCH selects (~2.2 us per
matching row at 100k rows), not to the size of the corpus. So the way to make
the arm cheaper without shortening its reach is to stop OR-ing in the terms that
select huge numbers of rows while contributing least to the ranking.

The rule under test:

    keep the terms whose document frequency is at or below `cutoff` of the
    corpus; if fewer than K terms survive, put the dropped ones back in
    ascending df order until K remain.

Two df sources, because the obvious one is not available at run time:

  exact  COUNT(*) ... MATCH '"term"'. The truth. It walks that term's postings,
         which costs what the query itself costs, so it exists here only as the
         offline yardstick the deployable source is judged against.
  proxy  min over the term's own trigram dfs, read from an fts5vocab table
         created in the TEMP schema (no schema change to the corpus). A document
         containing the phrase contains all of its trigrams, so the minimum is
         an upper bound on the phrase's df.

Two numbers per cell:

  cost      rows the MATCH selects, summed over the query set.
  fidelity  |top10(full) & top10(tiered)| under the statement recall really
            issues (JOIN + isolation + ORDER BY rank LIMIT 10), so a setting is
            scored on the rows recall would have returned, not on a bare MATCH.

The query set matters more than the grid does. Queries invented while writing a
benchmark inherit the author's habits -- term count, and how much of the text is
ASCII -- and both drive this rule directly. Supply real ones:

    python df_tiering_grid.py CORPUS.db QUERIES.json

where QUERIES.json is a list of {"query": "...", "n": <times issued>} (n is
optional, default 1, and only weights the reported totals). The corpus is opened
read-only; nothing is written to it.

Note for anyone extending this: PRAGMA query_only=1 cannot be set here. It
forbids every write including TEMP objects, so the fts5vocab table below fails
under it. Opening the file with mode=ro already makes the corpus unwritable.
"""

from __future__ import annotations

import json
import os
import sqlite3
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from cpersona.isolation import isolation_where  # noqa: E402
from cpersona.memory_handlers import _build_fts_query  # noqa: E402

CUTOFFS = (0.05, 0.10, 0.15, 0.20, 0.30)
KS = (2, 3, 4, 5, 6, "len/2")
AGENT = os.environ.get("AGENT_ID", "claude-code")


class Corpus:
    def __init__(self, path: str) -> None:
        self.db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self.db.execute("CREATE VIRTUAL TABLE temp.v USING fts5vocab(main, memories_fts, 'row')")
        self.rows = self.db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        iso = isolation_where(agent_id=AGENT, project_id=None, channel="", alias="m")
        self.iso_params = iso.params
        self.rank_sql = (
            "SELECT m.id FROM memories_fts f JOIN memories m ON f.rowid = m.id "
            f"WHERE memories_fts MATCH ? AND {iso.clause} ORDER BY rank LIMIT 10"
        )
        self._vocab: dict[str, int] = {}
        self._exact: dict[str, int] = {}

    @staticmethod
    def quote(term: str) -> str:
        return '"' + term.replace('"', '""') + '"'

    def expr(self, terms: list[str]) -> str:
        return " OR ".join(self.quote(t) for t in terms)

    def count(self, expr: str) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM memories_fts WHERE memories_fts MATCH ?", (expr,)
        ).fetchone()[0]

    def top10(self, expr: str) -> list[int]:
        return [r[0] for r in self.db.execute(self.rank_sql, (expr, *self.iso_params))]

    def timed(self, expr: str, reps: int = 5) -> float:
        self.db.execute(self.rank_sql, (expr, *self.iso_params)).fetchall()
        samples = []
        for _ in range(reps):
            t0 = time.perf_counter()
            self.db.execute(self.rank_sql, (expr, *self.iso_params)).fetchall()
            samples.append((time.perf_counter() - t0) * 1000)
        return statistics.median(samples)

    def vocab_df(self, trigram: str) -> int:
        if trigram not in self._vocab:
            row = self.db.execute("SELECT doc FROM temp.v WHERE term = ?", (trigram,)).fetchone()
            self._vocab[trigram] = row[0] if row else 0
        return self._vocab[trigram]

    def exact_df(self, term: str) -> int:
        if term not in self._exact:
            self._exact[term] = self.count(self.quote(term))
        return self._exact[term]

    def proxy_df(self, term: str) -> int:
        # A term the builder kept whole is a phrase over a trigram index, so it
        # has no vocab entry of its own -- asking for one answers 0, which any
        # tiering rule reads as "rarest possible". Take the minimum over its
        # trigrams instead. The tokenizer folds case, so lowercase first.
        s = term.lower()
        if len(s) < 3:
            return 0
        return min(self.vocab_df(s[i : i + 3]) for i in range(len(s) - 2))


def terms_of(expr: str) -> list[str]:
    if not expr:
        return []
    return [t.strip().strip('"').replace('""', '"') for t in expr.split(" OR ")]


def tier(terms: list[str], dfs: dict[str, int], cutoff_rows: float, k: int) -> list[str]:
    kept = [t for t in terms if dfs[t] <= cutoff_rows]
    if len(kept) >= k:
        return kept
    held = set(kept)
    back = sorted((t for t in terms if t not in held), key=lambda t: dfs[t])
    restored = held | set(back[: max(0, k - len(kept))])
    return [t for t in terms if t in restored]


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: df_tiering_grid.py CORPUS.db QUERIES.json", file=sys.stderr)
        print(
            'QUERIES.json is a list of {"query": "...", "n": <times issued>}; '
            "supply real queries, not invented ones -- see the module docstring.",
            file=sys.stderr,
        )
        return 2
    corpus = Corpus(sys.argv[1])
    with open(sys.argv[2], encoding="utf-8") as fh:
        raw = json.load(fh)

    prepared = []
    for item in raw:
        text = item["query"] if isinstance(item, dict) else item
        weight = item.get("n", 1) if isinstance(item, dict) else 1
        terms = terms_of(_build_fts_query(text))
        if not terms:
            continue
        expr = corpus.expr(terms)
        matched = corpus.count(expr)
        if matched == 0:
            continue
        prepared.append(
            {
                "terms": terms,
                "n": weight,
                "count": matched,
                "top": corpus.top10(expr),
                "exact": {t: corpus.exact_df(t) for t in terms},
                "proxy": {t: corpus.proxy_df(t) for t in terms},
            }
        )

    n = len(prepared)
    if not n:
        print("no query produced a usable MATCH expression", file=sys.stderr)
        return 1
    full_total = sum(p["count"] for p in prepared)
    full_worst = max(p["count"] for p in prepared)
    term_counts = [len(p["terms"]) for p in prepared]
    print(
        f"corpus rows={corpus.rows}  queries={n}  distinct terms={len(corpus._exact)}\n"
        f"terms/query: median {statistics.median(term_counts):.0f} "
        f"(min {min(term_counts)}, max {max(term_counts)})   "
        f"match rate: median {100 * statistics.median([p['count'] for p in prepared]) / corpus.rows:.1f}%, "
        f"max {100 * full_worst / corpus.rows:.1f}%\n"
    )

    header = (
        f"{'df':<6} {'cutoff':>7} {'K':>6} | {'matched':>9} {'gain':>7} {'worst':>6} "
        f"| {'intact':>9} {'>=8/10':>9} {'min':>4} {'top1':>9}"
    )
    print(header)
    print("-" * len(header))
    print(
        f"{'full':<6} {'-':>7} {'-':>6} | {full_total:>9} {'1.00x':>7} {full_worst:>6} "
        f"| {n:>4}/{n:<4} {n:>4}/{n:<4} {'10':>4} {n:>4}/{n:<4}"
    )

    best = None
    for src in ("exact", "proxy"):
        for cutoff in CUTOFFS:
            for k in KS:
                total = worst = intact = ge8 = top1 = 0
                lowest = 10
                for p in prepared:
                    kk = max(1, len(p["terms"]) // 2) if k == "len/2" else k
                    kept = tier(p["terms"], p[src], cutoff * corpus.rows, kk)
                    if kept == p["terms"]:
                        matched, top = p["count"], p["top"]
                    else:
                        expr = corpus.expr(kept)
                        matched, top = corpus.count(expr), corpus.top10(expr)
                    base = len(p["top"])
                    agree = len(set(p["top"]) & set(top))
                    total += matched
                    worst = max(worst, matched)
                    intact += agree == base
                    ge8 += agree >= min(8, base)
                    top1 += bool(top) and top[0] == p["top"][0]
                    if base >= 10:
                        lowest = min(lowest, agree)
                gain = full_total / total
                print(
                    f"{src:<6} {cutoff * 100:>6.0f}% {str(k):>6} | {total:>9} {gain:>6.2f}x {worst:>6} "
                    f"| {intact:>4}/{n:<4} {ge8:>4}/{n:<4} {lowest:>4} {top1:>4}/{n:<4}"
                )
                # The cheapest cell that moves no answer below 8 of 10 and keeps
                # the first hit for all but one query -- reported so the run ends
                # on a candidate rather than on a wall of numbers.
                if src == "proxy" and lowest >= 8 and top1 >= n - 1:
                    if best is None or gain > best[0]:
                        best = (gain, cutoff, k, intact, lowest)

    if best is None:
        print("\nno cell held every answer at 8/10 or better.")
        return 0

    gain, cutoff, k, intact, lowest = best
    print(
        f"\ncheapest cell that keeps every top10 at {lowest}/10 or better (proxy df): "
        f"cutoff {cutoff:.0%}, K={k} -- {gain:.2f}x fewer matched rows, "
        f"{intact}/{n} answers unchanged."
    )

    # A cost model that is never checked against a clock is a hypothesis. Time
    # the same queries under that cell and see whether the row ratio shows up.
    full_ms = tier_ms = 0.0
    for p in prepared:
        kk = max(1, len(p["terms"]) // 2) if k == "len/2" else k
        kept = tier(p["terms"], p["proxy"], cutoff * corpus.rows, kk)
        full_ms += corpus.timed(corpus.expr(p["terms"]))
        tier_ms += corpus.timed(corpus.expr(kept)) if kept != p["terms"] else corpus.timed(corpus.expr(p["terms"]))
    print(
        f"clock over the same queries: {full_ms:.1f} ms -> {tier_ms:.1f} ms = {full_ms / tier_ms:.2f}x "
        f"(per-query fixed cost dilutes the row ratio on a small corpus)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
