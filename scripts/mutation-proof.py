#!/usr/bin/env python3
"""Targeted mutation proof for the 2.5.2 refactor seams.

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

Safety: the working tree must be clean for the target files, staged edits
included — a mutant applied over a staged change describes a tree nobody is
shipping, and that run still goes green. Each mutation is
applied by exact string replacement and reverted in a finally block; the run
ends by asserting `git diff --quiet` so a crash can never leave a mutant on
disk. Mutants are never committed. Every write — mutant and restore — also
removes the file's cached bytecode (see `forget_bytecode`).
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
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
    # Test files that catch this mutant, run BEFORE the full suite. Only a
    # shortcut: a red subset is a red suite, so it can shorten a CAUGHT verdict
    # but never produce one the full suite would not. A green subset (a stale
    # list) falls through to the full suite, which stays the authority.
    tests: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# _search_vector (vector.py) — remote/local split.
# The remote branch is the extraction target, so its contract with the
# embedding service and its fall-through to local are what must stay pinned.
# ---------------------------------------------------------------------------

MUTATIONS: list[Mutation] = [
    Mutation(
        id="M01",
        tests=("tests/test_v2438_hardening.py",),
        target="_search_vector remote payload",
        file="cpersona/vector.py",
        find='"min_similarity": effective_min_sim,',
        replace='"min_similarity": 0.0,',
        breaks="remote /search ignores the caller's threshold and over-returns (bug-027)",
        expect="test_remote_search_honors_min_similarity_argument",
    ),
    Mutation(
        id="M02",
        tests=("tests/test_v2438_hardening.py",),
        target="_search_vector remote timeout",
        file="cpersona/vector.py",
        find="timeout=REMOTE_SEARCH_TIMEOUT_SECS,",
        replace="",
        breaks="recall hot path inherits the 30s client default; a flapping endpoint stalls recall (bug-033)",
        expect="test_remote_search_honors_min_similarity_argument (second assert)",
    ),
    Mutation(
        id="M03",
        tests=("tests/test_refactor_seams_252.py", "tests/test_equivalence_252.py"),
        target="_search_vector remote isolation",
        file="cpersona/vector.py",
        find="iso_fetch = isolation_where(agent_id=agent_id, project_id=project_id, channel=channel)",
        replace="iso_fetch = isolation_where(agent_id=agent_id, project_id=None, channel='')",
        breaks="remote by-id fetch loses the γ axes; another project's row can surface (bug-046/075/100)",
        expect="test_refactor_seams_252.py::test_remote_by_id_fetch_refuses_rows_outside_the_isolation_axes, test_equivalence_252.py[sv-remote-isolation-miss]",
    ),
    # ---------------------------------------------------------------------
    # do_import_memories (admin_handlers.py) — the highest-value split target
    # and the one the soak never exercises.
    # ---------------------------------------------------------------------
    # M04 and M06 were both filed as equivalent mutants. Both classifications
    # turned out to be wrong, and they were wrong in the same way: each was
    # reasoned about on the real-import path, where a second layer does hold the
    # invariant up, without asking whether that second layer exists on the
    # dry_run path. It does not. A preview has no INSERT, so a guard the real
    # path can afford to lose is often the only one a preview has.
    Mutation(
        id="M04",
        tests=("tests/test_equivalence_252.py",),
        target="do_import_memories msg_id pre-check — load-bearing on the dry_run path",
        file="cpersona/admin_handlers.py",
        find="if existing or (tally.dry_run and (aid, pid, msg_id) in tally.seen_msgid):",
        replace="if False:",
        # RECLASSIFIED. Filed as equivalent because "the row
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
        tests=("tests/test_audit_2500b1.py",),
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
        # (2) BEHAVIOURAL. dry_run had two write targets and only
        #     one was doubly defended:
        #         database       read seam (M10) + this guard   -> rolled back
        #         remote index   this guard, alone              -> nothing
        #     `remote_items` was populated inside this guard and shipped after
        #     the transaction closed, where no rollback reaches. Removing the
        #     guard left the database spotless and published the previewed rows
        #     to the live index — invisible to every DB assertion in the suite,
        #     and found only because the behavioural snapshot records outbound
        #     traffic as well as rows.
        # (3) EQUIVALENT again — but for a different reason than
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
        tests=("tests/test_refactor_seams_252.py",),
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
        tests=("tests/test_refactor_seams_252.py",),
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
        tests=("tests/test_refactor_seams_252.py",),
        target="do_import_memories collision semantics",
        file="cpersona/admin_handlers.py",
        # Two INSERT OR IGNORE sites (import at :1617, merge at :1902); anchor on
        # the import one via its distinct column list.
        find="""            "INSERT OR IGNORE INTO memories"
            " (agent_id, project_id, channel, msg_id, content, source, timestamp, metadata,\"""",
        replace="""            "INSERT OR REPLACE INTO memories"
            " (agent_id, project_id, channel, msg_id, content, source, timestamp, metadata,\"""",
        breaks="a re-import overwrites existing rows instead of skipping — silent data loss on restore",
        # bug-349 put a content probe on the real-run arm too, so this clause is
        # now the SECOND line of defence and an ordinary collision never reaches
        # it. The pin that catches this moved with it: it blinds the probe and
        # asserts the write still refuses.
        expect="test_import_write_refuses_a_collision_the_probe_did_not_see",
    ),
    # ---------------------------------------------------------------------
    # do_merge_memories (admin_handlers.py).
    # ---------------------------------------------------------------------
    Mutation(
        id="M07",
        tests=("tests/test_audit_2500b1.py",),
        target="do_merge_memories move semantics",
        file="cpersona/admin_handlers.py",
        find='if mode == "move" and not dry_run:',
        replace="if False:",
        breaks="move leaves the source agent's rows behind; merge is no longer atomic",
        expect="test_merge_move_is_one_atomic_unit, test_merge_move_deletes_source_in_same_call",
    ),
    # ---------------------------------------------------------------------
    # do_calibrate_threshold (admin_handlers.py).
    # ---------------------------------------------------------------------
    Mutation(
        id="M08",
        tests=("tests/test_refactor_seams_252.py", "tests/test_equivalence_252.py"),
        target="do_calibrate_threshold sample floor",
        file="cpersona/admin_handlers.py",
        find="if len(vecs) < 10:",
        replace="if len(vecs) < 0:",
        breaks="calibrates a threshold from a handful of vectors; the null distribution is noise",
        expect="test_refactor_seams_252.py::test_calibrate_rejects_when_dim_filter_drops_below_the_floor, test_equivalence_252.py[calibrate-ragged]",
    ),
    Mutation(
        id="M09",
        tests=("tests/test_v2438_hardening.py",),
        target="do_calibrate_threshold dim filter",
        file="cpersona/admin_handlers.py",
        find="vecs = [v for v in vecs if v.shape[0] == target_dim]",
        replace="vecs = list(vecs)",
        breaks="ragged embedding dims reach the matmul; calibration crashes or scores garbage",
        expect="test_calibrate_survives_mixed_embedding_dims",
    ),
    # -----------------------------------------------------------------------
    # MemoryTaskQueue attribution (tasks.py) — the map that decides whose pause
    # governs a queued row. _forget_session covers the paths the queue drives;
    # rows also vanish underneath it, and only the reconcile pass sees those.
    # -----------------------------------------------------------------------
    Mutation(
        id="M13",
        tests=("tests/test_257_session_key_stage2.py",),
        target="queue attribution reconcile",
        file="cpersona/tasks.py",
        find="await self._forget_vanished_rows()",
        replace="pass",
        breaks="attributions for rows deleted outside the queue leak until the cap evicts a live one (bug-270)",
        expect="test_attribution_does_not_outlive_the_row (the out-of-band delete case)",
    ),
    Mutation(
        id="M14",
        tests=("tests/test_bug287_temporal_merge_order.py",),
        target="recall_with_context temporal merge order",
        file="cpersona/memory_handlers.py",
        find="""    parsed = _parse_timestamp_utc(m.get("timestamp", "") or "")
    return (parsed is not None, parsed if parsed is not None else _UNDATED_ORDER_ANCHOR)""",
        replace="""    return m.get("timestamp", "") or \"\"""",
        breaks="the merged conversation is ordered by how a stamp is spelled, so a row in another UTC offset reads as a turn that happened later (bug-287)",
        expect="test_a_stamp_in_another_offset_is_merged_at_its_instant_not_at_its_spelling, and the rwc-mixed-offset / rwc-invalid-timestamp-mixed golden scenarios",
    ),
    Mutation(
        id="M-N03a",
        tests=("tests/test_future_timestamp.py", "tests/test_equivalence_252.py"),
        target="the write seam's verdict on a timestamp ahead of the clock (bug-293)",
        file="cpersona/utils.py",
        # The detector goes blind: every stamp reads as inside the allowance. This
        # is the state the finding describes, restored in one line — store accepts
        # a 2099 stamp, says nothing about it, and `reject` has nothing to refuse.
        find="    if ahead <= FUTURE_TIMESTAMP_SKEW_SECONDS:\n        return None",
        replace="    if True:\n        return None",
        breaks="a stamp ahead of the clock is stored with no report and cannot be refused; it then scores as a row written this instant and flattens the scope's decay rate",
        expect=(
            "test_future_timestamp.py::test_past_the_allowance_is_reported, "
            "::test_warn_stores_the_row_and_reports_the_stamp, "
            "::test_reject_refuses_and_writes_nothing, "
            "test_equivalence_252.py[store-future-timestamp]"
        ),
    ),
    Mutation(
        id="M-N03b",
        tests=("tests/test_future_timestamp.py", "tests/test_255_repairable_contract.py", "tests/test_equivalence_252.py"),
        target="the health check's boundary — the rows already stored (bug-293)",
        file="cpersona/checks.py",
        # A boundary nothing can reach. The check still runs, still reports zero,
        # and stops reading the allowance it is configured with — which is also
        # what a check written against SQL's own clock would look like on any day
        # the calendar happens to agree.
        find="    boundary = future_timestamp_boundary()",
        replace='    boundary = "2999-01-01T00:00:00+00:00"',
        breaks="check_health never names a stored row whose timestamp is ahead of the clock, so fix=true has nothing to repair",
        expect=(
            "test_future_timestamp.py::test_the_check_finds_only_what_is_past_the_allowance, "
            "::test_the_check_reads_the_allowance_it_is_configured_with, "
            "::test_the_check_reads_the_clock_it_is_given_not_sqlites, "
            "test_255_repairable_contract.py, test_equivalence_252.py[corpus-future-timestamp-health]"
        ),
    ),
    Mutation(
        id="M-N03c",
        tests=("tests/test_future_timestamp.py",),
        target="the restore seam's report — faithful, but not silent (bug-293)",
        file="cpersona/admin_handlers.py",
        find='    if future_timestamp_issue(record.get("timestamp", "")):\n        tally.future_timestamps += 1',
        replace='    if False:\n        tally.future_timestamps += 1',
        breaks="a restore carries rows stamped ahead of the clock back in and reports nothing, which is the one seam that deliberately does not refuse them",
        expect="test_future_timestamp.py::test_a_restore_reports_a_future_stamp_and_imports_it_anyway",
    ),
    Mutation(
        id="M-N04",
        tests=("tests/test_unicode_identity.py", "tests/test_equivalence_252.py"),
        target="the Unicode identity detector (bug-295)",
        file="cpersona/checks.py",
        # Compare the row against itself instead of against its normal form: the
        # check still runs, still reports a shape, and reports zero forever. This
        # is the state the finding describes — nothing looking at all — restored
        # in one line.
        find='            if not isinstance(text, str) or unicodedata.normalize("NFC", text) == text:',
        replace="            if not isinstance(text, str) or text == text:",
        breaks="deep_check never reports a row that is not in a normal form, so a corpus splitting one meaning across two identities looks clean",
        expect=(
            "test_unicode_identity.py::test_the_detector_finds_the_decomposed_row_only, "
            "::test_the_sample_names_the_characters_that_compose, "
            "::test_the_half_voiced_mark_is_found_too, "
            "test_equivalence_252.py[corpus-unnormalized-deep-check]"
        ),
    ),
    # ---------------------------------------------------------------------
    # reconstruct (reconstruct.py) — the reconstructive recall exit.
    # The design states eight invariants and a count window. Each entry below
    # destroys exactly one of them; a SURVIVED line here means an invariant is
    # written down but not held.
    # ---------------------------------------------------------------------
    Mutation(
        id="M15",
        tests=("tests/test_reconstruct.py",),
        target="reconstruct invariant 7 — count and breadth are decoupled",
        file="cpersona/reconstruct.py",
        find="bounds_top_k = config.RECONSTRUCT_TOP_K if top_k is None else max(1, int(top_k))",
        replace="bounds_top_k = effective_count",
        breaks="the candidate depth is derived from the response count again, so asking for fewer items also searches less deeply",
        expect="test_reconstruct.py::test_count_alone_does_not_move_the_pool",
    ),
    Mutation(
        id="M16",
        tests=("tests/test_reconstruct.py",),
        target="reconstruct invariant 8 — one cluster is one item",
        file="cpersona/reconstruct.py",
        find="    clusters = [sorted(members) for _, members in sorted(grouped.items())]",
        replace="    clusters = [[i] for i in range(len(candidates))]",
        breaks="a bundled record is split back into one item per row, so the window is padded with fragments of one memory",
        expect="test_reconstruct.py::test_msg_id_bundles_and_derives_supersedes, ::test_4b_evidence_cut_sets_truncated",
    ),
    Mutation(
        id="M17",
        tests=("tests/test_reconstruct.py",),
        target="reconstruct invariant 3 — the written-down total order",
        file="cpersona/reconstruct.py",
        find="        -c.ts.timestamp() if c.ts is not None else 0.0,",
        replace="        0.0,",
        breaks="claims stop reading newest first, so a supersession chain no longer starts from its current statement",
        expect="test_reconstruct.py::test_msg_id_bundles_and_derives_supersedes",
    ),
    Mutation(
        id="M18",
        tests=("tests/test_reconstruct.py",),
        target="reconstruct invariant 2 — content is a quotation",
        file="cpersona/reconstruct.py",
        find='        "content": head.content,',
        replace='        "content": f"summary of {len(ordered)} rows",',
        breaks="the server writes a sentence it did not store — the one thing a zero-model read path must never do",
        expect="test_reconstruct.py::test_2_content_is_a_quotation",
    ),
    Mutation(
        id="M19",
        tests=("tests/test_reconstruct.py",),
        target="reconstruct invariant 4 — dropped rows are named",
        file="cpersona/reconstruct.py",
        find='        if omitted:\n            out["bounds"]["omitted"] = omitted',
        replace='        if False:\n            out["bounds"]["omitted"] = omitted',
        breaks="an item cut by the evidence bound looks whole, so a caller cannot tell it holds only part of the cluster",
        expect="test_reconstruct.py::test_4b_evidence_cut_names_the_bound_and_counts_the_rows",
    ),
    Mutation(
        id="M20",
        tests=("tests/test_reconstruct.py",),
        target="reconstruct invariant 5 — every element says why it is present",
        file="cpersona/reconstruct.py",
        find='        claim: dict = {"ref": m.ref, "as_of": m.timestamp, "why": why.get(m.ref, "seed")}',
        replace='        claim: dict = {"ref": m.ref, "as_of": m.timestamp, "why": ""}',
        breaks="a claim stops naming the key that admitted it, so an item cannot be audited back to a reason",
        expect="test_reconstruct.py::test_5_every_element_says_why",
    ),
    Mutation(
        id="M21",
        tests=("tests/test_reconstruct.py",),
        target="reconstruct — the Reconstruction Window's ceiling",
        file="cpersona/reconstruct.py",
        find="    effective = min(base, maximum)",
        replace="    effective = base",
        breaks="the server maximum stops bounding the window, so a caller's number is the only limit on payload size",
        expect="test_reconstruct.py::test_count_window_arithmetic",
    ),
    Mutation(
        id="M21b",
        tests=("tests/test_reconstruct_excerpts.py",),
        target="reconstruct — the payload budget's ceiling",
        file="cpersona/reconstruct.py",
        find="    budget = min(base, maximum)",
        replace="    budget = base",
        breaks="the server maximum stops bounding the payload budget, so a caller's number is the only limit on quoted text",
        expect="test_reconstruct_excerpts.py::test_budget_default_request_force_and_clamps",
    ),
    Mutation(
        id="M22",
        tests=("tests/test_reconstruct.py",),
        target="reconstruct stage 2 — 'adjacent timestamps, same source' is ONE key",
        file="cpersona/reconstruct.py",
        find="""                gap = candidates[right].ts.timestamp() - candidates[anchor].ts.timestamp()
                if gap <= window:
                    uf.union(anchor, right, "cluster:adjacent")""",
        replace="""                gap = 0
                if gap <= window:
                    uf.union(anchor, right, "cluster:adjacent")""",
        breaks="source alone bundles, so in a single-agent store every candidate folds into one item on every call",
        expect="test_reconstruct.py::test_source_alone_does_not_bundle",
    ),
    Mutation(
        id="M23",
        tests=("tests/test_reconstruct.py",),
        target="reconstruct invariant 1 — stored rows are never modified",
        file="cpersona/reconstruct.py",
        find="    uf = p.reconstructor.bundle(candidates, spans, links)",
        replace="""    async with connection() as _mutant_db:
        await _mutant_db.execute(
            "UPDATE memories SET content = content || ' (touched)' WHERE agent_id = ?", (agent_id,)
        )
        await _mutant_db.commit()
    uf = p.reconstructor.bundle(candidates, spans, links)""",
        breaks="the read path writes to the rows it read — an injected defect, because an invariant of absence cannot be broken by deletion",
        expect="test_reconstruct.py::test_1_stored_rows_are_not_modified",
    ),
    Mutation(
        id="M24",
        tests=("tests/test_reconstruct.py",),
        target="reconstruct head claim — the most relevant row, not the newest",
        file="cpersona/reconstruct.py",
        find="    lead = min(members, key=_relevance_key)",
        replace="    lead = min(members, key=_order_key)",
        breaks="an item quotes the last thing said in a burst instead of the row the retrieval ranked highest",
        expect="test_reconstruct.py::test_head_of_distinct_rows_is_the_most_relevant_not_the_newest",
    ),
    Mutation(
        id="M25",
        tests=("tests/test_reconstruct.py",),
        target="reconstruct head claim — a version chain resolves to its latest version",
        file="cpersona/reconstruct.py",
        find="    return min(versions, key=_order_key)",
        replace="    return min(versions, key=_relevance_key)",
        breaks="an item quotes a superseded version as current because the older version ranked higher",
        expect="test_reconstruct.py::test_head_of_a_version_chain_is_its_latest_version_even_when_older_ranks_higher",
    ),
    Mutation(
        id="M26",
        tests=("tests/test_reconstruct.py",),
        target="reconstruct evidence cut — keeps the head, then relevance decides",
        file="cpersona/reconstruct.py",
        find="    others = sorted((m for m in members if m is not head), key=_relevance_key)",
        replace="    others = sorted((m for m in members if m is not head), key=_order_key)",
        breaks="a bounded item keeps the newest rows and drops the ones that made it relevant",
        expect="test_reconstruct.py::test_an_evidence_cut_keeps_the_head_then_the_most_relevant_rows",
    ),
    # ---------------------------------------------------------------------
    # get_contents ranges (memory_handlers.py) — reconstruction v1.1 expansion.
    # A range is served exactly or refused; it is never widened to the row.
    # ---------------------------------------------------------------------
    Mutation(
        id="M27",
        tests=("tests/test_get_contents_ranges.py",),
        target="get_contents ranges — a range the server cannot serve is refused, not widened",
        file="cpersona/memory_handlers.py",
        find='''                if served is None:
                    unresolved.append({"ref": ref, "reason": reason})
                    continue''',
        replace='''                if served is None:
                    served = {"span": [0, len(text)]}''',
        breaks="a node that does not exist comes back as the whole record, the payload the caller asked to avoid, with nothing saying so",
        expect="test_get_contents_ranges.py::test_the_last_node_is_served_and_one_past_it_is_refused, ::test_a_node_range_on_a_record_without_a_complete_node_set_is_refused_not_widened",
    ),
    Mutation(
        id="M28",
        tests=("tests/test_get_contents_ranges.py",),
        target="get_contents ranges — nodes are served only from a partition of the stored text",
        file="cpersona/memory_handlers.py",
        find="        and all(a[2] == b[1] for a, b in zip(rows, rows[1:]))",
        replace="        and True",
        breaks="a node set with a gap or an overlap is served by offset, so a node range returns characters that are not the nodes it names",
        expect="test_get_contents_ranges.py::test_nodes_that_overlap_or_leave_a_gap_are_not_a_partition",
    ),
    Mutation(
        id="M29",
        tests=("tests/test_get_contents_ranges.py",),
        target="get_contents ranges — a range is checked after ownership",
        file="cpersona/memory_handlers.py",
        find='''            if kind == "mem":
                rows = await db.execute_fetchall(
                    "SELECT msg_id''',
        replace='''            if invalid is not None:
                unresolved.append({"ref": ref, "reason": invalid})
                continue
            if kind == "mem":
                rows = await db.execute_fetchall(
                    "SELECT msg_id''',
        breaks="a malformed range on a ref the caller does not own is answered as unresolved, so `missing` stops meaning 'not yours or not there' and `unresolved` stops meaning 'yours, but not servable'",
        expect="test_get_contents_ranges.py::test_a_range_on_another_agents_row_is_missing_and_says_nothing_about_its_nodes",
    ),
    Mutation(
        id="M30",
        tests=("tests/test_reconstruct.py",),
        target="reconstruct — a bound that was met is not a bound that dropped rows",
        file="cpersona/reconstruct.py",
        find='BOUND_EVIDENCE = "max_evidence"',
        replace='BOUND_EVIDENCE = "top_k"',
        breaks="rows the evidence bound dropped are reported under the depth's name, the one bound the tool cannot tell was a cut, so `omitted` stops meaning 'these rows exist and were withheld'",
        expect="test_reconstruct.py::test_4b_evidence_cut_names_the_bound_and_counts_the_rows",
    ),
    Mutation(
        id="M31",
        tests=("tests/test_reconstruct_excerpts.py",),
        target="reconstruct — node choice without a query embedding is admitted",
        file="cpersona/reconstruct.py",
        find="    if (node_sets or block_sets) and query_vec is None:\n",
        replace="    if False:\n",
        breaks="an unreachable embedding server silently turns node choice into trigram matching, and the quote looks as considered as any other",
        expect="test_reconstruct_excerpts.py::test_nodes_ranked_without_a_query_embedding_say_so",
    ),
    Mutation(
        id="M32",
        tests=("tests/test_reconstruct_excerpts.py",),
        target="reconstruct — a cut quote that is only the record's start says so",
        file="cpersona/reconstruct.py",
        find='            "node_unavailable",\n            "expand",\n',
        replace='            "expand",\n',
        breaks="the first 500 characters of a long record pass for the part that matched the query, which is the failure v1.1 exists to remove",
        expect="test_reconstruct_excerpts.py::test_a_long_record_is_quoted_from_the_node_that_matches_and_the_items_do_not_move",
    ),
    Mutation(
        id="M33",
        tests=("tests/test_reconstruct_review.py",),
        target="reconstruct — a compact response still says when the server overrode the request",
        file="cpersona/reconstruct.py",
        find='    if not count_policy["clamped"] and count_policy["source"] in _ASKED:',
        replace='    if count_policy["source"] in _ASKED:',
        breaks="a clamped count is served without its policy, so the compact envelope hides exactly the case it exists to keep: the server doing something other than what was asked",
        expect="test_reconstruct_review.py::test_a_clamped_count_states_its_policy_and_a_clamped_budget_its_own",
    ),
    Mutation(
        id="M34",
        tests=("tests/test_reconstruct_excerpts.py",),
        target="reconstruct — a cut node quote hands over the read that continues it",
        file="cpersona/reconstruct.py",
        find='quote["expand"] = {"ref": claim.ref, "node": quote["node"]["index"]}',
        replace='quote["expand"] = {"ref": claim.ref, "node": 0}',
        breaks="the ready-made argument reads the start of the record instead of the node that matched, so the cheapest next read returns the wrong text",
        expect="test_reconstruct_excerpts.py::test_a_cut_node_quote_hands_over_the_argument_that_reads_the_rest_of_its_node",
    ),
    # ---------------------------------------------------------------------
    # associative memory read by reconstruct (associations.py, reconstruct.py) —
    # docs/ASSOCIATIVE_MEMORY_DESIGN.md §3 and §5. The loader and the pure walk
    # both bound the walk; each entry below breaks the one it names, and the
    # tests it expects reach that layer directly.
    # ---------------------------------------------------------------------
    Mutation(
        id="M35",
        tests=("tests/test_associations_reconstruct.py",),
        target="associative stage 1 — declared names reach the lexical arm only",
        file="cpersona/reconstruct.py",
        find="        query,\n        effective_top_k,  # the candidate depth",
        replace="        (query + ' ' + ' '.join(cue_terms)).strip(),\n        effective_top_k,  # the candidate depth",
        breaks="an alias is appended to the query the vector arm embeds, so expanding a name moves what the query means",
        expect="test_associations_reconstruct.py::test_the_vector_arm_sees_the_query_unchanged",
    ),
    Mutation(
        id="M36",
        tests=("tests/test_associations_reconstruct.py",),
        target="associative invariant 1 — recall does not read the graph",
        file="cpersona/memory_handlers.py",
        find="    exclude_set: set[str] = set()\n    if exclude_contents:",
        replace=(
            "    if lexical_terms is None:\n"
            "        from cpersona import associations as _a\n"
            "        lexical_terms = (await _a.query_terms(agent_id, query, project_id=project_id, channel=channel))[0] or None\n"
            "    exclude_set: set[str] = set()\n    if exclude_contents:"
        ),
        breaks="recall expands declared aliases on its own, so the flat contract changes with every declaration",
        expect="test_associations_reconstruct.py::test_recall_is_unchanged_by_a_populated_graph",
    ),
    Mutation(
        id="M37",
        tests=("tests/test_associations_reconstruct.py",),
        target="associative invariant 2 — a graph the query does not name is not read",
        file="cpersona/associations.py",
        find="        if not matched:\n            return [], {}\n        marks",
        replace=(
            "        matched = sorted({r[0] for r in await db.execute_fetchall("
            "f'SELECT e.id FROM entities e WHERE {iso.clause}', iso.params)})\n"
            "        if not matched:\n            return [], {}\n        marks"
        ),
        breaks="every entity in scope counts as named by every query, so a store with any declaration answers differently from one without",
        expect="test_associations_reconstruct.py::test_a_graph_that_does_not_apply_changes_nothing",
    ),
    Mutation(
        id="M38",
        tests=("tests/test_associations_reconstruct.py",),
        target="associative stage 2 — a declared record relation bundles its endpoints",
        file="cpersona/reconstruct.py",
        find='            uf.union(index[subject], index[obj], "cluster:relation", WHY_RELATION + predicate)',
        replace="            pass",
        breaks="two candidates an agent declared as one correction of the other come back as separate items",
        expect="test_associations_reconstruct.py::test_a_record_relation_merges_items_without_reordering_the_rest",
    ),
    Mutation(
        id="M39",
        tests=("tests/test_associations_reconstruct.py",),
        target="associative roles — the referenced row is the subject",
        file="cpersona/reconstruct.py",
        find='''        if obj == claim.ref and subject in retained and predicate in ROLE_VOCABULARY:
            role = {"ref": subject, "role": predicate}''',
        replace='''        if subject == claim.ref and obj in retained and predicate in ROLE_VOCABULARY:
            role = {"ref": obj, "role": predicate}''',
        breaks="a declared correction is emitted on the correcting row, so the reader is told the new statement is what was corrected",
        expect="test_associations_reconstruct.py::test_each_role_word_is_emitted_in_the_vocabulary_direction",
    ),
    Mutation(
        id="M40",
        tests=("tests/test_associations_reconstruct.py",),
        target="associative invariant 5 — the walk stops at max_hops by itself",
        file="cpersona/reconstruct.py",
        find="        for hop in range(1, max_hops + 1):\n            step",
        replace="        for hop in range(1, max_hops + 2):\n            step",
        breaks="the walk follows one relation more than the caller allowed whenever the graph holds it",
        expect="test_associations_reconstruct.py::test_the_walk_stops_at_its_hop_bound_even_when_the_graph_holds_more",
    ),
    Mutation(
        id="M41",
        tests=("tests/test_associations_reconstruct.py",),
        target="associative stage 3 — sharing an entity is not a relation",
        file="cpersona/reconstruct.py",
        find="            if hops == 0:\n                continue\n            if entity in graph.records_cut:\n                cuts[position].add(BOUND_EVIDENCE)\n            recency, predicate = via[entity]",
        replace="            if entity in graph.records_cut:\n                cuts[position].add(BOUND_EVIDENCE)\n            recency, predicate = via.get(entity, (0, 'mentions'))",
        breaks="every record that mentions the candidate's own entity becomes evidence, which is the contamination bundling by entity was refused for",
        expect="test_associations_reconstruct.py::test_the_walk_starts_from_each_items_own_candidates_and_skips_their_own_entities",
    ),
    Mutation(
        id="M42",
        tests=("tests/test_associations_reconstruct.py",),
        target="associative invariant 5 — the written order of the evidence cut",
        file="cpersona/associations.py",
        find="newest_first = sorted(edges, key=lambda r: (edges[r][3], -r), reverse=True)",
        replace="newest_first = sorted(edges, key=lambda r: (edges[r][3], -r))",
        breaks="an item bounded by max_evidence keeps what an old relation reached and drops what the latest declaration reached",
        expect="test_associations_reconstruct.py::test_the_evidence_cut_follows_hops_then_recency_then_record_id",
    ),
    Mutation(
        id="M43",
        tests=("tests/test_associations_reconstruct.py",),
        target="associative invariant 3 — a reached record is never an item",
        file="cpersona/reconstruct.py",
        find=(
            "        item, _ = p.reconstructor.structure(rows, why_by_ref, spans, bounds_max_evidence, extra, links)\n"
            "        providers.check_structure(item, {row.ref for row in rows} | {row.ref for row, _, _ in extra})\n"
            "        if position >= len(window):\n"
            "            # Said in the words recall uses for the same row, so one reading covers both.\n"
            "            item[\"admission\"] = \"reservation\"\n"
            "        selected.append(item)"
        ),
        replace=(
            "        item, _ = p.reconstructor.structure(rows, why_by_ref, spans, bounds_max_evidence, (), links)\n"
            "        if position >= len(window):\n"
            "            item[\"admission\"] = \"reservation\"\n"
            "        selected.append(item)\n"
            "        for row, label, hops in extra:\n"
            "            selected.append(structure([row], {}, spans, bounds_max_evidence)[0])"
        ),
        breaks="records the walk reached become items of their own, so a declaration reorders and pads the window",
        expect="test_associations_reconstruct.py::test_entity_relations_leave_item_heads_and_order_unchanged",
    ),
    Mutation(
        id="M44",
        tests=("tests/test_associations_reconstruct.py",),
        target="associative invariant 7 — the walk reads this agent's relations only",
        file="cpersona/associations.py",
        find='    iso_r = isolation_where(agent_id=agent_id, project_id=project_id, channel=channel, alias="r")\n    iso_s',
        replace='    iso_r = isolation_where(agent_id=None, project_id="", channel=channel, alias="r")\n    iso_s',
        breaks="a relation another agent's row holds is followed, so one agent's declarations shape another's evidence",
        expect="test_associations_reconstruct.py::test_rows_that_cross_agents_are_not_read_even_when_they_exist",
    ),
    Mutation(
        id="M45",
        tests=("tests/test_associations_reconstruct.py",),
        target="associative invariant 7 — a reached record is one the call could read",
        file="cpersona/associations.py",
        find='    iso_m = isolation_where(agent_id=agent_id, project_id=project_id, channel=channel, alias="t")',
        replace='    iso_m = isolation_where(agent_id=agent_id, project_id=None, channel=channel, alias="t")',
        breaks="a record in a project the call does not read is quoted as evidence, through a relation declared in one it does",
        expect="test_associations_reconstruct.py::test_a_record_the_call_could_not_read_is_not_reached",
    ),
    Mutation(
        id="M46",
        tests=("tests/test_associations_traverse.py", "tests/test_associations_reconstruct.py"),
        target="associative graph reads — relations are followed from either end",
        file="cpersona/associations.py",
        find='f"AND (r.subject_id IN ({marks}) OR r.object_id IN ({marks}))",',
        replace='f"AND (r.subject_id IN ({marks}) AND r.object_id IN ({marks}))",',
        breaks="an entity named as a relation's object reaches nothing through it, so traverse and the reconstruct walk see half the graph",
        expect="test_associations_traverse.py::test_the_neighbourhood_to_the_hop_bound, test_associations_reconstruct.py::test_relations_are_followed_in_both_directions",
    ),
    Mutation(
        id="M47",
        tests=("tests/test_associations_traverse.py",),
        target="traverse — limit bounds the entities returned",
        file="cpersona/associations.py",
        find="        kept = order[:limit]\n",
        replace="        kept = order\n",
        breaks="a hub entity returns its whole neighbourhood whatever the caller asked for",
        expect="test_associations_traverse.py::test_limit_bounds_entities_and_mentions_and_says_so",
    ),
    Mutation(
        id="M48",
        tests=("tests/test_associations_traverse.py",),
        target="traverse — refs only, never record text",
        file="cpersona/associations.py",
        find='                entry["mentions"] = [ref for _, _, ref, _ in found]',
        replace='                entry["mentions"] = [row["content"] for _, _, _, row in found]',
        breaks="the graph query returns stored text, so its payload grows with the records and bypasses the preview tier",
        expect="test_associations_traverse.py::test_no_record_text_is_returned",
    ),
    Mutation(
        id="M49",
        tests=("tests/test_associations_traverse.py",),
        target="traverse — the named entity is one this agent declared",
        file="cpersona/associations.py",
        find='    iso = isolation_where(agent_id=agent_id, project_id=project_id, channel=channel, alias="e")\n    async with connection() as db:\n        starts',
        replace='    iso = isolation_where(agent_id=None, project_id=project_id, channel=channel, alias="e")\n    async with connection() as db:\n        starts',
        breaks="another agent's entity of the same name becomes a start, and its identity shows in this agent's answer",
        expect="test_associations_traverse.py::test_isolation_agent_project_and_readable_records",
    ),
    Mutation(
        id="M50",
        tests=("tests/test_reconstruct_block_reservation.py",),
        target="reconstruct holds the block reservation beside the window",
        file="cpersona/reconstruct.py",
        find="    chosen = window + held\n",
        replace="    chosen = window\n",
        breaks="a record only the block arm reached never comes back once the gate has filled the window",
        expect="test_reconstruct_block_reservation.py::test_a_full_window_still_returns_the_reserved_record",
    ),
    Mutation(
        id="M51",
        tests=("tests/test_reconstruct_block_reservation.py",),
        target="a reserved record never takes a place in the window",
        file="cpersona/reconstruct.py",
        find="    window = [group for group in ordered if any(not candidates[i].reserved for i in group)][:effective_count]\n",
        replace="    window = ordered[:effective_count]\n",
        breaks="with room in the window a reserved row is ranked as an item the gate admitted, and the caller cannot tell",
        expect="test_reconstruct_block_reservation.py::test_a_reserved_record_never_takes_a_place_in_the_window",
    ),
    Mutation(
        id="M52",
        tests=("tests/test_260a7_prior_function.py",),
        target="with confidence enabled, the confidence score no longer re-sorts recall",
        file="cpersona/memory_handlers.py",
        find='    if _confidence_orders():\n        for r in results:\n            ts = r.get("timestamp", "")\n',
        replace='    if CONFIDENCE_ENABLED:\n        for r in results:\n            ts = r.get("timestamp", "")\n',
        breaks="confidence on discards the fusion order again, and any prior applied in the fusion does nothing",
        expect="test_260a7_prior_function.py::test_enabled_confidence_changes_nothing_but_the_confidence_field",
    ),
    Mutation(
        id="M53",
        tests=("tests/test_260a7_prior_function.py",),
        target="the gate calibration measures the signal the runtime gate compares",
        file="cpersona/admin_handlers.py",
        find='    if _confidence_orders():\n        return "confidence"\n',
        replace='    if config.CONFIDENCE_ENABLED:\n        return "confidence"\n',
        breaks="with confidence on, calibration collects no row whose signal matches and stores no gate",
        expect="test_260a7_prior_function.py::test_calibration_measures_the_signal_the_runtime_gate_compares",
    ),
    Mutation(
        id="M54",
        tests=("tests/test_260a7_prior_function.py",),
        target="the age weight orders the admitted rows by score times weight",
        file="cpersona/memory_handlers.py",
        find='        key=lambda r: r[key] * r["_prior"] if r.get("id") != -1 else float("-inf"),\n',
        replace='        key=lambda r: r[key] if r.get("id") != -1 else float("-inf"),\n',
        breaks="the age weight is computed and reported but never moves a row",
        expect="test_260a7_prior_function.py::test_the_weight_orders_by_score_times_age_weight",
    ),
    Mutation(
        id="M55",
        tests=("tests/test_260a7_prior_function.py",),
        target="the age weight never rewrites the score the gate reads",
        file="cpersona/memory_handlers.py",
        find='        r["_prior"] = _age_weight(age)\n',
        replace='        r["_prior"] = _age_weight(age)\n        r[key] = r[key] * r["_prior"]\n',
        breaks="the weight leaks into the score the gate and match_reason read, so a later move of the call ahead of the gate would let it remove rows",
        expect="test_260a7_prior_function.py::test_the_weight_never_rewrites_the_score_the_gate_reads",
    ),
    Mutation(
        id="M56",
        tests=("tests/test_recall_trace.py",),
        target="recall trace — a requested trace changes nothing in the messages",
        file="cpersona/memory_handlers.py",
        find="    token = rec.activate()\n    try:\n        result = await _do_recall(agent_id, query, limit, **kwargs)",
        replace="    token = rec.activate()\n    try:\n        result = await _do_recall(agent_id, query, max(1, limit - 1), **kwargs)",
        breaks="asking for a trace changes what the recall returns, so a traced run measures a different recall",
        expect="test_recall_trace.py::test_a_requested_trace_changes_nothing_in_the_messages",
    ),
    Mutation(
        id="M57",
        tests=("tests/test_blocks_retrieval.py",),
        target="recall trace — the block arm is an arm",
        file="cpersona/memory_handlers.py",
        find='                trace_rec.arm("block", block_rows, "_block_distance")',
        replace="                pass",
        breaks="a record only the block arm reached is confirmed as a candidate miss although it was returned",
        expect="test_blocks_retrieval.py::test_a_reserved_row_is_traced_as_reached_and_returned",
    ),
    Mutation(
        id="M58",
        tests=("tests/test_recall_trace.py",),
        target="recall trace — a gate decision says what the gate did",
        file="cpersona/memory_handlers.py",
        find='rec.gate_decision(r, "rrf", rrf, rrf_threshold, rrf >= rrf_threshold, "below_gate")',
        replace='rec.gate_decision(r, "rrf", rrf, rrf_threshold, True, "below_gate")',
        breaks="the trace says a row was admitted that the gate dropped, so a filter drop is confirmed as something else",
        expect="test_recall_trace.py::test_every_gate_branch_records_its_signal_and_reason",
    ),
    Mutation(
        id="M59",
        tests=("tests/test_recall_trace.py",),
        target="recall trace confirmation — a held seat returns a row",
        file="benchmarks/recall_trace_confirm.py",
        find='    reserved = {row["ref"] for row in trace.get("reservation", [])}\n',
        replace="    reserved = set()\n",
        breaks="a record the reservation returned is confirmed as lost at the gate or the count cut",
        expect="test_recall_trace.py::test_confirm_orders_the_stages",
    ),
    Mutation(
        id="M60",
        tests=("tests/test_recall_cue.py",),
        target="time cue — no row moves up more than L places",
        file="cpersona/memory_handlers.py",
        find='        bound = cue.LIFT[cue_note["confidence"]]\n',
        replace="        bound = 10\n",
        breaks="a wrong cue can carry a row from the bottom of the answer to the top, so its harm is no longer bounded by construction",
        expect="test_recall_cue.py::test_a_cue_changes_order_not_admission",
    ),
    Mutation(
        id="M61",
        tests=("tests/test_recall_cue.py",),
        target="time cue — a seat never holds a row the gate or autocut refused",
        file="cpersona/memory_handlers.py",
        find='if r["_rid"] not in present and (r["_rid"] not in reached or r["_rid"] in admitted_rids)\n',
        replace='if r["_rid"] not in present\n',
        breaks="a row the quality gate refused comes back through the cue's seat, so a cue changes which rows are admitted",
        expect="test_recall_cue.py::test_a_row_the_gate_refused_does_not_come_back_through_the_seat",
    ),
    Mutation(
        id="M62",
        tests=("tests/test_recall_cue.py",),
        target="time cue — the cue arm searches only the period",
        file="cpersona/memory_handlers.py",
        find='        src_clause_m += " AND datetime(m.timestamp) >= datetime(?) AND datetime(m.timestamp) < datetime(?)"',
        replace='        src_clause_m += " AND datetime(m.timestamp) >= datetime(?) AND ? IS NOT NULL"',
        breaks="the cue arm returns records after the period, so a cue lifts rows it does not point at",
        expect="test_recall_cue.py::test_the_cue_arm_searches_only_the_period",
    ),
]

# ---------------------------------------------------------------------------
# The provider seams of recall and reconstruct (cpersona/providers.py).
#
# A stage called back through its built-in directly returns the same rows -- the
# built-in IS the function the Core used to call -- so no test of what recall
# returns can see a call site that stopped using the installed provider. Only a
# test that installs a different provider can, and the bypass mutants below show
# that test_providers.py's recording providers are that test, one call site each.
# The check mutants show that each of the Core's checks on a stage's output is
# what stops a provider that breaks the contract, and the registry mutants that
# each refusal rule is held by its own test.
# ---------------------------------------------------------------------------

_MH = "cpersona/memory_handlers.py"
_RC = "cpersona/reconstruct.py"
_PV = "cpersona/providers.py"
_IMPORT_BUILTINS = {
    _MH: ("from cpersona import providers\n", "from cpersona import providers\nfrom cpersona import builtin_providers\n"),
    _RC: ("    from . import providers\n", "    from . import providers\n    from . import builtin_providers\n"),
}
_SPY = "test_providers.py::test_the_core_calls_each_operation_through_its_slot"
_SEATED = "test_providers.py::test_a_seated_recall_ranks_twice_through_the_slots"

# (id, file, the call as the Core writes it, the built-in class, slot, operation, the test that pins it)
_BYPASSES = [
    ("M63", _MH, "p.fusion.retrieve(", "Fusion", "fusion", "retrieve", None),
    ("M64", _MH, "p.scoring.score(", "Scoring", "scoring", "score", None),
    ("M65", _MH, "p.block_candidates.reserved_rows(", "BlockCandidates", "block_candidates", "reserved_rows", None),
    ("M66", _MH, "active.cue_interpreter.parse(", "CueInterpreter", "cue_interpreter", "parse", None),
    ("M67", _MH, "p.cue_interpreter.recent_only(", "CueInterpreter", "cue_interpreter", "recent_only", None),
    ("M68", _MH, "p.envelope_planner.period(time_cue, confidence,", "EnvelopePlanner", "envelope_planner", "period", None),
    ("M69", _MH, "p.envelope_planner.wider(", "EnvelopePlanner", "envelope_planner", "wider", None),
    ("M70", _MH, "p.cue_candidates.search(", "CueCandidates", "cue_candidates", "search", None),
    ("M71", _MH, "p.prior.apply(", "Prior", "prior", "apply", None),
    ("M72", _MH, "p.evidence_selector.lift(", "EvidenceSelector", "evidence_selector", "lift", None),
    ("M73", _MH, "p.evidence_selector.seats(", "EvidenceSelector", "evidence_selector", "seats", None),
    ("M74", _RC, "p.reconstruct_candidates.candidates(", "ReconstructCandidates", "reconstruct_candidates", "candidates", None),
    ("M75", _RC, "p.reconstructor.bundle(", "Reconstructor", "reconstructor", "bundle", None),
    ("M76", _RC, "p.reconstructor.walk(", "Reconstructor", "reconstructor", "walk", None),
    ("M77", _RC, "p.reconstructor.structure(", "Reconstructor", "reconstructor", "structure", None),
    ("M78", _RC, "p.reconstructor.allocate(", "Reconstructor", "reconstructor", "allocate", None),
    ("M79", _RC, "p.cue_interpreter.parse(", "CueInterpreter", "cue_interpreter", "parse",
     "test_providers.py::test_reconstruct_reads_the_cue_through_the_installed_interpreter"),
    # The propagation seat ranks the recall a second time, so fusion, scoring and
    # the prior each have two call sites; each is its own mutant, and the pin
    # counts the calls of one seated recall, because the spy's any-call test would
    # let one site's bypass hide behind the other's call.
    ("M106", _MH, "p.fusion.retrieve(", "Fusion", "fusion", "retrieve", _SEATED),
    ("M107", _MH, "p.scoring.score(", "Scoring", "scoring", "score", _SEATED),
    ("M108", _MH, "p.prior.apply(", "Prior", "prior", "apply", _SEATED),
    ("M109", _MH, "p.propagation_selector.seat(", "PropagationSelector", "propagation_selector", "seat", None),
]

# Where a call appears more than once in its file, the text before it names the site.
_LEADS = {
    "M63": "results = await ",
    "M64": "results, time_range_hours, recall_counts, newest_age_hours = await ",
    "M71": "results = ",
    "M106": "order = await ",
    "M107": "order, *_ = await ",
    "M108": "order = ",
}

MUTATIONS += [
    Mutation(
        id=mid,
        tests=("tests/test_providers.py",),
        target=f"provider seams — {'reconstruct' if file == _RC else 'recall'} calls {slot}.{op} through its slot",
        file=file,
        find=_LEADS.get(mid, "") + call,
        replace=_LEADS.get(mid, "") + f"builtin_providers.{cls}()" + call[call.index(f".{op}("):],
        also=(_IMPORT_BUILTINS[file],),
        breaks=f"an installed {slot} provider is ignored at this call and the built-in runs instead",
        expect=pin or f"{_SPY}[{slot}-{op}]",
    )
    for mid, file, call, cls, slot, op, pin in _BYPASSES
]

MUTATIONS += [
    Mutation(
        id="M80",
        tests=("tests/test_providers.py",),
        target="provider seams — the cue's move is checked against the bound the Core fixed",
        file=_MH,
        find="        providers.check_lift(results, lifted, bound)\n",
        replace="",
        breaks="a selector can move a row past the bound, or drop one, and the answer carries it",
        expect="test_providers.py::test_a_move_past_its_bound_or_out_of_its_rows_is_stopped",
    ),
    Mutation(
        id="M81",
        tests=("tests/test_providers.py",),
        target="provider seams — the seats go to eligible rows, no more than are held",
        file=_MH,
        find="        providers.check_seats(seated, eligible, places)\n",
        replace="",
        breaks="a selector can fill more places than are held, or seat a row the gate refused",
        expect="test_providers.py::test_a_seat_beyond_the_held_one_or_for_another_row_is_stopped",
    ),
    Mutation(
        id="M82",
        tests=("tests/test_providers.py",),
        target="provider seams — the prior reorders and neither admits nor removes",
        file=_MH,
        find='    providers.check_reorder("prior.apply", admitted, results)\n',
        replace="",
        breaks="a prior can remove a row the gate admitted",
        expect="test_providers.py::test_a_prior_that_removes_a_row_is_stopped",
    ),
    Mutation(
        id="M83",
        tests=("tests/test_providers.py",),
        target="provider seams — the walk reaches only records the graph read holds",
        file=_RC,
        find="    providers.check_walk(reached, chosen, graph.rows if graph is not None else {})\n",
        replace="",
        breaks="a walk can name a record the call never read, or lose a cluster's result",
        expect="test_providers.py::test_a_reconstructor_that_breaks_its_contract_is_stopped",
    ),
    Mutation(
        id="M84",
        tests=("tests/test_providers.py",),
        target="provider seams — an item's head and claims are its own records",
        file=_RC,
        find="        providers.check_structure(item, {row.ref for row in rows} | {row.ref for row, _, _ in extra})\n",
        replace="",
        breaks="an item can name a head that is none of the records it was built from",
        expect="test_providers.py::test_a_reconstructor_that_breaks_its_contract_is_stopped",
    ),
    Mutation(
        id="M85",
        tests=("tests/test_providers.py",),
        target="provider seams — the budget chooses a prefix and nothing else (invariant 9)",
        file=_RC,
        find="        providers.check_allocation(entries, items, used_budget, allowance, budget_cut)\n",
        replace="",
        breaks="an allocation can skip an item, misreport what it carries, or carry past the budget",
        expect="test_providers.py::test_an_allocation_past_the_budget_is_stopped",
    ),
    Mutation(
        id="M86",
        tests=("tests/test_providers.py",),
        target="provider seams — a request keeps the set it started with",
        file=_MH,
        find="newest_age_hours = await p.scoring.score(",
        replace="newest_age_hours = await providers.active().scoring.score(",
        breaks="a set installed while a recall runs takes over its later stages",
        expect="test_providers.py::test_a_request_keeps_the_set_it_started_with",
    ),
    Mutation(
        id="M110",
        tests=("tests/test_providers.py",),
        target="provider seams — the propagation seat chooses with the set the recall started with",
        file=_MH,
        find="p.propagation_selector.seat(",
        replace="providers.active().propagation_selector.seat(",
        breaks="a set installed while a seated recall runs chooses its seat",
        expect="test_providers.py::test_a_seated_request_keeps_the_set_it_started_with",
    ),
    Mutation(
        id="M96",
        tests=("tests/test_providers.py",),
        target="provider seams — the cue is read with the set the recall runs with",
        file=_MH,
        find="        providers_=active,\n",
        replace="",
        breaks="a set installed between reading the cue and retrieving takes over the retrieval",
        expect="test_providers.py::test_the_set_is_read_before_the_cue_and_kept_after_it",
    ),
]

# (id, the refusal as written, what replaces it, what the registry then accepts, the test id)
_REFUSALS = [
    ("M87", "        factory = allowlist.get(name, {}).get(provider_id)\n",
     "        factory = allowlist.get(name, {}).get(provider_id) or allowlist.get(name, {}).get(BUILTIN)\n",
     "an id the allowlist does not hold silently runs the built-in",
     "test_a_selection_the_allowlist_does_not_hold_is_refused"),
    ("M88", "    if manifest.contract[0] != CONTRACT_MAJOR:\n", "    if False:\n",
     "a provider built for another major contract", "test_a_manifest_the_core_cannot_call_is_refused"),
    ("M89", "    missing = [op for op in slot.operations if op not in manifest.capabilities]\n", "    missing = []\n",
     "a provider that does not declare an operation the Core calls", "test_a_manifest_the_core_cannot_call_is_refused"),
    ("M90", "    absent = [op for op in slot.operations if not callable(getattr(provider, op, None))]\n", "    absent = []\n",
     "a provider that declares an operation it does not have", "test_a_declared_operation_the_provider_does_not_have_is_refused"),
    ("M91", "    if manifest.generative:\n", "    if False:\n",
     "a provider that generates text", "test_a_manifest_the_core_cannot_call_is_refused"),
    ("M92", "    if not manifest.deterministic:\n", "    if False:\n",
     "a provider that is not deterministic", "test_a_manifest_the_core_cannot_call_is_refused"),
    ("M93", '    if manifest.locality != "in_process":\n', "    if False:\n",
     "a provider that runs outside this process", "test_a_manifest_the_core_cannot_call_is_refused"),
    ("M94", "    if manifest.slot != slot.name:\n", "    if False:\n",
     "a provider made for another slot", "test_a_manifest_the_core_cannot_call_is_refused"),
    ("M95", "    if manifest.provider_id != provider_id:\n", "    if False:\n",
     "an allowlist id that names a provider calling itself something else",
     "test_an_allowlist_id_must_be_the_name_the_provider_gives_itself"),
]

MUTATIONS += [
    Mutation(
        id=mid,
        tests=("tests/test_providers.py",),
        target="provider seams — the registry refuses what the Core cannot call",
        file=_PV,
        find=find,
        replace=replace,
        breaks=f"the registry accepts {accepted}",
        expect=f"test_providers.py::{test}",
    )
    for mid, find, replace, accepted, test in _REFUSALS
]

# ---------------------------------------------------------------------------
# The budget ledger (cpersona/budget.py) and the trace's record of each stage's
# input (recall_trace.stage_input). The ledger's limits are the bounds recall
# already held, so the mutants below either loosen a bound (and the loop runs a
# stage it never ran), stop counting (and the trace misreports what was spent),
# or move a recorded input to another stage (and a replay would start from the
# wrong rows).
# ---------------------------------------------------------------------------

_BG = "cpersona/budget.py"
_RT = "cpersona/recall_trace.py"

MUTATIONS += [
    Mutation(
        id="M97",
        tests=("tests/test_budget.py", "tests/test_recall_cue.py"),
        target="budget ledger — the cue loop runs at most two stages",
        file=_BG,
        find="CUE_STAGES = 2\n",
        replace="CUE_STAGES = 3\n",
        breaks="an empty widened period is widened again, to the vague width, a stage v0 never ran",
        expect="test_budget.py::test_a_cue_whose_widened_period_is_still_empty_stops_at_the_ledger",
    ),
    Mutation(
        id="M98",
        tests=("tests/test_budget.py", "tests/test_recall_cue.py"),
        target="budget ledger — the cue loop asks the ledger before another stage",
        file=_MH,
        find="                if cue_rows or not ledger.allows(budget.CUE_STAGE):\n",
        replace="                if cue_rows:\n",
        breaks="the loop starts a stage the ledger does not allow, and the recall fails instead of stopping",
        expect="test_budget.py::test_a_cue_whose_widened_period_is_still_empty_stops_at_the_ledger",
    ),
    Mutation(
        id="M99",
        tests=("tests/test_budget.py",),
        target="budget ledger — the one hypothesis a recall evaluates is counted",
        file=_MH,
        find="    ledger.spend(budget.ITERATION)\n",
        replace="",
        breaks="a trace reports no hypothesis evaluated, so an iteration budget reads as untouched",
        expect="test_budget.py::test_a_plain_recall_spends_one_fetch_and_one_iteration",
    ),
    Mutation(
        id="M100",
        tests=("tests/test_budget.py",),
        target="budget ledger — the stop reason tells an unused budget from a spent one",
        file=_BG,
        find="        return STOP_NO_HYPOTHESIS if self.allows(ITERATION) else STOP_BUDGET\n",
        replace="        return STOP_BUDGET\n",
        breaks="a budget of 256 that evaluated one hypothesis reads as though all 256 were spent",
        expect="test_budget.py::test_a_larger_iteration_budget_is_reported_unused_and_changes_nothing",
    ),
    Mutation(
        id="M101",
        tests=("tests/test_budget.py",),
        target="budget ledger — the block arm's fetch is its own",
        file=_MH,
        find="            ledger.spend(budget.BLOCK_FETCH)\n",
        replace="",
        breaks="a recall that ran the block arm reports it never fetched",
        expect="test_budget.py::test_the_block_arm_spends_its_own_fetch",
    ),
    Mutation(
        id="M102",
        tests=("tests/test_budget.py",),
        target="budget ledger — a spend past the limit is refused",
        file=_BG,
        find="        if not self.allows(kind):\n",
        replace="        if False:\n",
        breaks="a stage can spend past its limit and the ledger counts it as though it fit",
        expect="test_budget.py::test_spending_up_to_the_limit_is_counted_and_past_it_is_refused",
    ),
    Mutation(
        id="M103",
        tests=("tests/test_trace_seams.py",),
        target="trace seams — the cut's recorded input is what the prior returned",
        file=_MH,
        find='        trace_rec.stage_input("cut", results)\n',
        replace='        trace_rec.stage_input("cut", admitted)\n',
        breaks="a replay starting at the cut would start from the order before the prior",
        expect="test_trace_seams.py::test_only_the_stages_after_a_changed_stage_see_a_different_input",
    ),
    Mutation(
        id="M104",
        tests=("tests/test_trace_seams.py",),
        target="trace seams — the digest keeps the order",
        file=_RT,
        find="    refs = [ref_of(r) for r in rows]\n",
        replace="    refs = sorted(ref_of(r) for r in rows)\n",
        breaks="two inputs with the same rows in another order read as the same input",
        expect="test_trace_seams.py::test_only_the_stages_after_a_changed_stage_see_a_different_input",
    ),
    Mutation(
        id="M105",
        tests=("tests/test_trace_seams.py",),
        target="trace seams — a traced recall names the provider set",
        file=_MH,
        find='    rec.set("providers", {"digest": active.digest, "slots": active.describe()})\n',
        replace="",
        breaks="a trace cannot say which providers produced it, so a replay cannot check it runs the same set",
        expect="test_trace_seams.py::test_a_traced_recall_names_the_provider_set",
    ),
    # cued-v0.3 (docs/RECALL_PROCESS_DESIGN.md §2.11): what the policy changed, each
    # held by the test written for it.
    Mutation(
        id="M111",
        tests=("tests/test_recall_cue.py",),
        target="time cue — a seat may hold a row the count cut",
        file=_MH,
        find='if r["_rid"] not in present and (r["_rid"] not in reached or r["_rid"] in admitted_rids)\n',
        replace='if r["_rid"] not in present and r["_rid"] not in reached\n',
        breaks="a row the gate admitted and the count cut cannot take a seat, although the cue's period holds it",
        expect="test_recall_cue.py::test_a_row_the_count_cut_can_take_a_seat",
    ),
    Mutation(
        id="M112",
        tests=("tests/test_recall_cue.py",),
        target="time cue — as many seats as L for the confidence searched",
        file=_MH,
        find='        places = cue.SEATS[cue_note["confidence"]]\n',
        replace="        places = 1\n",
        breaks="a sure or likely cue holds one seat, so its period's other records stay out of the answer",
        expect="test_recall_cue.py::test_the_seats_are_as_many_as_the_confidence_holds",
    ),
    Mutation(
        id="M113",
        tests=("tests/test_recall_cue.py",),
        target="time cue — the cue arm searches to its own depth, whatever the count",
        file=_MH,
        find="depth=cue.DEPTH, window=window,",
        replace="depth=depth, window=window,",
        breaks="the cue arm ranks only as deep as the count, so on a wide period it finds what the ordinary arms returned",
        expect="test_recall_cue.py::test_the_cue_arm_searches_to_its_own_depth_whatever_the_count",
    ),
    Mutation(
        id="M114",
        tests=("tests/test_trace_embedding_model.py",),
        target="recall trace — names the embedding model that produced the vectors (bug-441)",
        file=_MH,
        find='"embedding_model": generation.trace_model(),',
        replace='"embedding_model": config.EMBEDDING_MODEL,',
        breaks="a trace over HTTP names the configured default, a model nothing ran",
        expect="test_trace_embedding_model.py::test_a_traced_recall_writes_it_and_an_untraced_one_does_not_ask",
    ),
    Mutation(
        id="M115",
        tests=("tests/test_record_blocks_schema.py",),
        target="health — every expected schema object names a severity the runner counts (bug-452)",
        file="cpersona/checks.py",
        find='"severity": "warn",\n        "sql": "CREATE INDEX idx_record_blocks_axes "',
        replace='"severity": "warning",\n        "sql": "CREATE INDEX idx_record_blocks_axes "',
        breaks="a missing axis index raises KeyError out of run_health_checks, and a fix run is rolled back with it",
        expect="test_record_blocks_schema.py::test_a_missing_axis_index_is_counted_by_the_health_runner",
    ),
    Mutation(
        id="M116",
        tests=("tests/test_record_nodes_build.py",),
        target="overflow tree — one currency rule: a node set with a gap or an overlap is not current (bug-471)",
        file="cpersona/nodes.py",
        find="    if any(a[1] != b[0] for a, b in zip(spans, spans[1:])):\n        return False\n",
        replace="",
        breaks="a set the reader rejects is current to the builder and the missing_nodes check, so it is never rebuilt",
        expect="test_record_nodes_build.py::test_the_builder_the_check_and_the_reader_agree_on_a_current_set",
    ),
    Mutation(
        id="M117",
        tests=("tests/test_record_nodes_build.py",),
        target="overflow tree — one currency rule: a node without its embedding is not current (bug-471)",
        file="cpersona/nodes.py",
        find="return all(has_embedding and model in keys for",
        replace="return all(model in keys for",
        breaks="a set with a NULL embedding is kept by every path, and the reader ranks a node it cannot score",
        expect="test_record_nodes_build.py::test_the_builder_the_check_and_the_reader_agree_on_a_current_set",
    ),
    Mutation(
        id="M118",
        tests=("tests/test_associations_declare.py",),
        target="associations — retract refuses a JSON boolean as an id (bug-447)",
        file="cpersona/associations.py",
        find="return isinstance(value, int) and not isinstance(value, bool)",
        replace="return isinstance(value, int)",
        breaks="retract with `true` deletes relation 1 and a mention of entity 1, and reports nothing dropped",
        expect="test_associations_declare.py::test_retract_refuses_a_json_boolean_as_an_id",
    ),
    Mutation(
        id="M119",
        tests=("tests/test_associations_declare.py",),
        target="associations — an anchor is stored as the canonical ref of its record (bug-449)",
        file="cpersona/associations.py",
        find="                anchor = canonical_ref(anchor)\n",
        replace="                anchor = anchor.strip()\n",
        breaks="`mem:001` is stored as spelled: the walk never reaches it and the record's delete leaves it behind",
        expect="test_associations_declare.py::test_an_anchor_with_leading_zeros_is_stored_as_the_record_it_names",
    ),
    Mutation(
        id="M120",
        tests=("tests/test_associations_declare.py",),
        target="associations — retract compares a mention by its canonical ref (bug-449)",
        file="cpersona/associations.py",
        find="(entity_id, canonical_ref(ref), agent_id)",
        replace="(entity_id, ref.strip(), agent_id)",
        breaks="retracting `mem:01` leaves the stored `mem:1` mention in place and reports 0 retracted",
        expect="test_associations_declare.py::test_retract_finds_a_mention_by_any_spelling_of_its_ref",
    ),
    Mutation(
        id="M125",
        tests=("tests/test_blocks_quotation.py",),
        target="blocks — a qualifier behind a line break is still attached (bug-445)",
        file="cpersona/blocks.py",
        find="or _starts_with_qualifier(_following(text, spans, last + 1))",
        replace="or _starts_with_qualifier(text[spans[last + 1][0] : spans[last + 1][1]])",
        breaks="'Enable caching.' is quoted alone and complete when 'However, never in production.' follows on the next line",
        expect="test_blocks_quotation.py::test_a_qualifier_on_the_next_line_is_still_attached",
    ),
    Mutation(
        id="M121",
        tests=("tests/test_associations_reconstruct.py",),
        target="reconstruct — max_hops shares traverse's ceiling (bug-459)",
        file=_RC,
        find="bounds_max_hops = min(requested_hops, associations.TRAVERSE_MAX_HOPS)",
        replace="bounds_max_hops = requested_hops",
        breaks="a large max_hops walks the whole connected entity graph in one read",
        expect="test_associations_reconstruct.py::test_the_hops_share_traverse_s_ceiling",
    ),
    Mutation(
        id="M122",
        tests=("tests/test_associations_reconstruct.py",),
        target="reconstruct — a lowered hop bound keeps `bounds` in the compact response (bug-459)",
        file=_RC,
        find="if not bound_lowered and not any(",
        replace="if not any(",
        breaks="a call whose max_hops was lowered reads as served as asked",
        expect="test_associations_reconstruct.py::test_a_lowered_hop_bound_is_reported_even_when_nothing_else_is",
    ),
    Mutation(
        id="M123",
        tests=("tests/test_associations_declare.py",),
        target="declare_associations — annotated destructive, because retract deletes (bug-458)",
        file="cpersona/server.py",
        find="can do.\n    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,",
        replace="can do.\n    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,",
        breaks="hosts that gate approval on destructiveHint let retract delete relations and mentions unasked",
        expect="test_associations_declare.py::test_declare_associations_is_annotated_destructive",
    ),
    Mutation(
        id="M124",
        tests=("tests/test_reconstruct_filled_quote.py",),
        target="reconstruct — an episode head is measured in its stored summary (bug-456)",
        file=_RC,
        find="text = block_entry[0] if block_entry is not None else _stored_text(claim)",
        replace="text = block_entry[0] if block_entry is not None else claim.content",
        breaks="an episode's ranges and expand span point 10 characters right of the passage they name",
        expect="test_reconstruct_filled_quote.py::test_an_episode_is_quoted_in_the_text_get_contents_serves",
    ),
    Mutation(
        id="M126",
        tests=("tests/test_associations_traverse.py",),
        target="associations — an alias attaches only to an entity of the declaring scope (bug-448)",
        file="cpersona/associations.py",
        find="db, scope, name, declared_by, now, exact=carries_aliases",
        replace="db, scope, name, declared_by, now, exact=False",
        breaks="a project's alias hangs on the global entity and every other project reads it",
        expect="test_associations_traverse.py::test_a_project_s_alias_is_not_read_from_another_project",
    ),
    Mutation(
        id="M127",
        tests=("tests/test_associations_traverse.py",),
        target="associations — a declaration resolves over every scope its reader sees (bug-450)",
        file="cpersona/associations.py",
        find="    return [(p, c) for p in projects for c in channels]",
        replace="    return [(scope.project_id, scope.channel), (\"\", \"\")]",
        breaks="a (P, C) declaration registers a second entity under a name its reader already resolves",
        expect="test_associations_traverse.py::test_a_channel_declaration_reuses_its_project_s_entity",
    ),
    Mutation(
        id="M128",
        tests=("tests/test_blocks_retrieval.py",),
        target="blocks — the design states the per-record share the code applies (bug-455)",
        file="cpersona/blocks.py",
        find="BLOCK_PER_PARENT_CAP = 64\n",
        replace="BLOCK_PER_PARENT_CAP = 65\n",
        breaks="the design names a share the scan no longer applies, and nothing says so",
        expect="test_blocks_retrieval.py::test_the_design_states_the_share_the_code_applies",
    ),
    Mutation(
        id="M129",
        tests=("tests/test_blocks_retrieval.py",),
        target="blocks — a record's share is its first blocks in text order (bug-455)",
        file="cpersona/blocks.py",
        find="        \" ORDER BY parent_kind, parent_id, block_index\"\n",
        replace="        \" ORDER BY parent_kind, parent_id, block_index DESC\"\n",
        breaks="the scan keeps a long record's last blocks, which the design does not describe",
        expect="test_blocks_retrieval.py::test_a_record_s_share_is_its_first_blocks",
    ),
    Mutation(
        id="M132",
        tests=("tests/test_bug388_index_behind.py",),
        target="vector_index — how far behind counts the unembedded holes (bug-388)",
        file="cpersona/vector_index.py",
        find="    holes_ids = tuple(index.excluded_ids) + tuple(index.unembedded_ids)\n    holes = ",
        replace="    holes_ids = tuple(index.excluded_ids)\n    holes = ",
        breaks="a hole filled by check_health(fix=True) is read on every query and reported as nothing",
        expect="test_bug388_index_behind.py::test_an_unembedded_hole_filled_after_the_build_is_counted",
    ),
    Mutation(
        id="M133",
        tests=("tests/test_bug388_index_behind.py",),
        target="vector_index — how far behind counts the excluded holes (bug-388)",
        file="cpersona/vector_index.py",
        find="    holes_ids = tuple(index.excluded_ids) + tuple(index.unembedded_ids)\n    holes = ",
        replace="    holes_ids = tuple(index.unembedded_ids)\n    holes = ",
        breaks="a row with a non-canonical created_at is read on every query and reported as nothing",
        expect="test_bug388_index_behind.py::test_an_excluded_row_is_counted",
    ),
    Mutation(
        id="M134",
        tests=("tests/test_bug388_index_behind.py",),
        target="check_vector_index — the tail finding uses the shared definition of behind (bug-388)",
        file="cpersona/checks.py",
        find="if index.count and read_exactly > index.count * INDEX_TAIL_RATIO:",
        replace="if index.count and tail > index.count * INDEX_TAIL_RATIO:",
        breaks="the health check stays quiet while every query reads the filled holes",
        expect="test_bug388_index_behind.py::test_the_health_check_reports_filled_holes",
    ),
    Mutation(
        id="M130",
        tests=("tests/test_bug356_in_memory_snapshot.py",),
        target="read_snapshot — an in-memory database gets a private copy (bug-356)",
        file="cpersona/database.py",
        find="        async with _in_memory_snapshot() as snap:\n            yield snap\n        return",
        replace="        yield await get_db()\n        return",
        breaks="the scope reads the shared connection, so a commit landing mid-scope is visible to it",
        expect="test_bug356_in_memory_snapshot.py::test_a_commit_inside_the_scope_is_not_seen_by_it",
    ),
    Mutation(
        id="M131",
        tests=("tests/test_bug356_in_memory_snapshot.py",),
        target="read_snapshot — the in-memory copy is taken under the write seam (bug-356)",
        file="cpersona/database.py",
        find="        async with transaction(scope_stats_neutral=True) as db:\n            await _copy_database(db, snap)",
        replace="        db = await get_db()\n        if True:\n            await _copy_database(db, snap)",
        breaks="a snapshot taken while a writer is mid-transaction fails instead of waiting for its commit",
        expect="test_bug356_in_memory_snapshot.py::test_a_writers_uncommitted_work_is_not_copied",
    ),
    Mutation(
        id="M135",
        tests=("tests/test_bug356_in_memory_snapshot.py",),
        target="read_snapshot — the in-memory copy refuses to wait on an uncommitted write (bug-356)",
        file="cpersona/database.py",
        find="    await source.backup(target, progress=_refuse_to_wait)",
        replace="    await source.backup(target)",
        breaks="a write outside the seam makes the copy retry forever on the process's only connection",
        expect="test_bug356_in_memory_snapshot.py::test_a_copy_refuses_to_wait_on_an_uncommitted_write",
    ),
    Mutation(
        id="M136",
        tests=("tests/test_bug385_row_cap_marker.py",),
        target="list_memories — a listing the row cap cut says so (bug-385)",
        file="cpersona/admin_handlers.py",
        find="    read = wanted + 1 if limit > LIST_MEMORIES_MAX_ROWS else wanted",
        replace="    read = wanted",
        breaks="a capped listing is indistinguishable from one that reached the end of the data",
        expect="test_bug385_row_cap_marker.py::test_a_listing_the_cap_cut_carries_the_marker",
    ),
    Mutation(
        id="M137",
        tests=("tests/test_bug385_row_cap_marker.py",),
        target="list_memories — the probe row is read, never returned (bug-385)",
        file="cpersona/admin_handlers.py",
        find="    rows = rows[:wanted]\n    memories = []",
        replace="    memories = []",
        breaks="a capped listing returns one row more than its cap",
        expect="test_bug385_row_cap_marker.py::test_a_listing_the_cap_cut_carries_the_marker",
    ),
    Mutation(
        id="M138",
        tests=("tests/test_bug385_row_cap_marker.py",),
        target="list_episodes — a listing the row cap cut says so (bug-385)",
        file="cpersona/admin_handlers.py",
        find="    read = wanted + 1 if limit > LIST_EPISODES_MAX_ROWS else wanted",
        replace="    read = wanted",
        breaks="a capped episode listing is indistinguishable from a complete one",
        expect="test_bug385_row_cap_marker.py::test_the_episode_listing_carries_the_marker_too",
    ),
    Mutation(
        id="M139",
        tests=("tests/test_bug386_session_key_bound.py",),
        target="session_key — a key past the bound is refused at the tool boundary (bug-386)",
        file="cpersona/session.py",
        find="SESSION_KEY_MAX_CHARS = 256",
        replace="SESSION_KEY_MAX_CHARS = 1_000_000",
        breaks="a caller chooses how much memory each key holds in the process-global maps",
        expect="test_bug386_session_key_bound.py::test_a_key_past_the_bound_is_refused_at_the_boundary",
    ),
    Mutation(
        id="M140",
        tests=("tests/test_bug386_session_key_bound.py",),
        target="session_key — every schema that takes the key declares the bound (bug-386)",
        file="cpersona/server.py",
        find='''"and not a data filter. Forwarded to the candidate recall."\n                ),\n                "maxLength": SESSION_KEY_MAX_CHARS,''',
        replace='''"and not a data filter. Forwarded to the candidate recall."\n                ),''',
        breaks="reconstruct accepts a key of any length while every other tool refuses it",
        expect="test_bug386_session_key_bound.py::test_every_tool_that_takes_the_key_declares_the_bound",
    ),
    Mutation(
        id="M141",
        tests=("tests/test_bug329_index_chunks.py",),
        target="index phase 1 — a copied window is built a chunk at a time (bug-329)",
        file="cpersona/vector.py",
        find="        for lo, hi in _scan_chunk_bounds(len(merged_ids), chunk_rows)\n",
        replace="        for lo, hi in [(0, len(merged_ids))]\n",
        breaks="the copied window is held whole, so its memory grows with the scan window and the reach",
        expect="test_bug329_index_chunks.py::test_the_peak_does_not_grow_with_the_window",
    ),
    Mutation(
        id="M142",
        tests=("tests/test_bug329_index_chunks.py",),
        target="index phase 1 — the chunks are the ranges the SQL scan scores (bug-329)",
        file="cpersona/vector.py",
        find="        bounds[-1] = (bounds[-1][0], rows)",
        replace="        bounds.append((bounds[-1][1], rows))",
        breaks="a short remainder is scored alone, in a shape the SQL scan never uses",
        expect="test_bug329_index_chunks.py::test_the_bounds_are_the_ranges_the_scan_scores",
    ),
    Mutation(
        id='M143',
        tests=('tests/test_blocks_segment.py',),
        target='segment — a whitespace-only block is folded away (bug-444)',
        file='cpersona/blocks.py',
        find='    return _fold_blank(text, spans, node_cuts, max_chars)\n',
        replace='    return spans\n',
        breaks="'Remember this.\\n' divides into the sentence and a block holding only the break",
        expect='test_blocks_segment.py::test_no_block_is_only_whitespace',
    ),
    Mutation(
        id='M144',
        tests=('tests/test_blocks_segment.py',),
        target='segment — a forward fold never crosses a node end (bug-444)',
        file='cpersona/blocks.py',
        find='            if s.start not in node_cuts and s.end - pending.start <= max_chars:\n',
        replace='            if s.end - pending.start <= max_chars:\n',
        breaks='leading whitespace is folded across a node end, so a block spans two nodes',
        expect='test_blocks_segment.py::test_a_node_boundary_keeps_the_whitespace_block_it_bounds',
    ),
    Mutation(
        id='M145',
        tests=('tests/test_blocks_segment.py',),
        target='segment — a backward fold never crosses a node end (bug-444)',
        file='cpersona/blocks.py',
        find='        if last is not None and pending.start not in node_cuts and pending.end - last.start <= max_chars:\n',
        replace='        if last is not None and pending.end - last.start <= max_chars:\n',
        breaks='trailing whitespace is folded across a node end, so a block spans two nodes',
        expect='test_blocks_segment.py::test_a_node_boundary_keeps_the_whitespace_block_it_bounds',
    ),
    Mutation(
        id='M146',
        tests=('tests/test_blocks_segment.py',),
        target='segment — a forward fold stays inside the size limit (bug-444)',
        file='cpersona/blocks.py',
        find='            if s.start not in node_cuts and s.end - pending.start <= max_chars:\n',
        replace='            if s.start not in node_cuts:\n',
        breaks='a fold makes a block longer than max_chars, past what one embedding holds',
        expect='test_blocks_segment.py::test_a_fold_that_would_pass_the_limit_is_not_made',
    ),
    Mutation(
        id='M147',
        tests=('tests/test_blocks_segment.py',),
        target='segment — a backward fold stays inside the size limit (bug-444)',
        file='cpersona/blocks.py',
        find='        if last is not None and pending.start not in node_cuts and pending.end - last.start <= max_chars:\n',
        replace='        if last is not None and pending.start not in node_cuts:\n',
        breaks='a fold makes a block longer than max_chars, past what one embedding holds',
        expect='test_blocks_segment.py::test_a_fold_that_would_pass_the_limit_is_not_made',
    ),
    Mutation(
        id='M148',
        tests=('tests/test_blocks_segment.py',),
        target='segment — a closing fence stays with its code (bug-446)',
        file='cpersona/blocks.py',
        find='        ranges.append((marks[i], len(text) if line_end == -1 else line_end + 1))\n',
        replace='        ranges.append((marks[i], marks[i + 1]))\n',
        breaks='the break before the closing ``` is a cut, dividing the code from its fence',
        expect='test_blocks_segment.py::test_a_closing_fence_stays_with_its_code',
    ),
    Mutation(
        id='M149',
        tests=('tests/test_blocks_segment.py',),
        target='segment — a closer passes over an unclosed opening (bug-473)',
        file='cpersona/blocks.py',
        find='        for depth in range(len(pending) - 1, -1, -1):\n',
        replace='        for depth in range(len(pending) - 1, len(pending) - 2, -1):\n',
        breaks='one unclosed ( inside a quotation makes the quotation look unclosed, so it is cut inside',
        expect='test_blocks_segment.py::test_an_unclosed_bracket_inside_a_quotation_does_not_open_the_quotation',
    ),
    Mutation(
        id='M150',
        tests=('tests/test_blocks_rerank.py',),
        target='block build — the storage refusal is asked once per block (bug-497)',
        file='cpersona/blocks.py',
        find='            forms = quantise(v)\n',
        replace='            forms = quantise(v) if pack_bits(v) is not None else None\n',
        breaks='every block vector runs the per-element storage check twice',
        expect='test_blocks_rerank.py::test_a_build_asks_the_storage_refusal_once_per_block',
    ),
    Mutation(
        id='M151',
        tests=('tests/test_blocks_rerank.py',),
        target='block search — the fallback reads the distances the cut measured (bug-481)',
        file='cpersona/blocks.py',
        find='    return _collapse(*measured)\n',
        replace='    return _hamming(rows, query_bits)\n',
        breaks='the Hamming fallback measures the whole examined set a second time',
        expect='test_blocks_rerank.py::test_the_fallback_measures_the_examined_rows_once',
    ),
    Mutation(
        id='M152',
        tests=('tests/test_recall_cue.py',),
        target='time_cue — a unit that is not a string is refused (bug-443)',
        file='cpersona/cue.py',
        find='    if not isinstance(unit, str) or unit not in _UNITS:\n',
        replace='    if unit not in _UNITS:\n',
        breaks='a list unit raises TypeError, so recall and reconstruct raise instead of refusing',
        expect='test_recall_cue.py::test_a_unit_that_is_not_a_string_is_refused_not_raised',
    ),
    Mutation(
        id='M153',
        tests=('tests/test_recall_cue.py',),
        target='time_cue — the widened period is clipped to the representable range (bug-443)',
        file='cpersona/cue.py',
        find='    return _earlier(start, margin), _later(end, margin)\n',
        replace='    return start - margin, end + margin\n',
        breaks='a vague cue after year 1 overflows and the recall raises',
        expect='test_recall_cue.py::test_a_period_past_the_representable_range_is_clipped_not_raised',
    ),
    Mutation(
        id='M154',
        tests=('tests/test_recall_cue.py',),
        target='time_cue — an ago centre past the range is clipped (bug-443)',
        file='cpersona/cue.py',
        find='        except OverflowError:  # the product, or the point it names, is out of range\n            centre = _EARLIEST\n',
        replace='        except ZeroDivisionError:\n            centre = _EARLIEST\n',
        breaks='ago of 100000 months overflows and the recall raises',
        expect='test_recall_cue.py::test_a_period_past_the_representable_range_is_clipped_not_raised',
    ),
    Mutation(
        id='M155',
        tests=('tests/test_recall_cue.py',),
        target='time_cue — the last representable day as `before` is clipped (bug-443)',
        file='cpersona/cue.py',
        find='            return _later(day, timedelta(days=1)) if end_of_day else day\n',
        replace='            return day + timedelta(days=1) if end_of_day else day\n',
        breaks='before 9999-12-31 overflows in the parser and the recall raises',
        expect='test_recall_cue.py::test_a_period_past_the_representable_range_is_clipped_not_raised',
    ),
    Mutation(
        id='M157',
        tests=('tests/test_span_index.py',),
        target='confidence span — the oldest end is settled chronologically (bug-286)',
        file='cpersona/scope_stats.py',
        find='        lo_text = rows[0][0] if rows else lo_text\n',
        replace='        pass\n',
        breaks='a scope holding two offsets reports a text minimum that is not its oldest row',
        expect='test_span_index.py::test_a_span_across_offsets_names_the_oldest_and_newest_rows',
    ),
    Mutation(
        id='M158',
        tests=('tests/test_span_index.py',),
        target='confidence span — the newest end is settled chronologically (bug-286)',
        file='cpersona/scope_stats.py',
        find='        hi_text = rows[0][0] if rows else hi_text\n',
        replace='        pass\n',
        breaks='a scope holding two offsets reports a text maximum that is not its newest row',
        expect='test_span_index.py::test_a_span_across_offsets_names_the_oldest_and_newest_rows',
    ),
    Mutation(
        id='M159',
        tests=('tests/test_span_index.py',),
        target="confidence span — the oldest end's window reaches two local days (bug-286)",
        file='cpersona/scope_stats.py',
        find='edge="timestamp < date(substr(?, 1, 10), \'+3 days\')"',
        replace='edge="timestamp < date(substr(?, 1, 10), \'+2 days\')"',
        breaks='an oldest row two local dates past the text minimum is outside the window',
        expect='test_span_index.py::test_a_true_end_two_local_days_from_the_text_end_is_found',
    ),
    Mutation(
        id='M160',
        tests=('tests/test_span_index.py',),
        target="confidence span — the newest end's window reaches two local days (bug-286)",
        file='cpersona/scope_stats.py',
        find='edge="timestamp >= date(substr(?, 1, 10), \'-2 days\')"',
        replace='edge="timestamp >= date(substr(?, 1, 10), \'-1 days\')"',
        breaks='a newest row two local dates before the text maximum is outside the window',
        expect='test_span_index.py::test_a_true_end_two_local_days_from_the_text_end_is_found',
    ),
    Mutation(
        id='M161',
        tests=('tests/test_associations_traverse.py',),
        target='traverse — mentions are read as refs, not text (bug-501)',
        file='cpersona/associations.py',
        find='source_id=source_id, excluded=[], limit=limit, count=True, refs_only=True,',
        replace='source_id=source_id, excluded=[], limit=limit, count=True,',
        breaks="every mentioning record's content is read only for its ref to be kept",
        expect='test_associations_traverse.py::test_traverse_reads_the_refs_and_not_the_text',
    ),
    Mutation(
        id='M162',
        tests=('tests/test_associations_traverse.py',),
        target='traverse — a read that came back short counts itself (bug-501)',
        file='cpersona/associations.py',
        find='        if len(mem_rows) >= limit:\n',
        replace='        if True:\n',
        breaks='a COUNT query runs for every entity however few mentions it has',
        expect='test_associations_traverse.py::test_traverse_counts_only_when_a_read_came_back_full',
    ),
    Mutation(
        id='M163',
        tests=('tests/test_associations_traverse.py',),
        target='traverse — a full read is still counted (bug-501)',
        file='cpersona/associations.py',
        find='            total = (await db.execute_fetchall(f"SELECT COUNT(*) {mem_where}", mem_params))[0][0]\n',
        replace='            pass\n',
        breaks='an entity with more mentions than the limit reports none omitted',
        expect='test_associations_traverse.py::test_traverse_counts_only_when_a_read_came_back_full',
    ),
    Mutation(
        id='M164',
        tests=('tests/test_associations_declare.py',),
        target='declare_associations — a non-object retract is refused by the schema (bug-475)',
        file='cpersona/server.py',
        find='            "retract": {\n                "type": "object",\n',
        replace='            "retract": {\n',
        breaks='a list retract reaches the handler as {} and is silently ignored',
        expect='test_associations_declare.py::test_a_retract_that_is_not_an_object_is_refused_at_the_boundary',
    ),
    Mutation(
        id='M165',
        tests=('tests/test_associations_declare.py',),
        target='declare_associations — its object is described in its own words (bug-489)',
        file='cpersona/server.py',
        find='            "associations": _DECLARE_ASSOCIATIONS_PROPERTY,\n',
        replace='            "associations": _ASSOCIATIONS_PROPERTY,\n',
        breaks='declare_associations tells callers of a stored memory and of associations.dropped',
        expect='test_associations_declare.py::test_declare_associations_describes_its_own_object',
    ),
    Mutation(
        id='M166',
        tests=('tests/test_docs_applies_to_banner.py',),
        target='version selector gate — a row must lead to a page (bug-434)',
        file='scripts/check-version-selector.py',
        find='            if not target.is_file():\n',
        replace='            if False:\n',
        breaks='a selector row linking to a tree the assembler never built passes the gate',
        expect='test_docs_applies_to_banner.py::test_a_selector_row_whose_tree_was_not_built_is_reported',
    ),
    Mutation(
        id='M167',
        tests=('tests/test_recall_cue.py',),
        target='recall_count bump — seats and reservations earn no credit (bug-453)',
        file='cpersona/memory_handlers.py',
        find='        returned_ids = credited_ids\n',
        replace='        returned_ids = _memory_ids(results)\n',
        breaks="a seated or reserved row's count rises until it passes the gate on unrelated queries",
        expect='test_recall_cue.py::test_a_seated_row_earns_no_recall_count',
    ),
    Mutation(
        id='M168',
        tests=('tests/test_blocks_retrieval.py',),
        target='block reservation — the filters run before the hits are counted (bug-454)',
        file='cpersona/builtin_providers.py',
        find='            hits,\n',
        replace='            hits[: limit + blocks.BLOCK_RESERVATION],\n',
        breaks='hits the source filter drops use up the window and the reservation stays empty',
        expect='test_blocks_retrieval.py::test_hits_the_filters_drop_do_not_use_up_the_reservation',
    ),
    Mutation(
        id='M169',
        tests=('tests/test_blocks_retrieval.py',),
        target='block reservation — hydration stops at its bound (bug-454)',
        file='cpersona/memory_handlers.py',
        find='    looked_at = hits[:BLOCK_HYDRATE_CAP]\n',
        replace='    looked_at = hits\n',
        breaks='a scope whose filters drop almost everything hydrates every hit, a query per page',
        expect='test_blocks_retrieval.py::test_hydrating_the_hits_stops_at_its_bound',
    ),
    Mutation(
        id='M170',
        tests=('tests/test_bug155_cosine_backfill.py',),
        target='shown cosine — the returned rows get one under fusion (bug-460)',
        file='cpersona/memory_handlers.py',
        find='                await _backfill_cosines(db, results, query, project_id, channel, into="_shown_cosine")\n',
        replace='                pass\n',
        breaks="a lexical-only row shows the cosine-less branch's confidence, above the real vector hit",
        expect='test_bug155_cosine_backfill.py::test_bug460_a_lexical_row_is_shown_on_its_real_cosine_under_fusion',
    ),
    Mutation(
        id='M171',
        tests=('tests/test_bug155_cosine_backfill.py',),
        target='shown cosine — kept apart from what the gate signal reads (bug-460)',
        file='cpersona/memory_handlers.py',
        find='                await _backfill_cosines(db, results, query, project_id, channel, into="_shown_cosine")\n',
        replace='                await _backfill_cosines(db, results, query, project_id, channel)\n',
        breaks='turning confidence on changes the gate signal a row reports',
        expect='test_bug155_cosine_backfill.py::test_bug460_the_shown_cosine_moves_no_gate_order_or_signal',
    ),
    Mutation(
        id='M172',
        tests=('tests/test_gate_remediation.py',),
        target='calibration fingerprint — names a non-default far weight (bug-470)',
        file='cpersona/memory_handlers.py',
        find='    if PRIOR_FAR_WEIGHT == 1.0:\n',
        replace='    if True:\n',
        breaks='a gate calibrated at one far weight is restored at another',
        expect='test_gate_remediation.py::test_a_gate_calibrated_at_another_far_weight_is_not_restored',
    ),
    Mutation(
        id='M173',
        tests=('tests/test_gate_remediation.py',),
        target='startup guard — compares the sidecar with the live fingerprint (bug-470)',
        file='cpersona/admin_handlers.py',
        find='    scoring_stale = state is not None and state.get("scoring_version") != _live_fingerprint()\n',
        replace='    scoring_stale = state is not None and state.get("scoring_version") is None\n',
        breaks='a sidecar stamped at another far weight is restored at boot',
        expect='test_gate_remediation.py::test_a_gate_calibrated_at_another_far_weight_is_not_restored',
    ),
    Mutation(
        id='M174',
        tests=('tests/test_gate_remediation.py',),
        target='deep check — compares the sidecar with the live fingerprint (bug-470)',
        file='cpersona/checks.py',
        find='    runtime_scoring_version = calibration_fingerprint()\n',
        replace='    runtime_scoring_version = sidecar_scoring_version\n',
        breaks='deep_check reports a sidecar stamped at another far weight as current',
        expect='test_gate_remediation.py::test_a_gate_calibrated_at_another_far_weight_is_not_restored',
    ),
    Mutation(
        id='M175',
        tests=('tests/test_recall_cue.py',),
        target="confidence — a seat's recall history is read (bug-472)",
        file='cpersona/memory_handlers.py',
        find='            unread = [i for i in _memory_ids(results) if i not in recall_counts]\n',
        replace='            unread = []\n',
        breaks='a cue-only seat shows the confidence of a row never recalled',
        expect='test_recall_cue.py::test_a_seated_row_shows_the_confidence_of_its_own_history',
    ),
    Mutation(
        id='M176',
        tests=('tests/test_260a7_prior_function.py',),
        target="calibration — follows the gate's own predicate (bug-482)",
        file='cpersona/admin_handlers.py',
        find='    if _confidence_orders():\n',
        replace='    if config.CONFIDENCE_ENABLED and config.CONFIDENCE_ORDERING == "legacy":\n',
        breaks='calibration measures a signal the gate does not key on when the two copies differ',
        expect='test_260a7_prior_function.py::test_calibration_follows_the_gates_own_predicate',
    ),
    Mutation(
        id='M177',
        tests=('tests/test_recall_cue.py',),
        target='recall — no reached set without a cue or far seats (bug-492)',
        file='cpersona/memory_handlers.py',
        find='        reached = {_row_rid(r) for r in results} if time_cue is not None or far_on else set()\n',
        replace='        reached = {_row_rid(r) for r in results}\n',
        breaks="every recall builds a set only the cue's seats read",
        expect='test_recall_cue.py::test_a_plain_recall_builds_no_seat_bookkeeping',
    ),
    Mutation(
        id='M178',
        tests=('tests/test_recall_cue.py',),
        target='recall — no admitted set without a cue or far seats (bug-492)',
        file='cpersona/memory_handlers.py',
        find='    admitted_rids = {_rid_of(r) for r in results} if cue_note is not None or far_hits else set()\n',
        replace='    admitted_rids = {_rid_of(r) for r in results}\n',
        breaks="every recall builds a set only the cue's seats read",
        expect='test_recall_cue.py::test_a_plain_recall_builds_no_seat_bookkeeping',
    ),
    Mutation(
        id='M179',
        tests=('tests/test_generation_key.py',),
        target='node build — asks the backend what it is before stamping (bug-461)',
        file='cpersona/nodes.py',
        find='    await generation.refresh()\n    async with connection() as db:\n        text = await _parent_text(db, kind, parent_id)\n',
        replace='    async with connection() as db:\n        text = await _parent_text(db, kind, parent_id)\n',
        breaks='a backend redeployed under the same URL leaves node builds stamping the old fingerprint',
        expect='test_generation_key.py::test_a_node_build_stamps_the_backend_as_it_is_now',
    ),
    Mutation(
        id='M180',
        tests=('tests/test_generation_key.py',),
        target='block build — asks the backend what it is before stamping (bug-461)',
        file='cpersona/blocks.py',
        find='    # identity, not the one learned at boot (see nodes.build_nodes).\n    await generation.refresh()\n',
        replace='    # identity, not the one learned at boot (see nodes.build_nodes).\n',
        breaks='a backend redeployed under the same URL leaves block builds stamping the old fingerprint',
        expect='test_generation_key.py::test_a_block_build_stamps_the_backend_as_it_is_now',
    ),
    Mutation(
        id='M181',
        tests=('tests/test_generation_key.py',),
        target='node repair — judges currency by the backend as it is now (bug-461)',
        file='cpersona/checks.py',
        find="    await generation.refresh()  # bug-461: judge currency by the backend's current identity\n",
        replace='',
        breaks='nodes built under a replaced backend read as current and are never rebuilt',
        expect='test_generation_key.py::test_a_health_repair_judges_by_the_backend_as_it_is_now',
    ),
    Mutation(
        id='M182',
        tests=('tests/test_record_nodes_build.py',),
        target='token report — only an http EmbeddingClient has one (bug-462)',
        file='cpersona/nodes.py',
        find='    return not isinstance(client, EmbeddingClient) or client.mode == "http"\n',
        replace='    return True\n',
        breaks="api mode reads every record's text for a token report that cannot come",
        expect='test_record_nodes_build.py::test_an_api_mode_health_check_reads_no_record_text',
    ),
    Mutation(
        id='M183',
        tests=('tests/test_task_queue.py',),
        target='queue — a failing build does not hold other work (bug-463)',
        file='cpersona/tasks.py',
        find='                    elif task_type in derived and await self._other_work_waits(task_id, derived):\n',
        replace='                    elif False:\n',
        breaks='a profile update waits behind every retry of a failing build',
        expect='test_task_queue.py::test_a_failing_build_does_not_hold_a_profile_update',
    ),
    Mutation(
        id='M184',
        tests=('tests/test_task_queue.py',),
        target='queue — builds behind only builds keep the delay (bug-463)',
        file='cpersona/tasks.py',
        find='                f"SELECT 1 FROM pending_memory_tasks WHERE id != ? AND task_type NOT IN ({marks})"\n',
        replace='                f"SELECT 1 FROM pending_memory_tasks WHERE id != ? AND task_type IN ({marks})"\n',
        breaks='failing builds rotate among themselves and spend their retries in a second',
        expect='test_task_queue.py::test_builds_waiting_only_behind_builds_keep_the_delay',
    ),
    Mutation(
        id='M185',
        tests=('tests/test_task_queue.py',),
        target='queue — a moved build keeps its session (bug-463)',
        file='cpersona/tasks.py',
        find='            self._remember_session(new_id, key)\n',
        replace='            pass\n',
        breaks="a moved build falls into the shared bucket, and another session's pause decides it",
        expect='test_task_queue.py::test_a_moved_build_keeps_its_session_and_its_place_is_new',
    ),
    Mutation(
        id='M186',
        tests=('tests/test_blocks_backfill.py',),
        target='block sweep — a one-block record costs the run nothing (bug-498)',
        file='cpersona/blocks.py',
        find='                    if len(segment(text, node_bounds=node_bounds)) <= 1:\n',
        replace='                    if False:\n',
        breaks='one-block records use up the record cap and stop the sweep before the records that need blocks',
        expect='test_blocks_backfill.py::test_records_that_need_no_blocks_do_not_use_up_the_record_cap',
    ),
    Mutation(
        id='M187',
        tests=('tests/test_blocks_backfill.py',),
        target="block sweep — node ends come from the page's one read (bug-498)",
        file='cpersona/blocks.py',
        find='    try:\n        prepared = await prepare_blocks(kind, row_id, text, node_bounds)\n',
        replace='    async with connection() as db:\n        node_bounds = await _node_bounds(db, kind, row_id)\n    try:\n        prepared = await prepare_blocks(kind, row_id, text, node_bounds)\n',
        breaks='every record the sweep builds opens a connection for its node ends',
        expect='test_blocks_backfill.py::test_a_record_that_needs_no_blocks_opens_no_connection_of_its_own',
    ),
    Mutation(
        id='M188',
        tests=('tests/test_record_nodes_build.py',),
        target='divide — after the first, a bounded prefix is measured (bug-499)',
        file='cpersona/nodes.py',
        find='        probe = rest if reach is None else rest[: 2 * reach]\n',
        replace='        probe = rest\n',
        breaks='an n-node record posts O(n^2) characters to the token report',
        expect='test_record_nodes_build.py::test_only_the_first_measurement_reads_the_whole_text',
    ),
    Mutation(
        id='M189',
        tests=('tests/test_record_nodes_build.py',),
        target='divide — a prefix too short for the window is widened (bug-499)',
        file='cpersona/nodes.py',
        find='            if len(probe) < len(rest):\n',
        replace='            if False:\n',
        breaks="a prefix the window does not close in is taken as the record's last span",
        expect='test_record_nodes_build.py::test_a_prefix_the_window_does_not_close_in_is_widened',
    ),
    Mutation(
        id='M190',
        tests=('tests/test_reconstruct_filled_quote.py',),
        target='reconstruct budget — the floor is one head quote (bug-457)',
        file='cpersona/reconstruct.py',
        find='    floor = max(_head_cap(), 1)\n',
        replace='    floor = max(config.RECALL_PREVIEW_CHARS, 1)\n',
        breaks='a budget between the preview tier and a head quote returns more than it reports',
        expect='test_reconstruct_filled_quote.py::test_a_budget_below_one_head_quote_is_raised_to_it',
    ),
    Mutation(
        id='M191',
        tests=('tests/test_reconstruct_block_reservation.py',),
        target='reconstruct shortfall — the budget is blamed only for a window cut (bug-464)',
        file='cpersona/reconstruct.py',
        find='        window_cut = len(items) - held_returned < len(window)\n',
        replace='        window_cut = budget_cut\n',
        breaks='a window short of candidates is reported as cut by the budget',
        expect='test_reconstruct_block_reservation.py::test_a_short_window_is_blamed_on_the_budget_only_when_it_cut_the_window',
    ),
    Mutation(
        id='M192',
        tests=('tests/test_reconstruct_block_reservation.py',),
        target='reconstruct bounds — top_k reached counts rows inside the limit (bug-465)',
        file='cpersona/reconstruct.py',
        find='    if within_limit >= effective_top_k:\n',
        replace='    if total >= effective_top_k:\n',
        breaks='held rows make a short retrieval read as one that reached top_k',
        expect='test_reconstruct_block_reservation.py::test_reserved_rows_do_not_make_a_short_retrieval_look_full',
    ),
    Mutation(
        id='M193',
        tests=('tests/test_blocks_quotation.py',),
        target='block quote — a cut hands over its governing range (bug-466)',
        file='cpersona/reconstruct.py',
        find='                    "block": [starts.index(span_start), ends.index(span_end)],\n',
        replace='                    "block": quote["block"]["index"],\n',
        breaks='following expand returns the best block, not the rest of what was cut',
        expect='test_blocks_quotation.py::test_a_cut_quote_says_it_is_no_longer_whole',
    ),
    Mutation(
        id='M194',
        tests=('tests/test_blocks_rerank.py',),
        target='block currency — a set may not cross a node end (bug-468)',
        file='cpersona/blocks.py',
        find='    return all(end in ends for end in node_ends if 0 < end < text_len)\n',
        replace='    return True\n',
        breaks='a block set built before its nodes landed stays current across a node end',
        expect='test_blocks_rerank.py::test_a_set_that_crosses_a_node_end_is_not_current',
    ),
    Mutation(
        id='M195',
        tests=('tests/test_recall_excerpt.py',),
        target='recall excerpt — block sets are read only with retrieval on (bug-469)',
        file='cpersona/excerpts.py',
        find='    block_sets = await reconstruct._current_block_sets(agent_id, parsed) if blocks.retrieval_enabled() else {}\n',
        replace='    block_sets = await reconstruct._current_block_sets(agent_id, parsed)\n',
        breaks='with retrieval off the excerpt reads the index the quotation does not, and the two disagree',
        expect='test_recall_excerpt.py::test_a_block_set_is_not_read_while_block_retrieval_is_off',
    ),
    Mutation(
        id='M196',
        tests=('tests/test_reconstruct_filled_quote.py',),
        target='reconstruct — the candidate recall uses its provider set (bug-474)',
        file='cpersona/reconstruct.py',
        find='        providers_=p,\n',
        replace='',
        breaks='a set installed between the two reads builds the pool with one set and the items with another',
        expect='test_reconstruct_filled_quote.py::test_one_provider_set_and_one_reading_of_the_cue',
    ),
    Mutation(
        id='M197',
        tests=('tests/test_reconstruct_filled_quote.py',),
        target='reconstruct — the cue is read once (bug-474)',
        file='cpersona/reconstruct.py',
        find='        **({"time_cue": parsed_cue} if parsed_cue is not None else {}),\n',
        replace='        **({"time_cue": time_cue} if time_cue else {}),\n',
        breaks='the time cue is parsed by reconstruct and again by its recall',
        expect='test_reconstruct_filled_quote.py::test_one_provider_set_and_one_reading_of_the_cue',
    ),
    Mutation(
        id='M198',
        tests=('tests/test_reconstruct_filled_quote.py',),
        target='block currency — every re-rank vector is required (bug-479)',
        file='cpersona/blocks.py',
        find='    if not all(has_vector and model in keys for _, _, has_vector, model in rows):\n',
        replace='    if not all(model in keys for _, _, has_vector, model in rows):\n',
        breaks='a set the sweep rebuilds for a missing vector is still quoted from',
        expect='test_reconstruct_filled_quote.py::test_a_block_set_missing_a_vector_is_not_quoted_from',
    ),
    Mutation(
        id='M199',
        tests=('tests/test_blocks_rerank.py',),
        target='block currency — the set is contiguous (bug-479)',
        file='cpersona/blocks.py',
        find='    if any(a[1] != b[0] for a, b in zip(rows, rows[1:])):\n        return False\n    if not all(has_vector',
        replace='    if not all(has_vector',
        breaks='a set with a gap is current, and offsets land in text it does not cover',
        expect='test_blocks_rerank.py::test_the_rule_reads_the_node_ends_it_is_given',
    ),
    Mutation(
        id='M200',
        tests=('tests/test_reconstruct_filled_quote.py',),
        target='excerpts — an episode is measured in its summary on both sides (bug-480)',
        file='cpersona/excerpts.py',
        find='    known = {ref: stored_text(ref, shown) for ref, shown in (texts or {}).items() if shown}\n',
        replace='    known = {ref: shown for ref, shown in (texts or {}).items() if shown}\n',
        breaks="an episode's recall excerpt is taken from its display string and differs from its head quote",
        expect='test_reconstruct_filled_quote.py::test_an_episode_head_quote_is_the_recall_excerpt_of_the_same_episode',
    ),
    Mutation(
        id='M201',
        tests=('tests/test_reconstruct_filled_quote.py',),
        target='excerpts — the texts recall rows carry are not read again (bug-491)',
        file='cpersona/excerpts.py',
        find='    known = {ref: stored_text(ref, shown) for ref, shown in (texts or {}).items() if shown}\n',
        replace='    known = {}\n',
        breaks='every record without a current block set is read again from the table',
        expect='test_reconstruct_filled_quote.py::test_excerpts_read_no_record_the_recall_row_already_carries',
    ),
    Mutation(
        id='M202',
        tests=('tests/test_reconstruct_filled_quote.py',),
        target="reconstruct — the recall's query vector is reused (bug-494)",
        file='cpersona/reconstruct.py',
        find='        query_vec = np.asarray(recalled_vec[0], dtype=np.float32) if recalled_vec else await _query_vector(query)\n',
        replace='        query_vec = await _query_vector(query)\n',
        breaks='every reconstruction embeds its query twice',
        expect='test_reconstruct_filled_quote.py::test_a_reconstruction_embeds_its_query_once',
    ),
    Mutation(
        id='M203',
        tests=('tests/test_reconstruct_filled_quote.py',),
        target='reconstruct — no node set for a filled head (bug-495)',
        file='cpersona/reconstruct.py',
        find='    node_claims = [c for _, head, others in entries_claims for c in ((head,) if head_cap <= 0 else ()) + tuple(others)]\n',
        replace='    node_claims = all_claims\n',
        breaks="every head's node set and embeddings are read although a filled quote never uses them",
        expect='test_reconstruct_filled_quote.py::test_a_head_quoted_by_filling_reads_no_node_set',
    ),
    Mutation(
        id='M204',
        tests=('tests/test_blocks_backfill.py',),
        target='block write — the one parent read still checks the text (bug-490)',
        file='cpersona/blocks.py',
        find='    if parent is None or parent[0] != prepared.text:\n',
        replace='    if parent is None:\n',
        breaks='a set divided from text the record no longer holds is written',
        expect='test_blocks_backfill.py::test_a_record_that_moved_under_the_build_is_not_counted_as_built',
    ),
    Mutation(
        id='M205',
        tests=('tests/test_config_parsing.py',),
        target='config — block reach is on by default (2.6.0)',
        file='cpersona/config.py',
        find='BLOCK_BUILD_ENABLED = os.environ.get("CPERSONA_BLOCK_BUILD_ENABLED", "true").lower() == "true"\n',
        replace='BLOCK_BUILD_ENABLED = os.environ.get("CPERSONA_BLOCK_BUILD_ENABLED", "false").lower() == "true"\n',
        breaks='a deployment that sets nothing ships without block reach',
        expect='test_config_parsing.py::test_block_reach_is_on_by_default',
    ),
    Mutation(
        id='M206',
        tests=('tests/test_config_parsing.py',),
        target='config — the reader follows construction when it is not set',
        file='cpersona/config.py',
        find='        "CPERSONA_BLOCK_RETRIEVAL_ENABLED", "true" if BLOCK_BUILD_ENABLED else "false"\n',
        replace='        "CPERSONA_BLOCK_RETRIEVAL_ENABLED", "true"\n',
        breaks='turning construction off alone becomes a startup error',
        expect='test_config_parsing.py::test_turning_construction_off_alone_turns_the_reader_off_too',
    ),
    Mutation(
        id='M207',
        tests=('tests/test_reconstruct_v1.py',),
        target='config — reconstruct returns 10 items when the caller omits count (2.6.0)',
        file='cpersona/config.py',
        find='    else min(10, RECONSTRUCT_MAX_COUNT)\n',
        replace='    else min(1, RECONSTRUCT_MAX_COUNT)\n',
        breaks='a caller that omits count gets one item, not the configuration that was measured',
        expect='test_reconstruct_v1.py::test_unconfigured_count_policy_is_the_measured_default',
    ),
    Mutation(
        id='M208',
        tests=('tests/test_reconstruct_v1.py',),
        target='config — an unset default count follows a lowered maximum',
        file='cpersona/config.py',
        find='    else min(10, RECONSTRUCT_MAX_COUNT)\n',
        replace='    else 10\n',
        breaks='lowering only the maximum becomes a startup error',
        expect='test_reconstruct_v1.py::test_an_unset_default_follows_a_lowered_maximum',
    ),
    Mutation(
        id='M209',
        tests=('tests/test_superauditor_findings.py',),
        target='get_session_findings — the response names the SuperAuditor version it conforms to',
        file='cpersona/maintenance_handlers.py',
        find='"superauditor": findings_seam.SUPERAUDITOR_VERSION}',
        replace='}',
        breaks='a consumer reads a v1.1 response as v1 and cannot tell which rules apply',
        expect='test_superauditor_findings.py::test_the_response_states_the_standard_version_it_conforms_to',
    ),    Mutation(
        id='M210',
        tests=('tests/test_vector_index_rebuild_while_loaded.py',),
        target='vector_index — the Windows read path reads each array at its own offset (bug-503)',
        file='cpersona/vector_index.py',
        find='        return np.fromfile(src, dtype=dtype, count=items, offset=offset).reshape(shape)\n',
        replace='        return np.fromfile(src, dtype=dtype, count=items, offset=0).reshape(shape)\n',
        breaks='on Windows every array is read from the start of the file: the index loads garbage',
        expect='test_vector_index_rebuild_while_loaded.py::test_read_mode_loads_the_same_arrays_and_maps_nothing',
    ),
    Mutation(
        id='M211',
        tests=('tests/test_coverage_ledger.py',),
        target='recall trace — the coverage ledger reads each returned record in full, not its preview',
        file='cpersona/memory_handlers.py',
        find='cov = await coverage.for_refs(agent_id, query, [m.get("ref") for m in result.get("messages") or []])',
        replace='cov = coverage.ledger(query, [(m.get("ref"), m.get("content") or "") for m in result.get("messages") or []])',
        breaks='a part past the preview reads as uncovered, so the ledger reports gaps the records do not have',
        expect='test_coverage_ledger.py::test_a_traced_recall_records_the_ledger_over_the_returned_records_in_full',
    ),
    Mutation(
        id='M212',
        tests=('tests/test_coverage_ledger.py',),
        target="coverage ledger — parts are spans and kinds, never the question's words",
        file='cpersona/coverage.py',
        find='"parts": [{"span": [s, e], "kind": kind} for s, e, kind in kept],',
        replace='"parts": [{"span": [s, e], "kind": kind, "word": q[s:e]} for s, e, kind in kept],',
        breaks='the trace carries text, which its contract (references, ranks, scores and reasons only) rules out',
        expect='test_coverage_ledger.py::test_the_ledger_carries_no_text',
    ),
    Mutation(
        id='M213',
        tests=('tests/test_coverage_ledger.py',),
        target='coverage ledger — only the records named cover a part',
        file='cpersona/coverage.py',
        find='return ledger(query, [(ref, texts[ref]) for ref in ordered if ref in texts])',
        replace='return ledger(query, list(texts.items()) + [("all", " ".join(texts.values()))])',
        breaks='a record that was not returned covers a part, so a gap the answer has reads as covered',
        expect='test_coverage_ledger.py::test_a_record_that_was_not_returned_covers_nothing',
    ),
    Mutation(
        id='M214',
        tests=('tests/test_coverage_ledger.py',),
        target="coverage ledger — another agent's record is not read",
        file='cpersona/coverage.py',
        find='WHERE agent_id = ? AND id IN',
        replace='WHERE (agent_id = ? OR 1) AND id IN',
        breaks="a ref naming another agent's row reads that row's text",
        expect='test_coverage_ledger.py::test_another_agents_record_is_not_read',
    ),
    Mutation(
        id='M215',
        tests=('tests/test_coverage_ledger.py',),
        target='coverage ledger — a run of kanji is a part',
        file='cpersona/coverage.py',
        find='(?P<kanji>[\\u4e00-\\u9fff\\u3005]{2,})',
        replace='(?P<kanji>(?!))',
        breaks="kanji words drop out of the ledger, so a Japanese question's content parts go unrecorded",
        expect='test_coverage_ledger.py::test_parts_are_words_cut_by_script',
    ),
    Mutation(
        id='M216',
        tests=('tests/test_recall_cue.py',),
        target="time cue — an empty query lists the period's newest records by their own time",
        file='cpersona/memory_handlers.py',
        find='"datetime(timestamp) DESC, id ASC" if window is not None',
        replace='"created_at DESC" if window is not None',
        breaks="the period's rows are ordered by when they were stored, so an import decides which of them the cue's seats hold",
        expect='test_recall_cue.py::test_an_empty_query_orders_the_period_by_the_records_time_not_by_when_they_were_stored',
    ),
    Mutation(
        id='M217',
        tests=('tests/test_recall_cue.py',),
        target='time cue — an empty query without a period keeps the storage order',
        file='cpersona/memory_handlers.py',
        find='if window is not None else "created_at DESC"',
        replace='if True else "created_at DESC"',
        breaks="the ordinary empty-query recall stops listing the most recently stored records first",
        expect='test_recall_cue.py::test_the_keyword_search_without_a_period_still_lists_the_most_recently_stored_first',
    ),
    Mutation(
        id='M218',
        tests=('tests/test_coarse_index.py',),
        target='coarse index — a record is quantised by the rule block reach stores',
        file='cpersona/coarse_index.py',
        find='return np.packbits(np.asarray(vectors, dtype=np.float32) > 0, axis=-1)',
        replace='return np.packbits(np.asarray(vectors, dtype=np.float32) >= 0, axis=-1)',
        breaks="a zero component sets a record's bit and clears a block's, so a Hamming distance between them measures two rules",
        expect='test_coarse_index.py::test_sign_bits_are_the_bits_block_reach_stores',
    ),
    Mutation(
        id='M219',
        tests=('tests/test_coarse_index.py',),
        target="coarse index — a row's time is the record's own time as SQLite reads it",
        file='cpersona/coarse_index.py',
        find='TIMESTAMP_EXPR = "datetime(timestamp)"',
        replace='TIMESTAMP_EXPR = "datetime(created_at)"',
        breaks="a period is compared against when the row was stored, so an import decides which records a cue's period holds",
        expect="test_coarse_index.py::test_each_rows_time_is_sqlites_own_reading_of_it",
    ),
    Mutation(
        id='M220',
        tests=('tests/test_coarse_index.py',),
        target='coarse index — a purge removes it with the rows it held',
        file='cpersona/admin_handlers.py',
        find='    stale += [(table, coarse_index.index_path(table)) for table in coarse_index.COARSE_TABLES]\n',
        replace='',
        breaks="a purged agent's identifiers and a one-bit form of its vectors stay on disk after a delete that reported ok",
        expect='test_coarse_index.py::test_a_purge_removes_the_coarse_index_with_the_rows_it_held',
    ),
    Mutation(
        id='M221',
        tests=('tests/test_coarse_index.py',),
        target='coarse index — an unreadable time is inside no period',
        file='cpersona/coarse_index.py',
        find='UNREADABLE_TIMESTAMP = b"\\0" * TIMESTAMP_WIDTH',
        replace='UNREADABLE_TIMESTAMP = b"0" * TIMESTAMP_WIDTH',
        breaks='a record whose time SQLite cannot read falls inside wide periods in the file and in none in SQL',
        expect='test_coarse_index.py::test_a_period_in_the_file_selects_the_rows_sql_selects',
    ),
    Mutation(
        id='M222',
        tests=('tests/test_coarse_search.py',),
        target='coarse search — a tie at the cut-off distance goes to the earlier scan position',
        file='cpersona/coarse_search.py',
        find='at = at[np.argpartition(positions[at], need - 1)[:need]] if need > 0 else at[:0]',
        replace='at = at[np.argpartition(-positions[at], need - 1)[:need]] if need > 0 else at[:0]',
        breaks='the rows kept at the cut-off are the latest of the tie, so K-prime holds older records than the order the answer states',
        expect='test_coarse_search.py::test_top_k_by_counting_is_the_prefix_of_the_full_sort',
    ),
    Mutation(
        id='M223',
        tests=('tests/test_coarse_search.py',),
        target="coarse search — the index's period includes its lower bound, as the SQL does",
        file='cpersona/coarse_search.py',
        find='selected = selected[(stamps >= low.encode("ascii")) & (stamps < high.encode("ascii"))]',
        replace='selected = selected[(stamps > low.encode("ascii")) & (stamps < high.encode("ascii"))]',
        breaks='a record at the first instant of a period is in the period in SQL and missing from the index: the index is stricter than the authority',
        expect='test_coarse_search.py::test_the_index_offers_every_row_the_authority_admits',
    ),
    Mutation(
        id='M224',
        tests=('tests/test_coarse_search.py',),
        target='coarse search — a live row is merged into the index order on created_at DESC then id ASC',
        file='cpersona/coarse_search.py',
        find='key = ((row[1] or "").encode("utf-8", "replace"), -int(row[0]))',
        replace='key = ((row[1] or "").encode("utf-8", "replace"), int(row[0]))',
        breaks='a row written after the build sorts before the indexed rows of its second, so every later scan position moves',
        expect='test_coarse_search.py::test_the_index_returns_what_the_live_store_returns',
    ),
    Mutation(
        id='M225',
        tests=('tests/test_coarse_search.py',),
        target='coarse search — an indexed row whose embedding was cleared sends the question to the live store',
        file='cpersona/coarse_search.py',
        find='        if await _lost_since_build(db, index, index.ids[selected], agent_id):',
        replace='        if False and await _lost_since_build(db, index, index.ids[selected], agent_id):',
        breaks='the index counts a row the scan no longer reads, so every later position is one off and a record with no vector can be offered',
        expect='test_coarse_search.py::test_a_cleared_embedding_sends_the_question_to_the_live_store',
    ),
    Mutation(
        id='M226',
        tests=('tests/test_coarse_search.py',),
        target='coarse search — a row of another width keeps its scan position in the live store',
        file='cpersona/coarse_search.py',
        find=(
            '                if len(blob) == width:\n'
            '                    ids.append(row_id)\n'
            '                    blobs.append(blob)\n'
            '                    positions.append(position)\n'
            '                position += 1\n'
        ),
        replace=(
            '                if len(blob) == width:\n'
            '                    ids.append(row_id)\n'
            '                    blobs.append(blob)\n'
            '                    positions.append(position)\n'
            '                    position += 1\n'
        ),
        breaks='the range is counted over comparable rows instead of the scan, so a far region starts inside the near window',
        expect='test_coarse_search.py::test_a_row_of_another_width_keeps_its_scan_position_and_is_skipped',
    ),
    Mutation(
        id='M227',
        tests=('tests/test_coarse_search.py',),
        target='coarse search — a candidate deleted since the build sends the question to the live store',
        file='cpersona/coarse_search.py',
        find='        if not await _all_exist(db, list(found.ids)):',
        replace='        if False and not await _all_exist(db, list(found.ids)):',
        breaks='the index offers a record that no longer exists and the answer is one record short',
        expect='test_coarse_search.py::test_a_deleted_candidate_sends_the_question_to_the_live_store',
    ),
    Mutation(
        id='M228',
        tests=('tests/test_coarse_search.py',),
        target="coarse search — the rows the index reads live are held to the cue's period",
        file='cpersona/coarse_search.py',
        find='source_id=source_id, scan_limit=-1, table="memories", window=period,',
        replace='source_id=source_id, scan_limit=-1, table="memories", window=None,',
        breaks='a record written after the build lands in every period, so a cue reaches rows its period does not hold',
        expect='test_coarse_search.py::test_the_index_returns_what_the_live_store_returns',
    ),
    Mutation(
        id='M229',
        tests=('tests/test_far_seats.py',),
        target="far seats — a far record is held to the vector arm's similarity floor",
        file='cpersona/far_seats.py',
        find='        if cosine >= floor\n',
        replace='        if cosine >= floor - 1.0\n',
        breaks='a far record the near rows of the same recall would have been dropped for takes a seat',
        expect='test_far_seats.py::test_a_far_record_below_the_vector_floor_is_not_seated',
    ),
    Mutation(
        id='M230',
        tests=('tests/test_far_seats.py',),
        target='far seats — a record an ordinary arm reached and the gate refused is not seated',
        file='cpersona/memory_handlers.py',
        find='            if ("mem", h.id) not in present and (("mem", h.id) not in reached or ("mem", h.id) in admitted_rids)\n',
        replace='            if ("mem", h.id) not in present\n',
        breaks='a row the quality gate or autocut refused comes back through a held place',
        expect='test_far_seats.py::test_a_record_an_ordinary_arm_reached_and_the_gate_refused_is_not_seated',
    ),
    Mutation(
        id='M231',
        tests=('tests/test_far_seats.py',),
        target="far seats — the reads by id re-apply the isolation predicate",
        file='cpersona/far_seats.py',
        find='    return f"{iso.and_clause}{src.and_clause}", (*iso.params, *src.params)\n',
        replace='    return f"{src.and_clause}", (*src.params,)\n',
        breaks="a candidate the index offered from outside the caller's scope is seated, so the index becomes an authority",
        expect='test_far_seats.py::test_the_hydrate_drops_a_candidate_the_authority_does_not_admit',
    ),
    Mutation(
        id='M232',
        tests=('tests/test_far_seats.py',),
        target='far seats — two places, fixed',
        file='cpersona/far_seats.py',
        find='SEATS = 2\n',
        replace='SEATS = 3\n',
        breaks='the bound on what a bad far hit can cost grows without the measurement decision D requires',
        expect='test_far_seats.py::test_the_seats_hold_the_best_far_records_and_displace_nothing',
    ),
    Mutation(
        id='M233',
        tests=('tests/test_far_seats.py',),
        target='far seats — the scan begins past every vector list of the recall',
        file='cpersona/far_seats.py',
        find='    return max(vector.MAX_MEMORIES, vector.VECTOR_REACH)\n',
        replace='    return vector.MAX_MEMORIES\n',
        breaks="the far list's rows are searched again and can be seated beside the votes they already cast",
        expect='test_far_seats.py::test_the_far_scan_begins_past_every_vector_list',
    ),
    Mutation(
        id='M234',
        tests=('tests/test_far_seats.py',),
        target='far seats — off is a guard: the far scan does not run at the default',
        file='cpersona/memory_handlers.py',
        find='    return far_seats.enabled() and bool(query.strip())\n',
        replace='    return bool(query.strip())\n',
        breaks='every recall pays for a coarse scan and may return two more rows with the setting off',
        expect='test_far_seats.py::test_off_runs_nothing',
    ),
    Mutation(
        id='M235',
        tests=('tests/test_far_seats.py',),
        target='far seats — no local query vector, no far scan',
        file='cpersona/memory_handlers.py',
        find='        if far_on and query_vec_out:\n',
        replace='        if far_on:\n',
        breaks='a recall whose vector search produced no local vector (remote, or no client) fails instead of answering',
        expect='test_far_seats.py::test_no_query_vector_no_far_scan',
    ),
    Mutation(
        id='M236',
        tests=('tests/test_far_seats.py',),
        target="far seats — ranked by the stored vector's cosine, ties by scan position",
        file='cpersona/far_seats.py',
        find='    hits.sort(key=lambda h: (-h.cosine, h.position))\n',
        replace='    hits.sort(key=lambda h: (h.position,))\n',
        breaks='the seats go to the earliest far records the Hamming pass kept instead of the closest ones',
        expect='test_far_seats.py::test_the_seats_hold_the_best_far_records_and_displace_nothing',
    ),
    Mutation(
        id='M237',
        tests=('tests/test_coarse_search.py',),
        target='coarse search — the candidate count is the measured decision',
        file='cpersona/coarse_search.py',
        find='CANDIDATES = 256\n',
        replace='CANDIDATES = 255\n',
        breaks='the Hamming pass keeps a count no measurement chose, and nothing says so',
        expect='test_coarse_search.py::test_the_candidate_count_is_the_measured_decision',
    ),
    Mutation(
        id='M238',
        tests=('tests/test_cue_coarse.py',),
        target='cue remainder — off is a guard',
        file='cpersona/memory_handlers.py',
        find='        if mode != "off":\n',
        replace='        if True:\n',
        breaks='the coarse search runs on every cued recall with the setting off',
        expect='test_cue_coarse.py::test_off_the_remainder_is_not_searched',
    ),
    Mutation(
        id='M239',
        tests=('tests/test_cue_coarse.py',),
        target='cue remainder — starts at the cap',
        file='cpersona/memory_handlers.py',
        find='                source_id=source_id, floor=floor, start=MAX_MEMORIES, period=(start, end),\n',
        replace='                source_id=source_id, floor=floor, start=0, period=(start, end),\n',
        breaks='the capped records are searched a second time',
        expect='test_cue_coarse.py::test_the_remainder_starts_at_the_cap_of_the_same_period',
    ),
    Mutation(
        id='M240',
        tests=('tests/test_cue_coarse.py',),
        target="cue remainder — searches the cue's period",
        file='cpersona/memory_handlers.py',
        find='                source_id=source_id, floor=floor, start=MAX_MEMORIES, period=(start, end),\n',
        replace='                source_id=source_id, floor=floor, start=MAX_MEMORIES, period=None,\n',
        breaks='records outside the period come back through the remainder',
        expect='test_cue_coarse.py::test_the_remainder_stays_inside_the_period',
    ),
    Mutation(
        id='M241',
        tests=('tests/test_cue_coarse.py',),
        target="cue remainder — held to the vector half's floor",
        file='cpersona/memory_handlers.py',
        find='                source_id=source_id, floor=floor, start=MAX_MEMORIES, period=(start, end),\n',
        replace='                source_id=source_id, floor=0.0, start=MAX_MEMORIES, period=(start, end),\n',
        breaks='records below the floor the capped list met reach the cue list',
        expect='test_cue_coarse.py::test_the_remainder_is_held_to_the_floor',
    ),
    Mutation(
        id='M242',
        tests=('tests/test_cue_coarse.py',),
        target='cue remainder — merged on cosine',
        file='cpersona/memory_handlers.py',
        find='                key=lambda s: (-s[3], s[0], s[1]),\n',
        replace='                key=lambda s: (s[0], -s[3], s[1]),\n',
        breaks='the remainder is appended after the capped list instead of ranked into it',
        expect='test_cue_coarse.py::test_a_close_record_past_the_cap_is_reached_and_ranked_by_cosine',
    ),
    Mutation(
        id='M243',
        tests=('tests/test_cue_coarse.py',),
        target='cue remainder — a tie goes to the capped list',
        file='cpersona/memory_handlers.py',
        find='                key=lambda s: (-s[3], s[0], s[1]),\n',
        replace='                key=lambda s: (-s[3], -s[0], s[1]),\n',
        breaks='an equally close record from the remainder outranks the capped one earlier in the scan',
        expect='test_cue_coarse.py::test_a_tie_goes_to_the_capped_list',
    ),
    Mutation(
        id='M244',
        tests=('tests/test_cue_coarse.py',),
        target='far seats ranking — the period is re-applied when candidates are read by id',
        file='cpersona/far_seats.py',
        find='        clause, params = f"{clause} AND {coarse_index.PERIOD_PREDICATE}", (*params, *period)\n',
        replace='        clause, params = clause, params\n',
        breaks='a stale index can hand the cue a record from outside its period',
        expect='test_cue_coarse.py::test_the_period_is_reapplied_when_the_candidates_are_read',
    ),
    Mutation(
        id='M245',
        tests=('tests/test_embedding_model_label.py',),
        target='embedding_model label — store writes the label beside the vector',
        file='cpersona/memory_handlers.py',
        find='(agent_id, project_id, msg_id, content, source, timestamp, metadata, embedding_blob, embedding_label, channel),\n',
        replace='(agent_id, project_id, msg_id, content, source, timestamp, metadata, embedding_blob, "", channel),\n',
        breaks='a stored vector carries no label, so a later model swap cannot be seen',
        expect='test_embedding_model_label.py::test_store_labels_the_vector_with_the_fingerprint_the_backend_reports',
    ),
    Mutation(
        id='M246',
        tests=('tests/test_embedding_model_label.py',),
        target='embedding_model label — store labels only a vector it wrote',
        file='cpersona/memory_handlers.py',
        find='            label = await generation.record_label()\n            embeddings = await vector._embedding_client.embed([content])\n',
        replace='            label = await generation.record_label()\n            embedding_label = label\n            embeddings = await vector._embedding_client.embed([content])\n',
        breaks='a row whose embed failed carries a label for a vector it does not have',
        expect='test_embedding_model_label.py::test_a_failed_embed_leaves_neither_vector_nor_label',
    ),
    Mutation(
        id='M247',
        tests=('tests/test_embedding_model_label.py',),
        target='embedding_model label — the label is learned, not read stale',
        file='cpersona/generation.py',
        find='    await refresh()\n    return record_keys()[0]\n',
        replace='    return record_keys()[0]\n',
        breaks='a write labels its vector with whatever was learned last, or nothing, instead of asking',
        expect='test_embedding_model_label.py::test_store_labels_the_vector_with_the_fingerprint_the_backend_reports',
    ),
    Mutation(
        id='M248',
        tests=('tests/test_embedding_model_label.py',),
        target='embedding_model label — an episode is labelled',
        file='cpersona/memory_handlers.py',
        find='        embedding_label,\n        int(resolved),\n',
        replace='        "",\n        int(resolved),\n',
        breaks='episode vectors carry no label',
        expect='test_embedding_model_label.py::test_an_episode_is_labelled',
    ),
    Mutation(
        id='M249',
        tests=('tests/test_embedding_model_label.py',),
        target='embedding_model label — update_memory rewrites the label with the vector',
        file='cpersona/admin_handlers.py',
        find='            "UPDATE memories SET content = ?, embedding = ?, embedding_model = ? WHERE id = ? AND locked = 0",\n',
        replace='            "UPDATE memories SET content = ?, embedding = ?, embedding_model = COALESCE(NULLIF(?, \'\'), embedding_model) WHERE id = ? AND locked = 0",\n',
        breaks="a NULLed vector keeps the old vector's label",
        expect='test_embedding_model_label.py::test_update_memory_relabels_the_new_vector_and_clears_a_lost_one',
    ),
    Mutation(
        id='M250',
        tests=('tests/test_embedding_model_label.py',),
        target='embedding_model label — export carries the label with the vector',
        file='cpersona/admin_handlers.py',
        find='                            record["embedding_model"] = row[14]\n',
        replace='                            pass\n',
        breaks='an export with vectors loses what wrote them; the restore labels them unknown',
        expect='test_embedding_model_label.py::test_the_label_travels_with_the_vector_through_export_and_import',
    ),
    Mutation(
        id='M251',
        tests=('tests/test_embedding_model_label.py',),
        target='embedding_model label — an imported label needs a restored vector',
        file='cpersona/admin_handlers.py',
        find='    if blob is None or not isinstance(label, str):\n',
        replace='    if not isinstance(label, str):\n',
        breaks='a restored row without a vector keeps a label that describes nothing',
        expect='test_embedding_model_label.py::test_an_imported_label_is_kept_only_beside_a_restored_vector',
    ),
    Mutation(
        id='M252',
        tests=('tests/test_embedding_model_label.py',),
        target='embedding_model label — merge carries the label',
        file='cpersona/admin_handlers.py',
        find='                    embedding_model,\n                    locked,\n',
        replace='                    "",\n                    locked,\n',
        breaks="a merged memory's vector arrives unlabelled",
        expect='test_embedding_model_label.py::test_merge_carries_the_label',
    ),
    Mutation(
        id='M253',
        tests=('tests/test_embedding_model_label.py',),
        target='embedding_model label — the dimension repair clears the label with the vector',
        file='cpersona/checks.py',
        find='                f"""UPDATE memories SET embedding = NULL, embedding_model = \'\'\n',
        replace='                f"""UPDATE memories SET embedding = NULL\n',
        breaks='a vector NULLed for its width keeps a label for a vector it no longer has',
        expect='test_embedding_model_label.py::test_a_dimension_repair_alone_leaves_no_stale_label',
    ),
    Mutation(
        id='M254',
        tests=('tests/test_embedding_model_label.py',),
        target='embedding_model label — a content rewrite clears the label',
        file='cpersona/checks.py',
        find='            "UPDATE memories SET content = ?, embedding = NULL, embedding_model = \'\' WHERE id = ? AND locked = 0",\n',
        replace='            "UPDATE memories SET content = ?, embedding = NULL WHERE id = ? AND locked = 0",\n',
        breaks="a rewritten row keeps the old vector's label",
        expect='test_embedding_model_label.py::test_a_content_rewrite_clears_the_label_with_the_vector',
    ),
    Mutation(
        id='M255',
        tests=('tests/test_embedding_model_label.py',),
        target='embedding_model label — the re-embed stamps the label it was computed under',
        file='cpersona/checks.py',
        find='                (blob, label, row_id, text),\n            )\n            if getattr(cur, "rowcount", 0) == 1:\n                applied += 1\n',
        replace='                (blob, "", row_id, text),\n            )\n            if getattr(cur, "rowcount", 0) == 1:\n                applied += 1\n',
        breaks='a re-embedded vector is written unlabelled',
        expect='test_embedding_model_label.py::test_nulling_a_vector_clears_its_label_and_the_re_embed_stamps_the_current_one',
    ),
    Mutation(
        id='M256',
        tests=('tests/test_embedding_model_label.py',),
        target='schema v18 — an existing database gains the column',
        file='cpersona/database.py',
        find='        if current < 18:\n',
        replace='        if False:\n',
        breaks='an upgraded database has no embedding_model column and every labelled write fails',
        expect='test_embedding_model_label.py::test_a_v17_database_gains_the_column_with_unknown_labels',
    ),
    Mutation(
        id='M257',
        tests=('tests/test_embedding_model_label.py',),
        target='embedding_model judgment — nothing is judged without an identity',
        file='cpersona/generation.py',
        find='    if current_identity() is None:\n        return None\n    return tuple(dict.fromkeys(("", *record_keys())))\n',
        replace='    return tuple(dict.fromkeys(("", *record_keys())))\n',
        breaks="an unknown backend reads every fingerprint label as another model's",
        expect='test_embedding_model_label.py::test_which_labels_are_current',
    ),
    Mutation(
        id='M258',
        tests=('tests/test_embedding_model_label.py',),
        target="embedding_model judgment — the api transport's model is its identity",
        file='cpersona/generation.py',
        find='    if config.EMBEDDING_MODE == "api":\n        return config.EMBEDDING_MODEL\n    return None\n',
        replace='    return None\n',
        breaks='api-mode deployments never judge a label',
        expect='test_embedding_model_label.py::test_which_labels_are_current',
    ),
    Mutation(
        id='M259',
        tests=('tests/test_embedding_model_label.py',),
        target='embedding_model check — a label beside no vector is not counted',
        file='cpersona/checks.py',
        find='            f"SELECT embedding_model, COUNT(*) FROM {table} WHERE embedding IS NOT NULL"\n',
        replace='            f"SELECT embedding_model, COUNT(*) FROM {table} WHERE 1=1"\n',
        breaks='rows with no vector are reported as written by another model',
        expect='test_embedding_model_label.py::test_the_check_counts_vectors_another_model_wrote',
    ),
    Mutation(
        id='M260',
        tests=('tests/test_embedding_model_label.py',),
        target='embedding_model check — off silences the report',
        file='cpersona/checks.py',
        find='    if config.EMBEDDING_MODEL_MODE == "off":\n        return []\n    accepted = generation.accepted_labels()\n',
        replace='    accepted = generation.accepted_labels()\n',
        breaks='the report cannot be turned off',
        expect='test_embedding_model_label.py::test_the_check_is_silent_under_off_and_without_an_identity',
    ),
    Mutation(
        id='M261',
        tests=('tests/test_embedding_model_label.py',),
        target='embedding_model check — check_health learns the identity before the run',
        file='cpersona/maintenance_handlers.py',
        find='    if "embedding_model" in checks_run:\n        await generation.refresh()\n',
        replace='    if False:\n        await generation.refresh()\n',
        breaks='a server that has not asked its backend yet judges nothing on its first check_health',
        expect='test_embedding_model_label.py::test_the_check_is_registered_as_a_report',
    ),
    Mutation(
        id='M262',
        tests=('tests/test_embedding_model_label.py',),
        target='reject — only under reject',
        file='cpersona/vector.py',
        find='    if config.EMBEDDING_MODEL_MODE != "reject":\n        return set()\n',
        replace='    if False:\n        return set()\n',
        breaks='warn drops rows from the vector arm, so the default changes what recall returns',
        expect='test_embedding_model_label.py::test_the_vector_arm_is_unchanged_unless_reject',
    ),
    Mutation(
        id='M263',
        tests=('tests/test_embedding_model_label.py',),
        target="reject — the vector arm's scan",
        file='cpersona/vector.py',
        find='    return await _drop_rejected_labels(db, [c[1] for c in top_k])\n',
        replace='    return [c[1] for c in top_k]\n',
        breaks="another model's vector ranks in the vector arm under reject",
        expect='test_embedding_model_label.py::test_reject_drops_another_models_vector_and_nothing_else',
    ),
    Mutation(
        id='M264',
        tests=('tests/test_embedding_model_label.py',),
        target='reject — the remote answer',
        file='cpersona/vector.py',
        find='        return await _drop_rejected_labels(db, remote_results)\n',
        replace='        return remote_results\n',
        breaks="the remote service's answer bypasses reject",
        expect='test_embedding_model_label.py::test_reject_covers_the_remote_answer',
    ),
    Mutation(
        id='M265',
        tests=('tests/test_embedding_model_label.py',),
        target='reject — the far list',
        file='cpersona/vector.py',
        find='            await _drop_rejected_labels(db, [row for row in far_rows if row.get("_rid") not in near_rids])\n',
        replace='            [row for row in far_rows if row.get("_rid") not in near_rids]\n',
        breaks='the far list bypasses reject',
        expect='test_embedding_model_label.py::test_reject_covers_the_far_list',
    ),
    Mutation(
        id='M266',
        tests=('tests/test_embedding_model_label.py',),
        target='reject — the cosine backfill',
        file='cpersona/memory_handlers.py',
        find='    rejected = await vector.rejected_rids(db, [("ep" if is_ep else "mem", rid) for (_, rid, is_ep) in needy])\n',
        replace='    rejected = set()\n',
        breaks="a lexically found row is scored with another model's cosine under reject",
        expect='test_embedding_model_label.py::test_reject_leaves_the_cosine_backfill_empty',
    ),
    Mutation(
        id='M267',
        tests=('tests/test_embedding_model_label.py',),
        target='reject — the far seats',
        file='cpersona/far_seats.py',
        find='    rejected = await vector.rejected_rids(db, [("mem", row_id) for row_id in stored])\n',
        replace='    rejected = set()\n',
        breaks="the far seats rank another model's vectors under reject",
        expect='test_embedding_model_label.py::test_reject_covers_the_far_seats',
    ),
    Mutation(
        id='M268',
        tests=('tests/test_embedding_model_label.py',),
        target="reject — the cue arm's memories",
        file='cpersona/memory_handlers.py',
        find='        lists.append(await vector._drop_rejected_labels(db, vector_rows))\n',
        replace='        lists.append(vector_rows)\n',
        breaks="the cue arm ranks another model's memory vectors under reject",
        expect='test_embedding_model_label.py::test_reject_covers_the_cue_arm',
    ),
    Mutation(
        id='M269',
        tests=('tests/test_embedding_model_label.py',),
        target="reject — the cue arm's episodes",
        file='cpersona/memory_handlers.py',
        find='            lists.append(await vector._drop_rejected_labels(db, ep_vector_rows))\n',
        replace='            lists.append(ep_vector_rows)\n',
        breaks="the cue arm ranks another model's episode vectors under reject",
        expect='test_embedding_model_label.py::test_reject_covers_the_cue_arm',
    ),
    Mutation(
        id='M270',
        tests=('tests/test_embedding_model_label.py',),
        target='reject — propagation',
        file='cpersona/memory_handlers.py',
        find='            for rid in await vector.rejected_rids(db, blobs):\n',
        replace='            for rid in []:\n',
        breaks="propagation measures closeness with another model's vectors under reject",
        expect='test_embedding_model_label.py::test_reject_covers_the_propagation_seat',
    ),
    Mutation(
        id='M271',
        tests=('tests/test_embedding_model_label.py',),
        target='embedding_model label — the prefetched re-embed carries its label to the locked write',
        file='cpersona/checks.py',
        find='                label = cached_label\n',
        replace='                label = ""\n',
        breaks='a vector re-embedded before the write lock is applied unlabelled',
        expect='test_embedding_model_label.py::test_a_row_already_without_a_vector_is_re_embedded_under_the_label_learned_unlocked',
    ),
    Mutation(
        id='M272',
        tests=('tests/test_embedding_model_label.py',),
        target='embedding_model label — the live re-embed labels what it writes',
        file='cpersona/checks.py',
        find='                label = await generation.record_label()\n                emb = await vector._embedding_client.embed([text])\n',
        replace='                label = ""\n                emb = await vector._embedding_client.embed([text])\n',
        breaks='a vector re-embedded by a direct caller is written unlabelled',
        expect='test_embedding_model_label.py::test_the_live_re_embed_labels_what_it_writes',
    ),
    Mutation(
        id='M273',
        tests=('tests/test_reconstruct_v1_2.py',),
        target='reconstruct v1.2 — blocks ranked by their int8 vectors',
        file='cpersona/reconstruct.py',
        find='    cosines = _int8_cosines(block_rows, query_vec)\n',
        replace='    cosines = None\n',
        breaks='the head quote and the excerpts rank blocks by their sign bits only',
        expect='test_reconstruct_v1_2.py::test_the_int8_vectors_rank_the_blocks_when_every_block_has_one',
    ),
    Mutation(
        id='M274',
        tests=('tests/test_reconstruct_v1_2.py',),
        target='reconstruct v1.2 — the int8 ranking is all or nothing',
        file='cpersona/reconstruct.py',
        find='    if any(v is None or len(v) != width for v in stored):\n        return None\n',
        replace='    if all(v is None or len(v) != width for v in stored):\n        return None\n',
        breaks='a record with one block missing its vector is ranked on a mix of scales (or not at all)',
        expect='test_reconstruct_v1_2.py::test_one_block_without_a_vector_leaves_the_ranking_to_the_bits',
    ),
    Mutation(
        id='M275',
        tests=('tests/test_reconstruct_v1_2.py',),
        target='reconstruct v1.2 — block sets carry their stored vectors',
        file='cpersona/reconstruct.py',
        find='                        block_sets[f"{kind}:{parent_id}"] = (text, [(*g[:4], g[6]) for g in group])\n',
        replace='                        block_sets[f"{kind}:{parent_id}"] = (text, [g[:4] for g in group])\n',
        breaks='the int8 ranking never runs: every block set reads as having no vectors',
        expect='test_reconstruct_v1_2.py::test_a_block_set_is_read_with_its_stored_vectors',
    ),
    Mutation(
        id='M276',
        tests=('tests/test_reconstruct_v1_2.py',),
        target='reconstruct v1.2 — the head quote is ranked with the query vector',
        file='cpersona/reconstruct.py',
        find='        text, block_entry[1] if block_entry is not None else None, query_bits, query_grams, cap, query_vec\n',
        replace='        text, block_entry[1] if block_entry is not None else None, query_bits, query_grams, cap\n',
        breaks='head quotes fall back to the bits',
        expect='test_reconstruct_v1_2.py::test_the_head_quote_is_ranked_with_the_query_vector',
    ),
    Mutation(
        id='M277',
        tests=('tests/test_reconstruct_v1_2.py',),
        target='reconstruct v1.2 — an excerpt is ranked with the query vector',
        file='cpersona/reconstruct.py',
        find='        quote = _block_quote(claim, block_entry, query_bits, query_grams, query_vec)\n',
        replace='        quote = _block_quote(claim, block_entry, query_bits, query_grams)\n',
        breaks='excerpts fall back to the bits',
        expect='test_reconstruct_v1_2.py::test_an_excerpt_is_ranked_with_the_query_vector',
    ),
    Mutation(
        id='M278',
        tests=('tests/test_recall_excerpt.py',),
        target='reconstruct v1.2 — the recall excerpt is ranked with the query vector',
        file='cpersona/excerpts.py',
        find='        rows, basis, bits, vec = stored_rows, "blocks", query_bits, query_vec\n',
        replace='        rows, basis, bits, vec = stored_rows, "blocks", query_bits, None\n',
        breaks='the recall excerpt falls back to the bits',
        expect='test_recall_excerpt.py::test_a_block_set_is_ranked_with_the_query_vector_the_recall_embedded',
    ),
    Mutation(
        id='M279',
        tests=('tests/test_reconstruct_v1_2.py',),
        target='reconstruct v1.2 — touching passages are one passage',
        file='cpersona/excerpts.py',
        find='    return _joined(sorted(chosen)), False\n',
        replace='    return sorted(chosen), False\n',
        breaks='a sentence running across two blocks is cut by a separator',
        expect='test_reconstruct_v1_2.py::test_touching_passages_are_joined_without_a_separator',
    ),
    Mutation(
        id='M280',
        tests=('tests/test_reconstruct_v1_2.py',),
        target='reconstruct v1.2 — a touching passage costs no separator',
        file='cpersona/excerpts.py',
        find='        cost = (end - start) + (len(SEPARATOR) if chosen and not touching else 0)\n',
        replace='        cost = (end - start) + (len(SEPARATOR) if chosen else 0)\n',
        breaks='a neighbour that fits without a separator is left out',
        expect='test_reconstruct_v1_2.py::test_a_touching_passage_costs_no_separator',
    ),
    Mutation(
        id='M281',
        tests=('tests/test_reconstruct_v1_2.py',),
        target='reconstruct v1.2 — later items take the tail size',
        file='cpersona/reconstruct.py',
        find='    return config.RECONSTRUCT_TAIL_QUOTE_CHARS\n',
        replace='    return _head_cap()\n',
        breaks='every item is quoted at the full size',
        expect='test_reconstruct_v1_2.py::test_the_first_items_are_full_and_the_rest_tail_sized',
    ),
    Mutation(
        id='M282',
        tests=('tests/test_reconstruct_v1_2.py',),
        target='reconstruct v1.2 — the default budget sums the sizes',
        file='cpersona/reconstruct.py',
        find='        heads = sum(max(_head_cap_at(position), 0) for position in range(int(count)))\n',
        replace='        heads = int(count) * max(_head_cap(), 0)\n',
        breaks='the default budget is sized for full quotes the response no longer carries',
        expect='test_reconstruct_v1_2.py::test_the_default_budget_sums_the_sizes',
    ),
    Mutation(
        id='M283',
        tests=('tests/test_reconstruct_v1_2.py',),
        target="reconstruct v1.2 — the quote size follows the item's place",
        file='cpersona/reconstruct.py',
        find='                head, block_sets.get(head.ref), query_bits, query_grams, _head_cap_at(position), query_vec\n',
        replace='                head, block_sets.get(head.ref), query_bits, query_grams, _head_cap(), query_vec\n',
        breaks='a reconstruction quotes every item at the full size',
        expect='test_reconstruct_v1_2.py::test_a_reconstruction_quotes_its_later_items_at_the_tail_size',
    ),
    Mutation(
        id='M284',
        tests=('tests/test_reconstruct_v1_2.py',),
        target='reconstruct v1.2 — a tail outside the head size is refused',
        file='cpersona/config.py',
        find='    if RECONSTRUCT_QUOTE_CHARS > 0 and not 0 < RECONSTRUCT_TAIL_QUOTE_CHARS <= RECONSTRUCT_QUOTE_CHARS:\n',
        replace='    if False:\n',
        breaks='a tail longer than the head, or empty, starts silently',
        expect='test_reconstruct_v1_2.py::test_a_tail_outside_the_head_size_is_a_startup_error',
    ),
    Mutation(
        id='M285',
        tests=('tests/test_reconstruct_v1_2.py',),
        target='reconstruct v1.2 — quote_basis and content_truncated go to the trace',
        file='cpersona/reconstruct.py',
        find='    out = {k: v for k, v in item.items() if k not in ("quote_basis", "content_truncated")}\n',
        replace='    out = dict(item)\n',
        breaks='the default response keeps fields a reader does not act on',
        expect='test_reconstruct_v1_2.py::test_compaction_drops_only_the_defaults_and_the_redundant',
    ),
    Mutation(
        id='M286',
        tests=('tests/test_reconstruct_v1_2.py',),
        target='reconstruct v1.2 — only the default independence reason is omitted',
        file='cpersona/reconstruct.py',
        find='    if out.get("independence_reason") == _DEFAULT_INDEPENDENCE:\n',
        replace='    if out.get("independence_reason"):\n',
        breaks='a bundled item no longer says why it is separate',
        expect='test_reconstruct_v1_2.py::test_compaction_drops_only_the_defaults_and_the_redundant',
    ),
    Mutation(
        id='M287',
        tests=('tests/test_reconstruct_v1_2.py',),
        target='reconstruct v1.2 — only the default why is omitted',
        file='cpersona/reconstruct.py',
        find='            {k: v for k, v in claim.items() if not (k == "why" and v == _DEFAULT_WHY)} for claim in out["claims"]\n',
        replace='            {k: v for k, v in claim.items() if k != "why"} for claim in out["claims"]\n',
        breaks='a claim a bundling key or a relation admitted no longer says so',
        expect='test_reconstruct_v1_2.py::test_compaction_drops_only_the_defaults_and_the_redundant',
    ),
    Mutation(
        id='M288',
        tests=('tests/test_reconstruct_review.py',),
        target='reconstruct v1.2 — the default response is compacted item by item',
        file='cpersona/reconstruct.py',
        find='    out["items"] = [_compact_item(item) for item in out["items"]]\n',
        replace='',
        breaks='the default response carries every item field again',
        expect='test_reconstruct_review.py::test_a_request_honoured_as_asked_is_not_restated',
    ),
    Mutation(
        id='M289',
        tests=('tests/test_coarse_index_health.py',),
        target='coarse_index health — silent while no setting reads the index',
        file='cpersona/checks.py',
        find='    if not starts:\n        return []\n    start = min(starts)\n',
        replace='    if not starts:\n        starts = [vector.MAX_MEMORIES]\n    start = min(starts)\n',
        breaks='the check warns about an index no setting reads',
        expect='test_coarse_index_health.py::test_settings_off_say_nothing_however_large_the_store',
    ),
    Mutation(
        id='M290',
        tests=('tests/test_coarse_index_health.py',),
        target='coarse_index health — the window is per agent',
        file='cpersona/checks.py',
        find='        " GROUP BY agent_id ORDER BY n DESC LIMIT 1",\n',
        replace='        "",\n',
        breaks='two agents that each fit the window read as one store past it',
        expect='test_coarse_index_health.py::test_the_window_is_per_agent_not_per_store',
    ),
    Mutation(
        id='M291',
        tests=('tests/test_coarse_index_health.py',),
        target='coarse_index health — the cue remainder starts at the window',
        file='cpersona/checks.py',
        find='    if config.CUE_COARSE_MODE != "off":\n        starts.append(vector.MAX_MEMORIES)\n',
        replace='    if config.CUE_COARSE_MODE != "off":\n        starts.append(far_seats.scan_start())\n',
        breaks='the cue remainder is measured from VECTOR_REACH and its records go unreported',
        expect='test_coarse_index_health.py::test_the_cue_remainder_begins_at_the_window_whatever_vector_reach_is',
    ),
    Mutation(
        id='M292',
        tests=('tests/test_coarse_index_health.py',),
        target='coarse_index health — absence below the size that matters is silent',
        file='cpersona/checks.py',
        find='        if far_rows < INDEX_MATTERS_ROWS:\n            return []\n',
        replace='        if far_rows < 0:\n            return []\n',
        breaks='a few records past the window raise a warning that costs nothing to ignore',
        expect='test_coarse_index_health.py::test_a_far_stratum_too_small_to_cost_anything_says_nothing',
    ),
    Mutation(
        id='M293',
        tests=('tests/test_coarse_index_health.py',),
        target='coarse_index health — absence under a reading setting is stamped warn',
        file='cpersona/checks.py',
        find='            "type": "coarse_index_absent",\n            "severity": "warn",\n',
        replace='            "type": "coarse_index_absent",\n',
        breaks="a setting paying the live store's price is reported as an observation",
        expect='test_coarse_index_health.py::test_no_file_under_a_setting_that_reads_it_is_a_stamped_warning_with_its_price',
    ),
    Mutation(
        id='M294',
        tests=('tests/test_coarse_index_health.py',),
        target='coarse_index health — rows the index holds and the store lost',
        file='cpersona/checks.py',
        find='        if index.count and live_indexed < index.count:\n            issue = {\n                "type": "coarse_index_rows_missing",\n',
        replace='        if index.count and live_indexed < index.count - 1:\n            issue = {\n                "type": "coarse_index_rows_missing",\n',
        breaks='one deleted indexed row leaves the fallback unreported',
        expect='test_coarse_index_health.py::test_rows_the_index_holds_and_the_store_lost_are_reported',
    ),
    Mutation(
        id='M295',
        tests=('tests/test_coarse_index_health.py',),
        target='coarse_index health — the tail threshold is strict',
        file='cpersona/checks.py',
        find='        read_exactly = await vector_index.rows_read_exactly(db, index, "memories", all_axes)\n        if index.count and read_exactly > INDEX_TAIL_RATIO * index.count:\n',
        replace='        read_exactly = await vector_index.rows_read_exactly(db, index, "memories", all_axes)\n        if index.count and read_exactly >= INDEX_TAIL_RATIO * index.count:\n',
        breaks='a tail exactly at the ratio is reported as overdue',
        expect='test_coarse_index_health.py::test_a_tail_inside_the_ratio_says_nothing',
    ),
    Mutation(
        id='M296',
        tests=('tests/test_coarse_index_health.py',),
        target='coarse_index health — no build where the builder would decline',
        file='cpersona/checks.py',
        find='    if fix and repairable:\n',
        replace='    if fix:\n',
        breaks='a fix run attempts a build the builder refuses mid model swap',
        expect='test_coarse_index_health.py::test_a_corpus_of_another_width_is_drift_and_one_of_two_widths_is_not_repairable',
    ),
    Mutation(
        id='M297',
        tests=('tests/test_coarse_index_health.py',),
        target='coarse_index health — fix builds the file',
        file='cpersona/checks.py',
        find='            built = await coarse_index.build_coarse_index(db, "memories")\n',
        replace='            built = {"built": False, "reason": "mutant"}\n',
        breaks='check_health(fix=true) reports the coarse index and never builds it',
        expect='test_coarse_index_health.py::test_fix_builds_the_file_and_the_next_run_is_quiet',
    ),
    Mutation(
        id='M298',
        tests=('tests/test_coarse_index_health.py',),
        target='coarse_index health — the repair is declared cross-agent',
        file='cpersona/checks.py',
        find='    Check("coarse_index", "info", True, check_coarse_index, cross_agent_fix=True),\n',
        replace='    Check("coarse_index", "info", True, check_coarse_index),\n',
        breaks="write on one agent authorises a rebuild of a file holding every agent's records",
        expect='test_coarse_index_health.py::test_the_repair_is_declared_cross_agent_and_the_guard_demands_every_agent',
    ),
    Mutation(
        id='M299',
        tests=('tests/test_coarse_index_health.py',),
        target='coarse_index health — two widths are not repairable',
        file='cpersona/checks.py',
        find='    repairable = 1 if len(widths) <= 1 else 0\n',
        replace='    repairable = 1\n',
        breaks='a finding no build can act on keeps gating as repairable',
        expect='test_coarse_index_health.py::test_a_corpus_of_another_width_is_drift_and_one_of_two_widths_is_not_repairable',
    ),
    Mutation(
        id='M300',
        tests=('tests/test_coarse_index_health.py',),
        target='coarse_index findings — stamped states deliver as degraded',
        file='cpersona/findings.py',
        find='    if check in ("vector_index", "coarse_index"):\n',
        replace='    if check == "vector_index":\n',
        breaks='a coarse index the recall should use and does not is delivered at info',
        expect='test_coarse_index_health.py::test_stamped_states_deliver_as_degraded_and_the_tail_as_the_check',
    ),
    Mutation(
        id='M301',
        tests=('tests/test_coarse_index_health.py',),
        target='coarse_index health — a corpus of another width is drift',
        file='cpersona/checks.py',
        find='    if issue is None and widths and widths != {index.dim * 4}:\n',
        replace='    if False:\n',
        breaks='an index of another dimension is unused and unreported',
        expect='test_coarse_index_health.py::test_a_corpus_of_another_width_is_drift_and_one_of_two_widths_is_not_repairable',
    ),
    Mutation(
        id='M302',
        tests=('tests/test_coarse_index_health.py',),
        target='coarse_index health — an unreadable file is unusable, not absent',
        file='cpersona/checks.py',
        find='            "type": "coarse_index_unusable",\n',
        replace='            "type": "coarse_index_absent",\n',
        breaks='a broken file is reported as one never built',
        expect='test_coarse_index_health.py::test_an_unreadable_file_is_unusable',
    ),
    Mutation(
        id='M303',
        tests=('tests/test_cue_coarse_auto.py',),
        target='cue remainder auto — index only',
        file='cpersona/coarse_search.py',
        find='    if not live:\n        return _empty("skipped")\n',
        replace='',
        breaks='auto falls back to the live store and pays a cost that grows with the store',
        expect='test_cue_coarse_auto.py::test_auto_without_an_index_is_off_and_never_reads_the_live_store',
    ),
    Mutation(
        id='M304',
        tests=('tests/test_cue_coarse_auto.py',),
        target='cue remainder auto — only "true" reads the live store',
        file='cpersona/memory_handlers.py',
        find='                live=mode == "on",\n',
        replace='                live=True,\n',
        breaks='the default mode reads the live store without an index',
        expect='test_cue_coarse_auto.py::test_auto_without_an_index_is_off_and_never_reads_the_live_store',
    ),
    Mutation(
        id='M305',
        tests=('tests/test_cue_coarse_auto.py',),
        target='cue remainder auto — searched through an index',
        file='cpersona/memory_handlers.py',
        find='        if mode != "off":\n',
        replace='        if mode == "on":\n',
        breaks='auto never searches the remainder, even through a usable index',
        expect='test_cue_coarse_auto.py::test_auto_with_an_index_is_on',
    ),
    Mutation(
        id='M306',
        tests=('tests/test_cue_coarse_auto.py',),
        target='cue remainder auto — nothing past the cap is not left out',
        file='cpersona/memory_handlers.py',
        find='                left_out = supplier == "skipped" and await _period_has_remainder(db, iso, src, start, end)\n',
        replace='                left_out = supplier == "skipped"\n',
        breaks='a period within the cap is reported as not searched whole',
        expect='test_cue_coarse_auto.py::test_a_period_within_the_cap_says_nothing_without_an_index',
    ),
    Mutation(
        id='M307',
        tests=('tests/test_cue_coarse_auto.py',),
        target='cue remainder auto — a remainder left out is said',
        file='cpersona/memory_handlers.py',
        find='        if last.get("remainder", {}).get("left_out"):\n',
        replace='        if False:\n',
        breaks='records of the period are dropped and the response does not say so',
        expect='test_cue_coarse_auto.py::test_auto_without_an_index_returns_the_off_recall_and_says_so',
    ),
    Mutation(
        id='M308',
        tests=('tests/test_cue_coarse_auto.py',),
        target='coarse_index health — auto alone pays no live price',
        file='cpersona/checks.py',
        find='    live_price = config.FAR_SEATS_ENABLED or config.CUE_COARSE_MODE == "on"\n',
        replace='    live_price = True\n',
        breaks='an absent index under the default mode is stamped warn for a cost no recall pays',
        expect='test_cue_coarse_auto.py::test_health_reports_an_absent_index_under_auto_as_lost_reach',
    ),
    Mutation(
        id='M309',
        tests=('tests/test_cue_coarse_auto.py',),
        target='coarse_index health — the far seats pay the live price under auto',
        file='cpersona/checks.py',
        find='    live_price = config.FAR_SEATS_ENABLED or config.CUE_COARSE_MODE == "on"\n',
        replace='    live_price = config.CUE_COARSE_MODE == "on"\n',
        breaks='far seats reading the live store go unstamped while the cue mode is auto',
        expect='test_cue_coarse_auto.py::test_health_stamps_the_absence_warn_when_a_reader_pays_the_live_store',
    ),
    Mutation(
        id='M310',
        tests=('tests/test_cue_coarse_auto.py',),
        target="coarse suggestion — starts at the health check's line",
        file='cpersona/coarse_notice.py',
        find='    if past < checks.INDEX_MATTERS_ROWS:\n',
        replace='    if past <= checks.INDEX_MATTERS_ROWS:\n',
        breaks='the suggestion starts one record later than the health finding',
        expect='test_cue_coarse_auto.py::test_the_suggestion_starts_where_the_health_check_does',
    ),
    Mutation(
        id='M311',
        tests=('tests/test_cue_coarse_auto.py',),
        target='coarse suggestion — once per session',
        file='cpersona/coarse_notice.py',
        find='        _remember_told(session_key)\n',
        replace='        pass\n',
        breaks='the suggestion repeats on every recall of a session',
        expect='test_cue_coarse_auto.py::test_the_suggestion_is_said_once_per_session',
    ),
    Mutation(
        id='M312',
        tests=('tests/test_cue_coarse_auto.py',),
        target='coarse suggestion — auto only',
        file='cpersona/coarse_notice.py',
        find='    if config.CUE_COARSE_MODE != "auto":\n',
        replace='    if config.CUE_COARSE_MODE == "off":\n',
        breaks='an operator who set the mode to true is told to build an index',
        expect='test_cue_coarse_auto.py::test_the_suggestion_is_for_auto_only',
    ),
    Mutation(
        id='M313',
        tests=('tests/test_cue_coarse_auto.py',),
        target='coarse suggestion — silent once an index loads',
        file='cpersona/coarse_notice.py',
        find='    if _index_usable():\n        return None\n',
        replace='',
        breaks='the suggestion stands after the index was built',
        expect='test_cue_coarse_auto.py::test_a_recall_below_the_line_or_with_an_index_carries_no_suggestion',
    ),
    Mutation(
        id='M314',
        tests=('tests/test_cue_coarse_auto.py',),
        target='coarse suggestion — reconstruct forwards it',
        file='cpersona/reconstruct.py',
        find='    for notice in ("advisory", "update", "suggestion", "time_cue"):\n',
        replace='    for notice in ("advisory", "update", "time_cue"):\n',
        breaks='a reconstruct caller never hears the suggestion its recall consumed',
        expect='test_cue_coarse_auto.py::test_a_recall_carries_the_suggestion_once_and_reconstruct_forwards_it',
    ),
    Mutation(
        id='M315',
        tests=('tests/test_cue_coarse_auto.py',),
        target='coarse suggestion — recall_with_context forwards it',
        file='cpersona/memory_handlers.py',
        find='    suggestion = recall_result.get("suggestion")\n',
        replace='    suggestion = None\n',
        breaks='a recall_with_context caller never hears the suggestion its recall consumed',
        expect='test_cue_coarse_auto.py::test_a_recall_carries_the_suggestion_once_and_reconstruct_forwards_it',
    ),
    Mutation(
        id='M316',
        tests=('tests/test_cue_coarse_auto.py',),
        target='cue mode — unset or empty is auto',
        file='cpersona/config.py',
        find='    if value in ("", "auto"):\n',
        replace='    if value == "auto":\n',
        breaks='an unset setting turns the remainder off instead of the default mode',
        expect='test_cue_coarse_auto.py::test_the_setting_names_three_modes_and_unset_is_auto',
    ),
    Mutation(
        id='M317',
        tests=('tests/test_cue_coarse_auto.py',),
        target='cue mode — only "true" is on',
        file='cpersona/config.py',
        find='    return "on" if value == "true" else "off"\n',
        replace='    return "on" if value in ("true", "1", "yes", "on") else "off"\n',
        breaks='values that meant off before 2.6.4 start reading the live store',
        expect='test_cue_coarse_auto.py::test_the_setting_names_three_modes_and_unset_is_auto',
    ),
    Mutation(
        id='M318',
        tests=('tests/test_reconstruct_evidence_sequence.py',),
        target='evidence sequence — the budget takes the longest prefix, it does not skip',
        file='cpersona/reconstruct.py',
        find='        if position and used + cost > budget:\n            break\n        used += cost\n        shown[i]',
        replace='        if position and used + cost > budget:\n            continue\n        used += cost\n        shown[i]',
        breaks='a short passage taken at a small budget is removed at a larger one, so raising the budget removes text',
        expect='test_reconstruct_evidence_sequence.py::test_raising_the_budget_never_removes_a_passage',
    ),
    Mutation(
        id='M319',
        tests=('tests/test_reconstruct_evidence_sequence.py',),
        target="evidence sequence — a passage costs what it adds to its record's quote",
        file='cpersona/reconstruct.py',
        find='        cost = _quoted_length(before + [(start, end)]) - _quoted_length(before)\n',
        replace='        cost = end - start\n',
        breaks='the separators are not paid for, so the quotes carry more than the budget and used_budget lies',
        expect='test_reconstruct_evidence_sequence.py::test_used_counts_exactly_what_the_quotes_carry',
    ),
    Mutation(
        id='M320',
        tests=('tests/test_reconstruct_evidence_sequence.py',),
        target='evidence order — the int8 cosine ranks passages across records',
        file='cpersona/reconstruct.py',
        find='        lists.append(cosine_ranks)\n',
        replace='        pass\n',
        breaks='a close passage of a later record never comes before a weak one of an earlier record',
        expect='test_reconstruct_evidence_sequence.py::test_a_close_passage_of_a_later_record_comes_before_a_weak_one_of_an_earlier',
    ),
    Mutation(
        id='M321',
        tests=('tests/test_reconstruct_evidence_sequence.py',),
        target="evidence order — the record's rank is one of the ranks",
        file='cpersona/reconstruct.py',
        find='    lists = [record_ranks, inside_ranks]\n',
        replace='    lists = [inside_ranks]\n',
        breaks="the recall's order of records stops counting, so a weak record's best passage ties the first record's",
        expect='test_reconstruct_evidence_sequence.py::test_without_cosines_the_record_then_the_inside_rank_decides',
    ),
    Mutation(
        id='M322',
        tests=('tests/test_reconstruct_evidence_sequence.py',),
        target='evidence sequence — read only under CPERSONA_RECONSTRUCT_SEQUENCE=evidence or whole',
        file='cpersona/reconstruct.py',
        find='    in_sequence = head_cap > 0 and sequence in (SEQUENCE_EVIDENCE, SEQUENCE_WHOLE)\n',
        replace='    in_sequence = False\n',
        breaks='the setting does nothing: every response keeps the items sequence',
        expect='test_reconstruct_evidence_sequence.py::test_the_evidence_sequence_through_do_reconstruct',
    ),
    Mutation(
        id='M323',
        tests=('tests/test_reconstruct_evidence_sequence.py',),
        target='evidence sequence — an item none of whose passages fit is not returned',
        file='cpersona/reconstruct.py',
        find='        positions = sorted(taken)\n',
        replace='        positions = list(range(len(entries_claims)))\n',
        breaks='items the cut left out come back with a filled head quote, over the budget the sequence kept',
        expect='test_reconstruct_evidence_sequence.py::test_the_evidence_sequence_through_do_reconstruct',
    ),
    Mutation(
        id='M324',
        tests=('tests/test_reconstruct_evidence_sequence.py',),
        target="evidence sequence — the walk's cuts are read by the returned items' positions",
        file='cpersona/reconstruct.py',
        find='    return set().union(*(walk_cuts[p] for p in positions[:returned]))\n',
        replace='    return set().union(*walk_cuts[:returned])\n',
        breaks='bounds.omitted names the cuts of items that were not returned, and misses those of items that were',
        expect='test_reconstruct_evidence_sequence.py::test_walk_cuts_are_read_by_the_returned_items_positions',
    ),
    Mutation(
        id='M325',
        tests=('tests/test_reconstruct_evidence_sequence.py',),
        target='evidence passages — an episode is measured in its stored summary (bug-456)',
        file='cpersona/reconstruct.py',
        find='    text = _stored_text(claim) if block_entry is None else block_entry[0]\n',
        replace='    text = claim.content if block_entry is None else block_entry[0]\n',
        breaks="an episode's passages and ranges point 10 characters right of the text they name",
        expect='test_reconstruct_evidence_sequence.py::test_an_episode_is_measured_in_its_stored_summary',
    ),
    Mutation(
        id='M326',
        tests=('tests/test_reconstruct_evidence_sequence.py',),
        target='evidence passages — a range governing two blocks is offered once',
        file='cpersona/reconstruct.py',
        find='        if any(start < e and s < end for s, e in governed):\n            continue\n        governed.append((start, end))\n',
        replace='        governed.append((start, end))\n',
        breaks='the same text is offered twice, so a quote repeats itself and the budget pays for it twice',
        expect='test_reconstruct_evidence_sequence.py::test_two_blocks_governed_by_one_range_give_one_passage',
    ),
    Mutation(
        id='M327',
        tests=('tests/test_background_read_seam.py',),
        target='background read seam — its own connection',
        file='cpersona/database.py',
        find='    db = await _get_background_read_db()\n',
        replace='    db = await _get_read_db()\n',
        breaks='background reads share the request connection again, so a search waits behind each one',
        expect='test_background_read_seam.py::test_a_request_read_does_not_wait_for_a_background_statement',
    ),
    Mutation(
        id='M328',
        tests=('tests/test_background_read_seam.py',),
        target='background read seam — calibration reads off the request connection',
        file='cpersona/admin_handlers.py',
        find='    async with background_connection() as db:\n        vecs, sample_error = await _sample_embeddings(db, agent_id, sample_n)\n',
        replace='    async with connection() as db:\n        vecs, sample_error = await _sample_embeddings(db, agent_id, sample_n)\n',
        breaks="a search issued during a calibration waits behind each simulate query",
        expect='test_background_read_seam.py::test_calibration_reads_beside_the_requests',
    ),
    Mutation(
        id='M329',
        tests=('tests/test_background_read_seam.py',),
        target='background read seam — the block sweep reads its pages off the request connection',
        file='cpersona/blocks.py',
        find='            async with background_connection() as db:\n                page = await _page(db, kind, page_after, _BACKFILL_PAGE)\n',
        replace='            async with connection() as db:\n                page = await _page(db, kind, page_after, _BACKFILL_PAGE)\n',
        breaks='a search issued during the boot sweep waits behind each page of whole records',
        expect='test_background_read_seam.py::test_block_sweep_pages_are_read_beside_the_requests',
    ),
    Mutation(
        id='M330',
        tests=('tests/test_background_read_seam.py',),
        target='background read seam — the startup agent listing',
        file='cpersona/admin_handlers.py',
        find='        async with background_connection() as db:\n            agent_rows = await db.execute_fetchall(\n',
        replace='        async with connection() as db:\n            agent_rows = await db.execute_fetchall(\n',
        breaks='the scan of every stored row for agent ids runs on the request connection',
        expect='test_background_read_seam.py::test_startup_calibration_lists_agents_on_the_background_seam',
    ),
    Mutation(
        id='M331',
        tests=('tests/test_background_read_seam.py',),
        target='background read seam — close_db closes it (bug-124 class)',
        file='cpersona/database.py',
        find='    if _bg_read_db is not None:\n        if _bg_read_db is not _db:\n            await _bg_read_db.close()\n        _bg_read_db = None\n    _bg_read_db_owner = None\n',
        replace='',
        breaks="close_db leaves the background connection's non-daemon thread running",
        expect='test_background_read_seam.py::test_close_db_closes_the_background_connection',
    ),
    Mutation(
        id='M332',
        tests=('tests/test_background_read_seam.py',),
        target='background read seam — the block sweep counts coverage off the request connection',
        file='cpersona/blocks.py',
        find='    async with background_connection() as db:\n        total, held = await coverage(db, keys)\n',
        replace='    async with connection() as db:\n        total, held = await coverage(db, keys)\n',
        breaks="the sweep's whole-corpus coverage count runs on the request connection",
        expect='test_background_read_seam.py::test_block_sweep_coverage_is_read_beside_the_requests',
    ),
    Mutation(
        id='M333',
        tests=('tests/test_background_read_seam.py',),
        target='background read seam — re-keyed on the write connection',
        file='cpersona/database.py',
        find='    if _bg_read_db is not None and _bg_read_db_owner is write_db:\n        return _bg_read_db\n    async with _read_lock:\n',
        replace='    if _bg_read_db is not None:\n        return _bg_read_db\n    async with _read_lock:\n',
        also=(
            (
                '        if _bg_read_db is not None and _bg_read_db_owner is write_db:\n            return _bg_read_db\n        stale, _bg_read_db',
                '        if _bg_read_db is not None:\n            return _bg_read_db\n        stale, _bg_read_db',
            ),
        ),
        breaks='after the write connection is replaced, background reads keep reading the old database file',
        expect='test_background_read_seam.py::test_background_seam_follows_a_rebooted_database',
    ),
    Mutation(
        id='M334',
        tests=('tests/test_background_read_seam.py',),
        target='background read seam — an in-memory database is shared, not reopened',
        file='cpersona/database.py',
        find='        if DB_PATH == ":memory:":\n            _bg_read_db, _bg_read_db_owner = write_db, write_db\n            return _bg_read_db\n',
        replace='',
        breaks='background reads of an in-memory database open a second, empty database',
        expect='test_background_read_seam.py::test_background_seam_shares_an_in_memory_database',
    ),
    Mutation(
        id='M338',
        tests=('tests/test_client_tool_surface.py',),
        target='client tool surface — the tools loaded with the session',
        file='cpersona/server.py',
        find='ALWAYS_LOADED_TOOLS = ("reconstruct", "store", "archive_episode")\n',
        replace='ALWAYS_LOADED_TOOLS = ("reconstruct", "archive_episode")\n',
        breaks='store is left to a tool search in every session that writes a memory',
        expect='test_client_tool_surface.py::test_the_session_tools_are_loaded_with_the_session_on_the_wire',
    ),
    Mutation(
        id='M339',
        tests=('tests/test_client_tool_surface.py',),
        target='client tool surface — the flag reaches the tool definitions',
        file='cpersona/server.py',
        find='        _tool.meta = {**(_tool.meta or {}), "anthropic/alwaysLoad": True}\n',
        replace='        pass\n',
        breaks='no tool carries anthropic/alwaysLoad, so every definition is deferred again',
        expect='test_client_tool_surface.py::test_the_session_tools_are_loaded_with_the_session_on_the_wire',
    ),
    Mutation(
        id='M340',
        tests=('tests/test_client_tool_surface.py',),
        target="client tool surface — an operator's summary follows CPersona's guidance",
        file='cpersona/server.py',
        find='    return f"{SERVER_INSTRUCTIONS}\\n\\n{operator_summary}"\n',
        replace='    return operator_summary\n',
        breaks="a configured operating context replaces the guidance that says when to use CPersona's tools",
        expect='test_client_tool_surface.py::test_an_operator_summary_follows_cpersonas_guidance',
    ),
    Mutation(
        id='M341',
        tests=('tests/test_client_tool_surface.py',),
        target='client tool surface — the registry is built with the guidance',
        file='cpersona/server.py',
        find='    "cloto-mcp-cpersona", instructions=server_instructions(operating_context.instructions_text())\n',
        replace='    "cloto-mcp-cpersona", instructions=operating_context.instructions_text()\n',
        breaks='with no operating context the initialize instructions are empty again',
        expect='test_client_tool_surface.py::test_the_instructions_open_with_cpersonas_guidance',
    ),
    Mutation(
        id='M335',
        tests=('tests/test_reconstruct_evidence_sequence.py',),
        target='whole sequence — the records shown whole open the order, in item order',
        file='cpersona/reconstruct.py',
        find='    return firsts + [entry for entry in order if not whole[entry[0]]]\n',
        replace='    return order\n',
        breaks='the floor is gone: a long record\'s passages can push a short record that holds the answer out of the budget',
        expect='test_reconstruct_evidence_sequence.py::test_whole_puts_the_records_shown_whole_first_in_item_order',
    ),
    Mutation(
        id='M336',
        tests=('tests/test_reconstruct_evidence_sequence.py',),
        target='whole sequence — the floor is drawn by length, not by rank inside the record',
        file='cpersona/reconstruct.py',
        find='    firsts = sorted((entry for entry in order if whole[entry[0]]), key=lambda entry: entry[0])\n',
        replace='    firsts = sorted((entry for entry in order if entry[1] == 0), key=lambda entry: entry[0])\n',
        breaks='every long record\'s best passage joins the floor, so the budget goes one passage deep on every long record',
        expect='test_reconstruct_evidence_sequence.py::test_whole_puts_the_records_shown_whole_first_in_item_order',
    ),
    Mutation(
        id='M337',
        tests=('tests/test_reconstruct_evidence_sequence.py',),
        target='whole sequence — the setting selects the order behind the length floor',
        file='cpersona/reconstruct.py',
        find='            order = whole_order(\n                record_sets, [basis == "whole" and i < floor_items for i, (_, basis, _) in enumerate(records)]\n            )\n',
        replace='            order = evidence_order(record_sets)\n',
        breaks='CPERSONA_RECONSTRUCT_SEQUENCE=whole answers with the evidence order',
        expect='test_reconstruct_evidence_sequence.py::test_the_whole_sequence_through_do_reconstruct',
    ),
    Mutation(
        id='M342',
        tests=('tests/test_reconstruct_evidence_sequence.py',),
        target='whole sequence — only a record shown whole is a short one',
        file='cpersona/reconstruct.py',
        find='            order = whole_order(\n                record_sets, [basis == "whole" and i < floor_items for i, (_, basis, _) in enumerate(records)]\n            )\n',
        replace='            order = whole_order(\n                record_sets, [basis != "start" and i < floor_items for i, (_, basis, _) in enumerate(records)]\n            )\n',
        breaks='a long record is floored whole-record first, ahead of the short records after it',
        expect='test_reconstruct_evidence_sequence.py::test_the_whole_sequence_through_do_reconstruct',
    ),
    Mutation(
        id='M343',
        tests=('tests/test_reconstruct_evidence_sequence.py',),
        target='whole sequence — a value the setting accepts',
        file='cpersona/config.py',
        find='RECONSTRUCT_SEQUENCES = ("items", "evidence", "whole")\n',
        replace='RECONSTRUCT_SEQUENCES = ("items", "evidence")\n',
        breaks='CPERSONA_RECONSTRUCT_SEQUENCE=whole is rejected and the default is used',
        expect='test_reconstruct_evidence_sequence.py::test_every_sequence_reconstruct_names_is_a_setting_value',
    ),
    Mutation(
        id='M344',
        tests=('tests/test_reconstruct_lite.py',),
        target='lite — the lite budget when the caller names none',
        file='cpersona/reconstruct.py',
        find='    if mode == MODE_LITE and budget is None:\n        budget = LITE_BUDGET\n',
        replace='    if False:\n        budget = LITE_BUDGET\n',
        breaks='lite answers at the default budget, as large as a plain call',
        expect='test_reconstruct_lite.py::test_lite_through_do_reconstruct',
    ),
    Mutation(
        id='M345',
        tests=('tests/test_reconstruct_lite.py',),
        target='lite — the whole sequence whatever the operator set',
        file='cpersona/reconstruct.py',
        find='    sequence = SEQUENCE_WHOLE if mode else config.RECONSTRUCT_SEQUENCE\n',
        replace='    sequence = config.RECONSTRUCT_SEQUENCE\n',
        breaks="lite cuts the operator's sequence, so its quotes are not the measured ones",
        expect='test_reconstruct_lite.py::test_lite_through_do_reconstruct',
    ),
    Mutation(
        id='M346',
        tests=('tests/test_reconstruct_lite.py',),
        target='lite — the shape is applied',
        file='cpersona/reconstruct.py',
        find='    if lite or mode:\n        for key in _LITE_ENVELOPE:\n',
        replace='    if False:\n        for key in _LITE_ENVELOPE:\n',
        breaks='lite returns the full compact shape and saves nothing beyond the budget',
        expect='test_reconstruct_lite.py::test_lite_leaves_the_compact_shape_alone_when_off',
    ),
    Mutation(
        id='M347',
        tests=('tests/test_reconstruct_lite.py',),
        target='lite — ranges are left out',
        file='cpersona/reconstruct.py',
        find='    out = {k: v for k, v in item.items() if k != "ranges"}\n',
        replace='    out = dict(item)\n',
        breaks='every lite item still carries its ranges',
        expect='test_reconstruct_lite.py::test_a_claim_that_says_more_keeps_the_list',
    ),
    Mutation(
        id='M348',
        tests=('tests/test_reconstruct_lite.py',),
        target='lite — only a lone claim moves onto the item',
        file='cpersona/reconstruct.py',
        find='    if claims and len(claims) == 1 and claims[0].get("ref") == out.get("head_ref") and set(claims[0]) <= {"ref", "as_of"}:\n',
        replace='    if claims and claims[0].get("ref") == out.get("head_ref") and set(claims[0]) <= {"ref", "as_of"}:\n',
        breaks='an item of several claims loses all but its head',
        expect='test_reconstruct_lite.py::test_a_claim_that_says_more_keeps_the_list',
    ),
    Mutation(
        id='M349',
        tests=('tests/test_reconstruct_lite.py',),
        target='lite — a claim that says more keeps the list',
        file='cpersona/reconstruct.py',
        find='    if claims and len(claims) == 1 and claims[0].get("ref") == out.get("head_ref") and set(claims[0]) <= {"ref", "as_of"}:\n',
        replace='    if claims and len(claims) == 1 and claims[0].get("ref") == out.get("head_ref"):\n',
        breaks='a claim with a why or roles is dropped, so the item no longer says why it is present',
        expect='test_reconstruct_lite.py::test_a_claim_that_says_more_keeps_the_list',
    ),
    Mutation(
        id='M350',
        tests=('tests/test_reconstruct_lite.py',),
        target='lite — the response says it is lite',
        file='cpersona/reconstruct.py',
        find='        out["lite"] = True\n',
        replace='        pass\n',
        breaks='a reader cannot tell a lite response from a full one missing fields',
        expect='test_reconstruct_lite.py::test_lite_leaves_the_compact_shape_alone_when_off',
    ),
    Mutation(
        id='M351',
        tests=('tests/test_reconstruct_lite.py',),
        target='lite — the boundary forwards it',
        file='cpersona/server.py',
        find='        lite=lite,\n',
        replace='',
        breaks='the tool parameter is accepted and ignored',
        expect='test_reconstruct_lite.py::test_the_tool_forwards_lite',
    ),
    Mutation(
        id='M352',
        tests=('tests/test_reconstruct_lite.py',),
        target='lite — the tool parameter reaches the handler',
        file='cpersona/server.py',
        find='        ("lite", bool, False),\n',
        replace='',
        breaks='the registry drops lite before the handler sees it',
        expect='test_reconstruct_lite.py::test_the_tool_forwards_lite',
    ),
    Mutation(
        id='M353',
        tests=('tests/test_blocks_retrieval.py',),
        target='bug-504 — the examined read counts a record\'s share over admitted rows',
        file='cpersona/blocks.py',
        find='                if taken > BLOCK_PER_PARENT_CAP:\n',
        replace='                if taken >= BLOCK_PER_PARENT_CAP:\n',
        breaks='every record gives one block fewer than its share',
        expect='test_blocks_retrieval.py::test_the_examined_rows_are_the_ones_a_window_over_them_keeps',
    ),
    Mutation(
        id='M354',
        tests=('tests/test_blocks_retrieval.py',),
        target='bug-504 — the examined read walks the primary key and sorts nothing',
        file='cpersona/blocks.py',
        find='        " INDEXED BY sqlite_autoindex_record_blocks_1"\n',
        replace='',
        breaks='the planner reads the agent through the axes index and sorts every row before the first comes back',
        expect='test_blocks_retrieval.py::test_the_examined_read_walks_the_key_without_sorting',
    ),
    Mutation(
        id='M355',
        tests=('tests/test_blocks_rerank.py',),
        target='bug-505 — the stored vectors are read by a join, not a row-value list',
        file='cpersona/blocks.py',
        find='        f"WITH wanted(kind, parent_id, block_index) AS (VALUES {marks}) "\n'
        '        "SELECT v.parent_kind, v.parent_id, v.block_index, v.embedding_i8 FROM wanted"\n'
        '        " JOIN record_block_vectors AS v ON v.parent_kind = wanted.kind"\n'
        '        " AND v.parent_id = wanted.parent_id AND v.block_index = wanted.block_index"\n',
        replace='        "SELECT parent_kind, parent_id, block_index, embedding_i8 FROM record_block_vectors "\n'
        '        f"WHERE (parent_kind, parent_id, block_index) IN (VALUES {marks})"\n',
        breaks='SQLite 3.40 reads every vector in the store to find two hundred',
        expect='test_blocks_rerank.py::test_the_stored_vectors_are_not_read_with_a_row_value_list',
    ),
    Mutation(
        id='M356',
        tests=('tests/test_blocks_rerank.py',),
        target='bug-505 — the join matches the block, not only its parent',
        file='cpersona/blocks.py',
        find='        " AND v.parent_id = wanted.parent_id AND v.block_index = wanted.block_index"\n',
        replace='        " AND v.parent_id = wanted.parent_id"\n',
        breaks="every block of a listed parent is read in, and a parent's other blocks answer for the listed one",
        expect='test_blocks_rerank.py::test_the_stored_vectors_are_exactly_those_of_the_listed_keys',
    ),
    Mutation(
        id='M362',
        tests=('tests/test_concurrent_arms.py',),
        target='the keyword arms run beside the vector arm under rrf',
        file='cpersona/memory_handlers.py',
        find='    lexical = _Beside(_lexical_arms, db, agent_id, query, depth, channel, project_id, source_id, lexical_terms, True)\n    rrf_min_sim',
        replace='    lexical = _Beside(_lexical_arms, db, agent_id, query, depth, channel, project_id, source_id, lexical_terms, True)\n    await lexical.outcome()\n    rrf_min_sim',
        breaks='the keyword arms finish before the query is embedded, and a recall takes their time plus the vector arm\'s',
        expect='test_concurrent_arms.py::test_the_keyword_arm_runs_while_the_vector_arm_does[rrf]',
    ),
    Mutation(
        id='M363',
        tests=('tests/test_concurrent_arms.py',),
        target='the keyword arms run beside the vector arm under rsf',
        file='cpersona/memory_handlers.py',
        find='    lexical = _Beside(_lexical_arms, db, agent_id, query, depth, channel, project_id, source_id, lexical_terms)\n    rsf_min_sim',
        replace='    lexical = _Beside(_lexical_arms, db, agent_id, query, depth, channel, project_id, source_id, lexical_terms)\n    await lexical.outcome()\n    rsf_min_sim',
        breaks='under rsf the keyword arms finish before the query is embedded',
        expect='test_concurrent_arms.py::test_the_keyword_arm_runs_while_the_vector_arm_does[rsf]',
    ),
    Mutation(
        id='M364',
        tests=('tests/test_concurrent_arms.py',),
        target='beside a connection other than the request one, an arm stays on it',
        file='cpersona/database.py',
        find='    if db is _read_db and db is not _db:\n',
        replace='    if db is not _db:\n',
        breaks="a calibration's seconds-long keyword searches move to the side connection and every request's keyword arm queues behind them",
        expect='test_concurrent_arms.py::test_beside_any_other_connection_is_that_connection',
    ),
    Mutation(
        id='M365',
        tests=('tests/test_concurrent_arms.py',),
        target='beside the request connection, an arm runs on the side connection',
        file='cpersona/database.py',
        find='    if db is _read_db and db is not _db:\n',
        replace='    if False:\n',
        breaks="the keyword arms' statements queue on the request connection's thread behind the vector arm's",
        expect='test_concurrent_arms.py::test_beside_the_request_connection_is_the_side_connection',
    ),
    Mutation(
        id='M366',
        tests=('tests/test_concurrent_arms.py',),
        target='the block arm starts when the query vector arrives',
        file='cpersona/memory_handlers.py',
        find='        if not self.arrived.done():\n            self.arrived.set_result(None)\n',
        replace='',
        breaks='the block arm waits for the fusion and the scoring to finish before it starts',
        expect='test_concurrent_arms.py::test_the_block_arm_runs_while_the_keyword_arm_does',
    ),
    Mutation(
        id='M367',
        tests=('tests/test_concurrent_arms.py',),
        target='a block arm that no vector will start is cancelled',
        file='cpersona/memory_handlers.py',
        find='                block_arm.cancel()  # still waiting for a vector that will not come\n',
        replace='                pass\n',
        breaks='every recall without a query vector leaves a task waiting forever',
        expect='test_concurrent_arms.py::test_no_vector_leaves_no_block_arm_waiting',
    ),
    Mutation(
        id='M368',
        tests=('tests/test_concurrent_arms.py',),
        target="an arm running beside raises where it is read, as it did in line",
        file='cpersona/memory_handlers.py',
        find='            return None, exc\n',
        replace='            return ([], []), None\n',
        breaks='a failing keyword or block arm is read as an arm that found nothing',
        expect='test_concurrent_arms.py::test_a_keyword_arm_that_raises_still_fails_the_recall',
    ),
    Mutation(
        id='M369',
        tests=('tests/test_concurrent_arms.py',),
        target='close_db closes the side connection',
        file='cpersona/database.py',
        find='    if _side_read_db is not None:\n        if _side_read_db is not _db:\n            await _side_read_db.close()\n',
        replace='    if _side_read_db is not None:\n',
        breaks="the side connection's worker thread outlives close_db and holds the interpreter open at exit (bug-124)",
        expect='test_concurrent_arms.py::test_close_db_closes_the_side_connection',
    ),
    Mutation(
        id='M370',
        tests=('tests/test_keyword_rare_phrases.py',),
        target='only a phrase of one trigram is classified',
        file='cpersona/memory_handlers.py',
        find='        if len(word) != 3:\n            continue\n',
        replace='        if len(word) < 3:\n            continue\n',
        breaks="a longer phrase is called common from one trigram's count, which is not the phrase's",
        expect='test_keyword_rare_phrases.py::test_only_a_phrase_of_one_trigram_is_classified',
    ),
    Mutation(
        id='M371',
        tests=('tests/test_keyword_rare_phrases.py',),
        target='a phrase in half the rows is at the idf floor',
        file='cpersona/memory_handlers.py',
        find='/ (held.get(term, 0) + 0.5)) <= 0.0\n',
        replace='/ (held.get(term, 0) + 0.5)) < 0.0\n',
        breaks="the floor's edge is missed: idf 0 is floored by FTS5 but not left out here",
        expect='test_keyword_rare_phrases.py::test_a_phrase_in_half_the_rows_is_common_and_one_fewer_is_not',
    ),
    Mutation(
        id='M372',
        tests=('tests/test_keyword_rare_phrases.py',),
        target='the gap at the cut is checked',
        file='cpersona/memory_handlers.py',
        find='any(scores[i] - scores[i + 1] <= bound for i in range(limit))',
        replace='any(scores[i] - scores[i + 1] <= bound for i in range(limit - 1))',
        breaks='a row just past the cut can overtake the last row taken',
        expect='test_keyword_rare_phrases.py::test_a_gap_inside_the_bound_at_the_cut_sends_it_to_the_whole',
    ),
    Mutation(
        id='M373',
        tests=('tests/test_keyword_rare_phrases.py',),
        target='the bound counts every phrase left out',
        file='cpersona/memory_handlers.py',
        find='    bound = COMMON_PHRASE_BOUND * left_out + _BM25_SUM_ROUNDING\n',
        replace='    bound = COMMON_PHRASE_BOUND * 0 + _BM25_SUM_ROUNDING\n',
        breaks='rows closer than the common phrases can move them are taken as ordered',
        expect='test_keyword_rare_phrases.py::test_a_gap_inside_the_bound_among_the_rows_sends_it_to_the_whole',
    ),
    Mutation(
        id='M374',
        tests=('tests/test_keyword_rare_phrases.py',),
        target='rsf, which reads the scores, ranks on the whole expression',
        file='cpersona/memory_handlers.py',
        find='    lexical = _Beside(_lexical_arms, db, agent_id, query, depth, channel, project_id, source_id, lexical_terms)\n    rsf_min_sim',
        replace='    lexical = _Beside(_lexical_arms, db, agent_id, query, depth, channel, project_id, source_id, lexical_terms, True)\n    rsf_min_sim',
        breaks='rsf fuses bm25 scores lowered by up to the bound',
        expect='test_keyword_rare_phrases.py::test_only_a_fusion_that_reads_the_order_asks[rsf-False]',
    ),
    Mutation(
        id='M375',
        tests=('tests/test_keyword_rare_phrases.py',),
        target='a failing shortcut falls back to the whole expression',
        file='cpersona/memory_handlers.py',
        find='                    except sqlite3.OperationalError as e:\n                        # The shortcut failing',
        replace='                    except ZeroDivisionError as e:\n                        # The shortcut failing',
        breaks='the keyword arm drops to the LIKE fallback while the whole expression would have worked',
        expect='test_keyword_rare_phrases.py::test_a_failing_shortcut_ranks_on_the_whole_expression_not_like',
    ),
    Mutation(
        id='M376',
        tests=('tests/test_keyword_rare_phrases.py',),
        target='an ASCII phrase is looked up folded, as the tokenizer folds it',
        file='cpersona/memory_handlers.py',
        find='            candidates[phrase] = word.lower()\n',
        replace='            candidates[phrase] = word\n',
        breaks='a capitalised common word is never found in the vocabulary',
        expect='test_keyword_rare_phrases.py::test_an_ascii_phrase_is_looked_up_as_the_tokenizer_folds_it',
    ),
    Mutation(
        id='M377',
        tests=('tests/test_keyword_rare_phrases.py',),
        target='the cascade reads the order and asks for it',
        file='cpersona/memory_handlers.py',
        find='            extra_terms=lexical_terms, rank_only=True,\n        )',
        replace='            extra_terms=lexical_terms,\n        )',
        breaks='the cascade ranks every common phrase again',
        expect='test_keyword_rare_phrases.py::test_only_a_fusion_that_reads_the_order_asks[cascade-True]',
    ),
    Mutation(
        id='M378',
        tests=('tests/test_keyword_rare_phrases.py',),
        target='rrf reads the order and asks for it',
        file='cpersona/memory_handlers.py',
        find='    lexical = _Beside(_lexical_arms, db, agent_id, query, depth, channel, project_id, source_id, lexical_terms, True)\n    rrf_min_sim',
        replace='    lexical = _Beside(_lexical_arms, db, agent_id, query, depth, channel, project_id, source_id, lexical_terms)\n    rrf_min_sim',
        breaks='rrf ranks every common phrase again',
        expect='test_keyword_rare_phrases.py::test_only_a_fusion_that_reads_the_order_asks[rrf-True]',
    ),
    Mutation(
        id='M379',
        tests=('tests/test_keyword_rare_phrases.py',),
        target='the shortcut returns exactly limit rows',
        file='cpersona/memory_handlers.py',
        find='    return rows[:limit], report\n',
        replace='    return rows, report\n',
        breaks='the arm returns one row past its depth',
        expect='test_keyword_rare_phrases.py::test_the_rows_and_order_are_the_whole_expressions',
    ),
    Mutation(
        id='M380',
        tests=('tests/test_store_flat_form.py',),
        target='a top-level store defaults its source to the writing agent',
        file='cpersona/server.py',
        find='message["source"] = {"type": "Agent", "id": agent_id, "name": ""} if source is _SOURCE_OMITTED else source',
        replace='message["source"] = {} if source is _SOURCE_OMITTED else source',
        breaks='a store that names no producer is stored as anonymous',
        expect='test_store_flat_form.py::test_top_level_fields_are_stored_with_this_agent_as_the_source',
    ),
    Mutation(
        id='M381',
        tests=('tests/test_store_flat_form.py',),
        target='an explicit {} source is kept as the unknown producer',
        file='cpersona/server.py',
        find='if source is _SOURCE_OMITTED else source',
        replace='if not source else source',
        breaks="a caller's explicit {} is replaced by the writing agent",
        expect='test_store_flat_form.py::test_top_level_fields_are_stored_with_this_agent_as_the_source',
    ),
    Mutation(
        id='M382',
        tests=('tests/test_store_flat_form.py',),
        target='the message form keeps its anonymous default',
        file='cpersona/server.py',
        find='    if not message:\n        message = _flat_message(agent_id, content, msg_id, source, timestamp, metadata)',
        replace='    if not message:\n        message = _flat_message(agent_id, content, msg_id, source, timestamp, metadata)\n    else:\n        message = {**message, "source": message.get("source") or {"type": "Agent", "id": agent_id, "name": ""}}',
        breaks="a legacy caller's attribution moves to the writing agent",
        expect='test_store_flat_form.py::test_the_legacy_message_form_keeps_its_anonymous_default',
    ),
    Mutation(
        id='M383',
        tests=('tests/test_store_flat_form.py',),
        target='a call mixing the two forms is refused',
        file='cpersona/server.py',
        find='    if message and flat_given:',
        replace='    if False and message and flat_given:',
        breaks='half of a mixed call is silently dropped',
        expect='test_store_flat_form.py::test_mixing_the_two_forms_is_refused_and_writes_nothing',
    ),
    Mutation(
        id='M384',
        tests=('tests/test_store_flat_form.py',),
        target='an omitted agent_id with no connection agent is refused',
        file='cpersona/server.py',
        find='    if agent_id is None:\n        # Under ACL',
        replace='    if agent_id is None:\n        agent_id = ""\n    if False:\n        # Under ACL',
        breaks='the write lands in the empty-agent bucket',
        expect='test_store_flat_form.py::test_without_acl_an_omitted_agent_id_is_refused_not_written_to_the_empty_bucket',
    ),
    Mutation(
        id='M385',
        tests=('tests/test_store_flat_form.py',),
        target='lock:true locks the stored row',
        file='cpersona/server.py',
        find='    if lock:\n        row_id',
        replace='    if False:\n        row_id',
        breaks='lock:true is accepted and the memory stays unlocked',
        expect='test_store_flat_form.py::test_over_mcp_the_schema_admits_the_top_level_form_and_refuses_a_string_source',
    ),
    Mutation(
        id='M386',
        tests=('tests/test_store_flat_form.py',),
        target='lock:true on a duplicate locks the existing row',
        file='cpersona/server.py',
        find='if isinstance(row_id, int) and row_id > 0:',
        replace='if isinstance(row_id, int) and row_id > 0 and result.get("result") == "stored":',
        breaks='a re-store asking for a lock leaves the row unlocked',
        expect='test_store_flat_form.py::test_lock_on_a_duplicate_locks_the_existing_row',
    ),
    Mutation(
        id='M387',
        tests=('tests/test_store_flat_form.py',),
        target='a wildcard write grant gives no connection agent',
        file='cpersona/acl.py',
        find='    if grants.get(WILDCARD, PERM_NONE) >= PERM_WRITE:\n        return ""',
        replace='    if False:\n        return ""',
        breaks='a client that can write anywhere gets one agent picked for it',
        expect='test_store_flat_form.py::test_connection_agent_is_the_single_writable_agent_or_nothing',
    ),
    Mutation(
        id='M388',
        tests=('tests/test_store_flat_form.py',),
        target='two writable agents give no connection agent',
        file='cpersona/acl.py',
        find='return writable[0] if len(writable) == 1 else ""',
        replace='return writable[0] if len(writable) >= 1 else ""',
        breaks='the first of two writable agents is picked',
        expect='test_store_flat_form.py::test_connection_agent_is_the_single_writable_agent_or_nothing',
    ),
    Mutation(
        id='M389',
        tests=('tests/test_store_flat_form.py',),
        target='an explicit empty agent_id is not an omission',
        file='cpersona/acl.py',
        find='if name in AGENT_FROM_CONNECTION and arguments.get("agent_id") is None:',
        replace='if name in AGENT_FROM_CONNECTION and not arguments.get("agent_id"):',
        breaks='a write addressed to the empty bucket is moved to the connection agent',
        expect='test_store_flat_form.py::test_an_explicit_empty_agent_id_is_not_an_omission_under_acl',
    ),
    Mutation(
        id='M390',
        tests=('tests/test_store_flat_form.py',),
        target='only store takes its agent from the connection',
        file='cpersona/acl.py',
        find='AGENT_FROM_CONNECTION = frozenset({"store"})',
        replace='AGENT_FROM_CONNECTION = frozenset({"store", "recall"})',
        breaks='a read lands in an agent it did not name',
        expect='test_store_flat_form.py::test_only_store_takes_its_agent_from_the_connection',
    ),
    Mutation(
        id='M391',
        tests=('tests/test_store_flat_form.py',),
        target='the connection agent is echoed as resolved_agent_id',
        file='cpersona/acl.py',
        find='            result["resolved_agent_id"] = defaulted_agent',
        replace='            pass',
        breaks='the caller cannot learn which agent it wrote to',
        expect='test_store_flat_form.py::test_an_omitted_agent_id_lands_in_the_one_agent_the_connection_can_write',
    ),
    Mutation(
        id='M392',
        tests=('tests/test_store_flat_form.py',),
        target="a per-subject client's omission means its own alias",
        file='cpersona/acl.py',
        find='            if boundary:\n                arguments = {**arguments, "agent_id": SELF_SENTINEL}',
        replace='            if False:\n                arguments = {**arguments, "agent_id": SELF_SENTINEL}',
        breaks='a per-subject client must name an alias it cannot know',
        expect='test_store_flat_form.py::test_a_per_subject_connection_writes_to_its_own_alias',
    ),
    Mutation(
        id='M393',
        tests=('tests/test_store_flat_form.py',),
        target='only write grants count toward the connection agent',
        file='cpersona/acl.py',
        find='if agent != WILDCARD and level >= PERM_WRITE]',
        replace='if agent != WILDCARD and level >= PERM_READ]',
        breaks='a read-only agent is picked as the write target',
        expect='test_store_flat_form.py::test_connection_agent_is_the_single_writable_agent_or_nothing',
    ),
    Mutation(
        id='M394',
        tests=('tests/test_store_flat_form.py',),
        target='the top-level timestamp is carried to the row',
        file='cpersona/server.py',
        find='    if timestamp:\n        message["timestamp"] = timestamp',
        replace='    if False:\n        message["timestamp"] = timestamp',
        breaks="a caller's timestamp is replaced by the server clock",
        expect='test_store_flat_form.py::test_top_level_fields_are_stored_with_this_agent_as_the_source',
    ),
    Mutation(
        id='M395',
        tests=('tests/test_store_flat_form.py',),
        target='the top-level id is carried as msg_id',
        file='cpersona/server.py',
        find='    if msg_id:\n        message["id"] = msg_id',
        replace='    if False:\n        message["id"] = msg_id',
        breaks='id-based dedup silently stops working',
        expect='test_store_flat_form.py::test_top_level_fields_are_stored_with_this_agent_as_the_source',
    ),
    Mutation(
        id='M402',
        tests=('tests/test_policy_block_call_shapes.py',),
        target="policy block call shapes — an argument the block names is one the tool declares",
        file='cpersona/server.py',
        find='            "ttl_seconds": {\n                "type": "integer",\n                "description": "TTL until automatic resume.',
        replace='            "ttl": {\n                "type": "integer",\n                "description": "TTL until automatic resume.',
        breaks="the block keeps telling agents pause_persistence(ttl_seconds=1800) after the tool stopped taking it",
        expect='test_policy_block_call_shapes.py::test_every_argument_a_shape_names_is_declared',
    ),
    Mutation(
        id='M403',
        tests=('tests/test_bug439_quick_check_stale_fts_cache.py',),
        target="check_fts_integrity — the enhanced integrity-check failing marks the index corrupt",
        file='cpersona/checks.py',
        find='            except sqlite3.DatabaseError:\n                corrupt = True\n            await _release_probe_transaction()',
        replace='            except sqlite3.DatabaseError:\n                corrupt = False\n            await _release_probe_transaction()',
        breaks="overwritten index blocks go unreported on SQLite whose quick_check does not inspect FTS5",
        expect='test_bug439_quick_check_stale_fts_cache.py::test_a_damaged_index_is_reported_by_the_fts_check_whatever_quick_check_sees',
    ),
    Mutation(
        id='M404',
        tests=('tests/test_keyword_rare_phrases.py',),
        target="common phrases — the rows holding a phrase are counted with the phrase",
        file='cpersona/memory_handlers.py',
        find='_PHRASE_ROWS_SQL = "SELECT count(*) FROM memories_fts WHERE memories_fts MATCH ?"\n',
        replace='_PHRASE_ROWS_SQL = "SELECT count(*) FROM memories_fts_docsize WHERE ? IS NOT NULL"\n',
        breaks="every phrase counts as held by every row, so a rare word is left out of the ranking as if it were common",
        expect='test_keyword_rare_phrases.py::test_the_counts_classify_each_phrase_as_the_index_vocabulary_does',
    ),
    Mutation(
        id='M405',
        tests=('tests/test_keyword_rare_phrases.py',),
        target="common phrases — a phrase is quoted before it is matched",
        file='cpersona/memory_handlers.py',
        find="        quoted = '\"' + term.replace('\"', '\"\"') + '\"'\n",
        replace="        quoted = term\n",
        breaks="a three-character word holding a quote is matched as query syntax and the classification fails",
        expect='test_keyword_rare_phrases.py::test_the_counts_classify_each_phrase_as_the_index_vocabulary_does',
    ),
    # ---------------------------------------------------------------------------
    # The block index file (docs/BLOCK_CANDIDATES_CONTRACT.md §3.4): the rows read
    # through it are the rows the SQLite read returns.
    # ---------------------------------------------------------------------------
    Mutation(
        id='M406',
        tests=('tests/test_block_index.py',),
        target="block index — a changed record's rows in the file are dropped",
        file='cpersona/block_index.py',
        find="                mask &= ~((kind == code) & np.isin(index.parent_id[sl], ids))\n",
        replace="                mask &= mask\n",
        breaks="a record rebuilt, deleted or retagged since the build is read twice, or as it was",
        expect='test_block_index.py::test_random_writes_after_a_build_read_the_same_rows',
    ),
    Mutation(
        id='M407',
        tests=('tests/test_block_index.py',),
        target="block index — a record's count runs on across the pieces the file is read in",
        file='cpersona/block_index.py',
        find="            place[:run] += last_count\n",
        replace="            place[:run] += 0\n",
        breaks="a record that runs across a piece's edge takes more than its share of the examined cap",
        expect='test_block_index.py::test_the_golden_through_the_file',
    ),
    Mutation(
        id='M408',
        tests=('tests/test_block_index.py',),
        target="block index — the admission filter sits in the join",
        file='cpersona/block_index.py',
        find='        f" AND b.embedding_bits IS NOT NULL AND b.embedding_model IN (?, ?){iso.and_clause}"\n',
        replace='        f" WHERE b.embedding_bits IS NOT NULL AND b.embedding_model IN (?, ?){iso.and_clause}"\n',
        breaks="a changed record left with no admitted rows vanishes from the read, and its old rows in the file are kept",
        expect='test_block_index.py::test_random_writes_after_a_build_read_the_same_rows',
    ),
    Mutation(
        id='M409',
        tests=('tests/test_block_index.py',),
        target="block index — a file newer than the snapshot is refused",
        file='cpersona/block_index.py',
        find="    if index.built_seq > head:\n        return _refuse(\"newer_than_snapshot\")\n",
        replace="    if False:\n        return _refuse(\"newer_than_snapshot\")\n",
        breaks="an old snapshot read with a newer file passes every other guard and returns rows the snapshot does not hold",
        expect='test_block_index.py::test_a_file_newer_than_the_snapshot_is_refused',
    ),
    Mutation(
        id='M410',
        tests=('tests/test_block_index.py',),
        target="block index — the pruning mark only rises",
        file='cpersona/block_index.py',
        find='            "UPDATE block_log_clock SET pruned_through = max(pruned_through, ?) WHERE id = 0",\n',
        replace='            "UPDATE block_log_clock SET pruned_through = ? WHERE id = 0",\n',
        breaks="a slower, older build finishing second lowers the mark, and a file whose changes were pruned reads as current",
        expect='test_block_index.py::test_pruning_never_lowers_the_mark',
    ),
    Mutation(
        id='M411',
        tests=('tests/test_block_index.py',),
        target="block index — a build without every logging trigger is declined",
        file='cpersona/block_index.py',
        find="    differ = await _log_triggers_differ(snap)\n    if differ:\n",
        replace="    differ = await _log_triggers_differ(snap)\n    if False:\n",
        breaks="a file is built while writes go unlogged, and the cookie compared later cannot see that it began incomplete",
        expect='test_block_index.py::test_a_build_without_every_logging_trigger_declines',
    ),
    Mutation(
        id='M412',
        tests=('tests/test_reconstruct_modes.py',),
        target="reconstruct sequences — excerpts follow every passage (invariant 9)",
        file='cpersona/reconstruct.py',
        find='                allowance = used_passages\n',
        replace='                pass\n',
        breaks="excerpts take what the passages leave of the budget again, so a larger budget that takes one more passage takes an excerpt away",
        expect='test_reconstruct_modes.py::test_raising_the_budget_never_takes_an_excerpt_away',
    ),
    Mutation(
        id='M413',
        tests=('tests/test_reconstruct_modes.py',),
        target="mode pro — only the first items join the floor of records shown whole",
        file='cpersona/reconstruct.py',
        find='floor_items = PRO_WHOLE_FLOOR_ITEMS if mode == MODE_PRO else len(records)',
        replace='floor_items = len(records)',
        breaks="on a store of short records, pro spends its cap on the short records of the last items",
        expect='test_reconstruct_modes.py::test_only_the_first_items_of_pro_join_the_floor_of_whole_records',
    ),
    Mutation(
        id='M414',
        tests=('tests/test_reconstruct_modes.py',),
        target="mode — the cap is searched for",
        file='cpersona/reconstruct.py',
        find='        if shaped["used_tokens"] > limit:\n',
        replace='        if False:\n',
        breaks="a response over its cap is returned as it is",
        expect='test_reconstruct_modes.py::test_the_cap_holds_counts_itself_and_returns_the_longest_prefix',
    ),
    Mutation(
        id='M415',
        tests=('tests/test_reconstruct_modes.py',),
        target="mode — the longest prefix that fits, not the shortest",
        file='cpersona/reconstruct.py',
        find='                    fits, low = (attempt, attempt_quoted, attempt_shaped), middle + 1\n',
        replace='                    fits, high = (attempt, attempt_quoted, attempt_shaped), middle - 1\n',
        breaks="a cap returns less than it holds",
        expect='test_reconstruct_modes.py::test_the_cap_holds_counts_itself_and_returns_the_longest_prefix',
    ),
    Mutation(
        id='M416',
        tests=('tests/test_reconstruct_modes.py',),
        target="mode — the search reaches the prefix one step under the whole sequence",
        file='cpersona/reconstruct.py',
        find='            low, high = 0, len(ends) - 2\n',
        replace='            low, high = 0, len(ends) - 3\n',
        breaks="a cap just under the whole response drops two steps where one would fit",
        expect='test_reconstruct_modes.py::test_one_token_under_the_whole_response_drops_exactly_one_step',
    ),
    Mutation(
        id='M417',
        tests=('tests/test_reconstruct_modes.py',),
        target="mode — the boundary's own fields count toward the cap",
        file='cpersona/reconstruct.py',
        find='            out.update(envelope or {})\n',
        replace='',
        breaks="the fields the tool boundary adds after the count push a response past its cap, and used_tokens understates it",
        expect='test_reconstruct_modes.py::test_the_cap_counts_what_the_tool_boundary_adds',
    ),
    Mutation(
        id='M418',
        tests=('tests/test_reconstruct_modes.py',),
        target="mode — used_tokens counts its own digits",
        file='cpersona/reconstruct.py',
        find='    for _ in range(4):\n        n = tokens.count_json(out)\n        if n == out["used_tokens"]:\n            return n\n        out["used_tokens"] = n\n',
        replace='    n = tokens.count_json(out)\n    out["used_tokens"] = n\n    return n\n',
        breaks="used_tokens is the count of the response before its own value was written",
        expect='test_reconstruct_modes.py::test_used_tokens_counts_its_own_digits',
    ),
    Mutation(
        id='M419',
        tests=('tests/test_reconstruct_modes.py',),
        target="mode pro — the window reaches past the configured maximum",
        file='cpersona/reconstruct.py',
        find='        maximum, default = max(maximum, PRO_COUNT), PRO_COUNT\n',
        replace='        maximum, default = maximum, PRO_COUNT\n',
        breaks="pro is clamped to the configured maximum and reads no more items than the default",
        expect='test_reconstruct_modes.py::test_pro_widens_the_window_past_the_configured_maximum',
    ),
    Mutation(
        id='M420',
        tests=('tests/test_reconstruct_modes.py',),
        target="mode — lite and mode together are refused",
        file='cpersona/reconstruct.py',
        find='    if mode is not None and (lite or mode not in MODE_CAPS):\n',
        replace='    if mode is not None and mode not in MODE_CAPS:\n',
        breaks="lite=true with mode pro is answered as pro without a word",
        expect='test_reconstruct_modes.py::test_lite_true_is_mode_lite_and_both_together_are_refused',
    ),
    Mutation(
        id='M421',
        tests=('tests/test_reconstruct_modes.py',),
        target="tokens — digits split in threes, as cl100k_base does",
        file='cpersona/tokens.py',
        find='\\p{N}{1,3}+|',
        replace='\\p{N}+|',
        breaks="a long number counts differently from the tokenizer the cap is defined by",
        expect='test_reconstruct_modes.py::test_the_shipped_vocabulary_counts_as_tiktoken_does',
    ),
    Mutation(
        id='M422',
        tests=('tests/test_reconstruct_modes.py',),
        target="mode — the trace is not held to the cap",
        file='cpersona/reconstruct.py',
        find='_compact({k: v for k, v in response.items() if k != "trace"}, ',
        replace='_compact(response, ',
        breaks="trace=true counts the audit toward the cap and returns fewer items than the same call without it",
        expect='test_reconstruct_modes.py::test_pro_widens_the_window_past_the_configured_maximum',
    ),
    Mutation(
        id='M423',
        tests=('tests/test_reconstruct_modes.py',),
        target="reconstruct tool — mode reaches do_reconstruct",
        file='cpersona/server.py',
        find='        mode=mode,\n        # What _oc_annotate',
        replace='        # What _oc_annotate',
        breaks="the tool ignores mode and answers with the uncapped default",
        expect='test_reconstruct_modes.py::test_the_cap_counts_what_the_tool_boundary_adds',
    ),
    Mutation(
        id='M424',
        tests=('tests/test_keyword_seats.py',),
        target="keyword seats — the seat holds the row the rrf gate cannot pass",
        file='cpersona/memory_handlers.py',
        find='and (_rid_of(r) in passed_gate or _gate_score(r)[1] == "rrf")',
        replace='and _rid_of(r) in passed_gate',
        breaks="a keyword-only row refused on its rrf score never takes a seat, so the seat brings in nothing the gate dropped",
        expect='test_keyword_seats.py::test_a_seat_holds_the_row_the_rrf_gate_cannot_pass',
    ),
    Mutation(
        id='M425',
        tests=('tests/test_keyword_seats.py',),
        target="keyword seats — only rows the keyword arms alone found",
        file='cpersona/memory_handlers.py',
        find='if r.get("_lexical_only") and _rid_of(r) not in present',
        replace='if _rid_of(r) not in present',
        breaks="a row the vector arm found and the gate refused on cosine re-enters through a keyword seat",
        expect='test_keyword_seats.py::test_a_row_refused_on_the_rsf_scale_takes_no_seat',
    ),
    Mutation(
        id='M426',
        tests=('tests/test_keyword_seats.py',),
        target="keyword seats — the row holding more of the question sits first",
        file='cpersona/memory_handlers.py',
        find='            eligible = sorted(eligible, key=lambda row: -_parts_held(row))\n',
        replace='',
        breaks="the seats fill in the gate's order and the row holding more of the question loses its seat",
        expect='test_keyword_seats.py::test_the_seat_goes_to_the_row_holding_more_of_the_question',
    ),
    Mutation(
        id='M427',
        tests=('tests/test_keyword_seats.py',),
        target="keyword seats — at most the number asked for",
        file='cpersona/memory_handlers.py',
        find='        seated = eligible[:keyword_seats]\n',
        replace='        seated = eligible\n',
        breaks="every eligible row takes a seat and the answer grows without bound",
        expect='test_keyword_seats.py::test_seats_hold_at_most_their_number',
    ),
    Mutation(
        id='M428',
        tests=('tests/test_keyword_seats.py',),
        target="reconstruct — a mode asks for the keyword seat",
        file='cpersona/reconstruct.py',
        find='        **({"keyword_seats": MODE_KEYWORD_SEATS} if mode else {}),\n',
        replace='',
        breaks="lite and pro answer without the keyword seat",
        expect='test_keyword_seats.py::test_reconstruct_holds_the_seat_beside_its_window',
    ),
    Mutation(
        id='M429',
        tests=('tests/test_keyword_seats.py',),
        target="reconstruct — no mode, no seat",
        file='cpersona/reconstruct.py',
        find='**({"keyword_seats": MODE_KEYWORD_SEATS} if mode else {}),',
        replace='**({"keyword_seats": MODE_KEYWORD_SEATS}),',
        breaks="a call that names no mode gains a seat and stops returning what 2.6.8 returned",
        expect='test_keyword_seats.py::test_reconstruct_without_a_mode_holds_no_seat',
    ),
    Mutation(
        id='M430',
        tests=('tests/test_keyword_seats.py',),
        target="keyword seats — the do_recall parameter reaches the recall",
        file='cpersona/memory_handlers.py',
        find='        **({"keyword_seats": keyword_seats} if keyword_seats else {}),\n        **({"query_vec_out_": query_vec_out}',
        replace='        **({"query_vec_out_": query_vec_out}',
        breaks="a caller's keyword_seats is dropped before the recall runs and no seat is ever held",
        expect='test_keyword_seats.py::test_a_seat_holds_the_row_the_rrf_gate_cannot_pass',
    ),
]


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, **kw)


def tree_is_clean(files: set[str]) -> bool:
    # bug-283: staged edits count. `git diff` alone reports only the unstaged set, so a
    # target file that was edited and then `git add`ed reads as clean, and the
    # run measures mutants applied on top of code nobody is shipping — while
    # reporting every seam pinned. The failure is quiet in the green direction,
    # which is the one direction this guard exists to rule out.
    out = run(["git", "diff", "--name-only"]).stdout.split()
    out += run(["git", "diff", "--cached", "--name-only"]).stdout.split()
    dirty = files & set(out)
    if dirty:
        print(f"!! working tree has uncommitted changes in target files: {sorted(dirty)}")
        return False
    return True


def forget_bytecode(path: Path) -> None:
    """Remove every cached bytecode file for `path`, whatever the interpreter.

    Python trusts a cached .pyc while the source's size and mtime match what the
    .pyc recorded, and the mtime is kept in whole seconds. A mutant is usually
    one token wide, so two mutants of one file -- or a mutant and the restored
    original -- are often the same size, and a targeted run can finish inside a
    second. The next run then imports the bytecode of the PREVIOUS text: a
    mutant is judged by code it did not contain, and a CAUGHT can belong to the
    mutant before it. Measured on a hand-run harness of this shape: two
    one-digit mutants reported each other's failing assertion.
    """
    cached = Path(importlib.util.cache_from_source(str(path)))
    for pyc in cached.parent.glob(f"{path.stem}.*.pyc"):
        pyc.unlink(missing_ok=True)


