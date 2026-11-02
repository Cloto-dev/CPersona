"""Attribute every wrong answer of the 2.6.11 Pro LongMemEval-S run to a failure code, twice (2026-11-02).

The same failures coded by the confirmation tool before (3cd1713) and after (e33e0e3) it learned
CAPTURE_MISS, REPRESENTATION_MISS, SYSTEM_DEGRADED and EVIDENCE_NOISE. The reader's answers and
the judge's verdicts are the published run's; nothing here calls a model. Results:
results-failure-attribution-2611.md.

Runs inside an OmniMemEval checkout (OMNIMEMEVAL_HOME, default: two levels above this file) whose
run directory (RUN_DIR, default lme/pr-lme-cp2611) holds the published run's store and its
scored results under pro-scored/.

Stages (run in order):
  gold   <store clone> <out gold.json>            read-only: each question's answer sessions -> stored refs,
                                                  their stored text and searchable reach
  search <out traces.jsonl>                       the run's Pro search again with trace=true, against a
                                                  v2.6.11 server on the clone at 127.0.0.1:8502
  codes  <gold.json> <traces.jsonl> <old tool> <new tool> <out.json>
                                                  both tools on every wrong answer, plus the replay check

usage: OMNIMEMEVAL_ENV_FILE=cpersona.envfile <harness venv python> g2_attribution.py <stage> ...
"""
import importlib.util
import json
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

H = Path(os.environ.get("OMNIMEMEVAL_HOME", Path(__file__).resolve().parent.parent))
RUN = H / os.environ.get("RUN_DIR", "lme/pr-lme-cp2611")
SCORED = RUN / "pro-scored"
VERSION = "lme2611"


def _harness():
    sys.path.insert(0, str(H / "scripts"))
    from longmemeval.lme_common import user_id_for
    from longmemeval.lme_data import load_lme_dataframe
    return user_id_for, load_lme_dataframe


def _judged():
    return json.load(open(SCORED / "cpersona_lme_judged.json"))


def _wrong(judged):
    return {u: r for u, r in judged.items() if not all(r["llm_judgments"].values())}


def _searched():
    """The run's search records, one per user (the harness keeps each as a one-element list)."""
    raw = json.load(open(SCORED / "cpersona_lme_search_results.json"))
    out = {}
    for user, rec in raw.items():
        if isinstance(rec, list):
            assert len(rec) == 1, (user, len(rec))
            rec = rec[0]
        out[user] = rec
    return out


def _quotes(evidences):
    """The harness writes an evidence turn as 'role : text'; the store holds 'role: text'. Keep the text."""
    out = []
    for e in evidences or []:
        out.append(e.split(" : ", 1)[1] if " : " in e else e)
    return [q for q in out if q.strip()]


def stage_gold(store, out):
    user_id_for, load_lme_dataframe = _harness()
    df = load_lme_dataframe(verbose=False)
    db = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
    gold, ambiguous, unmatched = {}, 0, 0
    for i, row in df.iterrows():
        user = user_id_for(VERSION, i)
        ids = list(row["haystack_session_ids"])
        refs, texts, reach = [], {}, {}
        for sid in row["answer_session_ids"]:
            if sid not in ids:
                unmatched += 1
                continue
            session = row["haystack_sessions"][ids.index(sid)]
            turns = [m for m in session if str(m.get("content", "")).strip()]
            if not turns:
                unmatched += 1
                continue
            probe = max(turns, key=lambda m: len(str(m["content"])))
            needle = f"{probe['role']}: {str(probe['content']).strip()}"
            hits = db.execute(
                "SELECT id, content FROM memories WHERE agent_id = ? AND instr(content, ?) > 0", (user, needle)
            ).fetchall()
            if len(hits) != 1:
                ambiguous += len(hits) > 1
                unmatched += not hits
                continue
            mid, content = hits[0]
            ref = f"mem:{mid}"
            refs.append(ref)
            texts[ref] = content
            # Semantic reach: the record's own vector covers its first node (or all of it when it has no
            # nodes); the block arm covers every block that carries bits. The lexical arm reads all text.
            node0 = db.execute(
                "SELECT end_char FROM record_nodes WHERE parent_kind = 'memory' AND parent_id = ? AND node_index = 0",
                (mid,),
            ).fetchone()
            blocks = db.execute(
                "SELECT max(end_char) FROM record_blocks WHERE parent_kind = 'memory' AND parent_id = ? "
                "AND embedding_bits IS NOT NULL",
                (mid,),
            ).fetchone()[0]
            reach[ref] = max(node0[0] if node0 else len(content), blocks or 0)
        gold[user] = {"refs": refs, "stored_texts": texts, "searchable_chars": reach,
                      "expected": len(row["answer_session_ids"])}
    json.dump({"gold": gold, "ambiguous": ambiguous, "unmatched": unmatched}, open(out, "w"))
    print(f"questions {len(gold)}, answer sessions matched {sum(len(g['refs']) for g in gold.values())}, "
          f"ambiguous {ambiguous}, unmatched {unmatched}")


