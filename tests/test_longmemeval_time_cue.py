"""The LongMemEval time cue instrument (``benchmarks/longmemeval_time_cue.py``).

What the verdict rests on is tested with values, not shapes: the times a session and a question are read at, the
moved cues the control arms send, the per-question scores, the sign-flip p, and each precondition that voids a run.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "benchmarks"


def _load():
    if str(BENCH) not in sys.path:
        sys.path.insert(0, str(BENCH))
    spec = importlib.util.spec_from_file_location("longmemeval_time_cue_under_test", BENCH / "longmemeval_time_cue.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


T = _load()
UTC = dt.timezone.utc


# --- time ------------------------------------------------------------------------

def test_title_instant_reads_the_twelve_hour_clock_as_utc():
    assert T.title_instant("Data time: 12:04 AM on Saturday 20 May, 2023 - Session 1") == dt.datetime(
        2023, 5, 20, 0, 4, tzinfo=UTC)
    assert T.title_instant("Data time: 03:22 PM on Monday 29 May, 2023 - Session 390") == dt.datetime(
        2023, 5, 29, 15, 22, tzinfo=UTC)


def test_title_without_a_time_is_an_error():
    with pytest.raises(ValueError):
        T.title_instant("Session 3")


def test_question_instant():
    assert T.question_instant("2023/04/10 (Mon) 23:07") == dt.datetime(2023, 4, 10, 23, 7, tzinfo=UTC)


def test_stopped_clock_is_what_the_recall_path_reads():
    import cpersona.memory_handlers as mh

    real = mh.datetime
    try:
        patched = T.stop_clock()
        assert "cpersona.memory_handlers" in patched
        T.Clock.at = dt.datetime(2023, 4, 10, 23, 7, tzinfo=UTC)
        assert mh.datetime.now(UTC) == T.Clock.at
        T.Clock.at = None
        assert abs((mh.datetime.now(UTC) - dt.datetime.now(UTC)).total_seconds()) < 5
    finally:
        T.Clock.at = None
        for name, mod in list(sys.modules.items()):
            if name.startswith("cpersona") and getattr(mod, "datetime", None) is T._StoppedDatetime:
                mod.datetime = real


# --- the moved cues --------------------------------------------------------------

def _cue():
    from cpersona import cue

    return cue


NOW = dt.datetime(2023, 3, 10, 23, 15, tzinfo=UTC)
FEB = {"after": "2023-02-01", "before": "2023-02-28", "confidence": "likely"}  # [Feb 1, Mar 1), 28 days


def test_half_moves_the_period_half_its_length_into_the_past():
    out = T.moved_cue(_cue(), FEB, NOW, (dt.datetime(2022, 1, 1, tzinfo=UTC), NOW), "half")
    assert out == {"after": "2023-01-18T00:00:00+00:00", "before": "2023-02-15T00:00:00+00:00",
                   "confidence": "likely"}


def test_shifted_clears_both_widened_periods():
    # likely widens by half the length on each side: 28 days -> distance 28 + 2 * 14 = 56 days.
    out = T.moved_cue(_cue(), FEB, NOW, (dt.datetime(2022, 1, 1, tzinfo=UTC), NOW), "shifted")
    assert out == {"after": "2022-12-07T00:00:00+00:00", "before": "2023-01-04T00:00:00+00:00",
                   "confidence": "likely"}


def test_shifted_goes_to_the_future_when_the_past_is_before_the_span():
    span = (dt.datetime(2023, 1, 1, 12, tzinfo=UTC), NOW)
    sure = {"after": "2023-01-02", "before": "2023-01-02", "confidence": "sure"}  # one day, no margin
    out = T.moved_cue(_cue(), sure, NOW, span, "shifted")
    assert out == {"after": "2023-01-03T00:00:00+00:00", "before": "2023-01-04T00:00:00+00:00",
                   "confidence": "sure"}


def test_shifted_is_none_without_a_disjoint_place():
    span = (dt.datetime(2023, 1, 20, tzinfo=UTC), NOW)
    assert T.moved_cue(_cue(), FEB, NOW, span, "shifted") is None


def test_shifted_to_the_future_must_clear_the_widened_periods_too():
    # Past impossible (the span starts after Dec 7). The moved period ends Apr 26, before now (Apr 30), but widened by
    # its margin it would reach past now, so there is no disjoint place.
    now = dt.datetime(2023, 4, 30, tzinfo=UTC)
    assert T.moved_cue(_cue(), FEB, now, (dt.datetime(2023, 1, 20, tzinfo=UTC), now), "shifted") is None
    later = dt.datetime(2023, 5, 25, tzinfo=UTC)
    assert T.moved_cue(_cue(), FEB, later, (dt.datetime(2023, 1, 20, tzinfo=UTC), later), "shifted") == {
        "after": "2023-03-29T00:00:00+00:00", "before": "2023-04-26T00:00:00+00:00", "confidence": "likely"}


def test_no_cue_moves_to_no_cue():
    assert T.moved_cue(_cue(), None, NOW, (None, None), "shifted") is None


# --- scoring ---------------------------------------------------------------------

def test_score_counts_a_row_past_the_limit_at_its_place():
    import math

    returned = [f"d{i}" for i in range(10)] + ["ev"]  # the evidence is the held seat, 11th
    s = T.score(returned, {"ev"})
    assert s["rr"] == pytest.approx(1 / 11)
    assert s["ndcg"] == pytest.approx(1 / math.log2(12))
    assert s["all_returned"] and s["any_returned"] and s["in_top5"] == 0


def test_score_normalises_by_all_evidence():
    import math

    s = T.score(["a", "x", "b"], {"a", "b", "c"})
    ideal = 1 + 1 / math.log2(3) + 1 / math.log2(4)
    assert s["ndcg"] == pytest.approx((1 + 1 / math.log2(4)) / ideal)
    assert s["rr"] == 1.0 and not s["all_returned"] and s["in_top5"] == 2


def test_score_with_no_evidence_returned():
    s = T.score(["x", "y"], {"a"})
    assert s == {"ndcg": 0.0, "rr": 0.0, "all_returned": False, "any_returned": False, "in_top5": 0}


# --- the test statistic ----------------------------------------------------------

def test_sign_flip_exact_values():
    assert T.sign_flip_p([1, 1, 1]) == (1 / 8, "exact")
    assert T.sign_flip_p([0.5, -0.5]) == (3 / 4, "exact")  # sums >= 0 in 3 of 4 sign vectors
    assert T.sign_flip_p([0, 0]) == (1.0, "exact")
    # Zeros neither help nor count: they leave the exact enumeration alone.
    assert T.sign_flip_p([1, 1, 1, 0, 0]) == (1 / 8, "exact")


def test_sign_flip_monte_carlo_is_seeded_and_small_for_a_clear_effect():
    diffs = [0.3] * 25
    p1, m1 = T.sign_flip_p(diffs, permutations=20_000)
    p2, _ = T.sign_flip_p(diffs, permutations=20_000)
    assert m1.startswith("monte_carlo") and p1 == p2 == pytest.approx(1 / 20_001)
    p3, _ = T.sign_flip_p([0.3, -0.3] * 13, permutations=20_000)
    assert p3 > 0.3


# --- matching --------------------------------------------------------------------

QUERIES = {
    "scene_1_q_0": {"type": "temporal_reasoning", "text": "What did I do  in February?", "relevant": {"scene_1_session_2"}},
    "scene_2_q_0": {"type": "multi_session", "text": "How many plants last month?", "relevant": {"scene_2_session_1"}},
}


def _cue_row(qid, text, qtype, cue):
    return {"question_id": qid, "question_type": qtype, "question_date": "2023/03/10 (Fri) 23:15",
            "question": text, "time_cue": cue}


def test_match_cues_pairs_by_text_and_skips_uncued():
    rows = [_cue_row("a1", "What did I do in February?", "temporal-reasoning", FEB),
            _cue_row("b1", "How many plants last month?", "multi-session", None)]
    out = T.match_cues(QUERIES, rows)
    assert [(q["qid"], q["question_id"]) for q in out] == [("scene_1_q_0", "a1")]


def test_match_cues_refuses_a_type_mismatch_and_an_unknown_text():
    with pytest.raises(ValueError):
        T.match_cues(QUERIES, [_cue_row("a1", "What did I do in February?", "multi-session", FEB)])
    with pytest.raises(ValueError):
        T.match_cues(QUERIES, [_cue_row("z", "Never asked", "temporal-reasoning", FEB)])


# --- the verdict -----------------------------------------------------------------

POLICY = "cued-v0.2"


def _rows(n_up=62, n_down=0, n_flat=0, *, replicate_ok=True, extra_rows=0, policy=POLICY, qtype="temporal_reasoning"):
    """Questions whose evidence the cue moves from 2nd to 1st (up), 1st to 2nd (down), or leaves (flat)."""
    rows = []
    specs = [("up", i) for i in range(n_up)] + [("down", i) for i in range(n_down)] + [("flat", i) for i in range(n_flat)]
    for kind, i in specs:
        qid = f"scene_{kind}{i}_q_0"
        base = ["x", "ev"] if kind != "down" else ["ev", "x"]
        cued = list(reversed(base)) if kind in ("up", "down") else list(base)
        cued += [f"extra{k}" for k in range(extra_rows)]
        common = {"qid": qid, "type": qtype, "relevant": ["ev"], "seat": [], "latency_ms": 1.0,
                  "relevant_times": {"ev": "2023-02-10T00:00:00+00:00"}}
        rt = {"policy": policy, "period": ["2023-02-01T00:00:00+00:00", "2023-03-01T00:00:00+00:00"]}
        rows.append({**common, "arm": "none", "returned": base, "time_cue": None, "response_time_cue": None})
        rows.append({**common, "arm": "none_again", "returned": base if replicate_ok else ["y"],
                     "time_cue": None, "response_time_cue": None})
        rows.append({**common, "arm": "extracted", "returned": cued, "time_cue": FEB, "response_time_cue": rt})
    return rows


def test_judge_passes_a_clear_lift():
    v = T.judge(_rows(n_up=62), POLICY)
    assert v["verdict"] == "pass" and v["valid"]
    assert v["primary"]["up"] == 62 and v["primary"]["down"] == 0
    assert v["report"]["period_holds_evidence"] == 62


def test_judge_is_null_without_an_effect():
    v = T.judge(_rows(n_up=31, n_down=31), POLICY)
    assert v["valid"] and v["verdict"] == "null"


def test_judge_type_guard_fails_on_net_three_down_in_one_type():
    rows = _rows(n_up=62) + _rows(n_up=0, n_down=3, qtype="knowledge_update")
    v = T.judge(rows, POLICY)
    assert v["primary"]["p_one_sided"] < 0.05
    assert not v["type_guard"]["ok"] and v["verdict"] == "null"
    ok = T.judge(_rows(n_up=62) + _rows(n_up=0, n_down=2, qtype="knowledge_update"), POLICY)
    assert ok["type_guard"]["ok"] and ok["verdict"] == "pass"


@pytest.mark.parametrize("rows,broken", [
    (_rows(n_up=59), "target_at_least"),
    (_rows(n_up=62, replicate_ok=False), "replicate_identical"),
    (_rows(n_up=62, extra_rows=2), "returned_preserved"),
    (_rows(n_up=0, n_flat=62), "positive_control"),
    (_rows(n_up=62, policy="cued-v0.1"), "policy_reported"),
])
def test_judge_voids_a_run_that_breaks_a_precondition(rows, broken):
    v = T.judge(rows, POLICY)
    assert v["verdict"] == "void" and not v["valid"]
    pre = v["preconditions"]
    assert (pre[broken] is False) or (broken == "positive_control" and pre[broken] == 0)


def test_judge_leaves_an_ignored_cue_out_of_the_target():
    rows = _rows(n_up=62)
    for r in rows:
        if r["qid"] == "scene_up0_q_0" and r["arm"] == "extracted":
            r["response_time_cue"] = {"policy": POLICY, "ignored": "recent_only", "period": r["response_time_cue"]["period"]}
            r["returned"] = ["x", "ev"]
    v = T.judge(rows, POLICY)
    assert v["preconditions"]["target"] == 61 and v["report"]["ignored"] == 1


def test_published_cue_file_reproduces_its_extractor_hash():
    import hashlib

    spec = json.loads((BENCH / "measurements" / "longmemeval_time_cue_extractor.json").read_text(encoding="utf-8"))
    sha = hashlib.sha256(json.dumps([spec["model"], spec["effort"], spec["system"], spec["schema"]],
                                    sort_keys=True).encode()).hexdigest()[:16]
    rows = [json.loads(line) for line in
            (BENCH / "measurements" / "longmemeval_time_cues.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 500 and {r["extractor_sha"] for r in rows} == {sha}
    assert sum(1 for r in rows if r["time_cue"]) == 73


# --- storing each session at its own time -----------------------------------------

@pytest.mark.asyncio
async def test_store_corpus_stores_each_record_at_the_time_it_is_given():
    import numpy as np

    import benchmark_trackb_lmeb as tb
    from cpersona.database import get_db

    class _Encoder:
        def encode(self, texts, **kw):
            out = np.zeros((len(texts), 8), dtype=np.float32)
            out[:, 0] = 1.0
            return out

    corpus = [{"id": "scene_1_session_1", "title": "Data time: 12:04 AM on Saturday 20 May, 2023 - Session 1",
               "text": "first"},
              {"id": "scene_1_session_2", "title": "Data time: 03:22 PM on Monday 29 May, 2023 - Session 2",
               "text": "second"}]
    db = await get_db()

    async def stored():
        rows = await db.execute_fetchall(
            "SELECT msg_id, timestamp FROM memories WHERE agent_id = ? ORDER BY msg_id", (tb.AGENT_ID,))
        await db.execute("DELETE FROM memories WHERE agent_id = ?", (tb.AGENT_ID,))
        await db.commit()
        return {r[0]: r[1] for r in rows}

    await stored()
    await tb.store_corpus(None, None, _Encoder(), corpus, isolate_scenes=True,
                          timestamp_of=lambda d: T.title_instant(d["title"]).isoformat())
    assert await stored() == {"scene_1_session_1": "2023-05-20T00:04:00+00:00",
                              "scene_1_session_2": "2023-05-29T15:22:00+00:00"}
    await tb.store_corpus(None, None, _Encoder(), corpus, isolate_scenes=True)
    assert set((await stored()).values()) == {"2026-01-01T00:00:00Z"}  # without it, one fixed time as before
