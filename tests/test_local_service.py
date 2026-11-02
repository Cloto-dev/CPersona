"""The local service (docs/LOCAL_SERVICE_DESIGN.md): what decides which service a
client uses, the service's door, the connection bucket, the idle stop, the relay's
recovery and the command's choice of what to run.

One test starts real services in subprocesses (``test_clients_of_one_store_share
one_service``); the rest drive the pieces directly.
"""

import asyncio
import contextlib
import json
import os
import sys

import httpx
import pytest

from cpersona import config, entry, health, proxy_stdio, server, service, session

# --- which service a client uses ---------------------------------------------------


def test_store_settings_leave_out_connection_settings_and_other_variables(tmp_path):
    env = {
        "CPERSONA_DB_PATH": str(tmp_path / "a.db"),
        "CPERSONA_MAX_MEMORIES": "5",
        "EMBEDDING_MODE": "http",
        "CPERSONA_AUTH_TOKEN": "t",
        "CPERSONA_TRANSPORT": "stdio",
        "CPERSONA_PROXY_MAX_INFLIGHT": "4",
        "HOME": "/home/x",
        "PATH": "/bin",
    }
    settings = service.store_settings(env)
    assert set(settings) == {"CPERSONA_DB_PATH", "CPERSONA_MAX_MEMORIES", "EMBEDDING_MODE"}


def test_relative_paths_are_compared_as_the_files_they_name(tmp_path, monkeypatch):
    # Same text, different working directory: a different file, so a different setting.
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    env = {"CPERSONA_DB_PATH": "x.db", "CPERSONA_ACL_FILE": "acl.json"}
    monkeypatch.chdir(tmp_path / "one")
    first = service.store_settings(env)
    monkeypatch.chdir(tmp_path / "two")
    second = service.store_settings(env)
    assert first["CPERSONA_DB_PATH"] == str(tmp_path / "one" / "x.db")
    assert service.differing_settings(first, second) == ["CPERSONA_ACL_FILE", "CPERSONA_DB_PATH"]


def test_the_default_store_is_the_relative_default_made_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert service.store_path({}) == str(tmp_path / "data" / "cpersona.db")


def test_a_mismatch_names_settings_and_never_carries_a_value():
    salt = "s"
    a = service.settings_digest({"EMBEDDING_API_KEY": "secret-one", "CPERSONA_X": "1"}, salt)
    b = service.settings_digest({"EMBEDDING_API_KEY": "secret-two", "CPERSONA_X": "1"}, salt)
    assert service.differing_settings(a, b) == ["EMBEDDING_API_KEY"]
    assert "secret" not in json.dumps(a)
    # A setting one side has and the other lacks is a difference too.
    assert service.differing_settings(a, {"CPERSONA_X": a["CPERSONA_X"]}) == ["EMBEDDING_API_KEY"]


def test_the_digest_is_keyed_by_the_service_salt():
    settings = {"EMBEDDING_API_KEY": "k"}
    assert service.settings_digest(settings, "a") != service.settings_digest(settings, "b")
    assert service.settings_digest(settings, "a") == service.settings_digest(settings, "a")


def test_versions_and_stores_keep_separate_services():
    assert service.service_key("/a.db", "2.7.0") != service.service_key("/a.db", "2.7.1")
    assert service.service_key("/a.db", "2.7.0") != service.service_key("/b.db", "2.7.0")
    assert service.service_key("/a.db", "2.7.0") == service.service_key("/a.db", "2.7.0")


@pytest.mark.parametrize(("value", "on"), [("", True), ("on", True), ("off", False), ("OFF", False), ("0", False), ("false", False)])
def test_the_in_process_escape(monkeypatch, value, on):
    monkeypatch.setenv("CPERSONA_LOCAL_SERVICE", value)
    assert service.enabled() is on


# --- the connection bucket ----------------------------------------------------------


def test_a_keyless_caller_lands_in_its_connection_bucket():
    token = session.CONNECTION.set("abc123")
    try:
        assert session.resolve_session_key("") == ("connection:abc123", False)
        assert session.resolve_session_key(session.TRANSPORT_KEY) == ("connection:abc123", False)
        # A declared key still wins.
        assert session.resolve_session_key("mine") == ("mine", True)
    finally:
        session.CONNECTION.reset(token)
    assert session.resolve_session_key("") == (session.TRANSPORT_KEY, False)


def test_a_keyless_pause_on_one_connection_leaves_another_writing():
    def pause_on(name):
        token = session.CONNECTION.set(name)
        try:
            key, declared = session.resolve_session_key("")
            session.pause_for(key, declared, 60)
        finally:
            session.CONNECTION.reset(token)

    def paused_on(name):
        token = session.CONNECTION.set(name)
        try:
            key, declared = session.resolve_session_key("")
            return session.pause_status_for(key, declared)["paused"]
        finally:
            session.CONNECTION.reset(token)

    pause_on("aaaa1111")
    try:
        assert paused_on("aaaa1111") is True
        assert paused_on("bbbb2222") is False
    finally:
        token = session.CONNECTION.set("aaaa1111")
        session.resume_for(*session.resolve_session_key(""))
        session.CONNECTION.reset(token)


def test_the_full_advisory_reaches_each_connection_once(monkeypatch):
    monkeypatch.setattr(health, "_state", health.FAULT)
    monkeypatch.setattr(health, "_severity", "fault")
    monkeypatch.setattr(health, "_advisory_emitted", False)
    monkeypatch.setattr(health, "_told_sessions", {})
    monkeypatch.setattr(config, "DEGRADED_ADVISORY_ENABLED", True)
    first = health.maybe_advisory("connection:aaaa1111", False)
    again = health.maybe_advisory("connection:aaaa1111", False)
    other = health.maybe_advisory("connection:bbbb2222", False)
    assert first["runbook"] != again["runbook"]
    assert other["runbook"] == first["runbook"]


# --- the service's door -------------------------------------------------------------

TOKEN = "service-token"
PORT = 48999


def _service_app(stopping=lambda: False):
    seen = {}
    activity = server._Activity()

    async def endpoint(scope, receive, send):
        seen["connection"] = session.CONNECTION.get()
        seen["principal"] = server.acl.current_principal() if server.acl.is_active() else None
        seen["in_flight"] = activity.in_flight
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        yield

    stopped = []
    app = server._build_local_service_app(TOKEN, PORT, endpoint, lifespan, activity, lambda: stopped.append(1), stopping)
    return app, seen, activity, stopped


async def _call(app, path="/mcp/", method="POST", headers=None, host=f"127.0.0.1:{PORT}"):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=f"http://{host}") as client:
        return await client.request(method, path, headers={"host": host, **(headers or {})})


def _auth(**extra):
    return {"Authorization": f"Bearer {TOKEN}", **extra}


@pytest.mark.asyncio
async def test_a_request_without_the_token_never_reaches_the_endpoint():
    app, seen, _, _ = _service_app()
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Basic {TOKEN}"}):
        response = await _call(app, headers=headers)
        assert response.status_code == 401
    assert seen == {}


@pytest.mark.asyncio
async def test_a_request_naming_another_host_is_refused():
    # A page that rebinds its own name to 127.0.0.1 sends its name as Host.
    app, seen, _, _ = _service_app()
    response = await _call(app, headers=_auth(), host=f"attacker.example:{PORT}")
    assert response.status_code == 401
    response = await _call(app, headers=_auth(), host="127.0.0.1:1")
    assert response.status_code == 401
    assert seen == {}


@pytest.mark.asyncio
async def test_the_connection_name_becomes_the_bucket_and_a_malformed_one_is_ignored():
    app, seen, activity, _ = _service_app()
    response = await _call(app, headers=_auth(**{service.CONNECTION_HEADER: "0123abcd0123abcd"}))
    assert response.status_code == 200
    assert seen["connection"] == "0123abcd0123abcd"
    assert seen["in_flight"] == 1 and activity.in_flight == 0
    await _call(app, headers=_auth(**{service.CONNECTION_HEADER: "not a name!"}))
    assert seen["connection"] is None
    assert session.CONNECTION.get() is None


@pytest.mark.asyncio
async def test_only_listed_paths_reach_anything():
    app, seen, _, _ = _service_app()
    response = await _call(app, path="/anything", headers=_auth())
    assert response.status_code == 404
    assert seen == {}


@pytest.mark.asyncio
async def test_with_an_acl_every_request_is_the_local_principal(monkeypatch):
    principal = server.acl.Principal(server.acl.LOCAL_CLIENT_ID)
    monkeypatch.setattr(server.acl, "is_active", lambda: True)
    holder = {}
    monkeypatch.setattr(server.acl, "set_principal", lambda p: holder.setdefault("p", p) and "tok")
    monkeypatch.setattr(server.acl, "reset_principal", lambda t: holder.setdefault("reset", t))
    monkeypatch.setattr(server.acl, "current_principal", lambda: holder.get("p"), raising=False)
    app, seen, _, _ = _service_app()
    await _call(app, headers=_auth())
    assert holder["p"] == principal
    assert holder["reset"] == "tok"


@pytest.mark.asyncio
async def test_instance_answers_who_it_is_and_refuses_while_stopping(monkeypatch):
    monkeypatch.setenv("CPERSONA_SERVICE_KEY", "k1")
    monkeypatch.setenv("CPERSONA_SERVICE_SETTINGS", json.dumps({"CPERSONA_X": "h"}))
    app, _, _, _ = _service_app()
    body = (await _call(app, path="/api/v1/instance", method="GET", headers=_auth())).json()
    assert body["service"] == "cpersona" and body["key"] == "k1" and body["settings"] == {"CPERSONA_X": "h"}
    assert body["version"] == server.__version__
    app, _, _, _ = _service_app(stopping=lambda: True)
    response = await _call(app, path="/api/v1/instance", method="GET", headers=_auth())
    assert response.status_code == 503 and "key" not in response.json()


@pytest.mark.asyncio
async def test_stop_asks_the_server_to_exit():
    app, _, _, stopped = _service_app()
    assert (await _call(app, path="/api/v1/stop", headers=_auth())).status_code == 200
    assert stopped == [1]
    assert (await _call(app, path="/api/v1/stop", method="GET", headers=_auth())).status_code == 405


# --- the idle stop ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_service_stops_once_idle_and_not_before():
    activity = server._Activity()
    stopped = []
    activity.last -= 10
    await asyncio.wait_for(server._idle_watch(activity, lambda: stopped.append(1), 5, interval=0.01), 1)
    assert stopped == [1]


@pytest.mark.asyncio
async def test_a_request_in_progress_or_a_recent_one_keeps_it_running():
    activity = server._Activity()
    stopped = []
    activity.last -= 10
    activity.in_flight = 1
    watch = asyncio.create_task(server._idle_watch(activity, lambda: stopped.append(1), 5, interval=0.01))
    await asyncio.sleep(0.05)
    assert stopped == []
    activity.in_flight = 0
    activity.last = __import__("time").monotonic()
    await asyncio.sleep(0.05)
    assert stopped == []
    watch.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await watch


@pytest.mark.asyncio
async def test_zero_never_stops():
    activity = server._Activity()
    activity.last -= 10_000
    stopped = []
    await asyncio.wait_for(server._idle_watch(activity, lambda: stopped.append(1), 0, interval=0.01), 1)
    assert stopped == []


# --- the relay's recovery -----------------------------------------------------------


class _Client:
    def __init__(self, fail_first):
        self.fail_first = fail_first
        self.urls = []

    async def post(self, url, content, headers):
        self.urls.append((url, headers.get("Authorization"), headers.get(service.CONNECTION_HEADER)))
        if len(self.urls) <= self.fail_first:
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})


@pytest.mark.asyncio
async def test_a_service_that_stopped_is_started_again_and_the_message_resent():
    client = _Client(fail_first=1)
    out = asyncio.Queue()

    async def recover(target):
        target.url, target.token = "http://127.0.0.1:2/mcp/", "new"
        return True

    target = proxy_stdio.Target(url="http://127.0.0.1:1/mcp/", token="old", headers={service.CONNECTION_HEADER: "c1"}, recover=recover)
    await proxy_stdio._forward(client, b'{"jsonrpc":"2.0","id":1,"method":"x"}', {}, out, None, target)
    assert client.urls == [("http://127.0.0.1:1/mcp/", "Bearer old", "c1"), ("http://127.0.0.1:2/mcp/", "Bearer new", "c1")]
    assert isinstance(out.get_nowait(), list)


@pytest.mark.asyncio
async def test_recovery_is_tried_once_and_a_failure_is_reported():
    client = _Client(fail_first=5)
    out = asyncio.Queue()
    calls = []

    async def recover(target):
        calls.append(1)
        return True

    target = proxy_stdio.Target(url="u", token="t", recover=recover)
    await proxy_stdio._forward(client, b'{"jsonrpc":"2.0","id":1,"method":"x"}', {}, out, None, target)
    assert calls == [1] and len(client.urls) == 2
    assert isinstance(out.get_nowait(), proxy_stdio._ErrorFor)

    client, out, calls = _Client(fail_first=1), asyncio.Queue(), []

    async def cannot(target):
        calls.append(1)
        return False

    target = proxy_stdio.Target(url="u", token="t", recover=cannot)
    await proxy_stdio._forward(client, b'{"jsonrpc":"2.0","id":1,"method":"x"}', {}, out, None, target)
    assert len(client.urls) == 1
    assert isinstance(out.get_nowait(), proxy_stdio._ErrorFor)


class _TimesOut(_Client):
    async def post(self, url, content, headers):
        self.urls.append(url)
        raise httpx.ReadTimeout("no answer")


@pytest.mark.asyncio
async def test_a_message_that_may_have_been_sent_is_never_sent_again():
    # A timeout or a dropped answer may follow a call that ran: sending it again
    # could store a memory twice. Only "nothing listens" is retried.
    client = _TimesOut(fail_first=0)
    out = asyncio.Queue()
    calls = []

    async def recover(target):
        calls.append(1)
        return True

    target = proxy_stdio.Target(url="u", token="t", recover=recover)
    await proxy_stdio._forward(client, b'{"jsonrpc":"2.0","id":1,"method":"x"}', {}, out, None, target)
    assert client.urls == ["u"] and calls == []
    assert isinstance(out.get_nowait(), proxy_stdio._ErrorFor)


@pytest.mark.asyncio
async def test_a_remote_target_is_not_retried():
    client = _Client(fail_first=1)
    out = asyncio.Queue()
    await proxy_stdio._forward(client, b'{"jsonrpc":"2.0","id":1,"method":"x"}', {}, out, None, proxy_stdio.Target(url="u", token=""))
    assert len(client.urls) == 1
    assert isinstance(out.get_nowait(), proxy_stdio._ErrorFor)


# --- the command --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, "relay"),
        ({"CPERSONA_TRANSPORT": "stdio"}, "relay"),
        ({"CPERSONA_LOCAL_SERVICE": "off"}, "serve"),
        ({"CPERSONA_TRANSPORT": "streamable-http"}, "serve"),
        ({"CPERSONA_TRANSPORT": "local-service"}, "serve"),
    ],
)
def test_the_command_relays_only_a_stdio_launch(monkeypatch, env, expected):
    for name in ("CPERSONA_TRANSPORT", "CPERSONA_LOCAL_SERVICE"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    ran = []
    monkeypatch.setattr(sys, "argv", ["cpersona"])
    monkeypatch.setattr(service, "run_relay", lambda: ran.append("relay"))
    monkeypatch.setattr(server, "serve", lambda: ran.append("serve"))
    entry.run()
    assert ran == [expected]


def test_the_local_service_counts_as_serving_the_local_user(monkeypatch):
    for name, local in (("stdio", True), ("local-service", True), ("streamable-http", False)):
        monkeypatch.setenv("CPERSONA_TRANSPORT", name)
        assert config.serves_local_user() is local
        assert config.shared_transport() is (name == "streamable-http")
    config.validate_transport()


def test_the_local_service_will_not_start_without_its_token(monkeypatch):
    monkeypatch.setenv("CPERSONA_TRANSPORT", "local-service")
    monkeypatch.delenv("CPERSONA_SERVICE_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="CPERSONA_SERVICE_TOKEN"):
        server._preflight_http_auth()
    monkeypatch.setenv("CPERSONA_SERVICE_TOKEN", "t")
    server._preflight_http_auth()


# --- the Windows launcher -----------------------------------------------------------


@pytest.fixture
def windows_spawn(monkeypatch, tmp_path):
    """Run _spawn's Windows branch here, with subprocess.run recorded instead of run."""
    for name, value in (("DETACHED_PROCESS", 0x8), ("CREATE_NEW_PROCESS_GROUP", 0x200), ("CREATE_NO_WINDOW", 0x08000000)):
        monkeypatch.setattr(service.subprocess, name, value, raising=False)
    monkeypatch.setattr(service.os, "name", "nt")
    calls = []

    def run(args, **kwargs):
        calls.append(kwargs)
        behaviour = calls_behaviour[0]
        if isinstance(behaviour, BaseException):
            raise behaviour
        return service.subprocess.CompletedProcess(args, 0, stdout=behaviour, stderr="")

    calls_behaviour = ["4242\n"]
    monkeypatch.setattr(service.subprocess, "run", run)
    return calls, calls_behaviour, str(tmp_path / "s.log")


def test_the_windows_launcher_does_not_inherit_the_relay_stdin(windows_spawn):
    # Restarting a service runs while the relay's reader thread has a read pending on
    # its stdin; a launcher that inherits that pipe waited on it at startup on Windows.
    calls, _, log = windows_spawn
    assert service._spawn({}, log) == 4242
    assert calls[0]["stdin"] is service.subprocess.DEVNULL
    assert calls[0]["creationflags"] & service._CREATE_BREAKAWAY_FROM_JOB


def test_a_launcher_that_hangs_or_fails_is_a_service_error(windows_spawn):
    calls, behaviour, log = windows_spawn
    behaviour[0] = service.subprocess.TimeoutExpired("python", 60)
    with pytest.raises(service.ServiceError, match="launcher failed"):
        service._spawn({}, log)
    behaviour[0] = "not a pid"
    with pytest.raises(service.ServiceError, match="launcher failed"):
        service._spawn({}, log)


# --- real services ------------------------------------------------------------------


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CPERSONA_SERVICE_DIR", str(tmp_path / "svc"))
    env = {
        k: v for k, v in os.environ.items() if not k.startswith(service.SETTING_PREFIXES)
    } | {
        "CPERSONA_SERVICE_DIR": str(tmp_path / "svc"),
        "CPERSONA_DB_PATH": str(tmp_path / "a.db"),
        "EMBEDDING_MODE": "none",
        "CPERSONA_DASHBOARD_STORES": "false",
        "CPERSONA_UPDATE_CHECK": "false",
    }
    yield env
    service.stop()


def test_clients_of_one_store_share_one_service(isolated):
    first = service.ensure(isolated)
    assert first.started
    second = service.ensure(isolated)
    assert (second.key, second.url, second.pid, second.started) == (first.key, first.url, first.pid, False)
    info = json.loads((open(os.path.join(isolated["CPERSONA_SERVICE_DIR"], f"{first.key}.json")).read()))
    if os.name != "nt":
        assert oct(os.stat(os.path.join(isolated["CPERSONA_SERVICE_DIR"], f"{first.key}.json")).st_mode & 0o777) == "0o600"
    assert info["token"] == first.token

    with pytest.raises(service.SettingsMismatch, match="CPERSONA_MAX_MEMORIES") as caught:
        service.ensure(isolated | {"CPERSONA_MAX_MEMORIES": "7"})
    assert "7" not in str(caught.value).replace(isolated["CPERSONA_DB_PATH"], "")

    assert service.stop(first.key) == [first.key]
    # A stopping or stopped service is not reused: the next client gets a new one.
    third = service.ensure(isolated)
    assert third.started and third.pid != first.pid
