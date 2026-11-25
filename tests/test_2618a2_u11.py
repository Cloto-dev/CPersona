"""The dashboard's key file beside the store (bug-916, bug-915, bug-917).

The key signs the remote dashboard's session cookies and the proofs the command
line sends to the local dashboard. The docs call the file owner-only and say that
deleting it ends every session; the permissions check is meant to list every path
the package places.

A planted key is detected through the public paths that use it: a cookie and a
proof made from known text are accepted only when the key in use is that text.
"""

import base64
import hashlib
import hmac
import logging
import os
import stat
import threading
import time

import pytest

from cpersona import acl, checks, config, fileperms
from cpersona.dashboard import local, remote

KNOWN = "known-text-for-a-planted-key-file-0123456789abcdef"
NEW = "another-key-text-written-by-the-operator-0123456789"

posix_modes = pytest.mark.skipif(not fileperms.mode_bits_are_enforced(), reason="posix modes only")
needs_symlinks = pytest.mark.skipif(not hasattr(os, "symlink") or os.name != "posix", reason="posix symlinks only")
needs_fifo = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this platform")


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DASHBOARD_SESSION_KEY", "")
    remote.clear()
    yield str(tmp_path / "memories.db")
    acl.activate(None)
    remote.clear()


def _key_file(tmp_path):
    return tmp_path / "memories-dashboard.key"


def _plant(path, text=KNOWN, mode=0o600):
    with open(path, "w") as f:
        f.write(text + "\n")
    os.chmod(path, mode)


def _forged_cookie(owner: str, text: str = KNOWN) -> str:
    key = hashlib.sha256(text.encode()).digest()
    until = int(time.time()) + 3600
    return f"v1.{remote._b64(owner.encode())}.{until}.{remote._mac(key, owner, until, b'')}"


# The port a proof is made for; the proof signs it (bug-914).
PROOF_PORT = 48700


def _forged_proof(text: str = KNOWN) -> str:
    key = hashlib.sha256(text.encode()).digest()
    stamp = str(int(time.time() * local._STAMP_UNIT))
    message = b"\0".join((local._DOMAIN, b"code", str(PROOF_PORT).encode(), stamp.encode()))
    raw = hmac.new(key, message, hashlib.sha256).digest()
    return f"{stamp}.{base64.urlsafe_b64encode(raw).decode().rstrip('=')}"


def _tokenless_client():
    # A client that signs in by OAuth holds no static token.
    acl.activate(acl.AclConfig(grants_by_client={"v": {"a": acl.PERM_READ}}, token_entries=()))


def _signs_with(store: str, text: str = KNOWN) -> tuple[bool, bool]:
    """Whether a cookie and a local proof made from ``text`` are accepted."""
    return remote.session(_forged_cookie("v", text), store) is not None, local.proof_ok(store, "code", _forged_proof(text), PROOF_PORT)


# --- bug-916: what the key file is ------------------------------------------


@posix_modes
def test_bug_916_control_a_private_key_file_already_there_is_the_one_in_use(store, tmp_path):
    # Without this, "refused" could not be told from "no key file is ever used".
    _plant(_key_file(tmp_path))
    _tokenless_client()
    assert _signs_with(store) == (True, True)


@needs_symlinks
def test_bug_916_a_symlink_at_the_key_path_is_not_followed(store, tmp_path, caplog):
    target = tmp_path / "elsewhere.txt"
    _plant(target)  # owner-only and ours: only the link is wrong
    os.symlink(target, _key_file(tmp_path))
    _tokenless_client()
    with caplog.at_level(logging.WARNING, logger="cpersona.dashboard.remote"):
        assert _signs_with(store) == (False, False), "a key read through a symlink signed a forged cookie or proof"
    assert "memories-dashboard.key" in caplog.text
    # Left as it was, not replaced or written through.
    assert os.path.islink(_key_file(tmp_path)) and target.read_text().strip() == KNOWN


@posix_modes
@pytest.mark.parametrize("mode", [0o640, 0o604, 0o620], ids=lambda m: f"{m:04o}")
def test_bug_916_a_key_file_other_accounts_can_reach_is_not_used(store, tmp_path, mode):
    _plant(_key_file(tmp_path), mode=mode)
    _tokenless_client()
    assert _signs_with(store) == (False, False), f"a {mode:04o} key file signed a forged cookie or proof"
    assert stat.S_IMODE(os.stat(_key_file(tmp_path)).st_mode) == mode  # its mode is the operator's


@posix_modes
def test_bug_916_a_key_file_owned_by_another_account_is_not_used(store, tmp_path, monkeypatch):
    _plant(_key_file(tmp_path))
    owner = os.stat(_key_file(tmp_path)).st_uid
    monkeypatch.setattr(os, "geteuid", lambda: owner + 1)
    _tokenless_client()
    assert _signs_with(store) == (False, False), "a key file owned by another account signed a forged cookie or proof"


