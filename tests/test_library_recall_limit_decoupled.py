"""The library recall ceiling is its own bound, not the vector scan window.

Both numbers used to be ``MAX_MEMORIES``. They answer different questions --
how far back the vector retriever looks, and how many rows one ``do_recall``
call may materialise -- so widening the window for a larger corpus widened a
response bound by the same factor, with nobody choosing to. The read/write
separation stated on ``MAX_CONTENT_LENGTH`` is the same rule; this is that rule
applied to the scan window.

The clamp is also made audible here. A ceiling that bites does not raise: it
returns a shallower ranking that looks like a result, which is exactly how the
old in-library cap of 100 collapsed LMEB deep ranking (81.17 -> 48.98) without
anything in the run saying so.
"""

import os
import tempfile

os.environ.setdefault(
    "CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "test_library_recall_limit.db")
)
os.environ.setdefault("CPERSONA_EMBEDDING_MODE", "none")

import importlib  # noqa: E402
import logging  # noqa: E402

import pytest  # noqa: E402

from cpersona import config  # noqa: E402
from cpersona import memory_handlers as M  # noqa: E402


def test_library_ceiling_default_is_pinned():
    """The shipped fallback, read from the environment the suite runs in."""
    assert config.RECALL_LIBRARY_MAX_LIMIT == 10000


def test_library_ceiling_does_not_follow_the_scan_window(monkeypatch):
    """Raising CPERSONA_MAX_MEMORIES must leave the response bound where it is.

    This is the whole point of the split: the scale ladder raises the window for
    a six-figure corpus, and that must not hand a library caller a 20x larger
    materialisation budget as a side effect.
    """
    monkeypatch.setenv("CPERSONA_MAX_MEMORIES", "200000")
    reloaded = importlib.reload(config)
    try:
        assert reloaded.MAX_MEMORIES == 200000
        assert reloaded.RECALL_LIBRARY_MAX_LIMIT == 10000, (
            "the library recall ceiling moved with the scan window -- the two "
            "bounds are coupled again"
        )
    finally:
        monkeypatch.delenv("CPERSONA_MAX_MEMORIES")
        importlib.reload(config)


def test_library_ceiling_is_configurable(monkeypatch):
    """A bench that legitimately ranks a corpus deeper than the ceiling has a knob."""
    monkeypatch.setenv("CPERSONA_RECALL_LIBRARY_MAX_LIMIT", "150000")
    reloaded = importlib.reload(config)
    try:
        assert reloaded.RECALL_LIBRARY_MAX_LIMIT == 150000
    finally:
        monkeypatch.delenv("CPERSONA_RECALL_LIBRARY_MAX_LIMIT")
        importlib.reload(config)


@pytest.mark.asyncio
async def test_handler_clamps_by_the_library_ceiling_not_the_window(caplog):
    """The tests above pin the constant; this one pins the *use*.

    The two are separable failures: a later edit can put ``MAX_MEMORIES`` back
    into the handler's clamp while ``RECALL_LIBRARY_MAX_LIMIT`` sits in config
    doing nothing, and every constant-level assertion here would still pass. So
    this drives ``do_recall`` with the two bounds set far apart and reads which
    one the call actually obeyed.

    The handler imports both by value, so the patch is applied to its module
    rather than to config's.
    """
    caplog.set_level(logging.WARNING, logger=M.__name__)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(M, "RECALL_LIBRARY_MAX_LIMIT", 5)
        mp.setattr(M, "MAX_MEMORIES", 200000)
        await M.do_recall("agent.libceiling", "anything", limit=50)
    messages = [r.getMessage() for r in caplog.records]
    assert any("reduced to the library ceiling 5" in m for m in messages), (
        "limit 50 was not clamped to the library ceiling of 5 -- the handler is "
        f"obeying some other bound (the scan window was 200000). Saw: {messages}"
    )


@pytest.mark.asyncio
async def test_clamped_limit_is_reported(caplog):
    """A caller whose limit is reduced is told, by name and with the way out."""
    with caplog.at_level(logging.WARNING, logger=M.__name__):
        await M.do_recall("agent.libceiling", "anything", limit=config.RECALL_LIBRARY_MAX_LIMIT + 1)
    assert any(
        "CPERSONA_RECALL_LIBRARY_MAX_LIMIT" in r.getMessage() for r in caplog.records
    ), "the clamp bit and said nothing -- this is the silent-shallow-ranking failure"


@pytest.mark.asyncio
async def test_unclamped_limit_is_silent(caplog):
    """And a caller under the ceiling is not warned at every recall."""
    with caplog.at_level(logging.WARNING, logger=M.__name__):
        await M.do_recall("agent.libceiling", "anything", limit=10)
    assert not any(
        "CPERSONA_RECALL_LIBRARY_MAX_LIMIT" in r.getMessage() for r in caplog.records
    )
