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


class AliasLedgerError(Exception):
    """A ledger defect — refuse rather than degrade (see module docstring)."""


class AliasLedger:
    """The (issuer, subject) → alias map, loaded once and persisted on issue."""

    def __init__(self, path: str):
        self._path = path
        # issuer → subject → alias. Nested rather than a joined key because a
        # subject is an arbitrary printable string (OIDC allows up to 255 ASCII
        # characters) and no separator can be guaranteed absent from it.
        self._aliases: dict[str, dict[str, str]] = {}
        if os.path.exists(path):
            self._aliases = self._load(path)

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

    def peek(self, issuer: str, subject: str) -> str | None:
        """The alias already issued for this pair, or None. Never issues."""
        return self._aliases.get(issuer, {}).get(subject)

    def issued_aliases(self) -> set[str]:
        """Every alias this ledger records, across issuers.

        The boot collision check exempts these (bug-267): an alias the ledger
        records is an issued subject space — the server's own prior issuance —
        not a pre-existing agent squatting on the prefix. Without the
        exemption, the first restart after an alias is minted fails the boot
        check on the server's own data.
        """
        return {
            alias
            for subjects in self._aliases.values()
            for alias in subjects.values()
        }

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
        # A miss is the one case where the snapshot taken at startup may be out
        # of date, and the only case that writes. Re-read before minting: this
        # module names the operator as a second writer (manual account linking
        # is an edit to this file), and a second server process over the same
        # path is the other one. Merging here rather than at the write is what
        # makes the whole-file rewrite below safe, and it is also what keeps the
        # freshly minted alias from colliding with one only the file knows.
        self._aliases = self._merged_with_disk()
        existing = self.peek(issuer, subject)
        if existing is not None:
            # Someone else issued for this pair between our startup and now.
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

    def _merged_with_disk(self) -> dict:
        """This process's rows, under any row a second writer has since made.

        A row present in both copies resolves to the one on disk. That is safe
        in the only direction it can differ: this process adds a row and
        persists it in the same call, so a row of ours that disagrees with the
        file is not a stale read of our own write — it is somebody else's edit.
        And the edit the operator makes is a change to an existing pair (two
        rows pointed at one alias), not only an addition, so a merge that let
        the snapshot win would still erase the documented workflow.

        A ledger on disk that no longer parses raises, as it does at startup:
        overwriting it with this snapshot would destroy whatever it holds, which
        is the loss this file exists to prevent. Refusing the issuance is the
        posture the module docstring already states for a persist that fails.
        """
        merged = {issuer: dict(subjects) for issuer, subjects in self._aliases.items()}
        if not os.path.exists(self._path):
            return merged
        for issuer, subjects in self._load(self._path).items():
            merged.setdefault(issuer, {}).update(subjects)
        return merged

    def _persist(self) -> None:
        """Write the whole ledger atomically (temp file + rename), mode 0600.

        The write is wholesale, so it is only correct directly after the
        re-read in ``resolve_or_issue`` — that is where a second writer's rows
        are picked up, and this method must not be called from anywhere that
        has not just done it.

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
            os.replace(tmp_path, self._path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise
        finally:
            if owned:
                with contextlib.suppress(OSError):
                    os.close(fd)
