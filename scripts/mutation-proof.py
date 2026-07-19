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
    # M04 and M06 were both filed as equivalent mutants. Both classifications
    # turned out to be wrong, and they were wrong in the same way: each was
    # reasoned about on the real-import path, where a second layer does hold the
    # invariant up, without asking whether that second layer exists on the
    # dry_run path. It does not. A preview has no INSERT, so a guard the real
    # path can afford to lose is often the only one a preview has.
    Mutation(
        id="M04",
        target="do_import_memories msg_id pre-check — load-bearing on the dry_run path",
        file="cpersona/admin_handlers.py",
        find="if existing or (tally.dry_run and (aid, pid, msg_id) in tally.seen_msgid):",
        replace="if False:",
        # RECLASSIFIED (CSC Task #287). Filed as equivalent because "the row
        # falls through to INSERT OR IGNORE and the v12 UNIQUE index turns it
        # into the same counted skip". True of a real import. On a dry_run there
        # IS no INSERT OR IGNORE, so this pre-check is the entire msg_id dedup
        # gate, and removing it makes the preview report an import the real run
        # would skip — 3 imported / 1 skipped where the truth is 2 / 2.
        #
        # It survived for a year of runs because no dry_run scenario contained a
        # within-file msg_id duplicate. import-dry-run-intra-file-duplicates does.
        breaks="a preview counts a within-file msg_id duplicate as imported; the previewed counts stop matching a real run (bug-070)",
        expect="test_equivalence_252.py[import-dry-run-intra-file-duplicates]",
    ),
    Mutation(
        id="M05",
        target="do_import_memories header validation",
        file="cpersona/admin_handlers.py",
        # _validate_file_header guards with an early return, so disabling the
        # check means always taking it — the inverse of the pre-#287 `if False:`.
        find="""    if tally.file_header is None:
        return""",
        replace="""    if True:
        return""",
        breaks="a truncated export restores partially and reports ok:true (bug-091/110)",
        expect="test_import_rejects_truncated_file, test_import_rejects_file_cut_at_profile_boundary",
    ),
    Mutation(
        id="M06",
        target="do_import_memories dry_run guard — the REMOTE half of the promise",
        file="cpersona/admin_handlers.py",
        # `if not dry_run:` appears six times; anchor on the memory-record body
        # that follows it so the match is unambiguous.
        find="""    if not tally.dry_run:
        source = json.dumps(record.get("source", {}))""",
        replace="""    if True:
        source = json.dumps(record.get("source", {}))""",
        # This entry has been classified three times, and the history is the
        # useful part — it is a record of an invariant gaining a layer.
        #
        # (1) EQUIVALENT, pre-#287. Reasoning: dry_run runs on the read seam, so
        #     an INSERT that escapes this guard is never committed. True of the
        #     database, and the database was all anyone was watching.
        # (2) BEHAVIOURAL, CSC Task #293. dry_run had two write targets and only
        #     one was doubly defended:
        #         database       read seam (M10) + this guard   -> rolled back
        #         remote index   this guard, alone              -> nothing
        #     `remote_items` was populated inside this guard and shipped after
        #     the transaction closed, where no rollback reaches. Removing the
        #     guard left the database spotless and published the previewed rows
        #     to the live index — invisible to every DB assertion in the suite,
        #     and found only because the behavioural snapshot records outbound
        #     traffic as well as rows.
        # (3) EQUIVALENT again, CSC Task #287 — but for a different reason than
        #     (1), and this is the point. The remote queue now goes through
        #     _ImportTally.queue_remote, which a preview cannot make write, so
        #     the second target has two layers too. The counts also survive: the
        #     escaped INSERT runs on the shared read connection, which sees its
        #     own uncommitted rows, so INSERT OR IGNORE reproduces exactly the
        #     skips that seen_msgid / seen_content were emulating.
        #
        # Keep it. A future edit that moves the queue back outside queue_remote
        # flips this to CAUGHT, and that is exactly the alarm we want.
        breaks="nothing observable: the read seam holds the database and queue_remote holds the index (see the history above)",
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
                """    if not tally.dry_run:
        source = json.dumps(record.get("source", {}))""",
                """    if True:
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
                """        if not tally.dry_run:
            cur = await db.execute(
                "INSERT OR IGNORE INTO memories\"""",
                """        if True:
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
        find="""            "INSERT OR IGNORE INTO memories"
            " (agent_id, project_id, channel, msg_id, content, source, timestamp, metadata,\"""",
        replace="""            "INSERT OR REPLACE INTO memories"
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

    # A crash mid-run must never leave a mutant behind. Scope the check to the
    # files this run actually wrote: a whole-tree `git diff --quiet` also trips
    # on unrelated work in progress, and reporting "FILES LEFT MODIFIED" for a
    # file no mutation touched trains the reader to ignore the one warning here
    # that must never be ignored.
    touched = sorted({m.file for m in selected})
    if run(["git", "diff", "--quiet", "--", *touched]).returncode != 0:
        print(f"!! MUTANT LEFT ON DISK in {touched} — restore before committing")
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
