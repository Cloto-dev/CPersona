"""The cap's token count and the qualifier check, computed faster, give what they gave before.

Both were rewritten for speed only: ``reconstruct._settle_tokens`` tokenizes a response once
instead of on every pass, and ``blocks._starts_with_qualifier`` compares the qualifier lists in
one call each and lowers only the prefix it compares. Each is checked here against the earlier
implementation, kept below as the reference, on ordinary and adversarial inputs.
"""

import random

import pytest

from cpersona import blocks, reconstruct, tokens


def _settle_reference(out: dict) -> int:
    """``_settle_tokens`` as it was through 2.6.11: the whole response counted on every pass."""
    out["used_tokens"] = 0
    for _ in range(4):
        n = tokens.count_json(out)
        if n == out["used_tokens"]:
            return n
        out["used_tokens"] = n
    raise RuntimeError("did not settle")


def _qualifier_reference(fragment: str) -> bool:
    """``_starts_with_qualifier`` as it was through 2.6.11."""
    stripped = fragment.lstrip()
    lowered = stripped.lower()
    return any(lowered.startswith(q) if q.isascii() else stripped.startswith(q) for q in blocks._QUALIFIERS)


# Characters that stress the tokenizer around a number and the lowering of a prefix: digits
# next to the field, punctuation, whitespace of several kinds, a capital sigma (the one
# lowering that depends on what follows), a dotted capital I (lowers to two characters), the
# Kelvin sign (lowers to an ASCII "k"), and Japanese.
_ALPHABET = list("abcdefghijklmnopqrstuvwxyz ABCDEFGHIJ0123456789.,:;!?\"'{}[]-_\n\t") + [
    "Σ", "İ", "K", "　", "。", "、", "ただし", "なお", "記憶", "😀",
]


def _random_text(rng: random.Random, length: int) -> str:
    return "".join(rng.choice(_ALPHABET) for _ in range(length))


def _random_response(rng: random.Random) -> dict:
    out = {"items": [{"ref": f"mem:{rng.randint(0, 10**6)}", "content": _random_text(rng, rng.randint(0, 400))}
                     for _ in range(rng.randint(0, 8))]}
    if rng.random() < 0.5:
        out["used_tokens"] = rng.randint(0, 10**7)  # the field already present, mid-object
    out["returned_count"] = len(out["items"])
    if rng.random() < 0.5:
        out["note"] = _random_text(rng, rng.randint(0, 60))
    return out


@pytest.mark.parametrize("seed", range(40))
def test_settled_count_is_the_reference_count(seed):
    rng = random.Random(seed)
    for _ in range(10):
        response = _random_response(rng)
        fast, slow = dict(response), dict(response)
        assert reconstruct._settle_tokens(fast) == _settle_reference(slow)
        assert fast == slow


@pytest.mark.parametrize("pad", list(range(985, 1015)) + list(range(330, 345)))
def test_settled_count_matches_the_reference_where_the_digits_change(pad):
    # Around 1,000 tokens the count's own digits go from one token to two.
    fast = {"items": [], "content": "a " * pad}
    slow = dict(fast)
    assert reconstruct._settle_tokens(fast) == _settle_reference(slow)
    assert fast == slow == {**fast, "used_tokens": tokens.count_json(fast)}


@pytest.mark.parametrize("seed", range(20))
def test_a_value_in_the_field_adds_only_its_own_digits(seed):
    # What the rewrite relies on: the count with any value in the field is the count with
    # 0 there, less the tokens of "0", plus the tokens of the value.
    rng = random.Random(1000 + seed)
    response = _random_response(rng)
    response["used_tokens"] = 0
    base = tokens.count_json(response) - tokens.count("0")
    for value in [rng.randint(0, 10**9) for _ in range(30)] + [9, 10, 99, 100, 999, 1000, 999999, 1000000]:
        response["used_tokens"] = value
        assert tokens.count_json(response) == base + tokens.count(str(value))


def _qualifier_cases(rng: random.Random) -> list[str]:
    cases = []
    for q in blocks._QUALIFIERS:
        for variant in {q, q.upper(), q.title(), q.capitalize(), q[:-1], q + "x"}:
            for lead in ("", " ", "\n\t ", "　"):
                cases.append(lead + variant)
                cases.append(lead + variant + " " + _random_text(rng, 30))
    # Fragments whose lowering changes length or depends on context inside the compared prefix.
    for prefix in ("İnstead", "Keep", "Σome", "howeverΣ", "on the other hanΣ"):
        cases.extend([prefix, prefix + " rest", " " + prefix.upper()])
    cases.extend(_random_text(rng, rng.randint(0, 80)) for _ in range(3000))
    return cases


@pytest.mark.parametrize("seed", range(5))
def test_qualifier_check_is_the_reference_check(seed):
    rng = random.Random(seed)
    for fragment in _qualifier_cases(rng):
        assert blocks._starts_with_qualifier(fragment) is _qualifier_reference(fragment), repr(fragment)


def test_every_qualifier_is_found_in_either_case_after_leading_space():
    for q in blocks._QUALIFIERS:
        assert blocks._starts_with_qualifier("  " + q + " the rest")
        if q.isascii():
            assert blocks._starts_with_qualifier("\n" + q.upper() + " the rest")