def stage_search(out):
    _harness()
    from utils.env import load_env
    load_env()
    from client_factory import create_client
    client = create_client("cpersona")
    searched = _searched()
    wrong = _wrong(_judged())
    done = set()
    if Path(out).exists():
        done = {json.loads(line)["user"] for line in open(out)}
    with open(out, "a") as f:
        for user, rec in wrong.items():
            if user in done:
                continue
            args = client.search_args(searched[user]["question"], user)
            assert args.get("mode") == "pro", "run with CPERSONA_SEARCH_MODE=pro, as the published search did"
            resp = client.call("reconstruct", {**args, "trace": True})
            trace = resp.pop("trace", {})
            shown_by_ref = {}
            for item in resp.get("items", []):
                if item.get("head_ref"):
                    shown_by_ref[item["head_ref"]] = shown_by_ref.get(item["head_ref"], "") + (item.get("content") or "")
                for x in item.get("excerpts", []):
                    if isinstance(x, dict) and x.get("ref"):
                        shown_by_ref[x["ref"]] = shown_by_ref.get(x["ref"], "") + "\n" + (x.get("content") or "")
            blocks = client.render(resp)
            f.write(json.dumps({
                "user": user, "trace": trace.get("recall", {}), "shown_by_ref": shown_by_ref,
                "rendered": "\n".join(blocks), "advisory": resp.get("advisory"),
            }) + "\n")
            f.flush()
    print(f"searched {len(wrong)} wrong answers")


def _load_tool(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.confirm


def stage_codes(gold_path, traces_path, old_path, new_path, out):
    gold = json.load(open(gold_path))["gold"]
    searched = _searched()
    judged = _judged()
    wrong = _wrong(judged)
    old, new = _load_tool(old_path, "confirm_old"), _load_tool(new_path, "confirm_new")
    rows, replay = [], Counter()
    for line in open(traces_path):
        t = json.loads(line)
        user = t["user"]
        g, s = gold[user], searched[user]
        quotes = _quotes(s.get("answer_evidences"))
        same = t["rendered"].strip() and t["rendered"].strip() in s["search_context"]
        replay["identical" if same else "differs"] += 1
        before = old(t["trace"], set(g["refs"]), shown_text=s["search_context"], quotes=quotes, correct=False)
        after = new(
            t["trace"], set(g["refs"]), shown_text=s["search_context"], quotes=quotes, correct=False,
            stored_texts=g["stored_texts"], searchable_chars=g["searchable_chars"],
            shown_by_ref=t["shown_by_ref"] if same else None,
            degraded=bool(t.get("advisory")),
        )
        rows.append({"user": user, "category": judged[user]["category"], "before": before, "after": after,
                     "gold_matched": len(g["refs"]), "gold_expected": g["expected"], "replay_identical": bool(same),
                     "quotes": len(quotes)})
    n = len(rows)
    summary = {
        "wrong_answers": len(wrong), "coded": n, "replay": dict(replay),
        "before": dict(Counter(r["before"] for r in rows)), "after": dict(Counter(r["after"] for r in rows)),
        "unattributed_rate_before": sum(r["before"] == "UNATTRIBUTED" for r in rows) / n if n else None,
        "unattributed_rate_after": sum(r["after"] == "UNATTRIBUTED" for r in rows) / n if n else None,
        "transitions": dict(Counter(f"{r['before']} -> {r['after']}" for r in rows if r["before"] != r["after"])),
    }
    json.dump({"summary": summary, "rows": rows}, open(out, "w"), indent=1)
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    stage, *rest = sys.argv[1:]
    {"gold": stage_gold, "search": stage_search, "codes": stage_codes}[stage](*rest)
