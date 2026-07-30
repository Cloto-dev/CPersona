"""bug-198 on the 2.4.x Stable line: say what is true, change nothing else.

The 2.5.3 Current line removed the bind address from the startup decision and
refuses to run unauthenticated wherever it binds. The Stable line promises
behaviour preservation, so that enforcement is deliberately NOT backported —
what is backported is the honesty. Two things are therefore pinned here at once,
and they pull in opposite directions:

* **Behaviour is unchanged.** Loopback + no token still starts; non-loopback +
  no token still exits; a token is still accepted anywhere. If a later edit
  makes the Stable line refuse to start, these tests fail — which is the point.
* **The wording no longer offers an all-clear.** The old text presented a
  loopback bind as containment ("bound to loopback <host> only", "loopback
  address (127.0.0.1) for local-only use"). It is not: tunnels, reverse
  proxies, ``kubectl port-forward``, ``ssh -L`` and published container ports
  all forward to 127.0.0.1.

The all-clear checks are distance regexes, not literal matches: the live string
interpolates the host between "loopback" and "only", and the host itself
contains periods, so both an adjacency check and a ``[^.]`` bounded check would
silently stop detecting the very phrasing they exist to reject.

Third: ``_warn_once_if_remotely_reached`` replaces the inference the startup
guard cannot make (who can reach this port?) with an observation (who did?).
It observes and passes the request through — it must never block, because that
would be the behaviour change this line does not make.
"""

import inspect
import logging
import re
from pathlib import Path

import pytest

from cpersona import server

WARN_LOGGER = "cpersona.server"

# Phrasings that tell an operator a loopback bind contains the exposure. Each is
# distance-bounded with DOTALL rather than adjacency-matched, so an interpolated
# host ("loopback 127.0.0.1 only") or a wrapped line still trips it.
_ALL_CLEAR_PATTERNS = (
    re.compile(r"loopback.{0,60}\bonly\b", re.I | re.S),
    re.compile(r"\bonly\b.{0,60}loopback", re.I | re.S),
    re.compile(r"local[-\s]?only", re.I),
)

# Ways traffic reaches a loopback-bound port from elsewhere. The message has to
# name some of them concretely — "may be reachable" is the same shrug that let
# the old wording pass review.
_FORWARDING_MECHANISMS = ("tunnel", "proxy", "port-forward", "ssh -L", "container port")


def _assert_no_all_clear(message: str) -> None:
    for pattern in _ALL_CLEAR_PATTERNS:
        assert not pattern.search(message), (
            f"message presents a loopback bind as containment via {pattern.pattern!r}: {message!r}"
        )


def _assert_names_the_exposure(message: str) -> None:
    """The claims that make the message worth emitting at all."""
    assert "CPERSONA_AUTH_TOKEN" in message, message
    assert "delete_agent_data" in message, message
    named = [m for m in _FORWARDING_MECHANISMS if m.lower() in message.lower()]
    assert len(named) >= 2, f"names too few forwarding paths {named}: {message!r}"
    # The Stable line only warns; an operator must be told where enforcement is.
    assert "2.5.3" in message, message


# ---------------------------------------------------------------------------
# The pitfall this suite exists downstream of: a sibling checkout of cpersona is
# installed editable in the same interpreter, so `import cpersona` can silently
# resolve to a DIFFERENT tree and every assertion below would describe code that
# is not the code under change. Compared by bytes rather than by path, so a
# packaged (non-editable) install of this same source still passes.
# ---------------------------------------------------------------------------


def test_module_under_test_is_this_checkout():
    loaded = Path(server.__file__).resolve()
    here = (Path(__file__).resolve().parent / "cpersona" / "server.py").resolve()
    assert loaded.read_bytes() == here.read_bytes(), (
        f"imported {loaded} does not match {here} — the suite is testing another checkout"
    )


# ---------------------------------------------------------------------------
# Startup guard: behaviour pinned, wording corrected.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_without_token_still_starts_and_warns(host, caplog):
    """Behaviour pin: the Stable line does not start refusing. It does warn."""
    with caplog.at_level(logging.WARNING, logger=WARN_LOGGER):
        server._assert_safe_http_bind("", host)  # must not raise
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, warnings
    assert host in warnings[0]


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_warning_offers_no_all_clear(host, caplog):
    with caplog.at_level(logging.WARNING, logger=WARN_LOGGER):
        server._assert_safe_http_bind("", host)
    message = caplog.records[-1].getMessage()
    _assert_no_all_clear(message)
    _assert_names_the_exposure(message)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.0.10"])
def test_non_loopback_without_token_still_exits(host):
    """Behaviour pin: the fail-closed branch is untouched."""
    with pytest.raises(SystemExit):
        server._assert_safe_http_bind("", host)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.0.10"])
def test_non_loopback_exit_message_offers_no_all_clear(host):
    """The exit message used to end with "...or rebind to loopback for local-only
    use" — the same all-clear in the shape of remediation advice."""
    with pytest.raises(SystemExit) as excinfo:
        server._assert_safe_http_bind("", host)
    message = str(excinfo.value)
    _assert_no_all_clear(message)
    _assert_names_the_exposure(message)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.0.10", "127.0.0.1"])
