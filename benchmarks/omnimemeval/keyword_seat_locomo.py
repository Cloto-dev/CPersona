"""Does the keyword seat on a reconstruct call without a mode cost LoCoMo evidence? (bug-808)

Search only: no model is called. The store is the one OmniMemEval's LoCoMo ingestion builds with the
cpersona adapter (cpersona_client.py): one record per session, msg_id "<agent>:session_<k>", one agent
per conversation (locomo_exp_user_<i>_speaker_a_<version>).

  python keyword_seat_locomo.py search <label> <code dir> <pub|fresh> <out dir>
      Searches every question of categories 1-4 (1,540) with reconstruct in its default, pro and lite
      modes, in that order, on an APFS clone of the store (recall updates recall counts, so each run
      needs its own), through CPersona's MCP server over stdio with PYTHONPATH=<code dir>. pub copies
      the store's calibration file beside the clone; fresh copies none and turns startup calibration
      off. Writes <out>/<label>-<config>.jsonl (the items' head refs per question and mode).
  python keyword_seat_locomo.py analyze <out dir> <base label> <new label> [seed]
      Per configuration and mode: the share of a question's evidence sessions among its items' head
      records, new - base, with a paired 95% bootstrap interval over questions (10,000 resamples).
      Instrument check: pro and lite must come back identical (the seat is not new there) and the
      default call's heads must include every head the base returned (a seat only adds).

Environment: CPERSONA_LOCOMO_STORE (store.db), CPERSONA_LOCOMO_DATA (locomo10.json),
CPERSONA_EMBEDDING_URL (the endpoint the store was embedded with), CPERSONA_EMBEDDING_MODEL.
The server runs on this interpreter, so run the search from an environment that has cpersona's
dependencies installed.
"""
import asyncio
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

MODES = ("default", "pro", "lite")
CONFIGS = ("pub", "fresh")


def _store() -> Path:
    return Path(os.environ["CPERSONA_LOCOMO_STORE"])


def _questions() -> list[dict]:
    out = []
    data = json.loads(Path(os.environ["CPERSONA_LOCOMO_DATA"]).read_text())
    for conv, sample in enumerate(data):
        for k, q in enumerate(sample["qa"]):
            if q.get("category") in (1, 2, 3, 4):
                out.append({"conv": conv, "k": k, "question": q["question"], "evidence": q.get("evidence") or []})
    return out


def _agents() -> dict:
    c = sqlite3.connect(f"file:{_store()}?mode=ro", uri=True)
    return {int(re.search(r"user_(\d+)_", a).group(1)): a for (a,) in c.execute("select distinct agent_id from memories")}


async def _search(label: str, code: Path, config: str, out: Path) -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    store = _store()
    out.mkdir(parents=True, exist_ok=True)
    db = out / f"{label}-{config}.db"
    assert not db.exists(), f"{db} exists"
    subprocess.run(["cp", "-c", str(store), str(db)], check=True)
    if Path(f"{store}-wal").exists():
        subprocess.run(["cp", "-c", f"{store}-wal", f"{db}-wal"], check=True)
    if config == "pub":
        shutil.copy2(f"{store}.calibration.json", f"{db}.calibration.json")
    env = {"PATH": "/usr/bin:/bin", "HOME": os.environ["HOME"], "TZ": "UTC", "PYTHONPATH": str(code),
           "CPERSONA_DB_PATH": str(db), "CPERSONA_EMBEDDING_MODE": "http",
           "CPERSONA_EMBEDDING_URL": os.environ["CPERSONA_EMBEDDING_URL"],
           "CPERSONA_EMBEDDING_MODEL": os.environ.get("CPERSONA_EMBEDDING_MODEL", "bge-m3"),
           "CPERSONA_VECTOR_SEARCH_MODE": "local", "CPERSONA_STORE_BLOB": "true", "CPERSONA_FTS_ENABLED": "true",
           "CPERSONA_MAX_CONTENT_LENGTH": "80000", "CPERSONA_AUTO_CALIBRATE": "false"}
    if config == "fresh":
        env["CPERSONA_CALIBRATE_ON_MODEL_CHANGE"] = "false"
    agents = _agents()
    params = StdioServerParameters(command=sys.executable, args=["-m", "cpersona.server"], env=env, cwd=str(out))
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            with open(out / f"{label}-{config}.jsonl", "w") as f:
                for mode in MODES:
                    for q in _questions():
                        args = {"agent_id": agents[q["conv"]], "query": q["question"]}
                        if mode != "default":
                            args["mode"] = mode
                        res = json.loads((await s.call_tool("reconstruct", args)).content[0].text)
                        heads = [i["head_ref"] for i in res.get("items") or [] if i.get("head_ref")]
                        f.write(json.dumps({"mode": mode, "conv": q["conv"], "k": q["k"], "heads": heads}) + "\n")


def _analyze(out: Path, base: str, new: str, seed: int) -> dict:
    import numpy as np

    c = sqlite3.connect(f"file:{_store()}?mode=ro", uri=True)
    sess = {}
    for mid, msg_id in c.execute("select id, msg_id from memories"):
        m = re.search(r":session_(\d+)$", msg_id or "")
        sess[f"mem:{mid}"] = int(m.group(1)) if m else None
    ev = {(q["conv"], q["k"]): {int(s) for e in q["evidence"] for s in re.findall(r"D(\d+):", e)} for q in _questions()}
    rows = {}
    for label in (base, new):
        for config in CONFIGS:
            for line in open(out / f"{label}-{config}.jsonl"):
                r = json.loads(line)
                rows[(label, config, r["mode"], r["conv"], r["k"])] = r["heads"]
    keys_all = [k for k in rows if k[0] == base]
    result = {"seed": seed, "instrument": {
        "mode_mismatches": sum(1 for k in keys_all if k[2] != "default" and rows[k] != rows[(new,) + k[1:]]),
        "default_not_superset": sum(1 for k in keys_all if k[2] == "default" and not set(rows[k]) <= set(rows[(new,) + k[1:]]))},
        "comparisons": {}}
    rng = np.random.default_rng(seed)
    for config in CONFIGS:
        for mode in MODES:
            keys = sorted({(k[3], k[4]) for k in keys_all if k[1] == config and k[2] == mode and ev[(k[3], k[4])]})
            def score(label: str, config=config, mode=mode, keys=keys):
                return np.array([len({sess.get(x) for x in rows[(label, config, mode) + key]} & ev[key]) / len(ev[key])
                                 for key in keys])

            a, b = score(base), score(new)
            d = b - a
            lo, hi = np.percentile(d[rng.integers(0, len(d), (10_000, len(d)))].mean(1), [2.5, 97.5])
            result["comparisons"][f"{config}/{mode}"] = {
                "questions": len(keys), base: round(float(a.mean()), 4), new: round(float(b.mean()), 4),
                "diff": round(float(d.mean()), 4), "ci95": [round(float(lo), 4), round(float(hi), 4)],
                "better": int((d > 0).sum()), "worse": int((d < 0).sum())}
    return result


if __name__ == "__main__":
    if sys.argv[1] == "search":
        asyncio.run(_search(sys.argv[2], Path(sys.argv[3]), sys.argv[4], Path(sys.argv[5])))
    elif sys.argv[1] == "analyze":
        print(json.dumps(_analyze(Path(sys.argv[2]), sys.argv[3], sys.argv[4],
                                  int(sys.argv[5]) if len(sys.argv) > 5 else 20261031), indent=2))
    else:
        sys.exit(__doc__)
