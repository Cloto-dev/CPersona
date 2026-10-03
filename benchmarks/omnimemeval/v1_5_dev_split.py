"""Draw, once, the LongMemEval-S questions the evidence-allocation work may tune on.

docs/EVIDENCE_ALLOCATION_DESIGN.md section 6: settings are chosen on 100 questions drawn by a
fixed seed, and the claim is measured once on the other 400. The list is committed
(v1_5_dev_questions.json) so the split does not depend on a library's sampling algorithm; this
script records how it was drawn and checks the committed list against the dataset.

usage: OMNIMEMEVAL_DIR=<checkout> python v1_5_dev_split.py [--write]
"""
import json
import os
import random
import sys
from pathlib import Path

SEED, N = 20261003, 100
OUT = Path(__file__).with_name("v1_5_dev_questions.json")


def main():
    data = json.load(open(Path(os.environ["OMNIMEMEVAL_DIR"]) / "data/longmemeval/longmemeval_s_cleaned.json"))
    assert len(data) == 500, "expected the 500 LongMemEval-S questions"
    picked = sorted(random.Random(SEED).sample(range(len(data)), N))
    dev = [{"index": i, "question_id": data[i]["question_id"], "question_type": data[i]["question_type"]}
           for i in picked]
    doc = {"seed": SEED, "drawn": N, "of": len(data), "dev": dev}
    if "--write" in sys.argv:
        OUT.write_text(json.dumps(doc, indent=1) + "\n")
        print(f"wrote {OUT.name}: {N} of {len(data)}")
        return
    committed = json.loads(OUT.read_text())
    ids = {q["question_id"] for q in data}
    assert all(q["question_id"] in ids and data[q["index"]]["question_id"] == q["question_id"]
               for q in committed["dev"]), "the committed list does not match the dataset"
    print(f"committed list matches the dataset: {len(committed['dev'])} development questions, "
          f"{len(data) - len(committed['dev'])} held out")


if __name__ == "__main__":
    main()
