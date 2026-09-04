#!/usr/bin/env python3
"""Record the current behaviour of the 2.5.2 refactor targets.

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

What is written back is RECONCILED against the golden first. A later version
may add a key the golden does not hold, or deliberately change a value it does
(`behaviour_252` keeps both lists, and the replay test applies them before it
compares -- which is why the suite is green while this script used to print 138
lines). Writing the raw observation would have quietly overwritten every one of
those recorded values the next time someone regenerated to add a scenario,
retiring the evidence the lists exist to keep. Reconciling first means a
regeneration adds the new scenario and changes nothing else, and the diff this
script prints is the genuine behaviour change it claims to be.

Scenarios the golden does not hold are exempt: a new scenario records what it
observes, including keys that postdate the golden. It has no recorded past to
preserve, and dropping those keys would hand it a hole nobody chose.

Read `tests/behaviour_252.py` for what an observation contains and what the
matrix does and does not cover.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from behaviour_252 import (  # noqa: E402
    SCENARIOS,
    _drop_keys_added_since_golden,
    _without_changed_values,
    close_db,
    observe_all,
    to_json,
)

GOLDEN = REPO / "tests" / "golden" / "behaviour_252.json"


def _reconciled(observed: dict, recorded: dict) -> dict:
    """What to write: the observation, minus the differences the golden is
    allowed to have with the current code.

    Applied per scenario and only where the golden already holds one, so a new
    scenario passes through untouched. The two transforms are the same objects
    the replay test compares with, imported rather than copied: a second copy
    would be a second thing to update, and the failure mode of missing one is
    silent -- the file simply stops holding what it recorded.
    """
    out = {}
    for sid, obs in observed.items():
        past = recorded.get(sid)
        if past is None:
            out[sid] = obs
            continue
        obs = _drop_keys_added_since_golden(obs, past)
        for key in _changed_keys(past, _without_changed_values(past, sid)):
            _restore(obs, past, key)
        out[sid] = obs
    return out


def _changed_keys(full: dict, stripped: dict) -> list[tuple]:
    """The key paths `_without_changed_values` removed for this scenario."""
    paths = []

    def walk(a, b, path):
        if not isinstance(a, dict):
            return
        for k, v in a.items():
            if not isinstance(b, dict) or k not in b:
                paths.append((*path, k))
            else:
                walk(v, b[k], (*path, k))

    walk(full, stripped, ())
    return paths


def _restore(obs: dict, past: dict, path: tuple) -> None:
    """Put the golden's value for one key path back into the observation."""
    src, dst = past, obs
    for key in path[:-1]:
        if not isinstance(src, dict) or not isinstance(dst, dict):
            return
        src, dst = src.get(key), dst.get(key)
    if isinstance(src, dict) and isinstance(dst, dict) and path[-1] in src:
        dst[path[-1]] = src[path[-1]]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="diff against the golden without writing")
    args = ap.parse_args()

    print(f"Observing {len(SCENARIOS)} scenarios against the current implementation...")
    try:
        observed = await observe_all()
    finally:
        await close_db()

    recorded = json.loads(GOLDEN.read_text(encoding="utf-8")) if GOLDEN.exists() else {}
    captured = to_json(_reconciled(observed, recorded))

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