def test_token_set_starts_anywhere_without_warning(host, caplog):
    with caplog.at_level(logging.WARNING, logger=WARN_LOGGER):
        server._assert_safe_http_bind("s3cret", host)  # must not raise
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []


# ---------------------------------------------------------------------------
# Observed reachability.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _rearm_once_flag(monkeypatch):
    """The warn-once latch is process-global; without a reset the second test to
    run would pass for the wrong reason (silent because already warned)."""
    monkeypatch.setattr(server, "_remote_reach_warned", False)


def _scope(headers=(), client=("127.0.0.1", 51234)):
    return {
        "type": "http",
        "method": "POST",
        "headers": [(k.encode(), v.encode()) for k, v in headers],
        "client": client,
    }


# Spelled out literally rather than parametrised over server._FORWARDED_HEADERS:
# reading the constant under test would make a deletion from it delete the test
# case too, so the suite would stay green while detection shrank.
_EXPECTED_FORWARDED_HEADERS = (
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "x-real-ip",
    "forwarded",
    "cf-connecting-ip",
    "via",
)


def test_forwarded_header_set_is_exactly_the_documented_one():
    assert set(server._FORWARDED_HEADERS) == set(_EXPECTED_FORWARDED_HEADERS)


@pytest.mark.parametrize("header", _EXPECTED_FORWARDED_HEADERS)
def test_each_forwarding_header_is_evidence_of_reach(header, caplog):
    """Any one of these means something in front of us forwarded the request —
    even though the peer is loopback, which is exactly the blind spot."""
    with caplog.at_level(logging.WARNING, logger=WARN_LOGGER):
        warned = server._warn_once_if_remotely_reached(_scope([(header, "example.test")]), "")
    assert warned, f"{header} did not register as evidence of remote reach"
    message = caplog.records[-1].getMessage()
    assert header in message
    assert "CPERSONA_AUTH_TOKEN" in message


@pytest.mark.parametrize("header", ("x-forwarded-for", "Via", "CF-Connecting-IP"))
def test_header_matching_is_case_insensitive(header, caplog):
    with caplog.at_level(logging.WARNING, logger=WARN_LOGGER):
        assert server._warn_once_if_remotely_reached(_scope([(header, "1.2.3.4")]), "")


def test_remote_peer_without_any_header_is_evidence_of_reach(caplog):
    with caplog.at_level(logging.WARNING, logger=WARN_LOGGER):
        warned = server._warn_once_if_remotely_reached(_scope(client=("203.0.113.9", 4444)), "")
    assert warned
    assert "203.0.113.9" in caplog.records[-1].getMessage()


@pytest.mark.parametrize("peer", ["127.0.0.1", "::1", "localhost"])
def test_local_peer_without_forwarding_headers_stays_quiet(peer, caplog):
    """The ordinary local-development case must not warn on every request."""
    scope = _scope([("content-type", "application/json"), ("user-agent", "curl/8")], (peer, 5))
    with caplog.at_level(logging.WARNING, logger=WARN_LOGGER):
        assert server._warn_once_if_remotely_reached(scope, "") is False
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_missing_client_in_scope_stays_quiet(caplog):
    """ASGI allows client=None; an unknown peer is not evidence of anything."""
    with caplog.at_level(logging.WARNING, logger=WARN_LOGGER):
        assert server._warn_once_if_remotely_reached(_scope(client=None), "") is False


@pytest.mark.parametrize(
    "scope",
    [_scope([("x-forwarded-for", "203.0.113.9")]), _scope(client=("203.0.113.9", 4444))],
)
def test_authenticated_server_never_warns(scope, caplog):
    """A proxy in front of an authenticated server is a normal deployment, not a
    finding — warning there would train operators to ignore the message."""
    with caplog.at_level(logging.WARNING, logger=WARN_LOGGER):
        assert server._warn_once_if_remotely_reached(scope, "s3cret") is False
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_warns_once_per_process(caplog):
    scope = _scope([("x-forwarded-for", "203.0.113.9")])
    with caplog.at_level(logging.WARNING, logger=WARN_LOGGER):
        first = server._warn_once_if_remotely_reached(scope, "")
        rest = [server._warn_once_if_remotely_reached(scope, "") for _ in range(5)]
    assert first is True
    assert rest == [False] * 5
    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1


def test_observer_is_mounted_in_the_http_middleware():
    """A helper that is unit-tested but never called is indistinguishable at
    runtime from one that was never written. The middleware is a closure inside
    ``_run_http_server`` and cannot be imported on this line (2.5.3 lifted it to
    module level; restructuring here would be the behaviour change this backport
    avoids), so the call site is checked in the source."""
    source = inspect.getsource(server._run_http_server)
    assert "_warn_once_if_remotely_reached(scope, auth_token)" in source
