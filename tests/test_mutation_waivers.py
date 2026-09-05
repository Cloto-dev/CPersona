"""Hermetic tests for the mutation-diff waiver registry (lane L2).

No cosmic-ray dependency: these gate the waiver logic itself — content-based
fingerprints, the verification rules, and the approval/expiry gating — under the
ordinary `test` job. The point of a waiver registry is that IT does not silently
rot; that promise is only real if these invariants are checked.
"""

import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import mutation_waivers as mw  # noqa: E402


def _waiver(**over):
    base = {
        "id": "waiver-x",
        "fingerprint": "0123456789abcdef",
        "code_context_hash": "fedcba9876543210",
        "operator": "core/ReplaceTrueWithFalse",
        "location": {"file": "cpersona/isolation.py", "definition": "foo", "line_hint": 1},
        "classification": "operator_noise",
        "reason": "reason",
        "evidence": "evidence",
        "created": "2026-07-24",
        "reverify_by": "2099-01-01",
        "tool_version": mw.TOOL_VERSION,
        "approved_by": "human",
    }
    base.update(over)
    return base


# --- content-based identity ------------------------------------------------

def test_fingerprint_survives_line_shift_but_not_content_change(tmp_path):
    """A function moving down the file keeps its fingerprint; editing the mutated
    line's content changes it — the two properties a line number cannot give."""
    line = "    return x > 0"
    (tmp_path / "m.py").write_text("# header\n" + line + "\n")
    fp_early, _ = mw.survivor_fingerprint(tmp_path, "m.py", "f", "core/X", 2)

    # Same line pushed 10 lines down (a refactor above it).
    (tmp_path / "m.py").write_text("\n" * 10 + line + "\n")
    fp_moved, _ = mw.survivor_fingerprint(tmp_path, "m.py", "f", "core/X", 11)
    assert fp_moved == fp_early  # line moved, identity stable

    # The mutated line itself changes -> a different mutant, different identity.
    (tmp_path / "m.py").write_text("    return x >= 0\n")
    fp_edited, _ = mw.survivor_fingerprint(tmp_path, "m.py", "f", "core/X", 1)
    assert fp_edited != fp_early


def test_fingerprint_ignores_surrounding_whitespace():
    assert mw.fingerprint("m.py", "f", "op", "  return  x>0 ") == mw.fingerprint("m.py", "f", "op", "return x>0")


def test_fingerprint_missing_line_is_none(tmp_path):
    (tmp_path / "m.py").write_text("one line\n")
    assert mw.survivor_fingerprint(tmp_path, "m.py", "f", "op", 99) == (None, None)


# --- verification rules ----------------------------------------------------

def _kinds(problems):
    return {p["kind"] for p in problems}


def test_verify_flags_malformed_shapes(tmp_path):
    (tmp_path / "cpersona").mkdir()
    (tmp_path / "cpersona" / "isolation.py").write_text("x = 1\n")
    reg = {"waivers": [
        _waiver(id="w1", operator="core/*"),                                  # operator-wide
        _waiver(id="w2", location={"file": "cpersona/*.py", "definition": "f"}),  # wildcard file
        _waiver(id="w3", location={"file": "cpersona/isolation.py"}),          # missing definition
        _waiver(id="w4", classification="not-a-real-class"),
        _waiver(id="w5", approved_by="claude"),                               # forged approval
        {"id": "w6"},                                                          # missing fields
    ]}
    kinds = _kinds(mw.verify(tmp_path, reg, date(2026, 7, 24)))
    assert {"bad_operator", "wildcard_location", "bad_classification", "bad_approval", "missing_field"} <= kinds


def test_verify_detects_duplicates(tmp_path):
    reg = {"waivers": [_waiver(id="dup", fingerprint="same"), _waiver(id="dup", fingerprint="same")]}
    kinds = _kinds(mw.verify(tmp_path, reg, date(2026, 7, 24)))
    assert "duplicate_id" in kinds and "duplicate_fingerprint" in kinds


def test_verify_expiry_and_tool_version_are_soft(tmp_path):
    (tmp_path / "cpersona").mkdir()
    # Make the code present so code_changed does not also fire.
    line = "    agent_id = None"
    (tmp_path / "cpersona" / "isolation.py").write_text(line + "\n")
    cch = mw.code_context_hash(line)
    reg = {"waivers": [
        _waiver(id="old", reverify_by="2000-01-01", code_context_hash=cch),
        _waiver(id="tool", tool_version="cosmic-ray==1.0.0", code_context_hash=cch),
    ]}
    problems = mw.verify(tmp_path, reg, date(2026, 7, 24))
    by_id = {(p["id"], p["kind"]): p["severity"] for p in problems}
    assert by_id[("old", "expired")] == "soft"
    assert by_id[("tool", "tool_version_mismatch")] == "soft"


