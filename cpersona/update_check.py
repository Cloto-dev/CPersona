"""Is the code answering this request the release its users are meant to run?

A memory server is installed once and then left alone — that is the point of
it — so the process that answers today's recall can be many releases behind the
one the operator believes they installed, and nothing in the running system
says so. Worse, a release can be *withdrawn* after it is installed (PEP 592
yank), and the running process is the last place that finds out. This module is
the answer to both questions, and nothing else: it reads the package index once
per process start, decides what the running version's situation is, and hands
that verdict to the surfaces the operator already reads (``recall`` and
``check_health``) plus one tool that answers on demand (``check_update``).

Three rules shape every line here, in this order:

1. **It never delays a request.** The fetch happens in a startup background
   task, bounded by :data:`TIMEOUT_SECONDS` end to end. Every other entry point
   — ``do_recall``, ``check_health``, ``check_update`` without ``refresh`` —
   READS the in-memory verdict and cannot reach the network. A version notice
   that costs a recall its latency would be a worse defect than the staleness
   it reports.
2. **It never fails loudly.** A network outage, a proxy, an air-gapped host, a
   500, a body that is not JSON, a schema that changed: all of them are the
   same answer, ``state="unknown"``, logged at debug. The server's job is
   memory, and an update check that can break a startup is a liability nobody
   asked for.
3. **It never updates anything by itself.** ``apply`` is an explicit argument
   on an explicit tool call, it runs an argv list (never a shell string), and a
   restart is always required afterwards — this process cannot become the new
   version, and pretending otherwise would be the one failure mode that
   silently serves half of each.

Why a dependency-free PEP 440 subset (:func:`_parse_version`) rather than
``packaging``: this package's dependency list is short on purpose and each
entry is argued for in ``pyproject.toml``. Adding a runtime dependency so the
server can tell you it is out of date inverts the trade. The subset is the part
of the grammar PyPI actually publishes for this project — epoch, release,
a/b/rc pre-releases, post, dev, local — and an unparseable string is simply not
a candidate, which is the safe direction: it can hide a newer release, never
invent one.

The comparison is against ``cpersona.__version__`` — the version of the code
that is running — not the installed distribution's metadata, for the reason
``maintenance_handlers._server_version`` states: on the clone path a checkout
serves every request while an older distribution sits in site-packages, and
asking the metadata answers for the wrong one.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import re
import shutil
import sys
from datetime import datetime, timezone

import httpx

from cpersona import __version__
from cpersona import config

logger = logging.getLogger(__name__)

# The JSON form of the simple index (PEP 691). The HTML form would need an HTML
# parser to answer the same questions, and the JSON form is the one that
# publishes `yanked` as a field rather than as a link attribute.
INDEX_URL = "https://pypi.org/simple/cpersona/"
INDEX_ACCEPT = "application/vnd.pypi.simple.v1+json"
PROJECT_NAME = "cpersona"

# End-to-end budget for the whole check: DNS, connect, transfer and parse. It is
# spent by a background task, so nothing waits on it — the bound exists so a
# hung endpoint cannot keep a task (and its socket) alive for the life of the
# process. Small, because there is nothing to salvage by waiting: a slow index
# is the same answer as an unreachable one.
TIMEOUT_SECONDS = 3.0

# A yank reason is untrusted text written by whoever published the release. It
# is truncated and it is NEVER interpolated into a command — the command is
# built from constants and a validated version string (see `detect_install`).
YANK_REASON_MAX_CHARS = 200

CACHE_FILENAME = "update-check.json"

# States. `unknown` is "we do not know" (no fetch has succeeded); `unlisted` is
# a positive finding — the running version is not on the index at all, which is
# what a development checkout looks like and is NOT a yank.
STATE_DISABLED = "disabled"
STATE_UNKNOWN = "unknown"
STATE_UNLISTED = "unlisted"
STATE_OK = "ok"
STATE_NEWER = "newer"
STATE_YANKED = "yanked"

# Notice kinds — the three things worth telling a calling agent about.
KIND_NEWER = "newer"
KIND_YANKED = "yanked"
KIND_PRERELEASE_FINAL = "prerelease_final"

# Same bound, and the same reason, as health.ADVISORY_SESSION_CAP: the key space
# is client-supplied, and eviction only forgets that a session was already told,
# so the worst case is telling one session twice.
NOTICE_SESSION_CAP = 256

# --- Module state (per process) ---------------------------------------------
#
# Concurrency: the mutators contain no `await` between read and write, so they
# run atomically between awaits under the asyncio single thread — the same
# argument health.py makes for its own module state, and the reason there is no
# Lock here.

_verdict: dict | None = None
_checked_at: str | None = None
_told_sessions: dict[str, None] = {}
_notice_emitted: bool = False

# Test seam: an httpx transport used instead of a real connection. Set by the
# suite (httpx.MockTransport) so every path below — including the client's own
# timeout plumbing and the JSON decode — runs for real against fixture bytes.
# Production leaves it None and never assigns it.
_transport: httpx.BaseTransport | None = None


# ---------------------------------------------------------------------------
# PEP 440 subset
# ---------------------------------------------------------------------------

_VERSION_RE = re.compile(
    r"""^\s*v?
    (?:(?P<epoch>\d+)!)?
    (?P<release>\d+(?:\.\d+)*)
    (?:[-_.]?(?P<pre_l>a|b|c|rc|alpha|beta|pre|preview)[-_.]?(?P<pre_n>\d+)?)?
    (?:-(?P<post_n1>\d+)|[-_.]?(?P<post_l>post|rev|r)[-_.]?(?P<post_n2>\d+)?)?
    (?:[-_.]?(?P<dev_l>dev)[-_.]?(?P<dev_n>\d+)?)?
    (?:\+(?P<local>[a-z0-9]+(?:[-_.][a-z0-9]+)*))?
    \s*$""",
    re.VERBOSE | re.IGNORECASE,
)

_PRE_LETTERS = {"a": "a", "alpha": "a", "b": "b", "beta": "b", "c": "rc", "rc": "rc", "pre": "rc", "preview": "rc"}
_PRE_RANK = {"a": 0, "b": 1, "rc": 2}


class ParsedVersion:
    """A comparable version, plus the two facts the callers ask it for.

    ``key`` is a plain tuple of tuples of ints, so ordering is ordinary tuple
    comparison with no sentinel objects to get wrong. The three sub-keys encode
    PEP 440's ordering rules numerically:

    - ``pre``: ``(-1, 0, 0)`` for a dev release of an otherwise final version
      (``1.0.dev1`` sorts BELOW ``1.0a1``), ``(0, rank, n)`` for a/b/rc, and
      ``(1, 0, 0)`` for "no pre-release", which is what makes ``1.0`` sort
      above every ``1.0rcN``.
    - ``post``: ``(0, 0)`` absent, ``(1, n)`` present — a post-release is
      NEWER than the release it post-dates, which is why post counts as final.
    - ``dev``: ``(1, 0)`` absent, ``(0, n)`` present — a dev release is older
      than the release it leads to.

    The local segment is dropped: ``1.0+local`` is the same upstream release as
    ``1.0``, and it is never a candidate to upgrade *to*.
    """

    __slots__ = ("key", "release", "is_prerelease", "text")

    def __init__(self, key: tuple, release: tuple[int, ...], is_prerelease: bool, text: str):
        self.key = key
        self.release = release
        self.is_prerelease = is_prerelease
        self.text = text


def _parse_version(text: str) -> ParsedVersion | None:
    """Parse a version string, or return None if it is outside the subset.

    None is not an error condition: an unparseable string is dropped from the
    candidate set, so the worst it can do is hide a release that exists. The
    opposite failure — inventing an ordering for a string nobody understands —
    is the one that would tell an operator to "upgrade" to something else.
    """
    if not isinstance(text, str):
        return None
    match = _VERSION_RE.match(text)
    if match is None:
        return None
    epoch = int(match.group("epoch") or 0)
    release = tuple(int(part) for part in match.group("release").split("."))
    # Trailing zeros carry no meaning in PEP 440 (1.0 == 1.0.0), and leaving
    # them in would make the two sort as different versions.
    trimmed = list(release)
    while len(trimmed) > 1 and trimmed[-1] == 0:
        trimmed.pop()
    release = tuple(trimmed)

    pre_letter = match.group("pre_l")
    pre = None
    if pre_letter:
        normalized = _PRE_LETTERS[pre_letter.lower()]
        pre = (_PRE_RANK[normalized], int(match.group("pre_n") or 0))

    post = None
    if match.group("post_n1") is not None:
        post = int(match.group("post_n1"))
    elif match.group("post_l") is not None:
        post = int(match.group("post_n2") or 0)

    # `1.0.dev` with no number is a legal dev release (== `1.0.dev0`), so the
    # segment's presence is read off the letter group, never off the number.
    dev = int(match.group("dev_n") or 0) if match.group("dev_l") else None

    if pre is None and post is None and dev is not None:
        pre_key = (-1, 0, 0)
    elif pre is None:
        pre_key = (1, 0, 0)
    else:
        pre_key = (0, pre[0], pre[1])
    post_key = (0, 0) if post is None else (1, post)
    dev_key = (1, 0) if dev is None else (0, dev)

    key = (epoch, release, pre_key, post_key, dev_key)
    return ParsedVersion(key, release, pre is not None or dev is not None, text)


def _final_of(parsed: ParsedVersion) -> tuple:
    """The sort key of the plain final release with the same release tuple."""
    return (parsed.key[0], parsed.release, (1, 0, 0), (0, 0), (1, 0))


# ---------------------------------------------------------------------------
# Index reading
# ---------------------------------------------------------------------------


def _version_of_filename(filename: str) -> str | None:
    """The version a distribution filename belongs to, or None.

    Wheels put the version in the second dash-separated field
    (``cpersona-2.5.10-py3-none-any.whl``); an sdist is ``name-version`` plus
    the archive suffix. Anything else — a signature, an unknown packaging
    format — is not a file whose yank status can be attributed to a version,
    so it is dropped rather than guessed at.
    """
    if not isinstance(filename, str):
        return None
    if filename.endswith(".whl"):
        parts = filename[: -len(".whl")].split("-")
        return parts[1] if len(parts) >= 3 else None
    for suffix in (".tar.gz", ".zip", ".tar.bz2"):
        if filename.endswith(suffix):
            stem = filename[: -len(suffix)]
            name, sep, version = stem.rpartition("-")
            return version if sep and name else None
    return None


def _is_yanked(value) -> bool:
    """PEP 592/691: ``yanked`` is ``false``, ``true``, or a reason string.

    An empty reason string is still a yank. Reading truthiness alone would make
    ``"yanked": ""`` — a withdrawal whose publisher gave no reason — read as a
    healthy file, which is the one direction this must not fail in.
    """
    return value is True or isinstance(value, str)


def _yank_reason(value) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()[:YANK_REASON_MAX_CHARS]
    return None


def _decide(payload: dict, running: str) -> dict:
    """Turn one index document into the verdict this module serves.

    The order of the branches is the order of urgency, and it is deliberate:
    an unlisted version is not judged at all (a checkout is not "up to date"
    and is certainly not yanked), a withdrawn running version outranks the
    existence of a newer one, and only then does a newer final become news.
    """
    running_parsed = _parse_version(running)
    if running_parsed is None:
        # The running version is ours and should always parse; if it does not,
        # every comparison below would be against a version nobody can order.
        return _verdict_dict(STATE_UNKNOWN, running, None, None, None)

    candidates: dict[tuple, ParsedVersion] = {}
    for raw in payload.get("versions") or []:
        parsed = _parse_version(raw)
        if parsed is not None:
            candidates.setdefault(parsed.key, parsed)

    files = [f for f in (payload.get("files") or []) if isinstance(f, dict)]
    running_files = []
    for entry in files:
        version = _version_of_filename(entry.get("filename", ""))
        parsed = _parse_version(version) if version else None
        if parsed is None:
            continue
        candidates.setdefault(parsed.key, parsed)
        if parsed.key == running_parsed.key:
            running_files.append(entry)

    if running_parsed.key not in candidates:
        # A development checkout, a private build, a version pulled entirely.
        # No notice: nothing here can tell the operator what they should be
        # running instead, and "upgrade to the newest release" is wrong advice
        # for someone running a tree that is ahead of it.
        return _verdict_dict(STATE_UNLISTED, running, None, None, None)

    newer = None
    for parsed in candidates.values():
        if parsed.is_prerelease or parsed.key <= running_parsed.key:
            continue
        if newer is None or parsed.key > newer.key:
            newer = parsed
    available = newer.text if newer is not None else None

    if running_files and all(_is_yanked(entry.get("yanked")) for entry in running_files):
        # EVERY file, not any: a release whose wheel was yanked and whose sdist
        # was not is still installable as published, and calling that "withdrawn"
        # would send an operator to replace something that is fine.
        reason = None
        for entry in running_files:
            reason = reason or _yank_reason(entry.get("yanked"))
        return _verdict_dict(STATE_YANKED, running, KIND_YANKED, available, reason)

    if newer is not None:
        # A pre-release's own final is a newer final, so this branch already
        # covers it; naming it separately is for the message, which can then
        # say "the final of the version you are running" instead of implying a
        # feature release. Only when that final is the NEWEST thing available —
        # if a later line exists, the later line is the honest recommendation.
        kind = KIND_NEWER
        if running_parsed.is_prerelease and newer.key == _final_of(running_parsed):
            kind = KIND_PRERELEASE_FINAL
        return _verdict_dict(STATE_NEWER, running, kind, available, None)

    return _verdict_dict(STATE_OK, running, None, None, None)


def _verdict_dict(state: str, running: str, kind: str | None, available: str | None, reason: str | None) -> dict:
    return {
        "state": state,
        "running": running,
        "kind": kind,
        "available": available,
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# Cache sidecar
# ---------------------------------------------------------------------------


def cache_path() -> str:
    """The verdict cache, beside the database.

    Same placement rule as the alias ledger (``config.alias_ledger_path``): the
    server writes this file, and the database's directory is the one directory
    a deployment has already granted it. Read at call time rather than frozen
    at import so a test — and an operator who moves the database — gets the
    path that is actually in force.
    """
    return os.path.join(os.path.dirname(config.DB_PATH) or ".", CACHE_FILENAME)


def _read_cache(running: str) -> dict | None:
    """The cached verdict if it is fresh AND about this version, else None.

    A cache written by a different running version is ignored rather than
    migrated: its verdict answers "is 2.5.9 current", and after an upgrade that
    question is not the one being asked. Any read failure — missing, truncated,
    not JSON, a dict shape from a future release — is a miss, silently: a cache
    is an optimisation, and an optimisation that can break a startup is not one.
    """
    try:
        with open(cache_path(), encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            return None
        if payload.get("running") != running:
            return None
        verdict = payload.get("verdict")
        fetched_at = payload.get("fetched_at")
        if not isinstance(verdict, dict) or not isinstance(fetched_at, str):
            return None
        age = (_now() - datetime.fromisoformat(fetched_at)).total_seconds()
        if age < 0 or age > config.UPDATE_CHECK_INTERVAL_SECONDS:
            return None
        return {"verdict": verdict, "fetched_at": fetched_at}
    except Exception as exc:  # noqa: BLE001 — every failure is the same miss
        logger.debug("update check: cache unreadable (%s)", exc)
        return None


def _write_cache(verdict: dict, fetched_at: str) -> None:
    """Persist the verdict. A failure here is logged and otherwise ignored:
    the in-memory answer is already correct, and the only cost is one extra
    request on the next start."""
    try:
        path = cache_path()
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"fetched_at": fetched_at, "running": verdict["running"], "verdict": verdict}, handle)
    except Exception as exc:  # noqa: BLE001
        logger.debug("update check: cache not written (%s)", exc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


async def _fetch_index() -> dict | None:
    """The index document, or None if anything at all went wrong.

    The deadline is applied twice on purpose. httpx's own timeout covers the
    phases it knows about (connect, read, write, pool); ``asyncio.timeout``
    covers the whole call, including the parse and any transport that does not
    honour the first one. The promise this function makes to its caller is a
    wall-clock bound, and a bound that holds only for the failure modes one
    library models is not that promise.
    """
    try:
        async with asyncio.timeout(TIMEOUT_SECONDS):
            async with httpx.AsyncClient(
                timeout=TIMEOUT_SECONDS, transport=_transport, follow_redirects=True
            ) as client:
                response = await client.get(INDEX_URL, headers={"Accept": INDEX_ACCEPT})
                response.raise_for_status()
                payload = response.json()
        return payload if isinstance(payload, dict) else None
    except Exception as exc:  # noqa: BLE001 — silence is the contract (rule 2)
        logger.debug("update check: index unavailable (%s)", exc)
        return None


def _store(verdict: dict, fetched_at: str | None) -> dict:
    global _verdict, _checked_at, _notice_emitted
    changed = _verdict is None or _verdict.get("kind") != verdict.get("kind") or _verdict.get(
        "available"
    ) != verdict.get("available")
    _verdict = verdict
    _checked_at = fetched_at
    if changed:
        # A different verdict is news again, even to a session that was told
        # the last one. Re-arming here rather than on every store keeps a
        # repeated `refresh` of the same answer from re-notifying every session.
        _notice_emitted = False
        _told_sessions.clear()
    return verdict


async def refresh() -> dict:
    """Fetch now, update the cache and the in-memory verdict, return it.

    The one entry point that reaches the network on demand — ``check_update``'s
    ``refresh=true``. Everything else reads :func:`current`.
    """
    if not config.UPDATE_CHECK_ENABLED:
        return _verdict_dict(STATE_DISABLED, __version__, None, None, None)
    payload = await _fetch_index()
    if payload is None:
        return _store(_verdict_dict(STATE_UNKNOWN, __version__, None, None, None), _checked_at)
    fetched_at = _now().isoformat()
    verdict = _decide(payload, __version__)
    _write_cache(verdict, fetched_at)
    return _store(verdict, fetched_at)


async def run_startup_check() -> dict:
    """The startup background task's whole body: cache, else one fetch.

    Scheduled — never awaited — by ``server.main``, for the reason the
    calibration guard is (bug-258): nothing the serving path needs comes from
    here, so holding the transport closed for a network round-trip would trade
    a real outage for a cosmetic notice.
    """
    if not config.UPDATE_CHECK_ENABLED:
        logger.debug("update check: disabled by CPERSONA_UPDATE_CHECK")
        return _verdict_dict(STATE_DISABLED, __version__, None, None, None)
    cached = _read_cache(__version__)
    if cached is not None:
        return _store(cached["verdict"], cached["fetched_at"])
    return await refresh()


# ---------------------------------------------------------------------------
# Readers (no I/O of any kind)
# ---------------------------------------------------------------------------


def current() -> dict:
    """The verdict as it stands, with no fetch and no side effect.

    ``state`` is ``disabled`` when the feature is off and ``unknown`` when no
    check has completed — two different facts that a single "no news" answer
    would flatten into one, leaving an operator unable to tell a silent server
    from a switched-off one.
    """
    if not config.UPDATE_CHECK_ENABLED:
        return {**_verdict_dict(STATE_DISABLED, __version__, None, None, None), "checked_at": None, "enabled": False}
    if _verdict is None:
        return {**_verdict_dict(STATE_UNKNOWN, __version__, None, None, None), "checked_at": None, "enabled": True}
    return {**_verdict, "checked_at": _checked_at, "enabled": True}


def notice(session_key: str = "", declared: bool = False) -> dict | None:
    """The ``update`` payload for a recall response, or None.

    None whenever there is nothing to say — the key is absent from the response
    rather than present and empty, because every recall pays for every key it
    carries and a `false` on the other 99% of calls is a response-shape change
    that says nothing.

    Once, then silent. The degraded-recall advisory downgrades to a short
    reminder because an outage is happening NOW and the reader may need telling
    again; a release that exists will still exist tomorrow, and repeating it on
    every recall would be the crying-wolf cost with none of the urgency. With a
    declared ``session_key`` the "once" is per session (every session hears it
    once); without one it is per process, which is the honest substitute when
    there is nobody to key on — the same split ``health.maybe_advisory`` makes,
    for the same reason.
    """
    global _notice_emitted
    if not config.UPDATE_CHECK_ENABLED:
        return None
    verdict = _verdict
    if not verdict or not verdict.get("kind"):
        return None
    if declared:
        if session_key in _told_sessions:
            return None
        _remember_told(session_key)
    else:
        if _notice_emitted:
            return None
        _notice_emitted = True
    install = detect_install(verdict.get("available"))
    payload = {
        "kind": verdict["kind"],
        "running": verdict["running"],
        "available": verdict.get("available"),
        "install": {
            "method": install["method"],
            "command": install["command"],
            "restart_required": True,
        },
        "message": describe(verdict, install),
    }
    if verdict["kind"] == KIND_YANKED:
        payload["reason"] = verdict.get("reason")
    return payload


def _remember_told(session_key: str) -> None:
    _told_sessions[session_key] = None
    while len(_told_sessions) > NOTICE_SESSION_CAP:
        _told_sessions.pop(next(iter(_told_sessions)))


def health_issues() -> list[dict]:
    """``check_health`` issues for the verdict, in that surface's issue shape.

    Read-only and fetch-free by construction: it reads the same module state
    every other reader does. The severities differ because the two findings are
    different kinds of fact — a newer release is an observation (``info``, and
    info never moves ``status``), while a running version whose files were all
    withdrawn is a defect the operator is expected to act on (``warn``).

    Neither carries ``repairable``: that key belongs to the fix contract of a
    registry check, and no ``check_health(fix=true)`` run can install software.
    The repair is named in the hint instead.
    """
    if not config.UPDATE_CHECK_ENABLED:
        return []
    verdict = _verdict
    if not verdict or not verdict.get("kind"):
        return []
    install = detect_install(verdict.get("available"))
    common = {
        # NOT a registry check name, and the only issue on this surface that no
        # probe in cpersona.checks produced: it describes the process, not the
        # stored data. So `check_health(checks=["update_status"])` is rejected
        # (the registry has no such name) and `get_session_findings` does not
        # carry it — that channel reports findings about stored state, and a
        # version is not stored state.
        "check": "update_status",
        "running": verdict["running"],
        "available": verdict.get("available"),
        "install_method": install["method"],
        "hint": f"{install['command'] or install['note']} — then restart the server",
    }
    if verdict["kind"] == KIND_YANKED:
        return [
            {
                **common,
                "type": "version_yanked",
                "severity": "warn",
                "yank_reason": verdict.get("reason"),
                "detail": describe(verdict, install),
            }
        ]
    return [
        {
            **common,
            "type": "update_available",
            "severity": "info",
            "detail": describe(verdict, install),
        }
    ]


def describe(verdict: dict, install: dict) -> str:
    """One sentence a calling agent can pass on to a person, plus the command."""
    running = verdict.get("running")
    available = verdict.get("available")
    kind = verdict.get("kind")
    if kind == KIND_YANKED:
        reason = verdict.get("reason")
        head = (
            f"The running release of {PROJECT_NAME} ({running}) has been YANKED on PyPI"
            + (f": {reason}." if reason else " (no reason given).")
        )
        tail = (
            f" A yanked release is one its publisher withdrew; {available} is the newest available release."
            if available
            else " No newer release is available yet — check the project's releases before reinstalling."
        )
    elif kind == KIND_PRERELEASE_FINAL:
        head = f"{PROJECT_NAME} {running} is a pre-release and its final, {available}, is published."
        tail = ""
    else:
        head = f"A newer {PROJECT_NAME} release is available: {available} (running {running})."
        tail = ""
    action = install["command"] or install["note"]
    return f"{head}{tail} Updating is never automatic: {action}. The server must be restarted afterwards."


# ---------------------------------------------------------------------------
# Install-method detection and the opt-in apply
# ---------------------------------------------------------------------------

# What a version string may contain before it is allowed into an argv element.
# Not a validator for PEP 440 — the parse above is that — but the answer to a
# different question: could this string, which arrived over the network, do
# anything other than name a version? Everything is rejected that is not in the
# grammar of a version, so no quoting rule anywhere downstream has to hold.
_SAFE_VERSION = re.compile(r"^[0-9A-Za-z.+!-]+$")

METHOD_UVX = "uvx"
METHOD_PIP = "pip"
METHOD_CHECKOUT = "checkout"
METHOD_UNKNOWN = "unknown"

# uv unpacks each cached environment under `<cache>/archive-v0/<hash>`, so a
# process whose prefix is inside one was launched by uvx. Matched on the path
# segment rather than on the cache root, which moves with UV_CACHE_DIR.
_UV_MARKERS = (os.path.join("uv", "archive-v0"), "uv/archive-v0", "uv\\archive-v0")

_SITE_MARKERS = ("site-packages", "dist-packages")

_UVX_NOTE = (
    "relaunch with `uvx {spec}` — uv reuses the environment it already cached for the "
    "current arguments, so the launch arguments in your MCP client config are what has "
    "to change (`uvx {project}@latest`, or `uvx --refresh {project}`, picks up a new "
    "release; restarting with the same pinned arguments does not)"
)


def _installer_prefix() -> list[str]:
    """The argv that installs into THIS interpreter's environment.

    ``python -m pip`` when pip is importable here. Environments created by uv
    (``uv venv``, ``uv sync``) ship without pip, and in those ``python -m pip``
    fails with "No module named pip" — so when pip is absent and ``uv`` is on
    PATH, the install goes through ``uv pip install --python <this python>``,
    which targets the same environment. If neither is available the pip form is
    returned anyway: it fails with a message naming the missing tool, which is
    the honest answer, rather than a silent no-op.
    """
    if importlib.util.find_spec("pip") is not None:
        return [sys.executable, "-m", "pip", "install"]
    uv = shutil.which("uv")
    if uv:
        return [uv, "pip", "install", "--python", sys.executable]
    return [sys.executable, "-m", "pip", "install"]


def _package_dir() -> str | None:
    """The directory this package was imported from, or None if there is none."""
    path = globals().get("__file__")
    if not isinstance(path, str) or not path:
        return None
    return os.path.dirname(os.path.abspath(path))


def detect_install(available: str | None = None) -> dict:
    """How this process was installed, and the command that would update it.

    Returns ``{method, command, argv_steps, note, restart_required}``.
    ``argv_steps`` is empty for every method :func:`apply` refuses to run, so
    "is there something to execute" is one question with one answer rather than
    a method check repeated at each call site.

    The order is what makes it correct. A uvx launch is ALSO inside a
    site-packages directory (uv builds a real environment), so asking "am I in
    site-packages" first would classify every uvx process as pip and hand its
    operator a `pip install --upgrade` that succeeds, changes a cached
    environment, and is discarded on the next launch.
    """
    package_dir = _package_dir()
    version_ok = isinstance(available, str) and bool(_SAFE_VERSION.match(available))
    spec = f"{PROJECT_NAME}=={available}" if version_ok else PROJECT_NAME
    uvx_spec = f"{PROJECT_NAME}@{available}" if version_ok else f"{PROJECT_NAME}@latest"

    if any(marker in sys.prefix for marker in _UV_MARKERS):
        return {
            "method": METHOD_UVX,
            "command": f"uvx {uvx_spec}",
            "argv_steps": [],
            "note": _UVX_NOTE.format(spec=uvx_spec, project=PROJECT_NAME),
            "restart_required": True,
        }

    if package_dir is None:
        return {
            "method": METHOD_UNKNOWN,
            "command": "",
            "argv_steps": [],
            "note": (
                "this process's install location could not be determined, so no update "
                f"command is offered; reinstall {PROJECT_NAME} the way it was installed"
            ),
            "restart_required": True,
        }

    if not any(marker in package_dir for marker in _SITE_MARKERS):
        # An editable install or a clone served through the root shim: the
        # source of truth is the working tree, so pip alone would not move it.
        repo_dir = os.path.dirname(package_dir)
        steps = [
            ["git", "-C", repo_dir, "pull"],
            [*_installer_prefix(), repo_dir],
        ]
        return {
            "method": METHOD_CHECKOUT,
            # Spelled with the directory in both halves rather than `pip install .`
            # so the command means the same thing wherever it is pasted — and so
            # the text shown is exactly the argv executed below, with nothing
            # carried by an unstated working directory.
            "command": " && ".join(" ".join(step) for step in steps),
            "argv_steps": steps,
            "note": "",
            "restart_required": True,
        }

    steps = [[*_installer_prefix(), "--upgrade", spec]]
    return {
        "method": METHOD_PIP,
        "command": " ".join(steps[0]),
        "argv_steps": steps,
        "note": "",
        "restart_required": True,
    }


APPLY_OUTPUT_TAIL_LINES = 40


async def apply() -> dict:
    """Run the detected update command. Never called except by an explicit
    ``check_update(apply=true)``.

    Refuses rather than improvises in three cases, each of which would
    otherwise produce a command that appears to work:

    - nothing newer is known — there is no version to install, and installing
      "the latest" from a stale verdict is a different operation than the one
      the caller asked for;
    - ``uvx`` — the environment this process runs in is a cache entry keyed by
      the launch arguments, so a successful install here is discarded on the
      next launch and the operator is left believing they upgraded;
    - ``unknown`` — an install we cannot name is one we cannot safely replace.

    Executed as an argv list through ``create_subprocess_exec``: no shell, so
    no metacharacter in any string (the version came off the network) can
    become a second command. Output is merged and tailed rather than returned
    whole — a pip resolution log is longer than the answer to "did it work".

    No deadline, unlike the index fetch, and for the opposite reason: a
    dependency resolution legitimately takes minutes, so a bound tight enough to
    protect a caller would kill honest installs, and killing an install partway
    through is worse than a slow answer. This is the one call on this surface
    that can take a long time, which is why it is opt-in.
    """
    verdict = current()
    install = detect_install(verdict.get("available"))
    if not verdict.get("available"):
        return {
            "applied": False,
            "reason": f"nothing newer to install (state={verdict['state']})",
            "install": _public_install(install),
        }
    if not install["argv_steps"]:
        return {
            "applied": False,
            "reason": f"apply is not available for a {install['method']} install",
            "install": _public_install(install),
        }

    output: list[str] = []
    exit_code = 0
    for step in install["argv_steps"]:
        exit_code, text = await _run_step(step)
        output.append(f"$ {' '.join(step)}")
        output.extend(text.splitlines())
        if exit_code != 0:
            break
    return {
        "applied": exit_code == 0,
        "exit_code": exit_code,
        "output_tail": output[-APPLY_OUTPUT_TAIL_LINES:],
        "restart_required": True,
        "install": _public_install(install),
    }


async def _run_step(argv: list[str]) -> tuple[int, str]:
    """One argv step; returns (exit code, merged output).

    A step that cannot start at all (no git on PATH) returns a non-zero code
    and the reason as output, rather than raising: the caller asked what
    happened, and an exception here would answer with a stack trace in a field
    that promises a command's output.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except Exception as exc:  # noqa: BLE001
        return 127, f"could not run {argv[0]!r}: {exc}"
    stdout, _ = await process.communicate()
    return process.returncode or 0, stdout.decode("utf-8", "replace")


def _public_install(install: dict) -> dict:
    """The install description as tools return it — argv_steps stays internal."""
    return {
        "method": install["method"],
        "command": install["command"],
        "note": install["note"],
        "restart_required": True,
    }


def _reset() -> None:
    """Test-only: restore module state to its initial values."""
    global _verdict, _checked_at, _notice_emitted, _transport
    _verdict = None
    _checked_at = None
    _notice_emitted = False
    _told_sessions.clear()
    _transport = None
