"""The local service: one long-lived server per store, which stdio clients relay to.

An MCP client launches ``cpersona`` over stdio. Up to 2.6 that process opened the
database and served the client itself, so every open client was another process
writing the same file, each with its own process-wide state. From 2.7 the stdio
process is a relay: it finds the service for its store, starts one if there is
none, and forwards the client's messages to it over loopback HTTP. Clients of the
same store share one service, and so one writer.

Design: ``docs/LOCAL_SERVICE_DESIGN.md``.

What decides which service a client uses
----------------------------------------
The service is keyed by the store (the absolute database path) and the version of
this package. Two versions of the package keep separate services, as two versions
of the stdio server kept separate processes; the older one stops when it has been
idle for the idle period.

The settings a client launches with travel too. Every ``CPERSONA_*`` and
``EMBEDDING_*`` variable except the few that describe one connection
(:data:`CONNECTION_SETTINGS`) is part of the store's configuration, and a client
whose settings differ from those of the running service is refused with the names
of the settings that differ, never their values. Serving it from a service
configured differently would be the silent loss of a setting the client asked
for; starting a second service on the same store would bring back the second
writer the service exists to remove.

Files
-----
``~/.cpersona/services/`` (``CPERSONA_SERVICE_DIR`` moves it), owner-only:
``<key>.json`` (port, process id, version, the settings digest and the access
token), ``<key>.lock`` (held while a service is found or started) and
``<key>.log`` (the service's output).
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import os
import secrets
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass

import httpx

from cpersona import __version__, fileperms

logger = logging.getLogger("cpersona-relay")

DEFAULT_DIR = "~/.cpersona/services"

#: Settings that describe one connection rather than the store, so clients of one
#: service may differ in them. Everything else under the two prefixes is the
#: store's configuration and has to match.
CONNECTION_SETTINGS = frozenset(
    {
        "CPERSONA_TRANSPORT",
        "CPERSONA_REMOTE_URL",
        "CPERSONA_AUTH_TOKEN",
        "CPERSONA_PROXY_MAX_INFLIGHT",
        "CPERSONA_HTTP_HOST",
        "CPERSONA_HTTP_PORT",
        "CPERSONA_LOCAL_SERVICE",
        "CPERSONA_SERVICE_DIR",
        "CPERSONA_SERVICE_TOKEN",
        "CPERSONA_SERVICE_KEY",
        "CPERSONA_SERVICE_SETTINGS",
    }
)
SETTING_PREFIXES = ("CPERSONA_", "EMBEDDING_")

#: The header a relay names its connection with. The service uses it as the bucket
#: of a caller that declares no session key, which is what the stdio process was.
CONNECTION_HEADER = "X-CPersona-Connection"

#: How long a relay waits for a service it started to answer.
START_TIMEOUT_SECS = 60.0
#: How often a relay tells the service its client is still there.
HEARTBEAT_SECS = 300.0
#: A log larger than this is started afresh when a service starts.
LOG_MAX_BYTES = 5 * 1024 * 1024

# Windows: start the service detached, in a group of its own and outside the job
# the client runs in. Breaking away from the job alone was not enough: a client
# that ends its process tree reaches the service through the parent link, so the
# service is started by an intermediate process that exits at once.
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000


class ServiceError(RuntimeError):
    """The service could not be found or started; the message says why."""


class SettingsMismatch(ServiceError):
    """The running service for this store was started with other settings."""


def service_dir() -> str:
    return os.path.expanduser(os.environ.get("CPERSONA_SERVICE_DIR") or DEFAULT_DIR)


def enabled() -> bool:
    """False when ``CPERSONA_LOCAL_SERVICE=off`` keeps the stdio process serving
    its client itself (until 2.7.0b1, for reporting problems with the service)."""
    return os.environ.get("CPERSONA_LOCAL_SERVICE", "").strip().lower() not in ("off", "false", "0")


def _absolute(name: str, value: str) -> str:
    # A relative path means a different file from a different working directory,
    # so the same text in two clients' settings is not the same setting.
    if value and name.endswith(("_PATH", "_FILE", "_DIR")) and not value.startswith("file:") and value != ":memory:":
        return os.path.abspath(os.path.expanduser(value))
    return value


def store_path(environ: dict[str, str] | None = None) -> str:
    """The absolute path of the database a client with ``environ`` opens."""
    env = os.environ if environ is None else environ
    return os.path.abspath(env.get("CPERSONA_DB_PATH") or "data/cpersona.db")


def store_settings(environ: dict[str, str] | None = None) -> dict[str, str]:
    """The settings that belong to the store, by name, with paths made absolute."""
    env = os.environ if environ is None else environ
    settings = {
        name: _absolute(name, value)
        for name, value in env.items()
        if name.startswith(SETTING_PREFIXES) and name not in CONNECTION_SETTINGS
    }
    settings["CPERSONA_DB_PATH"] = store_path(env)
    return settings


def settings_digest(settings: dict[str, str], salt: str) -> dict[str, str]:
    """One keyed hash per setting, so a mismatch can be named without carrying a value.

    Keyed with the service's own random salt: some settings are secrets (an
    embedding API key), and an unkeyed hash of a short one could be reversed by
    anyone who reads the service's answer.
    """
    return {
        name: hmac.new(salt.encode(), value.encode("utf-8"), hashlib.sha256).hexdigest()[:16]
        for name, value in sorted(settings.items())
    }


def differing_settings(mine: dict[str, str], theirs: dict[str, str]) -> list[str]:
    return sorted(name for name in set(mine) | set(theirs) if mine.get(name) != theirs.get(name))


def service_key(store: str, version: str = __version__) -> str:
    return hashlib.sha256(f"{store}\0{version}".encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Service:
    key: str
    url: str
    token: str
    pid: int
    started: bool  # True when this call started it


def _info_path(key: str) -> str:
    return os.path.join(service_dir(), f"{key}.json")


def log_path(key: str) -> str:
    return os.path.join(service_dir(), f"{key}.log")


def _read_info(key: str) -> dict | None:
    try:
        with open(_info_path(key), encoding="utf-8") as fh:
            info = json.load(fh)
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) and isinstance(info.get("port"), int) else None


def _write_info(key: str, info: dict) -> None:
    path = _info_path(key)
    tmp = f"{path}.tmp.{os.getpid()}"
    with fileperms.open_private(tmp, "w", encoding="utf-8") as fh:
        json.dump(info, fh)
    os.replace(tmp, path)


def instance(port: int, token: str, connection: str | None = None, timeout: float = 2.0) -> dict | None:
    """What the service on ``port`` says it is, or None when nothing answers as one.

    A process id alone is not trusted: after a restart of the machine the same
    number can belong to anything, and a port can be taken by another program.
    """
    headers = {"Authorization": f"Bearer {token}"}
    if connection:
        headers[CONNECTION_HEADER] = connection
    try:
        response = httpx.get(f"http://127.0.0.1:{port}/api/v1/instance", headers=headers, timeout=timeout)
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) and body.get("service") == "cpersona" else None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def _locked(key: str):
    fileperms.makedirs_private(service_dir())
    fd = fileperms.open_lock(os.path.join(service_dir(), f"{key}.lock"))
    try:
        deadline = time.monotonic() + START_TIMEOUT_SECS * 2
        while True:
            try:
                fileperms.lock_file(fd)
                break
            except OSError:
                # Windows gives up after ten seconds; another relay may be waiting
                # on a first start that takes longer.
                if time.monotonic() > deadline:
                    raise
        try:
            yield
        finally:
            fileperms.unlock_file(fd)
    finally:
        os.close(fd)


def _spawn(env: dict[str, str], log: str) -> int:
    """Start ``python -m cpersona`` detached from the client; return its process id."""
    with contextlib.suppress(OSError):
        if os.path.getsize(log) > LOG_MAX_BYTES:
            os.remove(log)
    out = fileperms.open_private(log, "a", encoding="utf-8")
    try:
        if os.name != "nt":
            proc = subprocess.Popen(
                [sys.executable, "-m", "cpersona"],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            return proc.pid
        flags = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | _CREATE_BREAKAWAY_FROM_JOB
        )
        launcher = (
            "import subprocess,sys;"
            "p=subprocess.Popen([sys.executable,'-m','cpersona'],stdin=subprocess.DEVNULL,"
            f"stdout=open({log!r},'a',encoding='utf-8'),stderr=subprocess.STDOUT,creationflags={flags});"
            "print(p.pid)"
        )
        done = subprocess.run(
            [sys.executable, "-c", launcher],
            env=env,
            capture_output=True,
            text=True,
            creationflags=_CREATE_BREAKAWAY_FROM_JOB | subprocess.CREATE_NO_WINDOW,
            timeout=START_TIMEOUT_SECS,
            check=False,
        )
        try:
            return int(done.stdout.strip())
        except ValueError:
            raise ServiceError(f"the service launcher failed: {done.stderr.strip()[-300:]}") from None
    finally:
        out.close()


def _alive(pid: int) -> bool:
    """Whether the process started for the service is still there to wait for."""
    if os.name == "nt":
        import ctypes

        process_query_limited_information, still_active = 0x1000, 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _log_tail(key: str, lines: int = 8) -> str:
    try:
        with open(log_path(key), encoding="utf-8", errors="replace") as fh:
            return "".join(fh.readlines()[-lines:]).strip()
    except OSError:
        return ""


def ensure(environ: dict[str, str] | None = None, *, connection: str | None = None) -> Service:
    """Find the service for this client's store, starting one if none answers.

    Raises :class:`SettingsMismatch` when the running service has other settings,
    and :class:`ServiceError` when one cannot be started.
    """
    env = dict(os.environ if environ is None else environ)
    store = store_path(env)
    key = service_key(store)
    settings = store_settings(env)
    with _locked(key):
        info = _read_info(key)
        if info is not None:
            seen = instance(info["port"], info.get("token", ""), connection)
            if seen is not None and seen.get("key") == key:
                mine = settings_digest(settings, str(info.get("salt", "")))
                theirs = seen.get("settings") or {}
                different = differing_settings(mine, theirs)
                if different:
                    raise SettingsMismatch(
                        f"the CPersona service for {store} is running with other settings: "
                        f"{', '.join(different)} differ. Give this client the same settings as the "
                        "others that use this store, or close those clients and let the service stop "
                        "(it stops when it has been idle; `cpersona service stop` stops it now)."
                    )
                return Service(key, f"http://127.0.0.1:{info['port']}/mcp/", info["token"], int(info.get("pid", 0)), False)
        port, token, salt = _free_port(), secrets.token_urlsafe(32), secrets.token_hex(16)
        mine = settings_digest(settings, salt)
        child = dict(env)
        child.update(
            {
                "CPERSONA_TRANSPORT": "local-service",
                "CPERSONA_HTTP_HOST": "127.0.0.1",
                "CPERSONA_HTTP_PORT": str(port),
                "CPERSONA_SERVICE_TOKEN": token,
                "CPERSONA_SERVICE_KEY": key,
                "CPERSONA_SERVICE_SETTINGS": json.dumps(mine, sort_keys=True),
                "CPERSONA_DB_PATH": store,
            }
        )
        child.pop("CPERSONA_AUTH_TOKEN", None)
        pid = _spawn(child, log_path(key))
        deadline = time.monotonic() + START_TIMEOUT_SECS
        while True:
            seen = instance(port, token, connection, timeout=1.0)
            if seen is not None and seen.get("key") == key:
                break
            if not _alive(pid) or time.monotonic() > deadline:
                tail = _log_tail(key)
                raise ServiceError(
                    f"the CPersona service for {store} did not start (log: {log_path(key)})"
                    + (f"; last lines:\n{tail}" if tail else "")
                )
            time.sleep(0.05)
        _write_info(
            key,
            {
                "key": key,
                "store": store,
                "port": port,
                "pid": pid,
                "token": token,
                "salt": salt,
                "version": __version__,
                "started_at": time.time(),
            },
        )
        return Service(key, f"http://127.0.0.1:{port}/mcp/", token, pid, True)


def known_services() -> list[dict]:
    """The services recorded in the directory, without their tokens."""
    rows = []
    try:
        names = sorted(os.listdir(service_dir()))
    except OSError:
        return rows
    for name in names:
        if name.endswith(".json"):
            info = _read_info(name[: -len(".json")])
            if info is not None:
                running = instance(info["port"], info.get("token", ""), timeout=1.0) is not None
                rows.append({k: v for k, v in info.items() if k not in ("token", "salt")} | {"running": running})
    return rows


def stop(key: str | None = None) -> list[str]:
    """Ask the recorded services (or the one with ``key``) to stop; return the keys asked."""
    asked = []
    for row in known_services():
        if key and row["key"] != key:
            continue
        info = _read_info(row["key"]) or {}
        if row["running"]:
            with contextlib.suppress(httpx.HTTPError):
                httpx.post(
                    f"http://127.0.0.1:{info['port']}/api/v1/stop",
                    headers={"Authorization": f"Bearer {info.get('token', '')}"},
                    timeout=2.0,
                )
            asked.append(row["key"])
    return asked


def connection_id() -> str:
    return uuid.uuid4().hex


def run_relay() -> None:
    """The stdio entry: find or start the service, then relay this client to it."""
    import asyncio

    from cpersona import proxy_stdio

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s: %(message)s", stream=sys.stderr)
    connection = connection_id()
    try:
        service = ensure(connection=connection)
    except ServiceError as e:
        # The client is waiting for answers on stdout; give it the reason as the
        # error of every request rather than a process that exits without one.
        logger.error("%s", e)
        asyncio.run(proxy_stdio.refuse_all(str(e)))
        return
    target = proxy_stdio.Target(
        url=service.url,
        token=service.token,
        headers={CONNECTION_HEADER: connection},
        recover=_recoverer(connection),
    )
    asyncio.run(_relay(target, connection))


def _recoverer(connection: str):
    """A coroutine factory the relay calls once when the service stops answering."""
    import asyncio

    lock = asyncio.Lock()

    async def recover(target) -> bool:
        async with lock:
            try:
                service = await asyncio.to_thread(ensure, connection=connection)
            except ServiceError as e:
                logger.error("%s", e)
                return False
            target.url, target.token = service.url, service.token
            if service.started:
                logger.info("started the CPersona service again (pid %d)", service.pid)
            return True

    return recover


async def _relay(target, connection: str) -> None:
    import asyncio

    from cpersona import proxy_stdio

    async def heartbeat():
        # Connected clients count as use, so a client that is open but quiet does
        # not see its service stop under it (and lose its in-process state).
        while True:
            await asyncio.sleep(HEARTBEAT_SECS)
            port = int(target.url.rsplit(":", 1)[1].split("/", 1)[0])
            await asyncio.to_thread(instance, port, target.token, connection)

    beat = asyncio.create_task(heartbeat())
    try:
        await proxy_stdio.main(target)
    finally:
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat
