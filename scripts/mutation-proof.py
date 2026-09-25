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
        find='    if omitted:\n        bounds["omitted"] = omitted',
        replace='    if False:\n        bounds["omitted"] = omitted',
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
        find="    uf = bundle(candidates, spans, links)",
        replace="""    async with connection() as _mutant_db:
        await _mutant_db.execute(
            "UPDATE memories SET content = content || ' (touched)' WHERE agent_id = ?", (agent_id,)
        )
        await _mutant_db.commit()
    uf = bundle(candidates, spans, links)""",
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
            "        item, _ = structure(rows, why_by_ref, spans, bounds_max_evidence, extra, links)\n"
            "        if position >= len(window):\n"
            "            # Said in the words recall uses for the same row, so one reading covers both.\n"
            "            item[\"admission\"] = \"reservation\"\n"
            "        selected.append(item)"
        ),
        replace=(
            "        item, _ = structure(rows, why_by_ref, spans, bounds_max_evidence, (), links)\n"
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
        find='    if config.CONFIDENCE_ENABLED and config.CONFIDENCE_ORDERING == "legacy":\n        return "confidence"\n',
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
        find='cue.lift(results, cue_rank, cue.LIFT[cue_note["confidence"]], _rid_of)',
        replace="cue.lift(results, cue_rank, 10, _rid_of)",
        breaks="a wrong cue can carry a row from the bottom of the answer to the top, so its harm is no longer bounded by construction",
        expect="test_recall_cue.py::test_a_cue_changes_order_not_admission",
    ),
    Mutation(
        id="M61",
        tests=("tests/test_recall_cue.py",),
        target="time cue — the held seat is for records no ordinary arm reached",
        file="cpersona/memory_handlers.py",
        find='r for r in cue_rows if r["_rid"] not in reached and r["_rid"] not in present]',
        replace='r for r in cue_rows if r["_rid"] not in present]',
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


def verdict(m: Mutation, run_tests) -> tuple[bool, str]:
    """Whether the test suite catches the applied mutant, and which run decided.

    `run_tests(paths)` runs pytest over `paths` (all tests when empty) and returns
    its exit code. The full suite decides unless the mutant's own test files
    are already red: an equivalent mutant must survive the WHOLE suite, so it
    never takes the shortcut.
    """
    if m.tests and not m.equivalent:
        if run_tests(list(m.tests)) in TARGETED_CAUGHT_CODES:
            return True, "targeted"
    return run_tests([]) != 0, "full"


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
    for m in selected:
        original = apply_mutation(m)
        try:
            # -x: the first failure is enough to prove the mutant is caught.
            caught, decided_by = verdict(
                m, lambda paths: run(["uv", "run", "pytest", "-q", "-x", *paths]).returncode
            )
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
