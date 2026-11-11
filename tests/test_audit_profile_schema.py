"""The auditor profile's published schemas (docs/schemas/, RELIABLE_RECALL_2_6.md §8).

The trace schema must accept every trace the server writes and refuse a trace that
lost a field the audit reads; the attribution schema must accept every record the
confirmation tool writes and refuse a record that breaks the profile's rules (an
unattributed failure with no reason, a code outside the taxonomy). Both directions are
tested, so a schema loosened until it accepts anything fails here as surely as one
tightened until it refuses the server's own traces.
"""
import copy
import json
import re
from pathlib import Path

import jsonschema
import pytest

from benchmarks import recall_trace_confirm as rtc
from cpersona import config, memory_handlers, reconstruct
from tests.test_recall_trace import AGENT, QUERY, _seed

ROOT = Path(__file__).resolve().parents[1]
TRACE_SCHEMA = json.loads((ROOT / "docs/schemas/recall-trace-v2.schema.json").read_text())
RECORD_SCHEMA = json.loads((ROOT / "docs/schemas/recall-failure-attribution-v1.schema.json").read_text())
TRACE = jsonschema.Draft202012Validator(TRACE_SCHEMA)
RECORD = jsonschema.Draft202012Validator(RECORD_SCHEMA)


def _errors(validator, doc):
    return [e.message for e in validator.iter_errors(doc)]


def test_both_schemas_are_valid_json_schema():
    jsonschema.Draft202012Validator.check_schema(TRACE_SCHEMA)
    jsonschema.Draft202012Validator.check_schema(RECORD_SCHEMA)


# --- the trace schema accepts what the server writes -------------------------------


async def _traces(monkeypatch):
    out = {}
    await _seed()
    for mode in ("rrf", "rsf", "cascade"):
        monkeypatch.setattr(config, "CPERSONA_FUSION_MODE", mode, raising=False)
        out[f"recall-{mode}"] = (await memory_handlers.do_recall(AGENT, QUERY, limit=3, trace=True))["trace"]
        out[f"recall-{mode}-cue"] = (await memory_handlers.do_recall(
            AGENT, QUERY, limit=3, trace=True,
            time_cue={"after": "2026-09-11", "before": "2026-09-13", "confidence": "sure"},
        ))["trace"]
        for m in (None, "lite", "pro"):
            r = await reconstruct.do_reconstruct(AGENT, QUERY, count=2, trace=True, mode=m)
            out[f"reconstruct-{mode}-{m}"] = r["trace"]["recall"]
    return out


@pytest.mark.asyncio
async def test_every_trace_the_server_writes_is_valid(fake_embedding_client, monkeypatch):
    traces = await _traces(monkeypatch)
    assert len(traces) == 15
    bad = {name: _errors(TRACE, t)[:2] for name, t in traces.items() if _errors(TRACE, t)}
    assert bad == {}
    # The cue runs record the cue's own block, so the optional part is exercised too.
    assert all("cue" in t for name, t in traces.items() if name.endswith("-cue"))


@pytest.mark.asyncio
@pytest.mark.parametrize("defect", [
    "no gate", "trace_version 1", "passed is a string", "a ref outside the spelling", "a digest that is not one",
    "no stage", "an arm row without its rank", "autocut without fired", "a reservation without its kind",
])
async def test_the_trace_schema_refuses_a_trace_that_lost_what_the_audit_reads(
    fake_embedding_client, monkeypatch, defect
):
    t = copy.deepcopy((await _traces(monkeypatch))["recall-rrf-cue"])
    assert _errors(TRACE, t) == []
    arm = next(rows for rows in t["arms"].values() if rows)
    if defect == "no gate":
        del t["gate"]
    elif defect == "trace_version 1":
        t["trace_version"] = 1
    elif defect == "passed is a string":
        t["gate"]["decisions"][0]["passed"] = "yes"
    elif defect == "a ref outside the spelling":
        arm[0]["ref"] = "memory:1"
    elif defect == "a digest that is not one":
        next(iter(t["stage_inputs"].values()))["order"] = "not-a-digest"
    elif defect == "no stage":
        t["stages"] = []
    elif defect == "an arm row without its rank":
        del arm[0]["rank"]
    elif defect == "autocut without fired":
        del t["autocut"]["fired"]
    elif defect == "a reservation without its kind":
        assert t["reservation"], "the cue trace holds a reservation to break"
        del t["reservation"][0]["kind"]
    assert _errors(TRACE, t) != []


# --- the attribution schema accepts what the tool writes ---------------------------


async def _one_trace(monkeypatch):
    return (await _traces(monkeypatch))["recall-rrf"]


def _returned(trace):
    order = trace["order"]
    return [row["ref"] for row in order["before_cut"][: order["limit"]]]


