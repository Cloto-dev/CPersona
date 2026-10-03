"""Run the harness's search step for arm B against the users the store actually holds (latency re-run).

The harness puts its --version into the user id (lme_exper_user_{version}_{i}), so a re-run
under another version name searches users that were never ingested. This calls the same
search wrapper (utils.search_helpers.dispatch_search) and client the harness uses, with the
ingestion's user ids (version "lme1"), two worker threads as the harness ran, and writes the
contexts and durations to its own file; the published results directory is not touched.

usage: OMNIMEMEVAL_DIR=<checkout> OMNIMEMEVAL_ENV_FILE=<envfile> <harness venv python> search_driver.py <out.json> [workers]
       SEARCH_INDICES=<json> searches only the questions it lists: a list of indices, or a file with a
       "dev" list of {"index": i} (v1_5_dev_questions.json)
(run from the checkout: the dataset loader reads a path relative to it)
"""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

H = Path(os.environ["OMNIMEMEVAL_DIR"])  # the OmniMemEval checkout the run used
sys.path.insert(0, str(H / "scripts"))
from utils.env import load_env  # noqa: E402

load_env()
from client_factory import create_client  # noqa: E402
from longmemeval.lme_common import user_id_for  # noqa: E402
from longmemeval.lme_data import load_lme_dataframe  # noqa: E402
from utils.search_helpers import dispatch_search, unpack_search_result  # noqa: E402

OUT = Path(sys.argv[1])
WORKERS = int(sys.argv[2]) if len(sys.argv) > 2 else 2
INDICES = None
if os.environ.get("SEARCH_INDICES"):
    _listed = json.load(open(os.environ["SEARCH_INDICES"]))
    _listed = _listed["dev"] if isinstance(_listed, dict) else _listed
    INDICES = [e["index"] if isinstance(e, dict) else int(e) for e in _listed]
df = load_lme_dataframe()


def one(i):
    row = df.iloc[i]
    client = create_client("cpersona")
    t0 = time.time()
    result = dispatch_search("cpersona", client, row["question"], user_id_for("lme1", i), 20,
                             question_date=row["question_date"])
    context, duration_ms, _, _ = unpack_search_result(result)
    return {"i": i, "started": t0, "search_duration_ms": duration_ms, "search_context": context}


rows = []
with ThreadPoolExecutor(max_workers=WORKERS) as ex:
    for r in ex.map(one, INDICES if INDICES is not None else range(len(df))):
        rows.append(r)
        if len(rows) % 50 == 0:
            print(f"{len(rows)}/{len(df)}", flush=True)
json.dump(rows, open(OUT, "w"), ensure_ascii=False)
print(f"done {len(rows)} -> {OUT}", flush=True)
