"""The S0 segmenter: docs/BLOCK_REACH_DESIGN.md §2.

What these tests are about is not "does it cut" but "does it refuse to cut in
the places that change what the text says". A divider that split on every comma
would pass a coverage test and fail the job, because the clause after a comma
routinely carries the negation or the condition the clause before it is about.

The fixtures reproduce the shapes that occur in this project's own corpus --
negation after a condition, a correction in a later sentence, an omitted
subject, quoted speech, fenced code, long URLs, node boundaries falling
mid-sentence -- written out here rather than copied from stored records, since
a test file is a published artefact and the corpus is not.
"""

import unicodedata

import pytest

from cpersona.blocks import MAX_BLOCK_CHARS, BlockSpan, covers, segment


def _texts(text: str, spans: list[BlockSpan]) -> list[str]:
    return [text[s.start : s.end] for s in spans]


# --------------------------------------------------------------------------
# the partition itself
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",
        "a",
        "一文だけ。",
        "本番環境ではキャッシュを無効にする。",
        "段落。\n\n次の段落。\n\nさらに次。",
        "- 一つ目\n- 二つ目\n- 三つ目",
        "```python\nprint(1)\n```\n説明文。",
        "括弧 (ここに . が入る) のあと。",
        "改行だけ\nで区切られる\n行",
        "。。。",
        "\n\n\n",
        "https://example.invalid/a/very/long/path/that/keeps/going/and/going?q=1&r=2 を参照。",
    ],
)
def test_the_spans_partition_the_text(text):
    spans = segment(text)
    assert covers(text, spans)
    assert "".join(_texts(text, spans)) == text


def test_empty_text_yields_no_spans():
    assert segment("") == []


def test_the_division_is_deterministic():
    text = "一文目。二文目。\n\n段落が変わる。「引用。内側。」続き。"
    assert segment(text) == segment(text)


def test_a_span_is_never_empty():
    text = "。\n\n。\n。"
    assert all(s.end > s.start for s in segment(text))


# --------------------------------------------------------------------------
# what it refuses to cut
# --------------------------------------------------------------------------


def test_a_comma_is_not_a_boundary():
    """The load-bearing refusal: the clause after a comma carries the limit."""
    text = "キャッシュを有効にする。ただし、本番環境には適用しない。"
    assert _texts(text, segment(text)) == [
        "キャッシュを有効にする。",
        "ただし、本番環境には適用しない。",
    ]


def test_a_negation_after_a_comma_stays_with_its_subject():
    text = "設定を変更した。ただし、反映は次回起動時なので、今は挙動が変わらない。"
    blocks = _texts(text, segment(text))
    tail = [b for b in blocks if "変わらない" in b]
    assert len(tail) == 1
    assert tail[0].startswith("ただし、"), tail


def test_a_sentence_end_inside_a_quotation_does_not_cut():
    text = "報告は「完了した。問題はない。」だった。次の話。"
    assert _texts(text, segment(text)) == ["報告は「完了した。問題はない。」だった。", "次の話。"]


def test_a_period_inside_parentheses_does_not_cut():
    text = "バージョン (3.14 が既定) を確認する。次へ。"
    assert _texts(text, segment(text)) == ["バージョン (3.14 が既定) を確認する。", "次へ。"]


def test_a_decimal_point_is_not_a_sentence_end():
    text = "The value is 3.14 and that is all."
    assert len(segment(text)) == 1


def test_a_blank_line_inside_a_fence_does_not_cut():
    text = "前置き。\n```\nfirst\n\nsecond\n```\nあとがき。"
    blocks = _texts(text, segment(text))
    fenced = [b for b in blocks if "first" in b]
    assert len(fenced) == 1
    assert "second" in fenced[0], blocks


def test_an_unterminated_fence_protects_the_rest():
    text = "前置き。\n```\ncode\n\nmore code\n改行もある"
    blocks = _texts(text, segment(text))
    assert len([b for b in blocks if "code" in b]) == 1


def test_an_unbalanced_bracket_does_not_protect_the_whole_record():
    """A stray opening bracket must degrade to 'not inside anything' -- the
    alternative is one enormous block whenever someone types 「 and never
    closes it."""
    text = "開いたまま「の文。次の文。さらに次の文。"
    assert len(segment(text)) > 1


# --------------------------------------------------------------------------
# node boundaries are the hard constraint
# --------------------------------------------------------------------------


