"""MCP boundary-layer forwarding tests (CSC Task #362 part 2).

The six functions under ``cpersona.server.*_boundary`` are the MCP entry
point for every tool that carries a project_id. They run the operating-
context gate (``check_project_id`` + ``_oc_reject`` + ``_oc_annotate``),
call the underlying library handler, and — for the two recall variants —
apply the preview tier. Existing coverage pins the gate and the preview:

- ``test_operating_context.py`` — @auto resolve+echo, reject blocks before
  the handler, dormant-transparent for ``do_store_boundary``; reject
  warn-but-serve for ``do_recall_boundary``.
- ``test_recall_preview.py`` — preview trimming, ``full_content`` bypass,
  ``RECALL_PREVIEW_CHARS=0`` kill, and short rows left unmarked, all on
  ``do_recall_boundary``.

The gap this file closes is the third slice: **every argument the boundary
receives is forwarded to the underlying handler unchanged, and no
boundary-layer kwarg leaks past the boundary.** A boundary that silently
drops ``channel`` / ``source_id`` / ``deep`` / ``exclude_contents`` is
exactly the failure this layer invites — invisible from the return value,
because the underlying handler will still return something plausible when
a kwarg it never received defaults to zero. The tests assert forwarding by
substituting the underlying handler with a capture-and-return stub, then
comparing the captured positional args and kwargs against what the
boundary was called with. The negative half (``full_content`` NOT reaching
``do_recall``) is asserted symmetrically: ``do_recall`` does not accept
``full_content`` in its signature, so a copy-paste ``**kw`` forward here
would break the library API contract.

Coverage split with the existing suite:

- ``do_store_boundary``   — new: forwarding (already: @auto, reject,
  dormant transparent — ``test_operating_context.py``).
- ``do_recall_boundary``  — new: forwarding + ``full_content`` non-forward
  (already: reject warn-but-serve — ``test_operating_context.py``;
  preview trim, short-row pass-through, ``RECALL_PREVIEW_CHARS=0`` kill,
  ``full_content`` bypass — ``test_recall_preview.py``).
- ``do_archive_episode_boundary``, ``do_list_memories_boundary``,
  ``do_list_episodes_boundary``, ``do_recall_with_context_boundary`` — no
  direct test at all before this file. Everything asserted here is new,
  including (for the with_context recall) the preview-by-default and
  ``full_content``-bypasses property that already exists on the plain
  recall variant in ``test_recall_preview.py``.

Note on the operating-context feature: conftest pins
``CPERSONA_OPERATING_CONTEXT=off`` (dormant), so ``check_project_id``
passes ``project_id`` through unchanged — the substituted handler sees the
same value the boundary caller passed. That is the mode we WANT for
forwarding tests; the gate's active-mode paths are exercised in
``test_operating_context.py`` and are outside this file's scope.
"""
import pytest

from cpersona import config, server