def restore_mutation(m: Mutation, original: str) -> None:
    """Write the original text back, and forget the mutant's bytecode with it."""
    path = REPO / m.file
    path.write_text(original)
    forget_bytecode(path)


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
    forget_bytecode(path)
    return original


# pytest exit codes a TARGETED run may count as caught: tests failed (1), or the
# run was interrupted, which is how a mutant that breaks an import surfaces (2).
# "No tests collected" (5), internal (3) and usage (4) errors say nothing about
# the mutant, so they fall through to the full suite instead of becoming CAUGHT.
TARGETED_CAUGHT_CODES = frozenset({1, 2})


@dataclass(frozen=True)
class Run:
    """One pytest run: its exit code and the node ids its summary named as failed."""

    code: int
    failed: tuple[str, ...] = ()


# `-rfE` prints one "FAILED <node id> - <message>" or "ERROR <node id> - <message>" line
# per failure; the message is optional and a node id may contain spaces inside [...].
_SUMMARY_LINE = re.compile(r"^(?:FAILED|ERROR) (\S+?(?:\[.*?\])?)(?: - .*)?$")


def failed_node_ids(output: str) -> tuple[str, ...]:
    """The node ids pytest's short summary names as failed or errored, in order."""
    return tuple(m.group(1) for m in map(_SUMMARY_LINE.match, output.splitlines()) if m)


