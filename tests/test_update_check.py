"""The new-release check: what it decides, what it costs, and what it refuses.

Every test here states what the unfixed (or deliberately mutated) code does, so
that breaking the behaviour turns an assertion red rather than merely changing
a number. Four properties are load-bearing and each has a mutation recorded
against it:

- **pre-releases are never proposed.** Drop the exclusion and an operator
  running the newest final release is told to "upgrade" to an alpha.
- **a session is told once.** Drop the debounce and the notice rides every
  recall response for the life of the process — the cost with none of the news.
- **the fetch is bounded.** Drop the deadline and an index that accepts the
  connection and then says nothing keeps a startup task (and its socket) alive
  for the life of the process.
- **apply executes an argv list.** Build a shell string instead and a version
  string that arrived over the network reaches a shell.

Nothing here touches the network: the fetch path runs for real — client,
timeout plumbing, JSON decode — against an ``httpx.MockTransport`` serving
fixture bytes. A test that reached PyPI would be a test whose result depends on
what was published this morning.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import time

import httpx
import pytest

from cpersona import config
from cpersona import maintenance_handlers
from cpersona import memory_handlers as M
from cpersona import update_check

RUNNING = "2.5.10"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Per-test isolation for a module whose state is per-process.

    The running version is pinned: the real one moves with every release, and a
    fixture index written against "whatever is installed today" would decide a
    different verdict next month for no reason anyone could see.
    """
    update_check._reset()
    monkeypatch.setattr(update_check, "__version__", RUNNING)
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "corpus.sqlite3"))
    monkeypatch.setattr(config, "UPDATE_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "UPDATE_CHECK_INTERVAL_SECONDS", 86400)
    yield
    update_check._reset()


# --- fixture index documents ------------------------------------------------


def _files(*versions, yanked=False):
    """The two distribution files PyPI publishes per release of this project."""
    out = []
    for version in versions:
        for name in (f"cpersona-{version}-py3-none-any.whl", f"cpersona-{version}.tar.gz"):
            out.append({"filename": name, "url": f"https://files.example/{name}", "yanked": yanked})
    return out


def _index(versions, files=None):
    return {
        "meta": {"api-version": "1.1"},
        "name": "cpersona",
        "versions": list(versions),
        "files": files or [],
    }


def _serve(payload, *, status=200, body=None, delay=0.0, calls=None):
    """A MockTransport that answers the index request, counting its calls."""

    async def handler(request):
        if calls is not None:
            calls.append(str(request.url))
        if delay:
            await asyncio.sleep(delay)
        if body is not None:
            return httpx.Response(
                status, content=body, headers={"content-type": "application/json"}
            )
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(handler)


def _install(monkeypatch, method):
    """Pin the install-method detection so a verdict test does not depend on
    where the suite happens to be running from."""
    # The installer is pinned to pip too: the suite's own venv may be a uv one
    # without pip, and whether it is must not decide what these tests assert.
    monkeypatch.setattr(
        update_check, "_installer_prefix", lambda: [sys.executable, "-m", "pip", "install"]
    )
    if method == "uvx":
        monkeypatch.setattr(update_check.sys, "prefix", "/home/u/.cache/uv/archive-v0/abcdef")
    elif method == "pip":
        monkeypatch.setattr(update_check.sys, "prefix", "/venv")
        monkeypatch.setattr(
            update_check, "__file__", "/venv/lib/python3.11/site-packages/cpersona/update_check.py"
        )
    elif method == "checkout":
        monkeypatch.setattr(update_check.sys, "prefix", "/venv")
        monkeypatch.setattr(update_check, "__file__", "/srv/cpersona/cpersona/update_check.py")
    else:
        raise AssertionError(method)


# ---------------------------------------------------------------------------
# 1. What the index means
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_newer_final_release_is_reported(monkeypatch):
    """A published final above the running version is the whole point.

    Without the comparison the state stays 'ok' and the operator is never told
    the release exists.
    """
    monkeypatch.setattr(
        update_check,
        "_transport",
        _serve(_index(["2.5.9", RUNNING, "2.5.11"], _files("2.5.9", RUNNING, "2.5.11"))),
    )
    verdict = await update_check.refresh()
    assert verdict["state"] == update_check.STATE_NEWER
    assert verdict["kind"] == update_check.KIND_NEWER
    assert verdict["available"] == "2.5.11"
    assert verdict["running"] == RUNNING


@pytest.mark.asyncio
async def test_only_newer_prereleases_is_not_news(monkeypatch):
    """M1: with the pre-release exclusion removed, 2.6.0a1/2.5.11rc1 become the
    'available' upgrade and every operator on the newest FINAL release is told
    to install an alpha. The correct answer is state='ok' with no notice."""
    monkeypatch.setattr(
        update_check,
        "_transport",
        _serve(
            _index(
                [RUNNING, "2.5.11a1", "2.5.11rc2", "2.6.0.dev4"],
                _files(RUNNING, "2.5.11a1", "2.5.11rc2"),
            )
        ),
    )
    verdict = await update_check.refresh()
    assert verdict["state"] == update_check.STATE_OK
    assert verdict["kind"] is None
    assert verdict["available"] is None
    assert update_check.notice("s1", True) is None


@pytest.mark.asyncio
async def test_running_version_yanked_carries_the_reason(monkeypatch):
    """Every file of the running version withdrawn (PEP 592) is the finding an
    installed server can otherwise never learn. Reading 'any file yanked' instead
    of 'every file' would report a half-yanked release the same way; reading the
    newest version's yank status instead of the RUNNING one would report nothing
    at all here."""
    files = _files(RUNNING, yanked=False)
    for entry in files:
        entry["yanked"] = "signing key compromised"
    files += _files("2.5.11")
    monkeypatch.setattr(update_check, "_transport", _serve(_index([RUNNING, "2.5.11"], files)))
    verdict = await update_check.refresh()
    assert verdict["state"] == update_check.STATE_YANKED
    assert verdict["kind"] == update_check.KIND_YANKED
    assert verdict["reason"] == "signing key compromised"
    assert verdict["available"] == "2.5.11"


@pytest.mark.asyncio
async def test_yanked_as_true_has_no_reason_and_is_still_a_yank(monkeypatch):
    """`yanked: true` is the reasonless form. Treating the field as a string
    (reading `.strip()` off it) raises; treating a missing reason as 'not
    yanked' loses the finding entirely."""
    monkeypatch.setattr(
        update_check, "_transport", _serve(_index([RUNNING], _files(RUNNING, yanked=True)))
    )
    verdict = await update_check.refresh()
    assert verdict["state"] == update_check.STATE_YANKED
    assert verdict["reason"] is None


@pytest.mark.asyncio
async def test_one_yanked_file_is_not_a_yanked_release(monkeypatch):
    """The wheel is withdrawn, the sdist is not: the release is still installable
    as published, so calling it withdrawn would send an operator to replace
    something that is fine."""
    files = _files(RUNNING)
    files[0]["yanked"] = "bad wheel tag"
    monkeypatch.setattr(update_check, "_transport", _serve(_index([RUNNING], files)))
    verdict = await update_check.refresh()
    assert verdict["state"] == update_check.STATE_OK


@pytest.mark.asyncio
async def test_prerelease_running_is_told_about_its_own_final(monkeypatch):
    """A pre-release's own final IS a newer final, so the plain comparison
    already catches it; the separate kind exists so the message can say 'the
    final of what you are running' rather than implying a feature release."""
    monkeypatch.setattr(update_check, "__version__", "2.5.11a1")
    monkeypatch.setattr(
        update_check,
        "_transport",
        _serve(_index(["2.5.11a1", "2.5.11"], _files("2.5.11a1", "2.5.11"))),
    )
    verdict = await update_check.refresh()
    assert verdict["state"] == update_check.STATE_NEWER
    assert verdict["kind"] == update_check.KIND_PRERELEASE_FINAL
    assert verdict["available"] == "2.5.11"


@pytest.mark.asyncio
async def test_prerelease_running_with_a_later_line_names_the_later_line(monkeypatch):
    """When something newer than its own final exists, the later release is the
    honest recommendation and the kind falls back to the plain 'newer'."""
    monkeypatch.setattr(update_check, "__version__", "2.5.11a1")
    monkeypatch.setattr(
        update_check,
        "_transport",
        _serve(_index(["2.5.11a1", "2.5.11", "2.6.0"], _files("2.5.11a1", "2.5.11", "2.6.0"))),
    )
    verdict = await update_check.refresh()
    assert verdict["kind"] == update_check.KIND_NEWER
    assert verdict["available"] == "2.6.0"


@pytest.mark.asyncio
async def test_unlisted_running_version_is_never_a_yank(monkeypatch):
    """A development checkout is not on the index. 'No files, therefore all of
    its files are yanked' is vacuously true and would report every clone as a
    withdrawn release — and 'upgrade to the newest release' is wrong advice for
    a tree that is ahead of it."""
    monkeypatch.setattr(update_check, "__version__", "2.6.0.dev1+local")
    monkeypatch.setattr(
        update_check, "_transport", _serve(_index([RUNNING, "2.5.11"], _files(RUNNING, "2.5.11")))
    )
    verdict = await update_check.refresh()
    assert verdict["state"] == update_check.STATE_UNLISTED
    assert verdict["kind"] is None
    assert update_check.notice("s1", True) is None


@pytest.mark.asyncio
async def test_post_release_counts_as_final(monkeypatch):
    """PEP 440 puts a post-release above the release it post-dates, and it is not
    a pre-release. Excluding it with the alphas would hide a real upgrade."""
    monkeypatch.setattr(
        update_check,
        "_transport",
        _serve(_index([RUNNING, "2.5.10.post1"], _files(RUNNING, "2.5.10.post1"))),
    )
    verdict = await update_check.refresh()
    assert verdict["available"] == "2.5.10.post1"


# ---------------------------------------------------------------------------
# 2. Failure is silence, and it is bounded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_body_is_unknown_not_an_exception(monkeypatch):
    """A body that is not JSON must not escape: the caller is a background task,
    and an exception there is swallowed for the life of the session."""
    monkeypatch.setattr(update_check, "_transport", _serve(None, body=b"<html>not json</html>"))
    verdict = await update_check.refresh()
    assert verdict["state"] == update_check.STATE_UNKNOWN
    assert update_check.current()["state"] == update_check.STATE_UNKNOWN


@pytest.mark.asyncio
async def test_http_503_is_unknown(monkeypatch):
    """Without raise_for_status the 503's body is parsed as an index, and an
    error document with no 'versions' key reads as 'this version is unlisted' —
    a positive finding invented out of an outage."""
    monkeypatch.setattr(update_check, "_transport", _serve(_index([]), status=503))
    verdict = await update_check.refresh()
    assert verdict["state"] == update_check.STATE_UNKNOWN


@pytest.mark.asyncio
async def test_a_version_list_that_is_not_a_list_is_unknown_or_unlisted(monkeypatch):
    """A schema change must degrade, not raise."""
    monkeypatch.setattr(update_check, "_transport", _serve({"versions": "2.5.11", "files": {}}))
    verdict = await update_check.refresh()
    assert verdict["state"] in (update_check.STATE_UNKNOWN, update_check.STATE_UNLISTED)


@pytest.mark.asyncio
async def test_the_fetch_is_bounded_by_its_deadline(monkeypatch):
    """M3: with the deadline removed (or raised), this test hangs until the
    per-test timeout kills it — which is exactly what a hung index would do to a
    startup task holding a socket open for the life of the process.

    The sleeping transport is not interrupted by httpx's own timeout (a mock
    transport does not implement it), which is the point: the guarantee has to
    be the outer wall-clock bound, not one library's model of the phases it
    knows about.
    """
    monkeypatch.setattr(update_check, "TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(update_check, "_transport", _serve(_index([RUNNING]), delay=5.0))
    started = time.monotonic()
    verdict = await update_check.refresh()
    elapsed = time.monotonic() - started
    assert verdict["state"] == update_check.STATE_UNKNOWN
    assert elapsed < 1.0, f"the fetch ran {elapsed:.2f}s against a 0.05s budget"


# ---------------------------------------------------------------------------
# 3. The cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_fresh_cache_is_served_without_a_fetch(monkeypatch):
    """The second process start inside the window costs no request. Without the
    freshness check every restart — and a crash loop is many — hits the index."""
    calls: list[str] = []
    payload = _index([RUNNING, "2.5.11"], _files(RUNNING, "2.5.11"))
    monkeypatch.setattr(update_check, "_transport", _serve(payload, calls=calls))
    await update_check.run_startup_check()
    assert len(calls) == 1
    update_check._reset()
    monkeypatch.setattr(update_check, "__version__", RUNNING)
    monkeypatch.setattr(update_check, "_transport", _serve(payload, calls=calls))
    verdict = await update_check.run_startup_check()
    assert len(calls) == 1, "the second start re-fetched despite a fresh cache"
    assert verdict["available"] == "2.5.11"


@pytest.mark.asyncio
async def test_an_expired_cache_is_refetched(monkeypatch):
    """The window is what makes the answer eventually true; without expiry the
    first verdict is the only verdict this installation will ever have."""
    calls: list[str] = []
    monkeypatch.setattr(update_check, "_transport", _serve(_index([RUNNING], _files(RUNNING)), calls=calls))
    await update_check.run_startup_check()
    update_check._reset()
    monkeypatch.setattr(update_check, "__version__", RUNNING)
    monkeypatch.setattr(config, "UPDATE_CHECK_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(
        update_check,
        "_transport",
        _serve(_index([RUNNING, "2.5.11"], _files(RUNNING, "2.5.11")), calls=calls),
    )
    verdict = await update_check.run_startup_check()
    assert len(calls) == 2
    assert verdict["available"] == "2.5.11"


@pytest.mark.asyncio
async def test_a_cache_from_another_version_is_ignored(monkeypatch):
    """It answers 'is 2.5.9 current', and after an upgrade that is not the
    question. Reusing it reports the upgrade the operator has already applied."""
    calls: list[str] = []
    payload = _index(["2.5.9", RUNNING], _files("2.5.9", RUNNING))
    monkeypatch.setattr(update_check, "__version__", "2.5.9")
    monkeypatch.setattr(update_check, "_transport", _serve(payload, calls=calls))
    await update_check.run_startup_check()
    assert len(calls) == 1
    update_check._reset()
    monkeypatch.setattr(update_check, "__version__", RUNNING)
    monkeypatch.setattr(update_check, "_transport", _serve(payload, calls=calls))
    verdict = await update_check.run_startup_check()
    assert len(calls) == 2, "the cache written by 2.5.9 was reused by 2.5.10"
    assert verdict["state"] == update_check.STATE_OK


@pytest.mark.asyncio
async def test_a_corrupt_cache_is_a_miss_not_a_crash(monkeypatch):
    """A truncated write (a container killed mid-flush) must cost one request,
    not a startup."""
    calls: list[str] = []
    with open(update_check.cache_path(), "w", encoding="utf-8") as handle:
        handle.write('{"fetched_at": "2026-')
    monkeypatch.setattr(update_check, "_transport", _serve(_index([RUNNING], _files(RUNNING)), calls=calls))
    verdict = await update_check.run_startup_check()
    assert len(calls) == 1
    assert verdict["state"] == update_check.STATE_OK


@pytest.mark.asyncio
async def test_the_cache_lands_beside_the_database(monkeypatch):
    """The sidecar convention: the database's directory is the one directory a
    deployment has already granted this process."""
    monkeypatch.setattr(update_check, "_transport", _serve(_index([RUNNING], _files(RUNNING))))
    await update_check.run_startup_check()
    with open(update_check.cache_path(), encoding="utf-8") as handle:
        payload = json.load(handle)
    assert update_check.cache_path().endswith("update-check.json")
    assert payload["running"] == RUNNING and payload["verdict"]["state"] == update_check.STATE_OK


# ---------------------------------------------------------------------------
# 4. The switch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_means_no_fetch_no_cache_no_notice(monkeypatch):
    """CPERSONA_UPDATE_CHECK=false is total. A partial off — no notice but still
    a request — would be the opposite of what an air-gapped operator set it for."""
    calls: list[str] = []
    monkeypatch.setattr(config, "UPDATE_CHECK_ENABLED", False)
    monkeypatch.setattr(
        update_check,
        "_transport",
        _serve(_index([RUNNING, "2.5.11"], _files(RUNNING, "2.5.11")), calls=calls),
    )
    verdict = await update_check.run_startup_check()
    assert verdict["state"] == update_check.STATE_DISABLED
    assert calls == []
    assert update_check.notice("s1", True) is None
    assert update_check.health_issues() == []
    assert update_check.current()["enabled"] is False


@pytest.mark.asyncio
async def test_disabled_suppresses_an_already_known_verdict(monkeypatch):
    """Turning it off mid-process silences the surfaces, not just the fetch."""
    monkeypatch.setattr(
        update_check, "_transport", _serve(_index([RUNNING, "2.5.11"], _files(RUNNING, "2.5.11")))
    )
    await update_check.run_startup_check()
    monkeypatch.setattr(config, "UPDATE_CHECK_ENABLED", False)
    assert update_check.notice("s1", True) is None
    assert update_check.health_issues() == []


# ---------------------------------------------------------------------------
# 5. Telling the caller, once
# ---------------------------------------------------------------------------


async def _seed_newer(monkeypatch):
    monkeypatch.setattr(
        update_check, "_transport", _serve(_index([RUNNING, "2.5.11"], _files(RUNNING, "2.5.11")))
    )
    await update_check.run_startup_check()


@pytest.mark.asyncio
async def test_a_declared_session_is_told_once(monkeypatch):
    """M2: with the debounce removed the notice rides every recall response for
    the life of the process — a release that exists will still exist tomorrow,
    so the second telling is pure cost."""
    await _seed_newer(monkeypatch)
    first = update_check.notice("session-a", True)
    second = update_check.notice("session-a", True)
    assert first is not None and first["kind"] == update_check.KIND_NEWER
    assert second is None


@pytest.mark.asyncio
async def test_a_second_session_is_told_too(monkeypatch):
    """The suppression is keyed on the session, not on the process: a session
    that never received the notice must not be silenced by another one's."""
    await _seed_newer(monkeypatch)
    assert update_check.notice("session-a", True) is not None
    assert update_check.notice("session-b", True) is not None


@pytest.mark.asyncio
async def test_a_keyless_caller_is_told_once_per_process(monkeypatch):
    """The honest substitute when there is nobody to key on."""
    await _seed_newer(monkeypatch)
    assert update_check.notice("", False) is not None
    assert update_check.notice("", False) is None
    assert update_check.notice("cpersona:transport", False) is None


@pytest.mark.asyncio
async def test_the_told_set_is_bounded(monkeypatch):
    """A client that rotates keys must not grow this without limit; eviction
    only forgets that a session was told, so the worst case is one repeat."""
    await _seed_newer(monkeypatch)
    for i in range(update_check.NOTICE_SESSION_CAP + 10):
        update_check.notice(f"s{i}", True)
    assert len(update_check._told_sessions) <= update_check.NOTICE_SESSION_CAP


@pytest.mark.asyncio
async def test_a_changed_verdict_is_news_again(monkeypatch):
    """A refresh that finds something different re-arms the notice; a refresh
    that finds the same thing does not."""
    await _seed_newer(monkeypatch)
    assert update_check.notice("session-a", True) is not None
    monkeypatch.setattr(
        update_check, "_transport", _serve(_index([RUNNING, "2.5.11"], _files(RUNNING, "2.5.11")))
    )
    await update_check.refresh()
    assert update_check.notice("session-a", True) is None, "the same verdict re-notified"
    monkeypatch.setattr(
        update_check, "_transport", _serve(_index([RUNNING, "2.6.0"], _files(RUNNING, "2.6.0")))
    )
    await update_check.refresh()
    assert update_check.notice("session-a", True) is not None


@pytest.mark.asyncio
async def test_the_notice_carries_the_command_and_the_restart(monkeypatch):
    """A notice that names no action is a complaint. The restart is the half
    that gets forgotten: this process keeps serving the old code either way."""
    _install(monkeypatch, "pip")
    await _seed_newer(monkeypatch)
    payload = update_check.notice("session-a", True)
    assert payload["install"]["method"] == "pip"
    assert payload["install"]["command"].endswith("-m pip install --upgrade cpersona==2.5.11")
    assert payload["install"]["restart_required"] is True
    assert "restarted" in payload["message"]


@pytest.mark.asyncio
async def test_the_yank_reason_never_reaches_a_command(monkeypatch):
    """The reason is text written by whoever published the release. It is
    carried as data and truncated; the command is built from constants and a
    version string that had to match the safe pattern first."""
    _install(monkeypatch, "pip")
    files = _files(RUNNING)
    for entry in files:
        entry["yanked"] = "x; rm -rf / #" + "y" * 500
    monkeypatch.setattr(update_check, "_transport", _serve(_index([RUNNING], files)))
    await update_check.refresh()
    payload = update_check.notice("session-a", True)
    assert payload["kind"] == update_check.KIND_YANKED
    assert len(payload["reason"]) <= update_check.YANK_REASON_MAX_CHARS
    assert "rm -rf" not in payload["install"]["command"]


def test_a_version_outside_the_safe_pattern_builds_no_command(monkeypatch):
    """Never build a command from an unvalidated string: an 'available' value
    that is not shaped like a version yields no version in the command rather
    than one with something else in it."""
    _install(monkeypatch, "pip")
    install = update_check.detect_install("2.5.11; rm -rf /")
    assert "rm -rf" not in install["command"]
    assert install["command"].endswith("pip install --upgrade cpersona")


# ---------------------------------------------------------------------------
# 6. Install detection
# ---------------------------------------------------------------------------


def test_uvx_is_detected_before_site_packages(monkeypatch):
    """A uvx launch is ALSO inside a site-packages directory, so asking 'am I in
    site-packages' first classifies every uvx process as pip and hands its
    operator a `pip install --upgrade` that succeeds, changes a cached
    environment, and is discarded on the next launch."""
    monkeypatch.setattr(update_check.sys, "prefix", "/home/u/.cache/uv/archive-v0/deadbeef")
    monkeypatch.setattr(
        update_check,
        "__file__",
        "/home/u/.cache/uv/archive-v0/deadbeef/lib/python3.11/site-packages/cpersona/update_check.py",
    )
    install = update_check.detect_install("2.5.11")
    assert install["method"] == "uvx"
    assert install["command"] == "uvx cpersona@2.5.11"
    assert "--refresh" in install["note"] and "cpersona@latest" in install["note"]
    assert install["argv_steps"] == []


def test_a_site_packages_install_is_pip(monkeypatch):
    _install(monkeypatch, "pip")
    install = update_check.detect_install("2.5.11")
    assert install["method"] == "pip"
    assert install["argv_steps"] == [
        [sys.executable, "-m", "pip", "install", "--upgrade", "cpersona==2.5.11"]
    ]


def test_a_checkout_updates_the_working_tree_first(monkeypatch):
    """pip alone would not move a tree that is served from where it sits; and
    the command is spelled with the directory in both halves so it means the
    same thing wherever it is pasted."""
    _install(monkeypatch, "checkout")
    install = update_check.detect_install("2.5.11")
    assert install["method"] == "checkout"
    assert install["argv_steps"] == [
        ["git", "-C", "/srv/cpersona", "pull"],
        [sys.executable, "-m", "pip", "install", "/srv/cpersona"],
    ]
    assert install["command"].startswith("git -C /srv/cpersona pull && ")


def test_a_venv_without_pip_installs_through_uv(monkeypatch):
    """uv-created environments ship without pip. Spelling the command as
    `python -m pip` there fails with "No module named pip" — the command the
    tool shows would be one the operator cannot run. With pip absent and `uv`
    on PATH the install goes through `uv pip install --python <this python>`,
    which targets the same environment."""
    monkeypatch.setattr(update_check.sys, "prefix", "/venv")
    monkeypatch.setattr(
        update_check, "__file__", "/venv/lib/python3.11/site-packages/cpersona/update_check.py"
    )
    monkeypatch.setattr(update_check.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(update_check.shutil, "which", lambda name: "/usr/local/bin/uv")
    install = update_check.detect_install("2.5.11")
    assert install["method"] == "pip"
    assert install["argv_steps"] == [
        ["/usr/local/bin/uv", "pip", "install", "--python", sys.executable, "--upgrade", "cpersona==2.5.11"]
    ]
    assert install["command"].startswith("/usr/local/bin/uv pip install --python ")


def test_pip_is_preferred_when_it_is_importable(monkeypatch):
    """With pip importable the plain form is used even if uv is also installed:
    the environment's own installer is the one that knows its layout."""
    monkeypatch.setattr(update_check.sys, "prefix", "/venv")
    monkeypatch.setattr(
        update_check, "__file__", "/venv/lib/python3.11/site-packages/cpersona/update_check.py"
    )
    monkeypatch.setattr(update_check.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(update_check.shutil, "which", lambda name: "/usr/local/bin/uv")
    install = update_check.detect_install("2.5.11")
    assert install["argv_steps"][0][:4] == [sys.executable, "-m", "pip", "install"]


def test_an_unlocatable_install_is_unknown(monkeypatch):
    """An install we cannot name is one we cannot safely replace."""
    monkeypatch.setattr(update_check.sys, "prefix", "/venv")
    monkeypatch.setattr(update_check, "__file__", "")
    install = update_check.detect_install("2.5.11")
    assert install["method"] == "unknown"
    assert install["command"] == "" and install["argv_steps"] == []
    assert "reinstall" in install["note"]


# ---------------------------------------------------------------------------
# 7. apply
# ---------------------------------------------------------------------------


class _FakeProcess:
    def __init__(self, code, output):
        self.returncode = code
        self._output = output

    async def communicate(self):
        return self._output, b""


def _spy_exec(monkeypatch, code=0, output=b"Successfully installed cpersona-2.5.11\n"):
    seen: list[tuple[tuple, dict]] = []

    async def fake_exec(*argv, **kwargs):
        seen.append((argv, kwargs))
        return _FakeProcess(code, output)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return seen


@pytest.mark.asyncio
async def test_apply_runs_an_argv_list_never_a_shell(monkeypatch):
    """M4: building a shell string instead puts a version that arrived over the
    network in front of a shell. create_subprocess_exec takes the argv as
    separate positional arguments and there is no shell to quote for."""
    _install(monkeypatch, "pip")
    await _seed_newer(monkeypatch)
    seen = _spy_exec(monkeypatch)
    result = await update_check.apply()
    assert result["applied"] is True and result["exit_code"] == 0
    assert result["restart_required"] is True
    assert [argv for argv, _ in seen] == [
        (sys.executable, "-m", "pip", "install", "--upgrade", "cpersona==2.5.11")
    ]
    for _, kwargs in seen:
        assert set(kwargs) == {"stdout", "stderr"}, kwargs
    assert any("Successfully installed" in line for line in result["output_tail"])


@pytest.mark.asyncio
async def test_apply_on_a_checkout_pulls_then_installs(monkeypatch):
    _install(monkeypatch, "checkout")
    await _seed_newer(monkeypatch)
    seen = _spy_exec(monkeypatch)
    result = await update_check.apply()
    assert result["applied"] is True
    assert [argv for argv, _ in seen] == [
        ("git", "-C", "/srv/cpersona", "pull"),
        (sys.executable, "-m", "pip", "install", "/srv/cpersona"),
    ]


@pytest.mark.asyncio
async def test_apply_stops_at_the_first_failing_step(monkeypatch):
    """Installing from a tree whose pull failed would install the old code and
    report success."""
    _install(monkeypatch, "checkout")
    await _seed_newer(monkeypatch)
    seen = _spy_exec(monkeypatch, code=1, output=b"fatal: could not read from remote\n")
    result = await update_check.apply()
    assert result["applied"] is False and result["exit_code"] == 1
    assert len(seen) == 1, "the install ran after the pull failed"


@pytest.mark.asyncio
async def test_apply_never_spawns_under_uvx(monkeypatch):
    """The environment is a cache entry keyed by the launch arguments: a
    successful install here is discarded on the next launch, and the operator is
    left believing they upgraded."""
    _install(monkeypatch, "uvx")
    await _seed_newer(monkeypatch)

    async def never(*argv, **kwargs):
        raise AssertionError(f"apply spawned a process under uvx: {argv}")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", never)
    result = await update_check.apply()
    assert result["applied"] is False
    assert "uvx" in result["reason"]
    assert "uvx cpersona@latest" in result["install"]["note"]


@pytest.mark.asyncio
async def test_apply_refuses_when_there_is_nothing_newer(monkeypatch):
    """Installing 'the latest' from a verdict that named nothing is a different
    operation than the one the caller asked for."""
    _install(monkeypatch, "pip")
    monkeypatch.setattr(update_check, "_transport", _serve(_index([RUNNING], _files(RUNNING))))
    await update_check.run_startup_check()

    async def never(*argv, **kwargs):
        raise AssertionError(f"apply spawned a process with nothing to install: {argv}")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", never)
    result = await update_check.apply()
    assert result["applied"] is False and "nothing newer" in result["reason"]


@pytest.mark.asyncio
async def test_a_step_that_cannot_start_is_reported_not_raised(monkeypatch):
    """No git on PATH is an answer to 'what happened', not a stack trace in a
    field that promises a command's output."""
    _install(monkeypatch, "checkout")
    await _seed_newer(monkeypatch)

    async def missing(*argv, **kwargs):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", missing)
    result = await update_check.apply()
    assert result["applied"] is False and result["exit_code"] == 127
    assert any("could not run 'git'" in line for line in result["output_tail"])


# ---------------------------------------------------------------------------
# 8. The surfaces: recall, check_health, check_update
# ---------------------------------------------------------------------------


class _EmptyDB:
    """An empty corpus: every retriever finds nothing, every aggregate is empty.

    The two aggregates answer with a row rather than with no rows at all —
    ``SELECT COUNT(*)`` and ``SELECT MIN(...)`` always return one, and a double
    that returns none sends the scope-statistics reader into an IndexError
    instead of the branch under test.
    """

    async def execute_fetchall(self, sql, params=()):
        statement = " ".join(sql.split())
        if statement.startswith("SELECT COUNT("):
            return [(0,)]
        if statement.startswith("SELECT MIN("):
            return [(None, None)]
        return []

    async def execute(self, sql, params=()):
        return None

    async def commit(self):
        return None


def _patch_recall_seams(monkeypatch):
    @contextlib.asynccontextmanager
    async def fake_cm(**_kwargs):
        yield _EmptyDB()

    monkeypatch.setattr(M, "connection", fake_cm)
    monkeypatch.setattr(M, "transaction", fake_cm)


@pytest.mark.asyncio
async def test_recall_carries_no_update_key_when_there_is_nothing_to_say(monkeypatch):
    """The response-shape pin. A key present-and-empty on every recall changes
    the payload of the whole surface — and every recorded behaviour golden — to
    say nothing. `unknown` is the state every process is in until the startup
    task lands, which is also the state the golden replay runs in."""
    _patch_recall_seams(monkeypatch)
    out = await M.do_recall("agent.t", "anything", limit=3)
    assert "update" not in out
    out = await M.do_recall_with_context("agent.t", "anything", [], limit=3)
    assert "update" not in out


@pytest.mark.asyncio
async def test_recall_never_fetches_even_with_an_empty_cache(monkeypatch):
    """The hot path READS the verdict; the fetch belongs to the startup task
    alone. A recall that could fetch would pay the network's latency on the one
    call every connected agent makes."""
    _patch_recall_seams(monkeypatch)

    async def never():
        raise AssertionError("do_recall reached the package index")

    monkeypatch.setattr(update_check, "_fetch_index", never)
    out = await M.do_recall("agent.t", "anything", limit=3)
    assert "update" not in out
    assert await maintenance_handlers.do_check_update() is not None


@pytest.mark.asyncio
async def test_recall_attaches_the_notice_once_per_session(monkeypatch):
    """Two recalls in one session carry it once; a second session gets its own."""
    _patch_recall_seams(monkeypatch)
    _install(monkeypatch, "pip")
    await _seed_newer(monkeypatch)
    first = await M.do_recall("agent.t", "q", limit=3, session_key="sess-1")
    second = await M.do_recall("agent.t", "q", limit=3, session_key="sess-1")
    other = await M.do_recall("agent.t", "q", limit=3, session_key="sess-2")
    assert first["update"]["kind"] == update_check.KIND_NEWER
    assert first["update"]["available"] == "2.5.11"
    assert "update" not in second
    assert other["update"]["available"] == "2.5.11"


@pytest.mark.asyncio
async def test_recall_with_context_forwards_the_notice_without_spending_it_twice(monkeypatch):
    """It builds its own response dict, so the key has to be forwarded — and
    calling notice() again here would consume this session's one delivery on a
    response the caller already has."""
    _patch_recall_seams(monkeypatch)
    await _seed_newer(monkeypatch)
    out = await M.do_recall_with_context("agent.t", "q", [], limit=3, session_key="sess-3")
    assert out["update"]["kind"] == update_check.KIND_NEWER
    again = await M.do_recall_with_context("agent.t", "q", [], limit=3, session_key="sess-3")
    assert "update" not in again


@pytest.mark.asyncio
async def test_check_health_reports_an_available_update_as_info(monkeypatch):
    """An observation, not a database defect: `status` must not degrade because
    a release exists."""
    _install(monkeypatch, "pip")
    await _seed_newer(monkeypatch)
    result = await maintenance_handlers.do_check_health(agent_id="update-agent")
    found = [i for i in result["issues"] if i["type"] == "update_available"]
    assert len(found) == 1
    assert found[0]["severity"] == "info" and found[0]["available"] == "2.5.11"
    assert "repairable" not in found[0]
    assert result["severity_summary"]["info"] >= 1
    assert result["status"] == "healthy"


@pytest.mark.asyncio
async def test_check_health_reports_a_yanked_running_version_as_warn(monkeypatch):
    """A release its publisher retracted is a defect the operator is expected to
    act on, and `degraded` is the honest verdict while it is being served."""
    files = _files(RUNNING)
    for entry in files:
        entry["yanked"] = "broken migration"
    monkeypatch.setattr(update_check, "_transport", _serve(_index([RUNNING], files)))
    await update_check.run_startup_check()
    result = await maintenance_handlers.do_check_health(agent_id="update-agent")
    found = [i for i in result["issues"] if i["type"] == "version_yanked"]
    assert len(found) == 1 and found[0]["severity"] == "warn"
    assert found[0]["yank_reason"] == "broken migration"
    assert result["status"] == "degraded"


@pytest.mark.asyncio
async def test_check_health_says_nothing_while_the_state_is_unknown(monkeypatch):
    """Every process is in this state until the startup task lands, and most
    test runs never leave it — so a finding here would be one nobody asked for."""
    result = await maintenance_handlers.do_check_health(agent_id="update-agent")
    assert [i for i in result["issues"] if i.get("check") == "update_status"] == []


@pytest.mark.asyncio
async def test_the_findings_pull_stays_about_stored_state(monkeypatch):
    """The boundary, asserted rather than left to be discovered: the pull channel
    reports findings about what is STORED, and a version is not stored state. It
    is also what keeps the standard's one-detector equality test honest — that
    test compares check_health's issues with the pull's findings, and would go
    red the day this issue leaked into the pull."""
    await _seed_newer(monkeypatch)
    result = await maintenance_handlers.do_check_health(agent_id="")
    pulled = await maintenance_handlers.do_get_session_findings(per_kind_limit=1000)
    assert any(i["type"] == "update_available" for i in result["issues"])
    assert not any(f.get("type") == "update_available" for f in pulled["findings"])
    # And the name it carries is deliberately not a registry name, so a caller
    # cannot be misled into selecting it.
    rejected = await maintenance_handlers.do_check_health(checks=["update_status"])
    assert rejected["ok"] is False and "unknown check name" in rejected["error"]


@pytest.mark.asyncio
async def test_check_update_reads_the_verdict_without_refreshing(monkeypatch):
    """The default call is the cheap one: no fetch, no disk."""
    _install(monkeypatch, "pip")
    await _seed_newer(monkeypatch)

    async def never():
        raise AssertionError("check_update fetched without refresh=true")

    monkeypatch.setattr(update_check, "_fetch_index", never)
    out = await maintenance_handlers.do_check_update()
    assert out["state"] == update_check.STATE_NEWER and out["available"] == "2.5.11"
    assert out["refreshed"] is False and out["enabled"] is True
    assert out["install"]["method"] == "pip"
    assert "restarted" in out["message"]
    assert "argv_steps" not in out["install"]


@pytest.mark.asyncio
async def test_check_update_refresh_fetches_now(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        update_check,
        "_transport",
        _serve(_index([RUNNING, "2.5.11"], _files(RUNNING, "2.5.11")), calls=calls),
    )
    out = await maintenance_handlers.do_check_update(refresh=True)
    assert calls and out["state"] == update_check.STATE_NEWER
    assert out["refreshed"] is True and out["checked_at"]


@pytest.mark.asyncio
async def test_check_update_reports_disabled_without_pretending_to_know(monkeypatch):
    monkeypatch.setattr(config, "UPDATE_CHECK_ENABLED", False)
    out = await maintenance_handlers.do_check_update()
    assert out["state"] == update_check.STATE_DISABLED and out["enabled"] is False
    assert "message" not in out


@pytest.mark.asyncio
async def test_check_update_apply_is_refused_while_disabled(monkeypatch):
    """With the feature off there is no verdict to act on, so an apply would be
    installing on the strength of nothing."""
    monkeypatch.setattr(config, "UPDATE_CHECK_ENABLED", False)

    async def never(*argv, **kwargs):
        raise AssertionError("apply spawned a process while disabled")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", never)
    out = await maintenance_handlers.do_check_update(apply=True)
    assert out["apply_result"]["applied"] is False


@pytest.mark.asyncio
async def test_check_update_apply_reports_what_it_ran(monkeypatch):
    _install(monkeypatch, "pip")
    await _seed_newer(monkeypatch)
    seen = _spy_exec(monkeypatch)
    out = await maintenance_handlers.do_check_update(apply=True)
    assert out["apply_result"]["applied"] is True
    assert out["apply_result"]["restart_required"] is True
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_the_tool_is_registered_with_the_two_arguments(monkeypatch):
    """A handler nothing reaches is a feature nothing has."""
    from cpersona import server

    tool = {t.name: t for t in server.registry._tools}["check_update"]
    assert set(tool.inputSchema["properties"]) == {"refresh", "apply"}
    assert tool.annotations.readOnlyHint is False
    assert "CPERSONA_UPDATE_CHECK=false" in tool.description
    assert "RESTART" in tool.description.upper()
