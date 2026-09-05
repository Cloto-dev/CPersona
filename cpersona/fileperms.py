"""The one place this package brings a file or a directory into existence.

Every path CPersona creates holds either the corpus itself — the database and
the ``-wal`` / ``-shm`` sidecars SQLite derives from it, an export, the
contiguous embedding index — or something that decides who reaches it: the
alias ledger, the calibration sidecar. Created through a bare
``open(path, "w")`` each one lands at ``0o666 & ~umask``, which under the
default ``umask 022`` is ``0o644``. The mode is then not a property of the
data at all; it is a property of whichever shell happened to start the
process, and on a shared host every local account can read the whole memory
corpus.

So creation goes through here, and the modes below are asked for at the moment
the inode appears rather than repaired afterwards — there is no window in which
the corpus exists world-readable. Three primitives, one per way this package
brings a path into being:

``open_private``      a file this process writes itself.
``create_private``    a file another library will open by name (SQLite).
``makedirs_private``  the directory either of those needs.

Two things are deliberately NOT here. Files that already exist keep the mode
they have: an operator who widened one meant it, and this module only decides
what a *new* file starts as. And ``tempfile.mkstemp`` / ``mkdtemp`` are already
``0600`` / ``0700`` by their own documented contract, so a caller using them is
not bypassing anything.

Portability is a constraint rather than an afterthought. The package ships as
``Operating System :: OS Independent`` and desktop clients install it on
Windows, where ``os.fchmod`` does not exist and a creation mode is ignored;
a FAT / exFAT / CIFS mount refuses ``fchmod`` with ``EPERM`` on Unix too.
Every helper here therefore *asks* for the narrow mode and never fails the
write when the platform or the filesystem will not honour it — a refused chmod
must not cost the caller its data. Where the request cannot be honoured these
functions degrade to exactly the plain call the caller would have written.
"""

from __future__ import annotations

import logging
import os
from typing import NamedTuple

logger = logging.getLogger(__name__)

# Owner-only. Applied to every file this package creates.
PRIVATE_FILE_MODE = 0o600

# Owner-only, traversable by the owner. Applied to directories this package
# creates. Note that a directory mode is defence in depth and not the control
# that matters: a 0600 file inside a 0755 directory is still unreadable by
# anyone else. It is the file mode that protects the corpus.
PRIVATE_DIR_MODE = 0o700

# CPython's own FileIO always passes this on Windows and does newline
# translation in the io layer instead. Omitting it would put the descriptor in
# text mode, which rewrites "\n" as "\r\n" — silently corrupting the embedding
# index, whose bytes are a struct, not a document.
_BINARY = getattr(os, "O_BINARY", 0)

_WRITE_MODES = ("w", "wb", "a", "ab")


def _tighten(fd: int, path: str) -> None:
    """Best-effort ``fchmod`` on an already-open descriptor.

    ``os.open`` applies its mode argument only when it CREATES the file, so a
    temp path left behind by a killed run is reopened carrying whatever mode it
    already had — and the ``os.replace`` that follows installs that mode over
    the destination. This call is what covers the reused inode; it is not
    redundant with the mode passed to ``os.open``.

    A failure is logged and swallowed, for the reason in the module docstring:
    losing the write is worse than writing at the platform's default mode on a
    platform that has no mode to speak of.
    """
    fchmod = getattr(os, "fchmod", None)
    if fchmod is None:  # Windows
        return
    try:
        fchmod(fd, PRIVATE_FILE_MODE)
    except OSError as exc:
        logger.debug("could not restrict %s to %o: %s", path, PRIVATE_FILE_MODE, exc)


def open_private(path, mode: str = "w", *, encoding: str | None = None):
    """``open(path, mode)`` for writing, with the file created owner-only.

    Accepts the write modes this package actually uses; a read mode is a
    caller error rather than a silently-ignored argument, because a reader
    that reached for this helper is confused about what it does.
    """
    if mode not in _WRITE_MODES:
        raise ValueError(
            f"open_private is for creating files, not reading them: {mode!r} "
            f"is not one of {_WRITE_MODES}"
        )
    flags = os.O_WRONLY | os.O_CREAT | _BINARY
    flags |= os.O_APPEND if "a" in mode else os.O_TRUNC
    fd = os.open(path, flags, PRIVATE_FILE_MODE)
    try:
        _tighten(fd, path)
        return os.fdopen(fd, mode, encoding=encoding)
    except BaseException:
        os.close(fd)
        raise