@needs_fifo
def test_bug_916_a_fifo_at_the_key_path_neither_blocks_nor_signs(store, tmp_path):
    os.mkfifo(_key_file(tmp_path), 0o600)
    got = {}
    reader = threading.Thread(target=lambda: got.update(key=remote._signing_key(store)), daemon=True)
    reader.start()
    reader.join(5)
    if reader.is_alive():
        # Let the blocked open return so the thread ends.
        os.close(os.open(_key_file(tmp_path), os.O_WRONLY | os.O_NONBLOCK))
        reader.join(5)
        pytest.fail("reading the key blocked on a FIFO at the key path")
    assert len(got["key"]) == 32


@posix_modes
def test_bug_916_control_sessions_under_a_refused_key_file_last_while_the_process_runs(store, tmp_path):
    # The key used instead of a refused file is made once, not on every request.
    _plant(_key_file(tmp_path), mode=0o644)
    value = remote.redeem(remote.issue_link(), store)
    assert remote.session(value, store) is not None
    assert remote.session(value, store) is not None
    assert local.proof_ok(store, "code", local.proof(store, "code", PROOF_PORT), PROOF_PORT)


@posix_modes
def test_bug_916_a_key_file_widened_while_the_server_runs_stops_signing(store, tmp_path):
    value = remote.redeem(remote.issue_link(), store)
    assert remote.session(value, store) is not None
    os.chmod(_key_file(tmp_path), 0o644)
    assert remote.session(value, store) is None, "a key file other accounts can now read still signs sessions"
    os.chmod(_key_file(tmp_path), 0o600)  # narrowed again, as check_health(fix=True) does
    assert remote.session(value, store) is not None


# --- bug-915: deleting or replacing the key file while the server runs ------


def test_bug_915_deleting_the_key_file_ends_every_session_without_a_restart(store, tmp_path):
    value = remote.redeem(remote.issue_link(), store)
    proof = local.proof(store, "code", PROOF_PORT)
    assert _key_file(tmp_path).exists()
    assert remote.session(value, store) is not None
    _key_file(tmp_path).unlink()  # nothing clears the process's state
    assert remote.session(value, store) is None, "a session outlived the deleted key file"
    assert not local.proof_ok(store, "code", proof, PROOF_PORT)
    # A new key file is made, and a new link opens a session that keeps working.
    fresh = remote.redeem(remote.issue_link(), store)
    assert _key_file(tmp_path).exists()
    assert remote.session(fresh, store) is not None
    assert remote.session(fresh, store) is not None


@pytest.mark.parametrize("how", ["replace", "rewrite"])
def test_bug_915_replacing_the_key_file_ends_every_session(store, tmp_path, how):
    value = remote.redeem(remote.issue_link(), store)
    assert remote.session(value, store) is not None
    if how == "replace":
        _plant(tmp_path / "new.key", NEW)
        os.replace(tmp_path / "new.key", _key_file(tmp_path))
    else:
        with open(_key_file(tmp_path), "w") as f:  # same file, new text
            f.write(NEW + "\n")
    assert remote.session(value, store) is None, "a session outlived the replaced key"
    # Without an ACL a session is bound to the server's token, empty here.
    assert remote.session(_forged_cookie("", NEW), store) is not None


# --- bug-917: the permissions check sees the key file -----------------------


@posix_modes
@pytest.mark.asyncio
async def test_bug_917_a_widened_key_file_is_reported_and_narrowed(tmp_path, monkeypatch):
    root = tmp_path / "store"
    root.mkdir(mode=0o700)
    db_path = str(root / "memories.db")
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(config, "DASHBOARD_SESSION_KEY", "")
    remote.clear()
    try:
        remote._signing_key(db_path)  # the key file the dashboard places
        key = root / "memories-dashboard.key"
        assert key.exists()
        with open(db_path, "w") as f:
            f.write("x")
        os.chmod(db_path, 0o600)
        os.chmod(key, 0o644)  # a restore that lost its modes

        assert str(key) in {o.path for o in fileperms.owned_paths()}, "the key file is not an owned path"
        issues = await checks.check_file_permissions(None, fix=True)
        hits = [i for i in issues if i.get("path") == str(key)]
        assert len(hits) == 1, f"the widened key file was not reported: {issues}"
        assert hits[0]["type"] == "file_reachable_by_other_accounts"
        assert hits[0]["mode"] == "0644" and hits[0]["fixed"] is True
        assert stat.S_IMODE(os.stat(key).st_mode) == 0o600
    finally:
        remote.clear()