# ---------------------------------------------------------------------------
# do_store_boundary — 4 args (agent_id, message, channel, project_id)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_boundary_forwards_every_arg_to_do_store(monkeypatch):
    """Positional (agent_id, message) and kwargs (channel, project_id) reach
    do_store; nothing else does.

    The negative half is load-bearing: the exact-equality kwargs assertion
    fails loudly if a future refactor copy-pasted extra kwargs (e.g. from
    the recall boundary) into this one.
    """
    captured: dict = {}

    async def fake_store(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {"ok": True, "id": 42}

    monkeypatch.setattr(server, "do_store", fake_store)

    message = {"content": "msg-payload", "id": "m-1"}
    result = await server.do_store_boundary("a-1", message, channel="c-1", project_id="")

    assert captured["args"] == ("a-1", message)
    assert captured["kwargs"] == {"channel": "c-1", "project_id": ""}
    assert result == {"ok": True, "id": 42}


@pytest.mark.asyncio
async def test_store_boundary_forwards_defaults(monkeypatch):
    """Defaults on the boundary (channel='', project_id='') reach do_store as
    empty strings, not omitted — the handler's own defaults are the same
    ''/global values, but forwarding the boundary's default explicitly is
    the invariant. Omission would still work today; it stops working the
    moment the handler's default changes."""
    captured: dict = {}

    async def fake_store(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {"ok": True, "id": 1}

    monkeypatch.setattr(server, "do_store", fake_store)

    await server.do_store_boundary("a-1", {"content": "x"})

    assert captured["args"] == ("a-1", {"content": "x"})
    assert captured["kwargs"] == {"channel": "", "project_id": ""}


# ---------------------------------------------------------------------------
# do_archive_episode_boundary — 7 args (agent_id, history, summary,
# keywords, resolved, project_id, channel).
#
# The substitution point is do_archive_episode_or_queue (server.py's own
# wrapper), NOT do_archive_episode: the boundary calls the wrapper by name
# and the wrapper's no-persist / queue-empty-summary branches are not
# part of what this test is pinning — those are wrapper concerns and
# already covered by test_no_persist.py.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_archive_episode_boundary_forwards_every_arg(monkeypatch):
    captured: dict = {}

    async def fake_or_queue(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {"ok": True, "queued": False, "episode_id": 7}

    monkeypatch.setattr(server, "do_archive_episode_or_queue", fake_or_queue)

    history = [{"role": "user", "content": "hi"}]
    result = await server.do_archive_episode_boundary(
        "a-1", history,
        summary="sum-body", keywords="kw", resolved=True,
        project_id="", channel="c-1",
    )

    assert captured["args"] == ("a-1", history)
    assert captured["kwargs"] == {
        "summary": "sum-body",
        "keywords": "kw",
        "resolved": True,
        "project_id": "",
        "channel": "c-1",
    }
    assert result == {"ok": True, "queued": False, "episode_id": 7}


@pytest.mark.asyncio
async def test_archive_episode_boundary_forwards_resolved_None(monkeypatch):
    """`resolved: bool | None = None` is a distinct third state (not
    inferred, deferred to the caller). It must reach the wrapper as None,
    not silently coerced to False."""
    captured: dict = {}

    async def fake_or_queue(*args, **kwargs):
        captured["kwargs"] = kwargs
        return {"ok": True, "episode_id": 0}

    monkeypatch.setattr(server, "do_archive_episode_or_queue", fake_or_queue)

    await server.do_archive_episode_boundary("a-1", [], summary="s")
    assert captured["kwargs"]["resolved"] is None


# ---------------------------------------------------------------------------
# do_list_memories_boundary / do_list_episodes_boundary — 3 args each
# (agent_id, limit, project_id). project_id defaults to None here (no
# filter), unlike the write boundaries which default to '' (global pool).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_memories_boundary_forwards_every_arg(monkeypatch):
    captured: dict = {}

    async def fake_list(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {"memories": [], "count": 0}

    monkeypatch.setattr(server, "do_list_memories", fake_list)

    result = await server.do_list_memories_boundary("a-1", 25, project_id="p-1")

    assert captured["args"] == ("a-1", 25)
    assert captured["kwargs"] == {"project_id": "p-1"}
    assert result == {"memories": [], "count": 0}


@pytest.mark.asyncio
async def test_list_memories_boundary_project_id_default_is_None(monkeypatch):
    """Reads default to `None` = no project filter, distinct from `""` =
    global-only (γ semantics). A boundary that coerced None to '' silently
    would narrow every unfiltered dashboard query to global rows only."""
    captured: dict = {}

    async def fake_list(*args, **kwargs):
        captured["kwargs"] = kwargs
        return {"memories": [], "count": 0}

    monkeypatch.setattr(server, "do_list_memories", fake_list)
    await server.do_list_memories_boundary("a-1", 25)
    assert captured["kwargs"] == {"project_id": None}


@pytest.mark.asyncio
async def test_list_episodes_boundary_forwards_every_arg(monkeypatch):
    captured: dict = {}

    async def fake_list(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {"episodes": [], "count": 0}

    monkeypatch.setattr(server, "do_list_episodes", fake_list)

    result = await server.do_list_episodes_boundary("a-1", 25, project_id="p-1")

    assert captured["args"] == ("a-1", 25)
    assert captured["kwargs"] == {"project_id": "p-1"}
    assert result == {"episodes": [], "count": 0}


@pytest.mark.asyncio
async def test_list_episodes_boundary_project_id_default_is_None(monkeypatch):
    captured: dict = {}

    async def fake_list(*args, **kwargs):
        captured["kwargs"] = kwargs
        return {"episodes": [], "count": 0}

    monkeypatch.setattr(server, "do_list_episodes", fake_list)
    await server.do_list_episodes_boundary("a-1", 25)
    assert captured["kwargs"] == {"project_id": None}


# ---------------------------------------------------------------------------
# do_recall_boundary — 8 forwarded args
# (agent_id, query, limit, deep, channel, exclude_contents, project_id, source_id)
# `full_content` is a boundary-layer preview switch; it must NOT reach
# do_recall (whose signature does not accept it).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recall_boundary_forwards_every_arg_to_do_recall(monkeypatch):
    """Positional (agent_id, query, limit) and kwargs
    (deep, channel, exclude_contents, project_id, source_id) reach do_recall.

    Distinct sentinel values so a boundary that swapped e.g. channel with
    source_id would flip both assertions rather than one -- the mirror-swap
    class of bug that returning the right SHAPE hides.
    """
    captured: dict = {}

    async def fake_recall(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {"messages": []}

    monkeypatch.setattr(server, "do_recall", fake_recall)

    result = await server.do_recall_boundary(
        "a-1", "query-text", 7, True, "c-1", ["prior-content"], "", "src:1"
    )

    assert captured["args"] == ("a-1", "query-text", 7)
    assert captured["kwargs"] == {
        "deep": True,
        "channel": "c-1",
        "exclude_contents": ["prior-content"],
        "project_id": "",
        "source_id": "src:1",
    }
    assert result == {"messages": []}


@pytest.mark.asyncio
async def test_recall_boundary_full_content_stays_at_the_boundary(monkeypatch):
    """`full_content` is a preview-tier switch (the two-liner in
    _apply_preview): it must NOT reach do_recall.

    do_recall's signature has no full_content parameter, so a boundary that
    started forwarding **kw would either raise TypeError at call time or
    silently drop the kwarg depending on how the copy went. Either way is a
    regression — the guarantee is that the library API contract is stable.
    """
    captured: dict = {}

    async def fake_recall(*args, **kwargs):
        captured["kwargs"] = kwargs
        return {"messages": []}

    monkeypatch.setattr(server, "do_recall", fake_recall)

    await server.do_recall_boundary(
        "a-1", "q", 5, False, "", [], "", "", full_content=True
    )
    assert "full_content" not in captured["kwargs"]


# ---------------------------------------------------------------------------
# do_recall_with_context_boundary — 8 forwarded args
# (agent_id, query, external_context, limit, channel, deep, project_id, source_id)
# Same non-forward property for full_content, and the same preview /
# full_content-bypass behaviour test_recall_preview.py already pins on
# do_recall_boundary — this variant had none of it before this file.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recall_with_context_boundary_forwards_every_arg(monkeypatch):
    """Positional (agent_id, query) and kwargs (external_context, limit,
    channel, deep, project_id, source_id) reach do_recall_with_context.

    Note the argument ordering here differs from do_recall_boundary
    (limit before channel/deep vs after) — a boundary that copy-pasted
    the wrong order would still return {"messages": []} and this test
    is the only one that would notice.
    """
    captured: dict = {}

    async def fake_rwc(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {"messages": []}

    monkeypatch.setattr(server, "do_recall_with_context", fake_rwc)

    ext = [{"role": "user", "content": "prior turn"}]
    result = await server.do_recall_with_context_boundary(
        "a-1", "query-text", ext, 7, "c-1", True, "", "src:1"
    )

    assert captured["args"] == ("a-1", "query-text")
    assert captured["kwargs"] == {
        "external_context": ext,
        "limit": 7,
        "channel": "c-1",
        "deep": True,
        "project_id": "",
        "source_id": "src:1",
    }
    assert result == {"messages": []}


@pytest.mark.asyncio
async def test_recall_with_context_boundary_full_content_stays_at_the_boundary(monkeypatch):
    captured: dict = {}

    async def fake_rwc(*args, **kwargs):
        captured["kwargs"] = kwargs
        return {"messages": []}

    monkeypatch.setattr(server, "do_recall_with_context", fake_rwc)

    await server.do_recall_with_context_boundary(
        "a-1", "q", [], 5, "", False, "", "", full_content=True
    )
    assert "full_content" not in captured["kwargs"]


@pytest.mark.asyncio
async def test_recall_with_context_boundary_previews_by_default_and_full_content_bypasses(
    monkeypatch,
):
    """Mirrors test_recall_preview.py::
    test_recall_boundary_previews_by_default_and_full_content_bypasses
    for the with_context variant, which had no equivalent test before.
    """
    monkeypatch.setattr(config, "RECALL_PREVIEW_CHARS", 10)
    long = "0123456789ABCDEF"

    async def fake_rwc(agent_id, query, **kw):
        return {"messages": [{"ref": "mem:1", "content": long}]}

    monkeypatch.setattr(server, "do_recall_with_context", fake_rwc)

    trimmed = await server.do_recall_with_context_boundary(
        "a-1", "q", [], 5, "", False, "", ""
    )
    assert trimmed["messages"][0]["content"] == long[:10]
    assert trimmed["messages"][0]["content_truncated"] is True

    full = await server.do_recall_with_context_boundary(
        "a-1", "q", [], 5, "", False, "", "", full_content=True
    )
    assert full["messages"][0]["content"] == long
    assert "content_truncated" not in full["messages"][0]
