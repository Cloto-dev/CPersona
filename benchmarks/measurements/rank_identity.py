"""Per-query returned-id identity — the instrument for "did this change move recall?"

A change to the recall read path is supposed to leave a healthy corpus alone.
That claim is about *which rows come back in which order*, and the record that
answers it did not exist: `lmeb_results` keeps summary scores (`main_score`), and
a mean can hold still while the rows underneath it are shuffled.

This module keeps the ordered ids instead, and compares two runs of them.

The measurement it reads is `scan_window_ab.py`'s `arm` output, at the shipped
regime, over the twelve rotations of that harness's scene-blocked LongMemEval
layout (seed 20260903, 237,654 documents, `limit=10`). That harness was built
for the window A/B; nothing here changes its instrument. What is used is the
part it already records per query — `results[<query id>]["ids"]` — which is the
ordered list a caller would have received.

Why ids and not scores: a float comparison fails for reasons that are not
regressions (a different summation order over the same vectors moves the last
bits, and the ranking is unchanged). Ids are what a caller acts on, and two runs
that return the same rows in the same order are the same behaviour whatever the
arithmetic did on the way.

Usage:

    # freeze the current build
    python benchmarks/measurements/rank_identity.py freeze \\
        --arms /tmp/rank-baseline --out benchmarks/measurements/rank-baseline-<label>.json

    # after a change, re-run the arms and compare against the frozen file
    python benchmarks/measurements/rank_identity.py compare \\
        --baseline benchmarks/measurements/rank-baseline-<label>.json \\
        --arms /tmp/rank-after

`compare` exits non-zero when any query's ordered ids differ, so it can be used
as a gate. It reports three levels, because they mean different things:

* **order differs, set identical** — the same rows came back in another order.
  A tie broken differently is enough to do this, so it is reported apart from a
  membership change rather than lumped in with it.
* **set differs** — a row entered or left the result. This is the one that
  changes what a caller sees.
* **absent / new queries** — the two runs are not over the same layout, which
  makes every other number meaningless. It is reported first and fails hard.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _load_arms(arms_dir: Path) -> dict:
    """Read every arm file in a directory into {rotation: {qid: [ids]}}.

    The regime of each arm is kept and checked for agreement: a baseline taken
    at one scan window and a comparison taken at another would differ for a
    reason that has nothing to do with the change under test.
    """
    rotations: dict[str, dict[str, list[str]]] = {}
    regimes: list[tuple[str, dict]] = []
    files = sorted(arms_dir.glob("arm-r*.json"))
    if not files:
        raise SystemExit(f"no arm files (arm-r*.json) in {arms_dir}")
    for path in files:
        arm = json.loads(path.read_text(encoding="utf-8"))
        rotation = str(arm["rotation"])
        if rotation in rotations:
            raise SystemExit(
                f"rotation {rotation} appears twice in {arms_dir} "
                f"(second file: {path.name})"
            )
        rotations[rotation] = {
            qid: row["ids"] for qid, row in sorted(arm["results"].items())
        }
        regimes.append((path.name, arm["regime"]))
    first_name, first_regime = regimes[0]
    for name, regime in regimes[1:]:
        if regime != first_regime:
            raise SystemExit(
                f"regime mismatch: {name} differs from {first_name}. "
                "Every arm of one baseline must be the same regime."
            )
    return {"regime": first_regime, "rotations": rotations}


def cmd_freeze(args: argparse.Namespace) -> int:
    loaded = _load_arms(Path(args.arms))
    n_queries = sum(len(q) for q in loaded["rotations"].values())
    out = {
        "label": args.label,
        "commit": args.commit,
        "note": args.note,
        "regime": loaded["regime"],
        "rotations": loaded["rotations"],
        "query_count": n_queries,
    }
    Path(args.out).write_text(
        json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        f"froze {n_queries} queries over {len(loaded['rotations'])} rotations "
        f"-> {args.out}"
    )
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    current = _load_arms(Path(args.arms))

    if baseline["regime"] != current["regime"]:
        print("REGIME MISMATCH — the two runs are not comparable.")
        print(f"  baseline: {baseline['regime']}")
        print(f"  current:  {current['regime']}")
        return 2

    base_rot, cur_rot = baseline["rotations"], current["rotations"]
    missing = sorted(set(base_rot) - set(cur_rot), key=int)
    extra = sorted(set(cur_rot) - set(base_rot), key=int)
    if missing or extra:
        print("ROTATION SET MISMATCH — every other number below would be over "
              "a different layout.")
        print(f"  in baseline only: {missing}")
        print(f"  in current only:  {extra}")
        return 2

    compared = identical = reordered = 0
    set_changed: list[tuple[str, str, list[str], list[str]]] = []
    query_mismatch: list[str] = []

    for rotation in sorted(base_rot, key=int):
        b, c = base_rot[rotation], cur_rot[rotation]
        if set(b) != set(c):
            query_mismatch.append(rotation)
            continue
        for qid in sorted(b):
            compared += 1
            if b[qid] == c[qid]:
                identical += 1
            elif set(b[qid]) == set(c[qid]):
                reordered += 1
            else:
                set_changed.append((rotation, qid, b[qid], c[qid]))

    if query_mismatch:
        print("QUERY SET MISMATCH in rotations: " + ", ".join(query_mismatch))
        return 2

    changed = reordered + len(set_changed)
    print(f"queries compared:      {compared}")
    print(f"identical id sequence: {identical}  ({identical / compared:.1%})")
    print(f"order differs, set same: {reordered}")
    print(f"set differs:             {len(set_changed)}")

    for rotation, qid, before, after in set_changed[: args.show]:
        gone = [i for i in before if i not in after]
        came = [i for i in after if i not in before]
        print(f"  r{rotation} {qid}: -{gone} +{came}")
    if len(set_changed) > args.show:
        print(f"  ... and {len(set_changed) - args.show} more")

    if changed:
        print(f"\nFAIL: {changed} of {compared} queries changed.")
        return 1
    print("\nPASS: every query returned the same rows in the same order.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("freeze", help="consolidate a directory of arm files")
    f.add_argument("--arms", required=True, help="directory holding arm-r*.json")
    f.add_argument("--out", required=True)
    f.add_argument("--label", default="baseline")
    f.add_argument("--commit", default="", help="the build the arms were run on")
    f.add_argument("--note", default="")
    f.set_defaults(func=cmd_freeze)

    c = sub.add_parser("compare", help="compare a new run against a frozen file")
    c.add_argument("--baseline", required=True)
    c.add_argument("--arms", required=True, help="directory holding arm-r*.json")
    c.add_argument("--show", type=int, default=10,
                   help="how many changed queries to print")
    c.set_defaults(func=cmd_compare)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
