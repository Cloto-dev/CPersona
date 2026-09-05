"""bug-289: every file this server creates is owner-only, whatever the umask is.

Before this, one write path out of eight asked for a mode. The database, its
``-wal`` / ``-shm`` sidecars, exports, the embedding index, the calibration
sidecar and its backups all landed at ``0o666 & ~umask`` -- ``0o644`` under the
default -- so on a shared host every local account could read the whole corpus.
Measured on a real deployment before the fix: the database and both sidecars
were ``0o644``, and the alias ledger, the one path that already called
``fchmod``, was ``0o600``.

Every test here pins its own umask to ``0o022`` rather than inheriting the
runner's. The bug IS umask sensitivity, so a suite that ran at ``umask 077``
would pass against the unfixed code and prove nothing.
"""

import os
import sqlite3
import stat

import pytest
import pytest_asyncio

from cpersona import admin_handlers, config, database, fileperms
from cpersona.database import get_db

# POSIX modes are what this whole module is about. Windows has no fchmod and
# ignores a creation mode, which the seam handles by degrading rather than
# failing -- there is simply nothing here to assert there.
pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")


@pytest.fixture
def loose_umask():
    """Run the body under the default umask, the one the bug needs."""
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


def mode_of(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


# ---------------------------------------------------------------------------
# The seam itself
# ---------------------------------------------------------------------------


def test_open_private_creates_an_owner_only_file(tmp_path, loose_umask):
    path = tmp_path / "new.jsonl"
    with fileperms.open_private(path, "w", encoding="utf-8") as fh:
        fh.write("row\n")
    assert mode_of(path) == 0o600

    # And the plain call it replaces does not, or this test would pass against
    # the code the finding is about.
    plain = tmp_path / "plain.jsonl"
    with open(plain, "w", encoding="utf-8") as fh:
        fh.write("row\n")
    assert mode_of(plain) == 0o644


def test_open_private_tightens_a_path_it_did_not_create(tmp_path, loose_umask):
    """The case a creation mode alone does not cover.

    Three of the four write paths write ``<dest>.tmp`` and ``os.replace`` it
    onto the destination. A run killed mid-write leaves that temp path behind
    at whatever mode it had; ``os.open`` applies its mode only when it CREATES
    the file, so the next run would reuse the loose inode and then install its
    mode over the destination. The ``fchmod`` in the seam is what closes that,
    and removing it must fail here.
    """
    stale = tmp_path / "export.jsonl.tmp.999"
    stale.write_text("half a run\n", encoding="utf-8")
    os.chmod(stale, 0o644)

    with fileperms.open_private(stale, "w", encoding="utf-8") as fh:
        fh.write("a whole one\n")

    assert mode_of(stale) == 0o600
    assert stale.read_text(encoding="utf-8") == "a whole one\n", "O_TRUNC lost"


def test_open_private_round_trips_bytes_untouched(tmp_path, loose_umask):
    """The embedding index is a struct, not a document.

    Pins the binary contract the seam has to preserve: no newline translation,
    which is what an ``os.open`` without ``O_BINARY`` would introduce on
    Windows.
    """
    path = tmp_path / "index.bin"
    payload = b"\x00\r\n\x01\n\xff"
    with fileperms.open_private(path, "wb") as fh:
        fh.write(payload)
    assert path.read_bytes() == payload


def test_open_private_refuses_a_read_mode(tmp_path):
    with pytest.raises(ValueError, match="not reading"):
        fileperms.open_private(tmp_path / "x", "r")


def test_create_private_reports_whether_it_made_the_file(tmp_path, loose_umask):
    path = tmp_path / "corpus.db"

    assert fileperms.create_private(path) is True
    assert mode_of(path) == 0o600

    # An existing file belongs to whoever set its mode. Widening it by hand and
    # asking again must leave that alone and say so.
    os.chmod(path, 0o644)
    assert fileperms.create_private(path) is False
    assert mode_of(path) == 0o644


def test_sqlite_gives_its_sidecars_the_database_file_s_mode(tmp_path, loose_umask):
    """Why the database is pre-created rather than chmod-ed after connecting.

    SQLite derives the ``-wal`` and ``-shm`` modes from the database file's, so
    handing it one that is already private settles all three at once -- and
    leaves no window in which the corpus exists world-readable. This test is the
    claim itself: if a future SQLite stops doing it, the design premise is gone
    and this fails rather than the protection quietly thinning.
    """
    handed = tmp_path / "handed.db"
    fileperms.create_private(handed)
    conn = sqlite3.connect(handed)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t (x)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
        for suffix in ("", "-wal", "-shm"):
            sidecar = tmp_path / f"handed.db{suffix}"
            assert sidecar.exists(), f"{sidecar.name} was never created"
            assert mode_of(sidecar) == 0o600, sidecar.name
    finally:
        conn.close()


def test_makedirs_private_creates_narrow_and_leaves_existing_alone(tmp_path, loose_umask):
    fresh = tmp_path / "fresh"
    fileperms.makedirs_private(fresh)
    assert mode_of(fresh) == 0o700

    already = tmp_path / "already"
    already.mkdir(mode=0o755)
    os.chmod(already, 0o755)
    fileperms.makedirs_private(already)
    assert mode_of(already) == 0o755, "an existing directory's mode is the operator's"


# ---------------------------------------------------------------------------
# The call sites, end to end
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def clean_db():
    db = await get_db()
    for table in ("memories", "episodes", "profiles", "pending_memory_tasks"):
        await db.execute(f"DELETE FROM {table}")
    await db.commit()
    return db


@pytest.mark.asyncio
async def test_booting_a_database_leaves_no_world_readable_corpus(
    tmp_path, monkeypatch, loose_umask
):
    """The whole point, through the real boot path.

    Covers what the structural gate cannot: the gate proves ``database.py``
    calls the seam, not that it calls it BEFORE handing the path to SQLite.
    Moving the call after ``connect`` keeps the gate green and the corpus at
    0644.
    """
    home = tmp_path / "made" / "by" / "cpersona"
    saved = database._db
    monkeypatch.setattr(database, "DB_PATH", str(home / "cpersona.db"))
    database._db = None
    try:
        async with database.transaction() as db:
            await db.execute(
                "INSERT INTO memories (agent_id, content, source, timestamp) "
                "VALUES ('n07', 'row', '{}', 't')"
            )
        assert mode_of(home) == 0o700, "the directory the boot created"
        for suffix in ("", "-wal", "-shm"):
            path = home / f"cpersona.db{suffix}"
            assert path.exists(), f"{path.name} was never created"
            assert mode_of(path) == 0o600, path.name
    finally:
        if database._db is not None:
            await database._db.close()
        database._db = saved


@pytest.mark.asyncio
async def test_an_export_is_not_readable_by_the_rest_of_the_host(
    clean_db, tmp_path, loose_umask
):
    """An export is the entire corpus in one file, in plain text."""
    await clean_db.execute(
        "INSERT INTO memories (agent_id, content, source, timestamp) "
        "VALUES ('n07-export', 'secret enough', '{}', 't')"
    )
    await clean_db.commit()

    out = tmp_path / "backups" / "corpus.jsonl"
    result = await admin_handlers.do_export_memories(
        agent_id="n07-export", output_path=str(out)
    )

    assert result["ok"] is True
    assert mode_of(out) == 0o600
    assert mode_of(out.parent) == 0o700, "the directory the export created"


def test_the_calibration_sidecar_and_its_backup_are_owner_only(
    tmp_path, monkeypatch, loose_umask
):
    """Not the corpus, but it names every agent that has one."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "cal.db"))

    assert admin_handlers._save_calibration_state(
        768, "a-model", 0.5, {"n07": 0.5}
    ), "the sidecar write itself failed; this test is not measuring modes"

    sidecar = admin_handlers._calibration_sidecar_path()
    assert mode_of(sidecar) == 0o600

    backup = admin_handlers._backup_calibration_sidecar("older")
    assert backup, "no backup was written; the mode assertion below would be vacuous"
    assert mode_of(backup) == 0o600


def test_a_platform_without_fchmod_still_gets_its_write(tmp_path, monkeypatch):
    """Windows has no ``os.fchmod``.

    A missing chmod must cost the caller the mode, never the data -- so this
    asserts the write still lands, not that the mode does.
    """
    monkeypatch.delattr(os, "fchmod", raising=False)
    path = tmp_path / "no-fchmod.txt"
    with fileperms.open_private(path, "w", encoding="utf-8") as fh:
        fh.write("still written\n")
    assert path.read_text(encoding="utf-8") == "still written\n"


def test_a_filesystem_that_refuses_fchmod_still_gets_its_write(tmp_path, monkeypatch):
    """A FAT / exFAT / CIFS mount raises EPERM. Same rule: ask, do not insist."""

    def refuse(*_args, **_kwargs):
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(os, "fchmod", refuse, raising=False)
    path = tmp_path / "refused.txt"
    with fileperms.open_private(path, "w", encoding="utf-8") as fh:
        fh.write("also written\n")
    assert path.read_text(encoding="utf-8") == "also written\n"


def test_a_pre_create_that_cannot_happen_defers_to_the_real_opener(tmp_path):
    """``create_private`` swallowing its own OSError is deliberate.

    An unwritable directory used to surface as SQLite's own "unable to open
    database file". Raising PermissionError from the pre-create instead would
    replace a specific diagnostic with a less specific one for no gain, since
    the connect that follows fails anyway.
    """
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    os.chmod(locked, 0o500)
    try:
        assert fileperms.create_private(locked / "cpersona.db") is False
        assert not (locked / "cpersona.db").exists()
    finally:
        os.chmod(locked, 0o700)


def test_the_probe_would_have_failed_before_the_fix():
    """A guard on the guard: the constants are the narrow ones.

    Cheap, but it is the one assertion that fails if someone 'fixes' a mode
    mismatch by widening the constant instead of the call site.
    """
    assert fileperms.PRIVATE_FILE_MODE == 0o600
    assert fileperms.PRIVATE_DIR_MODE == 0o700
    assert not fileperms.PRIVATE_FILE_MODE & (stat.S_IRWXG | stat.S_IRWXO)
    assert not fileperms.PRIVATE_DIR_MODE & (stat.S_IRWXG | stat.S_IRWXO)



# ---------------------------------------------------------------------------
# The half that pre-creation cannot reach: a database that already exists
# ---------------------------------------------------------------------------


async def _boot_against(path, caplog):
    """Boot a fresh connection against ``path`` and return what was logged."""
    import logging

    saved = database._db
    saved_path = database.DB_PATH
    database.DB_PATH = str(path)
    database._db = None
    try:
        with caplog.at_level(logging.WARNING, logger="cpersona.database"):
            await database.get_db()
        return [r.getMessage() for r in caplog.records]
    finally:
        if database._db is not None:
            await database._db.close()
        database._db = saved
        database.DB_PATH = saved_path


@pytest.mark.asyncio
async def test_an_upgrade_says_so_when_the_existing_corpus_is_readable(tmp_path, caplog):
    """The one thing code cannot fix, so it has to report it.

    An install that upgrades keeps the 0644 database it already has -- the fix
    only decides what a NEW file starts as. Nothing else in the system would
    ever mention that, which is how it survived on a live deployment.
    """
    existing = tmp_path / "inherited.db"
    existing.touch()
    os.chmod(existing, 0o644)

    messages = await _boot_against(existing, caplog)

    assert any("group/world-accessible" in m for m in messages), messages
    assert any("chmod 600" in m for m in messages), "the warning has to say what to do"
    assert mode_of(existing) == 0o644, "a warning must not silently rewrite the operator's mode"


@pytest.mark.asyncio
async def test_a_private_corpus_says_nothing(tmp_path, caplog):
    """Or the warning becomes the line everyone learns to skip."""
    private = tmp_path / "private.db"
    private.touch()
    os.chmod(private, 0o600)

    messages = await _boot_against(private, caplog)

    assert not any("group/world-accessible" in m for m in messages), messages


@pytest.mark.asyncio
async def test_no_warning_where_the_bits_are_not_the_access_control(tmp_path, caplog, monkeypatch):
    """The same rule the seam already follows for writes, applied to this read.

    This package ships ``Operating System :: OS Independent`` and the helpers
    above degrade rather than fail where a mode cannot be honoured. The warning
    was the one place that still had an opinion: on such a platform the mode is
    not the access control, so it would fire on every boot of every install and
    advise a chmod that changes nothing -- the line everyone learns to skip, for
    a whole platform, permanently. CI runs on Linux only, so nothing here would
    have said so.
    """
    widened = tmp_path / "widened.db"
    widened.touch()
    os.chmod(widened, 0o644)

    monkeypatch.setattr(fileperms, "mode_bits_are_enforced", lambda: False)
    messages = await _boot_against(widened, caplog)

    assert not any("group/world-accessible" in m for m in messages), messages
    assert mode_of(widened) == 0o644, "and it still does not touch the file"