def create_private(path: str) -> bool:
    """Create ``path`` as an empty owner-only file; report whether we made it.

    For a file some other library opens by name. SQLite is the case that
    motivates it, and the reason pre-creation beats a chmod afterwards is that
    SQLite copies the database file's mode onto the ``-wal`` and ``-shm``
    sidecars it derives from it — measured on 3.40.1 and 3.53.1: handing it a
    file that is already ``0600`` yields ``0600`` for all three, while creating
    the database itself yields ``0644`` for all three under ``umask 022``. A
    zero-length file is a valid empty database, so SQLite initialises it
    exactly as it would one of its own.

    Returns False when the path already exists — an existing file's mode is the
    operator's — and also when creation failed for any other reason, so that
    the library opening the path next produces its own diagnostic rather than
    this call replacing it with a less specific one.
    """
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, PRIVATE_FILE_MODE)
    except FileExistsError:
        return False
    except OSError as exc:
        logger.debug("could not pre-create %s: %s", path, exc)
        return False
    os.close(fd)
    return True


def makedirs_private(path: str) -> None:
    """``os.makedirs(path, exist_ok=True)`` with a new leaf created owner-only.

    Two limits worth naming rather than discovering. The mode reaches the leaf
    only — since 3.7 intermediate directories are created at the default — and
    it is masked by the umask, which can narrow it further but never widen it.
    An existing directory is left exactly as it is.
    """
    os.makedirs(path, mode=PRIVATE_DIR_MODE, exist_ok=True)


# --- Reading the modes back -------------------------------------------------
#
# The three primitives above decide what a NEW path starts as. What follows is
# their counterpart: naming the paths so something can look at what the ones
# already on disk actually are. Pre-creation cannot reach those -- an install
# that upgraded keeps the files it already had, deliberately -- so the only way
# an operator learns about them is if something enumerates them and says so.


class OwnedPath(NamedTuple):
    """A path this package places, and what reaching it would give someone."""

    path: str
    kind: str  # "file" | "dir"
    holds: str


def mode_bits_are_enforced(platform_name: str | None = None) -> bool:
    """Whether this platform's permission bits are the access control.

    On Windows they are not: the ACL decides, ``os.chmod`` honours only the
    read-only flag by its own documented limit, and the rest of the mode is not
    a statement anything enforces. A reader that scored those bits there would
    report every install as reachable by everyone and offer a repair that
    changes nothing -- so it declines to have an opinion instead. This is the
    same rule the creators above already follow for writes, stated for reads.

    ``platform_name`` defaults to ``os.name`` and exists so both answers can be
    measured. Rebinding ``os.name`` to do that is not an option: it is global,
    and it makes ``pathlib`` mint a ``WindowsPath`` -- which pytest constructs
    while formatting a failure, so a test written that way passes cleanly and
    then crashes the run the first time it has something to report.
    """
    return (os.name if platform_name is None else platform_name) == "posix"


def owned_paths() -> list[OwnedPath]:
    """Every path this package places, asked of the module that places it.

    Derived rather than listed: each entry calls the function that decides where
    the file goes, so moving one moves this with it. Paths that do not exist are
    included -- the caller decides what absence means, and a list that quietly
    dropped them could not tell "not created yet" from "not known about".

    One surface is deliberately absent. An export goes to a path its CALLER
    chooses; this package places it nowhere, and a reader cannot enumerate
    directories it was never told about. New exports are created owner-only like
    everything else here, so what is out of reach is the ones written before
    that -- named here rather than left as a silent hole in the list.
    """
    import glob as _glob

    from cpersona import config, update_check, vector_index

    db = config.DB_PATH
    if db == ":memory:":
        return []

    sidecar = f"{db}.calibration.json"
    out = [
        OwnedPath(db, "file", "every stored memory"),
        OwnedPath(f"{db}-wal", "file", "the memories not yet checkpointed into the database"),
        OwnedPath(f"{db}-shm", "file", "the shared-memory index for the write-ahead log"),
        OwnedPath(sidecar, "file", "the calibrated thresholds that decide recall breadth"),
        OwnedPath(update_check.cache_path(), "file", "the cached release verdict"),
        OwnedPath(config.alias_ledger_path(), "file", "which memory space each caller reaches"),
    ]
    out += [
        OwnedPath(vector_index.index_path(table), "file", f"an embedding of every {table} row")
        for table in ("memories", "episodes")
    ]
    # Written by _back_up_calibration_sidecar under a name it mints, so the
    # pattern is the only way to name them; it is the one the pruner uses.
    out += [
        OwnedPath(p, "file", "a previous generation of the calibrated thresholds")
        for p in sorted(_glob.glob(f"{_glob.escape(sidecar)}.before-*"))
    ]
    directory = os.path.dirname(db)
    if directory:
        out.append(OwnedPath(directory, "dir", "the files above"))
    return out
