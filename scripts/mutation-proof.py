#!/usr/bin/env python3
"""Targeted mutation proof for the 2.5.2 refactor seams (CSC Task #285).

The 2.5.2 alpha stage splits five large functions apart. Every one of them is
covered by tests that pass today — but a test passing is not evidence that it
would *fail* if the code broke. Before moving code we want that evidence, and
we want it precisely where the seams are: a suite that is green both before and
after a refactor tells us nothing if it was green against broken code too.

So this is not a general mutation-testing run. Each entry below is a specific,
hand-authored claim of the form "if this behaviour silently regressed, the
suite must go red". The harness applies one mutation, runs the suite, restores
the file, and reports:

    CAUGHT     the suite failed  -> the behaviour is genuinely pinned
    SURVIVED   the suite passed  -> a test gap; fix it BEFORE refactoring here

A SURVIVED line is the whole point of the exercise. It names a place where the
refactor would have been unguarded, which is exactly what we could not see by
reading a green test report.

Usage:
    uv run python scripts/mutation-proof.py            # all mutations
    uv run python scripts/mutation-proof.py --id M01   # one, for iterating

Safety: the working tree must be clean for the target files. Each mutation is
applied by exact string replacement and reverted in a finally block; the run
ends by asserting `git diff --quiet` so a crash can never leave a mutant on
disk. Mutants are never committed.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


@dataclass
class Mutation:
    id: str
    target: str  # which refactor seam this protects
    file: str
    find: str
    replace: str
    breaks: str  # the behaviour destroyed, in one line
    expect: str  # the test we believe pins it (informational; not asserted)
    # Extra (find, replace) pairs applied together with the primary one. Needed
    # when an invariant is held up by two independently-sufficient layers: each
    # alone is an equivalent mutant, and only removing both reveals whether a
    # test actually watches the outcome rather than one of the mechanisms.
    also: tuple[tuple[str, str], ...] = ()
    # An equivalent mutant removes one layer of a redundant defence, so the
    # observable behaviour does not change and no test can catch it. Surviving
    # is the CORRECT outcome; being caught would mean the redundancy is gone.
    # They are kept because "this guard is not the one holding the invariant up"
    # is exactly the kind of thing a refactor needs to know.
    equivalent: bool = False


# ---------------------------------------------------------------------------
# _search_vector (vector.py) — remote/local split, CSC Task #286.
# The remote branch is the extraction target, so its contract with the
# embedding service and its fall-through to local are what must stay pinned.
# ---------------------------------------------------------------------------

MUTATIONS: list[Mutation] = [
    Mutation(
        id="M01",
        target="_search_vector remote payload",
        file="cpersona/vector.py",
        find='"min_similarity": effective_min_sim,',
        replace='"min_similarity": 0.0,',
        breaks="remote /search ignores the caller's threshold and over-returns (bug-027)",
        expect="test_remote_search_honors_min_similarity_argument",
    ),
    Mutation(
        id="M02",
        target="_search_vector remote timeout",
        file="cpersona/vector.py",
        find="timeout=REMOTE_SEARCH_TIMEOUT_SECS,",
        replace="",
        breaks="recall hot path inherits the 30s client default; a flapping endpoint stalls recall (bug-033)",
        expect="test_remote_search_honors_min_similarity_argument (second assert)",
    ),
    Mutation(
        id="M03",
        target="_search_vector remote isolation",
        file="cpersona/vector.py",
        find="iso_fetch = isolation_where(agent_id=agent_id, project_id=project_id, channel=channel)",
        replace="iso_fetch = isolation_where(agent_id=agent_id, project_id=None, channel='')",
        breaks="remote by-id fetch loses the γ axes; another project's row can surface (bug-046/075/100)",
        expect="tests/test_isolation.py",
    ),
    # ---------------------------------------------------------------------
    # do_import_memories (admin_handlers.py) — the highest-value split target
    # and the one the soak never exercises. CSC Task #287.
    # ---------------------------------------------------------------------
    # M04 and M06 are kept as documented EQUIVALENT mutants. Both survive, and
    # both should: each removes one layer of a two-layer defence, leaving
    # observable behaviour identical. Deleting them would lose the finding; the
    # load-bearing layer each one sits above is mutated separately (M10/M11).
    Mutation(
        id="M04",
        target="do_import_memories msg_id pre-check (EQUIVALENT — expected to survive)",
        file="cpersona/admin_handlers.py",
        find="if existing or (dry_run and (aid, pid, msg_id) in seen_msgid):",
        replace="if False:",
        breaks="nothing observable: the row falls through to INSERT OR IGNORE and the "
        "v12 UNIQUE index turns it into the same counted skip. The pre-check is a "
        "fast path, not the correctness guarantee — that is M11.",
        expect="(none — equivalent mutant)",
        equivalent=True,
    ),
    Mutation(
        id="M05",
        target="do_import_memories header validation",
        file="cpersona/admin_handlers.py",
        find="if file_header is not None:",
        replace="if False:",
        breaks="a truncated export restores partially and reports ok:true (bug-091/110)",
        expect="test_import_rejects_truncated_file, test_import_rejects_file_cut_at_profile_boundary",
    ),
    Mutation(
        id="M06",
        target="do_import_memories dry_run guard (EQUIVALENT — expected to survive)",
        file="cpersona/admin_handlers.py",
        # `if not dry_run:` appears six times; anchor on the memory-record body
        # that follows it so the match is unambiguous.
        find="""                    if not dry_run:
                        source = json.dumps(record.get("source", {}))""",
        replace="""                    if True:
                        source = json.dumps(record.get("source", {}))""",
        breaks="nothing observable: under dry_run the whole import runs on the read "
        "seam (`connection() if dry_run else transaction()`, admin_handlers.py:1557), "
        "so an INSERT that escapes the guard is never committed. The seam choice is "
        "the real invariant — that is M10.",
        expect="(none — equivalent mutant)",
        equivalent=True,
    ),
    # The load-bearing layers the two equivalent mutants sit above.
    Mutation(
        id="M10",
        target="do_import_memories dry_run read seam",
        file="cpersona/admin_handlers.py",
        # Both import and merge use this idiom; anchor on the import one via the
        # line-enumeration loop that follows it.
        find="""        async with (connection() if dry_run else transaction()) as db:
            for line_num, line in enumerate(lines, 1):""",
        replace="""        async with transaction() as db:
            for line_num, line in enumerate(lines, 1):""",
        # dry_run write-freedom has two independently-sufficient layers, so each
        # alone is equivalent. Remove BOTH: this is the real failure mode — a
        # preview that silently writes — and a test must watch the database to
        # see it. M06 alone and M10's seam edit alone both survive.
        also=(
            (
                """                    if not dry_run:
                        source = json.dumps(record.get("source", {}))""",
                """                    if True:
                        source = json.dumps(record.get("source", {}))""",
            ),
        ),
        breaks="dry_run both runs on the WRITE seam AND executes its INSERTs — the preview commits real rows",
        expect="test_import_dry_run_writes_nothing_to_the_database",
    ),
    Mutation(
        id="M12",
        target="do_merge_memories dry_run read seam",
        file="cpersona/admin_handlers.py",
        find="""    # exit and auto-rolls-back on fault. dry_run does no writes → read seam.
    try:
        async with (connection() if dry_run else transaction()) as db:""",
        replace="""    # exit and auto-rolls-back on fault. dry_run does no writes → read seam.
    try:
        async with transaction() as db:""",
        also=(
            (
                """                if not dry_run:
                    cur = await db.execute(
                        "INSERT OR IGNORE INTO memories\"""",
                """                if True:
                    cur = await db.execute(
                        "INSERT OR IGNORE INTO memories\"""",
            ),
        ),
        breaks="merge preview both runs on the WRITE seam AND executes its INSERTs — the preview commits copied rows",
        expect="test_merge_dry_run_writes_nothing_to_the_database",
    ),
    Mutation(
        id="M11",
        target="do_import_memories collision semantics",
        file="cpersona/admin_handlers.py",
        # Two INSERT OR IGNORE sites (import at :1617, merge at :1902); anchor on
        # the import one via its distinct column list.
        find="""                            "INSERT OR IGNORE INTO memories"
                            " (agent_id, project_id, channel, msg_id, content, source, timestamp, metadata,\"""",
        replace="""                            "INSERT OR REPLACE INTO memories"
                            " (agent_id, project_id, channel, msg_id, content, source, timestamp, metadata,\"""",
        breaks="a re-import overwrites existing rows instead of skipping — silent data loss on restore",
        expect="test_import_skips_rows_whose_msg_id_already_exists",
    ),
    # ---------------------------------------------------------------------
    # do_merge_memories (admin_handlers.py) — CSC Task #287.
    # ---------------------------------------------------------------------
    Mutation(
        id="M07",
        target="do_merge_memories move semantics",
        file="cpersona/admin_handlers.py",
        find='if mode == "move" and not dry_run:',
        replace="if False:",
        breaks="move leaves the source agent's rows behind; merge is no longer atomic",
        expect="test_merge_move_is_one_atomic_unit, test_merge_move_deletes_source_in_same_call",
    ),
    # ---------------------------------------------------------------------
    # do_calibrate_threshold (admin_handlers.py) — CSC Task #287.
    # ---------------------------------------------------------------------
    Mutation(
        id="M08",
        target="do_calibrate_threshold sample floor",
        file="cpersona/admin_handlers.py",
        find="if len(vecs) < 10:",
        replace="if len(vecs) < 0:",
        breaks="calibrates a threshold from a handful of vectors; the null distribution is noise",
        expect="test_calibrate_threshold_insufficient_embeddings",
    ),
    Mutation(
        id="M09",
        target="do_calibrate_threshold dim filter",
        file="cpersona/admin_handlers.py",
        find="vecs = [v for v in vecs if v.shape[0] == target_dim]",
        replace="vecs = list(vecs)",
        breaks="ragged embedding dims reach the matmul; calibration crashes or scores garbage",
        expect="test_calibrate_survives_mixed_embedding_dims",
    ),
]


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, **kw)


