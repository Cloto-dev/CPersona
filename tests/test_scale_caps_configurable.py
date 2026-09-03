"""The caps sized for a 10,000-row corpus are settings, not literals.

Four absolute numbers used to sit in the modules that consume them: how many
holes the vector index may name, how many NULL-embedding rows one repair run
re-embeds, how many rows the near-duplicate check compares, and how many
offending source rows one run classifies. Each was chosen against a corpus of
about 10,000 rows, and each means something different against 150,000 — a cap
that used to cover the whole corpus becomes a sample of it, silently, because a
cap that bites returns a smaller answer rather than an error.

So this file pins two separable things:

- the shipped defaults, which are the numbers the measurements in ``config.py``
  justify, and
- that the consuming module reads the config value rather than carrying its own
  copy. The two failures look identical from the outside: a module that
  hard-codes ``1000`` again answers exactly as it does today on a small corpus
  and ignores the environment variable on a large one.

The override tests reload ``config`` rather than monkeypatching the attribute,
because the question is whether the value is *parsed* from the environment —
an attribute set by a test proves only that Python has attributes.
"""

# No environment pinning here: conftest.py fixes CPERSONA_DB_PATH and
# CPERSONA_EMBEDDING_MODE before any cpersona module is imported, and these
# tests set and unset only the variables they are about.
import importlib

import pytest

from cpersona import checks, config, vector_index

# (config attribute, environment variable, shipped default)
_CAPS = [
    ("VECTOR_INDEX_MAX_EXCLUDED_IDS", "CPERSONA_VECTOR_INDEX_MAX_EXCLUDED_IDS", 10000),
    ("REEMBED_ROW_CAP", "CPERSONA_REEMBED_ROW_CAP", 5000),
    ("NEAR_DUPLICATE_ROW_CAP", "CPERSONA_NEAR_DUPLICATE_ROW_CAP", 5000),
    ("INVALID_SOURCE_CLASSIFY_CAP", "CPERSONA_INVALID_SOURCE_CLASSIFY_CAP", 10000),
    ("CALIBRATE_MAX_SAMPLE", "CPERSONA_CALIBRATE_MAX_SAMPLE", 5000),
]


@pytest.mark.parametrize("attr,env,default", _CAPS, ids=[c[0] for c in _CAPS])
def test_the_shipped_default_is_pinned(attr, env, default):
    """The fallback the suite runs under, read from the module the server imports."""
    assert getattr(config, attr) == default


@pytest.mark.parametrize("attr,env,default", _CAPS, ids=[c[0] for c in _CAPS])
def test_the_environment_moves_the_cap(monkeypatch, attr, env, default):
    """An operator with a six-figure corpus has a knob, and it is this one.

    The value used is deliberately not a multiple of the default: a reload that
    silently kept the default would still differ from a doubled number only by
    arithmetic nobody checks.
    """
    monkeypatch.setenv(env, "37")
    reloaded = importlib.reload(config)
    try:
        assert getattr(reloaded, attr) == 37, (
            f"{env} did not reach {attr}: the cap is a literal again"
        )
    finally:
        monkeypatch.delenv(env)
        importlib.reload(config)


@pytest.mark.parametrize("attr,env,default", _CAPS, ids=[c[0] for c in _CAPS])
def test_a_malformed_value_falls_back_to_the_default(monkeypatch, attr, env, default):
    """bug-133's warn-and-default path, which is what makes the knob safe to
    document: a typo in a deployment's environment must not stop the server from
    importing, and must not leave the cap at some coerced zero either."""
    monkeypatch.setenv(env, "not-a-number")
    reloaded = importlib.reload(config)
    try:
        assert getattr(reloaded, attr) == default
    finally:
        monkeypatch.delenv(env)
        importlib.reload(config)


def test_the_consumers_read_the_config_value():
    """The half a default assertion cannot make.

    Every test above would stay green against a `checks.py` that carried its own
    `NEAR_DUPLICATE_ROW_CAP = 5000`, because the numbers would agree — until an
    operator set the environment variable and only one of the two moved. What is
    asserted here is the wiring: the consuming module's name IS the config value.
    """
    assert checks.NEAR_DUPLICATE_ROW_CAP == config.NEAR_DUPLICATE_ROW_CAP
    assert checks.INVALID_SOURCE_CLASSIFY_CAP == config.INVALID_SOURCE_CLASSIFY_CAP
    assert checks.REEMBED_ROW_CAP == config.REEMBED_ROW_CAP
    assert vector_index.MAX_EXCLUDED_IDS == config.VECTOR_INDEX_MAX_EXCLUDED_IDS


def test_the_consumers_do_not_carry_their_own_copy_of_the_number():
    """The equality above is blind to one mutation: a module that hard-codes the
    SAME number reads as wired while the environment variable moves only config.
    Reloading the consumer to tell the two apart is not available — other modules
    hold references into ``checks`` — so the assignment itself is what is
    inspected, which is the level the mutation lives at anyway.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent / "cpersona"
    expected = {
        root / "checks.py": [
            "NEAR_DUPLICATE_ROW_CAP",
            "INVALID_SOURCE_CLASSIFY_CAP",
            "REEMBED_ROW_CAP",
        ],
        root / "vector_index.py": ["MAX_EXCLUDED_IDS"],
    }
    for source, names in expected.items():
        text = source.read_text()
        for name in names:
            assignments = re.findall(rf"^{name} = (.+)$", text, re.MULTILINE)
            assert len(assignments) == 1, (
                f"{source.name} assigns {name} {len(assignments)} times; this gate reads one"
            )
            assert assignments[0].startswith("config."), (
                f"{source.name} sets {name} to {assignments[0]!r} instead of reading it "
                "from config — the environment variable would move one of the two"
            )


def test_the_quadratic_caps_stay_inside_the_measured_envelope():
    """The two caps that bound an O(n^2) dense cosine matrix are bounded in turn
    by what was actually measured: 5,000 rows of 1024-d float32 peaked at 266 MB
    and 10,000 at 982 MB, so a default past 10,000 would be asking an 8 GB
    machine for a gigabyte of scratch inside a maintenance call — a number no
    measurement here supports. The environment variable can still exceed this;
    the SHIPPED default may not."""
    assert config.NEAR_DUPLICATE_ROW_CAP <= 10000
    assert config.CALIBRATE_MAX_SAMPLE <= 10000
