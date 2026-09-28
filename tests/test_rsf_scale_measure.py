"""The verdict functions of benchmarks/rsf_scale_measure.py for the bug-247 second design.

They decide control 2 and Rule L of benchmarks/measurements/prereg-rsf-gate-scale.md, so
each is checked against rows built to sit on either side of its line.
"""
import pytest

from benchmarks import rsf_scale_measure as m


def row(qid, variant, returned, ndcg, qtype="knowledge_update"):
    return {"qid": qid, "type": qtype, "variant": variant, "returned": returned,
            "n_returned": len(returned), "ndcg10": ndcg}


def test_order_agreement_ignores_rows_only_one_side_returned():
    rows = [row("q1", "legacy", ["a", "b", "c"], 0.5), row("q1", "gate", ["a", "x", "b", "c"], 0.5),
            row("q2", "legacy", ["a", "b"], 0.5), row("q2", "gate", ["b"], 0.5)]
    assert m.order_agreement(rows) == {"questions": 2, "disagree": 0, "examples": []}


def test_order_agreement_names_a_swapped_pair():
    rows = [row("q1", "legacy", ["a", "b", "c"], 0.5), row("q1", "gate", ["b", "a", "c"], 0.5)]
    assert m.order_agreement(rows)["disagree"] == 1
    assert m.order_agreement(rows)["examples"] == ["q1"]


def _type_rows(qtype, n, fell, rose, prefix):
    rows = []
    for i in range(n):
        qid = f"{prefix}{i}"
        after = 0.4 if i < fell else 0.6 if i < fell + rose else 0.5
        rows += [row(qid, "legacy", ["a"], 0.5, qtype), row(qid, "gate", ["a"], after, qtype)]
    return rows


def test_rule_l_allows_max_of_two_and_five_percent_net_falls():
    # n = 60: the allowance is max(2, ceil(3.0)) = 3. Net 3 passes, net 4 does not.
    ok = m.rule_l(_type_rows("knowledge_update", 60, fell=4, rose=1, prefix="a"))
    assert ok["per_type"]["knowledge_update"]["allowed"] == 3
    assert ok["per_type"]["knowledge_update"]["net_fell"] == 3 and ok["rule1_no_type_falls"]
    bad = m.rule_l(_type_rows("knowledge_update", 60, fell=4, rose=0, prefix="b"))
    assert not bad["rule1_no_type_falls"] and not bad["pass"]


def test_rule_l_small_types_get_an_allowance_of_two():
    ok = m.rule_l(_type_rows("temporal_reasoning", 10, fell=2, rose=0, prefix="c"))
    assert ok["per_type"]["temporal_reasoning"]["allowed"] == 2 and ok["rule1_no_type_falls"]


def test_rule_l_macro_must_fall_by_less_than_one_point():
    # One type, every question down by exactly 0.0099 -> macro falls 0.99 points: passes.
    rows = []
    for i in range(3):
        rows += [row(f"d{i}", "legacy", ["a"], 0.5), row(f"d{i}", "gate", ["a"], 0.5 - 0.0099)]
    verdict = m.rule_l(rows)
    assert verdict["macro"]["delta_unrounded"] == pytest.approx(-0.99)
    assert verdict["rule2_mean_holds"]
    rows = []
    for i in range(3):
        rows += [row(f"e{i}", "legacy", ["a"], 0.5), row(f"e{i}", "gate", ["a"], 0.49)]
    assert not m.rule_l(rows)["rule2_mean_holds"]


def test_added_rows_counts_an_answer_only_among_the_added_rows():
    rows = [
        # q1: gate adds "x", which is an answer.
        row("q1", "legacy", ["a"], 0.5), row("q1", "gate", ["a", "x"], 0.6),
        # q2: gate adds "y"; the answer "a" was already returned, so it does not count.
        row("q2", "legacy", ["a"], 0.5), row("q2", "gate", ["a", "y"], 0.5),
        # q3: gate returns fewer.
        row("q3", "legacy", ["a", "b"], 0.5), row("q3", "gate", ["a"], 0.5),
    ]
    got = m.added_rows(rows, {"q1": {"x"}, "q2": {"a"}, "q3": {"a"}})
    assert got == {"more": 2, "more_with_answer_added": 1, "fewer": 1}


def test_variants_parse():
    assert m.parse_variant("gate") == ("gate", None)
    assert m.parse_variant("legacy") == ("legacy", None)
    assert m.parse_variant("none:8") == ("none", 8.0)
    with pytest.raises(ValueError):
        m.parse_variant("active:8")