def test_a_node_boundary_always_cuts_even_mid_sentence():
    text = "この文はノード境界をまたいでいて切れ目がない長い文である"
    boundary = 10
    spans = segment(text, node_bounds=(boundary,))
    assert covers(text, spans)
    assert boundary in {s.end for s in spans}


def test_a_node_boundary_cuts_inside_a_quotation_too():
    """The bracket rule yields to the node rule: a block that spanned two nodes
    would be built from text two node vectors already cover."""
    text = "報告は「完了した。問題はない。」だった。"
    boundary = text.index("問題")
    spans = segment(text, node_bounds=(boundary,))
    assert boundary in {s.end for s in spans}
    assert covers(text, spans)


def test_node_bounds_outside_the_text_are_ignored():
    text = "一文。二文。"
    spans = segment(text, node_bounds=(0, len(text), len(text) + 50, -3))
    assert covers(text, spans)
    assert _texts(text, spans) == ["一文。", "二文。"]


def test_a_node_boundary_that_coincides_with_a_sentence_end_adds_nothing():
    text = "一文。二文。"
    plain = segment(text)
    with_bound = segment(text, node_bounds=(len("一文。"),))
    assert plain == with_bound


# --------------------------------------------------------------------------
# forced boundaries (§2, §7)
# --------------------------------------------------------------------------


def test_a_block_longer_than_the_limit_is_split_and_marked():
    text = "あ" * (MAX_BLOCK_CHARS * 2 + 5)
    spans = segment(text)
    assert covers(text, spans)
    assert all(s.end - s.start <= MAX_BLOCK_CHARS for s in spans)
    assert [s.forced for s in spans] == [True, True, False]


def test_only_the_forced_ends_are_marked():
    """The tail of a forced split ends where the structure already ended, so it
    is not itself forced."""
    text = "あ" * (MAX_BLOCK_CHARS + 10) + "。次の文。"
    spans = segment(text)
    assert spans[0].forced is True
    assert spans[-1].forced is False


def test_nothing_is_forced_when_everything_fits():
    text = "短い文。もう一つ。"
    assert not any(s.forced for s in segment(text))


def test_a_forced_split_prefers_a_weak_boundary_to_cutting_mid_word():
    half = MAX_BLOCK_CHARS // 2
    text = "あ" * (half + 40) + "、" + "い" * (MAX_BLOCK_CHARS)
    spans = segment(text, max_chars=MAX_BLOCK_CHARS)
    assert spans[0].end == half + 41, "the comma just past the midpoint was not used"
    assert spans[0].forced is True


def test_a_weak_boundary_too_close_to_the_start_is_not_used():
    """Taking it would leave a three-character block, which is not an
    improvement on cutting mid-word."""
    text = "あ、" + "い" * (MAX_BLOCK_CHARS * 2)
    spans = segment(text)
    assert spans[0].end > 2, "a boundary at offset 2 was taken"


def test_a_custom_limit_is_honoured():
    text = "あ" * 100
    spans = segment(text, max_chars=30)
    assert all(s.end - s.start <= 30 for s in spans)
    assert covers(text, spans)


def test_a_limit_below_one_is_refused():
    with pytest.raises(ValueError, match="max_chars"):
        segment("text", max_chars=0)


# --------------------------------------------------------------------------
# covers() is itself worth pinning: the write path will assert with it
# --------------------------------------------------------------------------


def test_spans_are_offsets_into_the_original_not_a_normalised_copy():
    """A combining mark and its composed form differ in length, so a divider
    that normalised its input would hand back offsets that do not address the
    text its caller holds -- and every quotation drawn from them would be off
    by however many marks preceded it."""
    text = "か\u3099き\u3099。つぎ。"
    assert len(text) != len(unicodedata.normalize("NFC", text)), "fixture is not composable"
    spans = segment(text)
    assert covers(text, spans)
    assert _texts(text, spans) == ["か\u3099き\u3099。", "つぎ。"]


def test_covers_rejects_a_gap():
    text = "0123456789"
    assert not covers(text, [BlockSpan(0, 4), BlockSpan(5, 10)])


def test_covers_rejects_an_overlap():
    text = "0123456789"
    assert not covers(text, [BlockSpan(0, 6), BlockSpan(5, 10)])


def test_covers_rejects_a_short_tail():
    text = "0123456789"
    assert not covers(text, [BlockSpan(0, 5)])


def test_covers_accepts_the_real_thing():
    text = "一文。二文。\n\n段落。"
    assert covers(text, segment(text))