@pytest.mark.asyncio
async def test_the_records_the_tool_writes_are_valid(fake_embedding_client, monkeypatch):
    trace = await _one_trace(monkeypatch)
    hit = _returned(trace)[0]
    records = {
        "candidate miss": rtc.attribution_record("q1", trace, ["mem:999999"]),
        "capture miss": rtc.attribution_record("q2", trace, [hit], stored_texts={}),
        "reconstruction loss": rtc.attribution_record("q3", trace, [hit], shown_text="nothing", quotes=["storm"]),
        "agent misuse": rtc.attribution_record("q4", trace, [hit], shown_text="the storm", quotes=["storm"],
                                               correct=False, unattributed_reason="ignored: the code is attributed"),
        "unattributed": rtc.attribution_record("q5", trace, [hit], correct=False,
                                               unattributed_reason="the shown text was not recorded"),
        "replayed": rtc.attribution_record(
            "q6", trace, ["mem:999999"],
            replay={"identical": True, "change": {"kind": "depth", "value": 100},
                    "code_before": "CANDIDATE_MISS", "code_after": "RANKING_MISS"},
        ),
    }
    codes = {name: r["code"] for name, r in records.items()}
    assert codes == {"candidate miss": "CANDIDATE_MISS", "capture miss": "CAPTURE_MISS",
                     "reconstruction loss": "RECONSTRUCTION_LOSS", "agent misuse": "AGENT_MISUSE",
                     "unattributed": "UNATTRIBUTED", "replayed": "CANDIDATE_MISS"}
    assert {name: _errors(RECORD, r) for name, r in records.items() if _errors(RECORD, r)} == {}
    assert records["unattributed"]["unattributed_reason"] == "the shown text was not recorded"
    assert "unattributed_reason" not in records["agent misuse"]
    assert records["capture miss"]["inputs"]["stored_texts"] is True
    assert records["candidate miss"]["inputs"]["stored_texts"] is False
    assert records["candidate miss"]["trace_digest"] == rtc.trace_digest(trace)


@pytest.mark.asyncio
async def test_a_recall_that_did_not_fail_writes_no_record(fake_embedding_client, monkeypatch):
    trace = await _one_trace(monkeypatch)
    hit = _returned(trace)[0]
    assert rtc.attribution_record("ok", trace, [hit], shown_text="the storm", quotes=["storm"], correct=True) is None


@pytest.mark.asyncio
async def test_the_tool_refuses_what_the_profile_cannot_hold(fake_embedding_client, monkeypatch):
    trace = await _one_trace(monkeypatch)
    hit = _returned(trace)[0]
    with pytest.raises(ValueError, match="reason"):
        rtc.attribution_record("q", trace, [hit], correct=False)
    with pytest.raises(ValueError, match="at least one"):
        rtc.attribution_record("q", trace, [])
    old = dict(trace, trace_version=1)
    with pytest.raises(ValueError, match="trace_version"):
        rtc.attribution_record("q", old, ["mem:999999"])


def _valid_record():
    return {
        "profile_version": 1, "case": "q", "code": "CANDIDATE_MISS", "gold_refs": ["mem:1"],
        "inputs": {"shown_text": False, "quotes": False, "correct": None, "stored_texts": False,
                   "searchable_chars": False, "shown_by_ref": False, "degraded": None},
        "trace_version": 2, "trace_digest": "0123456789abcdef",
    }


@pytest.mark.parametrize("defect", [
    "unattributed without a reason", "a reason on an attributed code", "a code outside the taxonomy",
    "no gold reference", "a digest that is not one", "a change without the codes", "an unknown change",
    "a field the profile does not have", "an input missing",
])
def test_the_record_schema_refuses_a_record_that_breaks_the_profile(defect):
    r = _valid_record()
    assert _errors(RECORD, r) == []
    if defect == "unattributed without a reason":
        r["code"] = "UNATTRIBUTED"
    elif defect == "a reason on an attributed code":
        r["unattributed_reason"] = "why"
    elif defect == "a code outside the taxonomy":
        r["code"] = "SOMETHING_ELSE"
    elif defect == "no gold reference":
        r["gold_refs"] = []
    elif defect == "a digest that is not one":
        r["trace_digest"] = "xyz"
    elif defect == "a change without the codes":
        r["replay"] = {"identical": True, "change": {"kind": "depth"}}
    elif defect == "an unknown change":
        r["replay"] = {"identical": True, "change": {"kind": "magic"}, "code_before": "CANDIDATE_MISS",
                       "code_after": "CANDIDATE_MISS"}
    elif defect == "a field the profile does not have":
        r["memory_text"] = "a stored text"
    elif defect == "an input missing":
        del r["inputs"]["degraded"]
    assert _errors(RECORD, r) != []


def test_an_unattributed_record_with_its_reason_is_valid():
    r = dict(_valid_record(), code="UNATTRIBUTED", unattributed_reason="the benchmark marks no answer turn")
    assert _errors(RECORD, r) == []


# --- one taxonomy -------------------------------------------------------------------


def test_the_schema_codes_are_the_design_taxonomy():
    text = (ROOT / "docs/RELIABLE_RECALL_2_6.md").read_text()
    section = text[text.index("**The failure taxonomy.**"):text.index("**Counterfactual replay.**")]
    table = re.findall(r"^\| `([A-Z_]+)` \|", section, re.M)
    start = section.index("The recall process adds its own")
    process = re.findall(r"`([A-Z_]+)`", section[start:section.index("Because the loop's policy", start)])
    assert len(table) == 12 and len(process) == 8
    assert RECORD_SCHEMA["$defs"]["code"]["enum"] == table + process


def test_every_code_the_tool_assigns_is_in_the_schema():
    doc_codes = set(re.findall(r"^    ([A-Z_]{6,})\s", rtc.__doc__, re.M))
    assert len(doc_codes) == 10
    assert doc_codes <= set(RECORD_SCHEMA["$defs"]["code"]["enum"])


def test_the_digest_names_the_trace_not_its_key_order():
    a = {"trace_version": 2, "gate": {"decisions": []}, "arms": {}}
    b = {"arms": {}, "gate": {"decisions": []}, "trace_version": 2}
    assert rtc.trace_digest(a) == rtc.trace_digest(b)
    assert rtc.trace_digest(a) != rtc.trace_digest(dict(a, arms={"block": []}))
    assert re.fullmatch(r"[0-9a-f]{16}", rtc.trace_digest(a))