def _as_run(result) -> Run:
    # A runner may answer with a bare exit code; it then names no failures.
    return result if isinstance(result, Run) else Run(int(result))


def verdict(m: Mutation, run_tests, report: list | None = None) -> tuple[bool, str]:
    """Whether the test suite catches the applied mutant, and which run decided.

    `run_tests(paths)` runs pytest over `paths` (all tests when empty) and returns
    its exit code, or a `Run` naming the tests that failed. The full suite decides
    unless the mutant's own test files are already red: an equivalent mutant must
    survive the WHOLE suite, so it never takes the shortcut.

    A red full run is only a verdict when it reproduces (see `_full_verdict`).
    `report`, when given, collects ("failed" | "flaky", node ids) entries for the
    caller to print.
    """
    if m.tests and not m.equivalent:
        if _as_run(run_tests(list(m.tests))).code in TARGETED_CAUGHT_CODES:
            return True, "targeted"
    return _full_verdict(run_tests, report), "full"


def _full_verdict(run_tests, report: list | None) -> bool:
    """Run the whole suite; a red run counts only if its failure reproduces.

    The full suite stops at its first failure (-x), and any test that fails once
    for a reason unrelated to the mutant used to decide the verdict on its own:
    an equivalent mutant read as OVER-PINNED, and a behavioural one as CAUGHT --
    the direction that hides a survivor. So the failing tests are run again with
    the mutant still applied. If they fail again, the mutant is caught. If they
    pass, they are set aside as flaky and the rest of the suite decides. A red
    run that names no test, or a rerun that cannot answer (nothing collected,
    usage or internal error), keeps the conservative verdict: caught.
    """
    first = _as_run(run_tests([]))
    if first.code == 0:
        return False
    if not first.failed:
        return True
    if report is not None:
        report.append(("failed", first.failed))
    again = _as_run(run_tests(list(first.failed)))
    if again.code != 0:
        return True
    if report is not None:
        report.append(("flaky", first.failed))
    rest = _as_run(run_tests([], deselect=first.failed))
    if rest.code != 0 and rest.failed and report is not None:
        report.append(("failed", rest.failed))
    return rest.code != 0


