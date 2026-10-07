#!/usr/bin/env python3
"""Record what the block arm's candidate generation returns, for other implementations.

    uv run python scripts/capture-block-candidates.py            # rewrite the golden
    uv run python scripts/capture-block-candidates.py --check    # diff without writing

The golden file, `tests/golden/block_candidates.json`, holds block rows, a
query, the caps, and the rows recall's candidate generation returns for them.
The answers are observed from the functions recall runs (see
`tests/block_candidates_cases.py`); nobody writes them down. An implementation
that reads the rows another way passes when it returns every case's rows in the
same order (`docs/BLOCK_CANDIDATES_CONTRACT.md`).

Regenerating is legitimate when a case is added or an intended change to the
candidate generation lands. In both cases the diff is the review surface: a
changed `expected` line is a candidate that changed, and every other
implementation has to follow it. `tests/test_block_candidates_golden.py` fails
until the file agrees with the code again, which is the point.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from block_candidates_cases import capture, to_json  # noqa: E402

GOLDEN = REPO / "tests" / "golden" / "block_candidates.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="print the diff against the golden and write nothing")
    args = parser.parse_args()

    text = to_json(asyncio.run(capture()))
    old = GOLDEN.read_text(encoding="utf-8") if GOLDEN.exists() else ""
    if args.check:
        diff = list(difflib.unified_diff(old.splitlines(), text.splitlines(), str(GOLDEN), "observed", lineterm=""))
        print("\n".join(diff) if diff else "golden matches the code")
        return 1 if diff else 0
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(text, encoding="utf-8")
    print(f"wrote {GOLDEN.relative_to(REPO)} ({len(text):,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
