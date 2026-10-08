"""Morpheme-aware phrases for the keyword arms (CPERSONA_QUERY_SEGMENTER, measurement variants).

The shipped builder cuts every Japanese or Chinese run of a query into all of its
overlapping three-character pieces and ORs them. FTS5's bm25 adds up the phrases a row
holds, so a request's own wording counts: "bug-191 について教えて" is one phrase for the
identifier and five for "について教えて", and a row that says "について" twice can
outscore the one row that names bug-191. These variants cut each run into morphemes
first (SudachiPy, split mode C, the core dictionary) and keep the pieces that come from
content words, so the wording of the request stops voting.

A morpheme is a function word, and is left out, when its first part of speech is a
particle, an auxiliary verb, punctuation or a symbol, a space, a conjunction, an
interjection, an adnominal, a pronoun or an adverb, or when it is marked as not
independent (する, ある, こと, ...).

- ``morph`` joins the kept morphemes that are adjacent in the text into runs and cuts the
  runs of three characters or more into trigrams, as the shipped builder cuts a run.
  A shorter run cannot match a trigram index and is dropped, as a short run always was.
- ``morph_overlap`` keeps nouns only (with prefixes, suffixes and adjectival nouns), and
  keeps each of the shipped builder's trigrams that has at least two of its three
  characters inside them. A two-character noun joined to another by a particle
  ("共通の契約") is then still found through the trigrams that bridge the particle.

ASCII terms are built exactly as the shipped builder builds them, so an identifier such
as ``CVE-2024-3094`` stays one phrase. SudachiPy is not a dependency of CPersona: these
variants exist to be measured, and selecting one without it installed raises.
"""

from functools import lru_cache

_FUNCTION_WORDS = frozenset(
    {"助詞", "助動詞", "補助記号", "記号", "空白", "接続詞", "感動詞", "連体詞", "代名詞", "副詞"}
)
_NOUNISH = frozenset({"名詞", "接頭辞", "接尾辞", "形状詞"})


@lru_cache(maxsize=1)
def _tokenizer():
    from sudachipy import Dictionary

    return Dictionary(dict="core").tokenizer()


def _morphemes(run: str):
    from sudachipy import SplitMode

    for m in _tokenizer().tokenize(run, SplitMode.C):
        yield m.begin(), m.end(), m.surface(), m.part_of_speech()


def _kept(pos, nouns_only: bool) -> bool:
    if pos[0] in _FUNCTION_WORDS or pos[1] == "非自立可能":
        return False
    return not nouns_only or pos[0] in _NOUNISH


def content_runs(run: str) -> list[str]:
    """The runs of kept morphemes adjacent in ``run`` (``morph``).

    The morphemes of one run tile it, so two kept morphemes are adjacent exactly when
    no left-out morpheme lies between them.
    """
    out: list[str] = []
    cur = ""
    for _begin, _end, surface, pos in _morphemes(run):
        if _kept(pos, nouns_only=False):
            cur += surface
        elif cur:
            out.append(cur)
            cur = ""
    if cur:
        out.append(cur)
    return out


def overlap_trigrams(run: str) -> list[str]:
    """The trigrams of ``run`` with two or more characters in kept nouns (``morph_overlap``)."""
    inside = [False] * len(run)
    for begin, end, _surface, pos in _morphemes(run):
        if _kept(pos, nouns_only=True):
            for i in range(begin, end):
                inside[i] = True
    return [run[i : i + 3] for i in range(len(run) - 2) if sum(inside[i : i + 3]) >= 2]


def cjk_terms(run: str, segmenter: str) -> list[str]:
    """The FTS terms of one Japanese or Chinese run under ``segmenter`` (not ``trigram``)."""
    if len(run) < 3:
        return []
    if segmenter == "morph":
        return [r[i : i + 3] for r in content_runs(run) if len(r) >= 3 for i in range(len(r) - 2)]
    if segmenter == "morph_overlap":
        return overlap_trigrams(run)
    raise ValueError(f"unknown query segmenter: {segmenter}")
