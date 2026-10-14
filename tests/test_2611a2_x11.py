"""check_update: a failed refresh, a detached checkout, apply concurrency, stdin.

Four properties of the update check that each failed quietly:

- A refresh whose fetch fails knows nothing new about the index, so it must
  not replace a verdict that a completed check produced -- above all a
  ``yanked`` one, which is the one finding an operator is expected to act on.
- A checkout parked at a release tag is a detached HEAD, where ``git pull``
  refuses to run. Every surface that hands the operator a command to paste must
  hand that checkout one that works there.
- ``apply`` installs into the environment this process runs from. Two
  installers over one environment are not safe, and a cancelled call leaves its
  child running, so a second apply must be refused until the first one's child
  has exited.
- Under the stdio transport, fd 0 is the client's JSON-RPC stream. A child
  that inherits it can read protocol messages meant for the server.

Every fetch goes through an ``httpx.MockTransport``; no test reaches PyPI.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import httpx
import pytest

from cpersona import config
from cpersona import maintenance_handlers
from cpersona import update_check

RUNNING = "2.5.10"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    update_check._reset()
    monkeypatch.setattr(update_check, "__version__", RUNNING)
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "corpus.sqlite3"))
    monkeypatch.setattr(config, "UPDATE_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "UPDATE_CHECK_INTERVAL_SECONDS", 86400)
    yield
    update_check._reset()


def _files(*versions, yanked=False):
    out = []
    for version in versions:
        for name in (f"cpersona-{version}-py3-none-any.whl", f"cpersona-{version}.tar.gz"):
            out.append({"filename": name, "url": f"https://files.example/{name}", "yanked": yanked})
    return out


def _index(versions, files=None):
    return {"meta": {"api-version": "1.1"}, "name": "cpersona", "versions": list(versions), "files": files or []}


def _yanked_index():
    return _index([RUNNING, "2.5.11"], _files(RUNNING, yanked="broken migration") + _files("2.5.11"))


def _serve(payload, *, status=200):
    async def handler(request):
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(handler)


def _pin_pip_prefix(monkeypatch):
    monkeypatch.setattr(
        update_check, "_installer_prefix", lambda: [sys.executable, "-m", "pip", "install"]
    )


# ---------------------------------------------------------------------------
# A failed refresh keeps the verdict a completed check produced
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_refresh_keeps_a_known_yanked_verdict(monkeypatch):
    """Going offline does not un-withdraw a release.

    The failure branch used to store ``unknown`` stamped with the standing
    check time, so ``version_yanked`` vanished from check_health and a bare
    check_update answered ``unknown`` next to a non-null ``checked_at`` -- the
    state documented as "no check has completed".
    """
    monkeypatch.setattr(update_check, "_transport", _serve(_yanked_index()))
    first = await update_check.refresh()
    assert first["state"] == update_check.STATE_YANKED
    checked_at = update_check.current()["checked_at"]
    assert checked_at is not None

    monkeypatch.setattr(update_check, "_transport", _serve({}, status=503))
    after = await maintenance_handlers.do_check_update(refresh=True)
    assert after["state"] == update_check.STATE_YANKED, after
    assert after["reason"] == "broken migration"
    assert after["checked_at"] == checked_at

    assert update_check.current()["state"] == update_check.STATE_YANKED
    assert [i["type"] for i in update_check.health_issues()] == ["version_yanked"]


@pytest.mark.asyncio
async def test_an_unusable_index_keeps_a_known_verdict_and_the_cache(monkeypatch):
    """A document whose schema changed is the same answer as an outage (rule 2),
    so it gets the same treatment: the known verdict stands, in memory and in
    the cache file, instead of a day-long cached ``unknown``."""
    monkeypatch.setattr(update_check, "_transport", _serve(_yanked_index()))
    await update_check.refresh()
    checked_at = update_check.current()["checked_at"]

    monkeypatch.setattr(
        update_check, "_transport", _serve({"meta": {}, "name": "cpersona", "versions": 3, "files": []})
    )
    result = await update_check.refresh()
    assert result["state"] == update_check.STATE_YANKED, result
    assert update_check.current()["checked_at"] == checked_at
    cached = json.loads(open(update_check.cache_path(), encoding="utf-8").read())
    assert cached["verdict"]["state"] == update_check.STATE_YANKED, cached


@pytest.mark.asyncio
async def test_a_failed_refresh_does_not_rearm_a_notice_already_delivered(monkeypatch):
    """The notice is once per session for a verdict that has not changed.

    Wiping the verdict on a failed fetch counted as a change, and so did its
    return on the next success, so every session was told the same withdrawn
    release a second time.
    """
    monkeypatch.setattr(update_check, "_transport", _serve(_yanked_index()))
    await update_check.refresh()
    assert update_check.notice("sess-a", True)["kind"] == update_check.KIND_YANKED

    monkeypatch.setattr(update_check, "_transport", _serve({}, status=503))
    await update_check.refresh()
    monkeypatch.setattr(update_check, "_transport", _serve(_yanked_index()))
    await update_check.refresh()

    assert update_check.notice("sess-a", True) is None


@pytest.mark.asyncio
async def test_a_slow_failing_fetch_does_not_overwrite_a_fresher_verdict(monkeypatch):
    """The startup fetch, issued first, fails last; an explicit refresh issued
    in the meantime succeeded. The late failure must not erase that answer."""
    started = asyncio.Event()
    release = asyncio.Event()
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            started.set()
            await release.wait()
            return httpx.Response(503, json={})
        return httpx.Response(200, json=_index([RUNNING, "2.6.0"]))

    monkeypatch.setattr(update_check, "_transport", httpx.MockTransport(handler))
    startup = asyncio.create_task(update_check.run_startup_check())
    await asyncio.wait_for(started.wait(), timeout=2)

    assert (await update_check.refresh())["available"] == "2.6.0"
    release.set()
    await asyncio.wait_for(startup, timeout=2)

    now = update_check.current()
    assert now["state"] == update_check.STATE_NEWER, now
    assert now["available"] == "2.6.0"
    cached = json.loads(open(update_check.cache_path(), encoding="utf-8").read())
    assert cached["verdict"]["available"] == "2.6.0"


@pytest.mark.asyncio
async def test_a_failed_refresh_with_nothing_known_is_unknown(monkeypatch):
    """The control: with no completed check there is nothing to keep, and the
    answer is the documented one -- unknown, with no check time."""
    monkeypatch.setattr(update_check, "_transport", _serve({}, status=503))
    result = await update_check.refresh()
    assert result["state"] == update_check.STATE_UNKNOWN
    now = update_check.current()
    assert now["state"] == update_check.STATE_UNKNOWN and now["checked_at"] is None


# ---------------------------------------------------------------------------
# A detached checkout is told a command that works there
# ---------------------------------------------------------------------------

_SHA = "0123456789abcdef0123456789abcdef01234567"


def _checkout(monkeypatch, tmp_path, head: str, *, worktree: bool = False) -> str:
    """A source checkout at ``tmp_path/repo`` whose HEAD file holds ``head``."""
    repo = tmp_path / "repo"
    (repo / "cpersona").mkdir(parents=True)
    if worktree:
        # A linked worktree: `.git` is a file naming the per-worktree git dir.
        gitdir = tmp_path / "main.git" / "worktrees" / "repo"
        gitdir.mkdir(parents=True)
        (gitdir / "HEAD").write_text(head + "\n", encoding="utf-8")
        (repo / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    else:
        (repo / ".git").mkdir()
        (repo / ".git" / "HEAD").write_text(head + "\n", encoding="utf-8")
    _pin_pip_prefix(monkeypatch)
    monkeypatch.setattr(update_check.sys, "prefix", "/venv")
    monkeypatch.setattr(update_check, "__file__", str(repo / "cpersona" / "update_check.py"))
    return str(repo)


async def _seed_newer(monkeypatch):
    monkeypatch.setattr(
        update_check, "_transport", _serve(_index([RUNNING, "2.5.11"], _files(RUNNING, "2.5.11")))
    )
    await update_check.refresh()


@pytest.mark.parametrize("worktree", [False, True])
@pytest.mark.asyncio
async def test_a_detached_checkout_is_given_the_tag_command_everywhere(monkeypatch, tmp_path, worktree):
    """`git pull` refuses on a detached HEAD ("You are not currently on a
    branch"), and the `&&` then skips the install. The notice, the check_health
    hint, check_update's command and its message must all carry the form that
    works on a tree parked at a tag."""
    repo = _checkout(monkeypatch, tmp_path, _SHA, worktree=worktree)
    await _seed_newer(monkeypatch)
    expected = (
        f"git -C {repo} fetch --tags && git -C {repo} checkout v2.5.11 && "
        f"{sys.executable} -m pip install {repo}"
    )

    assert update_check.detect_install("2.5.11")["command"] == expected

    notice = update_check.notice("sess-a", True)
    assert notice["install"]["command"] == expected
    assert expected in notice["message"]

    (issue,) = update_check.health_issues()
    assert issue["hint"].startswith(expected)

    result = await maintenance_handlers.do_check_update()
    assert result["install"]["command"] == expected
    assert expected in result["message"]
    for text in (notice["message"], issue["hint"], result["message"]):
        assert " pull" not in text, text


@pytest.mark.asyncio
async def test_a_checkout_on_a_branch_keeps_the_pull_command(monkeypatch, tmp_path):
    """The control: a tree on a branch is told `git pull`, and apply runs it."""
    repo = _checkout(monkeypatch, tmp_path, "ref: refs/heads/main")
    install = update_check.detect_install("2.5.11")
    assert install["command"] == f"git -C {repo} pull && {sys.executable} -m pip install {repo}"
    assert install["argv_steps"][0] == ["git", "-C", repo, "pull"]


@pytest.mark.asyncio
async def test_an_unreadable_head_keeps_the_pull_command(monkeypatch, tmp_path):
    """No `.git` beside the package: the state is unknown, and the command is
    the one apply would run, which then reports what git finds."""
    _pin_pip_prefix(monkeypatch)
    monkeypatch.setattr(update_check.sys, "prefix", "/venv")
    (tmp_path / "plain" / "cpersona").mkdir(parents=True)
    monkeypatch.setattr(update_check, "__file__", str(tmp_path / "plain" / "cpersona" / "update_check.py"))
    install = update_check.detect_install("2.5.11")
    assert install["command"].startswith(f"git -C {tmp_path / 'plain'} pull && ")


# ---------------------------------------------------------------------------
# apply is single-flight, and a cancelled call keeps the slot until its child exits
# ---------------------------------------------------------------------------

_STEP_SCRIPT = """
import os, sys, time
log = sys.argv[1]
with open(log, "a") as h:
    h.write(f"start {sys.argv[2]} {os.getpid()}\\n")
time.sleep(float(sys.argv[3]))
with open(log, "a") as h:
    h.write(f"end {sys.argv[2]} {os.getpid()}\\n")
"""


def _slow_steps(monkeypatch, tmp_path, *names, seconds=1.0):
    script = tmp_path / "step.py"
    script.write_text(_STEP_SCRIPT, encoding="utf-8")
    log = tmp_path / "steps.log"
    log.write_text("", encoding="utf-8")
    steps = [[sys.executable, str(script), str(log), name, str(seconds)] for name in names]

    def fake_detect(available=None):
        return {
            "method": update_check.METHOD_PIP,
            "command": " && ".join(" ".join(s) for s in steps),
            "argv_steps": steps,
            "note": "",
            "restart_required": True,
        }

    monkeypatch.setattr(update_check, "detect_install", fake_detect)
    return log


def _lines(log) -> list[str]:
    return [line for line in log.read_text(encoding="utf-8").splitlines() if line]


def _apply_slot_free() -> bool:
    task = getattr(update_check, "_apply_task", None)
    return task is None or task.done()


async def _wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_a_second_apply_is_refused_while_a_cancelled_one_still_installs(monkeypatch, tmp_path):
    """A client that times out cancels the call, but the installer keeps
    running. A retry must not start a second installer over the same
    environment while the first is still writing."""
    await _seed_newer(monkeypatch)
    log = _slow_steps(monkeypatch, tmp_path, "one", seconds=1.0)

    first = asyncio.create_task(update_check.apply())
    await _wait_for(lambda: any(line.startswith("start") for line in _lines(log)))
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    retry = await update_check.apply()
    assert retry["applied"] is False, retry
    assert "already running" in retry["reason"]
    assert [line.split()[0] for line in _lines(log)] == ["start"], _lines(log)

    # Once the orphaned child has exited, the slot is free again.
    await _wait_for(lambda: any(line.startswith("end") for line in _lines(log)))
    await _wait_for(_apply_slot_free)
    again = await update_check.apply()
    assert again["applied"] is True, again
    starts = [line for line in _lines(log) if line.startswith("start")]
    assert len(starts) == 2


@pytest.mark.asyncio
async def test_two_concurrent_applies_run_one_installer(monkeypatch, tmp_path):
    await _seed_newer(monkeypatch)
    log = _slow_steps(monkeypatch, tmp_path, "one", seconds=0.5)
    a, b = await asyncio.gather(update_check.apply(), update_check.apply())
    assert sorted([a["applied"], b["applied"]]) == [False, True], (a, b)
    assert len([line for line in _lines(log) if line.startswith("start")]) == 1


@pytest.mark.asyncio
async def test_a_cancelled_apply_starts_no_further_step(monkeypatch, tmp_path):
    """Cancelling still means "do not start anything else": the step that is
    running finishes, the next one never starts."""
    await _seed_newer(monkeypatch)
    log = _slow_steps(monkeypatch, tmp_path, "one", "two", seconds=0.5)

    first = asyncio.create_task(update_check.apply())
    await _wait_for(lambda: any(line.startswith("start one") for line in _lines(log)))
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await _wait_for(lambda: any(line.startswith("end one") for line in _lines(log)))
    await _wait_for(_apply_slot_free)
    await asyncio.sleep(0.3)
    names = [line.split()[1] for line in _lines(log)]
    assert names == ["one", "one"], _lines(log)


# ---------------------------------------------------------------------------
# The apply child does not inherit stdin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_apply_step_cannot_read_the_servers_stdin():
    """Under stdio, fd 0 carries the client's requests. A step that prompts --
    pip asking for index credentials -- would consume them. The child is given
    an empty stdin instead."""
    read_end, write_end = os.pipe()
    os.write(write_end, b'{"jsonrpc": "2.0", "id": 7, "method": "tools/call"}\n')
    os.close(write_end)
    saved = os.dup(0)
    try:
        os.dup2(read_end, 0)
        code, output = await update_check._run_step(
            [sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"]
        )
    finally:
        os.dup2(saved, 0)
        os.close(saved)
        os.close(read_end)
    assert code == 0
    assert output.strip() == "''", output
