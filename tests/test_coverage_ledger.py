"""The coverage ledger (docs/RECALL_PROCESS_DESIGN.md §1.5, cpersona/coverage.py).

A traced recall or reconstruct records which parts of the question the returned
records hold. The checks here pin what the ledger claims: parts are words cut by
script, it carries spans and refs but no text, it reads each returned record in
full rather than its preview, a record that was not returned covers nothing, and
the answer is the same with or without it.
"""
import json

import pytest

from cpersona import config, coverage, memory_handlers, reconstruct

AGENT = "agent.coverage-ledger"
LONG_TAIL_WORD = "ferrocrystal"


# --- parts -------------------------------------------------------------------------


def _words(query):
    q = coverage.normalize(query)
    return [(q[s:e], kind) for s, e, kind in coverage.parts(q)]


def test_parts_are_words_cut_by_script():
    words = _words("CPersona 2.5.5a1 のリリース前にやった包括コードレビューで、最優先度 P1 と判定されたのはどんなバグだった？ bug-218 PR #354 a/b")
    assert words == [
        ("cpersona", "identifier"), ("2.5.5a1", "identifier"), ("リリース", "katakana"), ("包括", "kanji"),
        ("コードレビュー", "katakana"), ("最優先度", "kanji"), ("p1", "identifier"), ("判定", "kanji"),
        ("バグ", "katakana"), ("bug-218", "identifier"), ("pr", "identifier"), ("#354", "identifier"),
        ("a/b", "identifier"),
    ]


def test_hiragana_single_characters_and_repeats_are_not_parts():
    words = [w for w, _ in _words("前 の a 記憶 と 記憶 を x2 で")]
    # 前 is one kanji, の/と/を/で are hiragana, a is one letter; 記憶 appears twice.
    assert words == ["記憶", "x2"]


def test_spans_recover_the_words_from_the_normalized_question():
    query = "ＡＰＩキー の ローテーション は PR #1856"
    q = coverage.normalize(query)
    got = [q[s:e] for s, e, _ in coverage.parts(q)]
    assert got == ["api", "キー", "ローテーション", "pr", "#1856"]


# --- the ledger --------------------------------------------------------------------


def test_the_ledger_says_which_record_holds_each_part_and_which_part_none_holds():
    out = coverage.ledger("包括コードレビュー の P1 バグ", [
        ("r1", "包括的なコードレビューを行った"),
        ("r2", "P1 のバグは merge_memories だった"),
    ])
    assert out["normalization"] == "nfkc-lower"
    assert [p["kind"] for p in out["parts"]] == ["kanji", "katakana", "identifier", "katakana"]
    assert out["covered_by"] == [["r1"], ["r1"], ["r2"], ["r2"]]
    assert out["uncovered"] == []
    assert out["records"] == 2

    none = coverage.ledger("包括コードレビュー の P1 バグ", [("r3", "関係のない記録")])
    assert none["covered_by"] == [[], [], [], []]
    assert none["uncovered"] == [0, 1, 2, 3]


def test_the_ledger_carries_no_text():
    query = "ferrocrystal の リリース日 は bug-218 の後か"
    out = coverage.ledger(query, [("r1", "ferrocrystal released after bug-218 リリース日")])
    blob = json.dumps(out, ensure_ascii=False)
    q = coverage.normalize(query)
    for s, e, _ in coverage.parts(q):
        assert q[s:e] not in blob
    assert set(out) == {"normalization", "parts", "covered_by", "uncovered", "records"}


def test_the_number_of_parts_is_bounded():
    query = " ".join(f"id{i:02d}" for i in range(coverage.MAX_PARTS + 8))
    out = coverage.ledger(query, [])
    assert len(out["parts"]) == coverage.MAX_PARTS
    assert out["parts_omitted"] == 8


# --- in a traced recall ------------------------------------------------------------


async def _seed(contents):
    from cpersona.database import get_db

    db = await get_db()
    for table in ("memories", "episodes", "profiles"):
        await db.execute(f"DELETE FROM {table} WHERE agent_id = ?", (AGENT,))
    await db.commit()
    refs = []
    for i, content in enumerate(contents):
        out = await memory_handlers.do_store(
            AGENT,
            {"content": content, "source": {"System": "test"}, "timestamp": f"2026-09-{10 + i:02d}T00:00:00+00:00"},
        )
        refs.append(f"mem:{out['id']}")
    return refs


def _long_record():
    """The tail word sits past the preview, so a ledger read from the preview misses it."""
    filler = "harbor lighthouse keeper logbook notes. " * 40
    assert len(filler) > config.RECALL_PREVIEW_CHARS + 100
    return filler + LONG_TAIL_WORD


