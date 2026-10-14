"""The alias ledger: (issuer, subject) → alias, persisted (docs/OAUTH_DESIGN.md §12).

Per-subject partitioning needs a name for "this signed-in person's memory
space", and the one identifier that is stable for a person is the (issuer,
subject) pair the verified token carries (OIDC Core §5.7). That pair is not the
name used, though: a raw subject baked into ``agent_id`` would outlive the
provider that minted it — a provider migration, a custom-domain move, or a
switch to pairwise identifiers each re-issue every subject, and data keyed by
the old values would be orphaned with no seam to repair it at. So the ledger
issues an opaque alias and owns the mapping, and repairing any of those events
is an edit to this file rather than a rewrite of the memory store.

The ledger is written by the server (first connection issues an alias) and by
the operator (pointing two (issuer, subject) rows at one alias is manual
account linking — the escape hatch for every re-issue event above). It
therefore lives beside the database, not beside ``acl.json``: the grant table
is operator-written policy the server must never touch, and on a hardened
deployment its directory is not writable by the service user at all.

Failure posture matches the ACL loader (docs/ACL_DESIGN.md §7): a ledger that
exists but cannot be parsed refuses startup. Silently starting over would
re-issue fresh aliases for every known subject, severing each person from the
memory space they already own — the quiet version of the exact loss the ledger
exists to prevent. A persist failure at issuance refuses the request the same
way: an alias that authorized a write but was never durably recorded would be
re-rolled on restart, stranding whatever the write stored.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import secrets
import tempfile
import time

from cpersona import fileperms

logger = logging.getLogger(__name__)

#: The reserved prefix. Every alias the ledger issues starts with it, the boot
#: check refuses a database already using it for something else, and the ACL
#: boundary treats names under it as subject space — one namespace, three
#: readers, so the constant lives here and is imported, never retyped.
ALIAS_PREFIX = "u-"

#: What an alias may look like, issued or operator-written. Operators edit this
#: file (manual account linking), so the shape is validated on load rather than
#: trusted: an alias outside the reserved namespace would dodge the boot
#: collision check that keeps aliases and pre-existing agent ids apart.
_ALIAS_RE = re.compile(r"^u-[0-9a-f]{4,64}$")

_LEDGER_VERSION = 1

#: How long after a modification the file's stat fingerprint is not trusted.
#: Filesystems stamp mtime at a coarse grain (a scheduler tick on some, whole
#: seconds on others), so a same-size edit in place made within that grain of
#: our read leaves inode, size and mtime all unchanged. A file modified this
#: recently is re-read on every call until it is older than the window.
_RACY_WINDOW_NS = 2_000_000_000


class AliasLedgerError(Exception):
    """A ledger defect — refuse rather than degrade (see module docstring)."""


class AliasLedger:
    """The (issuer, subject) → alias map, following its file.

    The file is the record, and the operator is one of its writers: relinking a
    subject or removing a row is an edit to it. Every read therefore checks the
    file's fingerprint and re-reads it when it changed, so an edit takes effect
    on the next call rather than at the next restart. This process's own rows
    need no merging back in, because it persists each one in the call that
    mints it.
    """

    def __init__(self, path: str):
        self._path = path
        # issuer → subject → alias. Nested rather than a joined key because a
        # subject is an arbitrary printable string (OIDC allows up to 255 ASCII
        # characters) and no separator can be guaranteed absent from it.
        self._aliases: dict[str, dict[str, str]] = {}
        # The (inode, size, mtime) of the file the rows above were read from or
        # written to; None when they reflect no file, or one too fresh to trust.
        self._seen: tuple[int, int, int] | None = None
        # What the last failed read met (the file's fingerprint, or the error
        # when even the stat failed), so a broken state is logged once.
        self._unreadable: object = None
        self._issued: frozenset[str] | None = None
        st = self._stat()
        if st is not None:
            self._aliases = self._load(path)
            self._seen = self._trusted(st)

    @staticmethod
    def _load(path: str) -> dict[str, dict[str, str]]:
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except OSError as e:
            raise AliasLedgerError(f"alias ledger {path!r} unreadable: {e}") from e
        except json.JSONDecodeError as e:
            raise AliasLedgerError(f"alias ledger {path!r} is not valid JSON: {e}") from e
        if not isinstance(raw, dict) or raw.get("version") != _LEDGER_VERSION:
            raise AliasLedgerError(
                f"alias ledger {path!r}: expected {{\"version\": {_LEDGER_VERSION}, "
                "\"aliases\": {...}}"
            )
        aliases = raw.get("aliases")
        if not isinstance(aliases, dict):
            raise AliasLedgerError(f"alias ledger {path!r}: \"aliases\" must be an object")
        for issuer, subjects in aliases.items():
            if not isinstance(issuer, str) or not issuer:
                raise AliasLedgerError(f"alias ledger {path!r}: empty issuer key")
            if not isinstance(subjects, dict):
                raise AliasLedgerError(
                    f"alias ledger {path!r}: issuer {issuer!r} must map subjects to aliases"
                )
            for subject, alias in subjects.items():
                if not isinstance(subject, str) or not subject:
                    raise AliasLedgerError(
                        f"alias ledger {path!r}: issuer {issuer!r} has an empty subject key"
                    )
                if not isinstance(alias, str) or not _ALIAS_RE.match(alias):
                    raise AliasLedgerError(
                        f"alias ledger {path!r}: alias {alias!r} for issuer {issuer!r} "
                        f"is outside the reserved shape ({ALIAS_PREFIX}<hex>); an alias "
                        "elsewhere in the agent namespace would dodge the boot check "
                        "that keeps subject space and pre-existing agents apart"
                    )
        return aliases

    def _stat(self) -> os.stat_result | None:
        """The file's stat, or None when there is no file to read."""
        try:
            return os.stat(self._path)
        except FileNotFoundError:
            return None
        except OSError as e:
            raise AliasLedgerError(f"alias ledger {self._path!r} unreadable: {e}") from e

    @staticmethod
    def _fingerprint(st: os.stat_result) -> tuple[int, int, int]:
        # No ctime: a rename changes it on some filesystems, and the rename is
        # how this process's own write lands.
        return (st.st_ino, st.st_size, st.st_mtime_ns)

    def _trusted(self, st: os.stat_result) -> tuple[int, int, int] | None:
        """The fingerprint to remember for a file just read, or None to re-read."""
        if time.time_ns() - st.st_mtime_ns < _RACY_WINDOW_NS:
            return None
        return self._fingerprint(st)

    def _refresh(self, *, strict: bool) -> None:
        """Re-read the file if it changed since this process last read or wrote it.

        bug-531: the rows used to be read at startup and re-read only when a
        lookup missed, and that re-read was merged OVER the startup rows. An
        operator's relink of a subject this process already held went unseen
        until some other subject's first sign-in, and a row the operator removed
        stayed live in memory and was written back to the file by the next
        issuance -- so undoing a wrong account link did not take, before or
        after a restart. The file is now the record: a changed file replaces the
        rows wholesale.

        A missing file is not an edit to apply. Removing the whole file is not
        how a row is revoked (the next start would refuse every alias it no
        longer records), so the rows are kept and the next issuance writes them
        out again.

        A file that no longer parses: a write (``strict``) refuses, as at
        startup, because rewriting it from these rows would destroy whatever it
        holds. A read keeps the rows it last read and logs the defect once, so a
        save that is still being typed does not cut every subject off; the edit
        applies once the file parses.
        """
        fingerprint = None
        try:
            st = self._stat()
            if st is None:
                return
            fingerprint = self._fingerprint(st)
            if fingerprint == self._seen:
                return
            rows = self._load(self._path)
        except AliasLedgerError as e:
            if strict:
                raise
            # Logged once per broken state, not on every call that meets it.
            marker = fingerprint if fingerprint is not None else str(e)
            if marker != self._unreadable:
                self._unreadable = marker
                logger.error("%s; keeping the rows last read until the file reads cleanly", e)
            return
        self._aliases = rows
        self._seen = self._trusted(st)
        self._unreadable = None
        self._issued = None

    def peek(self, issuer: str, subject: str) -> str | None:
        """The alias issued for this pair as the file now records it, or None.

        Never issues."""
        self._refresh(strict=False)
        return self._aliases.get(issuer, {}).get(subject)

    def issued_aliases(self) -> frozenset[str]:
        """Every alias this ledger records, across issuers.

        The boot collision check exempts these (bug-267): an alias the ledger
        records is an issued subject space — the server's own prior issuance —
        not a pre-existing agent squatting on the prefix. Without the
        exemption, the first restart after an alias is minted fails the boot
        check on the server's own data. The same set decides, at runtime,
        which alias-shaped agents a write may create rows under, so it is kept
        rather than rebuilt per call.
        """
        self._refresh(strict=False)
        if self._issued is None:
            self._issued = frozenset(
                alias
                for subjects in self._aliases.values()
                for alias in subjects.values()
            )
        return self._issued

    def resolve_or_issue(self, issuer: str, subject: str) -> tuple[str, bool]:
        """The alias for this pair, issuing and persisting one on first sight.

        Returns ``(alias, issued)``; ``issued`` is True only on the call that
        minted it, which is what lets the response surface a fresh issuance to
        the caller while every later call stays quiet. Raises
        ``AliasLedgerError`` when the mint cannot be made durable.
        """
        existing = self.peek(issuer, subject)
        if existing is not None:
            return existing, False
        # A miss is the only case that writes, and the whole-file rewrite below
        # is safe only over rows that are the file's own. This module names the
        # operator as a second writer (manual account linking is an edit to this
        # file), and a second server process over the same path is the other
        # one. The re-read is strict: a file that no longer parses must not be
        # overwritten from these rows. It is also what keeps the freshly minted
        # alias from colliding with one only the file knows.
        self._refresh(strict=True)
        existing = self._aliases.get(issuer, {}).get(subject)
        if existing is not None:
            # Someone else issued for this pair between our last read and now.
            # Theirs is the one on disk, so it is the one that survives a
            # restart — return it rather than minting a rival for the same
            # person, and say we did not issue it, because we did not.
            return existing, False
        taken = {a for subjects in self._aliases.values() for a in subjects.values()}
        while True:
            alias = ALIAS_PREFIX + secrets.token_hex(6)
            if alias not in taken:
                break
        self._aliases.setdefault(issuer, {})[subject] = alias
        self._issued = None
        try:
            self._persist()
        except BaseException as e:
            # Undo the in-memory entry: handing out an alias that survives only
            # in this process would strand the first session's writes behind a
            # different alias after restart.
            #
            # bug-348: this used to catch OSError only, so a persist that failed
            # any other way left the unpersisted alias live in memory and the
            # retry took the cached fast path -- returning it with no error, and
            # authorising the caller under an identity recorded nowhere. That is
            # the loss this module exists to prevent, reached through the one
            # exception class the rollback did not name. What the persist can
            # raise is not a list worth keeping current: the rollback is correct
            # for every way it can fail, so it runs for all of them. An exception
            # that is not an ordinary error (cancellation, interrupt) is still
            # rolled back and then re-raised as itself -- turning it into a
            # ledger error would tell the caller the ledger failed when it did
            # not.
            del self._aliases[issuer][subject]
            if not self._aliases[issuer]:
                del self._aliases[issuer]
            self._issued = None
            if isinstance(e, Exception):
                raise AliasLedgerError(
                    f"alias ledger {self._path!r} could not be written: {e}"
                ) from e
            raise
        logger.info(
            "alias issued: %s for subject %r at issuer %s (ledger %s)",
            alias,
            subject,
            issuer,
            self._path,
        )
        return alias, True

    def _persist(self) -> None:
        """Write the whole ledger atomically (temp file + rename), mode 0600.

        The write is wholesale, so it is only correct directly after the
        re-read in ``resolve_or_issue`` — that is where a second writer's rows
        are picked up, and this method must not be called from anywhere that
        has not just done it. The written file's fingerprint is remembered, so
        the next read does not mistake this process's own write for an edit.

        The ledger maps identities rather than secrets, but it decides whose
        memory space a request reaches, so it gets the same file hygiene the
        grant table is warned toward.
        """
        directory = os.path.dirname(self._path) or "."
        payload = json.dumps(
            {"version": _LEDGER_VERSION, "aliases": self._aliases},
            indent=2,
            sort_keys=True,
        )
        # bug-322: the mode goes through the package's own helper rather than a
        # bare os.fchmod. That attribute does not exist on Windows and this
        # package ships as OS-independent, so the bare call raised AttributeError
        # there -- past the OSError handler below, so the temp file and its
        # descriptor leaked and the caller was authorised under an alias that had
        # never been written. The helper returns quietly where the attribute is
        # absent and logs a filesystem that cannot honour the mode. (The chmod is
        # belt-and-braces either way: a file created through mkstemp is already
        # private by contract.)
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".alias_ledger.")
        owned = True
        try:
            fileperms.tighten(fd, tmp_path)
            f = os.fdopen(fd, "w", encoding="utf-8")
            owned = False  # fdopen took the descriptor; closing it is now f's job
            with f:
                f.write(payload + "\n")
            # Taken before the rename, which keeps inode, size and mtime: the
            # file at the path is then this one until somebody else writes.
            written = os.stat(tmp_path)
            os.replace(tmp_path, self._path)
            self._seen = self._fingerprint(written)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise
        finally:
            if owned:
                with contextlib.suppress(OSError):
                    os.close(fd)
