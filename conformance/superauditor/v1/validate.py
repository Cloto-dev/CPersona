#!/usr/bin/env python3
"""Reference check for the SuperAuditor v1 conformance fixtures.

The standard deliberately ships no shared library, so the fixtures carry the
consistency instead. This script does two jobs:

1. It is the executable statement of the reference algorithm (`deliver`) —
   40 lines, reimplementable in any language.
2. It proves the fixtures are self-consistent, so a fixture authoring
   mistake cannot be mistaken for an implementation bug.

An implementation conforms by feeding each case's `detector_output` through
its own delivery path and comparing against `expect`. Nothing here needs to
be imported to do that.

    python3 validate.py [fixture.json ...]
"""
import json
import os
import sys

FALLBACK_SEVERITY = "info"


def deliver(detector_output, severity_map, per_kind_limit):
    """The reference delivery transform (standard 5.2, 6).

    Keeps the first `per_kind_limit` findings of each kind in detector
    order, names every kind that had more, and derives the counts from what
    is actually returned.
    """
    kept, seen, capped = [], {}, []
    for f in detector_output:
        kind = f["kind"]
        n = seen.get(kind, 0)
        seen[kind] = n + 1
        if n >= per_kind_limit:
            if kind not in capped:
                capped.append(kind)      # observed, not inferred from == limit
            continue
        kept.append({**f, "severity": severity_map.get(kind, FALLBACK_SEVERITY)})

    counts_by_kind, counts_by_severity = {}, {}
    for f in kept:
        counts_by_kind[f["kind"]] = counts_by_kind.get(f["kind"], 0) + 1
        counts_by_severity[f["severity"]] = counts_by_severity.get(f["severity"], 0) + 1
    return {
        "findings": kept,
        "total": len(kept),
        "counts_by_kind": counts_by_kind,
        "counts_by_severity": counts_by_severity,
        "capped_kinds": capped,
    }


def check(path):
    doc = json.load(open(path, encoding="utf-8"))
    failures = 0
    for case in doc["cases"]:
        got = deliver(case["detector_output"], case["severity_map"],
                      case["per_kind_limit"])
        exp = case["expect"]
        bad = [k for k in exp if got.get(k) != exp[k]]
        # the invariant the counts exist to carry, checked independently of
        # the expectations: prose about the returned set must match the set
        if got["total"] != len(got["findings"]):
            bad.append("total<->findings")
        if sum(got["counts_by_kind"].values()) != got["total"]:
            bad.append("counts_by_kind<->total")
        if sum(got["counts_by_severity"].values()) != got["total"]:
            bad.append("counts_by_severity<->total")
        for kind, n in got["counts_by_kind"].items():
            if n > case["per_kind_limit"]:
                bad.append(f"{kind} over per_kind_limit")
        if bad:
            failures += 1
            print(f"FAIL {os.path.basename(path)}::{case['name']}: {', '.join(bad)}")
            for k in bad:
                if k in exp:
                    print(f"     expected {k}={exp[k]!r}\n     got      {k}={got.get(k)!r}")
        else:
            print(f"ok   {os.path.basename(path)}::{case['name']}")
    return failures


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    paths = sys.argv[1:] or sorted(
        os.path.join(here, f) for f in os.listdir(here) if f.endswith(".json"))
    failures = sum(check(p) for p in paths)
    print(f"\n{len(paths)} fixture file(s), {failures} failing case(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