def tree_is_clean(files: set[str]) -> bool:
    out = run(["git", "diff", "--name-only"]).stdout.split()
    dirty = files & set(out)
    if dirty:
        print(f"!! working tree has uncommitted changes in target files: {sorted(dirty)}")
        return False
    return True


def apply_mutation(m: Mutation) -> str:
    """Write the mutant, returning the original text for restoration."""
    path = REPO / m.file
    original = path.read_text()
    text = original
    for find, replace in ((m.find, m.replace), *m.also):
        count = text.count(find)
        if count == 0:
            raise SystemExit(
                f"{m.id}: anchor not found in {m.file} — the code moved, update the mutation:\n  {find}"
            )
        if count > 1:
            raise SystemExit(
                f"{m.id}: anchor is ambiguous ({count} matches) in {m.file} — make it unique:\n  {find}"
            )
        text = text.replace(find, replace)
    path.write_text(text)
    return original


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", help="run a single mutation by id")
    args = ap.parse_args()

    selected = [m for m in MUTATIONS if not args.id or m.id == args.id]
    if not selected:
        raise SystemExit(f"no mutation matches --id {args.id}")

    if not tree_is_clean({m.file for m in selected}):
        return 2

    print(f"Baseline: running the suite unmutated ({len(selected)} mutations queued)...")
    base = run(["uv", "run", "pytest", "-q", "-x"])
    if base.returncode != 0:
        print("!! the suite is already failing — fix that first; mutation results would be meaningless")
        print(base.stdout[-2000:])
        return 2
    print("Baseline green.\n")

    survived: list[Mutation] = []
    for m in selected:
        original = apply_mutation(m)
        try:
            # -x: the first failure is enough to prove the mutant is caught.
            result = run(["uv", "run", "pytest", "-q", "-x"])
        finally:
            (REPO / m.file).write_text(original)

        caught = result.returncode != 0
        if m.equivalent:
            # Inverted expectation: an equivalent mutant that gets CAUGHT means a
            # test is asserting the redundant layer itself, which will break the
            # moment someone legitimately simplifies it.
            status = "OVER-PINNED" if caught else "EQUIVALENT "
        else:
            status = "CAUGHT     " if caught else "SURVIVED   "
        print(f"[{status}] {m.id}  {m.target}")
        print(f"           {m.breaks}")
        if not caught and not m.equivalent:
            survived.append(m)
            print(f"           !! no test failed. Expected pin: {m.expect}")
        if caught and m.equivalent:
            survived.append(m)
            print("           !! a test pins a redundant layer — it will fail on a valid simplification")
        print()

    # A crash mid-run must never leave a mutant behind.
    if run(["git", "diff", "--quiet"]).returncode != 0:
        print("!! FILES LEFT MODIFIED — restore manually before committing")
        return 2

    print("=" * 70)
    real = [m for m in selected if not m.equivalent]
    equiv = [m for m in selected if m.equivalent]
    print(f"{len(real) - len([m for m in survived if not m.equivalent])}/{len(real)} behavioural mutations caught")
    print(f"{len(equiv)} equivalent mutants (expected to survive; they document redundant defences)")
    if survived:
        print("\nUnresolved — address these BEFORE refactoring the named seam:")
        for m in survived:
            print(f"  {m.id}  {m.target}\n       {m.breaks}")
        return 1
    print("\nEvery seam is pinned. The refactor has a real safety net.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
