#!/usr/bin/env python3
"""Record the current behaviour of the 2.5.2 refactor targets (CSC Task #293).

    uv run python scripts/capture-behaviour.py            # rewrite the golden
    uv run python scripts/capture-behaviour.py --check    # diff without writing

The golden file is the pre-refactor implementation's observed behaviour, and
`tests/test_equivalence_252.py` asserts the post-refactor code reproduces it.
That makes WHEN this is run the whole point:

    BEFORE a split      capture, commit the golden, then move code
    AFTER a split       do NOT capture. A regenerated golden agrees with
                        whatever the code now does, which is the one thing the
                        artifact exists to disprove.

Regenerating is legitimate when a scenario is added or an intended behaviour
change lands. In both cases the diff is the review surface: every changed line
is a behaviour that changed, and it must be explainable before it is committed.
`--check` prints that diff without touching the file.

Read `tests/behaviour_252.py` for what an observation contains and what the
matrix does and does not cover.
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

from behaviour_252 import SCENARIOS, close_db, observe_all, to_json  # noqa: E402

GOLDEN = REPO / "tests" / "golden" / "behaviour_252.json"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="diff against the golden without writing")
    args = ap.parse_args()

    print(f"Observing {len(SCENARIOS)} scenarios against the current implementation...")
    try:
        captured = to_json(await observe_all())
    finally:
        await close_db()

    if not GOLDEN.exists():
        if args.check:
            print(f"!! no golden at {GOLDEN.relative_to(REPO)} — run without --check to create it")
            return 1
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(captured, encoding="utf-8")
        print(f"Created {GOLDEN.relative_to(REPO)} ({len(SCENARIOS)} scenarios).")
        return 0

    existing = GOLDEN.read_text(encoding="utf-8")
    if existing == captured:
        print("No change: the current behaviour matches the golden.")
        return 0

    diff = list(
        difflib.unified_diff(
            existing.splitlines(keepends=True),
            captured.splitlines(keepends=True),
            fromfile="golden (recorded)",
            tofile="current (observed)",
        )
    )
    sys.stdout.writelines(diff)
    print(f"\n{sum(1 for line in diff if line.startswith(('+', '-')) and not line.startswith(('+++', '---')))} changed lines.")

    if args.check:
        print("Behaviour differs from the golden. Every line above must be explainable.")
        return 1

    GOLDEN.write_text(captured, encoding="utf-8")
    print(f"\nRewrote {GOLDEN.relative_to(REPO)}. Review the diff above before committing it.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