def _admit_everything(monkeypatch):
    """Nothing is dropped before the count, so which records return is fixed by the count alone."""
    monkeypatch.setattr(config, "FUSED_GATE_ENABLED", False)
    monkeypatch.setattr(memory_handlers, "_adaptive_min_score", lambda count: 0.0)
    monkeypatch.setattr(memory_handlers, "AUTOCUT_ENABLED", False)


@pytest.mark.asyncio
async def test_a_traced_recall_records_the_ledger_over_the_returned_records_in_full(fake_embedding_client, monkeypatch):
    _admit_everything(monkeypatch)
    refs = await _seed([_long_record(), "harbor lighthouse keeper", "an unrelated kitchen note about bread"])
    query = f"harbor lighthouse {LONG_TAIL_WORD} kitchen"
    plain = await memory_handlers.do_recall(AGENT, query, limit=3)
    traced = await memory_handlers.do_recall(AGENT, query, limit=3, trace=True)
    assert traced["messages"] == plain["messages"]
    assert "trace" not in plain

    returned = [m["ref"] for m in traced["messages"]]
    assert set(refs) <= set(returned)
    cov = traced["trace"]["coverage"]
    q = coverage.normalize(query)
    by_word = {q[p["span"][0]:p["span"][1]]: cov["covered_by"][i] for i, p in enumerate(cov["parts"])}
    # The tail word sits past the preview a recall returns, so this holds only if the ledger
    # read the record in full.
    assert by_word[LONG_TAIL_WORD] == [refs[0]]
    assert by_word["kitchen"] == [refs[2]]
    assert cov["uncovered"] == []
    assert cov["records"] == len(set(returned))
    assert traced["trace"]["timing_ms"]["coverage"] >= 0


@pytest.mark.asyncio
async def test_a_record_that_was_not_returned_covers_nothing(fake_embedding_client):
    refs = await _seed([_long_record(), "harbor lighthouse keeper", "an unrelated kitchen note about bread"])
    query = f"harbor lighthouse {LONG_TAIL_WORD} kitchen"
    cov = await coverage.for_refs(AGENT, query, [refs[1], refs[0]])
    q = coverage.normalize(query)
    words = [q[p["span"][0]:p["span"][1]] for p in cov["parts"]]
    assert [words[i] for i in cov["uncovered"]] == ["kitchen"]
    # Holders are listed in the order the refs were given.
    assert cov["covered_by"][words.index("harbor")] == [refs[1], refs[0]]
    assert cov["records"] == 2


@pytest.mark.asyncio
async def test_another_agents_record_is_not_read(fake_embedding_client):
    refs = await _seed(["harbor lighthouse keeper"])
    cov = await coverage.for_refs("agent.someone-else", "harbor lighthouse", refs)
    assert cov["records"] == 0 and cov["uncovered"] == [0, 1]


@pytest.mark.asyncio
async def test_a_ledger_that_cannot_be_built_is_reported_and_the_recall_still_answers(fake_embedding_client, monkeypatch):
    await _seed(["harbor lighthouse keeper logbook"])

    async def broken(agent_id, refs):
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(coverage, "stored_texts", broken)
    traced = await memory_handlers.do_recall(AGENT, "harbor lighthouse", limit=3, trace=True)
    assert traced["messages"]
    assert traced["trace"]["coverage"] == {"error": "RuntimeError"}


@pytest.mark.asyncio
async def test_a_traced_reconstruct_records_the_ledger_over_the_cited_records(fake_embedding_client):
    refs = await _seed([_long_record(), "harbor lighthouse keeper", "an unrelated kitchen note about bread"])
    query = f"harbor lighthouse {LONG_TAIL_WORD}"
    plain = await reconstruct.do_reconstruct(AGENT, query, count=3)
    traced = await reconstruct.do_reconstruct(AGENT, query, count=3, trace=True)
    assert [i["head_ref"] for i in traced["items"]] == [i["head_ref"] for i in plain["items"]]
    assert "coverage" not in json.dumps(plain)

    cov = traced["trace"]["coverage"]
    cited = {r for it in traced["items"] for r in (it["head_ref"], *(c["ref"] for c in it.get("claims", [])))}
    assert cov["records"] == len(cited)
    for holders in cov["covered_by"]:
        assert set(holders) <= cited
    if refs[0] in cited:
        q = coverage.normalize(query)
        by_word = {q[p["span"][0]:p["span"][1]]: cov["covered_by"][i] for i, p in enumerate(cov["parts"])}
        assert refs[0] in by_word[LONG_TAIL_WORD]