def test_verify_code_changed_when_line_gone(tmp_path):
    (tmp_path / "cpersona").mkdir()
    (tmp_path / "cpersona" / "isolation.py").write_text("something else entirely\n")
    problems = mw.verify(tmp_path, {"waivers": [_waiver(code_context_hash="deadbeefdeadbeef")]}, date(2026, 7, 24))
    assert ("code_changed", "soft") in {(p["kind"], p["severity"]) for p in problems}


# --- approval / expiry gating ----------------------------------------------

def test_active_requires_human_approval_unexpired_and_code_present(tmp_path):
    (tmp_path / "cpersona").mkdir()
    line = "    return a and b"
    (tmp_path / "cpersona" / "isolation.py").write_text(line + "\n")
    cch = mw.code_context_hash(line)

    approved = _waiver(id="ok", fingerprint="FP", code_context_hash=cch, approved_by="human")
    pending = _waiver(id="pending", fingerprint="FP2", code_context_hash=cch, approved_by=None)
    expired = _waiver(id="exp", fingerprint="FP3", code_context_hash=cch, reverify_by="2000-01-01")
    gone = _waiver(id="gone", fingerprint="FP4", code_context_hash="0000000000000000")

    active = mw.active_waivers(tmp_path, {"waivers": [approved, pending, expired, gone]}, date(2026, 7, 24))
    assert set(active) == {"FP"}  # only the approved, unexpired, code-present one suppresses


# --- the shipped registry --------------------------------------------------

def test_shipped_registry_has_no_hard_problems():
    reg = mw.load_registry()
    hard = [p for p in mw.verify(REPO, reg, date.today()) if p["severity"] == "hard"]
    assert hard == [], f"shipped waiver registry is malformed: {hard}"


def test_shipped_example_is_pending_and_matches_live_code():
    """waiver-001 is a real, un-approved submission for the `str | None` BitOr
    noise. It must (a) not suppress anything yet (approved_by=null) and (b) still
    point at live code — its fingerprint must equal what the lane computes now."""
    reg = mw.load_registry()
    w = next(x for x in reg["waivers"] if x["id"] == "waiver-001")
    assert w["approved_by"] is None
    assert mw.active_waivers(REPO, reg, date.today()) == {}

    live_fp, live_cch = mw.survivor_fingerprint(
        REPO, w["location"]["file"], w["location"]["definition"], w["operator"], w["location"]["line_hint"]
    )
    assert live_fp == w["fingerprint"], "waiver-001 fingerprint drifted from cpersona/isolation.py"
    assert live_cch == w["code_context_hash"]


# --- bug-302: a waiver is checked against itself -----------------------------


def test_verify_rejects_a_fingerprint_that_does_not_match_its_own_declaration(tmp_path):
    """The fingerprint is what the runner matches a LIVE survivor against, and it
    was never recomputed anywhere — so a value copied from another survivor, or
    edited, or left behind when the location was retargeted, was accepted and
    went on to suppress a survivor other than the one the entry documents and a
    human approved. Every input it is built from is declared on the waiver
    itself, so the check needs nothing the registry does not already carry.
    """
    line = "    return x > 0"
    (tmp_path / "m.py").write_text("# header\n" + line + "\n")
    good_fp, ctx = mw.survivor_fingerprint(tmp_path, "m.py", "f", "core/X", 2)

    honest = _waiver(
        fingerprint=good_fp,
        code_context_hash=ctx,
        operator="core/X",
        location={"file": "m.py", "definition": "f", "line_hint": 2},
    )
    assert mw.verify(tmp_path, {"waivers": [honest]}, date(2026, 1, 1)) == []

    # Same waiver, same live code, only the declared fingerprint replaced by one
    # that belongs to a different line. Nothing else about the entry is wrong.
    foreign_fp = mw.fingerprint("m.py", "f", "core/X", "    return x < 0")
    assert foreign_fp != good_fp
    problems = mw.verify(
        tmp_path, {"waivers": [_waiver(**{**honest, "fingerprint": foreign_fp})]}, date(2026, 1, 1)
    )
    kinds = {(p["kind"], p["severity"]) for p in problems}
    assert ("fingerprint_mismatch", "hard") in kinds, problems


def test_a_stale_waiver_is_not_also_reported_as_a_mismatch(tmp_path):
    """When the code moved on there is no line to recompute from, so the entry
    gets the one finding that is true (`code_changed`) and not a second,
    misleading one about a fingerprint nothing could check."""
    (tmp_path / "m.py").write_text("# header\n    something else entirely\n")
    problems = mw.verify(
        tmp_path,
        {"waivers": [_waiver(location={"file": "m.py", "definition": "f", "line_hint": 2})]},
        date(2026, 1, 1),
    )
    kinds = {p["kind"] for p in problems}
    assert "code_changed" in kinds
    assert "fingerprint_mismatch" not in kinds