def run_pytest(paths: list[str], deselect: tuple[str, ...] = ()) -> Run:
    """pytest over `paths` (the whole suite when empty), stopping at the first failure."""
    # -x: the first failure is enough to prove the mutant is caught.
    cmd = ["uv", "run", "pytest", "-q", "-x", "-rfE", *paths]
    for node_id in deselect:
        cmd += ["--deselect", node_id]
    proc = run(cmd)
    return Run(proc.returncode, failed_node_ids(proc.stdout))


def missing_test_files(selected: list[Mutation]) -> list[str]:
    return sorted({f"{m.id}: {t}" for m in selected for t in m.tests if not (REPO / t).is_file()})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", help="run a single mutation by id")
    args = ap.parse_args()

    selected = [m for m in MUTATIONS if not args.id or m.id == args.id]
    if not selected:
        raise SystemExit(f"no mutation matches --id {args.id}")

    if not tree_is_clean({m.file for m in selected}):
        return 2

    missing = missing_test_files(selected)
    if missing:
        print("!! mutation test files do not exist — update the `tests` field:")
        for line in missing:
            print(f"   {line}")
        return 2

    print(f"Baseline: running the suite unmutated ({len(selected)} mutations queued)...")
    base = run(["uv", "run", "pytest", "-q", "-x"])
    if base.returncode != 0:
        print("!! the suite is already failing — fix that first; mutation results would be meaningless")
        print(base.stdout[-2000:])
        return 2
    print("Baseline green.\n")

    survived: list[Mutation] = []
    flaky: list[tuple[str, str]] = []
    for m in selected:
        original = apply_mutation(m)
        report: list = []
        try:
            caught, decided_by = verdict(m, run_pytest, report)
        finally:
            restore_mutation(m, original)

        if m.equivalent:
            # Inverted expectation: an equivalent mutant that gets CAUGHT means a
            # test is asserting the redundant layer itself, which will break the
            # moment someone legitimately simplifies it.
            status = "OVER-PINNED" if caught else "EQUIVALENT "
        else:
            status = "CAUGHT     " if caught else "SURVIVED   "
        print(f"[{status}] {m.id}  {m.target}")
        print(f"           {m.breaks}")
        print(f"           decided by: {decided_by} run")
        for kind, node_ids in report:
            for node_id in node_ids:
                if kind == "failed":
                    print(f"           failed: {node_id}")
                else:
                    flaky.append((m.id, node_id))
                    print(f"           !! flaky: {node_id} passed when run again with the mutant applied;")
                    print("              it was set aside and the rest of the suite decided")
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
    if flaky:
        # A flaky test does not fail this run -- it did not decide any verdict -- but it
        # is reported where a reader will see it, as an annotation on CI.
        print("\nFlaky tests set aside (each failed once, then passed with the mutant still applied):")
        for mid, node_id in flaky:
            print(f"  {node_id}  (while judging {mid})")
            if os.environ.get("GITHUB_ACTIONS") == "true":
                print(f"::warning title=mutation-proof: flaky test::{node_id} failed once while judging {mid} and passed on a rerun")
    if survived:
        print("\nUnresolved — address these BEFORE refactoring the named seam:")
        for m in survived:
            print(f"  {m.id}  {m.target}\n       {m.breaks}")
        return 1
    print("\nEvery seam is pinned. The refactor has a real safety net.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
